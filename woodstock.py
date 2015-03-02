import re
import copy
import operator
import random
from itertools import chain
_cfi = chain.from_iterable
from collections import defaultdict as dd

try:
    from . import common
    from . import core
except: # "__main__" case
    import common
    import core
from common import timed
    
#_mad = common.MAX_AGE_DEFAULT

class GreedyAreaSelector:
    """
    Selects area for treatment from oldest age classes.
    """
    def __init__(self, parent):
        self.parent = parent

    def operate(self, period, acode, target_area):
        """
        Greedily operate on oldest operable age classes.
        """
        wm = self.parent
        key = lambda item: max(item[1])
        odt = sorted(wm.operable_dtypes(acode, period).items(), key=key)
        print ' entering selector.operate()', len(odt), 'operable dtypes'
        while target_area > 0 and odt:
            while target_area > 0 and odt:
                popped = odt.pop()
                try:
                    dtk, ages = popped #odt.pop()
                except:
                    print odt
                    print popped
                    raise
                age = sorted(ages)[-1]
                oa = wm.dtypes[dtk].operable_area(acode, period, age)
                if not oa: continue # nothing to operate
                area = min(oa, target_area)
                target_area -= area
                #print ' selector found area', acode, period, age, area
                wm.apply_action(dtk, acode, period, age, area)
            odt = sorted(wm.operable_dtypes(acode, period).items(), key=key)
        print ' exiting selector.operate. remaining target_area:', target_area
    
class Action:
    def __init__(self,
                 code,
                 targetage=None,
                 descr='',
                 lockexempt=False,
                 #oper_expr='',
                 components=None,
                 partial=None):
        self.code = code
        self.targetage = targetage
        self.descr = descr
        self.lockexempt = lockexempt
        self.oper_a = None 
        self.oper_p = None
        #self.oper_expr = oper_expr
        self.components = components or []
        self.partial = partial or []
        self.is_compiled = False
    
class DevelopmentType:
    """
    Encapsulates Woodstock development type (curves, age, area).
    """
    _bo = {'AND':operator.and_, '&':operator.and_, 'OR':operator.or_, '|':operator.or_}
    
    def __init__(self,
                 key,
                 parent):
        self.key = key
        self.parent = parent
        self._rc = parent.register_curve # shorthand
        self._max_age = parent.max_age
        self._ycomps = {}
        self._complex_ycomps = {}
        self._zero_curve = parent.common_curves['zero']
        self._unit_curve = parent.common_curves['unit']
        self._ages_curve = parent.common_curves['ages']                           
        self._resolvers = {'MULTIPLY':self._resolver_multiply,
                           'DIVIDE':self._resolver_divide,
                           'SUM':self._resolver_sum,
                           'CAI':self._resolver_cai,
                           'MAI':self._resolver_mai,
                           'YTP':self._resolver_ytp,
                           'RANGE':self._resolver_range}
        self.transitions = {} # keys are (acode, age) tuples
        #######################################################################
        # Use period 0 slot to store starting inventory.
        self._areas = {p:dd(float) for p in range(0, self.parent.horizon+1)}
        #######################################################################
        self.oper_expr = dd(list)
        self.operability = {}

    def operable_ages(self, acode, period):
        if acode not in self.oper_expr: # action not defined for this development type
            return []
        if acode not in self.operability: # action not compiled yet...
            self.compile_action(acode)
        if period not in self.operability[acode]:
            return []
        else:
            lo, hi = self.operability[acode][period]
            return list(set(range(lo, hi+1)).intersection(self._areas[period].keys()))        
    
    def is_operable(self, acode, period, age=None):
        """
        Test hypothetical operability.
        Does not imply that there is any operable area in current inventory.
        """
        if acode not in self.oper_expr: # action not defined for this development type
            return False
        if acode not in self.operability: # action not compiled yet...
            self.compile_action(acode)
        if period not in self.operability[acode]:
            return False
        else:
            lo, hi = self.operability[acode][period]
            return age >= lo and age <= hi
        
    def operable_area(self, acode, period, age=None, cleanup=True):
        """
        Returns 0. if inoperable or no current inventory. 
        """
        if acode not in self.oper_expr: # action not defined for this development type
            return 0.
        if acode not in self.operability: # action not compiled yet...
            self.compile_action(acode)
        #else:
        #    print 'operable_area', ' '.join(self.key), self.operability[acode]
        if age is None: # return total operable area
            return sum(self.operable_area(acode, period, a) for a in self._areas[period].keys())
        if age not in self._areas[period]:
            # age class not in inventory
            return 0.
        elif abs(self._areas[period][age]) < self.parent.area_epsilon:
            # negligible area
            if cleanup: # remove ageclass from dict (frees up memory)
                del self._areas[period][age]
            return 0.
        elif self.is_operable(acode, period, age):
            return self._areas[period][age]
        else:
            return 0.
                
    def area(self, period, age=None, area=None, delta=True):
        if area is None: # return area for period and age
            if age is not None:
                try:
                    return self._areas[period][age]
                except:
                    return 0.
            else: # return total area
                return sum(self._areas[period][a] for a in self._areas[period])
        else: 
            if delta:
                self._areas[period][age] += area
            else:
                self._areas[period][age] = area
        
    def resolve_condition(self, ycomp, lo, hi):
        return [x for x, y in enumerate(self.ycomp(ycomp)) if y >= lo and y <= hi]
       
    def reset_areas(self, period=None):
        if period is None:
            for p in self.parent.periods:
                self.reset_areas(p)
        else:
            self._areas[period] = dd(float)

    def ycomp(self, yname, silent_fail=True):
        if yname in self._ycomps:
            if not self._ycomps[yname]: # complex ycomp not compiled yet
                self._compile_complex_ycomp(yname)
            return self._ycomps[yname]
        else: # not a valid yname
            if silent_fail:
                return None 
            else: 
                 raise KeyError("ycomp '%s' not in development type '%s'" % (yname, ' '.join(self.key)))
                    
    def _o(self, s, default_ycomp=None): # resolve string operands
        if not default_ycomp: default_ycomp = self._zero_curve
        if common.is_num(s):
            return float(s)
        elif s.startswith('#'):
            return self.parent.constants[s[1:]]
        else:
            s = s.lower() # just to be safe
            ycomp = self.ycomp(s)
            return ycomp if ycomp else default_ycomp
        
    def _resolver_multiply(self, yname, d):
        args = [self._o(s.lower()) for s in re.split('\s?,\s?', re.search('(?<=\().*(?=\))', d).group(0))]
        ##################################################################################################
        # NOTE: Not consistent with Remsoft documentation on 'complex-compound yields' (fix me)...
        ytype_set = set(a.type for a in args if isinstance(a, core.Curve))
        return ytype_set.pop() if len(ytype_set) == 1 else 'c', self._rc(reduce(lambda x, y: x*y, args))
        ##################################################################################################

    def _resolver_divide(self, yname, d):
        _tmp = zip(re.split('\s?,\s?', re.search('(?<=\().*(?=\))', d).group(0)),
                   (self._zero_curve, self._unit_curve))
        args = [self._o(s, default_ycomp) for s, default_ycomp in _tmp]
        return args[0].type if not args[0].is_special else args[1].type, self._rc(args[0] / args[1])
        
    def _resolver_sum(self, yname, d):
        args = [self._o(s.lower()) for s in re.split('\s?,\s?', re.search('(?<=\().*(?=\))', d).group(0))] 
        ytype_set = set(a.type for a in args if isinstance(a, core.Curve))
        return ytype_set.pop() if len(ytype_set) == 1 else 'c', self._rc(reduce(lambda x, y: x+y, [a for a in args]))
        
    def _resolver_cai(self, yname, d):
        arg = self._o(re.split('\s?,\s?', re.search('(?<=\().*(?=\))', d).group(0))[0])
        return arg.type, self._rc(arg.mai())
        
    def _resolver_mai(self, yname, d):
        arg = self._o(re.split('\s?,\s?', re.search('(?<=\().*(?=\))', d).group(0))[0])
        return arg.type, self._rc(arg.mai())
        
    def _resolver_ytp(self, yname, d):
        arg = self._o(re.search('(?<=\().*(?=\))', d).group(0).lower())
        return arg.type, self._rc(arg.ytp())
        
    def _resolver_range(self, yname, d):
        args = [self._o(s.lower()) for s in re.split('\s?,\s?', re.search('(?<=\().*(?=\))', d).group(0))] 
        arg_triplets = [args[i:i+3] for i in xrange(0, len(args), 3)]
        return args[0].type, self._rc(reduce(lambda x, y: x*y, [t[0].range(t[1], t[2]) for t in arg_triplets]))

    def _compile_complex_ycomp(self, yname):
        expression = self._complex_ycomps[yname]
        keyword = re.search('(?<=_)[A-Z]+(?=\()', expression.group(0))
        try:
            ytype, ycomp = self._resolvers[keyword](yname, expression)
            ycomp.label = yname
            ycomp.type = ytype
            self._ycomps[yname] = ycomp 
        except KeyError:
                raise ValueError('Problem compileing complex yield: %s, %s' % (yname, expression))
            
    def compile_actions(self, verbose=False):
        for acode in self.oper_expr:
            self.compile_action(acode, verbose)

    def compile_action(self, acode, verbose=False):
        self.operability[acode] = {}
        for expr in self.oper_expr[acode]:
            self._compile_oper_expr(acode, expr, verbose)
        is_operable = False
        for p in self.operability[acode]:
            if self.operability[acode][p] is not None:
                #print 'compile_action', expr, acode, p, self.operability[acode][p]
                is_operable = True
        if not is_operable:
            del self.operability[acode]
            
    def _compile_oper_expr(self, acode, expr, verbose=False):
        expr = expr.replace('&', 'and').replace('|', 'or')
        oper = None
        plo, phi = 1, self.parent.horizon # count periods from 1, as in Woodstock...
        alo, ahi = 0, self._max_age 
        if 'and' in expr:
            oper = 'and'
        elif 'or' in expr:
            oper = 'or'
            alo, ahi = self._max_age+1, -1
        cond_comps = expr.split(' %s ' % oper)
        lhs, rel_operators, rhs = zip(*[cc.split(' ') for cc in cond_comps])
        rhs = map(float, rhs)
        _plo, _phi, _alo, _ahi = None, None, None, None
        for i, o in enumerate(lhs):
            if o == '_cp':
                #print 'rhs', rhs
                period = int(rhs[i])
                assert period <= self.parent.horizon # sanity check
                #################################################################
                # Nonsense to relate time-based and age-based conditions with OR?
                # Recondider if this actually ever comes up...
                assert oper != 'or'  
                #################################################################
                if rel_operators[i] == '=':
                    _plo, _phi = period, period
                elif rel_operators[i] == '>=':
                    _plo = period
                elif rel_opertors[i] == '<=':
                    _phi = period
                else:
                    raise ValueError('Bad relational operator.')
                plo, phi = max(_plo, plo), min(_phi, phi)
            elif o == '_age':
                age = int(rhs[i])
                if rel_operators[i] == '=':
                    _alo, _ahi = age, age
                elif rel_operators[i] == '>=':
                    _alo = age
                elif rel_operators[i] == '<=':
                    _ahi = age
                else:
                    raise ValueError('Bad relational operator.')                    
            else: # must be yname
                ycomp = self.ycomp(o)
                if rel_operators[i] == '=':
                    _alo = _ahi = ycomp.lookup(rhs[i])
                elif rel_operators[i] == '>=':
                    #print ' ge', o, ycomp[45], ycomp.lookup(0)  
                    _alo = ycomp.lookup(rhs[i])
                elif rel_operators[i] == '<=':
                    #print ' le', o 
                    _ahi = ycomp.lookup(rhs[i])
                else:
                    raise ValueError('Bad relational operator.')
                #print ' ', o, (alo, _alo), (ahi, _ahi)
            if oper == 'and':
                if _alo is not None: alo = max(_alo, alo)
                if _ahi is not None: ahi = min(_ahi, ahi)
            else: # or
                if _alo is not None: alo = min(_alo, alo)
                if _ahi is not None: ahi = max(_ahi, ahi)
        if plo > phi:
            print plo, phi
            assert plo <= phi # should never explicitly declare infeasible period range...
        for p in range(plo, phi+1):
            self.operability[acode][p] = (alo, ahi) if alo <= ahi else None 
                
    def add_ycomp(self, ytype, yname, ycomp, first_match=True):
        if first_match and yname in self._ycomps: return # already exists (reject)
        if ytype == 'c':
            self._complex_ycomps[yname] = ycomp
            self._ycomps[yname] = None
        if isinstance(ycomp, core.Curve):
            self._ycomps[yname] = ycomp
    
    def grow(self, start_period=1, cascade=True):
        end_period = start_period + 1 if not cascade else self.parent.horizon
        for p in range(start_period, end_period): #self.parent.periods[start_period:end_period]:
            self.reset_areas(p+1)
            for age, area in self._areas[p].items(): self._areas[p+1][age+1] = area

    def initialize_areas(self):
        self._areas[1] = copy.copy(self._areas[0])
        
class Output:
    def __init__(self,
                 parent,
                 code=None,
                 expression=None,
                 factor=(1., 1),
                 description='',
                 theme_index=-1,
                 is_basic=False,
                 is_level=False):
        self.parent = parent
        self.code = code
        self.expression = expression
        self._factor = factor
        self.description = description
        self.theme_index = theme_index
        self.is_themed = True if theme_index > -1 else False 
        self.is_basic = is_basic
        if is_basic:
            self._compile_basic(expression) # shortcut
        elif not is_level:
            self._compile(expression) # will detect is_basic
        self.is_level = is_level

    def _lval(self, s):
        """
        Resolve left operand in sub-expression.
        """
        if s.lower() in self.parent.outputs:
            return self.parent.outputs[s.lower()]
        else: # expression
            return s.lower()

    def _rval(self, s): 
        """
        Resolve right operand in sub-expression.
        """
        if common.is_num(s):
            return float(s)
        elif s.startswith('#'):
            return self.parent.constants[s[1:].lower()]
        else: # time-based ycomp code
            return s.lower()
            
    def _compile(self, expression):
        """
        Resolve operands in expression to the extent possible.
        Can be basic or summary.
        Assuming operand pattern:
          lval_1 [*|/ rval_1] +|- .. +|- lval_n [*|/ rval_n]
        where
          lval := ocode or expression
          rval := number or #constant or ycomp
        """
        t = re.split(r'\s+(\+|-)\s+', expression)
        ocomps = t[::2]  # output component sub-expressions
        signs = [1.] # implied + in front of expression
        signs.extend(1. if s == '+' else -1 for s in t[1::2]) 
        factors = [(1., 1) for i in ocomps]
        for i, s in enumerate(ocomps):
            tt = re.split(r'\s+(\*|/)\s+', s) # split on */ operator
            lval = self._lval(tt[0])
            if len(tt) > 1:
                factors[i] = self._rval(tt[2]), 1 if tt[1] == '*' else -1
            if not isinstance(lval, Output):     
                if len(ocomps) == 1: # simple basic output (special case)
                    self.is_basic = True
                    self._factor = factors[0]
                    self._compile_basic(lval)
                    return
                else: # compound basic output
                    ocomps[i] = Output(parent=self.parent,
                                       expression=lval,
                                       factor=factors[i],
                                       is_basic=True)
            else: # summary output
                ocomps[i] = lval #self.parent.outputs[lval]
        self._ocomps = ocomps
        self._signs = signs
        self._factors = factors

    def _compile_basic(self, expression):
        # clean up (makes parsing easier)
        s = re.sub('\s+', ' ', expression) # separate tokens by single space
        s = s.replace(' (', '(')  # remove space to left of left parentheses
        t = s.lower().split(' ')
        # filter dtypes, if starts with mask
        mask = None
        if not (t[0] == '@' or t[0] == '_' or t[0] in self.parent.actions):
            mask = tuple(t[:self.parent.nthemes])
            t = t[self.parent.nthemes:] # pop
        self._dtype_keys = self.parent.unmask(mask) if mask else self.parent.dtypes.keys()
        # extract @AGE or @YLD condition, if present
        self._ages = None
        self._condition = None
        if t[0].startswith('@age'):
            lo, hi = [int(a)+i for i, a in enumerate(t[0][5:-1].split('..'))]
            hi = min(hi, self.parent.max_age+1) # they get carried away with range bounds...
            self._ages = range(lo, hi)
            t = t[1:] # pop
        elif t[0].startswith('@yld'):
            ycomp, args = t[0][5:-1].split(',')
            self._condition = tuple([ycomp] + [float(a) for a in args.split('..')])
            self._ages = None
            t = t[1:] # pop
        if not self._ages and not self._condition: self._ages = self.parent.ages
        # extract _INVENT or acode
        if t[0].startswith('_'): # _INVENT
            self._is_invent = True
            self._invent_acodes = t[0][8:-1].split(',') if len(t[0]) > 7 else None
            self._acode = None
        else: # acode
            self._is_invent = False
            self._invent_acodes = None
            self._acode = t[0]
        t = t[1:] # pop
        # extract _AREA or ycomp
        if t[0].startswith('_'): # _AREA
            self._is_area = True
            self._ycomp = None
        else: # acode
            self._is_area = False
            self._ycomp = t[0]
        t = t[1:] # pop

    def _evaluate_basic(self, period, factors, verbose=0, cut_corners=True):
        result = 0.
        if self._invent_acodes:
            acodes = [acode for acode in self._invent_acodes if parent.applied_actions[period][acode]]
            if cut_corners and not acodes:
                return 0. # area will be 0...
        for k in self._dtype_keys:
            dt = self.parent.dtypes[k]
            if cut_corners and not self._is_invent and not self.parent.applied_actions[period][self._acode][k]:
                if verbose: print 'bailing on', period, self._acode, ' '.join(k)
                continue # area will be 0...
            if isinstance(self._factor[0], float):
                f = pow(*self._factor)
            else:
                f = pow(dt.ycomp(self._factor[0])[period], self._factor[1])
            for factor in factors:
                if isinstance(factor[0], float):
                    f *= pow(*factor)
                else:
                    f *= pow(dt.ycomp(factor[0])[period], factor[0])
            if cut_corners and not f:
                if verbose: print 'f is null', f
                continue # one of the factors is 0, no point calculating area...
            ages = self._ages if not self._condition else dt.resolve_condition(*self._condition)
            for age in ages:
                area = 0.
                if self._is_invent:
                    if cut_corners and not dt.area(period, age):
                        continue
                    if self._invent_acodes:
                        any_operable = False
                        for acode in acodes:
                            if acode not in dt.operability: continue
                            if dt.is_operable(acode, period, age):
                                any_operable = True
                        if any_operable:
                            area += dt.area(period, age)
                    else:
                        area += dt.area(period, age)
                else:
                    assert False # not implemented correctly yet...
                    aa = self.parent.applied_actions
                    key = k, self._acode, age
                    if key in aa: area += aa[key]
                y = 1. if self._is_area else dt.ycomp(self._ycomp)[age]
                result += y * area * f
        return result

    def _evaluate_summary(self, period, factors):
        result = 0.
        for i, ocomp in enumerate(self._ocomps):
            result += ocomp(period, [self._factors[i]] + factors)
        return result

    def _evaluate_basic_themed(self, period):
        pass

    def _evaluate_summed_themed(self, period):
        pass
            
    def __call__(self, period, factors=[(1., 1)]):
        if self.is_basic:
            return self._evaluate_basic(period, factors)
        else:
            return self._evaluate_summary(period, factors)

    def __add__(self, other):
        # assume Output + Output
        if self.is_themed:
            return [i + j for i, j in zip(self(), other())]
        else:
            return self() + other()

    def __sub__(self, other):
        # assume Output - Output 
        if self.is_themed:
            return [i - j for i, j in zip(self(), other())]
        else:
            return self() - other()

class WoodstockModel:
    """
    Interface to import Woodstock models.
    """
    _ytypes = {'*Y':'a', '*YT':'t', '*YC':'c'}
    tree = (lambda f: f(f))(lambda a: (lambda: dd(a(a))))
        
    def __init__(self,
                 model_name,
                 model_path,
                 horizon=common.HORIZON_DEFAULT,
                 period_length=common.PERIOD_LENGTH_DEFAULT,
                 max_age=common.MAX_AGE_DEFAULT,
                 species_groups=common.SPECIES_GROUPS_WOODSTOCK_QC,
                 area_epsilon=common.AREA_EPSILON_DEFAULT):
        self.model_name = model_name
        self.model_path = model_path
        self.horizon = horizon
        self.periods = range(1, horizon+1)
        self.period_length = period_length
        self.max_age = max_age
        self.ages = range(max_age+1)
        self._species_groups = species_groups
        self.yields = []
        self.actions = {}
        self.transitions = {}
        self.oper_expr = {}
        self._themes = []
        self._theme_basecodes = []
        self.dtypes = {}
        self.constants = {}
        self.output_groups = {}
        self.outputs = {}        
        self.reset_actions()
        self.curves = {}
        c_zero = self.register_curve(core.Curve('zero',
                                                is_special=True,
                                                type=''))
        c_unit = self.register_curve(core.Curve('unit',
                                                points=[(0, 1)],
                                                is_special=True,
                                                type=''))
        c_ages = self.register_curve(core.Curve('ages',
                                                points=[(0, 0), (max_age, max_age)],
                                                is_special=True,
                                                type='')) 
        self.common_curves = {'zero':c_zero,
                              'unit':c_unit,
                              'ages':c_ages}
        self.area_epsilon = area_epsilon
        self.areaselector = GreedyAreaSelector(self)

    def operable_dtypes(self, acode, period, mask=None):
        result = {}
        dtype_keys = self.unmask(mask) if mask else self.dtypes.keys()
        for dtk in dtype_keys:
            dt = self.dtypes[dtk]
            operable_ages = dt.operable_ages(acode, period)
            if operable_ages:
                result[dt.key] = operable_ages
        return result

    def inventory(self, period, yname=None, age=None, mask=None):
        result = 0.
        dtype_keys = self.unmask(mask) if mask else self.dtypes.keys()
        for dtk in dtype_keys:
            dt = self.dtypes[dtk]
            if yname:
                ycomp = dt.ycomp(yname)
                if age:
                    result += dt.area(period, age) * ycomp[age]
                else:
                    result += sum(dt.area(period, a) * ycomp[a] for a in dt._areas[period])
            else:
                result += dt.area(period, age)
        return result
        
    def operable_area(self, acode, period, age=None):
        return sum(dt.operable_area(acode, period, age) for dt in self.dtypes.values())
        
    def initialize_areas(self):
        """
        Copies areas from period 0 to period 1.
        """
        for dt in self.dtypes.values(): dt.initialize_areas()
        
    def register_curve(self, curve):
        key = tuple(curve.points())
        if key not in self.curves:
            # new curve (lock and register)
            curve.is_locked = True # points list must not change, else not valid key
            self.curves[key] = curve
        return self.curves[key]
            
        
    def _rdd(self):
        """
        Recursive defaultdict (i.e., tree)
        """
        return dd(self._rdd)
        
    def reset_actions(self, period=None, acode=None):
        if period is None:
            self.applied_actions = {p:self._rdd() for p in self.periods}
        else:
            if acode is None:
                # NOTE: This DOES NOT deal with consequences in future periods...
                self.applied_actions[period] = self._rdd()
            else:
                assert period is not None
                self.applied_actions[period][acode] = self._rdd()

    def operated_area(self, acode, period, dtype_key=None, age=None):
        aa = self.applied_actions
        if acode not in aa[period]: return 0.
        result = 0.
        if dtype_key is None: 
            if age is None:
                for dtype_key in aa[period][acode]:
                    for age in aa[period][acode][dtype_key]:
                        result += aa[period][acode][dtype_key][age]
            else:
                for dtype_key in aa[period][acode]:
                   result += aa[period][acode][dtype_key][age]
        else:
            if age is None:
                for age in aa[period][acode][dtype_key]:
                    result += aa[period][acode][dtype_key][age]
            else:
                result += aa[period][acode][dtype_key][age]
        return result

    def repair_actions(self, period, areaselector=None):
        """
        Attempts to repair the action schedule for given period.
        """
        if areaselector is None: # use default (greedy) selector
            areaselector = self.areaselector
        aa = copy.copy(self.applied_actions[period])
        self.reset_actions(period)
        for acode in aa:
            print ' ', acode
            old_area = 0.
            new_area = 0.
            # start by re-applying as much of the old solution as possible
            for dtype_key in aa[acode]:
                for age in aa[acode][dtype_key]:
                    aaa = aa[acode][dtype_key][age]
                    old_area += aaa
                    oa = self.dtypes[dtype_key].operable_area(acode, period, age)
                    if not oa: continue
                    applied_area = min(aaa, oa)
                    #print ' applying old area', applied_area
                    new_area += applied_area
                    self.apply_action(dtype_key, acode, period, age, applied_area)
                    #self.applied_actions[period][acode][dtype_key][age] = applied_area
            # try to make up for missing area...
            target_area = old_area - new_area
            print ' patched %i of %i solution hectares, missing' % (int(new_area), int(old_area)), target_area
            if areaselector is None: # use default area selector
                areaselector = self.areaselector
            areaselector.operate(period, acode, target_area)
                     
        
    def commit_actions(self, period=1, repair_future_actions=False, verbose=True):
        while period < self.horizon:
            if verbose: print 'growing period', period
            self.grow(period, cascade=False)
            period += 1
            if repair_future_actions:
                if verbose: print 'repairing actions in period', period
                self.repair_actions(period)
            else:
                self.reset_actions(period)
                            
    def apply_action(self, dtype_key, acode, period, age, area):
        dt = self.dtypes[dtype_key]
        # TO DO: better error handling... ##########
        assert acode in dt.operability
        assert dt.area(period, age) >= area
        assert area > 0
        ############################################
        if not dt.is_operable(acode, period, age): return
        action = self.actions[acode]
        #if not dt.actions[acode].is_compiled: dt.compile_action(acode)
        def resolve_replace(dtk, expr):
            # HACK ####################################################################
            # Too lazy to implement all the use cases.
            # This should work OK for BFEC models (TO DO: confirm).
            tokens = re.split('\s+', expr)
            i = int(tokens[0][3]) - 1
            try:
                return str(eval(expr.replace(tokens[0], dtk[i])))
            except:
                print 'source', ' '.join(dtype_key)
                print 'target', ' '.join(tmask), tprop, tage, tlock, treplace, tappend
                print 'dtk', ' '.join(dtk)
                raise
            ###########################################################################
        def resolve_append(dtk, expr):
            # HACK ####################################################################
            # Too lazy to implement.
            # Not used in BFEC models (TO DO: confirm).
            assert False # brick wall (deal with this case later, as needed)
            ###########################################################################
        dt.area(period, age, -area)
        for target in dt.transitions[acode, age]:
            tmask, tprop, tage, tlock, treplace, tappend = target # unpack tuple
            dtk = list(dtype_key) # start with source key
            ###########################################################################
            # DO TO: Confirm correct order for evaluating mask, _APPEND and _REPLACE...
            dtk = [t if tmask[i] == '?' else tmask[i] for i, t in enumerate(dtk)] 
            if treplace: dtk[treplace[0]] = resolve_replace(dtk, treplace[1])
            if tappend: dtk[tappend[0]] = resolve_append(dtk, tappend[1])
            dtk = tuple(dtk)
            ###########################################################################
            if dtk not in self.dtypes: # new development type (clone source type)
                self.create_dtype_fromkey(dtk)
            if tage is not None: # target age override specifed in transition
                targetage = tage
            elif action.targetage is None: # use source age
                targetage = age
            else: # default: age reset to 0
                targetage = 0
            #print 'new dt', [' '.join(dtk)], period, targetage, area, tprop, area*tprop
            self.dtypes[dtk].area(period, targetage, area*tprop)
        aa = self.applied_actions[period][acode][dtype_key][age]
        if not aa: self.applied_actions[period][acode][dtype_key][age] = 0. 
        self.applied_actions[period][acode][dtype_key][age] += area

    def create_dtype_fromkey(self, key):
        assert key not in self.dtypes # should not be creating new dtypes from existing key
        dt = DevelopmentType(key, self)
        self.dtypes[key] = dt
        # assign yields
        for mask, t, ycomps in self.yields:
            if self.match_mask(mask, key):
                for yname, ycomp in ycomps:
                    dt.add_ycomp(t, yname, ycomp)
        # assign actions and transitions
        for acode in self.oper_expr:
            for mask in self.oper_expr[acode]:
                if self.match_mask(mask, key):
                    dt.oper_expr[acode].append(self.oper_expr[acode][mask]) 
            for mask in self.transitions[acode]:
                if self.match_mask(mask, key):
                    for scond in self.transitions[acode][mask]:
                        for x in self.resolve_condition(scond, key): 
                            dt.transitions[acode, x] = self.transitions[acode][mask][scond] 
       #return dt
    
    def _resolve_outputs_buffer(self, s, for_flag=None):
        n = self.nthemes
        group = 'no_group' # outputs declared at top of file assigned to 'no_group'
        self.output_groups[group] = set()
        ocode = ''
        buffering_for = False
        s = re.sub(r'\{.*?\}', '', s, flags=re.M|re.S) # remove curly-bracket comments
        for l in re.split(r'[\r\n]+', s, flags=re.M|re.S):
            if re.match('^\s*(;|$)', l): continue # skip comments and blank lines
            if buffering_for:
                if l.strip().startswith('ENDFOR'):
                    for i in range(for_lo, for_hi+1):
                        ss = '\n'.join(for_buffer).replace(for_var, str(i))
                        self._resolve_outputs_buffer(ss, for_flag=i)
                    buffering_for = False
                    continue
                else:
                    for_buffer.append(l)
                    continue
            l = re.sub('\s+', ' ', l) # separate tokens by single space
            l = l.strip().partition(';')[0].strip()
            l = l.replace(' (', '(')  # remove space to left of left parentheses
            t = l.lower().split(' ')
            ##################################################
            # HACK ###########################################
            # substitute ugly symbols have in ocodes...
            l = l.replace(r'%', 'p')
            l = l.replace(r'$', 's')
            ##################################################
            tokens = l.lower().split(' ')
            if l.startswith('*GROUP'):
                keyword = 'group'
                group = tokens[1].lower()
                self.output_groups[group] = set()
            elif l.startswith('FOR'):
                # pattern matching may not be very robust, but works for now with:
                # 'FOR XX := 1 to 99'
                # TO DO: implement DOWNTO, etc.
                for_var = re.search(r'(?<=FOR\s).+(?=:=)', l).group(0).strip()
                for_lo = int(re.search(r'(?<=:=).+(?=to)', l).group(0))
                for_hi = int(re.search(r'(?<=to).+', l).group(0))
                for_buffer = []
                buffering_for = True
                continue
            if l.startswith('*OUTPUT') or l.startswith('*LEVEL'):
                keyword = 'output' if l.startswith('*OUTPUT') else 'level'
                if ocode: # flush data collected from previous lines
                    self.outputs[ocode] = Output(parent=self,
                                                 code=ocode,
                                                 expression=expression,
                                                 description=description,
                                                 theme_index=theme_index)
                tt = tokens[1].split('(')
                ocode = tt[0]
                theme_index = tt[1][3:-1] if len(tt) > 1 else None
                description = ' '.join(tokens[2:])
                expression = ''
                self.output_groups[group].add(ocode)
                if keyword == 'level':
                    self.outputs[ocode] = Output(parent=self,
                                                 code=ocode,
                                                 expression=expression,
                                                 description=description,
                                                 theme_index=theme_index,
                                                 is_level=True)
                    ocode = ''
            elif l.startswith('*SOURCE'):
                keyword = 'source'
                expression += l[8:]
            elif keyword == 'source': # continuation line of SOURCE expression
                expression += ' '
                expression += l       
        
    @timed
    def import_outputs_section(self, filename_suffix='out'):
        with open('%s/%s.%s' % (self.model_path, self.model_name, filename_suffix)) as f:
            s = f.read()
        self._resolve_outputs_buffer(s)
            
    @timed
    def import_landscape_section(self, filename_suffix='lan'):
        with open('%s/%s.%s' % (self.model_path, self.model_name, filename_suffix)) as f:
            data = f.read()
        _data = re.search(r'\*THEME.*', data, re.M|re.S).group(0) # strip leading junk
        t_data = re.split(r'\*THEME.*\n', _data)[1:] # split into theme-wise chunks
        for ti, t in enumerate(t_data):
            self._themes.append({})
            self._theme_basecodes.append([])
            defining_aggregates = False
            for l in [l for l in t.split('\n') if not re.match('^\s*(;|{|$)', l)]: 
                if re.match('^\s*\*AGGREGATE', l): # aggregate theme attribute code
                    tac = re.split('\s+', l.strip())[1].lower()
                    self._themes[ti][tac] = []
                    defining_aggregates = True
                    continue
                if not defining_aggregates: # line defines basic theme attribute code
                    tac = re.search('\S+', l.strip()).group(0).lower()
                    self._themes[ti][tac] = tac
                    self._theme_basecodes[ti].append(tac)
                else: # line defines aggregate values (parse out multiple values before comment)
                    _tacs = [_tac.lower() for _tac in re.split('\s+', l.strip().partition(';')[0].strip())]
                    self._themes[ti][tac].extend(_tacs)
        self.nthemes = len(self._themes)

    def theme_basecodes(self, theme_index):
        return self._themes[theme_index]
        
    @timed    
    def import_areas_section(self, filename_suffix='are'):
        n = self.nthemes
        with open('%s/%s.%s' % (self.model_path, self.model_name, filename_suffix)) as f:
            for l in f:
                if re.match('^\s*(;|$)', l): continue # skip comments and blank lines
                l = l.strip().partition(';')[0] # strip leading whitespace and trailing comments
                t = re.split('\s+', l)
                key = tuple(_t.lower() for _t in t[1:n+1])
                age = int(t[n+1])
                area = float(t[n+2].replace(',', ''))
                if key not in self.dtypes: self.dtypes[key] = DevelopmentType(key, self)
                self.dtypes[key].area(0, age, area)
                    
    def _expand_action(self, c):
        self._actions = t
        return [c] if t[c] == c else list(_cfi(self._expand_action(t, c) for c in t[c]))
                
    def _expand_theme(self, t, c): # depth-first search recursive aggregate theme code expansion
        return [c] if t[c] == c else list(_cfi(self._expand_theme(t, c) for c in t[c]))

    def match_mask(self, mask, key):
        """
        Returns True if key matches mask.
        """
        #dt = self.dtypes[key]
        for ti, tac in enumerate(mask):
            if tac == '?': continue # wildcard matches all keys
            tacs = self._expand_theme(self._themes[ti], tac)
            if key[ti] not in tacs: return False # reject key
        return True # key matches
               
    def unmask(self, mask):
        """
        Iteratively filter list of development type keys using mask values.
        """
        dtype_keys = copy.copy(self.dtypes.keys()) # filter this
        for ti, tac in enumerate(mask):
            if tac == '?': continue # wildcard matches all
            tacs = self._expand_theme(self._themes[ti], tac)
            dtype_keys = [dtk for dtk in dtype_keys if dtk[ti] in tacs] # exclude bad matches
        return dtype_keys

    @timed                            
    def import_constants_section(self, filename_suffix='con'):
        with open('%s/%s.%s' % (self.model_path, self.model_name, filename_suffix)) as f:
            for lnum, l in enumerate(f):
                if re.match('^\s*(;|$)', l): continue # skip comments and blank lines
                l = l.strip().partition(';')[0].strip() # strip leading whitespace, trailing comments
                t = re.split('\s+', l)
                self.constants[t[0].lower()] = float(t[1])

    @timed        
    def import_yields_section(self, filename_suffix='yld', verbose=False):
        ###################################################
        # local utility functions #########################
        def flush_ycomps(t, m, n, c):
            #self.ycomps.update(n)
            if t == 'a': # age-based ycomps
                _c = lambda y: self.register_curve(core.Curve(y,
                                                              points=c[y],
                                                              type='a'))
                ycomps = [(y, _c(y)) for y in n]
            elif t == 't': # time-based ycomps (skimp on x range)
                _c = lambda y: self.register_curve(core.Curve(y,
                                                              points=c[y],
                                                              type='t',
                                                              max_x=self.horizon))
                ycomps = [(y, _c(y)) for y in n]
            else: # complex ycomps
                ycomps = [(y, c[y]) for y in n]
            self.yields.append((m, t, ycomps)) # stash for creating new dtypes at runtime...
            for k in self.unmask(m):
                for yname, ycomp in ycomps:
                    self.dtypes[k].add_ycomp(t, yname, ycomp)
        ###################################################
        n = self.nthemes
        ytype = ''
        mask = ('?',) * self.nthemes
        ynames = []
        data = None
        with open('%s/%s.%s' % (self.model_path, self.model_name, filename_suffix)) as f:
            for lnum, l in enumerate(f):
                if re.match('^\s*(;|$)', l): continue # skip comments and blank lines
                l = l.strip().partition(';')[0].strip() # strip leading whitespace and trailing comments
                t = re.split('\s+', l)
                if t[0].startswith('*Y'): # new yield definition
                    newyield = True
                    flush_ycomps(ytype, mask, ynames, data) # apply yield from previous block
                    ytype = self._ytypes[t[0]]
                    mask = tuple(_t.lower() for _t in t[1:])
                    if verbose: print lnum, ' '.join(mask)
                    continue
                if newyield:
                    if t[0] == '_AGE':
                        is_tabular = True
                        ynames = [_t.lower() for _t in t[1:]]
                        data = {yname:[] for yname in ynames}
                        newyield = False
                        continue
                    else:
                        is_tabular = False
                        ynames = []
                        data = {}
                        newyield = False
                if is_tabular:
                    x = int(t[0])
                    for i, yname in enumerate(ynames):
                        data[yname].append((x, float(t[i+1])))
                else:
                    if ytype in 'at': # standard or time-based yield (extract xy values)
                        if not common.is_num(t[0]): # first line of row-based yield component
                            yname = t[0].lower()
                            ynames.append(yname)
                            data[yname] = [(i+int(t[1]), float(t[i+2])) for i in range(len(t)-2)]
                        else: # continuation of row-based yield compontent
                            x_last = data[yname][-1][0]
                            data[yname].extend([(i+x_last+1, float(t[i])) for i in range(len(t))])
                    else:
                        yname = t[0].lower()
                        ynames.append(yname)
                        data[yname] = t[1] # complex yield (defer interpretation) 
        flush_ycomps(ytype, mask, ynames, data)

    @timed        
    def import_actions_section(self, filename_suffix='act'):
        n = self.nthemes
        actions = {}
        #oper = {}
        aggregates = {}
        partials = {}
        keyword = ''
        with open('%s/%s.%s' % (self.model_path, self.model_name, filename_suffix)) as f: s = f.read().lower()
        s = re.sub(r'\{.*?\}', '', s, flags=re.M|re.S) # remove curly-bracket comments
        for l in re.split(r'[\r\n]+', s, flags=re.M|re.S):
            if re.match('^\s*(;|$)', l): continue # skip comments and blank lines
            l = l.strip().partition(';')[0].strip() # strip leading whitespace and trailing comments
            l = re.sub('\s+', ' ', l) # separate tokens by single space
            tokens = l.split(' ')
            if l.startswith('*action'): 
                keyword = 'action'
                acode = tokens[1]
                targetage = 0 if tokens[2] == 'Y' else None
                descr = ' '.join(tokens[3:])
                lockexempt = '_lockexempt' in tokens
                self.actions[acode] = Action(acode, targetage, descr, lockexempt)
                self.oper_expr[acode] = {}
            elif l.startswith('*operable'):
                keyword = 'operable'
                acode = tokens[1]
            elif l.startswith('*aggregate'):
                keyword = 'aggregate'
                acode = tokens[1]
                self.actions[acode] = Action(acode)
            elif l.startswith('*partial'): 
                keyword = 'partial'
                acode = tokens[1]
                partials[acode] = []
            else: # continuation of OPERABLE, AGGREGATE, or PARTIAL block
                if keyword == 'operable':
                    self.oper_expr[acode][tuple(tokens[:n])] = ' '.join(tokens[n:])
                elif keyword == 'aggregate':
                    self.actions[acode].components.extend(tokens)
                elif keyword == 'partial':
                    self.actions[acode].partial.extend(tokens)
        for acode, a in self.actions.items():
            if a.components: continue # aggregate action, skip
            for mask, expression in self.oper_expr[acode].items():
                for k in self.unmask(mask):
                    #if acode == 'act1': print ' '.join(k), acode, expression
                    self.dtypes[k].oper_expr[acode].append(expression)

    def resolve_treplace(self, dt, treplace):
        if '_TH' in treplace: # assume incrementing integer theme value
            i = int(re.search('(?<=_TH)\w+', treplace).group(0))
            return eval(re.sub('_TH%i'%i, str(dt.key[i-1]), treplace))
        else:
            assert False # many other possible arguments (see Woodstock documentation)

    def resolve_tappend(self, dt, tappend):
        assert False # brick wall (not implemented yet)

    def resolve_tmask(self, dt, tmask, treplace, tappend):
        key = list(dt.key)
        if treplace:
            key[treplace[0]] = resolve_treplace(dt, treplace[1])
        if tappend:
            key[tappend[0]] = resolve_tappend(dt, tappend[1])
        for i, val in enumerate(tmask):
            if theme == '?': continue # wildcard (skip it)
            key[i] = val
        return tuple(key)

    def resolve_condition(self, condition, dtype_key=None):
        """
        Evaluate @AGE or @YLD condition.
        Returns list of ages.
        """
        if not condition:
            return self.ages
        elif condition.startswith('@AGE'):
            lo, hi = [int(a) for a in condition[5:-1].split('..')]
            return range(lo, hi+1)
        elif condition.startswith('@YLD'):
            args = re.split('\s?,\s?', condition[5:-1])
            ycomp = args[0].lower()
            lo, hi = [float(y) for y in args[1].split('..')]
            return self.dtypes[dtype_key].resolve_condition(ycomp, hi, lo)
        
    @timed                        
    def import_transitions_section(self, filename_suffix='trn'):
        # local utility function ####################################
        def flush_transitions(acode, sources):
            if not acode: return # nothing to flush on first loop
            self.transitions[acode] = {}
            for smask, scond in sources:
                # store transition data for future dtypes creation 
                if smask not in self.transitions[acode]:
                    self.transitions[acode][smask] = {}
                #if scond not in self.transitions[acode][smask]:
                #    self.transitions[acode][smask][scond] = []
                self.transitions[acode][smask][scond] = sources[smask, scond]
                # assign transitions to existing dtypes
                for k in self.unmask(smask):
                    dt = self.dtypes[k]
                    for x in self.resolve_condition(scond, k): # store targets
                        dt.transitions[acode, x] = sources[smask, scond] 
        # def flush_transitions(acode, sources):
        #     if not acode: return # nothing to flush on first loop
        #     for smask, scond in sources:
        #         for k in self.unmask(smask):
        #             dt = self.dtypes[k]
        #             for x in self.resolve_condition(scond, k): # store targets
        #                 dt.transitions[acode, x] = sources[smask, scond] 
        #############################################################                    
        acode = None
        with open('%s/%s.%s' % (self.model_path, self.model_name, filename_suffix)) as f:
            s = f.read()
        s = re.sub(r'\{.*?\}', '', s, flags=re.M|re.S) # remove curly-bracket comments
        for l in re.split(r'[\r\n]+', s, flags=re.M|re.S):
            if re.match('^\s*(;|$)', l): continue # skip comments and blank lines
            l = l.strip().partition(';')[0].strip() # strip leading whitespace, trailing comments
            tokens = re.split('\s+', l)
            if l.startswith('*CASE'):
                if acode: flush_transitions(acode, sources)
                acode = tokens[1].lower()
                sources = {}
            elif l.startswith('*SOURCE'):
                smask = tuple(t.lower() for t in tokens[1:self.nthemes+1])
                match = re.search(r'@.+\)', l)
                scond = match.group(0) if match else ''
                sources[(smask, scond)] = []
            elif l.startswith('*TARGET'):
                tmask = tuple(t.lower() for t in tokens[1:self.nthemes+1])
                tprop = float(tokens[self.nthemes+1]) * 0.01
                try: # _AGE keyword
                    tage = int(tokens[tokens.index('_AGE')+1])
                except:
                    tage = None
                try: # _LOCK keyword
                    tlock = int(tokens[tokens.index('_LOCK')+1])
                except:
                    tlock = None
                try: # _REPLACE keyword (TO DO: implement other cases)
                    args = re.split('\s?,\s?', re.search('(?<=_REPLACE\().*(?=\))', l).group(0))
                    theme_index = int(args[0][3]) - 1
                    treplace = theme_index, args[1]
                except:
                    treplace = None
                try: # _APPEND keyword (TO DO: implement other cases)
                    args = re.split('\s?,\s?', re.search('(?<=_APPEND\().*(?=\))', l).group(0))
                    theme_index = int(args[0][3]) - 1
                    tappend = theme_index, args[1]
                except:
                    tappend = None
                sources[(smask, scond)].append((tmask, tprop, tage, tlock, treplace, tappend))
        flush_transitions(acode, sources)

    
    def import_optimize_section(self, filename_suffix='opt'):
        pass

    def import_graphics_section(self, filename_suffix='gra'):
        pass

    def import_lifespan_section(self, filename_suffix='lif'):
        pass

    def import_lifespan_section(self, filename_suffix='lif'):
        pass

    def import_schedule_section(self, filename_suffix='seq'):
        pass

    def import_control_section(self, filename_suffix='run'):
        pass

    def grow(self, start_period=0, cascade=True):
        for dt in self.dtypes.values(): dt.grow(start_period, cascade)

if __name__ == '__main__':
    pass
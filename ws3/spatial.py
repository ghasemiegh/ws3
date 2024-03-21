###################################################################################
# MIT License

# Copyright (c) 2015-2020 Gregory Paradis

# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:

# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.

# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.
###################################################################################

import pandas as pd
import numpy as np
import rasterio
import os
from profilehooks import profile
import random
import copy

"""
This module implements the ``ForestRaster`` class, which can be used to allocate an 
aspatial disturbance schedule (for example, an optimal solution to a wood supply problem 
generated by an instance of the ``forest.ForestModel`` class) to a rasterized 
representation of the forest inventory. 
"""


class ForestRaster:
    """
    The ``ForestRaster`` class can be used to allocate an aspatial disturbance schedule 
    (for example, an optimal solution to a wood supply problem generated by an instance 
    of the ``forest.ForestModel`` class) to a rasterized representation of the forest inventory. 
    """
    def __init__(self,
                 hdt_map,
                 hdt_func,
                 src_path,
                 snk_path,
                 acode_map,
                 forestmodel,
                 base_year,
                 horizon=None,
                 period_length=10,
                 tif_compress='lzw',
                 tif_dtype=rasterio.uint8,
                 piggyback_acodes=None,
                 time_step=1,
                 disturb_thresh=10):
        """

        :param dict hdt_map: A dictionary mapping hash values to development types.
          The rasterized forest inventory is stored in a 3-layer GeoTIFF 
          file. Pixel values for layer 1 represent the *theme* values
          (i.e., the stratification variables used to stratify the forest 
          inventory into development types). The value of the ``hdt_map`` 
          parameter is used to *expand* hash value back into a tuple of theme
          values. Pixel values for layer 2 represent age (time unit may vary depending on 
          how the model was compiled). Pixel values for layer 3 represent block ID code 
          (the notion of what constitutes a block, and how ID codes are assigned, is entirely
          up to the user when compiling the rasterized inventory).  
        :param function hdt_func: A function that accepts a tuple of theme values, and 
          returns a hash value. Must be the same function used to encode the 
          rasterized forest inventory (see documentation of the ``hdt_map`` 
          parameter, above).
        :param str src_path: Filesystem path pointing to the input GeoTIFF file 
          (i.e., the rasterized forest inventory). Note that this file will be
          used as a model for the output GeoTIFF files (i.e., pixel matrix 
          height and width, coordinate reference system, compression 
          parameters, etc.).
        :param str snk_path: Filesystem path pointing to a directory where the 
          output GeoTIFF files. The output GeoTIFF files are automatically 
          created inside the class constructor method (one GeoTIFF file for 
          each combination of disturbance type and year. 
        :param dict acode_map: Dict keyed on disturbance codes, returning string prefix 
          to use for GeoTIFF output filenames.
        :param int forestmodel: An instance of the 
          :py:class:`~.forest.ForestModel` class. 
        :param int or None horizon: Length of planning horizon (expressed as a number of 
          periods). If ``None``, defaults to ``forestmodel.horizon``.
        :param int base_year: Base year for numbering of annual time steps (affects 
          GeoTIFF output filenames).
        :param str tiff_compress: GeoTIFF output file compression mode (uses LZW lossless 
          compression by default).
        :param rasterio.dtype tif_dtype: Data type for output GeoTIFF files (defaults to 
          ``rasterio.uint8``, i.e., an 8-byte unsigned integer).
        :param dict(str, list) piggyback_acodes: A dictionary of list of tuples, describing 
          piggyback disturbance parameters. By *piggyback* disturbance, we mean
          a disturbance that was not explicitly scheduled by the ``ForestModel`` 
          instance, but rather is modelled as a (randomly-selected) subset of 
          one of the explicitly modelled disturbances. 

        For example, if we want to model that 85% of pixels disturbed using the 
        *clearcut* disturbance are disturbed by a piggybacked *slashburn* 
        disturbance, we would pass a value of  
        ``{'clearcut':[('slashburn', 0.85)]}`` for the ``piggyback_acodes``
        parameter. 
        """
        self._hdt_map = hdt_map
        self._hdt_func = hdt_func
        self._acodes = list(acode_map.keys())
        self._acode_map = acode_map
        self._forestmodel = forestmodel
        self._horizon = horizon
        self._base_year = base_year
        self._period_length = period_length
        self._time_step = time_step
        self._i2a = {i: a for i, a in enumerate(self._acodes)}
        self._a2i = {a: i for i, a in enumerate(self._acodes)}
        self._p = 1 # initialize current period
        self._src = rasterio.open(src_path, 'r')
        self._x = self._src.read()
        self._ix_forested = np.where(self._x[0] != 0)
        self._blkid = np.unique(self._x[2])
        self._d = self._src.transform.a # pixel width
        self._pixel_area = pow(self._d, 2) * 0.0001 # m to hectares
        profile = copy.copy(self._src.profile)
        profile.update(dtype=tif_dtype, compress=tif_compress, count=1, nodata=0)
        self._piggyback_acodes = piggyback_acodes
        self._snk_path = snk_path
        self._tif_dtype = tif_dtype
        self._tif_compress = tif_compress
        self._snk = {(p, dy):{acode:rasterio.open(snk_path+'/%s_%i.tif' % (acode_map[acode], base_year+(p-1)*period_length + dy), 'w+', **profile)
            for acode in self._acodes}
            for dy in range(0, period_length, self._time_step) for p in range(1, (horizon+1))}
        self._init_snkd()
        self._is_valid = True
        self._disturb_thresh = disturb_thresh
        
    def commit(self):
        """
        Closes all open handles for output GeoTIFF files, which commits changes
        stored in data buffer memory. The :py:class:`.ForestRaster` instance is 
        essentially expired after this method has been called, and further
        calls to :py:meth:`.allocate_schedule` will trigger a 
        :py:exc:`~exceptions.RuntimeError` exception.
        """
        for p in self._snk:
            for acode in self._snk[p]:
                self._snk[p][acode].close()
        self._is_valid = False

    def cleanup(self):
        """
        Calls :py:meth:`~.commit`, and also closes the open file handle for the forest inventory input 
        GeoTIFF file. The :py:class:`.ForestRaster` instance is 
        essentially expired after this method has been called, and further
        calls to :py:meth:`.allocate_schedule` will trigger a 
        :py:exc:`~exceptions.RuntimeError` exception.
        """
        self.commit()
        self._src.close()

    #@profile(immediate=True)
    def allocate_schedule(self, mask=None, verbose=False, commit=True, sda_mode='randblk',
                          da=0, ovrflwthr=0, fudge=1., nthresh=0, minage=1, aggregate_disturbance=False):
        """
        Allocates the current disturbance schedule from the
        :py:class:~`.forest.ForestModel` instance passed via the ``forestmodel``
        parameter. This is the core method of the :py:class:`~.ForestRaster` class.
        This method should only be called once. Calls the :py:meth:`~.commit` 
        method by default, which closes the open file handles for the output
        GeoTIFF files (else the files are locked until the instance is destroyed).

        The :py:class:`~.ForestRaster` class defines both ``__enter__`` and 
        ``__exit__`` methods, so instance scope and lifetime can easily be
        managed using a ``with`` block. This will automatically closes all open
        file handles, thereby avoid hard-to-debug problems downstream 
        (highly recommended). For example

        .. code-block :: python

          with ForestRaster(**kwargs) as fr:
              fr.allocate_schedule()
        
        This method allocates the aspatial disturbance schedule (simulated in 
        the :py:class:`~.forest.ForestModel` instance passed to the
        :py:class:`~.ForestRaster` instance constructor) to a spatial raster
        grid. The raster grid specifications are copied from the input 
        GeoTIFF file (containing initial forest inventory data), such that
        the output GeoTIFF files can be exactly overlaid onto the 
        raster inventory layers. The :py:class:`~.forest.ForestModel`
        simulates disturbances using time steps of user-defined length.
        This method disaggregates the disturbance schedule down to annual
        time steps (assuming uniform distribution of disturbed area within
        each planning period).
        
        .. note:: The algorithm uses the development-type-wise operability rules 
          from the :py:class:`~.forest.ForestModel` instance to allocate the
          aspatial schedule to the raster pixels. Thus, the output should be
          feasible with respect to the original model.

        .. note:: Currently this method randomly selects a subset of operable pixels 
          for disturbance at each annual time step. This is not likely to produce a
          realistic landscape-level spatial disturbance pattern. Realistically, 
          each disturbance type is likely to have a distinct patch size 
          distribution (e.g., clearcut harvest disturbances might have a uniform
          patch size distribution, whereas wildfire disturbances might follow a 
          Weibull distribution, etc.). We may revisit this in a later release,
          possibly adding extra parameters to allow disturbance-type-wise 
          parametrization of pixel aggregation methodology.

        *Piggyback* disturbances (if specified in the ``piggyback_acodes``
        parameter in the constructor) are simulated at the end of the process.

        :param tuple(str) mask: Tuple of strings, used to filter development 
          types. Typically, this will be used to filter multi-management-unit
          models (e.g., if input raster inventory and output disturbance 
          raster files need to be generated on a per-management-unit basis).
        :param bool verbose: Prints extra info to the console if ``True`` 
          (default ``False``).
        :param bool commit: Automatically calls :py:meth:`~.commit` if ``True``
          (default ``True``).
        :param int da: *[FOR DEBUG USE ONLY. DO NOT MODIFY.]*
        :param float fudge: *[FOR DEBUG USE ONLY. DO NOT MODIFY.]* 
        """
        if not self._is_valid: raise RuntimeError('commit() already called (i.e., instance is toast).')
        if mask: dtype_keys = self._forestmodel.unmask(mask)
        for p in range(1, self._horizon+1):
            if verbose > 0: print('processing schedule for period %i' % p)
            for acode in self._forestmodel.applied_actions[p]:
                for dtk in self._forestmodel.applied_actions[p][acode]:
                    #print('dtk', dtk)
                    if mask:
                        if dtk not in dtype_keys: continue
                    for from_age in self._forestmodel.applied_actions[p][acode][dtk]:
                        area = self._forestmodel.applied_actions[p][acode][dtk][from_age][0]
                        print('processing case', p, acode, dtk, from_age, area) # DEBUG
                        if not area: continue
                        from_dtk = list(dtk) 
                        trn = self._forestmodel.dtypes[dtk].transitions[acode, from_age][0]
                        tmask, tprop, tyield, tage, tlock, treplace, tappend = trn
                        to_dtk = [t if tmask[i] == '?' else tmask[i] for i, t in enumerate(from_dtk)] 
                        if treplace: to_dtk[treplace[0]] = self._forestmodel.resolve_replace(from_dtk, treplace[1])
                        to_dtk = tuple(to_dtk)
                        to_age = self._forestmodel.resolve_targetage(to_dtk, tyield, from_age, tage, acode, verbose=False)
                        to_age = max(to_age, minage) # hack! (yuck)
                        tk = tuple(to_dtk)+(to_age,)
                        _target_area = area
                        DY = list(range(0, self._period_length, self._time_step))
                        #random.shuffle(DY)
                        for dy in DY:
                            print('dy', dy)
                            _tr = self._period_length / self._time_step # target ratio
                            if area < (_tr * self._pixel_area): # less than one pixel per year
                                if _target_area > self._pixel_area * 0.5:
                                    target_area = self._pixel_area
                                    _target_area -= self._pixel_area
                                else:
                                    break
                            else:
                                target_area = area / _tr
                            from_ages = [from_age]
                            while from_ages and target_area:
                                from_age = from_ages.pop()
                                target_area = self._transition_cells(from_dtk, from_age,
                                                                     to_dtk, to_age,
                                                                     target_area, acode, dy,
                                                                     mode=sda_mode, da=da, fudge=fudge,
                                                                     ovrflwthr=ovrflwthr,
                                                                     verbose=verbose,
                                                                     nthresh=nthresh,
                                                                     aggregate_disturbance=aggregate_disturbance)
                            if target_area and verbose > 0:
                                print('failed', (from_dtk, from_age, to_dtk, to_age, acode), end=' ')
                                print('(missing %4.1f of %4.1f)' % (target_area, area / _tr),
                                      'in p%i dy%i' % (p, dy))
                if acode in self._piggyback_acodes:
                    for _acode, _p in self._piggyback_acodes[acode]:
                        for dy in range(0, self._period_length, self._time_step):
                            x = np.where(self._snkd[(acode, dy)] == 1)
                            xn = len(x[0])
                            if not xn: continue # bug fix (is this OK?)
                            r = np.random.choice(xn, int(_p * xn), replace=False)
                            ix = x[0][r], x[1][r]
                            self._snkd[(_acode, dy)][ix] = 1
            self._write_snk()
            year = self._base_year + ((p - 1) * self._period_length)
            snk_filename = self._snk_path+'/inventory_%i.tif' % year
            with rasterio.open(snk_filename, 'w', **self._src.profile) as snk:
                __x = np.copy(self._x)
                if verbose > 0:
                    print('saving %i post-harvest pixels to %s' % (year, snk_filename))
                snk.write(__x) 
            if p < self._horizon: self.grow()


    def __enter__(self):
        # The value returned by this method is
        # assigned to the variable after ``as``
        return self

    
    def __exit__(self, exc_type, exc_value, exc_traceback):
        # returns either True or False
        # Don't raise any exceptions in this method
        self.cleanup()
        return True

    
    def _read_snk(self, acode, dy, verbose=False):
        if verbose: print('ForestRaster._read_snk()', self._p, acode)
        return self._snk[(self._p, dy)][acode].read(1)


    def _write_snk(self, write=True):
        for dy in range(0, self._period_length, self._time_step):
            for acode in self._acodes:
                snk = self._snk[(self._p, dy)][acode]
                if write: snk.write(self._snkd[(acode, dy)], indexes=1)
                snk.close()


    def _init_snkd(self):
        self._snkd = {(acode, dy):np.full(self._x[0].shape, 0, dtype=self._tif_dtype) 
                      for dy in range(0, self._period_length, self._time_step) 
                      for acode in self._acodes}
                
                
    def _transition_cells(self, from_dtk, from_age, to_dtk, to_age, tarea, acode, dy,
                          mode='randblk', da=0, fudge=1., ovrflwthr=0, allow_split=True,
                          verbose=False, nthresh=0, aggregate_disturbance=False):
        """
        Modes:
          'randpxl': randomly select individual pixels
          'randblk': randomly select blocks (pixel aggregates)
        randblk mode allocates entire blocks, using the block layer (3) from the raster inventory.
        """
        assert mode in ('randpxl', 'randblk')
        fk, tk = tuple(from_dtk), tuple(to_dtk)
        fh, th = self._hdt_func(fk), self._hdt_func(tk)
        if 1:
            #print(fh, from_age)
            #global ___foo
            #___foo = self._x
            #assert False
            _ix = np.where((self._x[0][self._ix_forested] == fh) & 
                           (self._x[1][self._ix_forested]+da >= from_age-0) &
                           (self._x[1][self._ix_forested]+da <= from_age+0))
            x = self._ix_forested[0][_ix], self._ix_forested[1][_ix]
        else:
            x = np.where((self._x[0] == fh) & (self._x[1]+da == from_age))
        xn = len(x[0])
        xa = float(xn * self._pixel_area)
        c = tarea / xa if xa else np.inf
        print(xn, xa, c, tarea)
        if c > 1. and verbose > 1: print('missing area:', acode, dy, tarea - xa, from_dtk)
        c = min(c, 1.)
        n = int(round(xa * c / self._pixel_area))
        print('n', n, 'tarea', tarea)
        if not n: return # found nothing to transition
        if mode == 'randpxl' or n <= nthresh:
            missing_area = self._transition_cells_randpxl(x, xn, n, th, to_age, acode, dy, tarea, xa)
        elif mode == 'randblk':
            missing_area = self._transition_cells_randblk(x, n, th, to_age, acode, dy,
                                                          ovrflwthr=ovrflwthr, allow_split=allow_split,
                                                          aggregate_disturbance=aggregate_disturbance)      
        else:
            assert False # no valid mode specified
        return missing_area

    
    def _transition_cells_randpxl(self, x, xn, n, th, to_age, acode, dy, tarea, xa):
        r = np.random.choice(xn, n, replace=False)
        ix = x[0][r], x[1][r]
        self._x[0][ix] = th
        self._x[1][ix] = to_age
        self._snkd[(acode, dy)][ix] = 1 
        missing_area = max(0., tarea - xa)
        return missing_area
    
        
    def _transition_cells_randblk(self, x, n, th, to_age, acode, dy, ovrflwthr=0, allow_split=True, aggregate_disturbance=False):
        import scipy
        _n = 0
        if not aggregate_disturbance: # classic behaviour
            blkid = np.unique(self._x[2][x])
            np.random.shuffle(blkid)
            blkid = list(blkid)
        else: # new experimental behaviour
            #print(np.unique(self._x[1]))
            #assert False
            disturb_heat = scipy.ndimage.gaussian_filter(((self._x[1] >= 0) & (self._x[1] <= self._disturb_thresh)).astype(float), sigma=100)
            blkid = sorted(list(np.unique(self._x[2][x])), 
                           key=lambda b: np.ma.MaskedArray(disturb_heat, self._x[2] != b).mean(), 
                           reverse=True)
            #print(np.ma.MaskedArray(disturb_heat, self._x[2] != blkid[0]).mean())
            #print(np.ma.MaskedArray(disturb_heat, self._x[2] != blkid[-1]).mean())
            #assert False
        while _n < n and blkid:
            b = blkid.pop()
            _ix = np.where(self._x[2][x] == b)
            ix = x[0][_ix], x[1][_ix]
            if _n+ix[0].shape[0] > n+ovrflwthr:
                if blkid: # look for smaller block
                    continue 
                elif allow_split:
                    ix = ix[0][:n-_n], ix[1][:n-_n]
            _n += ix[0].shape[0]
            self._x[0][ix] = th
            self._x[1][ix] = to_age
            self._snkd[(acode, dy)][ix] = 1
        missing_area = max(0., (n - _n) * self._pixel_area)
        return missing_area

    
    def grow(self):
        """ Grows trees, only increment non-NA values"""
        self._p += 1
        # HACK! #############
        # only increment non-NA values
        self._x[1][self._x[1] != int(self._src.profile['nodata'])] += 1
        #####################
        self._init_snkd()

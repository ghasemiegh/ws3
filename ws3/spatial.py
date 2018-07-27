###################################################################################
# MIT License

# Copyright (c) 2015-2017 Gregory Paradis

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
#import fiona
import numpy as np
import rasterio
import os
from profilehooks import profile
#from random import randrange
import random

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
                 period_length=1,
                 tif_compress='lzw',
                 tif_dtype=rasterio.uint8,
                 piggyback_acodes=None):
        """

        :param dict hdt_map: A dictionary mapping hash values to development types.
          The rasterized forest inventory is stored in a 2-layer GeoTIFF 
          file. Pixel values for the first layer represent the *theme* values
          (i.e., the stratification variables used to stratify the forest 
          inventory into development types). The value of the ``hdt_map`` 
          parameter is used to *expand* hash value back into a tuple of theme
          values. 
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
        self._i2a = {i: a for i, a in enumerate(self._acodes)}
        self._a2i = {a: i for i, a in enumerate(self._acodes)}
        self._p = 1 # initialize current period
        self._src = rasterio.open(src_path, 'r')
        self._x = self._src.read()
        self._ix_forested = np.where(self._x[0] != 0)
        #self._blkix = {b:np.where(self._x[2]==b) for b in np.unique(self._x[2])}
        self._blkid = np.unique(self._x[2])
        self._d = self._src.transform.a # pixel width
        self._pixel_area = pow(self._d, 2) * 0.0001 # m to hectares
        profile = self._src.profile
        profile.update(dtype=tif_dtype, compress=tif_compress, count=1, nodata=0)
        self._piggyback_acodes = piggyback_acodes
        #for acode1 in piggyback_acodes:
        #    for acode2, _ in piggyback_acodes[acode1]:
        #        self._acodes.append(acode2)
        self._snk = {(p, dy):{acode:rasterio.open(snk_path+'/%s_%i.tif' % (acode_map[acode], base_year+(p-1)*period_length + dy),
                                                  'w', **profile)
                      for acode in self._acodes}
                      for dy in range(period_length) for p in range(1, (horizon+1))}
        self._snkd = {(acode, dy):self._read_snk(acode, dy) for dy in range(period_length) for acode in self._acodes}
        self._is_valid = True
        
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
                          da=0, ovrflwthr=0, fudge=1., nthresh=0):
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
            #assert p < 2
            if verbose > 0: print('processing schedule for period %i' % p)
            for acode in self._forestmodel.applied_actions[p]:
                for dtk in self._forestmodel.applied_actions[p][acode]:
                    if mask:
                        if dtk not in dtype_keys: continue
                    for from_age in self._forestmodel.applied_actions[p][acode][dtk]:
                        area = self._forestmodel.applied_actions[p][acode][dtk][from_age][0]
                        if not area: continue
                        from_dtk = list(dtk) 
                        trn = self._forestmodel.dtypes[dtk].transitions[acode, from_age][0]
                        tmask, tprop, tyield, tage, tlock, treplace, tappend = trn
                        to_dtk = [t if tmask[i] == '?' else tmask[i]
                                  for i, t in enumerate(from_dtk)] 
                        if treplace: to_dtk[treplace[0]] = self._forestmodel.resolve_replace(from_dtk,
                                                                                       treplace[1])
                        to_dtk = tuple(to_dtk)
                        to_age = self._forestmodel.resolve_targetage(to_dtk, tyield, from_age,
                                                               tage, acode, verbose=False)
                        tk = tuple(to_dtk)+(to_age,)
                        #th = self._hdt_func(tk)
                        #if acode in ['fire']:
                        #    print(from_dtk, from_age, to_dtk, to_age, acode, area)
                        _target_area = area
                        DY = list(range(self._period_length))
                        random.shuffle(DY)
                        for dy in DY:
                            #if not dy: _target_area = area
                            if area < (self._period_length * self._pixel_area): # less than one pixel per year
                                if _target_area > self._pixel_area * 0.5:
                                    target_area = self._pixel_area
                                    _target_area -= self._pixel_area
                                else:
                                    break
                            else:
                                target_area = area / self._period_length
                            from_ages = [from_age]
                            while from_ages and target_area:
                                from_age = from_ages.pop()
                                target_area = self._transition_cells(from_dtk, from_age,
                                                                     to_dtk, to_age,
                                                                     target_area, acode, dy,
                                                                     mode=sda_mode, da=da, fudge=fudge,
                                                                     ovrflwthr=ovrflwthr,
                                                                     verbose=verbose,
                                                                     nthresh=nthresh)
                            # old code ################################################
                            #    target_area = self._transition_cells_random(from_dtk, from_age,
                            #                                                to_dtk, to_age,
                            #                                                target_area, acode,
                            #                                                dy, da=da, fudge=fudge,
                            #                                                verbose=False)
                            if target_area:
                                print('failed', (from_dtk, from_age, to_dtk, to_age, acode),
                                      end=' ')
                                print('(missing %4.1f of %4.1f)' % (target_area, area /
                                                                    self._period_length),
                                      'in p%i dy%i' % (p, dy))
                if acode in self._piggyback_acodes:
                    for _acode, _p in self._piggyback_acodes[acode]:
                        for dy in range(self._period_length):
                            x = np.where(self._snkd[(acode, dy)] == 1)
                            xn = len(x[0])
                            if not xn: continue # bug fix (is this OK?)
                            r = np.random.choice(xn, int(_p * xn), replace=False)
                            ix = x[0][r], x[1][r]
                            self._snkd[(_acode, dy)][ix] = 1
            self._write_snk()
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

    def _write_snk(self):
        for dy in range(self._period_length):
            for acode in self._acodes:
                #print('writing snk', dy, acode)
                snk = self._snk[(self._p, dy)][acode]
                snk.write(self._snkd[(acode, dy)], indexes=1)
                snk.close()

    def _transition_cells(self, from_dtk, from_age, to_dtk, to_age, tarea, acode, dy,
                          mode='randblk', da=0, fudge=1., ovrflwthr=0, allow_split=True,
                          verbose=False, nthresh=0):
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
            _ix = np.where((self._x[0][self._ix_forested] == fh) & (self._x[1][self._ix_forested]+da == from_age))
            x = self._ix_forested[0][_ix], self._ix_forested[1][_ix]
        else:
            x = np.where((self._x[0] == fh) & (self._x[1]+da == from_age))
        #print(x)
        #assert False
        xn = len(x[0])
        #print(from_dtk, from_age, to_dtk, to_age, tarea, acode, dy, xn)
        xa = float(xn * self._pixel_area)
        c = tarea / xa if xa else np.inf
        if c > 1. and verbose > 1: print('missing area:', acode, dy, tarea - xa, from_dtk)
        c = min(c, 1.)
        n = int(round(xa * c / self._pixel_area))
        if not n: return # found nothing to transition
        if mode == 'randpxl' or n <= nthresh:
            missing_area = self._transition_cells_randpxl(x, xn, n, th, to_age, acode, dy, tarea, xa)
        elif mode == 'randblk':
            missing_area = self._transition_cells_randblk(x, n, th, to_age, acode, dy,
                                                          ovrflwthr=ovrflwthr, allow_split=allow_split)            
        return missing_area

    
    def _transition_cells_randpxl(self, x, xn, n, th, to_age, acode, dy, tarea, xa):
        #print('randpxl', n, th, to_age, acode, dy, tarea, xa)
        r = np.random.choice(xn, n, replace=False)
        ix = x[0][r], x[1][r]
        self._x[0][ix] = th
        self._x[1][ix] = to_age
        self._snkd[(acode, dy)][ix] = 1 #self._a2i[acode]
        missing_area = max(0., tarea - xa)

        
    def _transition_cells_randblk(self, x, n, th, to_age, acode, dy, ovrflwthr=0, allow_split=True):
        #print('randblk', n, th, to_age, acode, dy)
        if 0:
            _n = 0
            #print('transitioning cells:', from_dtk, from_age, to_dtk, to_age, tarea, acode, dy)
            blkid = np.unique(self._x[2][x])
            np.random.shuffle(blkid)
            blkid = list(blkid)
            while _n < n and blkid:
                b = blkid.pop()
                b = 29958
                print('b', b)
                ix = np.where(self._x[2] == b)
                if _n+ix[0].shape[0] > n+ovrflwthr:
                    if blkid: # look for smaller block
                        continue 
                    elif allow_split:
                        #print('split', n - _n, ix[0].shape[0]) 
                        ix = ix[0][:n-_n], ix[1][:n-_n]
                _n += ix[0].shape[0]
                print(ix)
                assert False
                self._x[0][ix] = th
                self._x[1][ix] = to_age
                self._snkd[(acode, dy)][ix] = 1
            missing_area = max(0., (n - _n) * self._pixel_area)
        else:
            _n = 0
            blkid = np.unique(self._x[2][x])
            np.random.shuffle(blkid)
            blkid = list(blkid)
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

    # def _transition_cells_randblk(self, x, n, th, to_age, acode, dy, ovrflwthr=0, allow_split=True):
    #     #print('randblk', n, th, to_age, acode, dy)
    #     _n = 0
    #     #print('transitioning cells:', from_dtk, from_age, to_dtk, to_age, tarea, acode, dy)
    #     blkid = np.unique(self._x[2][x])
    #     np.random.shuffle(blkid)
    #     blkid = list(blkid)
    #     while _n < n and blkid:
    #         b = blkid.pop()
    #         ix = np.where(self._x[2] == b)
    #         if _n+ix[0].shape[0] > n+ovrflwthr:
    #             if blkid: # look for smaller block
    #                 continue 
    #             elif allow_split:
    #                 #print('split', n - _n, ix[0].shape[0]) 
    #                 ix = ix[0][:n-_n], ix[1][:n-_n]
    #         _n += ix[0].shape[0]
    #         self._x[0][ix] = th
    #         self._x[1][ix] = to_age
    #         self._snkd[(acode, dy)][ix] = 1
    #         #print('allocated', _n, 'of', n)
    #         #return 0
    #     missing_area = max(0., (n - _n) * self._pixel_area)

        
    # def _transition_cells_random(self, from_dtk, from_age, to_dtk, to_age, tarea, acode, dy, da=0, fudge=1., verbose=False):
    #     fk, tk = tuple(from_dtk), tuple(to_dtk)
    #     fh, th = self._hdt_func(fk), self._hdt_func(tk)
    #     x = np.where((self._x[0] == fh) & (self._x[1]+da == from_age))
    #     xn = len(x[0])
    #     xa = float(xn * self._pixel_area)
    #     missing_area = max(0., tarea - xa)
    #     c = tarea / xa if xa else np.inf
    #     if c > 1. and verbose: print('missing area', from_dtk, tarea - xa)
    #     c = min(c, 1.)
    #     n = int(xa * c / self._pixel_area)
    #     if not n: return # found nothing to transition
    #     r = np.random.choice(xn, n, replace=False)
    #     ix = x[0][r], x[1][r]
    #     self._x[0][ix] = th
    #     self._x[1][ix] = to_age
    #     self._snkd[(acode, dy)][ix] = 1 #self._a2i[acode]
    #     return missing_area
          
    def grow(self):
        self._p += 1
        self._x[1] += 1 # age
        self._snkd = {(acode, dy):self._read_snk(acode, dy) for dy in range(self._period_length) for acode in self._acodes}

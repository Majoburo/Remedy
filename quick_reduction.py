# -*- coding: utf-8 -*-
"""
Created on Tue Jan 22 15:11:39 2019

@author: gregz
"""

import argparse as ap
import glob
import numpy as np
import os.path as op
import sys

from astropy.convolution import convolve, Gaussian2DKernel
from astropy.io import fits
from astropy.stats import biweight_location
from datetime import datetime, timedelta
from input_utils import setup_logging
from scipy.interpolate import griddata
from tables import open_file
from bokeh.plotting import figure, output_file, save
from bokeh.models.mappers import LinearColorMapper
from bokeh.models import ColorBar


# Configuration    
amps = ['LL', 'LU', 'RL', 'RU']
dither_pattern = np.array([[0., 0.], [1.27, -0.73], [1.27, 0.73]])
def_wave = np.arange(3470., 5542., 2.)
color_dict = {'blue': [3600., 3900.], 'green': [4350, 4650],
              'red': [5100., 5400.]}
for col_ in color_dict:
     color_dict[col_].append(np.searchsorted(def_wave, color_dict[col_][0]))
     color_dict[col_].append(np.searchsorted(def_wave, color_dict[col_][1]))

parser = ap.ArgumentParser(add_help=True)

parser.add_argument("-d", "--date",
                    help='''Date for reduction''',
                    type=str, default='20181108')

parser.add_argument("-o", "--observation",
                    help='''Observation ID''',
                    type=int, default=None)

parser.add_argument("-r", "--rootdir",
                    help='''Directory for raw data. 
                    (just before date folders)''',
                    type=str, default='/work/03946/hetdex/maverick')

parser.add_argument("-hf", "--hdf5file",
                    help='''HDF5 calibration file''',
                    type=str, default=None)

parser.add_argument("-i", "--ifuslot",
                    help='''If ifuslot is not provided, then all are reduced''',
                    type=int, default=None)

args = parser.parse_args(args=None)

def build_path(rootdir, date, obs, ifuslot, amp, base='sci', exp='exp*',
               instrument='virus'):
    if obs != '*':
        obs = '%07d' % obs
    path = op.join(rootdir, date, instrument, '%s%s' % (instrument, obs),
                   exp, instrument, '2*%s%s*%s.fits' % (ifuslot, amp, base))
    return path
    

def orient_image(image, amp, ampname):
    '''
    Orient the images from blue to red (left to right)
    Fibers are oriented to match configuration files
    '''
    if amp == "LU":
        image[:] = image[::-1, ::-1]
    if amp == "RL":
        image[:] = image[::-1, ::-1]
    if ampname is not None:
        if ampname == 'LR' or ampname == 'UL':
            image[:] = image[:, ::-1]
    return image


def make_avg_spec(wave, spec, binsize=35, per=50):
    ind = np.argsort(wave.ravel())
    T = 1
    for p in wave.shape:
        T *= p
    wchunks = np.array_split(wave.ravel()[ind],
                             T / binsize)
    schunks = np.array_split(spec.ravel()[ind],
                             T / binsize)
    nwave = np.array([np.mean(chunk) for chunk in wchunks])
    nspec = np.array([np.percentile(chunk, per) for chunk in schunks])
    nwave, nind = np.unique(nwave, return_index=True)
    return nwave, nspec[nind]


def base_reduction(filename, get_header=False):
    a = fits.open(filename)
    image = np.array(a[0].data, dtype=float)
    # overscan sub
    overscan_length = 32 * (image.shape[1] / 1064)
    O = biweight_location(image[:, -(overscan_length-2):])
    image[:] = image - O
    # trim image
    image = image[:, :-overscan_length]
    gain = a[0].header['GAIN']
    gain = np.where(gain > 0., gain, 0.85)
    rdnoise = a[0].header['RDNOISE']
    rdnoise = np.where(rdnoise > 0., rdnoise, 3.)
    amp = (a[0].header['CCDPOS'].replace(' ', '') +
           a[0].header['CCDHALF'].replace(' ', ''))
    try:
        ampname = a[0].header['AMPNAME']
    except:
        ampname = None
    header = a[0].header
    a = orient_image(image, amp, ampname) * gain
    E = np.sqrt(rdnoise**2 + np.where(a > 0., a, 0.))
    if get_header:
        return a, E, header
    return a, E


def get_mastertwi(twi_path, masterbias):
    files = glob.glob(twi_path)
    listtwi = []
    for filename in files:
        a, e = base_reduction(filename)
        a[:] -= masterbias
        listtwi.append(a)
    twi_array = np.array(listtwi, dtype=float)
    norm = np.median(twi_array, axis=(1, 2))[:, np.newaxis, np.newaxis]
    return np.median(twi_array / norm, axis=0) * np.median(norm)


def get_cal_path(pathname, date):
    datec = date
    daten = date
    cnt = 0
    while len(glob.glob(pathname)) == 0:
        datec_ = datetime(int(daten[:4]), int(daten[4:6]), int(daten[6:]))
        daten_ = datec_ - timedelta(days=1)
        daten = '%04d%02d%02d' % (daten_.year, daten_.month, daten_.day)
        pathname = pathname.replace(datec, daten)
        cnt += 1
        log.info(pathname)
        if cnt > 30:
            log.error('Could not find cal within 30 days.')
            break
    return pathname, daten


def get_script_path():
    return op.dirname(op.realpath(sys.argv[0]))


def get_spectra(array_sci, array_flt, array_trace, wave, def_wave):
    sci_spectrum = np.zeros((array_trace.shape[0], def_wave.shape[0]))
    twi_spectrum = np.zeros((array_trace.shape[0], def_wave.shape[0]))
    N = array_flt.shape[0]
    x = np.arange(array_flt.shape[1])
    for fiber in np.arange(array_trace.shape[0]):
        if array_trace[fiber].min() < 0.:
            continue
        if np.ceil(array_trace[fiber]).max() >= N:
            continue
        indl = np.floor(array_trace[fiber]).astype(int)
        indh = np.ceil(array_trace[fiber]).astype(int)
        tw = array_flt[indl, x] / 2. + array_flt[indh, x] / 2.
        twi_spectrum[fiber] = np.interp(def_wave, wave[fiber], tw, left=0.0,
                                        right=0.0)
        sw = (array_sci[indl, x] / array_flt[indl, x] +
              array_sci[indh, x] / array_flt[indh, x])
        sci_spectrum[fiber] = np.interp(def_wave, wave[fiber], sw, left=0.0,
                                        right=0.0)
    twi_spectrum[~np.isfinite(twi_spectrum)] = 0.0
    sci_spectrum[~np.isfinite(sci_spectrum)] = 0.0
    return twi_spectrum, sci_spectrum / 2.

def reduce_ifuslot(ifuloop, h5table):
    p, t, s = ([], [], [])
    for ind in ifuloop:
        ifuslot = '%03d' % h5table[ind]['ifuslot']
        amp = h5table[ind]['amp']
        amppos = h5table[ind]['ifupos']
        wave = h5table[ind]['wavelength']
        trace = h5table[ind]['trace']
        try:
            masterbias = h5table[ind]['masterbias']
        except:
            masterbias = 0.0
        twibase = build_path(args.rootdir, args.date, '*', ifuslot, amp,
                             base='twi')
        twibase, newdate = get_cal_path(twibase, args.date)
    
        if newdate != args.date:
            log.info('Found twi files on %s and using them for %s' %
                     (newdate, args.date))
        log.info('Making mastertwi for %s%s' % (ifuslot, amp))
        masterflt = get_mastertwi(twibase, masterbias)
        log.info('Done making mastertwi for %s%s' % (ifuslot, amp))

        filenames = build_path(args.rootdir, args.date, args.observation,
                               ifuslot, amp)
        filenames = sorted(glob.glob(filenames))
        for j, fn in enumerate(filenames):
            sciimage, scierror = base_reduction(fn)
            sciimage[:] = sciimage - masterbias
            twi, spec = get_spectra(sciimage, masterflt, trace, wave, def_wave)
            pos = amppos + dither_pattern[j]
            for x, i in zip([p, t, s], [pos, twi, spec]):
                x.append(i * 1.)
    p, t, s = [np.vstack(j) for j in [p, t, s]]
    return p, t, s 
            
def make_plot(image):
    #image = image / np.median(image)
    G = Gaussian2DKernel(7)
    image = convolve(image, G, boundary='extend')
    image[:] -= np.median(image)
    color_mapper = LinearColorMapper(palette="gray",
                                  low=np.percentile(image, 2),
                                  high=np.percentile(image, 98))
    p = figure(x_range=(-25, 25), y_range=(-25, 25),
               tooltips=[("x", "$x"), ("y", "$y"), ("value", "@image")])

    p.image(image=[image], x=-25, y=-25, dw=50, dh=50,
            color_mapper=color_mapper)
    color_bar = ColorBar(color_mapper=color_mapper,
                         label_standoff=12, border_line_color=None, 
                         location=(0,0))
    p.add_layout(color_bar, 'right')
    output_file("image.html", title="image.py example")
    save(p)

def output_fits(image):
    F = fits.PrimaryHDU(np.array(image, 'float32'))
    

DIRNAME = get_script_path()
instrument = 'virus'
log = setup_logging()
if args.hdf5file is None:
    args.hdf5file = op.join(DIRNAME, 'cals', 'default_cals.h5')

h5file = open_file(args.hdf5file, mode='r')
h5table = h5file.root.Info.Cals

grid_x, grid_y = np.meshgrid(np.linspace(-25, 25, 401),
                             np.linspace(-25, 25, 401))

ifuslots = h5table.cols.ifuslot[:]
if args.ifuslot is not None:
    ifuloop = np.where(args.ifuslot == ifuslots)[0]
else:
    ifuloop = np.arange(0, len(ifuslots))

log.info('Reducing ifuslot: %03d' % args.ifuslot)
pos, twispectra, scispectra = reduce_ifuslot(ifuloop, h5table)
average_twi = np.median(twispectra, axis=0)
scispectra = scispectra * average_twi
color = color_dict['red']
image = np.median(scispectra[:, color[2]:color[3]], axis=1)
back = [np.percentile(chunk, 20) 
        for chunk in np.array_split(image, image.shape[0] / 112)]
avg = np.median(back)
chunks = np.array_split(image, image.shape[0] / 112)
newimage = np.hstack([avg*chunk/b for b, chunk in zip(back, chunks)])
log.info('Done base reduction for ifuslot: %03d' % args.ifuslot)


grid_z0 = griddata(pos, newimage, (grid_x, grid_y), method='nearest')
make_plot(grid_z0)

h5file.close()
    
        
import os
from collections import defaultdict
from datetime import datetime
from subprocess import PIPE, call

import astropy.io.fits as pyfits
import astropy.units as u
import astropy.wcs as pywcs
import matplotlib.pyplot as plt
import numpy as np
import pyregion._region_filter as rfilter
import scipy.interpolate as interpolate
from six import string_types
from tqdm import tqdm

from xcs_soxs.constants import erg_per_keV, sigma_to_fwhm
from xcs_soxs.events import write_event_file
from xcs_soxs.instrument_registry import instrument_registry
from xcs_soxs.simput import read_simput_catalog
from xcs_soxs.utils import mylog, ensure_numpy_array, \
    parse_prng, parse_value, get_rot_mat, soxs_cfg


def get_response_path(fn):
    if os.path.exists(fn):
        return os.path.abspath(fn)
    else:
        resp_path = soxs_cfg.get("soxs", "response_path")
        if not os.path.exists(resp_path):
            raise IOError("The SOXS response directory %s does not exist!" % resp_path)
        resp_fn = os.path.join(resp_path, fn)
        if os.path.exists(resp_fn):
            return resp_fn
    raise IOError("Could not find file %s! Please download it from " % fn +
                  "http://hea-www.cfa.harvard.edu/~jzuhone/soxs/responses.html "
                  "and place it in the current working directory or place it in "
                  "the SOXS response directory %s." % resp_path)


class SpatialARF(object):
    def __init__(self, filenames, response_regions):
        self.filename = filenames[0]
        self.arf_files = filenames
        self.response_regions = response_regions
        first_file = pyfits.open(self.filename)
        # Only need to read in one set of energy limits, for a set of ARFs generated to describe an instrument the
        # energy bands should be the same
        self.elo = first_file["SPECRESP"].data.field("ENERG_LO")
        self.ehi = first_file["SPECRESP"].data.field("ENERG_HI")
        self.emid = 0.5 * (self.elo + self.ehi)
        first_file.close()

        eff_areas = []
        for filename in self.arf_files:
            f = pyfits.open(filename)
            eff_areas.append(np.nan_to_num(f["SPECRESP"].data.field("SPECRESP")).astype("float64"))
            f.close()
        self.eff_areas = np.array(eff_areas)

        maxes = [areas.max() for areas in self.eff_areas]
        self.max_area = max(maxes)

    @classmethod
    def from_instrument(cls, name):
        """
        Return an :class:`~xcs_soxs.instrument.SpatialARF`
        object from the name of an existing instrument
        specification in SOXS.

        Parameters
        ----------
        name : string
            The name of the instrument specification to use
            to obtain the ARF object from.

        Examples
        --------
        >>> arf = xcs_soxs.SpatialARF.from_instrument("xmm_epn_0201903501")
        """
        instr = instrument_registry.get(name, None)
        if instr is None:
            raise KeyError("Instrument '%s' not in registry!" % name)
        return cls(instr["arf"])

    def __str__(self):
        return self.filename

    def find_response_region(self, x_coord, y_coord):
        """
        Use the positions of the events, and the response regions, to determine which ARF to use.

        Parameters
        ----------
        x_coord : np.ndarray
            The x coordinates of events, in the 'chip' coordinate system
        y_coord : np.ndarray
            The y coordinates of events, in the 'chip' coordinate system
        """
        num_evts = x_coord.shape[0]
        reg_ids = -np.ones(num_evts, dtype='int')
        for reg_ind, reg in enumerate(self.response_regions):
            if reg[0] == "Box":
                inside_reg = np.logical_and.reduce((x_coord >= (reg[1] - (reg[3]/2)), x_coord <= (reg[1] + (reg[3]/2)),
                                                    y_coord >= (reg[2] - (reg[4]/2)), y_coord <= (reg[2] + (reg[4]/2))))
            else:
                region_type, region_args = (reg[0], reg[1:])
                r = getattr(rfilter, region_type)(*region_args)
                inside_reg = r.inside(x_coord, y_coord)

            reg_ids[inside_reg] = reg_ind

        return reg_ids

    def interpolate_area(self, energy, arf_ind):
        """
        Interpolate the effective area to the energies
        provided  by the supplied *energy* array.
        """
        unique_arf_inds = np.unique(arf_ind)
        e_area = np.zeros((1, len(energy)))

        for a_ind in unique_arf_inds:
            if a_ind != -1:
                rel_inds = np.where(arf_ind == a_ind)[0]
                rel_energies = energy[rel_inds]
                e_area[0, rel_inds] = np.interp(rel_energies, self.emid, self.eff_areas[a_ind, :], left=0.0, right=0.0)

        return u.Quantity(list(e_area[0, :]), "cm**2")

    def detect_events(self, events, exp_time, flux, refband, prng=None):
        """
        Use the ARF to determine a subset of photons which
        will be detected. Returns a boolean NumPy array
        which is the same is the same size as the number
        of photons, wherever it is "true" means those photons
        have been detected.

        Parameters
        ----------
        events : dict of np.ndarrays
            The energies and positions of the photons.
        exp_time : float
            The exposure time in seconds.
        flux : float
            The total flux of the photons in erg/s/cm^2.
        refband : array_like
            A two-element array or list containing the limits
            of the energy band which the flux was computed in.
        resp_regs : list of lists
            A list of lists that describe the regions each ARF file was generated for.
        prng : :class:`~numpy.random.RandomState` object, integer, or None
            A pseudo-random number generator. Typically will only
            be specified if you have a reason to generate the same
            set of random numbers, such as for a test. Default is None,
            which sets the seed based on the system time.
        """
        prng = parse_prng(prng)
        energy = events["energy"]
        if energy.size == 0:
            return events

        which_arfs = self.find_response_region(events["cx"], events["cy"])
        earea = self.interpolate_area(energy, which_arfs).value
        idxs = np.logical_and(energy >= refband[0], energy <= refband[1])
        rate = flux/(energy[idxs].sum()*erg_per_keV)*earea[idxs].sum()
        n_ph = prng.poisson(lam=rate*exp_time)
        fak = float(n_ph)/energy.size
        if fak > 1.0:
            mylog.error("Number of events in sample: %d, Number of events wanted: %d" % (energy.size, n_ph))
            raise ValueError("This combination of exposure time and effective area "
                             "will result in more photons being drawn than are available "
                             "in the sample!!!")
        w = earea / self.max_area
        randvec = prng.uniform(size=energy.size)
        eidxs = prng.permutation(np.where(randvec < w)[0])[:n_ph].astype("int64")
        mylog.info("%s events detected." % n_ph)
        for key in events:
            events[key] = events[key][eidxs]
        return events


class AuxiliaryResponseFile(object):
    r"""
    A class for auxiliary response files (ARFs).

    Parameters
    ----------
    filename : string
        The filename of the ARF to be read.

    Examples
    --------
    >>> arf = AuxiliaryResponseFile("xrs_mucal_3x10_3.0eV.arf")
    """
    def __init__(self, filename):
        self.filename = get_response_path(filename)
        f = pyfits.open(self.filename)
        self.elo = f["SPECRESP"].data.field("ENERG_LO")
        self.ehi = f["SPECRESP"].data.field("ENERG_HI")
        self.emid = 0.5*(self.elo+self.ehi)
        self.eff_area = np.nan_to_num(f["SPECRESP"].data.field("SPECRESP")).astype("float64")
        self.max_area = self.eff_area.max()
        f.close()

    @classmethod
    def from_instrument(cls, name):
        """
        Return an :class:`~xcs_soxs.instrument.AuxiliaryResponseFile`
        object from the name of an existing instrument
        specification in SOXS.

        Parameters
        ----------
        name : string
            The name of the instrument specification to use 
            to obtain the ARF object from.

        Examples
        --------
        >>> arf = xcs_soxs.AuxiliaryResponseFile.from_instrument("xmm_epn_0201903501")
        """
        instr = instrument_registry.get(name, None)
        if instr is None:
            raise KeyError("Instrument '%s' not in registry!" % name)
        return cls(instr["arf"])

    def __str__(self):
        return self.filename

    def interpolate_area(self, energy):
        """
        Interpolate the effective area to the energies 
        provided  by the supplied *energy* array.
        """
        earea = np.interp(energy, self.emid, self.eff_area, left=0.0, right=0.0)
        return u.Quantity(earea, "cm**2")

    def detect_events(self, events, exp_time, flux, refband, prng=None):
        """
        Use the ARF to determine a subset of photons which 
        will be detected. Returns a boolean NumPy array 
        which is the same is the same size as the number 
        of photons, wherever it is "true" means those photons 
        have been detected.

        Parameters
        ----------
        events : dict of np.ndarrays
            The energies and positions of the photons. 
        exp_time : float
            The exposure time in seconds.
        flux : float
            The total flux of the photons in erg/s/cm^2. 
        refband : array_like
            A two-element array or list containing the limits 
            of the energy band which the flux was computed in. 
        prng : :class:`~numpy.random.RandomState` object, integer, or None
            A pseudo-random number generator. Typically will only 
            be specified if you have a reason to generate the same 
            set of random numbers, such as for a test. Default is None, 
            which sets the seed based on the system time. 
        """
        prng = parse_prng(prng)
        energy = events["energy"]
        if energy.size == 0:
            return events
        earea = self.interpolate_area(energy).value
        idxs = np.logical_and(energy >= refband[0], energy <= refband[1])
        rate = flux/(energy[idxs].sum()*erg_per_keV)*earea[idxs].sum()
        n_ph = prng.poisson(lam=rate*exp_time)
        fak = float(n_ph)/energy.size
        if fak > 1.0:
            mylog.error("Number of events in sample: %d, Number of events wanted: %d" % (energy.size, n_ph))
            raise ValueError("This combination of exposure time and effective area "
                             "will result in more photons being drawn than are available "
                             "in the sample!!!")
        w = earea / self.max_area
        randvec = prng.uniform(size=energy.size)
        eidxs = prng.permutation(np.where(randvec < w)[0])[:n_ph].astype("int64")
        mylog.info("%s events detected." % n_ph)
        for key in events:
            events[key] = events[key][eidxs]
        return events

    def plot(self, xscale="log", yscale="log", xlabel=None,
             ylabel=None, fig=None, ax=None, **kwargs):
        """
        Make a quick plot of the effective area curve.

        Parameters
        ----------
        xscale : string
            The scale of the x-axis. "linear" or "log".
        yscale : string
            The scale of the y-axis. "linear" or "log".
        xlabel : string
            The label of the x-axis. Default: "E (keV)"
        ylabel : string
            The label of the y-axis. Default: "$\mathrm{A\ (cm^2)}$"
        fig : :class:`~matplotlib.figure.Figure`, optional
            The figure to place the plot in. If not supplied, 
            one will be created.
        ax : :class:`~matplotlib.axes.Axes`, optional
            The axes to place the plot in. If not supplied, 
            one will be created.

        All other arguments are passed to the call to 
        :meth:`~matplotlib.axes.Axes.plot`.

        Returns
        -------

        A tuple of the :class:`~matplotlib.figure.Figure` and 
        :class:`~matplotlib.axes.Axes` objects.
        """
        if xlabel is None:
            xlabel = "E (keV)"
        if ylabel is None:
            ylabel = "$\mathrm{A\ (cm^2)}$"
        if fig is None:
            fig = plt.figure(figsize=(10, 10))
        if ax is None:
            ax = fig.add_subplot(111)
        ax.plot(self.emid, self.eff_area, **kwargs)
        ax.set_xscale(xscale)
        ax.set_yscale(yscale)
        ax.set_xlabel(xlabel)
        ax.set_ylabel(ylabel)
        return fig, ax


class FlatResponse(AuxiliaryResponseFile):
    """
    A flat effective area response.

    Parameters
    ----------
    emin : float
        The minimum energy of the response in keV.
    emax : float
        The maximum energy of the response in keV. 
    area : float
        The effective area in cm**2. 
    nbins : integer
        The number of bins in the response file. 

    Examples
    --------
    >>> arf = FlatResponse(0.1, 10.0, 3000.0, 10000)
    """
    def __init__(self, emin, emax, area, nbins):
        self.filename = "flat_response"
        de = (emax-emin)/nbins
        self.elo = np.arange(nbins)*de + emin
        self.ehi = self.elo + de
        self.emid = 0.5*(self.elo+self.ehi)
        self.eff_area = area*np.ones(nbins)
        self.max_area = area


class RedistributionMatrixFile(object):
    r"""
    A class for redistribution matrix files (RMFs).

    Parameters
    ----------
    filename : string
        The filename of the RMF to be read.

    Examples
    --------
    >>> rmf = RedistributionMatrixFile("xrs_hdxi.rmf")
    """
    def __init__(self, filename):
        self.filename = get_response_path(filename)
        self.handle = pyfits.open(self.filename, memmap=True)
        if "MATRIX" in self.handle:
            self.mat_key = "MATRIX"
        elif "SPECRESP MATRIX" in self.handle:
            self.mat_key = "SPECRESP MATRIX"
        else:
            raise RuntimeError("Cannot find the response matrix in the RMF "
                               "file %s! " % filename+"It should be named "
                               "\"MATRIX\" or \"SPECRESP MATRIX\".")
        self.header = self.handle[self.mat_key].header
        self.num_mat_columns = len(self.handle[self.mat_key].columns)
        self.ebounds_header = self.handle["EBOUNDS"].header
        self.weights = np.array([w.sum() for w in self.data["MATRIX"]])
        self.elo = self.data["ENERG_LO"]
        self.ehi = self.data["ENERG_HI"]
        self.ebins = np.append(self.data["ENERG_LO"], self.data["ENERG_HI"][-1])
        self.emid = 0.5*(self.elo+self.ehi)
        self.de = self.ehi-self.elo
        self.n_e = self.elo.size
        self.n_ch = self.header["DETCHANS"]
        num = 0
        for i in range(1, self.num_mat_columns+1):
            if self.header["TTYPE%d" % i] == "F_CHAN":
                num = i
                break
        self.cmin = self.header.get("TLMIN%d" % num, 1)
        self.cmax = self.header.get("TLMAX%d" % num, self.n_ch)

    @classmethod
    def from_instrument(cls, name):
        """
        Return an :class:`~xcs_soxs.instrument.RedistributionMatrixFile`
        object from the name of an existing instrument
        specification in SOXS.

        Parameters
        ----------
        name : string
            The name of the instrument specification to use 
            to obtain the RMF object from.

        Examples
        --------
        >>> arf = xcs_soxs.RedistributionMatrixFile.from_instrument("hdxi")
        """
        instr = instrument_registry.get(name, None)
        if instr is None:
            raise KeyError("Instrument '%s' not in registry!" % name)
        return cls(instr["rmf"])

    @property
    def data(self):
        return self.handle[self.mat_key].data

    @property
    def ebounds_data(self):
        return self.handle["EBOUNDS"].data

    def __str__(self):
        return self.filename

    def _make_channels(self, k):
        # build channel number list associated to array value,
        # there are groups of channels in rmfs with nonzero probabilities
        trueChannel = []
        f_chan = ensure_numpy_array(np.nan_to_num(self.data["F_CHAN"][k]))
        n_chan = ensure_numpy_array(np.nan_to_num(self.data["N_CHAN"][k]))
        for start, nchan in zip(f_chan, n_chan):
            if nchan == 0:
                trueChannel.append(start)
            else:
                trueChannel += list(range(start, start + nchan))
        return np.array(trueChannel)

    def e_to_ch(self, energy):
        energy = parse_value(energy, "keV")
        return np.searchsorted(self.ebounds_data["E_MIN"], energy)-1

    def scatter_energies(self, events, prng=None):
        """
        Scatter photon energies with the RMF and produce the 
        corresponding channel values.

        Parameters
        ----------
        events : dict of np.ndarrays
            The energies and positions of the photons. 
        prng : :class:`~numpy.random.RandomState` object, integer, or None
            A pseudo-random number generator. Typically will only 
            be specified if you have a reason to generate the same 
            set of random numbers, such as for a test. Default is None, 
            which sets the seed based on the system time. 
        """
        prng = parse_prng(prng)
        eidxs = np.argsort(events["energy"])
        sorted_e = events["energy"][eidxs]

        detectedChannels = []

        # run through all photon energies and find which bin they go in
        fcurr = 0
        last = sorted_e.shape[0]

        emin = sorted_e[0]
        emax = sorted_e[-1]

        pbar = tqdm(leave=True, total=last, desc="Scattering energies ")
        for (k, low), high in zip(enumerate(self.elo), self.ehi):
            if high < emin or low > emax:
                continue
            e = sorted_e[fcurr:last]
            nn = np.logical_and(low <= e, e < high).sum()
            if nn == 0:
                continue
            # weight function for probabilities from RMF
            weights = np.nan_to_num(np.float64(self.data["MATRIX"][k]))
            weights /= weights.sum()
            trueChannel = self._make_channels(k)
            if len(trueChannel) > 0:
                channelInd = prng.choice(len(weights), size=nn, p=weights)
                detectedChannels.append(trueChannel[channelInd])
                fcurr += nn
                pbar.update(nn)

        pbar.close()

        for key in events:
            events[key] = events[key][eidxs]
        events[self.header["CHANTYPE"]] = np.concatenate(detectedChannels)

        return events

    def convolve_spectrum(self, cspec, exp_time, noisy=True, prng=None):
        prng = parse_prng(prng)
        exp_time = parse_value(exp_time, "s")
        counts = cspec.flux.value * exp_time * cspec.de.value
        spec = np.histogram(cspec.emid.value, self.ebins, weights=counts)[0]
        conv_spec = np.zeros(self.n_ch)
        pbar = tqdm(leave=True, total=self.n_e, desc="Convolving spectrum ")
        if np.all(self.data["N_GRP"] == 1):
            # We can do things a bit faster if there is only one group each
            f_chan = ensure_numpy_array(np.nan_to_num(self.data["F_CHAN"]))
            n_chan = ensure_numpy_array(np.nan_to_num(self.data["N_CHAN"]))
            mat = np.nan_to_num(np.float64(self.data["MATRIX"]))
            mat_size = np.minimum(n_chan, self.n_ch-f_chan)
            for k in range(self.n_e):
                conv_spec[f_chan[k]:f_chan[k]+n_chan[k]] += spec[k]*mat[k,:mat_size[k]]
                pbar.update()
        else:
            # Otherwise, we have to go step-by-step
            for k in range(self.n_e):
                f_chan = ensure_numpy_array(np.nan_to_num(self.data["F_CHAN"][k]))
                n_chan = ensure_numpy_array(np.nan_to_num(self.data["N_CHAN"][k]))
                mat = np.nan_to_num(np.float64(self.data["MATRIX"][k]))
                mat_size = np.minimum(n_chan, self.n_ch-f_chan)
                for i, f in enumerate(f_chan):
                    conv_spec[f:f+n_chan[i]] += spec[k]*mat[:mat_size[i]]
                pbar.update()
        pbar.close()
        if noisy:
            return prng.poisson(lam=conv_spec)
        else:
            return conv_spec


def perform_dither(t, dither_dict):
    if dither_dict["dither_on"]:
        a = 2.0*np.pi/dither_dict["x_period"]
        b = 2.0*np.pi/dither_dict["y_period"]
        A = dither_dict["x_amp"]/dither_dict["plate_scale"]
        B = dither_dict["y_amp"]/dither_dict["plate_scale"]
        x_offset = A*np.sin(a*t)
        y_offset = B*np.sin(b*t)
    else:
        x_offset = np.zeros(t.size)
        y_offset = np.zeros(t.size)
    return x_offset, y_offset


def generate_events(input_events, exp_time, instrument, sky_center, no_dither=False, dither_params=None,
                    roll_angle=0.0, subpixel_res=False, prng=None):
    """
    Take unconvolved events and convolve them with instrumental responses. This 
    function does the following:

    1. Determines which events are observed using the ARF
    2. Pixelizes the events, applying PSF effects and dithering
    3. Determines energy channels using the RMF

    This function is not meant to be called by the end-user but is used by
    the :func:`~xcs_soxs.instrument.instrument_simulator` function.

    Parameters
    ----------
    input_events : string, dict, or None
        The unconvolved events to be used as input. Can be one of the
        following:
        1. The name of a SIMPUT catalog file.
        2. A Python dictionary containing the following items:
        "ra": A NumPy array of right ascension values in degrees.
        "dec": A NumPy array of declination values in degrees.
        "energy": A NumPy array of energy values in keV.
        "flux": The flux of the entire source, in units of erg/cm**2/s.
    out_file : string
        The name of the event file to be written.
    exp_time : float, (value, unit) tuple, or :class:`~astropy.units.Quantity`
        The exposure time to use, in seconds. 
    instrument : string
        The name of the instrument to use, which picks an instrument
        specification from the instrument registry. 
    sky_center : array, tuple, or list
        The center RA, Dec coordinates of the observation, in degrees.
    no_dither : boolean, optional
        If True, turn off dithering entirely. Default: False
    dither_params : array-like of floats, optional
        The parameters to use to control the size and period of the dither
        pattern. The first two numbers are the dither amplitude in x and y
        detector coordinates in arcseconds, and the second two numbers are
        the dither period in x and y detector coordinates in seconds. 
        Default: [8.0, 8.0, 1000.0, 707.0].
    roll_angle : float, (value, unit) tuple, or :class:`~astropy.units.Quantity`, optional
        The roll angle of the observation in degrees. Default: 0.0
    subpixel_res: boolean, optional
        If True, event positions are not randomized within the pixels 
        within which they are detected. Default: False
    prng : :class:`~numpy.random.RandomState` object, integer, or None
        A pseudo-random number generator. Typically will only 
        be specified if you have a reason to generate the same 
        set of random numbers, such as for a test. Default is None, 
        which sets the seed based on the system time. 
    """
    import pyregion._region_filter as rfilter

    def pixel_evts(sky_evts, inst_name, external, ccf, expmap):
        mylog.info("Pixeling events.")

        if external and "xmm" in inst_name:
            ra_col = pyfits.Column(name='RA', array=sky_evts["ra"], format='D')
            dec_col = pyfits.Column(name='DEC', array=sky_evts["dec"], format='D')
            chip_table = pyfits.BinTableHDU.from_columns([ra_col, dec_col])
            chip_table.name = "EVENTS"
            # chip_table.header["DATE"] = date
            temp_evts_name = "temp_events_{}.fits".format(datetime.today().timestamp())
            chip_table.writeto(temp_evts_name, overwrite=True)

            os.environ["SAS_CCF"] = get_response_path(ccf)
            call("esky2det datastyle=set intab={n} calinfostyle=set "
                 "calinfoset={d} outunit=det".format(d=get_response_path(expmap), n=temp_evts_name), stdout=PIPE,
                 stdin=PIPE, stderr=PIPE, shell=True)
            with pyfits.open(temp_evts_name) as temp:
                sky_evts["detx"] = temp["EVENTS"].data["DETX"]
                detx_nan = np.isnan(sky_evts["detx"])
                sky_evts["dety"] = temp["EVENTS"].data["DETY"]
            for evts_key in sky_evts:
                sky_evts[evts_key] = sky_evts[evts_key][~detx_nan]
            os.remove(temp_evts_name)
        elif external and "xmm" not in inst_name.lower():
            raise NotImplementedError("Using external coordinate conversion currently only supports XMM instruments")
        elif not external:
            # Convert RA, Dec to pixel coordinates
            x_pix_coord, y_pix_coord = w.wcs_world2pix(sky_evts["ra"], sky_evts["dec"], 1)
            x_pix_coord -= event_params["pix_center"][0]
            y_pix_coord -= event_params["pix_center"][1]

            # Rotate physical coordinates to detector coordinates
            det_rot = np.dot(rot_mat, np.array([x_pix_coord, y_pix_coord]))
            sky_evts["detx"] = det_rot[0, :] + event_params["aimpt_coords"][0]
            sky_evts["dety"] = det_rot[1, :] + event_params["aimpt_coords"][1]

        # Convert detector coordinate to "chip coordinates", needed to use the region filters for the different chips
        sky_evts["cx"] = np.trunc(sky_evts["detx"]) + 0.5 * np.sign(sky_evts["detx"])
        sky_evts["cy"] = np.trunc(sky_evts["dety"]) + 0.5 * np.sign(sky_evts["dety"])

        return sky_evts

    exp_time = parse_value(exp_time, "s")
    roll_angle = parse_value(roll_angle, "deg")
    prng = parse_prng(prng)
    if isinstance(input_events, dict):
        parameters = {}
        for key in ["flux", "emin", "emax", "sources"]:
            parameters[key] = input_events[key]
        event_list = []
        for i in range(len(parameters["flux"])):
            edict = {}
            for key in ["ra", "dec", "energy"]:
                edict[key] = input_events[key][i]
            event_list.append(edict)
    elif isinstance(input_events, string_types):
        # Assume this is a SIMPUT catalog
        event_list, parameters = read_simput_catalog(input_events)

    try:
        instrument_spec = instrument_registry[instrument]
    except KeyError:
        raise KeyError("Instrument %s is not in the instrument registry!" % instrument)
    if not instrument_spec["imaging"]:
        raise RuntimeError("Instrument '%s' is not " % instrument_spec["name"] +
                           "designed for imaging observations!")

    if isinstance(instrument_spec["response_regions"], list):
        if not isinstance(instrument_spec["arf"], list):
            raise RuntimeError("Instrument {i} has response regions, "
                               "please supply a list of arf files!".format(i=instrument_spec["name"]))
        elif len(instrument_spec["arf"]) == 0 or len(instrument_spec["response_regions"]) == 0:
            raise RuntimeError("Instrument {i} has a zero-length list for its arf or "
                               "response_regions entry".format(i=instrument_spec["name"]))
        elif len(instrument_spec["arf"]) != len(instrument_spec["response_regions"]):
            raise RuntimeError("Instrument {i}'s arf and response_regions entries "
                               "are not the same length".format(i=instrument_spec["name"]))
        else:
            arf_files = list(map(get_response_path, instrument_spec["arf"]))
            rmf_file = get_response_path(instrument_spec["rmf"])
            arf = SpatialARF(arf_files, instrument_spec["response_regions"])
            rmf = RedistributionMatrixFile(rmf_file)
    elif instrument_spec["response_regions"] is not None:
        raise RuntimeError("Instrument {i} response_regions entry "
                           "should either be None or a list".format(i=instrument_spec["name"]))
    elif instrument_spec["response_regions"] is None:
        arf_file = get_response_path(instrument_spec["arf"])
        rmf_file = get_response_path(instrument_spec["rmf"])
        arf = AuxiliaryResponseFile(arf_file)
        rmf = RedistributionMatrixFile(rmf_file)

    if not isinstance(instrument_spec["external_coord_conv"], bool):
        raise TypeError("external_coord_conv must be boolean")
    elif instrument_spec["external_coord_conv"] and instrument_spec["expmap"] is None:
        raise TypeError("If using external coordinate conversion, expmap entry cannot be None")

    nx = instrument_spec["num_pixels"]
    plate_scale = instrument_spec["fov"]/nx/60.  # arcmin to deg
    plate_scale_arcsec = plate_scale * 3600.0

    if not instrument_spec["dither"]:
        dither_on = False
    else:
        dither_on = not no_dither
    if dither_params is None:
        dither_params = [8.0, 8.0, 1000.0, 707.0]
    dither_dict = {"x_amp": dither_params[0],
                   "y_amp": dither_params[1],
                   "x_period": dither_params[2],
                   "y_period": dither_params[3],
                   "dither_on": dither_on,
                   "plate_scale": plate_scale_arcsec}

    event_params = {}
    event_params["exposure_time"] = exp_time
    event_params["arf"] = arf.filename
    event_params["sky_center"] = sky_center
    event_params["pix_center"] = np.array([0.5*(2*nx+1)]*2)
    event_params["num_pixels"] = nx
    event_params["plate_scale"] = plate_scale
    event_params["rmf"] = rmf.filename
    event_params["channel_type"] = rmf.header["CHANTYPE"]
    event_params["telescope"] = rmf.header["TELESCOP"]
    event_params["instrument"] = instrument_spec['name']
    event_params["mission"] = rmf.header.get("MISSION", "")
    event_params["nchan"] = rmf.n_ch
    event_params["roll_angle"] = roll_angle
    event_params["fov"] = instrument_spec["fov"]
    event_params["chan_lim"] = [rmf.cmin, rmf.cmax]
    event_params["chips"] = instrument_spec["chips"]
    event_params["dither_params"] = dither_dict
    event_params["aimpt_coords"] = instrument_spec["aimpt_coords"]
    event_params["response_regions"] = instrument_spec["response_regions"]

    w = pywcs.WCS(naxis=2)
    w.wcs.crval = event_params["sky_center"]
    w.wcs.crpix = event_params["pix_center"]
    w.wcs.cdelt = [-plate_scale, plate_scale]
    w.wcs.ctype = ["RA---TAN", "DEC--TAN"]
    w.wcs.cunit = ["deg"]*2

    rot_mat = get_rot_mat(roll_angle)

    all_events = defaultdict(list)
    for i, evts in enumerate(event_list):
        mylog.info("Detecting events from source %s." % parameters["sources"][i])
        # Step 1: Assign detector coordinates to events, then use ARF to determine which photons are observed, or the
        # other way around if no spatial responses.
        refband = [parameters["emin"][i], parameters["emax"][i]]
        if instrument_spec["response_regions"] is None:
            mylog.info("Applying energy-dependent effective area from %s" % os.path.split(arf.filename)[-1])
            events = arf.detect_events(evts, exp_time, parameters["flux"][i], refband, prng=prng)
            events = pixel_evts(events, instrument_spec["name"], instrument_spec["external_coord_conv"], None, None)
        else:
            evts = pixel_evts(evts, instrument_spec["name"], instrument_spec["external_coord_conv"],
                              instrument_spec["ccf"], instrument_spec["expmap"])
            mylog.info("Applying {i}'s position and energy-dependent effective area".format(i=instrument_spec["name"]))
            events = arf.detect_events(evts, exp_time, parameters["flux"][i], refband, prng=prng)

        n_evt = events["energy"].size

        if n_evt == 0:
            mylog.warning("No events were observed for this source!!!")
        else:
            # Step 2: Apply dithering and PSF. Clip events that don't fall within the detection region.
            # Add times to events
            events['time'] = prng.uniform(size=n_evt, low=0.0,
                                          high=event_params["exposure_time"])

            detx = events["detx"]
            dety = events["dety"]

            # Apply dithering
            x_offset, y_offset = perform_dither(events["time"], dither_dict)
            detx -= x_offset
            dety -= y_offset

            # PSF scattering of detector coordinates
            if instrument_spec["psf"] is not None:
                psf_type, psf_spec = instrument_spec["psf"]
                if psf_type == "gaussian":
                    sigma = psf_spec/sigma_to_fwhm/plate_scale_arcsec
                    detx += prng.normal(loc=0.0, scale=sigma, size=n_evt)
                    dety += prng.normal(loc=0.0, scale=sigma, size=n_evt)
                elif psf_type.lower() == "sas":
                    # This is a very crude way to get a central coordinate for PSF generation
                    av_ra = events["ra"].mean()
                    av_dec = events["dec"].mean()
                    # PSF differs for different energies, so dividing up into chunks of 0.5keV
                    en_step_num = np.ceil(events["energy"].max() / 0.5).astype(int)
                    en_bin_bounds = np.arange(0, en_step_num+1)*0.5
                    en_bin_mids = ((en_bin_bounds[:-1]+0.25)*1000).astype(int).astype(str)
                    psf_name = "psf_{}.fits".format(datetime.today().timestamp())
                    psf_cmd = "psfgen image={i} energy='{el}' coordtype=EQPOS x={ra} y={dec} xsize=400 ysize=400 " \
                              "level=ELLBETA output={n}".format(i=instrument_spec["og_image"], el=' '.join(en_bin_mids),
                                                                ra=av_ra, dec=av_dec, n=psf_name)

                    os.environ["SAS_CCF"] = get_response_path(instrument_spec["ccf"])
                    with open(psf_name.split('.')[0] + ".log", 'w') as loggy:
                        call(psf_cmd, shell=True, stdout=loggy, stdin=PIPE, stderr=loggy)

                    evt_idx = np.arange(0, len(events["energy"]), 1).astype(int)
                    psf_obj = pyfits.open(psf_name)

                    for mid_ind, mid in enumerate(en_bin_mids):
                        cur_psf = psf_obj[mid_ind+1].data
                        cur_wcs = pywcs.WCS(psf_obj[mid_ind+1].header)
                        cur_psf /= cur_psf.sum()
                        flat_psf = cur_psf.flatten()
                        psf_ind = np.indices(cur_psf.shape)
                        y_lookup = psf_ind[0, :, :].flatten()
                        y_lookup = np.append(y_lookup, y_lookup[-1])
                        y_lookup = np.insert(y_lookup, 0, y_lookup[0])
                        x_lookup = psf_ind[1, :, :].flatten()
                        x_lookup = np.append(x_lookup, x_lookup[-1])
                        x_lookup = np.insert(x_lookup, 0, x_lookup[0])
                        psf_cdf = np.cumsum(flat_psf)
                        psf_cdf = np.append(psf_cdf, 1)
                        psf_cdf = np.insert(psf_cdf, 0, 0)

                        bounded_events = evt_idx[(en_bin_bounds[mid_ind] <= events["energy"]) &
                                                 (events["energy"] < en_bin_bounds[mid_ind+1])]
                        num_to_gen = len(bounded_events)

                        rand_samples = np.random.uniform(low=0, high=1, size=num_to_gen)
                        inv_cdf = interpolate.interp1d(psf_cdf, np.arange(0, len(psf_cdf)))
                        inv_cdf_vals = inv_cdf(rand_samples).astype(int)
                        ys = y_lookup[inv_cdf_vals]
                        xs = x_lookup[inv_cdf_vals]
                        ra_samples, dec_samples = cur_wcs.all_pix2world(xs, ys, 0)
                        ra_samples_diff = ra_samples - av_ra
                        dec_samples_diff = dec_samples - av_dec

                        events["ra"][bounded_events] += ra_samples_diff
                        events["dec"][bounded_events] += dec_samples_diff
                    events["x_offset_temp"] = x_offset
                    events["y_offset_temp"] = y_offset
                    events = pixel_evts(events, instrument_spec["name"], instrument_spec["external_coord_conv"],
                                        instrument_spec["ccf"], instrument_spec["expmap"])
                    n_evt = events["energy"].size
                    detx = events["detx"]
                    dety = events["dety"]
                    x_offset = events["x_offset_temp"]
                    y_offset = events["y_offset_temp"]
                    os.remove(psf_name)
                else:
                    raise NotImplementedError("PSF type %s not implemented!" % psf_type)

            # Convert detector coordinates to chip coordinates.
            # Throw out events that don't fall on any chip.

            cx = np.trunc(detx)+0.5*np.sign(detx)
            cy = np.trunc(dety)+0.5*np.sign(dety)

            if event_params["chips"] is None:
                events["chip_id"] = np.zeros(n_evt, dtype='int')
                keepx = np.logical_and(cx >= -0.5*nx, cx <= 0.5*nx)
                keepy = np.logical_and(cy >= -0.5*nx, cy <= 0.5*nx)
                keep = np.logical_and(keepx, keepy)
            else:
                events["chip_id"] = -np.ones(n_evt, dtype='int')
                for i, chip in enumerate(event_params["chips"]):
                    thisc = np.ones(n_evt, dtype='bool')
                    rtype = chip[0]
                    args = chip[1:]
                    r = getattr(rfilter, rtype)(*args)
                    inside = r.inside(cx, cy)
                    thisc = np.logical_and(thisc, inside)
                    events["chip_id"][thisc] = i
                keep = events["chip_id"] > -1

            mylog.info("%d events were rejected because " % (n_evt-keep.sum()) +
                       "they do not fall on any CCD.")
            n_evt = keep.sum()

            if n_evt == 0:
                mylog.warning("No events are within the field of view for this source!!!")
            else:

                # Keep only those events which fall on a chip

                for key in events:
                    events[key] = events[key][keep]

                # Convert chip coordinates back to detector coordinates, unless the
                # user has specified that they want subpixel resolution

                if subpixel_res:
                    events["detx"] = detx[keep]
                    events["dety"] = dety[keep]
                else:
                    events["detx"] = cx[keep] + prng.uniform(low=-0.5, high=0.5, size=n_evt)
                    events["dety"] = cy[keep] + prng.uniform(low=-0.5, high=0.5, size=n_evt)

                # Convert detector coordinates back to pixel coordinates by
                # adding the dither offsets back in and applying the rotation
                # matrix again

                det = np.array([events["detx"] + x_offset[keep] - event_params["aimpt_coords"][0],
                                events["dety"] + y_offset[keep] - event_params["aimpt_coords"][1]])
                pix = np.dot(rot_mat.T, det)

                events["xpix"] = pix[0, :] + event_params['pix_center'][0]
                events["ypix"] = pix[1, :] + event_params['pix_center'][1]

        if n_evt > 0:
            for key in events:
                all_events[key] = np.concatenate([all_events[key], events[key]])

    if len(all_events["energy"]) == 0:
        mylog.warning("No events from any of the sources in the catalog were detected!")
        for key in ["xpix", "ypix", "detx", "dety", "time", "chip_id", event_params["channel_type"]]:
            all_events[key] = np.array([])
    else:
        # Step 4: Scatter energies with RMF
        mylog.info("Scattering energies with RMF %s." % os.path.split(rmf.filename)[-1])
        all_events = rmf.scatter_energies(all_events, prng=prng)

    return all_events, event_params


def make_background(exp_time, instrument, sky_center, foreground=True, 
                    ptsrc_bkgnd=True, instr_bkgnd=True, no_dither=False,
                    dither_params=None, roll_angle=0.0, subpixel_res=False, 
                    input_sources=None, absorb_model="wabs", nH=0.05, prng=None):
    """
    Make background events. 

    Parameters
    ----------
    exp_time : float, (value, unit) tuple, or :class:`~astropy.units.Quantity`
        The exposure time to use, in seconds. 
    instrument : string
        The name of the instrument to use, which picks an instrument
        specification from the instrument registry. 
    sky_center : array, tuple, or list
        The center RA, Dec coordinates of the observation, in degrees.
    foreground : boolean, optional
        Whether or not to include the Galactic foreground. Default: True
    instr_bkgnd : boolean, optional
        Whether or not to include the instrumental background. Default: True
    no_dither : boolean, optional
        If True, turn off dithering entirely. Default: False
    dither_params : array-like of floats, optional
        The parameters to use to control the size and period of the dither
        pattern. The first two numbers are the dither amplitude in x and y
        detector coordinates in arcseconds, and the second two numbers are
        the dither period in x and y detector coordinates in seconds. 
        Default: [8.0, 8.0, 1000.0, 707.0].
    ptsrc_bkgnd : boolean, optional
        Whether or not to include the point-source background. Default: True
        Default: 0.05
    roll_angle : float, (value, unit) tuple, or :class:`~astropy.units.Quantity`, optional
        The roll angle of the observation in degrees. Default: 0.0
    subpixel_res: boolean, optional
        If True, event positions are not randomized within the pixels 
        within which they are detected. Default: False
    input_sources : string, optional
        If set to a filename, input the point source positions, fluxes,
        and spectral indices from an ASCII table instead of generating
        them. Default: None
    absorb_model : string, optional
        The absorption model to use, "wabs" or "tbabs". Default: "wabs"
    nH : float, optional
        The hydrogen column in units of 10**22 atoms/cm**2. 
        Default: 0.05
    prng : :class:`~numpy.random.RandomState` object, integer, or None
        A pseudo-random number generator. Typically will only 
        be specified if you have a reason to generate the same 
        set of random numbers, such as for a test. Default is None, 
        which sets the seed based on the system time. 
    """
    from xcs_soxs.background import make_instrument_background, \
        make_foreground, make_ptsrc_background
    prng = parse_prng(prng)
    exp_time = parse_value(exp_time, "s")
    roll_angle = parse_value(roll_angle, "deg")
    try:
        instrument_spec = instrument_registry[instrument]
    except KeyError:
        raise KeyError("Instrument %s is not in the instrument registry!" % instrument)
    if not instrument_spec["imaging"]:
        raise RuntimeError("Instrument '%s' is not " % instrument_spec["name"] +
                           "designed for imaging observations!")
    fov = instrument_spec["fov"]

    input_events = defaultdict(list)

    arf_file = get_response_path(instrument_spec["arf"])
    arf = AuxiliaryResponseFile(arf_file)
    rmf_file = get_response_path(instrument_spec["rmf"])
    rmf = RedistributionMatrixFile(rmf_file)

    if ptsrc_bkgnd:
        mylog.info("Adding in point-source background.")
        ptsrc_events = make_ptsrc_background(exp_time, fov, sky_center,
                                             area=1.2*arf.max_area,
                                             input_sources=input_sources, 
                                             absorb_model=absorb_model,
                                             nH=nH, prng=prng)
        for key in ["ra", "dec", "energy"]:
            input_events[key].append(ptsrc_events[key])
        input_events["flux"].append(ptsrc_events["flux"])
        input_events["emin"].append(ptsrc_events["energy"].min())
        input_events["emax"].append(ptsrc_events["energy"].max())
        input_events["sources"].append("ptsrc_bkgnd")
        events, event_params = generate_events(input_events, exp_time,
                                               instrument, sky_center,
                                               no_dither=no_dither,
                                               dither_params=dither_params, 
                                               roll_angle=roll_angle,
                                               subpixel_res=subpixel_res,
                                               prng=prng)
        mylog.info("Generated %d photons from the point-source background." % len(events["energy"]))
    else:
        nx = instrument_spec["num_pixels"]
        events = defaultdict(list)
        if not instrument_spec["dither"]:
            dither_on = False
        else:
            dither_on = not no_dither
        if dither_params is None:
            dither_params = [8.0, 8.0, 1000.0, 707.0]
        dither_dict = {"x_amp": dither_params[0],
                       "y_amp": dither_params[1],
                       "x_period": dither_params[2],
                       "y_period": dither_params[3],
                       "dither_on": dither_on,
                       "plate_scale": instrument_spec["fov"]/nx*60.0}
        event_params = {"exposure_time": exp_time, 
                        "fov": instrument_spec["fov"],
                        "num_pixels": nx,
                        "pix_center": np.array([0.5*(2*nx+1)]*2),
                        "channel_type": rmf.header["CHANTYPE"],
                        "sky_center": sky_center,
                        "dither_params": dither_dict,
                        "plate_scale": instrument_spec["fov"]/nx/60.0,
                        "chan_lim": [rmf.cmin, rmf.cmax],
                        "rmf": rmf_file, "arf": arf_file,
                        "telescope": rmf.header["TELESCOP"],
                        "instrument": instrument_spec['name'],
                        "mission": rmf.header.get("MISSION", ""),
                        "nchan": rmf.n_ch,
                        "roll_angle": roll_angle,
                        "aimpt_coords": instrument_spec["aimpt_coords"]}

    if "chips" not in event_params:
        event_params["chips"] = instrument_spec["chips"]

    if foreground:
        mylog.info("Adding in astrophysical foreground.")
        bkg_events = make_foreground(event_params, arf, rmf, prng=prng)
        for key in bkg_events:
            events[key] = np.concatenate([events[key], bkg_events[key]])
    if instr_bkgnd and instrument_spec["bkgnd"] is not None:
        mylog.info("Adding in instrumental background.")
        bkg_events = make_instrument_background(instrument_spec["bkgnd"], 
                                                event_params, rmf, prng=prng)
        for key in bkg_events:
            events[key] = np.concatenate([events[key], bkg_events[key]])

    return events, event_params


def make_background_file(out_file, exp_time, instrument, sky_center,
                         overwrite=False, foreground=True, instr_bkgnd=True,
                         ptsrc_bkgnd=True, no_dither=False, dither_params=None,
                         subpixel_res=False, input_sources=None, 
                         absorb_model="wabs", nH=0.05, prng=None):
    """
    Make an event file consisting entirely of background events. This will be 
    useful for creating backgrounds that can be added to simulations of sources.

    Parameters
    ----------
    exp_time : float, (value, unit) tuple, or :class:`~astropy.units.Quantity`
        The exposure time to use, in seconds. 
    instrument : string
        The name of the instrument to use, which picks an instrument
        specification from the instrument registry. 
    sky_center : array, tuple, or list
        The center RA, Dec coordinates of the observation, in degrees.
    overwrite : boolean, optional
        Whether or not to overwrite an existing file with the same name.
        Default: False
    foreground : boolean, optional
        Whether or not to include the Galactic foreground. Default: True
    instr_bkgnd : boolean, optional
        Whether or not to include the instrumental background. Default: True
    ptsrc_bkgnd : boolean, optional
        Whether or not to include the point-source background. Default: True
    no_dither : boolean, optional
        If True, turn off dithering entirely. Default: False
    dither_params : array-like of floats, optional
        The parameters to use to control the size and period of the dither
        pattern. The first two numbers are the dither amplitude in x and y
        detector coordinates in arcseconds, and the second two numbers are
        the dither period in x and y detector coordinates in seconds. 
        Default: [8.0, 8.0, 1000.0, 707.0].
    subpixel_res: boolean, optional
        If True, event positions are not randomized within the pixels 
        within which they are detected. Default: False
    input_sources : string, optional
        If set to a filename, input the point source positions, fluxes,
        and spectral indices from an ASCII table instead of generating
        them. Default: None
    absorb_model : string, optional
        The absorption model to use, "wabs" or "tbabs". Default: "wabs"
    nH : float, optional
        The hydrogen column in units of 10**22 atoms/cm**2. 
        Default: 0.05
    prng : :class:`~numpy.random.RandomState` object, integer, or None
        A pseudo-random number generator. Typically will only 
        be specified if you have a reason to generate the same 
        set of random numbers, such as for a test. Default is None, 
        which sets the seed based on the system time. 
    """
    prng = parse_prng(prng)
    events, event_params = make_background(exp_time, instrument, sky_center, 
                                           ptsrc_bkgnd=ptsrc_bkgnd, 
                                           foreground=foreground, 
                                           instr_bkgnd=instr_bkgnd,
                                           no_dither=no_dither,
                                           dither_params=dither_params, 
                                           subpixel_res=subpixel_res,
                                           input_sources=input_sources,
                                           absorb_model=absorb_model,
                                           nH=nH, prng=prng)
    write_event_file(events, event_params, out_file, overwrite=overwrite)


def instrument_simulator(input_events, out_file, exp_time, instrument,
                         sky_center, overwrite=False, instr_bkgnd=True, 
                         foreground=True, ptsrc_bkgnd=True, 
                         bkgnd_file=None, no_dither=False, 
                         dither_params=None, roll_angle=0.0, 
                         subpixel_res=False, prng=None):
    """
    Take unconvolved events and create an event file from them. This
    function calls generate_events to do the following:

    1. Determines which events are observed using the ARF
    2. Pixelizes the events, applying PSF effects and dithering
    3. Determines energy channels using the RMF

    and then calls make_background to add instrumental and astrophysical
    backgrounds, unless a background file is provided, in which case
    the background events are read from this file. The events are
    then written out to a file.

    Parameters
    ----------
    input_events : string, dict, or None
        The unconvolved events to be used as input. Can be one of the
        following:
        1. The name of a SIMPUT catalog file.
        2. A Python dictionary containing the following items:
        "ra": A NumPy array of right ascension values in degrees.
        "dec": A NumPy array of declination values in degrees.
        "energy": A NumPy array of energy values in keV.
        "flux": The flux of the entire source, in units of erg/cm**2/s.
    out_file : string
        The name of the event file to be written.
    exp_time : float, (value, unit) tuple, or :class:`~astropy.units.Quantity`
        The exposure time to use, in seconds. 
    instrument : string
        The name of the instrument to use, which picks an instrument
        specification from the instrument registry. 
    sky_center : array, tuple, or list
        The center RA, Dec coordinates of the observation, in degrees.
    overwrite : boolean, optional
        Whether or not to overwrite an existing file with the same name.
        Default: False
    instr_bkgnd : boolean, optional
        Whether or not to include the instrumental/particle background. 
        Default: True
    foreground : boolean, optional
        Whether or not to include the local foreground. 
        Default: True
    ptsrc_bkgnd : boolean, optional
        Whether or not to include the point-source background. 
        Default: True
    bkgnd_file : string, optional
        If set, backgrounds will be loaded from this file and not generated
        on the fly. Default: None
    no_dither : boolean, optional
        If True, turn off dithering entirely. Default: False
    dither_params : array-like of floats, optional
        The parameters to use to control the size and period of the dither
        pattern. The first two numbers are the dither amplitude in x and y
        detector coordinates in arcseconds, and the second two numbers are
        the dither period in x and y detector coordinates in seconds. 
        Default: [8.0, 8.0, 1000.0, 707.0].
    roll_angle : float, (value, unit) tuple, or :class:`~astropy.units.Quantity`, optional
        The roll angle of the observation in degrees. Default: 0.0
    subpixel_res: boolean, optional
        If True, event positions are not randomized within the pixels 
        within which they are detected. Default: False
    prng : :class:`~numpy.random.RandomState` object, integer, or None
        A pseudo-random number generator. Typically will only 
        be specified if you have a reason to generate the same 
        set of random numbers, such as for a test. Default is None, 
        which sets the seed based on the system time. 

    Examples
    --------
    >>> instrument_simulator("sloshing_simput.fits", "sloshing_evt.fits", 
    ...                      300000.0, "hdxi_3x10", [30., 45.], overwrite=True)
    """
    from xcs_soxs.background import add_background_from_file
    if not out_file.endswith(".fits"):
        out_file += ".fits"
    mylog.info("Making observation of source in %s." % out_file)
    # Make the source first
    events, event_params = generate_events(input_events, exp_time, instrument, sky_center,
                                           no_dither=no_dither, dither_params=dither_params, 
                                           roll_angle=roll_angle, subpixel_res=subpixel_res, 
                                           prng=prng)
    # If the user wants backgrounds, either make the background or add an already existing
    # background event file. It may be necessary to reproject events to a new coordinate system.
    if bkgnd_file is None:
        if not instr_bkgnd and not ptsrc_bkgnd and not foreground:
            mylog.info("No backgrounds will be added to this observation.")
        else:
            mylog.info("Adding background events.")
            bkg_events, _ = make_background(exp_time, instrument, sky_center,
                                            foreground=foreground, instr_bkgnd=instr_bkgnd, 
                                            no_dither=no_dither, dither_params=dither_params, 
                                            ptsrc_bkgnd=ptsrc_bkgnd, prng=prng, 
                                            subpixel_res=subpixel_res, roll_angle=roll_angle)
            for key in events:
                events[key] = np.concatenate([events[key], bkg_events[key]])
    else:
        mylog.info("Adding background events from the file %s." % bkgnd_file)
        if not os.path.exists(bkgnd_file):
            raise IOError("Cannot find the background event file %s!" % bkgnd_file)
        events = add_background_from_file(events, event_params, bkgnd_file)
    if len(events["energy"]) == 0:
        raise RuntimeError("No events were detected from source or background!!")
    write_event_file(events, event_params, out_file, overwrite=overwrite)
    mylog.info("Observation complete.")


def simulate_spectrum(spec, instrument, exp_time, out_file,
                      instr_bkgnd=False, foreground=False,
                      ptsrc_bkgnd=False, bkgnd_area=None,
                      absorb_model="wabs", nH=0.05,
                      overwrite=False, prng=None):
    """
    Generate a PI or PHA spectrum from a :class:`~xcs_soxs.spectra.Spectrum`
    by convolving it with responses. To be used if one wants to 
    create a spectrum without worrying about spatial response. Similar
    to XSPEC's "fakeit".

    Parameters
    ----------
    spec : :class:`~soxs.spectra.Spectrum`
        The spectrum to be convolved. If None is supplied, only backgrounds
        will be simulated (if they are turned on).
    instrument : string
        The name of the instrument to use, which picks an instrument
        specification from the instrument registry.
    exp_time : float, (value, unit) tuple, or :class:`~astropy.units.Quantity`
        The exposure time in seconds.
    out_file : string
        The file to write the spectrum to.
    instr_bkgnd : boolean, optional
        Whether or not to include the instrumental/particle background. 
        Default: False
    foreground : boolean, optional
        Whether or not to include the local foreground.
        Default: False
    ptsrc_bkgnd : boolean, optional
        Whether or not to include the unresolved point-source background. 
        Default: False
    bkgnd_area : float, (value, unit) tuple, or :class:`~astropy.units.Quantity`
        The area on the sky for the background components, in square arcminutes.
        Default: None, necessary to specify if any of the background components
        are turned on. 
    absorb_model : string, optional
        The absorption model to use, "wabs" or "tbabs". Default: "wabs"
    nH : float, optional
        The hydrogen column in units of 10**22 atoms/cm**2. 
        Default: 0.05
    overwrite : boolean, optional
        Whether or not to overwrite an existing file. Default: False
    prng : :class:`~numpy.random.RandomState` object, integer, or None
        A pseudo-random number generator. Typically will only 
        be specified if you have a reason to generate the same 
        set of random numbers, such as for a test. Default is None, 
        which sets the seed based on the system time. 

    Examples
    --------
    >>> spec = xcs_soxs.Spectrum.from_file("my_spectrum.txt")
    >>> xcs_soxs.simulate_spectrum(spec, "lynx_lxm", 100000.0,
    ...                        "my_spec.pi", overwrite=True)
    """
    from xcs_soxs.events import _write_spectrum
    from xcs_soxs.instrument import RedistributionMatrixFile, \
        AuxiliaryResponseFile
    from xcs_soxs.spectra import ConvolvedSpectrum
    from xcs_soxs.background.foreground import hm_astro_bkgnd
    from xcs_soxs.background.instrument import instrument_backgrounds
    from xcs_soxs.background.spectra import BackgroundSpectrum, \
        ConvolvedBackgroundSpectrum
    prng = parse_prng(prng)
    exp_time = parse_value(exp_time, "s")
    try:
        instrument_spec = instrument_registry[instrument]
    except KeyError:
        raise KeyError("Instrument %s is not in the instrument registry!" % instrument)
    if foreground or instr_bkgnd or ptsrc_bkgnd:
        if instrument_spec["grating"]:
            raise NotImplementedError("Backgrounds cannot be included in simulations "
                                      "of gratings spectra at this time!")
        if bkgnd_area is None:
            raise RuntimeError("The 'bkgnd_area' argument must be set if one wants "
                               "to simulate backgrounds! Specify a value in square "
                               "arcminutes.")
        bkgnd_area = np.sqrt(parse_value(bkgnd_area, "arcmin**2"))
    elif spec is None:
        raise RuntimeError("You have specified no source spectrum and no backgrounds!")
    arf_file = get_response_path(instrument_spec["arf"])
    rmf_file = get_response_path(instrument_spec["rmf"])
    arf = AuxiliaryResponseFile(arf_file)
    rmf = RedistributionMatrixFile(rmf_file)

    event_params = {}
    event_params["RESPFILE"] = os.path.split(rmf.filename)[-1]
    event_params["ANCRFILE"] = os.path.split(arf.filename)[-1]
    event_params["TELESCOP"] = rmf.header["TELESCOP"]
    event_params["INSTRUME"] = rmf.header["INSTRUME"]
    event_params["MISSION"] = rmf.header.get("MISSION", "")

    out_spec = np.zeros(rmf.n_ch)

    if spec is not None:
        cspec = ConvolvedSpectrum(spec, arf)
        out_spec += rmf.convolve_spectrum(cspec, exp_time, prng=prng)

    fov = None if bkgnd_area is None else np.sqrt(bkgnd_area)

    if foreground:
        mylog.info("Adding in astrophysical foreground.")
        cspec_frgnd = ConvolvedSpectrum(hm_astro_bkgnd.to_spectrum(fov), arf)
        out_spec += rmf.convolve_spectrum(cspec_frgnd, exp_time, prng=prng)
    if instr_bkgnd and instrument_spec["bkgnd"] is not None:
        mylog.info("Adding in instrumental background.")
        instr_spec = instrument_backgrounds[instrument_spec["bkgnd"]]
        cspec_instr = instr_spec.to_scaled_spectrum(fov,
                                                    instrument_spec["focal_length"])
        out_spec += rmf.convolve_spectrum(cspec_instr, exp_time, prng=prng)
    if ptsrc_bkgnd:
        mylog.info("Adding in background from unresolved point-sources.")
        spec_plaw = BackgroundSpectrum.from_powerlaw(1.45, 0.0, 2.0e-7, emin=0.01,
                                                     emax=10.0, nbins=300000)
        spec_plaw.apply_foreground_absorption(nH, model=absorb_model)
        cspec_plaw = ConvolvedBackgroundSpectrum(spec_plaw.to_spectrum(fov), arf)
        out_spec += rmf.convolve_spectrum(cspec_plaw, exp_time, prng=prng)

    bins = (np.arange(rmf.n_ch)+rmf.cmin).astype("int32")

    _write_spectrum(bins, out_spec, exp_time, rmf.header["CHANTYPE"], 
                    event_params, out_file, overwrite=overwrite)

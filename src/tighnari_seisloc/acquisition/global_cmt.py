"""Replay a vertical long-period regional-independent CMT with a public Pyrocko store."""

from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from hashlib import file_digest

import numpy as np
from obspy import Stream, UTCDateTime, read, read_inventory
from obspy.clients.fdsn import Client
from obspy.clients.fdsn.header import FDSNException
from obspy.geodetics import gps2dist_azimuth, locations2degrees
from obspy.taup import TauPyModel
from pyproj import Geod

from ..processing.cmt import bandpass, invert, kagan
from ..processing.cmt_research import horizontal_solution
from ..processing.exclusions import filter_stream

BAND = (0.002, 0.008)
LONG_BAND = (0.0015, 0.005)
DT = 5.0
TIMES = np.arange(-600, 2600, DT)
DISTANCE = (20, 90)
DEPTHS = tuple(range(4, 101, 4))
DELAYS = tuple(range(0, 51, 5))
SERVER = "https://service.earthscope.org"
METHOD = "global-qssp-wphase-window-v1"
WAVEFORM_READY_AGE = int(TIMES[-1]) + 10


class NoGlobalWaveforms(RuntimeError):
    """The archive returned no waveforms in the requested window."""


def inventory(event, client, output, *, refresh=False):
    origin = UTCDateTime(event["origin"])
    xml = output / "global.xml"
    if not refresh and xml.is_file():
        return read_inventory(str(xml))
    inventory = client.get_stations(
        network="IU,II,IC,AU,GE,G,US",
        channel="LHZ",
        starttime=origin,
        endtime=origin + 1,
        latitude=event["lat"],
        longitude=event["lon"],
        minradius=DISTANCE[0],
        maxradius=DISTANCE[1],
        level="response",
    )
    temp = xml.with_suffix(".xml.tmp")
    inventory.write(str(temp), format="STATIONXML")
    temp.replace(xml)
    return inventory


def download(event, output, *, reuse_inventory=False):
    client = Client(SERVER, timeout=26)
    origin = UTCDateTime(event["origin"])
    start, end = origin + TIMES[0] - 10, origin + TIMES[-1] + 10
    inv = inventory(event, client, output, refresh=not reuse_inventory)
    sectors = defaultdict(list)
    for net in inv:
        for site in net:
            valid = [ch for ch in site if ch.response and abs(ch.dip + 90) <= 5]
            if not valid:
                continue
            channel = min(
                valid, key=lambda ch: (ch.location_code not in ("00", ""), ch.location_code)
            )
            delta = locations2degrees(event["lat"], event["lon"], site.latitude, site.longitude)
            _, azimuth, _ = gps2dist_azimuth(
                event["lat"], event["lon"], site.latitude, site.longitude
            )
            sectors[int(azimuth // 30)].append(
                (abs(delta - 55), net.code, site.code, channel.location_code)
            )
    choices = [row for sites in sectors.values() for row in sorted(sites)[:5]]
    wave = output / "global.mseed"
    previous = read(str(wave)) if reuse_inventory and wave.is_file() else Stream()
    complete = {
        trace.id
        for trace in previous
        if trace.stats.starttime <= origin + TIMES[0] and trace.stats.endtime >= origin + TIMES[-1]
    }
    choices = [row for row in choices if ".".join((*row[1:], "LHZ")) not in complete]

    def fetch(row):
        _, net, site, loc = row
        try:
            return client.get_waveforms(net, site, loc, "LHZ", start, end)
        except (FDSNException, OSError):
            return Stream()

    with ThreadPoolExecutor(max_workers=8) as pool:
        stream = Stream(trace for part in pool.map(fetch, choices) for trace in part)
    if not stream:
        if previous:
            return
        raise NoGlobalWaveforms("No long-period waveforms returned")
    refreshed = {trace.id for trace in stream}
    stream.extend([trace for trace in previous if trace.id not in refreshed])
    temp = wave.with_suffix(".mseed.tmp")
    stream.write(str(temp), format="MSEED")
    temp.replace(wave)


def prepare(event, output, band):
    origin = UTCDateTime(event["origin"])
    stream = filter_stream(read(str(output / "global.mseed")))
    inventory = read_inventory(str(output / "global.xml"))
    taup = TauPyModel("ak135")
    stations, data = [], []
    for identifier in sorted({trace.id for trace in stream}):
        segment = stream.select(id=identifier).merge(method=0)
        if len(segment) != 1 or np.ma.is_masked(segment[0].data):
            continue
        tr = segment[0]
        if tr.stats.starttime > origin + TIMES[0] or tr.stats.endtime < origin + TIMES[-1]:
            continue
        coords = inventory.get_coordinates(identifier, origin)
        lat, lon = coords["latitude"], coords["longitude"]
        degrees = locations2degrees(event["lat"], event["lon"], lat, lon)
        if not DISTANCE[0] <= degrees <= DISTANCE[1]:
            continue
        p = taup.get_travel_times(event["depth_km"], degrees, phase_list=["P"])
        if not p:
            continue
        tr.detrend("linear").taper(0.05)
        tr.remove_response(
            inventory=inventory,
            output="DISP",
            water_level=None,
            pre_filt=(0.0005, 0.001, 0.012, 0.025),
            zero_mean=False,
            taper=False,
        )
        values = bandpass(tr.data, dt=tr.stats.delta, band=band)
        values = np.interp(TIMES, tr.times() + float(tr.stats.starttime - origin), values)
        lo, hi = np.searchsorted(TIMES, (p[0].time, p[0].time + 15 * degrees)).tolist()
        noise = float(np.sqrt(np.mean(values[(TIMES > -420) & (TIMES < -65)] ** 2)))
        signal = float(np.sqrt(np.mean(values[lo:hi] ** 2)))
        if not np.isfinite(values).all() or noise <= 0 or signal < 3 * noise:
            continue
        _, azimuth, _ = gps2dist_azimuth(event["lat"], event["lon"], lat, lon)
        stations.append(
            dict(
                id=identifier,
                lat=lat,
                lon=lon,
                azimuth=azimuth,
                window=[lo, hi],
                noise=noise,
                snr=signal / noise,
            )
        )
        data.append(np.stack((values, np.zeros_like(values), np.zeros_like(values))))
    return stations, np.asarray(data)


def greens(event, stations, store_path, band):
    from pyrocko import gf

    from tighnari_seisloc.acquisition.cmt_greens import MOMENTS, UNIT

    engine = gf.LocalEngine(store_dirs=[str(store_path)])
    store = engine.get_store()
    targets = [
        gf.Target(
            lat=site["lat"],
            lon=site["lon"],
            dip=-90,
            azimuth=0,
            quantity="displacement",
            codes=tuple(site["id"].split(".")),
            interpolation="multilinear",
            store_id=store.config.id,
        )
        for site in stations
    ]
    bank = np.zeros((len(DEPTHS), len(stations), 3, len(TIMES), len(MOMENTS)))
    for i, depth in enumerate(DEPTHS):
        sources = [
            gf.MTSource(
                lat=event["lat"],
                lon=event["lon"],
                depth=depth * 1000,
                **dict(zip(("mnn", "mee", "mdd", "mne", "mnd", "med"), np.asarray(moment) * UNIT)),
            )
            for moment in MOMENTS
        ]
        results = engine.process(sources, targets, nthreads=1)
        for column, rows in enumerate(results.results_list):
            for station, item in enumerate(rows):
                trace = item.trace.pyrocko_trace()
                bank[i, station, 0, :, column] = (
                    np.interp(
                        TIMES,
                        trace.get_xdata(),
                        trace.ydata,
                        left=0,
                    )
                    / UNIT
                )
    return store.config.id, bandpass(bank, dt=DT, band=band, axis=3)


def prefetch(event, output):
    output.mkdir(parents=True, exist_ok=True)
    inventory(event, Client(SERVER, timeout=26), output)


# Gate for using the teleseismic W-phase-window solution as the representative
# mechanism of a large event. Only one global Green's function store is available,
# so a fit in the independent LONG_BAND replaces the second velocity model. On the
# 2022-2026 Taiwan replay (research/global-cmt-taiwan-2022-2026.json) these limits
# kept 2024-04-03 Mw 7.4 (13 deg from USGS Mww) and 2025-12-27 Mw 6.6 (4 deg) and
# rejected every solution farther than 30 deg; 2022-09-18 (16 deg) failed the
# leave-one-station-out limit.
QUALIFY = dict(stations=20, holdout_vr=80, band_angle=30, band_stations=10)
SENSITIVITY_METHOD = "frequency_band"


def qualify(result, long_result=None):
    """Apply the large-event gate in place and return the result."""
    if not result.get("tensor_ned_nm"):
        return result
    reasons = [r for r in result.get("reasons", []) if r != "sensitivity_model_missing"]
    if result.get("station_count", 0) < QUALIFY["stations"]:
        reasons.append("too_few_long_period_stations")
    holdout = result.get("holdout_vr_pct")
    if (holdout is None or holdout < QUALIFY["holdout_vr"]) and "holdout_fit" not in reasons:
        reasons.append("holdout_fit")
    result["sensitivity_method"] = SENSITIVITY_METHOD
    usable = (
        long_result is not None
        and long_result.get("tensor_ned_nm")
        and long_result.get("station_count", 0) >= QUALIFY["band_stations"]
    )
    if usable:
        angle = kagan(np.asarray(result["tensor_ned_nm"]), np.asarray(long_result["tensor_ned_nm"]))
        result["band_kagan_deg"] = angle
        result["band_mw"] = long_result.get("mw")
        if angle is None or angle > QUALIFY["band_angle"]:
            reasons.append("band_sensitivity")
    else:
        # Not blocking: the leave-one-station-out spread still tests stability.
        result["band_kagan_deg"] = None
    result["reasons"] = reasons
    result["status"] = "unconstrained" if reasons else "experimental"
    return result


def analyze(event, store_path, output, *, band=BAND, centroid=False):
    sites, data = prepare(event, output, band)
    if len(sites) < 10:
        return dict(
            method=METHOD,
            status="insufficient_data",
            station_count=len(sites),
            band_hz=list(band),
            reasons=["too_few_long_period_stations"],
        )
    weights = np.zeros_like(data)
    for i, site in enumerate(sites):
        lo, hi = site["window"]
        weights[i, 0, lo:hi] = 1
    offsets = [(0, 0)]
    if centroid:
        offsets.extend(
            (east, north) for east in (-30, 0, 30) for north in (-30, 0, 30) if east or north
        )
    geod = Geod(ellps="WGS84")
    trials = []
    for east, north in offsets:
        lon, lat, _ = geod.fwd(
            event["lon"],
            event["lat"],
            np.degrees(np.arctan2(east, north)),
            1000 * np.hypot(east, north),
        )
        point = dict(lat=lat, lon=lon)
        geometry = [
            dict(site, azimuth=gps2dist_azimuth(lat, lon, site["lat"], site["lon"])[1])
            for site in sites
        ]
        name, bank = greens(point, sites, store_path, band)
        fitted, _ = invert(
            data,
            {name: bank},
            geometry,
            DEPTHS,
            DELAYS,
            DT,
            band=band,
            energy_cap=3,
            sample_weights=weights,
            research_details=True,
            skip_degenerate_depths=True,
        )
        trials.append(dict(fitted, offset_km=[east, north], centroid_location=point))
    result = horizontal_solution(trials, outer_edge_km=30) if centroid else trials[0]
    provenance = {}
    for file in (
        output / "global.mseed",
        output / "global.xml",
        *(store_path / key for key in ("config", "index", "traces")),
    ):
        with file.open("rb") as stream:
            provenance[file.name] = file_digest(stream, "sha256").hexdigest()
    result.update(
        method=METHOD,
        centroid_location_fixed=not centroid,
        location={k: event[k] for k in ("lat", "lon", "origin")},
        provenance=provenance,
    )
    return result

"""Prepare long-period displacement independently of short-period locator filters."""

from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path

import numpy as np
from obspy import Stream, UTCDateTime, read
from obspy.geodetics import gps2dist_azimuth
from obspy.signal.rotate import rotate_ne_rt

from ..processing.cmt import AFTER, BAND, BEFORE, DT, bandpass
from .fdsn import clipped_counts, remove_response
from .gdms import event_inventory
from ..processing.exclusions import filter_stream

CHANNELS = "BH?,HH?"
EARTHSCOPE = "https://service.earthscope.org"
DISTANCE_KM = (40, 650)
# Short-period CAP fits use stations from 8 km; long-period fits keep 40 km.
DOWNLOAD_DISTANCE_KM = (8, 650)
MAX_STATIONS = 12


def geometry(event, lat, lon):
    meters, azimuth, backward = gps2dist_azimuth(event["lat"], event["lon"], lat, lon)
    return meters / 1000, azimuth, backward


def _cmt_traces(event, stream, inventory, provider, *, band=BAND):
    origin = float(event["origin"])
    start, end = UTCDateTime(origin - BEFORE), UTCDateTime(origin + AFTER)
    relative_times = np.arange(-BEFORE, AFTER, DT)
    groups = defaultdict(list)
    for trace in stream:
        if trace.stats.channel[:2] in ("BH", "HH"):
            groups[trace.id[:-1]].append(trace)
    for sid, raw in sorted(groups.items()):
        part = Stream(raw).copy().trim(start, end, nearest_sample=False)
        part.merge(method=0, fill_value=None)
        if (
            len(part) != 3
            or part.get_gaps()
            or any(
                np.ma.is_masked(t.data)
                or not np.isfinite(t.data).all()
                or t.stats.starttime > start + t.stats.delta
                or t.stats.endtime < end - t.stats.delta
                or t.stats.sampling_rate < 1
                or clipped_counts(t.data, t.stats.sampling_rate)
                for t in part
            )
        ):
            continue
        try:
            for trace in part:
                inv = inventory.select(
                    network=trace.stats.network,
                    station=trace.stats.station,
                    location=trace.stats.location,
                    channel=trace.stats.channel,
                )
                channels = [
                    cha
                    for net in inv
                    for sta in net
                    for cha in sta
                    if cha.response
                    and (cha.start_date is None or cha.start_date <= trace.stats.starttime)
                    and (cha.end_date is None or cha.end_date >= trace.stats.endtime)
                ]
                if len(channels) != 1:
                    raise ValueError("Missing response epoch")
            coords = inventory.get_coordinates(part[0].id, UTCDateTime(origin))
            lat, lon = coords["latitude"], coords["longitude"]
            distance, azimuth, backward = geometry(event, lat, lon)
            if not DISTANCE_KM[0] <= distance <= DISTANCE_KM[1]:
                continue
            part.detrend("linear").taper(0.05)
            remove_response(
                part,
                inventory=inventory,
                output="DISP",
                water_level=None,
                pre_filt=(0.005, 0.01, max(0.08, 1.1 * band[1]), max(0.16, 1.5 * band[1])),
                zero_mean=False,
                taper=False,
            )
            part.rotate("->ZNE", inventory=inventory)
            z, n, e = (part.select(component=c)[0] for c in "ZNE")
            if any(not np.isfinite(t.data).all() for t in (z, n, e)):
                continue
        except (ValueError, IndexError, KeyError, LookupError):
            continue
        samples = []
        for trace in (z, n, e):
            times = float(trace.stats.starttime - UTCDateTime(origin)) + trace.times()
            filtered = bandpass(trace.data, dt=trace.stats.delta, band=band)
            samples.append(np.interp(relative_times, times, filtered))
        radial, transverse = rotate_ne_rt(samples[1], samples[2], backward)
        data = np.array([samples[0], radial, transverse])
        lo = round((BEFORE + max(0, distance / 8 - 10)) / DT)
        hi = round((BEFORE + distance / 2.2 + 70) / DT)
        noise = float(np.sqrt(np.mean(data[:, 30:120] ** 2)))
        snr = float(np.sqrt(np.mean(data[:, lo:hi] ** 2)) / max(noise, 1e-30))
        if noise <= 0 or snr < 3:
            continue
        yield (
            dict(
                id=".".join(sid.split(".")[:2]),
                source_id=sid,
                provider=provider,
                lat=lat,
                lon=lon,
                azimuth=azimuth,
                radial_azimuth=(backward + 180) % 360,
                distance_km=distance,
                noise=noise,
                snr=snr,
                window=[lo, hi],
            ),
            data,
        )


def _spread(rows, limit, event, location):
    """Build coverage across occupied sectors before taking a second site in a sector."""
    sectors = defaultdict(list)
    for item in rows:
        lat, lon, quality, stable_key = location(item)
        distance, azimuth, _ = geometry(event, lat, lon)
        sectors[int(azimuth // 45)].append((-quality, distance, stable_key, item))
    for sector in sectors.values():
        sector.sort(key=lambda row: row[:3])
    result = []
    while sectors and len(result) < limit:
        for key in sorted(sectors, key=lambda key: (sectors[key][0][:3], key)):
            result.append(sectors[key].pop(0)[-1])
            if not sectors[key]:
                del sectors[key]
            if len(result) == limit:
                break
    return result


def _download(event, label, base, directory):
    from obspy import read_inventory
    from obspy.clients.fdsn import Client

    origin = float(event["origin"])
    start, end = UTCDateTime(origin - BEFORE), UTCDateTime(origin + AFTER)
    xml = directory / f"{label}.xml"
    wave = directory / f"{label}.mseed"
    client = None
    if xml.exists():
        inventory = read_inventory(str(xml))
    else:
        client = Client(base, timeout=18)
        inventory = client.get_stations(
            network="TW" if label == "bats" else "*",
            channel=CHANNELS,
            level="response",
            starttime=start,
            endtime=end,
            latitude=event["lat"],
            longitude=event["lon"],
            minradius=0.05,
            maxradius=6,
            includerestricted=False,
        )
        temp = xml.with_suffix(".tmp")
        inventory.write(str(temp), format="STATIONXML")
        temp.replace(xml)
    existing = read(str(wave)) if wave.is_file() else Stream()
    covered = defaultdict(set)
    for trace in existing:
        if (
            trace.stats.starttime <= start + trace.stats.delta
            and trace.stats.endtime >= end - trace.stats.delta
        ):
            key = (
                trace.stats.network,
                trace.stats.station,
                trace.stats.location,
                trace.stats.channel[:2],
            )
            covered[key].add(trace.stats.channel)
    known = {key[:2] for key, channels in covered.items() if len(channels) >= 3}
    stations = [
        (net.code, s.code, s.latitude, s.longitude)
        for net in inventory
        for s in net
        if DOWNLOAD_DISTANCE_KM[0]
        <= geometry(event, s.latitude, s.longitude)[0]
        <= DOWNLOAD_DISTANCE_KM[1]
    ]

    def location(row):
        return row[2], row[3], 0, row[:2]

    rows = _spread(stations, 24, event, location)
    for index in range(0, len(rows), 6):
        bulk = [
            (n, s, "*", CHANNELS, start, end)
            for n, s, *_ in rows[index : index + 6]
            if (n, s) not in known
        ]
        if not bulk:
            continue
        from obspy.clients.fdsn.header import FDSNException

        try:
            client = client or Client(base, timeout=18)
            downloaded = client.get_waveforms_bulk(bulk)
            replacements = {trace.id for trace in downloaded}
            existing = (
                Stream([trace for trace in existing if trace.id not in replacements]) + downloaded
            )
        except FDSNException:
            continue
    if existing:
        tmp = wave.with_suffix(".tmp")
        existing.write(str(tmp), format="MSEED")
        if not wave.exists() or _sha(tmp) != _sha(wave):
            tmp.replace(wave)
        else:
            tmp.unlink()
    return [(label, wave, xml)] if existing else []


def _sha(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def gdms_inputs(inbox):
    inbox = Path(inbox).resolve()
    manifest = inbox / "manifest.json"
    if not manifest.is_file():
        return []
    files = json.loads(manifest.read_text(encoding="utf-8"))["files"]
    pairs = [(inbox / r["waveform"], inbox / r["inventory"]) for r in files]
    return [
        ("gdms", wave, xml)
        for wave, xml in pairs
        if all(p.resolve().is_relative_to(inbox) and p.is_file() for p in (wave, xml))
    ]


def prepare(event, inputs, directory=None, *, bats_url=None, station_limit=MAX_STATIONS, band=BAND):
    """Return stations, aligned displacement and source checksums for offline or live runs."""
    origin = float(event["origin"])
    pairs = list(inputs)
    errors = []
    if directory is not None:
        directory.mkdir(parents=True, exist_ok=True)
        endpoints = {"earthscope": EARTHSCOPE}
        if bats_url:
            endpoints["bats"] = bats_url
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [
                (name, pool.submit(_download, event, name, url, directory))
                for name, url in endpoints.items()
            ]
            for name, future in futures:
                try:
                    pairs.extend(future.result())
                except Exception as exc:
                    errors.append(dict(provider=name, error=type(exc).__name__))
    choices, provenance = [], []
    for provider, wave, xml in pairs:
        try:
            hashes = [_sha(p) for p in (wave, xml)]
            inventory = event_inventory(xml, origin - BEFORE, origin + AFTER)
            stream = filter_stream(
                read(
                    str(wave),
                    starttime=UTCDateTime(origin - BEFORE),
                    endtime=UTCDateTime(origin + AFTER),
                )
            )
            rows = list(_cmt_traces(event, stream, inventory, provider, band=band))
            if hashes != [_sha(p) for p in (wave, xml)]:
                raise OSError("CMT input changed")
        except (OSError, ValueError) as exc:
            errors.append(dict(provider=provider, error=type(exc).__name__))
            continue
        choices.extend(rows)
        provenance.append(
            dict(
                provider=provider,
                waveform_sha256=hashes[0],
                inventory_sha256=hashes[1],
                eligible_stations=len(rows),
            )
        )

    unique = []
    for site, data in sorted(
        choices,
        key=lambda row: (-row[0]["snr"], row[0]["id"], row[0]["source_id"], row[0]["provider"]),
    ):
        if any(
            site["id"] == s["id"]
            or gps2dist_azimuth(s["lat"], s["lon"], site["lat"], site["lon"])[0] < 2000
            for s, _ in unique
        ):
            continue
        unique.append((site, data))

    def location(row):
        s = row[0]
        return s["lat"], s["lon"], min(s["snr"], 30), s["id"]

    chosen = _spread(unique, station_limit, event, location)
    stations = [site for site, _ in chosen]
    data = np.asarray([samples for _, samples in chosen])
    return stations, data, dict(provenance=provenance, errors=errors, band_hz=list(band))

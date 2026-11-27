"""Component-level preparation of existing regional archives for CMT replay."""

from collections import Counter, defaultdict

import numpy as np
from obspy import Stream, UTCDateTime, read

from ..processing.cmt import AFTER, BEFORE, DT, bandpass
from .cmt import DISTANCE_KM, MAX_STATIONS, _sha, _spread, geometry
from .fdsn import clipped_counts, remove_response
from .gdms import event_inventory
from ..processing.exclusions import filter_stream


# Accelerometers integrated to long-period displacement carry more tilt and
# instrument noise, so they need a clearer signal than broadband sensors.
MIN_SNR = dict(broadband=3, strong_motion=5)


def instrument(channel):
    return "strong_motion" if channel[1:2] in ("N", "L", "G") else "broadband"


def _component(trace, inventory, origin, band, quantity, window):
    rate = trace.stats.sampling_rate
    if rate < 1 or clipped_counts(trace.data, rate):
        return None, "clipped_or_bad_samples"
    channel = inventory.select(
        network=trace.stats.network,
        station=trace.stats.station,
        location=trace.stats.location,
        channel=trace.stats.channel,
        starttime=trace.stats.starttime,
        endtime=trace.stats.endtime,
    )
    epochs = [
        c
        for n in channel
        for s in n
        for c in s
        if c.response
        and (c.start_date is None or c.start_date <= trace.stats.starttime)
        and (c.end_date is None or c.end_date >= trace.stats.endtime)
    ]
    if len(epochs) != 1 or epochs[0].dip is None or epochs[0].azimuth is None:
        return None, "response_or_orientation"
    epoch = epochs[0]
    available_start = float(trace.stats.starttime - origin)
    available_end = float(trace.stats.endtime - origin)
    pad = 0.8 / band[0]
    lo = max(window[0], int(np.ceil(available_start + pad + BEFORE)))
    hi = min(window[1], int(np.floor(available_end - pad + BEFORE)))
    if available_start > -56 or hi - lo < 45:
        return None, "short_or_gapped_window"
    trace.detrend("linear").taper(0.05)
    try:
        remove_response(
            trace,
            inventory=inventory,
            output=quantity,
            water_level=None,
            pre_filt=(0.005, 0.01, max(0.08, band[1] * 1.10), max(0.16, band[1] * 1.5)),
            zero_mean=False,
            taper=False,
        )
        filtered = bandpass(trace.data, dt=trace.stats.delta, band=band)
    except (ValueError, LookupError, TypeError):
        return None, "response_or_filter"
    times = np.arange(-BEFORE, AFTER, DT)
    aligned = np.interp(times, available_start + trace.times(), filtered, left=0, right=0)
    noise_start = max(available_start + 10, -45)
    noise_mask = (times >= noise_start) & (times < -12)
    if not np.isfinite(aligned).all() or noise_mask.sum() < 20:
        return None, "invalid_noise_window"
    noise = float(np.sqrt(np.mean(aligned[noise_mask] ** 2)))
    signal = float(np.sqrt(np.mean(aligned[lo:hi] ** 2)))
    if noise <= 0 or signal / noise < MIN_SNR[instrument(trace.stats.channel)]:
        return None, "low_snr"
    # Assign to the closest nominal geographic axis. The forward model uses the exact channel orientation.
    az, dip = np.radians([epoch.azimuth, epoch.dip])
    slot = int(np.argmax(np.abs([np.sin(dip), np.cos(dip) * np.cos(az), np.cos(dip) * np.sin(az)])))
    return (
        slot,
        float(epoch.azimuth),
        float(epoch.dip),
        lo,
        hi,
        noise,
        signal / noise,
        aligned,
    ), None


def prepare_components(
    event,
    inputs,
    band,
    quantity,
    *,
    station_limit=MAX_STATIONS,
    distance_km=DISTANCE_KM,
    channels=("BH", "HH"),
):
    """Return data, component masks and a channel-by-channel rejection record without downloads."""
    origin = UTCDateTime(event["origin"])
    issues, provenance, choices = [], [], []
    for provider, wave, xml in inputs:
        try:
            hashes = [_sha(p) for p in (wave, xml)]
            stream = read(str(wave), starttime=origin - BEFORE, endtime=origin + AFTER)
            stream = filter_stream(Stream(t for t in stream if t.stats.channel[:2] in channels))
            if not stream:
                continue
            inventory = event_inventory(xml, float(origin - BEFORE), float(origin + AFTER))
        except (OSError, ValueError, LookupError) as exc:
            issues.append(dict(provider=provider, trace=wave.name, reason=type(exc).__name__))
            continue
        groups = defaultdict(list)
        for trace in stream:
            groups[trace.id[:-1]].append(trace)
        archive_rows = []
        for sid, traces in sorted(groups.items()):
            try:
                coordinates = inventory.get_coordinates(traces[0].id, origin)
            # ObsPy raises a bare Exception when StationXML lacks the channel.
            except Exception:  # noqa: BLE001
                issues.append(dict(provider=provider, trace=sid, reason="station_coordinates"))
                continue
            lat, lon = coordinates["latitude"], coordinates["longitude"]
            distance, azimuth, _ = geometry(event, lat, lon)
            if not distance_km[0] <= distance <= distance_km[1]:
                issues.append(dict(provider=provider, trace=sid, reason="green_distance"))
                continue
            window = (
                round(BEFORE + max(0, distance / 8 - 10)),
                round(BEFORE + distance / 2.2 + 70),
            )
            parts = Stream(traces).copy()
            try:
                parts.merge(method=0, fill_value=None)
            except ValueError:
                issues.append(dict(provider=provider, trace=sid, reason="inconsistent_segments"))
                continue
            row = {}
            for trace in parts.split():
                found, reason = _component(trace, inventory, origin, band, quantity, window)
                if found:
                    previous = row.get(found[0])
                    if previous is None or (found[4] - found[3], found[6]) > (
                        previous[4] - previous[3],
                        previous[6],
                    ):
                        row[found[0]] = found
                else:
                    issues.append(dict(provider=provider, trace=trace.id, reason=reason))
            if not row:
                continue
            data = np.zeros((3, BEFORE + AFTER))
            mask = np.zeros_like(data)
            axes = [[axis, az, dip] for axis, az, dip in (("Z", 0, -90), ("N", 0, 0), ("E", 90, 0))]
            for slot, az, dip, lo, hi, noise, snr, aligned in row.values():
                axes[slot][1:] = [az, dip]
                data[slot] = aligned
                mask[slot, lo:hi] = 1
            site = dict(
                id=".".join(sid.split(".")[:2]),
                source_id=sid,
                provider=provider,
                instrument=instrument(sid.split(".")[-1]),
                lat=lat,
                lon=lon,
                azimuth=azimuth,
                distance_km=distance,
                window=window,
                target_axes=axes,
                components=[axes[i][0] for i in sorted(row)],
                component_noise=[row[i][5] if i in row else None for i in range(3)],
                noise=float(np.median([r[5] for r in row.values()])),
                snr=float(np.median([r[6] for r in row.values()])),
            )
            archive_rows.append((site, data, mask))
        try:
            if hashes != [_sha(p) for p in (wave, xml)]:
                raise OSError("Archived waveform or response changed")
        except OSError:
            issues.append(dict(provider=provider, trace=wave.name, reason="archive_changed"))
            continue
        choices.extend(archive_rows)
        provenance.append(
            dict(provider=provider, waveform_sha256=hashes[0], inventory_sha256=hashes[1])
        )
    unique = []
    for item in sorted(
        choices,
        # A broadband record wins over an accelerometer at the same site.
        key=lambda x: (
            x[0]["instrument"] != "broadband",
            -len(x[0]["components"]),
            -int(x[2].sum()),
            -x[0]["snr"],
            x[0]["id"],
        ),
    ):
        s = item[0]
        if any(
            s["id"] == old[0]["id"] or geometry(s, old[0]["lat"], old[0]["lon"])[0] < 2
            for old in unique
        ):
            issues.append(
                dict(provider=s["provider"], trace=s["source_id"], reason="duplicate_site")
            )
        else:
            unique.append(item)
    selected = _spread(
        unique,
        station_limit,
        event,
        lambda row: (row[0]["lat"], row[0]["lon"], min(row[0]["snr"], 30), row[0]["id"]),
    )
    selected_ids = {id(row[0]) for row in selected}
    issues.extend(
        dict(provider=s["provider"], trace=s["source_id"], reason="station_limit")
        for s, *_ in unique
        if id(s) not in selected_ids
    )
    return (
        [x[0] for x in selected],
        np.asarray([x[1] for x in selected]),
        np.asarray([x[2] for x in selected]),
        dict(
            sources=provenance,
            rejections=issues,
            reason_counts=dict(Counter(r["reason"] for r in issues)),
        ),
    )


def centroid_nodes(event, greens, stations):
    """Regenerate source geometry while retaining observed component axes and windows."""
    from pyproj import Geod

    from ..processing.cmt_centroid import DEPTHS, OFFSETS
    from ..processing.cmt_moment import VALIDATION_BAND

    geod = Geod(ellps="WGS84")
    for east, north in OFFSETS:
        lon, lat, _ = geod.fwd(
            event["lon"],
            event["lat"],
            np.degrees(np.arctan2(east, north)),
            1000 * np.hypot(east, north),
        )
        location = dict(lat=lat, lon=lon)
        site_geometry = [geometry(location, site["lat"], site["lon"]) for site in stations]
        if any(not DISTANCE_KM[0] <= distance <= DISTANCE_KM[1] for distance, *_ in site_geometry):
            continue
        # Keep the observed component axes and time windows fixed for every node.
        sites = [
            dict(site, distance_km=distance, azimuth=azimuth)
            for site, (distance, azimuth, _) in zip(stations, site_geometry, strict=True)
        ]
        banks = dict(greens.banks(location, sites, DEPTHS, band=VALIDATION_BAND))
        yield (east, north), location, sites, banks

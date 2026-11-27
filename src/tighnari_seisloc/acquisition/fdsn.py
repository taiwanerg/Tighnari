"""Fault-isolated regional FDSN archive acquisition with original response provenance."""

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import hashlib
import json
import logging
from pathlib import Path
from threading import Lock
import numpy as np
from ..models import Record
from .stations import INVENTORY_BOUNDS, select_stations

LOG = logging.getLogger(__name__)
PROVIDERS = {"earthscope": "https://service.earthscope.org", "geofon": "https://geofon.gfz.de"}
_RESPONSE_LOCK = Lock()
_RESPONSES_SEEN = set()


def remove_response(trace, **kwargs):
    """Serialize evalresp: concurrent calls can corrupt response-stage gains.

    Network requests stay concurrent. This also protects calls made by the
    independent teleseismic path in the same process.
    """
    with _RESPONSE_LOCK:
        # evalresp writes sensitivity notices directly to stderr. Let each
        # response be checked/reported on first use, rather than for every packet.
        inventory = kwargs.get("inventory")
        keys = []
        if inventory is not None:
            traces = trace if hasattr(trace, "traces") else [trace]
            for tr in traces:
                response = inventory.get_response(tr.id, tr.stats.starttime)
                sensitivity = response.instrument_sensitivity
                keys.append(
                    (
                        tr.id,
                        getattr(sensitivity, "value", None),
                        tuple(
                            (s.stage_sequence_number, s.stage_gain, s.stage_gain_frequency)
                            for s in response.response_stages
                        ),
                    )
                )
        if keys:
            kwargs.setdefault(
                "hide_sensitivity_mismatch_warning", all(k in _RESPONSES_SEEN for k in keys)
            )
        result = trace.remove_response(**kwargs)
        if len(_RESPONSES_SEEN) > 2048:
            _RESPONSES_SEEN.clear()
        _RESPONSES_SEEN.update(keys)
        return result


def checksum(path):
    with Path(path).open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def clipped_counts(values, rate):
    """Detect repeated extrema, including short ADC plateaus in long exports."""
    y = np.asarray(values, float)
    if not len(y) or not np.isfinite(y).all():
        return True
    peak = float(np.max(np.abs(y)))
    if peak == 0:
        return False
    if np.count_nonzero(np.abs(y) >= peak * 0.999999) > max(8, len(y) * 0.005):
        return True
    if peak < 1024:
        return False  # Do not treat low-resolution quantization as ADC saturation.
    edges = np.diff(np.r_[False, np.abs(y) >= peak - 0.5, False].astype(int))
    lengths = np.flatnonzero(edges == -1) - np.flatnonzero(edges == 1)
    return bool(len(lengths) and lengths.max() >= max(3, np.ceil(0.03 * rate)))


def save_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8"
    )
    tmp.replace(path)


def discover(start, end, directory, limit=60):
    from obspy.clients.fdsn import Client
    from obspy import UTCDateTime

    directory = Path(directory)
    south, north, west, east = INVENTORY_BOUNDS
    rows, errors = [], []
    for provider, base in PROVIDERS.items():
        cache = directory / f"{provider}-stations-taiwan-assisted-v1.json"
        try:
            if cache.exists():
                rows.extend(json.loads(cache.read_text()))
                continue
            inv = Client(base, timeout=35).get_stations(
                starttime=UTCDateTime(start),
                endtime=UTCDateTime(end),
                minlatitude=south,
                maxlatitude=north,
                minlongitude=west,
                maxlongitude=east,
                channel="BH?,HH?,HN?,EH?",
                level="channel",
                includerestricted=False,
            )
            found = []
            for net in inv:
                for st in net:
                    if not any(c.sample_rate >= 20 for c in st):
                        continue
                    found.append(
                        dict(
                            provider=provider,
                            base=base,
                            network=net.code,
                            station=st.code,
                            lat=st.latitude,
                            lon=st.longitude,
                        )
                    )
            save_json(cache, found)
            rows.extend(found)
        except Exception as exc:
            errors.append(dict(provider=provider, stage="discovery", error=type(exc).__name__))
    return select_stations(rows, limit), errors


def calibrated_records(stream, inventory, provider=""):
    """Produce velocity traces, splitting gaps instead of interpolating across them."""
    from obspy import Stream

    records, errors = [], []
    sites = sorted({(t.stats.network, t.stats.station) for t in stream})
    for net, sta in sites:
        selected = False
        for prefix in ("BH", "HH", "EH", "HN"):
            locations = sorted(
                {
                    t.stats.location
                    for t in stream.select(network=net, station=sta, channel=prefix + "?")
                }
            )
            for loc in locations:
                group = stream.select(
                    network=net, station=sta, location=loc, channel=prefix + "?"
                ).copy()
                group.merge(method=0, fill_value=None)
                # Separate continuous Z segments; each is independently usable.
                zs = group.select(component="Z").split()
                for zraw in zs:
                    if len(zraw) / zraw.stats.sampling_rate < 20:
                        continue
                    start, end = zraw.stats.starttime, zraw.stats.endtime
                    part = group.copy().trim(start, end, nearest_sample=True).split()
                    full = len(part) == 3 and not part.get_gaps()
                    rates = {t.stats.sampling_rate for t in part}
                    if len(rates) != 1 or not 20 <= min(rates) <= 500:
                        continue
                    rate = min(rates)
                    if full:
                        lo = max(t.stats.starttime for t in part)
                        hi = min(t.stats.endtime for t in part)
                        # get_gaps() checks within channels, not their common coverage.
                        full = hi - lo >= part[0].stats.delta
                    if full:
                        part.trim(lo, hi, nearest_sample=True)
                        if len({len(t) for t in part}) != 1:
                            continue
                    else:
                        part = Stream([zraw.copy()])
                    try:
                        coords = inventory.get_coordinates(zraw.id, zraw.stats.starttime)
                        clipped_channels = []
                        vertical_clipped = False
                        for raw_trace in part:
                            if clipped_counts(raw_trace.data, rate):
                                clipped_channels.append(raw_trace.id)
                                dip = inventory.get_orientation(
                                    raw_trace.id, raw_trace.stats.starttime
                                )["dip"]
                                vertical_clipped |= abs(np.sin(np.deg2rad(dip))) > 0.01
                        clipped = bool(clipped_channels)
                        part.detrend("linear").taper(0.03)
                        remove_response(
                            part,
                            inventory=inventory,
                            output="VEL",
                            pre_filt=(0.005, 0.01, min(12, rate * 0.35), rate * 0.45),
                            water_level=60,
                        )
                        if full:
                            components = "".join(t.stats.channel[-1] for t in part)
                            if len(set(components)) != 3:
                                raise ValueError("Three independent channels required")
                            part.rotate(method="->ZNE", inventory=inventory, components=components)
                        if not full:
                            orientation = inventory.get_orientation(zraw.id, zraw.stats.starttime)
                            dip = float(orientation["dip"])
                            if abs(abs(dip) - 90) > 0.1:
                                raise ValueError("Unverified vertical orientation")
                            if dip > 0:
                                part[0].data *= -1
                        axes = [part.select(component=c) for c in "ZNE"]
                        if len(axes[0]) != 1:
                            continue
                        n = len(axes[0][0])
                        arr = np.column_stack(
                            [a[0].data[:n] if len(a) == 1 else np.full(n, np.nan) for a in axes]
                        )
                        pad = int(2 * rate)
                        if n <= pad * 2:
                            continue
                        rid = f"{net}.{sta}.{loc}.{prefix}"
                        records.append(
                            Record(
                                rid,
                                coords["latitude"],
                                coords["longitude"],
                                float(axes[0][0].stats.starttime) + 2,
                                rate,
                                arr[pad:-pad],
                                coords["elevation"],
                                provider,
                                prefix,
                                clipped,
                                metadata=dict(
                                    vertical_orientation_verified=True,
                                    response_corrected=True,
                                    zne_rotated=full,
                                    clipped_components=clipped_channels,
                                    vertical_clipped=bool(vertical_clipped),
                                    raw_clip_version=2,
                                ),
                            )
                        )
                        selected = True
                    except Exception as exc:
                        errors.append(
                            dict(station=zraw.id, error=type(exc).__name__, stage="calibration")
                        )
                if selected:
                    break
            if selected:
                break
    from ..processing.exclusions import apply_records

    return apply_records(records), errors


def fetch_station(row, start, end, directory):
    from obspy import read, read_inventory, UTCDateTime
    from obspy.clients.fdsn import Client

    key = f"{row['provider']}-{row['network']}.{row['station']}"
    base = Path(directory) / key
    wave, xml = base.with_suffix(".mseed"), base.with_suffix(".xml")
    # Keep the complete station identifier: Path.with_suffix would discard .station.
    wave, xml = Path(str(base) + ".mseed"), Path(str(base) + ".xml")
    cached = wave.exists() and xml.exists()
    if not cached:
        client = Client(row["base"], timeout=30)
        inv = client.get_stations(
            network=row["network"],
            station=row["station"],
            channel="BH?,HH?,HN?,EH?",
            starttime=UTCDateTime(start),
            endtime=UTCDateTime(end),
            level="response",
            includerestricted=False,
        )
        stream = client.get_waveforms(
            row["network"],
            row["station"],
            "*",
            "BH?,HH?,HN?,EH?",
            UTCDateTime(start),
            UTCDateTime(end),
        )
        wave.parent.mkdir(parents=True, exist_ok=True)
        stream.write(str(wave) + ".tmp", format="MSEED")
        inv.write(str(xml) + ".tmp", format="STATIONXML")
        Path(str(wave) + ".tmp").replace(wave)
        Path(str(xml) + ".tmp").replace(xml)
    stream, inv = read(wave), read_inventory(xml)
    records, errors = calibrated_records(stream, inv, row["provider"])
    status = dict(
        **row,
        cached=cached,
        raw=str(wave),
        inventory=str(xml),
        raw_sha256=checksum(wave),
        inventory_sha256=checksum(xml),
        records=len(records),
        errors=errors,
    )
    return records, status


def acquire(start, end, directory, limit=60, workers=4, retry_failed=False):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    rows, errors = discover(start, end, directory, limit)
    manifest = dict(
        start=start,
        end=end,
        stations=[],
        errors=errors,
        acquired_utc=datetime.now(timezone.utc).isoformat(),
    )
    prior = directory / "manifest.json"
    if prior.exists() and not retry_failed:
        previous = json.loads(prior.read_text())
        failed = {
            (r.get("provider"), r.get("network"), r.get("station"))
            for r in previous.get("errors", [])
            if r.get("stage") == "download"
        }
        manifest["errors"].extend(
            r for r in previous.get("errors", []) if r.get("stage") == "download"
        )
        rows = [r for r in rows if (r["provider"], r["network"], r["station"]) not in failed]
    records = []
    with ThreadPoolExecutor(max_workers=min(workers, 4)) as pool:
        jobs = {pool.submit(fetch_station, r, start, end, directory / "raw"): r for r in rows}
        for future in as_completed(jobs):
            row = jobs[future]
            try:
                rec, status = future.result()
                records.extend(rec)
                manifest["stations"].append(status)
                LOG.info("%s.%s: %d calibrated segments", row["network"], row["station"], len(rec))
            except Exception as exc:
                manifest["errors"].append(dict(**row, stage="download", error=type(exc).__name__))
                LOG.info("%s.%s: %s", row["network"], row["station"], type(exc).__name__)
    # A provider alternative never increases the independent site count.
    manifest["station_count"] = len({r.id for r in records})
    save_json(directory / "manifest.json", manifest)
    return sorted(records, key=lambda r: (r.start, r.id)), manifest

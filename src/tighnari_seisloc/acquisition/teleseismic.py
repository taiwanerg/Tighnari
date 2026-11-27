"""Separate archived long-period data path for moment-magnitude proxy estimation."""

from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
import json
import logging
import numpy as np
from ..legacy.location import distance
from ..processing.magnitude import measure_mwp, aggregate_mwp
from .fdsn import save_json, checksum, remove_response, clipped_counts

LOG = logging.getLogger(__name__)


def estimate(event, directory, limit=20, *, max_stations=60):
    from obspy.clients.fdsn import Client
    from obspy.taup import TauPyModel
    from obspy import UTCDateTime, read, read_inventory
    from obspy.geodetics import gps2dist_azimuth

    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    limit = min(60, max(5, limit))
    max_stations = min(60, max(limit, max_stations))
    cache = directory / "stations.json"
    base = "https://service.earthscope.org"
    model = TauPyModel("iasp91")
    if cache.exists():
        rows = json.loads(cache.read_text())
    else:
        inv = Client(base, timeout=35).get_stations(
            network="IU,II,GE,G",
            channel="BHZ",
            starttime=UTCDateTime(event["origin"]),
            endtime=UTCDateTime(event["origin"] + 1800),
            latitude=event["lat"],
            longitude=event["lon"],
            minradius=30,
            maxradius=90,
            level="station",
            includerestricted=False,
        )
        rows = [
            dict(network=net.code, station=st.code, lat=st.latitude, lon=st.longitude)
            for net in inv
            for st in net
        ]
        # True forward azimuth handles the dateline and high latitudes.
        for r in rows:
            r["az"] = float(gps2dist_azimuth(event["lat"], event["lon"], r["lat"], r["lon"])[1])
        rows.sort(key=lambda r: r["az"])
        if len(rows) > max_stations:
            rows = [rows[i] for i in np.linspace(0, len(rows) - 1, max_stations, dtype=int)]
        save_json(cache, rows)

    def fetch(row):
        delta = float(distance(event["lat"], event["lon"], row["lat"], row["lon"]) / 111.195)
        arrivals = model.get_travel_times(event["depth_km"], delta, phase_list=["P", "S"])
        pt = min(a.time for a in arrivals if a.name == "P") + event["origin"]
        st = min(a.time for a in arrivals if a.name == "S") + event["origin"]
        stem = f"{row['network']}.{row['station']}"
        wave, xml = directory / (stem + ".mseed"), directory / (stem + ".xml")
        if not (wave.exists() and xml.exists()):
            client = Client("https://geofon.gfz.de" if row["network"] == "GE" else base, timeout=30)
            inv = client.get_stations(
                network=row["network"],
                station=row["station"],
                channel="BHZ",
                starttime=UTCDateTime(pt - 390),
                endtime=UTCDateTime(pt + 200),
                level="response",
            )
            stream = client.get_waveforms(
                row["network"],
                row["station"],
                "*",
                "BHZ",
                UTCDateTime(pt - 390),
                UTCDateTime(pt + 200),
            )
            inv.write(xml, format="STATIONXML")
            stream.write(wave, format="MSEED")
        inv = read_inventory(xml)
        stream = read(wave)
        stream.merge(method=0, fill_value=None)
        segments = [
            tr
            for tr in stream
            if len(tr) / tr.stats.sampling_rate >= 530
            and not (np.ma.isMaskedArray(tr.data) and np.ma.getmaskarray(tr.data).any())
        ]
        if not segments:
            return dict(**row, accepted=False, reason="incomplete-continuous-Z")
        tr = segments[0].copy()
        # Keep the phase detector broadband; Mwp uses its own long-period path.
        detector = segments[0].copy().detrend("linear").taper(0.02)
        rate = detector.stats.sampling_rate
        remove_response(
            detector,
            inventory=inv,
            output="VEL",
            pre_filt=(0.02, 0.05, min(10, rate * 0.35), rate * 0.45),
            water_level=60,
        )
        if clipped_counts(tr.data, tr.stats.sampling_rate):
            return dict(**row, accepted=False, reason="clipped")
        tr.detrend("linear").taper(0.02)
        remove_response(
            tr, inventory=inv, output="VEL", pre_filt=(0.0005, 0.001, 1, 2), water_level=60
        )
        # Actual broadband P pick; theory supplies a search window, not an observation.
        from ..models import Record
        from ..processing.picking import pick

        data = np.column_stack([detector.data, np.full(len(tr), np.nan), np.full(len(tr), np.nan)])
        r = Record(
            tr.id[:-1],
            row["lat"],
            row["lon"],
            float(tr.stats.starttime),
            tr.stats.sampling_rate,
            data,
        )
        picks, method = pick(r, "phasenet", "geofon")
        candidates = [
            p
            for p in picks
            if p["phase"] == "P" and abs(p["time"] - pt) <= 15 and p["score"] >= 0.25
        ]
        if not candidates:
            return dict(**row, accepted=False, reason="no-P-pick-near-theory")
        observed = max(candidates, key=lambda p: p["score"])["time"]
        result = measure_mwp(
            tr.data,
            tr.stats.sampling_rate,
            float(tr.stats.starttime),
            observed,
            st,
            delta,
            quality_velocity=detector.data,
        )
        return dict(
            **row,
            **result,
            id=tr.id,
            raw_sha256=checksum(wave),
            inventory_sha256=checksum(xml),
            p_time=observed,
            picker=method,
        )

    indices = np.linspace(0, len(rows) - 1, min(limit, len(rows)), dtype=int) if rows else []
    first = [rows[i] for i in indices]
    identities = {(r["network"], r["station"]) for r in first}
    rest = [r for r in rows if (r["network"], r["station"]) not in identities]
    results = []
    for batch in (first, rest):
        if not batch:
            continue
        with ThreadPoolExecutor(max_workers=3) as pool:
            jobs = {pool.submit(fetch, r): r for r in batch}
            for future in as_completed(jobs):
                try:
                    result = future.result()
                except Exception as exc:
                    result = dict(**jobs[future], accepted=False, reason=type(exc).__name__)
                results.append(result)
                LOG.info(
                    "Mwp %s.%s: %s",
                    result["network"],
                    result["station"],
                    result.get("magnitude", result.get("reason")),
                )
        if aggregate_mwp(results)["value"] is not None:
            break
    result = aggregate_mwp(
        results,
        processing_version="response-serialized-broadband-snr-v2",
        source_hypocenter={k: event[k] for k in ("origin", "lat", "lon", "depth_km")},
        method="Tsuboi-physical-SI/radiation0.52/HP0.008Hz/no-empirical-correction",
        attempted_stations=len(results),
        candidate_stations=len(rows),
        picker="PhaseNet/geofon/2",
        note="Research integrated-P moment proxy; not USGS Mw or operational PTWC reproduction",
    )
    save_json(directory / "result.json", result)
    return result

"""Delayed public BATS waveforms, calibrated through the existing physical path."""

from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from .fdsn import fetch_station, save_json

CHANNELS = "BH?,HH?,EH?,HN?"


def acquire_bats(start, end, directory, base_url, limit=60):
    from obspy import UTCDateTime
    from obspy.clients.fdsn import Client

    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    client = Client(base_url, timeout=30)
    inventory = client.get_stations(
        network="TW",
        channel=CHANNELS,
        starttime=UTCDateTime(start),
        endtime=UTCDateTime(end),
        level="channel",
        includerestricted=False,
    )
    rows = []
    for network in inventory:
        for station in network:
            if any(c.sample_rate >= 20 for c in station):
                rows.append(
                    dict(
                        provider="bats",
                        base=base_url,
                        network=network.code,
                        station=station.code,
                        lat=station.latitude,
                        lon=station.longitude,
                    )
                )
    rows = sorted(rows, key=lambda r: (r["network"], r["station"]))[:limit]
    manifest = dict(source="BATS / IESDMC", start=start, end=end, stations=[], errors=[])
    records = []
    with ThreadPoolExecutor(max_workers=4) as pool:
        jobs = {pool.submit(fetch_station, row, start, end, directory / "raw"): row for row in rows}
        for future in as_completed(jobs):
            row = jobs[future]
            try:
                found, status = future.result()
                records.extend(found)
                manifest["stations"].append(status)
            except Exception as exc:
                # Failed stations remain retryable; successful raw files are reused on restart.
                manifest["errors"].append(dict(station=row["station"], error=type(exc).__name__))
    manifest["station_count"] = len({r.id for r in records})
    save_json(directory / "manifest.json", manifest)
    return sorted(records, key=lambda r: (r.id, r.start)), manifest

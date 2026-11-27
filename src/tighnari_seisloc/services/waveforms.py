"""Shared waveform envelopes preserve peaks and explicit time gaps."""

import json
import hashlib
import asyncio
from itertools import chain
import time
from collections import OrderedDict
from pathlib import Path
import numpy as np
from ..models import Record


def merge_gdms_measurements(archive, downloaded=()):
    """Replace an archived GDMS segment with the latest calibrated copy on retry."""
    segments = {}
    original = []
    for record in chain(archive, downloaded):
        if record.provider == "gdms":
            key = (record.id, record.start, record.rate, len(record.velocity))
            segments[key] = record
        else:
            original.append(record)
    return [*original, *segments.values()]


class LiveWaveforms:
    """Share a bounded set of envelopes across viewers, including concurrent misses."""

    def __init__(self, snapshot, *, maxsize=64):
        self.snapshot = snapshot
        self.maxsize = maxsize
        self.lock = asyncio.Lock()
        self.updated = float("-inf")
        self.records, self.picks, self.versions = [], [], {}
        self.frames = OrderedDict()

    async def get(self, ids=None, seconds=60, bins=900, *, default_limit=8):
        async with self.lock:
            if time.monotonic() - self.updated >= 1:
                self.records, self.picks = self.snapshot()
                self.versions = await asyncio.to_thread(
                    waveform_versions,
                    self.records,
                    self.picks,
                    {r.id for r in self.records},
                )
                self.updated = time.monotonic()
            ids = tuple(
                sorted(set(ids if ids is not None else sorted(self.versions)[:default_limit]))
            )
            versions = {sid: self.versions[sid] for sid in ids if sid in self.versions}
            key = (ids, seconds, bins, tuple(versions.items()))
            if key not in self.frames:
                self.frames[key] = await asyncio.to_thread(
                    envelope, self.records, ids, seconds, None, bins, self.picks
                )
                if len(self.frames) > self.maxsize:
                    self.frames.popitem(last=False)
            self.frames.move_to_end(key)
            # Callers add socket-specific delta metadata; never mutate the cached frame.
            return dict(self.frames[key]), versions


def waveform_versions(records, picks, ids):
    """A late station/backfilled gap can change even if global time does not."""
    wanted = set(ids)
    versions = {}
    for record in records:
        if record.id not in wanted:
            continue
        version = record.metadata.get("wave_version")
        if version is None:
            version = hashlib.blake2b(record.velocity.tobytes(), digest_size=16).hexdigest()
        versions.setdefault(record.id, []).append((record.start, record.end, record.rate, version))
    return {
        sid: (
            tuple(sorted(rows)),
            tuple(
                (p["phase"], p["time"], p.get("method"), p.get("adopted", False))
                for p in picks
                if p["id"] == sid
            ),
        )
        for sid, rows in versions.items()
    }


def save_records(records, path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    metadata = []
    arrays = {}
    for i, r in enumerate(records):
        metadata.append(
            dict(
                id=r.id,
                lat=r.lat,
                lon=r.lon,
                start=r.start,
                rate=r.rate,
                elevation_m=r.elevation_m,
                provider=r.provider,
                family=r.family,
                clipped=r.clipped,
                metadata=r.metadata,
            )
        )
        arrays[str(i)] = r.velocity
    arrays["metadata"] = np.array(json.dumps(metadata))
    np.savez_compressed(path, **arrays)
    return path


def load_records(path):
    with np.load(path, allow_pickle=False) as data:
        metadata = json.loads(str(data["metadata"]))
        return [Record(**row, velocity=data[str(i)]) for i, row in enumerate(metadata)]


def envelope(records, ids=None, seconds=60, end=None, bins=900, picks=()):
    records = [r for r in records if ids is None or r.id in ids]
    end = end if end is not None else max((r.end for r in records), default=0)
    start = end - seconds
    rows = {}
    width = seconds / max(1, bins)
    for r in records:
        if r.end < start or r.start > end:
            continue
        row = rows.setdefault(r.id, dict(**r.station(), segments=[], picks=[]))
        row["end"] = max(row["end"], r.end)
        lo = max(0, int((start - r.start) * r.rate))
        hi = min(len(r.velocity), int((end - r.start) * r.rate))
        if hi <= lo:
            continue
        z = r.velocity[lo:hi, 0] * 1e6
        ts = r.start + np.arange(lo, hi) / r.rate
        bucket = np.floor((ts - start) / width).astype(int)
        split = np.r_[0, np.where(np.diff(bucket) != 0)[0] + 1, len(z)]
        # Reduce all bins in NumPy instead of thousands of Python loops per viewer.
        starts = split[:-1]
        valid = np.logical_and.reduceat(np.isfinite(z), starts)
        samples = np.column_stack(
            (ts[starts], np.minimum.reduceat(z, starts), np.maximum.reduceat(z, starts))
        )[valid].tolist()
        row["segments"].append(samples)
        row["picks"] = [
            dict(
                time=p["time"],
                phase=p["phase"],
                method=p.get("method", ""),
                score=p.get("score"),
                adopted=p.get("adopted", False),
            )
            for p in picks
            if p["id"] == r.id and start <= p["time"] <= end
        ]
    return dict(start=start, end=end, bin_s=width, unit="µm/s", rows=list(rows.values()))

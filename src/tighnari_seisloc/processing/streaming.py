"""Incremental live processing; archive replay keeps its full-window path."""

from collections import OrderedDict
import hashlib
import numpy as np
from ..models import Record
from ..acquisition.fdsn import calibrated_records
from .picking import pick


def stream_signature(stream, inventory):
    digest = hashlib.blake2b(digest_size=16)
    digest.update(str(id(inventory)).encode())
    for trace in sorted(stream, key=lambda t: (t.id, float(t.stats.starttime))):
        digest.update(
            repr((trace.id, float(trace.stats.starttime), trace.stats.sampling_rate)).encode()
        )
        digest.update(np.ma.filled(trace.data, 0).tobytes())
        if np.ma.isMaskedArray(trace.data):
            digest.update(np.ma.getmaskarray(trace.data).tobytes())
    return digest.digest()


class LiveCalibration:
    """Reuse unchanged stations while other stations deliver new packets."""

    def __init__(self):
        self.cache = {}

    def update(self, sources):
        records, errors, active = [], [], set()
        for provider, stream, inventory in sources:
            if not stream or inventory is None:
                continue
            for net, sta in sorted({(t.stats.network, t.stats.station) for t in stream}):
                key = (provider, net, sta)
                active.add(key)
                group = stream.select(network=net, station=sta)
                signature = stream_signature(group, inventory)
                cached = self.cache.get(key)
                if cached is None or cached[0] != signature:
                    rows, bad = calibrated_records(group, inventory, provider)
                    for record in rows:
                        record.metadata["wave_version"] = signature.hex()
                    cached = (signature, rows, bad)
                    self.cache[key] = cached
                records.extend(cached[1])
                errors.extend(cached[2])
        self.cache = {k: v for k, v in self.cache.items() if k in active}
        return sorted(records, key=lambda r: (r.start, r.id)), errors


class LivePicker:
    def __init__(self, context_s=90, retention_s=600):
        self.context_s, self.retention_s = context_s, retention_s
        self.seen = OrderedDict()
        self.ends = {}
        self.picks = []
        self.methods = {}

    def status(self, backend="auto"):
        from collections import Counter

        methods = Counter(self.methods.values())
        return dict(
            requested=backend,
            neural_stations=sum(
                n
                for method, n in methods.items()
                if method.startswith("PhaseNet/") and "warmup" not in method
            ),
            trigger_stations=sum(
                n for method, n in methods.items() if method.startswith("adaptive")
            ),
            methods=dict(methods),
        )

    def update(self, records, backend="auto"):
        if not records:
            return [], {}
        watermark = max(r.end for r in records)
        self.picks = [p for p in self.picks if p["time"] >= watermark - self.retention_s]
        active = {r.id for r in records}
        self.ends = {k: v for k, v in self.ends.items() if k in active}
        self.methods = {k: v for k, v in self.methods.items() if k in active}
        self.picks = [p for p in self.picks if p["id"] in active]
        for record in records:
            digest = hashlib.blake2b(record.velocity.tobytes(), digest_size=16).digest()
            key = (record.id, record.start, record.rate, backend, digest)
            if key in self.seen:
                self.seen.move_to_end(key)
                continue
            previous_end = self.ends.get(record.id)
            start = record.start
            # A newly extended continuous segment only needs overlapping context.
            # A backfilled/previously gapped segment is evaluated in full instead.
            if previous_end is not None and record.end > previous_end:
                start = max(start, min(record.end - self.context_s, previous_end - 30))
            lo = max(0, int((start - record.start) * record.rate))
            window = Record(
                record.id,
                record.lat,
                record.lon,
                record.start + lo / record.rate,
                record.rate,
                record.velocity[lo:],
                record.elevation_m,
                record.provider,
                record.family,
                record.clipped,
            )
            found, method = pick(window, backend)
            # Replace only the reliable interior, retaining earlier P/S evidence.
            self.picks = [
                p
                for p in self.picks
                if not (p["id"] == record.id and window.start + 8 <= p["time"] <= window.end - 2)
            ]
            self.picks.extend(found)
            self.methods[record.id] = method
            self.ends[record.id] = max(record.end, previous_end or record.end)
            self.seen[key] = True
            while len(self.seen) > 2048:
                self.seen.popitem(last=False)
        return sorted(self.picks, key=lambda p: (p["time"], p["id"], p["phase"])), dict(
            self.methods
        )

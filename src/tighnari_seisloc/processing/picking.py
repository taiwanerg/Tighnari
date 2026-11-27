"""CPU neural phase picking and explicitly labelled adaptive conventional fallback."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
import logging
import threading
import numpy as np
from scipy.signal import butter, sosfiltfilt, find_peaks
from ..logging_utils import RepeatedLog

LOG = logging.getLogger(__name__)
_MODELS = {}
_MODEL_LOCK = threading.Lock()
MODEL_VERSIONS = {"original": "2", "geofon": "2"}
_REPEATED = RepeatedLog(LOG)


class _PhaseNetWorker:
    """Reuse the loop and the native inference thread across SeisBench calls."""

    def __init__(self):
        self.lock = threading.Lock()
        self.executor = None
        self.runner = None

    def classify(self, model, stream, **options):
        with self.lock:
            if self.executor is None:
                self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="phasenet")
            return self.executor.submit(self._classify, model, stream, options).result()

    def _classify(self, model, stream, options):
        if self.runner is None:
            self.runner = asyncio.Runner()
            self.runner.get_loop().set_default_executor(
                ThreadPoolExecutor(max_workers=1, thread_name_prefix="phasenet-inference")
            )
        return self.runner.run(model.classify_async(stream, **options))

    def close(self):
        with self.lock:
            if self.executor is None:
                return
            try:
                if self.runner is not None:
                    self.executor.submit(self.runner.close).result()
            finally:
                self.executor.shutdown()
                self.executor = self.runner = None


_PHASENET = _PhaseNetWorker()


def close_phase_picker():
    _PHASENET.close()


def model_for(weights="original"):
    with _MODEL_LOCK:
        return _load_model(weights)


def _load_model(weights):
    if weights not in _MODELS:
        from pathlib import Path
        import os
        import json

        # Resolve the same cache before SeisBench initializes its module globals.
        root = Path(os.environ.setdefault("SEISBENCH_CACHE_ROOT", "runtime/models")).resolve()
        cache = root / "models/v3/phasenet"
        version = MODEL_VERSIONS[weights]
        if not all(
            (cache / f"{weights}.{extension}.v{version}").exists() for extension in ("pt", "json")
        ):
            raise FileNotFoundError(
                "Run tighnari-seisloc setup-models first; no implicit network download"
            )
        metadata = json.loads((cache / f"{weights}.json.v{version}").read_text())
        if weights == "geofon" and metadata.get("model_args", {}).get("norm") != "peak":
            raise ValueError("Pinned geofon v2 requires corrected peak normalization")
        import torch
        import seisbench
        import seisbench.models as sbm

        if Path(seisbench.cache_root).resolve() != root:
            raise RuntimeError("SeisBench cache initialized before configured model root")
        torch.set_num_threads(2)
        model = sbm.PhaseNet.from_pretrained(weights, version_str=version)
        model.eval()
        _MODELS[weights] = model
    return _MODELS[weights]


def conventional(record):
    z = np.nan_to_num(record.velocity[:, 0])
    picks = []
    rate = record.rate
    for low, high in ((0.5, 3), (1, 8), (3, min(15, rate * 0.4))):
        if low >= high:
            continue
        y = sosfiltfilt(butter(3, [low, high], fs=rate, btype="bandpass", output="sos"), z)
        energy = y * y
        from scipy.ndimage import uniform_filter1d

        sta = uniform_filter1d(energy, max(1, int(0.3 * rate)))
        lta = uniform_filter1d(energy, max(1, int(15 * rate)))
        noise = max(np.quantile(lta, 0.25), np.finfo(float).eps ** 2)
        score = sta / np.maximum(lta, noise)
        peaks, _ = find_peaks(score, height=3, prominence=1, distance=int(3 * rate))
        for idx in peaks:
            if idx < 10 * rate or idx > len(z) - 2 * rate:
                continue
            if any(abs(record.start + idx / rate - p["time"]) < 2 for p in picks):
                continue
            picks.append(
                dict(
                    id=record.id,
                    lat=record.lat,
                    lon=record.lon,
                    elevation_m=record.elevation_m,
                    time=record.start + idx / rate,
                    phase="P",
                    score=min(0.7, float(score[idx]) / 12),
                    sigma_s=0.5,
                    method="adaptive-multiband-P-candidate",
                    snr=float(np.sqrt(score[idx])),
                )
            )
    return sorted(picks, key=lambda p: p["time"])


def pick(record, backend="auto", weights="original"):
    if backend == "traditional":
        return conventional(record), "adaptive-multiband"
    if len(record.velocity) < 31 * record.rate:
        return [], "PhaseNet/warmup-under-31s"
    try:
        from obspy import Stream, Trace, UTCDateTime

        stream = Stream()
        for j, c in enumerate("ZNE"):
            values = record.velocity[:, j]
            if not np.isfinite(values).all():
                continue
            tr = Trace(
                values.astype(np.float32),
                header=dict(
                    network=record.id.split(".")[0],
                    station=record.id.split(".")[1],
                    location=record.id.split(".")[2],
                    channel=record.family + c,
                    sampling_rate=record.rate,
                    starttime=UTCDateTime(record.start),
                ),
            )
            stream += tr
        model = model_for(weights)
        result = _PHASENET.classify(
            model,
            stream,
            P_threshold=0.12,
            S_threshold=0.12,
            batch_size=32,
            strict=False,
        )
        picks = []
        for row in result.picks:
            stamp = float(row.peak_time)
            i = int((stamp - record.start) * record.rate)
            y = np.nan_to_num(record.velocity[:, 0])
            before = y[max(0, i - int(8 * record.rate)) : max(1, i - int(record.rate))]
            after = y[max(0, i) : min(len(y), i + int(2 * record.rate))]
            noise = max(float(np.sqrt(np.mean(before**2))) if len(before) else 0, 1e-12)
            snr = float(np.sqrt(np.mean(after**2)) / noise) if len(after) else 0
            if stamp < record.start + 8 or stamp > record.end - 2:
                continue
            picks.append(
                dict(
                    id=record.id,
                    lat=record.lat,
                    lon=record.lon,
                    elevation_m=record.elevation_m,
                    time=stamp,
                    phase=row.phase,
                    score=float(row.peak_value),
                    sigma_s=max(
                        0.08 if row.phase == "P" else 0.15, float(row.end_time - row.start_time) / 2
                    ),
                    method=f"PhaseNet/{weights}/{model.weights_version}",
                    snr=snr,
                )
            )
        return picks, f"PhaseNet/{weights}/{model.weights_version}"
    except Exception as exc:
        if backend == "phasenet":
            raise
        _REPEATED.warning(
            type(exc).__name__,
            "PhaseNet unavailable (%s); explicit adaptive fallback",
            type(exc).__name__,
        )
        return conventional(record), "adaptive-fallback:" + type(exc).__name__

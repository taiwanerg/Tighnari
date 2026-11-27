"""Precompute Taiwan QSEIS stores and sample them on the inversion UTC grid."""

import contextlib
import hashlib
import io
import json
from pathlib import Path

import numpy as np

from ..processing.cmt import AFTER, BAND, BEFORE, DEPTHS, DT, bandpass

MODELS = ("taiwan_wu2007", "taiwan_wu2007_slow")
# Short-period stores for M3.5-5 waveform fitting near the source: denser
# depths and distances from 4 km, sampled at 1 Hz for passbands up to 0.25 Hz.
SHORT_MODELS = ("taiwan_wu2007_short", "taiwan_wu2007_slow_short")
GRIDS = {
    MODELS: dict(
        source_depth_min=2000.0,
        source_depth_max=100000.0,
        source_depth_delta=7000.0,
        distance_min=40000.0,
        distance_max=650000.0,
        distance_delta=10000.0,
        sample_rate=0.5,
        window=("-50", "+570"),
    ),
    SHORT_MODELS: dict(
        source_depth_min=1000.0,
        source_depth_max=101000.0,
        source_depth_delta=2000.0,
        distance_min=4000.0,
        distance_max=304000.0,
        distance_delta=2000.0,
        sample_rate=1.0,
        window=("-20", "+260"),
    ),
}
UNIT = 1e15
MOMENTS = [
    (1, 0, -1, 0, 0, 0),
    (0, 1, -1, 0, 0, 0),
    (0, 0, 0, 1, 0, 0),
    (0, 0, 0, 0, 1, 0),
    (0, 0, 0, 0, 0, 1),
]


def earth_model(velocity_scale):
    from obspy.taup import TauPyModel
    from pyrocko import cake

    model_path = Path(__file__).resolve().parents[1] / "assets/taiwan_wu2007.npz"
    layers = TauPyModel(str(model_path)).model.s_mod.v_mod.layers
    background = cake.load_model().extract(depth_max="cmb")
    z, _, _, rho, qp, qs = np.asarray(background.to_scanlines())[:, :6].T
    scanlines = []
    for layer in layers:
        if layer["top_depth"] >= 700:
            break
        for key in ("top", "bot"):
            depth = float(layer[key + "_depth"]) * 1000
            scale = 1 + (velocity_scale - 1) * max(0, 1 - depth / 70000)
            point = (
                depth,
                cake.Material(
                    vp=float(layer[key + "_p_velocity"]) * 1000 * scale,
                    vs=float(layer[key + "_s_velocity"]) * 1000 * scale,
                    rho=float(np.interp(depth, z, rho)),
                    qp=float(np.interp(depth, z, qp)),
                    qs=float(np.interp(depth, z, qs)),
                ),
                None,
            )
            if not scanlines or scanlines[-1][:2] != point[:2]:
                scanlines.append(point)
    last_local_depth = scanlines[-1][0]
    for depth, vp, vs, density, p, s in background.to_scanlines():
        if depth > last_local_depth:
            scanlines.append((depth, cake.Material(vp=vp, vs=vs, rho=density, qp=p, qs=s), None))
    return cake.LayeredModel.from_scanlines(iter(scanlines))


def _build_block(folder, index):
    from pyrocko.fomosto import qseis

    try:
        qseis.build(folder, iblock=index)
    except FileNotFoundError as error:
        # Every single-block build removes the shared .status file after the
        # block is stored; concurrent blocks can find it already removed.
        if Path(error.filename or "").name != ".status":
            raise
    return index


def _build_blocks(folder, workers):
    """Build QSEIS blocks in spawned processes and record finished blocks.

    Pyrocko's forked worker pool can stall on macOS, so each block runs in a
    separate spawned interpreter. A rerun skips blocks already listed as done.
    """
    from concurrent.futures import ProcessPoolExecutor
    import multiprocessing

    from pyrocko.fomosto import qseis

    folder = str(folder)
    done_file = Path(folder) / "blocks-done.txt"
    if not (Path(folder) / "index").is_file():
        with contextlib.redirect_stdout(io.StringIO()):
            qseis.build(folder, iblock=-1)
        done_file.unlink(missing_ok=True)
    done = set(map(int, done_file.read_text().split())) if done_file.is_file() else set()
    total = qseis.QSeisGFBuilder(folder, 0, {}).nblocks
    pending = [index for index in range(total) if index not in done]
    context = multiprocessing.get_context("spawn")
    with ProcessPoolExecutor(max(1, workers), mp_context=context) as pool:
        for index in pool.map(_build_block, [folder] * len(pending), pending):
            with done_file.open("a") as record:
                record.write(f"{index}\n")
    tmp = Path(folder) / "tmp"
    if tmp.is_dir() and not any(tmp.iterdir()):
        tmp.rmdir()


def build_stores(root, workers=1, *, diagnostic_grid=False, models=MODELS):
    from pyrocko import __version__, gf, guts
    from pyrocko.fomosto import qseis

    root = Path(root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    grid = dict(GRIDS[models])
    window = grid.pop("window")
    if diagnostic_grid:
        grid.update(
            source_depth_delta=2 * grid["source_depth_delta"],
            distance_delta=2 * grid["distance_delta"],
            distance_max=grid["distance_max"] + grid["distance_delta"],
        )
    for model_id, scale in zip(models, (1, 0.95)):
        folder = root / model_id
        if (folder / "cmt-model.json").is_file():
            continue
        if not (folder / "config").is_file():
            qseis.init(
                str(folder),
                "2006b",
                dict(
                    **grid,
                    component_scheme="elastic10",
                    earthmodel_1d=earth_model(scale),
                    tabulated_phases=[],
                ),
            )
        config_file = folder / "extra/qseis"
        config = guts.load(filename=str(config_file))
        config.time_region = config.cut = tuple(gf.meta.Timing(value) for value in window)
        guts.dump(config, filename=str(config_file))
        _build_blocks(folder, workers)
        (folder / "cmt-model.json").write_text(
            json.dumps(
                dict(
                    model=model_id,
                    pyrocko=__version__,
                    backend="qseis.2006b",
                    velocity="Wu et al. (2007) Table 1 above 700 km",
                    density_and_q="Pyrocko default ak135-f model",
                    surface_velocity_scale=scale,
                    scale_tapers_to_one_at_km=70,
                    diagnostic_grid=diagnostic_grid,
                ),
                indent=2,
            ),
            encoding="utf-8",
        )


class GreenStores:
    def __init__(self, root, models=MODELS):
        from pyrocko import gf

        self.models = tuple(models)
        directories = [Path(root) / name for name in self.models]
        digest = hashlib.sha256()
        self.diagnostic_grid = False
        for directory in directories:
            provenance = json.loads((directory / "cmt-model.json").read_text())
            self.diagnostic_grid |= bool(provenance.get("diagnostic_grid"))
            for filename in ("config", "extra/qseis", "cmt-model.json", "index", "traces"):
                with (directory / filename).open("rb") as data:
                    digest.update(hashlib.file_digest(data, "sha256").digest())
        self.version = digest.hexdigest()
        self.engine = gf.LocalEngine(store_dirs=[str(path) for path in directories])

    def banks(self, event, stations, depths=DEPTHS, *, band=BAND, velocity=False):
        from pyrocko import gf

        times = np.arange(-BEFORE, AFTER, DT)
        for model in self.models:
            targets = [
                gf.Target(
                    lat=site["lat"],
                    lon=site["lon"],
                    azimuth=azimuth,
                    dip=dip,
                    codes=(*site["id"].split("."), "", component),
                    store_id=model,
                    interpolation="multilinear",
                    quantity="displacement",
                )
                for site in stations
                for component, azimuth, dip in (
                    site["target_axes"]
                    if "target_axes" in site
                    else (
                        ("Z", 0, -90),
                        ("R", site["radial_azimuth"], 0),
                        ("T", (site["radial_azimuth"] + 90) % 360, 0),
                    )
                )
            ]
            bank = np.empty((len(depths), len(stations), 3, len(times), len(MOMENTS)))
            for i, depth in enumerate(depths):
                sources = [
                    gf.MTSource(
                        lat=event["lat"],
                        lon=event["lon"],
                        depth=depth * 1000,
                        **dict(
                            zip(
                                ("mnn", "mee", "mdd", "mne", "mnd", "med"),
                                np.asarray(moment) * UNIT,
                            )
                        ),
                    )
                    for moment in MOMENTS
                ]
                response = self.engine.process(sources, targets, nthreads=1)
                for column, traces in enumerate(response.results_list):
                    for j, item in enumerate(traces):
                        if not hasattr(item, "trace"):
                            raise ValueError(
                                "Green functions missing for a selected source or station"
                            )
                        trace = item.trace.pyrocko_trace()
                        sample_times = trace.tmin + np.arange(len(trace.ydata)) * trace.deltat
                        values = (
                            bandpass(trace.ydata, dt=trace.deltat, band=band)
                            if velocity
                            else trace.ydata
                        )
                        if velocity:
                            pad = 32
                            extended = np.pad(values, pad)
                            omega = 2 * np.pi * np.fft.rfftfreq(len(extended), trace.deltat)
                            values = np.fft.irfft(
                                1j * omega * np.fft.rfft(extended), n=len(extended)
                            )[pad:-pad]
                        samples = np.interp(
                            times, sample_times, values, left=0, right=0 if velocity else None
                        )
                        row, channel = divmod(j, 3)
                        bank[i, row, channel, :, column] = samples
            if not velocity:
                bank = bandpass(bank, band=band, axis=3)
            yield model, bank / UNIT

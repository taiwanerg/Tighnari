"""Cut-and-paste (CAP) double-couple search for small regional earthquakes.

Each station contributes up to three windows: Pnl (vertical and radial), Rayleigh
(vertical and radial) and Love (transverse). Every window has its own bounded
time shift, which absorbs travel-time errors from lateral structure that a 1-D
Green's function cannot predict (Zhao & Helmberger 1994; Zhu & Helmberger 1996;
Zhu & Ben-Zion 2013). The double couple is searched on the first-motion grid,
the scalar moment is solved analytically, and uncertainty comes from leave-one-
station-out searches, a station bootstrap and a second velocity model.

Arrays use (station, Z/R/T, samples) on the common inversion UTC grid with
(depth, station, Z/R/T, samples, basis) Green's functions in metres per N m.
"""

from functools import lru_cache
import math

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view

from .cmt import BEFORE, DT, kagan, moment_magnitude, nodal_planes
from .cmt_ensemble import LEAVE_ONE_OUT
from .focal import TAIWAN_MODEL, grid, tensor

VERSION = "regional-cap-v2"
GROUPS = (("pnl", (0, 1)), ("rayleigh", (0, 1)), ("love", (2,)))
# Bands by catalogue magnitude. Tighnari ML runs about 0.5 above the RMT and
# AutoBATS Mw in the 2026 Taiwan replay set, so the limits are written in ML.
# Event-wide offsets (s) searched before the per-window shifts. Surface waves
# arrived 1-3 s (up to 8 s for large events) after the 1-D synthetics in the
# 2026-11-24 replay, while per-station delays varied as much as their medians.
PROFILES = (
    dict(
        name="small",
        below=4.5,
        pnl=(0.06, 0.2),
        surface=(0.04, 0.1),
        distance_km=(8, 200),
        offsets=dict(pnl=(0,), surface=(0, 2, 4)),
    ),
    dict(
        name="moderate",
        below=5.5,
        pnl=(0.04, 0.15),
        surface=(0.03, 0.08),
        distance_km=(10, 300),
        offsets=dict(pnl=(0,), surface=(0, 2, 4)),
    ),
    dict(
        name="large",
        below=math.inf,
        pnl=(0.03, 0.1),
        surface=(0.02, 0.06),
        distance_km=(20, 300),
        offsets=dict(pnl=(0, 2, 4), surface=(0, 2, 4, 6, 8)),
    ),
)
DEFAULT_OFFSETS = dict(pnl=(0,), surface=(0,))
DEPTHS = tuple(range(1, 100, 2))
WEIGHTS = dict(pnl=2.0, rayleigh=1.0, love=1.0)
# Distance scaling (r / 100 km)^p equalises body and surface wave decay.
DISTANCE_POWER = dict(pnl=1.0, rayleigh=0.5, love=0.5)
# Polarities choose among mechanisms whose waveform fit is within this many VR
# points of the best (Fojtikova & Zahradnik 2014), rather than trading misfit.
POLARITY_VR_TOLERANCE = 3
# Calibrated on the 2026-11-24 replay calibration split (31 events with RMT,
# AutoBATS or USGS references); the blind split was not used to set them.
LIMITS = dict(stations=5, gap=220, vr=45, holdout_vr=30, angle=30, bootstrap=45, model=40)
# A station fitting far worse than no prediction usually has a response,
# orientation or timing fault. At most a quarter of the stations are removed.
OUTLIER_VR = -50
OUTLIER_FRACTION = 0.25
# Velocity models whose fits differ by less than this are equally plausible,
# so their mechanisms must agree.
MODEL_VR_MARGIN = 5
BOOTSTRAP_TRIALS = 100
ENERGY_CAP = 3
MIN_PNL_SEPARATION_S = 8


def profile_for(magnitude):
    for profile in PROFILES:
        if magnitude is None or not math.isfinite(magnitude) or magnitude < profile["below"]:
            return profile
    return PROFILES[-1]


def max_shift(band):
    """About half a period at the upper corner limits cycle skipping."""
    return int(np.clip(round(0.5 / band[1] / DT), 2, 10))


@lru_cache(maxsize=1)
def _taiwan_model():
    from obspy.taup import TauPyModel

    return TauPyModel(str(TAIWAN_MODEL))


def arrival_times(depth_km, distance_km):
    from obspy.geodetics import kilometers2degrees

    arrivals = _taiwan_model().get_travel_times(
        max(0.001, depth_km), kilometers2degrees(distance_km), phase_list=["p", "P", "s", "S"]
    )
    first = {}
    for arrival in arrivals:
        first.setdefault(arrival.name.lower(), arrival.time)
    return first.get("p"), first.get("s")


def group_windows(depth_km, distance_km):
    """Return sample windows on the inversion grid for each CAP group."""
    tp, ts = arrival_times(depth_km, round(distance_km, 1))
    if tp is None or ts is None:
        return {}
    windows = {}
    if ts - tp >= MIN_PNL_SEPARATION_S:
        windows["pnl"] = (tp - 4, ts - 2)
    surface = (ts - 4, max(ts + 30, distance_km / 2.5 + 30))
    windows["rayleigh"] = windows["love"] = surface
    return {
        name: (round((BEFORE + start) / DT), round((BEFORE + end) / DT))
        for name, (start, end) in windows.items()
    }


def coefficients(tensors):
    """Map NED tensors to the five deviatoric Green's function basis weights."""
    return np.asarray(tensors)[:, [0, 1, 0, 0, 1], [0, 1, 1, 2, 2]]


_PAIRS = [(k, m) for k in range(5) for m in range(k, 5)]


def _quadratic(c):
    """Features whose dot product with the packed Gram matrix gives c^T A c."""
    return np.stack([c[:, k] * c[:, m] * (1 if k == m else 2) for k, m in _PAIRS], axis=1)


def window_statistics(data, mask, bank, stations, bands_shift, offsets=None):
    """Lagged correlations and Gram matrices for every station window.

    ``data`` and ``bank`` map a band key ("pnl" or "surface") to arrays.
    ``offsets`` gives an event-wide synthetic delay in samples for each band.
    The returned X has shape (depth, station, group, lag, 5), A (..., lag, 15)
    and E (station, group). Lag index l delays the synthetic by
    offset + (lags - l) samples. Unusable windows have zero energy.
    """
    offsets = offsets or {}
    depths = next(iter(bank.values())).shape[0]
    lags = max(bands_shift.values())
    count = len(stations)
    x = np.zeros((depths, count, len(GROUPS), 2 * lags + 1, 5))
    a = np.zeros((depths, count, len(GROUPS), 2 * lags + 1, len(_PAIRS)))
    energy = np.zeros((count, len(GROUPS)))
    lag_valid = np.zeros((len(GROUPS), 2 * lags + 1), dtype=bool)
    group_offsets = np.zeros(len(GROUPS), dtype=int)
    for g, (name, components) in enumerate(GROUPS):
        key = "pnl" if name == "pnl" else "surface"
        shift = bands_shift[key]
        offset = int(offsets.get(key, 0))
        group_offsets[g] = offset
        lag_valid[g, lags - shift : lags + shift + 1] = True
        for s, site in enumerate(stations):
            window = site["windows"].get(name)
            if window is None:
                continue
            lo, hi = window
            if lo - offset - shift < 0 or hi - offset + shift > data[key].shape[-1]:
                continue
            valid = [c for c in components if mask[key][s, c, lo:hi].all()]
            if not valid:
                continue
            observed = data[key][s, valid, lo:hi]
            scale = (
                WEIGHTS[name] * (site["distance_km"] / 100) ** DISTANCE_POWER[name]
            ) ** 2 * site.get("weight", 1.0)
            energy[s, g] = scale * np.sum(observed**2)
            # Window j starts at lo - offset - shift + j: a delay of offset + shift - j.
            greens = bank[key][:, s][:, valid, lo - offset - shift : hi - offset + shift]
            view = sliding_window_view(greens, hi - lo, axis=2)
            start = lags - shift
            x[:, s, g, start : start + 2 * shift + 1] = scale * np.einsum(
                "cw,dcjkw->djk", observed, view
            )
            gram = np.einsum("dcjkw,dcjmw->djkm", view, view)
            a[:, s, g, start : start + 2 * shift + 1] = scale * np.stack(
                [gram[..., k, m] for k, m in _PAIRS], axis=-1
            )
    return x, a, energy, lag_valid, group_offsets


def _evaluate(x, a, c, q, lag_valid):
    """Per-station correlation and synthetic energy at the best lag of each window.

    Lags outside a window's own shift limit are excluded rather than left at
    zero, which would otherwise let a contradicted window drop out of the fit.
    """
    cc = np.einsum("sglk,nk->sgln", x, c)
    cc = np.where(lag_valid[None, :, :, None], cc, -np.inf)
    lag = cc.argmax(axis=2)
    best = np.take_along_axis(cc, lag[:, :, None, :], axis=2)[:, :, 0]
    energy = np.einsum("sglp,np->sgln", a, q)
    synthetic = np.take_along_axis(energy, lag[:, :, None, :], axis=2)[:, :, 0]
    return best.sum(axis=1), synthetic.sum(axis=1), lag


def _variance_reduction(s, d, e):
    with np.errstate(divide="ignore", invalid="ignore"):
        vr = np.where((s > 0) & (d > 0), s * s / (d * e), 0.0)
    return 100 * vr


def _station_factors(energy):
    total = energy.sum(axis=1)
    used = total > 0
    factors = np.zeros_like(total)
    if used.any():
        cap = ENERGY_CAP * np.median(total[used])
        factors[used] = np.minimum(1, cap / total[used])
    return factors


def polarity_penalty(tensors, observations):
    """Fraction of first motions contradicted by each tensor; near-nodal rays count half."""
    if not observations:
        return np.zeros(len(tensors))
    azimuth = np.deg2rad([row["azimuth"] for row in observations])
    theta = np.deg2rad([row["takeoff"] for row in observations])
    rays = np.column_stack(
        [np.sin(theta) * np.cos(azimuth), np.sin(theta) * np.sin(azimuth), np.cos(theta)]
    )
    signs = np.array([row["polarity"] for row in observations])
    amplitude = np.einsum("ni,kij,nj->kn", rays, tensors, rays)
    loss = np.where(np.abs(amplitude) < 0.05, 0.5, (amplitude * signs < 0).astype(float))
    return loss.mean(axis=1)


def _sums(row, c, q):
    x, a, energy, factors, lag_valid = row
    s, d, lag = _evaluate(x, a, c, q, lag_valid)
    return s * factors[:, None], d * factors[:, None], energy.sum(axis=1) * factors, lag


def _search(stats_by_depth, c, q, rng_weights, *, uncertainty=True):
    """Full, leave-one-out and bootstrap waveform searches over depth and mechanism.

    ``profile`` keeps the VR of every mechanism at every depth so a later step
    can choose within the set of near-optimal fits.
    """
    best = None
    profile = []
    loo = None
    boot = None
    for di, row in enumerate(stats_by_depth):
        s, d, e, lag = _sums(row, c, q)
        total_s, total_d, total_e = s.sum(axis=0), d.sum(axis=0), e.sum()
        score = _variance_reduction(total_s, total_d, total_e)
        n = int(np.argmax(score))
        profile.append(score)
        if best is None or score[n] > best["vr"]:
            best = dict(depth_index=di, mechanism=n, vr=float(score[n]))
        if not uncertainty:
            continue
        # Leave one station out: subtract that station's sums from the totals.
        loo_score = _variance_reduction(total_s - s, total_d - d, total_e - e[:, None])
        loo_n = loo_score.argmax(axis=1)
        loo_best = loo_score[np.arange(len(s)), loo_n]
        if loo is None:
            loo = dict(
                score=np.full(len(s), -np.inf),
                mechanism=loo_n.copy(),
                depth=np.zeros(len(s), int),
                predicted=np.full(len(s), np.nan),
            )
        better = loo_best > loo["score"]
        for i in np.flatnonzero(better):
            n_i = loo_n[i]
            rest_d = total_d[n_i] - d[i, n_i]
            m0 = (total_s[n_i] - s[i, n_i]) / rest_d if rest_d > 0 else 0.0
            residual = e[i] - 2 * m0 * s[i, n_i] + m0 * m0 * d[i, n_i]
            loo["predicted"][i] = 100 * (1 - residual / e[i]) if e[i] > 0 else np.nan
        loo["score"] = np.where(better, loo_best, loo["score"])
        loo["mechanism"] = np.where(better, loo_n, loo["mechanism"])
        loo["depth"] = np.where(better, di, loo["depth"])
        # Station bootstrap with fixed multinomial weights per trial.
        bs, bd, be = s.T @ rng_weights, d.T @ rng_weights, e @ rng_weights
        boot_score = _variance_reduction(bs, bd, be[None, :])
        boot_n = boot_score.argmax(axis=0)
        boot_best = boot_score[boot_n, np.arange(rng_weights.shape[1])]
        if boot is None:
            boot = dict(
                score=np.full(len(boot_n), -np.inf),
                mechanism=boot_n.copy(),
                depth=np.zeros(len(boot_n), int),
            )
        better = boot_best > boot["score"]
        boot["score"] = np.where(better, boot_best, boot["score"])
        boot["mechanism"] = np.where(better, boot_n, boot["mechanism"])
        boot["depth"] = np.where(better, di, boot["depth"])
    return best, np.array(profile), loo, boot


def _local_mechanisms(plane, step=2, span=10):
    strike, dip, rake = plane
    rows = []
    for ds in range(-span, span + 1, step):
        for dd in range(-span, span + 1, step):
            for dr in range(-span, span + 1, step):
                d = dip + dd
                if 1 <= d <= 90:
                    rows.append(((strike + ds) % 360, d, ((rake + dr + 180) % 360) - 180))
    planes = np.asarray(rows, dtype=float)
    return planes, np.array([tensor(*p) for p in planes])


def _statistics_for(data, mask, bank, stations, shifts_by_band, offsets=None):
    x, a, energy, lag_valid, group_offsets = window_statistics(
        data, mask, bank, stations, shifts_by_band, offsets
    )
    factors = _station_factors(energy)
    rows = [(x[di], a[di], energy, factors, lag_valid) for di in range(x.shape[0])]
    return rows, energy, group_offsets


def _solution(row, unit):
    """Waveform statistics of one unit double couple at one depth."""
    c = coefficients(unit[None])
    s, d, e, lag = _sums(row, c, _quadratic(c))
    total_s, total_d = s[:, 0].sum(), d[:, 0].sum()
    return dict(
        tensor=unit,
        vr=float(_variance_reduction(total_s, total_d, e.sum())),
        m0=float(total_s / total_d) if total_d > 0 else 0.0,
        station_s=s[:, 0],
        station_d=d[:, 0],
        station_e=e,
        lag=lag[:, :, 0],
    )


def _station_vr(solution):
    e, s, d, m0 = (solution[k] for k in ("station_e", "station_s", "station_d", "m0"))
    with np.errstate(divide="ignore", invalid="ignore"):
        return np.where(e > 0, 100 * (1 - (e - 2 * m0 * s + m0 * m0 * d) / e), np.nan)


def _choose_offsets(data, mask, bank, stations, shifts_by_band, c, q, choices):
    """Pick event-wide surface, then Pnl, delays by the best waveform fit."""
    chosen = dict(pnl=0, surface=0)
    for key in ("surface", "pnl"):
        scores = {}
        for value in choices.get(key, (0,)):
            trial = dict(chosen, **{key: round(value / DT)})
            stats, _, _ = _statistics_for(data, mask, bank, stations, shifts_by_band, trial)
            scores[trial[key]] = _search(stats, c, q, None, uncertainty=False)[0]["vr"]
        chosen[key] = max(scores, key=lambda value: (scores[value], -abs(value)))
    return chosen


def _fit_model(data, mask, bank, stations, shifts_by_band, c, q, weights, offset_choices):
    """Search one velocity model, removing gross outlier stations first."""
    offsets = _choose_offsets(data, mask, bank, stations, shifts_by_band, c, q, offset_choices)
    stats, energy, group_offsets = _statistics_for(
        data, mask, bank, stations, shifts_by_band, offsets
    )
    _, tensors = grid()
    keep = energy.sum(axis=1) > 0
    allowed = int(OUTLIER_FRACTION * keep.sum())
    rejected = []
    while True:
        current = [(x, a, e, f * keep, v) for x, a, e, f, v in stats]
        best, profile, loo, boot = _search(current, c, q, weights)
        solution = _solution(current[best["depth_index"]], tensors[best["mechanism"]])
        vr = _station_vr(solution)
        bad = [i for i in np.argsort(vr) if keep[i] and vr[i] < OUTLIER_VR]
        if not bad or len(rejected) >= allowed or keep.sum() - 1 < LIMITS["stations"]:
            break
        worst = int(bad[0])
        keep[worst] = False
        rejected.append(dict(id=stations[worst]["id"], vr_pct=float(vr[worst])))
    return dict(
        stats=current,
        group_offsets=group_offsets,
        offsets_s={key: value * DT for key, value in offsets.items()},
        used=keep,
        best=best,
        profile=profile,
        loo=loo,
        boot=boot,
        rejected=rejected,
    )


def _pick(vr, penalty, threshold):
    """Index of the best waveform fit, or of the fewest polarity errors among
    fits within the tolerance when polarities are given."""
    if penalty is None:
        return int(np.argmax(vr))
    eligible = vr >= threshold
    if not eligible.any():
        return int(np.argmax(vr))
    key = np.where(eligible, -penalty * 1e3 + vr * 1e-3, -np.inf)
    return int(np.argmax(key))


def invert_cap(
    data,
    mask,
    banks,
    stations,
    depths=DEPTHS,
    *,
    bands,
    observations=None,
    offsets=DEFAULT_OFFSETS,
    seed=0,
):
    """Search double couples for one profile.

    ``data``/``mask`` map "pnl" and "surface" to (station, ZRT, samples) arrays,
    and ``banks`` maps a model name to {"pnl": bank, "surface": bank}. The model
    with the better fit is primary; the other tests velocity-model sensitivity.
    With ``observations`` the mechanism is the one with the fewest polarity
    errors among fits within POLARITY_VR_TOLERANCE of the best.
    """
    result = dict(
        method=VERSION,
        status="insufficient_data",
        station_count=len(stations),
        band_hz=list(bands["surface"]),
        pnl_band_hz=list(bands["pnl"]),
        reasons=[],
        validation_method=LEAVE_ONE_OUT,
    )
    if len(stations) < LIMITS["stations"]:
        result["reasons"].append("too_few_stations")
        return result
    shifts_by_band = {key: max_shift(band) for key, band in bands.items()}
    planes, tensors = grid()
    c, q = coefficients(tensors), _quadratic(coefficients(tensors))
    use_polarity = bool(observations)
    penalty = polarity_penalty(tensors, observations) if use_polarity else None
    rng = np.random.default_rng(seed)
    weights = rng.multinomial(
        len(stations), np.full(len(stations), 1 / len(stations)), size=BOOTSTRAP_TRIALS
    ).T.astype(float)

    fits = {
        model: _fit_model(data, mask, bank, stations, shifts_by_band, c, q, weights, offsets)
        for model, bank in banks.items()
    }
    # The better-fitting velocity model is primary; the other tests sensitivity.
    models = sorted(fits, key=lambda name: -fits[name]["best"]["vr"])
    fit = fits[models[0]]
    primary_stats, used = fit["stats"], fit["used"]
    best, profile, loo, boot = fit["best"], fit["profile"], fit["loo"], fit["boot"]
    if used.sum() < LIMITS["stations"]:
        result.update(station_count=int(used.sum()), reasons=["too_few_stations"])
        return result

    threshold = best["vr"] - POLARITY_VR_TOLERANCE
    flat = _pick(
        profile.ravel(), None if penalty is None else np.tile(penalty, len(profile)), threshold
    )
    depth_index, mechanism = divmod(flat, len(tensors))
    # Refine near the chosen grid node at the chosen and adjacent depths.
    _, fine_tensors = _local_mechanisms(planes[mechanism])
    fine_c = coefficients(fine_tensors)
    fine_q = _quadratic(fine_c)
    fine_penalty = polarity_penalty(fine_tensors, observations) if use_polarity else None
    candidates = [(depth_index, tensors[mechanism])]
    for di in (depth_index - 1, depth_index, depth_index + 1):
        if 0 <= di < len(depths):
            s, d, e, _ = _sums(primary_stats[di], fine_c, fine_q)
            vr = _variance_reduction(s.sum(0), d.sum(0), e.sum())
            candidates.append((di, fine_tensors[_pick(vr, fine_penalty, threshold)]))
    solutions = [
        dict(_solution(primary_stats[di], unit), depth_index=di) for di, unit in candidates
    ]
    for row in solutions:
        row["polarity"] = (
            float(polarity_penalty(row["tensor"][None], observations)[0]) if use_polarity else None
        )

    def rank(row):
        # Fits within the tolerance first, then fewest polarity errors, then VR.
        return (row["vr"] >= threshold, -(row["polarity"] or 0.0), row["vr"])

    chosen = max(solutions, key=rank)
    unit = chosen["tensor"]
    m0 = chosen["m0"]
    if m0 <= 0:
        result.update(status="unconstrained", reasons=["unresolved_dc"])
        return result

    # Depth profile: every depth within 3 VR points of the optimum must agree.
    depth_best = profile.max(axis=1)
    plausible = np.flatnonzero(depth_best >= depth_best.max() - 3)
    depth_angles = [kagan(unit, tensors[int(profile[i].argmax())]) for i in plausible]
    depth_angle = None if None in depth_angles else float(max(depth_angles))
    plausible_depths = [depths[i] for i in plausible]
    loo_angles = [kagan(unit, tensors[n]) for n in loo["mechanism"][used]]
    boot_angles = [kagan(unit, tensors[n]) for n in boot["mechanism"]]
    boot_depths = [depths[i] for i in boot["depth"]]

    model_angle = model_margin = None
    if len(models) > 1:
        alternative = fits[models[1]]["best"]
        model_angle = kagan(tensors[best["mechanism"]], tensors[alternative["mechanism"]])
        model_margin = best["vr"] - alternative["vr"]

    azimuth = np.sort(np.asarray([site["azimuth"] for site, ok in zip(stations, used) if ok]) % 360)
    gap = float(np.diff(np.r_[azimuth, azimuth[0] + 360]).max())
    predicted = loo["predicted"][used]
    holdout = float(np.nanmedian(predicted)) if np.isfinite(predicted).any() else None
    loo_p90 = None if None in loo_angles else float(np.percentile(loo_angles, 90))
    boot_p90 = None if None in boot_angles else float(np.percentile(boot_angles, 90))
    centroid_resolved = not (
        plausible_depths[0] == depths[0]
        or plausible_depths[-1] == depths[-1]
        or np.ptp(plausible_depths) > 20
        or np.ptp(np.percentile(boot_depths, [5, 95])) > 20
    )
    reasons = result["reasons"]
    for invalid, reason in (
        (gap > LIMITS["gap"], "azimuth_gap"),
        (chosen["vr"] < LIMITS["vr"], "waveform_fit"),
        (holdout is None or holdout < LIMITS["holdout_vr"], "holdout_fit"),
        (
            loo_p90 is None
            or loo_p90 > LIMITS["angle"]
            or boot_p90 is None
            or boot_p90 > LIMITS["bootstrap"]
            or (
                model_angle is not None
                and model_angle > LIMITS["model"]
                and model_margin < MODEL_VR_MARGIN
            ),
            "unstable_mechanism",
        ),
        (len(models) < 2, "sensitivity_model_missing"),
        (
            not centroid_resolved and (depth_angle is None or depth_angle > LIMITS["angle"]),
            "unresolved_centroid",
        ),
    ):
        if invalid:
            reasons.append(reason)
    moment = unit * m0
    lags = primary_stats[0][4].shape[1] // 2
    station_vr = _station_vr(chosen)
    station_rows = []
    for i, site in enumerate(stations):
        if not used[i]:
            continue
        station_rows.append(
            dict(
                id=site["id"],
                provider=site.get("provider"),
                instrument=site.get("instrument", "broadband"),
                distance_km=site["distance_km"],
                azimuth=site["azimuth"],
                vr_pct=None if np.isnan(station_vr[i]) else float(station_vr[i]),
                holdout_vr_pct=None
                if np.isnan(loo["predicted"][i])
                else float(loo["predicted"][i]),
                # Positive: the observed window arrives later than the 1-D synthetic.
                shifts_s={
                    name: float((fit["group_offsets"][g] + lags - chosen["lag"][i, g]) * DT)
                    for g, (name, _) in enumerate(GROUPS)
                    if name in site["windows"]
                },
            )
        )
    result.update(
        status="unconstrained" if reasons else "experimental",
        station_count=int(used.sum()),
        model=models[0],
        sensitivity_models=models[1:],
        centroid_depth_km=float(depths[chosen["depth_index"]]),
        centroid_depth_range_km=[float(v) for v in np.percentile(boot_depths, [5, 95])],
        near_optimum_depth_km=[float(v) for v in plausible_depths],
        centroid_depth_resolved=bool(centroid_resolved),
        depth_mechanism_kagan_max_deg=depth_angle,
        mw=moment_magnitude(m0),
        scalar_moment_nm=m0,
        tensor_ned_nm=moment.tolist(),
        nodal_planes=nodal_planes(moment),
        vr_pct=chosen["vr"],
        holdout_vr_pct=holdout,
        validation_vr_pct=holdout,
        kagan_p90_deg=loo_p90,
        bootstrap_kagan_p90_deg=boot_p90,
        model_kagan_deg=model_angle,
        model_vr_margin_pct=model_margin,
        rejected_stations=fit["rejected"],
        event_offsets_s=fit["offsets_s"],
        azimuth_gap_deg=gap,
        max_shift_s={key: value * DT for key, value in shifts_by_band.items()},
        fit_stations=station_rows,
    )
    if use_polarity:
        result.update(
            polarity_count=len(observations),
            polarity_misfit=chosen["polarity"],
            waveform_kagan_deg=kagan(unit, tensors[best["mechanism"]]),
        )
    return result


def fit_waveforms(result, data, mask, banks, stations, depths=DEPTHS):
    """Observed and shifted synthetic traces of a CAP solution for review plots.

    Each window uses the synthetic delay reported in ``fit_stations`` (positive
    when the record arrives later than the 1-D synthetic) and the solution's
    scalar moment, so amplitudes are absolute rather than normalised.
    """
    if not result.get("tensor_ned_nm") or result.get("model") not in banks:
        return []
    bank = banks[result["model"]]
    depth = depths.index(result["centroid_depth_km"])
    c = coefficients(np.asarray(result["tensor_ned_nm"])[None])[0]
    index = {site["id"]: i for i, site in enumerate(stations)}
    rows = []
    for station in result.get("fit_stations") or []:
        s = index.get(station["id"])
        if s is None:
            continue
        site = stations[s]
        traces = {}
        for name, components in GROUPS:
            window = site["windows"].get(name)
            shift = (station.get("shifts_s") or {}).get(name)
            if window is None or shift is None:
                continue
            key = "pnl" if name == "pnl" else "surface"
            lo, hi = window
            delay = round(shift / DT)
            for component in components:
                if not mask[key][s, component, lo:hi].all():
                    continue
                synthetic = np.roll(bank[key][depth, s, component] @ c, delay)
                label = ("Pnl " if name == "pnl" else "") + "ZRT"[component]
                traces[label] = dict(
                    observed=data[key][s, component, lo:hi].tolist(),
                    synthetic=synthetic[lo:hi].tolist(),
                    shift_s=float(shift),
                )
        if traces:
            rows.append(
                dict(
                    id=station["id"],
                    distance_km=station["distance_km"],
                    azimuth=station["azimuth"],
                    vr_pct=station.get("vr_pct"),
                    traces=traces,
                )
            )
    return rows

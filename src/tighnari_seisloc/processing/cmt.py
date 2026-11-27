"""Independent regional waveform inversion in the north-east-down convention."""

from functools import lru_cache
from itertools import product

import numpy as np
from scipy.signal import butter, sosfiltfilt

VERSION = "regional-cmt-v1"
ENSEMBLE_VERSION = "regional-cmt-ensemble-v1"
BASIS = ("NN-DD", "EE-DD", "NE", "ND", "ED")
BAND = (0.02, 0.05)
DT = 1.0
BEFORE, AFTER = 180, 600
DEPTHS = tuple(range(2, 101, 2))
DELAYS = tuple(range(0, 21, 2))
LIMITS = dict(stations=5, gap=200, vr=55, holdout_vr=35, angle=30, depth_span=20)


@lru_cache(maxsize=32)
def _filter_coefficients(dt, band):
    return butter(3, band, fs=1 / dt, btype="bandpass", output="sos")


def bandpass(data, dt=DT, band=BAND, axis=-1):
    return sosfiltfilt(_filter_coefficients(dt, tuple(band)), data, axis=axis)


def from_coefficients(coefficients):
    n, e, ne, nd, ed = coefficients
    return np.array([[n, ne, nd], [ne, e, ed], [nd, ed, -n - e]])


def moment_magnitude(moment_nm):
    return float(2 / 3 * (np.log10(moment_nm) - 9.1))


def dc_frame(tensor):
    values, vectors = np.linalg.eigh(tensor)
    if np.linalg.det(vectors) < 0:
        vectors[:, 1] *= -1
    return values, vectors


def kagan(left, right):
    """Minimum proper rotation of the two best double couples."""
    left_values, a = dc_frame(left)
    right_values, b = dc_frame(right)
    for values in (left_values, right_values):
        if min(np.diff(values)) <= 1e-8 * max(abs(values)):
            return None
    diagonal = np.diag(a.T @ b)
    trace = max(sum(diagonal * (x, y, x * y)) for x, y in product((-1, 1), repeat=2))
    return float(np.degrees(np.arccos(np.clip((trace - 1) / 2, -1, 1))))


def rtp_to_ned(components):
    """Convert (rr, tt, pp, rt, rp, tp), where r is up, theta south and phi east."""
    rr, tt, pp, rt, rp, tp = components
    return np.array([[tt, -tp, rt], [-tp, pp, -rp], [rt, -rp, rr]])


def nodal_planes(tensor):
    """Return the two strike/dip/rake triples of the best double couple."""
    values, frame = dc_frame(tensor)
    if min(np.diff(values)) <= 1e-8 * max(abs(values)):
        return None
    pressure, tension = frame[:, 0], frame[:, 2]
    planes = []
    for normal, slip in (
        (tension - pressure, tension + pressure),
        (tension + pressure, tension - pressure),
    ):
        if normal[2] < 0:
            normal, slip = -normal, -slip
        normal /= np.sqrt(2)
        slip /= np.sqrt(2)
        # The existing focal tensor uses down-cross-strike as the upward normal.
        strike = np.arctan2(normal[0], -normal[1]) if np.hypot(*normal[:2]) > 1e-10 else 0.0
        dip = np.arccos(np.clip(normal[2], -1, 1))
        along = np.array([np.cos(strike), np.sin(strike), 0])
        down = np.array([-np.sin(strike) * np.cos(dip), np.cos(strike) * np.cos(dip), np.sin(dip)])
        rake = np.arctan2(slip @ down, -slip @ along)
        planes.append(
            [float(np.degrees(strike) % 360), float(np.degrees(dip)), float(np.degrees(rake))]
        )
    return sorted(planes)


def delayed_waveforms(greens, shift):
    shifted = np.roll(greens, shift, axis=2)
    if shift > 0:
        shifted[:, :, :shift] = 0
    elif shift < 0:
        shifted[:, :, shift:] = 0
    return shifted


def phase_prediction(greens, shift, surface_shift=0, surface_fraction=None):
    body = delayed_waveforms(greens, shift)
    if not surface_shift:
        return body
    surface = delayed_waveforms(greens, shift + surface_shift)
    fraction = surface_fraction if body.ndim == 3 else surface_fraction[..., None]
    return body * (1 - fraction) + surface * fraction


def _statistics(data, bank, weights, timings, dt, surface_fraction):
    """Small per-station normal systems allow complete re-searches for each omission."""
    energy = np.einsum("sct,sct,sct->s", data, data, weights)
    shape = (len(bank), len(timings), len(data))
    gram = np.empty((*shape, 5, 5))
    rhs = np.empty((*shape, 5))
    samples = data.reshape(len(data), -1, 1)
    sample_weights = weights.reshape(len(data), -1, 1)
    for depth_index, greens in enumerate(bank):
        for delay_index, (delay, surface) in enumerate(timings):
            shifted = phase_prediction(
                greens, round(delay / dt), round(surface / dt), surface_fraction
            )
            design = shifted.reshape(len(data), -1, 5)
            weighted = (design * sample_weights).swapaxes(1, 2)
            gram[depth_index, delay_index] = weighted @ design
            rhs[depth_index, delay_index] = (weighted @ samples)[..., 0]
    return gram, rhs, energy


def _best(stats, keep, *, return_profile=False, energy_cap=None):
    gram, rhs, energy = stats
    energy = energy[keep]
    if energy_cap is None:
        g = gram[:, :, keep].sum(axis=2)
        b = rhs[:, :, keep].sum(axis=2)
    else:
        factors = np.minimum(1, energy_cap * np.median(energy) / energy)
        g = np.einsum("dtsij,s->dtij", gram[:, :, keep], factors)
        b = np.einsum("dtsi,s->dti", rhs[:, :, keep], factors)
        energy = energy * factors
    values = np.linalg.eigvalsh(g)
    valid = (values[..., 0] > values[..., -1] * 1e-10) & (values[..., -1] > 0)
    if not valid.any():
        return None
    coeff = np.zeros_like(b)
    coeff[valid] = np.linalg.solve(g[valid], b[valid][..., None])[..., 0]
    improvement = np.einsum("...i,...i->...", b, coeff)
    surface = 100 * np.where(valid, improvement, -np.inf) / energy.sum()
    di, ti = np.unravel_index(np.argmax(surface), valid.shape)
    trial = (di, ti, coeff[di, ti], float(surface[di, ti]))
    if not return_profile:
        return trial
    best_delay = surface.argmax(axis=1)
    depth_coefficients = coeff[np.arange(len(coeff)), best_delay]
    return trial, surface.max(axis=1), depth_coefficients


def _prediction_score(stats, station, trial):
    gram, rhs, energy = stats
    di, ti, m, _ = trial
    improvement = 2 * rhs[di, ti, station] @ m - m @ gram[di, ti, station] @ m
    return float(100 * improvement / energy[station])


def invert(
    data,
    banks,
    stations,
    depths=DEPTHS,
    delays=DELAYS,
    dt=DT,
    *,
    band=BAND,
    energy_cap=None,
    sample_weights=None,
    research_details=False,
    surface_offsets=(0,),
    surface_fraction=None,
    skip_degenerate_depths=False,
):
    """Fit displacement against five Green functions per channel and unit N m.

    ``skip_degenerate_depths`` leaves out near-optimal depths whose tensor has no
    defined double couple (Kagan angle undefined) when testing whether the
    mechanism depends on depth; by default any such depth counts as dependence.

    Arrays use (station, Z/R/T, samples) with (depth, station, Z/R/T, samples, basis)
    for each bank. Use a common UTC grid; the first bank is the preferred model.
    """
    data = np.asarray(data, dtype=float)
    result = dict(
        method=VERSION,
        status="insufficient_data",
        station_count=len(stations),
        band_hz=list(band),
        reasons=[],
    )
    if len(stations) < LIMITS["stations"]:
        result["reasons"] = ["too_few_stations"]
        return result, None
    if (
        data.ndim != 3
        or data.shape[:2] != (len(stations), 3)
        or len({s["id"] for s in stations}) != len(stations)
        or not np.isfinite(data).all()
        or len(depths) < 3
        or not len(delays)
        or not np.isfinite([*depths, *delays, dt]).all()
        or dt <= 0
        or min(delays) < 0
        or max(delays) >= data.shape[-1] * dt
        or any(not np.isclose(d / dt, round(d / dt)) for d in delays)
        or np.any(np.diff(depths) <= 0)
        or np.any(np.diff(delays) <= 0)
        or not banks
    ):
        raise ValueError("Invalid regional CMT observations or grid")
    weights = np.zeros_like(data)
    if (
        not len(surface_offsets)
        or not np.isfinite(surface_offsets).all()
        or any(not np.isclose(s / dt, round(s / dt)) for s in surface_offsets)
        or min(delays) + min(surface_offsets) <= -data.shape[-1] * dt
        or max(delays) + max(surface_offsets) >= data.shape[-1] * dt
    ):
        raise ValueError("Invalid surface wave timing grid")
    if any(surface_offsets):
        surface_fraction = np.asarray(surface_fraction)
        if (
            surface_fraction.shape != (len(stations), 1, data.shape[-1])
            or not np.isfinite(surface_fraction).all()
            or np.any((surface_fraction < 0) | (surface_fraction > 1))
        ):
            raise ValueError("Invalid surface wave fraction")
    timings = list(product(delays, surface_offsets))
    noise = np.asarray([s["noise"] for s in stations], dtype=float)
    if not np.isfinite(noise).all() or min(noise) <= 0:
        raise ValueError("Expected positive station noise")
    scale = np.clip(noise / np.median(noise), 0.5, 2.0)
    for i, site in enumerate(stations):
        lo, hi = site["window"]
        if not 0 <= lo < hi <= data.shape[-1]:
            raise ValueError("Invalid station fitting window")
        weights[i, :, lo:hi] = 1
    if sample_weights is not None:
        sample_weights = np.asarray(sample_weights, dtype=float)
        if (
            sample_weights.shape != data.shape
            or not np.isfinite(sample_weights).all()
            or np.any(sample_weights < 0)
        ):
            raise ValueError("Invalid component sample weights")
        weights = sample_weights
    totals = weights.sum(axis=(1, 2))
    if np.any(totals <= 0):
        raise ValueError("Empty station fitting window")
    weights = weights * (3 / (totals * scale**2))[:, None, None]
    if energy_cap is not None and (not np.isfinite(energy_cap) or energy_cap <= 0):
        raise ValueError("Invalid station energy cap")
    azimuth = np.sort(np.asarray([s["azimuth"] for s in stations]) % 360)
    gap = float(max(np.diff(np.r_[azimuth, azimuth[0] + 360])))
    result["azimuth_gap_deg"] = gap
    if np.any(np.einsum("sct,sct,sct->s", data, data, weights) <= 0):
        raise ValueError("Empty station fitting window")
    keep = np.ones(len(stations), dtype=bool)
    primary = None
    alternatives = []
    for name, bank in banks.items():
        if bank.shape != (len(depths), *data.shape, 5) or not np.isfinite(bank).all():
            raise ValueError("Incompatible Green function bank")
        stats = _statistics(data, bank, weights, timings, dt, surface_fraction)
        trial = _best(stats, keep, return_profile=primary is None, energy_cap=energy_cap)
        if primary is None:
            if trial is None:
                return dict(result, status="unconstrained", reasons=["rank_deficient"]), None
            primary = (name, bank, stats, trial)
        else:
            alternatives.append(trial)
    name, bank, stats, ((di, ti, coeff, vr), profile, depth_coefficients) = primary
    delay, surface = timings[ti]
    plausible = profile >= vr - 3
    plausible_depths = np.asarray(depths)[plausible]
    profile_span = float(np.ptp(plausible_depths))
    profile_boundary = bool(plausible_depths[0] == depths[0] or plausible_depths[-1] == depths[-1])
    moment = from_coefficients(coeff)
    m0 = float(np.linalg.norm(moment) / np.sqrt(2))
    planes = nodal_planes(moment)
    if not m0 or planes is None:
        return dict(result, status="unconstrained", reasons=["unresolved_dc"]), None

    scores = []
    sensitivities = []
    for i, site in enumerate(stations):
        keep[i] = False
        trial = _best(stats, keep, energy_cap=energy_cap)
        keep[i] = True
        if trial is not None:
            score = dict(id=site["id"], vr_pct=_prediction_score(stats, i, trial))
            if research_details:
                score.update(
                    training_vr_pct=trial[3],
                    centroid_depth_km=float(depths[trial[0]]),
                    tensor_ned_nm=from_coefficients(trial[2]).tolist(),
                )
            scores.append(score)
            sensitivities.append(trial)
    sensitivities.extend(test for test in alternatives if test is not None)
    angles = [kagan(moment, from_coefficients(test[2])) for test in sensitivities]
    complete = len(scores) == len(stations) and None not in alternatives and None not in angles
    p90 = float(np.percentile(angles, 90)) if complete else None
    holdout = float(np.median([row["vr_pct"] for row in scores])) if scores else None
    range_km = [
        float(x)
        for x in np.percentile([depths[test[0]] for test in sensitivities] + [depths[di]], [5, 95])
    ]
    # A flat depth profile leaves the centroid depth open. The mechanism remains
    # usable when every near-optimal depth gives the same double couple.
    centroid_resolved = not (
        di in (0, len(depths) - 1)
        or any(t[0] in (0, len(depths) - 1) for t in sensitivities)
        or range_km[1] - range_km[0] > LIMITS["depth_span"]
        or profile_boundary
        or profile_span > LIMITS["depth_span"]
    )
    depth_angles = [
        kagan(moment, from_coefficients(c)) for c in depth_coefficients[plausible] if np.any(c)
    ]
    if skip_degenerate_depths:
        depth_angles = [angle for angle in depth_angles if angle is not None]
    depth_angle = None if None in depth_angles or not depth_angles else max(depth_angles)
    mechanism_depends_on_depth = depth_angle is None or depth_angle > LIMITS["angle"]
    reasons = result["reasons"]
    for invalid, reason in (
        (gap > LIMITS["gap"], "azimuth_gap"),
        (vr < LIMITS["vr"], "waveform_fit"),
        (holdout is None or holdout < LIMITS["holdout_vr"], "holdout_fit"),
        (not alternatives, "sensitivity_model_missing"),
        (
            p90 is None
            or p90 > LIMITS["angle"]
            or (alternatives and complete and max(angles[-len(alternatives) :]) > LIMITS["angle"]),
            "unstable_mechanism",
        ),
        (
            (len(delays) > 1 and delay == delays[-1])
            or (not centroid_resolved and mechanism_depends_on_depth),
            "unresolved_centroid",
        ),
    ):
        if invalid:
            reasons.append(reason)
    prediction = phase_prediction(
        bank[di] @ coeff, round(delay / dt), round(surface / dt), surface_fraction
    )
    result.update(
        status="unconstrained" if reasons else "experimental",
        model=name,
        sensitivity_models=list(banks)[1:],
        centroid_depth_km=float(depths[di]),
        centroid_delay_s=float(delay),
        mw=moment_magnitude(m0),
        scalar_moment_nm=m0,
        tensor_ned_nm=moment.tolist(),
        nodal_planes=planes,
        vr_pct=vr,
        holdout_vr_pct=holdout,
        kagan_p90_deg=p90,
        centroid_depth_range_km=range_km,
        near_optimum_depth_km=plausible_depths.tolist(),
        centroid_depth_resolved=centroid_resolved,
        depth_mechanism_kagan_max_deg=depth_angle,
        stations=stations,
        holdout=scores,
    )
    if any(surface_offsets):
        result["surface_delay_offset_s"] = float(surface)
    if research_details:
        energy = stats[2]
        factors = (
            np.minimum(1, energy_cap * np.median(energy) / energy)
            if energy_cap
            else np.ones_like(energy)
        )
        result["station_influence"] = [
            dict(
                id=site["id"], weight_factor=float(factor), observed_energy_fraction=float(fraction)
            )
            for site, factor, fraction in zip(stations, factors, energy / energy.sum())
        ]
        result["mw_sensitivity_range"] = np.percentile(
            [result["mw"]]
            + [
                moment_magnitude(np.linalg.norm(from_coefficients(test[2])) / np.sqrt(2))
                for test in sensitivities
                if np.any(test[2])
            ],
            [5, 95],
        ).tolist()
    return result, prediction

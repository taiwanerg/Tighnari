"""Regional moment experiments with a common, unretimed validation panel."""

import numpy as np
from scipy.ndimage import convolve1d

from .cmt import (
    BAND,
    BEFORE,
    DEPTHS,
    DT,
    LIMITS,
    bandpass,
    invert,
    phase_prediction,
)
from .cmt_ensemble import competing_solutions, jackknife_validation, validation_plan

VERSION = "regional-cmt-moment-v1"
VALIDATION_BAND = (0.015, 0.09)
PROFILES = (
    ("point", BAND, 0, (0,)),
    ("short-phase", (0.035, 0.085), 0, (0, -8, 8)),
    ("long-phase", (0.016, 0.045), 0, (0, -8, 8)),
    ("long-8s", (0.016, 0.045), 8, (0, -8, 8)),
    ("long-16s", (0.016, 0.045), 16, (0, -8, 8)),
)
DELAYS = tuple(range(0, 29, 4))


def source_duration(bank, duration_s, dt):
    """Convolve with a centered triangle of unit discrete area."""
    half_samples = round(duration_s / (2 * dt))
    if not half_samples:
        return bank
    kernel = half_samples + 1 - np.abs(np.arange(-half_samples, half_samples + 1))
    return convolve1d(bank, kernel / kernel.sum(), axis=3, mode="constant")


def surface_fraction(stations, samples, dt):
    """Taper from regional body waves to surface waves at a group-velocity proxy."""
    distance = np.array([site["distance_km"] for site in stations])
    arrival = BEFORE + distance / 3.45 + 8
    phase = np.clip((np.arange(samples) * dt - arrival[:, None] + 8) / 16, 0, 1)
    return ((1 - np.cos(np.pi * phase)) / 2)[:, None, :]


def component_weights(data, stations, dt, mask):
    weights = np.zeros_like(data)
    noise = (
        np.sqrt(np.mean(data[:, :, round(30 / dt) : round(120 / dt)] ** 2, axis=-1))
        if mask is None
        else np.asarray([site["component_noise"] for site in stations], dtype=float)
    )
    scale = np.nanmedian(noise, axis=1, keepdims=True)
    ratio = np.divide(noise, scale, out=np.ones_like(noise), where=scale > 0)
    factors = np.clip(np.nan_to_num(ratio, nan=1), 0.5, 2) ** -2
    for i, site in enumerate(stations):
        lo, hi = site["window"]
        weights[i, :, lo:hi] = factors[i, :, None]
        if mask is None:
            site["noise"] = max(float(np.sqrt(np.mean(noise[i] ** 2))), 1e-30)
    return weights if mask is None else weights * mask


def amplitude_scores(data, predicted, stations, validation, mask):
    """Post-fit amplitudes diagnose scale and polarity, without recalibrating the tensor."""
    scores = []
    for i in validation:
        lo, hi = stations[i]["window"]
        recorded = data[i, :, lo:hi]
        synthetic = predicted[i, :, lo:hi]
        components = [axis[0] for axis in stations[i].get("target_axes", ())] or list("ZRT")
        valid = np.ones(recorded.shape, dtype=bool) if mask is None else mask[i, :, lo:hi] > 0
        per_component = [
            dict(component=component, **amplitude_metrics(observed[keep], modeled[keep]))
            for component, observed, modeled, keep in zip(
                components, recorded, synthetic, valid, strict=True
            )
            if keep.any()
        ]
        if mask is not None:
            recorded, synthetic = recorded[valid], synthetic[valid]
        scores.append(
            dict(
                id=stations[i]["id"],
                components=per_component,
                **amplitude_metrics(recorded, synthetic),
            )
        )
    return scores


def amplitude_metrics(recorded, synthetic):
    cross = float(np.sum(recorded * synthetic))
    dd = float(np.sum(recorded**2))
    ss = float(np.sum(synthetic**2))
    scale = cross / ss if ss else None
    return dict(
        vr_pct=float(100 * (1 - np.sum((recorded - synthetic) ** 2) / dd)) if dd else None,
        amplitude_scale=scale,
        correlation=float(np.clip(cross / (np.sqrt(dd) * np.sqrt(ss)), -1, 1))
        if dd and ss
        else None,
        implied_mw_shift=float(2 / 3 * np.log10(scale)) if scale and scale > 0 else None,
    )


def fit_profile(data, banks, stations, training, depths, delays, dt, profile, mask):
    name, band, duration, offsets = profile
    ix = list(training)
    filtered = bandpass(data[ix], dt=dt, band=band)
    sites = [stations[i].copy() for i in ix]
    weights = component_weights(filtered, sites, dt, None if mask is None else mask[ix])
    fractions = surface_fraction(stations, data.shape[-1], dt)
    train_banks = {
        model: bandpass(source_duration(bank[:, ix], duration, dt), dt=dt, band=band, axis=3)
        for model, bank in banks.items()
    }
    fitted, _ = invert(
        filtered,
        train_banks,
        sites,
        depths,
        delays,
        dt,
        band=band,
        energy_cap=3,
        sample_weights=weights,
        research_details=True,
        surface_offsets=offsets,
        surface_fraction=fractions[ix],
    )
    fitted.update(id=name, strategy=name, source_duration_s=duration, validation_vr_pct=None)
    if "tensor_ned_nm" not in fitted:
        return fitted, None
    tensor = np.asarray(fitted["tensor_ned_nm"])
    coefficients = tensor[[0, 1, 0, 0, 1], [0, 1, 1, 2, 2]]
    depth = list(depths).index(fitted["centroid_depth_km"])
    bank = source_duration(banks[fitted["model"]][depth : depth + 1], duration, dt)[0]
    modeled = phase_prediction(
        bank @ coefficients,
        round(fitted["centroid_delay_s"] / dt),
        round(fitted.get("surface_delay_offset_s", 0) / dt),
        fractions,
    )
    return fitted, modeled


def validate_profile(fitted, data, modeled, stations, validation, mask):
    if modeled is None:
        return
    if not validation:
        jackknife_validation(fitted)
        return
    scores = amplitude_scores(data, modeled, stations, validation, mask)
    fitted["validation_scores"] = scores
    vr = [row["vr_pct"] for row in scores if row["vr_pct"] is not None]
    if len(vr) == len(validation):
        fitted["validation_vr_pct"] = float(np.median(vr))
    if len(vr) != len(validation) or np.median(vr) < LIMITS["holdout_vr"]:
        fitted["status"] = "unconstrained"
        fitted["reasons"].append("common_validation_fit")


def invert_moment(data, banks, stations, depths=DEPTHS, delays=DELAYS, dt=DT, *, mask=None):
    result = dict(
        method=VERSION,
        status="insufficient_data",
        station_count=len(stations),
        pool_station_count=len(stations),
        band_hz=list(VALIDATION_BAND),
        candidates=[],
        solutions=[],
    )
    training, validation, method = validation_plan(stations)
    if method is None:
        return dict(result, reasons=["too_few_validation_stations"]), None
    result["validation_method"] = method
    result["validation_stations"] = [stations[i]["id"] for i in validation]
    if not banks:
        return dict(result, reasons=["sensitivity_model_missing"]), None

    predictions = {}
    for profile in PROFILES:
        fitted, modeled = fit_profile(
            data, banks, stations, training, depths, delays, dt, profile, mask
        )
        validate_profile(fitted, data, modeled, stations, validation, mask)
        predictions[fitted["id"]] = modeled
        result["candidates"].append(fitted)

    solutions = competing_solutions(result["candidates"])
    result["solutions"] = solutions
    result["validation_band_hz"] = list(VALIDATION_BAND)
    if not solutions:
        return dict(result, status="unconstrained", reasons=["no_validated_candidate"]), None
    if len(solutions) > 1:
        return dict(result, status="ambiguous", reasons=["competing_mechanisms"]), None
    selected = solutions[0]
    result.update(selected)
    result.update(method=VERSION, selected_candidate=selected["id"])
    return result, predictions[selected["id"]]

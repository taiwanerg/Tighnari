"""Compare regional CMT candidates using the same stations withheld from inversion."""

from itertools import combinations

import numpy as np

from .cmt import BAND, DELAYS, DEPTHS, DT, ENSEMBLE_VERSION, LIMITS, invert, kagan

POOL_LIMIT = 16
TRAIN_LIMIT = 12
VALIDATION_COUNT = 3
MIN_POOL_SIZE = LIMITS["stations"] + VALIDATION_COUNT
# Without a separate panel, each station is predicted by a full re-search
# that omits it. One station more than the fitting minimum keeps every
# omission at or above that minimum.
JACKKNIFE_MIN = LIMITS["stations"] + 1
COMPETING_VR_MARGIN = 10
COMMON_PANEL = "common_panel"
LEAVE_ONE_OUT = "leave_one_station_out"


def split_stations(stations):
    """Choose a validation panel from geometry alone, leaving a usable training pool."""
    azimuth = np.asarray([site["azimuth"] for site in stations]) % 360
    indices = tuple(range(len(stations)))

    def gap(group):
        az = np.sort(azimuth[list(group)])
        return float(np.diff(np.r_[az, az[0] + 360]).max())

    ranked = []
    for validation in combinations(indices, VALIDATION_COUNT):
        training = tuple(i for i in indices if i not in validation)
        train_gap = gap(training)
        if train_gap <= LIMITS["gap"]:
            ranked.append((gap(validation), train_gap, validation, training))
    if not ranked:
        return (), ()
    _, _, validation, training = min(ranked)
    return training, validation


def validation_plan(stations):
    """Return training, validation and method, preferring a common held-out panel.

    Small regional events seldom leave enough stations for both a three-station
    panel and an azimuthally adequate training set. They fall back to
    leave-one-station-out prediction, which ``invert`` computes with a complete
    depth, delay and tensor search for every omission.
    """
    if len(stations) >= MIN_POOL_SIZE:
        training, validation = split_stations(stations)
        if validation:
            return training, validation, COMMON_PANEL
    if len(stations) >= JACKKNIFE_MIN:
        return tuple(range(len(stations))), (), LEAVE_ONE_OUT
    return (), (), None


def jackknife_validation(candidate):
    """Use the omitted-station predictions as the candidate's validation score."""
    candidate["validation_method"] = LEAVE_ONE_OUT
    candidate["validation_vr_pct"] = candidate.get("holdout_vr_pct")
    if candidate["validation_vr_pct"] is None:
        candidate["reasons"].append("holdout_fit")
    if candidate["reasons"] and candidate["status"] == "experimental":
        candidate["status"] = "unconstrained"


def station_groups(stations, training):
    """Compare sector coverage, strongest signals and nearest useful station groups."""
    size = min(TRAIN_LIMIT, max(LIMITS["stations"], (len(training) + 1) // 2))
    balanced = tuple(training[:TRAIN_LIMIT])
    signal = sorted(training, key=lambda i: (-stations[i]["snr"], stations[i]["id"]))
    nearest = sorted(training, key=lambda i: (stations[i]["distance_km"], stations[i]["id"]))
    groups = {
        "balanced": balanced,
        "high_snr": tuple(sorted(signal[:size])),
        "nearby": tuple(sorted(nearest[:size])),
    }
    unique = {}
    for name, indices in groups.items():
        unique.setdefault(indices, name)
    return {name: indices for indices, name in unique.items()}


def predict(bank, result, depths, dt):
    """Evaluate the already fitted tensor without changing its time or amplitude."""
    tensor = np.asarray(result["tensor_ned_nm"])
    coefficients = tensor[[0, 1, 0, 0, 1], [0, 1, 1, 2, 2]]
    depth_index = list(depths).index(result["centroid_depth_km"])
    shift = round(result["centroid_delay_s"] / dt)
    modeled = np.roll(bank[depth_index] @ coefficients, shift, axis=2)
    modeled[:, :, :shift] = 0
    return modeled


def validation_scores(data, modeled, stations, indices, *, mask=None):
    scores = []
    for index in indices:
        site = stations[index]
        lo, hi = site["window"]
        recorded = data[index, :, lo:hi]
        predicted = modeled[index, :, lo:hi]
        if mask is not None:
            valid = mask[index, :, lo:hi] > 0
            recorded, predicted = recorded[valid], predicted[valid]
        energy = np.sum(recorded**2)
        difference = recorded - predicted
        vr = float(100 * (1 - np.sum(difference**2) / energy)) if energy > 0 else None
        scores.append(dict(id=site["id"], vr_pct=vr))
    return scores


def competing_solutions(candidates):
    """Return representatives of similarly predictive but distinct DC families."""
    passing = sorted(
        (row for row in candidates if not row["reasons"] and row["status"] == "experimental"),
        key=lambda row: (-row["validation_vr_pct"], row["kagan_p90_deg"], row["id"]),
    )
    if not passing:
        return []
    best_vr = passing[0]["validation_vr_pct"]
    groups = []
    for row in passing:
        if best_vr - row["validation_vr_pct"] > COMPETING_VR_MARGIN:
            break
        tensor = np.asarray(row["tensor_ned_nm"])
        for group in groups:
            angles = [kagan(tensor, np.asarray(member["tensor_ned_nm"])) for member in group]
            if all(angle is not None and angle <= LIMITS["angle"] for angle in angles):
                group.append(row)
                break
        else:
            groups.append([row])
    return [dict(group[0], supporting_candidates=[row["id"] for row in group]) for group in groups]


def invert_ensemble(data, banks, stations, depths=DEPTHS, delays=DELAYS, dt=DT):
    result = dict(
        method=ENSEMBLE_VERSION,
        status="insufficient_data",
        station_count=len(stations),
        pool_station_count=len(stations),
        band_hz=list(BAND),
        reasons=[],
        candidates=[],
        solutions=[],
    )
    training, validation, method = validation_plan(stations)
    if method is None:
        result["reasons"] = ["too_few_validation_stations"]
        return result, None
    result["validation_method"] = method
    result["validation_stations"] = [stations[i]["id"] for i in validation]
    if not banks:
        result["reasons"] = ["sensitivity_model_missing"]
        return result, None
    data = np.asarray(data)
    models = list(banks)
    groups = station_groups(stations, training)
    for strategy, indices in groups.items():
        ix = list(indices)
        subset = {name: bank[:, ix] for name, bank in banks.items()}
        train_stations = [stations[i] for i in ix]
        profiles = [(name, None) for name in models]
        if strategy == "balanced":
            profiles.append((models[0], 3))
        for model, energy_cap in profiles:
            ordered = {model: subset[model]}
            ordered.update((name, subset[name]) for name in models if name != model)
            candidate, _ = invert(
                data[ix], ordered, train_stations, depths, delays, dt, energy_cap=energy_cap
            )
            candidate.update(
                id=f"{strategy}-{model}" + ("-capped" if energy_cap else ""),
                strategy=strategy,
                model=model,
                validation_vr_pct=None,
                validation_scores=[],
                training_stations=[site["id"] for site in train_stations],
            )
            if "tensor_ned_nm" in candidate and not validation:
                jackknife_validation(candidate)
            elif "tensor_ned_nm" in candidate:
                modeled = predict(banks[model], candidate, depths, dt)
                scores = validation_scores(data, modeled, stations, validation)
                candidate["validation_scores"] = scores
                values = [r["vr_pct"] for r in scores if r["vr_pct"] is not None]
                if len(values) == len(validation):
                    candidate["validation_vr_pct"] = float(np.median(values))
                if len(values) != len(validation) or np.median(values) < LIMITS["holdout_vr"]:
                    candidate["status"] = "unconstrained"
                    candidate["reasons"].append("common_validation_fit")
            result["candidates"].append(candidate)
    solutions = competing_solutions(result["candidates"])
    result["solutions"] = solutions
    if not solutions:
        result.update(status="unconstrained", reasons=["no_validated_candidate"])
        return result, None
    if len(solutions) > 1:
        result.update(status="ambiguous", reasons=["competing_mechanisms"])
        return result, None
    selected = solutions[0]
    result.update(selected)
    result.update(method=ENSEMBLE_VERSION, selected_candidate=selected["id"])
    return result, predict(banks[selected["model"]], selected, depths, dt)

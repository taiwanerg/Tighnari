"""Offline-only CMT alternatives with a location search repeated in every held-out fold."""

import numpy as np

from .cmt import BEFORE, LIMITS, kagan


def phase_weights(stations, masks):
    """Give both early regional phases and late surface waves weight when observed."""
    weights = masks.copy()
    for i, site in enumerate(stations):
        boundary = round(BEFORE + site["distance_km"] / 3.45 + 8)
        early = weights[i, :, :boundary]
        late = weights[i, :, boundary:]
        if early.sum() >= 24 and late.sum() >= 24:
            early /= early.sum()
            late /= late.sum()
    return weights


def horizontal_solution(trials, *, outer_edge_km=None):
    """Select geometry by waveform fit on the training stations alone."""
    candidates = [trial for trial in trials if "tensor_ned_nm" in trial]
    if not candidates:
        return trials[0]
    solution = max(candidates, key=lambda r: r["vr_pct"]).copy()
    full_tensor = np.asarray(solution["tensor_ned_nm"])
    held_out = {}
    for trial in candidates:
        for row in trial["holdout"]:
            previous = held_out.get(row["id"])
            if previous is None or row["training_vr_pct"] > previous["training_vr_pct"]:
                held_out[row["id"]] = dict(row, offset_km=trial["offset_km"])
    solution["holdout"] = list(held_out.values())
    reasons = solution["reasons"].copy()
    if held_out:
        holdout = float(np.median([row["vr_pct"] for row in held_out.values()]))
        solution["holdout_vr_pct"] = holdout
        if "holdout_fit" in reasons:
            reasons.remove("holdout_fit")
        if holdout < LIMITS["holdout_vr"] or len(held_out) != solution["station_count"]:
            reasons.append("holdout_fit")
        angles = [kagan(full_tensor, np.asarray(r["tensor_ned_nm"])) for r in held_out.values()]
        if None in angles or np.percentile(angles, 90) > LIMITS["angle"]:
            if "unstable_mechanism" not in reasons:
                reasons.append("unstable_mechanism")
        solution["horizontal_fold_kagan_p90_deg"] = (
            float(np.percentile(angles, 90)) if None not in angles else None
        )
        depths = [r["centroid_depth_km"] for r in held_out.values()]
        if np.ptp(np.percentile(depths, [5, 95])) > LIMITS["depth_span"]:
            # Fold mechanism stability is tested above; depth spread alone
            # leaves the centroid depth, not the mechanism, unresolved.
            solution["centroid_depth_resolved"] = False
    offset = np.asarray(solution["offset_km"])
    if max(abs(offset)) >= outer_edge_km if outer_edge_km is not None else offset.any():
        reasons.append("horizontal_grid_edge")
    if outer_edge_km is not None:
        near = [row["offset_km"] for row in candidates if row["vr_pct"] >= solution["vr_pct"] - 3]
        spread = np.asarray(near + [r["offset_km"] for r in held_out.values()])
        solution["centroid_offset_range_km"] = np.stack(
            (spread.min(axis=0), spread.max(axis=0))
        ).tolist()
        if np.ptp(spread, axis=0).max() > outer_edge_km:
            reasons.append("unresolved_horizontal_centroid")
    solution["status"] = "unconstrained" if reasons else "experimental"
    solution["reasons"] = reasons
    solution["horizontal_trials"] = [
        dict(offset_km=r["offset_km"], vr_pct=r["vr_pct"], centroid_depth_km=r["centroid_depth_km"])
        for r in candidates
    ]
    return solution

"""Joint regional centroid search with nested station omission."""

from .cmt import DT, LIMITS
from .cmt_ensemble import competing_solutions, validation_plan
from .cmt_moment import DELAYS, PROFILES, VALIDATION_BAND, fit_profile, validate_profile
from .cmt_research import horizontal_solution

VERSION = "regional-cmt-centroid-v1"
OUTER_KM = 36
OFFSETS = (
    (0, 0),
    (18, 0),
    (-18, 0),
    (0, 18),
    (0, -18),
    (36, 0),
    (-36, 0),
    (0, 36),
    (0, -36),
    (36, 36),
    (-36, 36),
    (36, -36),
    (-36, -36),
)
DEPTHS = (*range(2, 99, 4), 100)
SEARCH_PROFILES = tuple(profile for profile in PROFILES if profile[0] in ("point", "long-8s"))


def invert_centroid(data, stations, nodes, *, mask=None, depths=DEPTHS, delays=DELAYS):
    """Keep the geometry and tensor of every candidate independent of the common holdouts."""
    result = dict(
        method=VERSION,
        status="insufficient_data",
        station_count=len(stations),
        pool_station_count=len(stations),
        candidates=[],
        solutions=[],
        band_hz=list(VALIDATION_BAND),
        validation_band_hz=list(VALIDATION_BAND),
    )
    training, validation, method = validation_plan(stations)
    if method is None:
        return dict(result, reasons=["too_few_validation_stations"]), None
    result["validation_method"] = method
    result["validation_stations"] = [stations[i]["id"] for i in validation]
    trials = {profile[0]: {} for profile in SEARCH_PROFILES}
    for offset, location, sites, banks in nodes:
        for profile in SEARCH_PROFILES:
            fitted, modeled = fit_profile(
                data, banks, sites, training, depths, delays, DT, profile, mask
            )
            if modeled is not None:
                fitted.update(offset_km=list(offset), centroid_location=location)
                if fitted["azimuth_gap_deg"] <= LIMITS["gap"]:
                    trials[profile[0]][offset] = fitted, modeled

    predictions = {}
    for profile, by_offset in trials.items():
        if (0, 0) not in by_offset:
            continue
        fixed, fixed_prediction = by_offset[(0, 0)]
        moved = horizontal_solution([row for row, _ in by_offset.values()], outer_edge_km=OUTER_KM)
        offset = tuple(moved["offset_km"])
        moved_prediction = by_offset[offset][1]
        if len(by_offset) < 5 or (
            offset != (0, 0)
            and tuple(2 * value for value in offset) not in by_offset
            and "horizontal_grid_edge" not in moved["reasons"]
        ):
            moved["reasons"].append("horizontal_grid_coverage")
            moved["status"] = "unconstrained"
        for tag, candidate, modeled in (
            ("fixed", fixed, fixed_prediction),
            ("grid", moved, moved_prediction),
        ):
            candidate["id"] = f"{tag}-{profile}"
            candidate["strategy"] = tag
            candidate["centroid_location_fitted"] = tag == "grid"
            validate_profile(candidate, data, modeled, stations, validation, mask)
            result["candidates"].append(candidate)
            predictions[candidate["id"]] = modeled

    if not result["candidates"]:
        return dict(result, reasons=["no_supported_horizontal_grid"]), None
    solutions = competing_solutions(
        [row for row in result["candidates"] if row["strategy"] == "grid"]
    )
    result["solutions"] = solutions
    if not solutions:
        return dict(result, status="unconstrained", reasons=["no_validated_centroid"]), None
    if len(solutions) > 1:
        return dict(result, status="ambiguous", reasons=["competing_mechanisms"]), None
    selected = solutions[0]
    result.update(selected)
    result.update(method=VERSION, selected_candidate=selected["id"])
    return result, predictions[selected["id"]]

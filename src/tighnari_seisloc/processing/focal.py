"""Experimental P first-motion double-couple inversion, independent of old branch.

NED coordinates; positive radiation is compression. No Mw or moment-tensor
ISO/CLVD fractions can be inferred from polarity signs. All solutions are research
candidates, with coverage and near-optimal tensor spread explicitly reported.
"""

from collections import Counter
from functools import lru_cache
from pathlib import Path
import numpy as np

FOCAL_VERSION = "first-motion-v10"
LOCATION_FIELDS = ("origin", "lat", "lon", "depth_km")
STABILITY_ANGLE_DEG = 35
TAIWAN_MODEL = Path(__file__).resolve().parents[1] / "assets/taiwan_wu2007.npz"
# Event-relative P residual limit. The common part of the residual reflects the
# origin time and regional velocity bias, not the station's ray geometry.
RESIDUAL_LIMIT_S = 1.0
# Independent first-motion check of waveform moment tensors.
POLARITY_CHECK_MIN = 8
POLARITY_CONSISTENT = 0.15
POLARITY_CONFLICT = 0.25


def ray_model(event):
    """Return the TauP model name and instance used for first-motion rays.

    Taiwan events use the Wu et al. (2007) 1-D model. Its shallow velocities
    differ enough from iasp91 to move regional rays between up- and down-going
    branches, which changes where they plot on the focal sphere.
    """
    from obspy.taup import TauPyModel

    if event.get("region", "tw") == "tw":
        return "Wu2007-table1-1D", TauPyModel(str(TAIWAN_MODEL))
    return "iasp91", TauPyModel("iasp91")


def depth_unconstrained(event):
    """True only when the locator reported that the hypocentral depth is not resolved."""
    return event.get("depth_status") in ("poorly-constrained", "boundary")


def at_depth(observations, event, depth_km):
    """Recompute P azimuths and take-off angles from the epicentre at another depth.

    Used when the hypocentral depth is unconstrained (for example offshore events
    pinned to the surface), where take-off angles at the located depth put most
    rays on the wrong part of the focal sphere.
    """
    from obspy.geodetics import gps2dist_azimuth, kilometers2degrees

    _, model = ray_model(event)
    rows = []
    for row in observations:
        if row.get("polarity") not in (-1, 1) or "lat" not in row or "lon" not in row:
            continue
        meters, azimuth, _ = gps2dist_azimuth(event["lat"], event["lon"], row["lat"], row["lon"])
        arrivals = model.get_travel_times(
            max(0.001, float(depth_km)), kilometers2degrees(meters / 1000), phase_list=["p", "P"]
        )
        if arrivals:
            first = min(arrivals, key=lambda arrival: arrival.time)
            rows.append(dict(row, azimuth=azimuth, takeoff=float(first.takeoff_angle)))
    return rows


# Display grades after HASH (Hardebeck & Shearer 2002): near-optimal spread stands in
# for the fault-plane uncertainty. Grades describe the solution; publication still
# requires the full gate (stations, gap, take-off range, misfit, spread, stability).
GRADES = (
    ("A", dict(spread=25, misfit=0.10, gap=90)),
    ("B", dict(spread=35, misfit=0.15, gap=160)),
    ("C", dict(spread=45, misfit=0.25, gap=220)),
)


def quality_grade(result):
    if "tensor_spread_deg" not in result:
        return None
    for grade, limit in GRADES:
        if (
            result["tensor_spread_deg"] <= limit["spread"]
            and result["misfit"] <= limit["misfit"]
            and result["azimuth_gap_deg"] <= limit["gap"]
        ):
            return grade
    return "D"


def gdms_mechanism_check(event, attempted):
    """Return the reason to preserve a qualified mechanism at the same hypocenter."""
    from .cmt import kagan

    previous = event.get("focal_mechanism") or {}
    location = {key: event.get(key) for key in LOCATION_FIELDS}
    if previous.get("status") != "experimental" or previous.get("location") != location:
        return None
    if attempted.get("location", location) != location:
        return None
    if attempted["status"] != "experimental":
        return dict(reason="new_mechanism_unqualified")
    if previous.get("tensor_ned") is None or attempted.get("tensor_ned") is None:
        return None
    angle = kagan(np.asarray(previous["tensor_ned"]), np.asarray(attempted["tensor_ned"]))
    if angle is None or angle > STABILITY_ANGLE_DEG:
        return dict(reason="mechanism_disagrees", kagan_deg=angle)
    return None


def tensor(strike, dip, rake):
    s, d, r = np.deg2rad([strike, dip, rake])
    along = np.array([np.cos(s), np.sin(s), 0.0])
    down = np.array([-np.sin(s) * np.cos(d), np.cos(s) * np.cos(d), np.sin(d)])
    normal = np.cross(down, along)
    slip = np.cos(r) * along - np.sin(r) * down
    return np.outer(slip, normal) + np.outer(normal, slip)


@lru_cache(maxsize=1)
def grid():
    planes = np.array(
        [
            (s, d, r)
            for s in range(0, 360, 10)
            for d in range(10, 91, 10)
            for r in range(-180, 180, 10)
        ]
    )
    return planes, np.array([tensor(*p) for p in planes])


def representative_best(loss, planes, tensors):
    """Select the best-fit DC nearest the orientation-weighted mean of tied DCs.

    Plane normals on a strike/dip grid occupy solid angle proportional to
    sin(dip). The chordal mean is used only to rank actual grid mechanisms.
    """
    tied = np.flatnonzero(loss == loss.min())
    weights = np.sin(np.deg2rad(planes[tied, 1]))
    mean = np.average(tensors[tied], axis=0, weights=weights)
    scores = np.einsum("kij,ij->k", tensors[tied], mean)
    return int(tied[np.argmax(scores)])


def polarity_losses(observations):
    azimuth = np.deg2rad([r["azimuth"] for r in observations])
    theta = np.deg2rad([r["takeoff"] for r in observations])
    rays = np.column_stack(
        [np.sin(theta) * np.cos(azimuth), np.sin(theta) * np.sin(azimuth), np.cos(theta)]
    )
    signs = np.array([r["polarity"] for r in observations])
    _, tensors = grid()
    amplitude = np.einsum("ni,kij,nj->kn", rays, tensors, rays)
    # Near-nodal observations cannot distinguish the two signs and count as half-errors.
    return np.where(np.abs(amplitude) < 0.05, 0.5, (amplitude * signs < 0).astype(float))


def polarity_agreement(moment, focal, event, depth_km=None):
    """Score a moment tensor against first motions observed at the same hypocenter.

    The P radiation sign of a moment tensor is independent of waveform fitting,
    so first motions test a waveform solution without sharing its data. Near-nodal
    rays count as half errors, as in the first-motion inversion. ``depth_km``
    (the waveform centroid) replaces the located depth when that is unconstrained.
    """
    location = {key: event.get(key) for key in LOCATION_FIELDS}
    if not focal or focal.get("location") != location:
        return None
    try:
        moment = np.asarray(moment, dtype=float)
    except (TypeError, ValueError):
        return None
    scale = np.linalg.norm(moment) / np.sqrt(2) if moment.shape == (3, 3) else 0
    if not np.isfinite(scale) or scale <= 0:
        return None
    observations = [
        row
        for row in focal.get("observations") or []
        if row.get("polarity") in (-1, 1)
        and np.isfinite([row.get("azimuth", np.nan), row.get("takeoff", np.nan)]).all()
    ]
    if depth_km is not None and depth_unconstrained(event):
        observations = at_depth(observations, event, depth_km)
    else:
        depth_km = None
    if len(observations) < POLARITY_CHECK_MIN:
        return dict(status="insufficient_data", count=len(observations))
    azimuth = np.deg2rad([row["azimuth"] for row in observations])
    theta = np.deg2rad([row["takeoff"] for row in observations])
    rays = np.column_stack(
        [np.sin(theta) * np.cos(azimuth), np.sin(theta) * np.sin(azimuth), np.cos(theta)]
    )
    amplitude = np.einsum("ni,ij,nj->n", rays, moment / scale, rays)
    signs = np.array([row["polarity"] for row in observations])
    loss = np.where(np.abs(amplitude) < 0.05, 0.5, (amplitude * signs < 0).astype(float))
    misfit = float(loss.mean())
    status = (
        "consistent"
        if misfit <= POLARITY_CONSISTENT
        else "conflict"
        if misfit > POLARITY_CONFLICT
        else "mixed"
    )
    check = dict(
        status=status,
        count=len(observations),
        misfit=misfit,
        discordant=[row["id"] for row, value in zip(observations, loss) if value == 1],
    )
    if depth_km is not None:
        check["ray_depth_km"] = float(depth_km)
    return check


def leave_one_site(result):
    observations = result["observations"]
    losses = polarity_losses(observations)
    total = losses.sum(axis=1)
    _, tensors = grid()
    cosine = np.einsum("kij,ij->k", tensors, np.asarray(result["tensor_ned"])) / 2
    angles = np.degrees(np.arccos(np.clip(cosine, -1, 1)))
    rows = []
    for i, observation in enumerate(observations):
        training = total - losses[:, i]
        tied = training == training.min()
        rows.append(
            dict(
                id=observation["id"],
                provider=observation.get("provider"),
                worst_tensor_angle_deg=float(angles[tied].max()),
                held_out_misfit=float(losses[tied, i].mean()),
            )
        )
    maximum = max(row["worst_tensor_angle_deg"] for row in rows)
    return dict(
        accepted=maximum <= STABILITY_ANGLE_DEG, max_tensor_angle_deg=maximum, stations=rows
    )


def solve(observations, *, diagnostics=None):
    """Input: independent sites with azimuth, TauP takeoff (down=0), sign +/-1."""
    unique = {}
    groups, anchors = {}, {}
    by_group = {}
    for row in observations:
        if (
            row.get("polarity") in (-1, 1)
            and np.isfinite([row["azimuth"], row["takeoff"]]).all()
            and 0 <= row["takeoff"] <= 180
        ):
            name = ".".join(row["id"].split(".")[:2])
            if name not in groups:
                from ..legacy.location import distance

                groups[name] = name
                if all(k in row for k in ("lat", "lon")):
                    for key, anchor in anchors.items():
                        if (
                            all(k in anchor for k in ("lat", "lon"))
                            and distance(row["lat"], row["lon"], anchor["lat"], anchor["lon"]) < 2
                        ):
                            groups[name] = key
                            break
                anchors.setdefault(groups[name], row)
            site = groups[name]
            if diagnostics is not None:
                by_group.setdefault(site, []).append(diagnostics[id(row)])
            if site in unique and (
                unique[site] is None or unique[site]["polarity"] != row["polarity"]
            ):
                unique[site] = None
            elif site not in unique:
                unique[site] = row
    if diagnostics is not None:
        for site, chosen in unique.items():
            if chosen is None:
                for entry in by_group[site]:
                    entry.update(
                        disposition="conflicting_polarities",
                        reasons=["same_site_polarity_conflict"],
                    )
            else:
                diagnostics[id(chosen)]["disposition"] = "used"
    obs = [r for r in unique.values() if r is not None]
    result = dict(
        status="insufficient_data",
        method="P-polarity-DC-grid-v2",
        station_count=len(obs),
        observations=obs,
        reasons=[],
        experimental=True,
    )
    if len(obs) < 10:
        result["reasons"] = ["有效獨立初動站數不足 10 站"]
        return result
    az = np.sort([r["azimuth"] % 360 for r in obs])
    gap = float(np.diff(np.r_[az, az[0] + 360]).max())
    takeoff = np.array([r["takeoff"] for r in obs])
    planes, tensors = grid()
    losses = polarity_losses(obs)
    misfit = losses.mean(axis=1)
    best = representative_best(misfit, planes, tensors)
    alternatives = tensors[misfit <= misfit[best] + 1 / len(obs)]
    # DC nodal-plane symmetries share a tensor. A sign-reversed tensor is distinct.
    cosine = np.einsum("kij,ij->k", alternatives, tensors[best]) / 2
    spread = float(np.rad2deg(np.arccos(np.clip(cosine, -1, 1))).max())
    reasons = []
    if gap > 160:
        reasons.append("測站方位缺口大於 160°")
    if np.ptp(takeoff) < 25:
        reasons.append("出射角覆蓋不足 25°")
    if misfit[best] > 0.15:
        reasons.append("初動不符比例高於 15%")
    if spread > 35:
        reasons.append("近最佳解存在明顯多解")
    s, d, r = planes[best]
    from obspy.imaging.beachball import aux_plane

    second = aux_plane(float(s), float(d), float(r))
    nodal_planes = [
        dict(strike=float(s), dip=float(d), rake=float(r)),
        dict(zip(("strike", "dip", "rake"), map(float, second))),
    ]
    result.update(
        status="unconstrained" if reasons else "experimental",
        reasons=reasons,
        azimuth_gap_deg=gap,
        takeoff_range_deg=float(np.ptp(takeoff)),
        misfit=float(misfit[best]),
        tensor_spread_deg=spread,
        plane=dict(strike=int(s), dip=int(d), rake=int(r)),
        nodal_planes=nodal_planes,
        tensor_ned=tensors[best].tolist(),
        grid_step_deg=10,
    )
    result["quality_grade"] = quality_grade(result)
    return result


def first_motions(records, event, *, refine_onsets=False, stability=False, validate_joint=False):
    """Conservative automatic signs. Reject uncertain onsets rather than use peak signs."""
    from obspy.geodetics import gps2dist_azimuth, kilometers2degrees

    model_name, model = ray_model(event)
    observations, diagnostics = [], []
    sites = {}
    for record in records:
        sites.setdefault(tuple(record.id.split(".")[:2]), []).append(record)

    @lru_cache(maxsize=None)
    def geometry(lat, lon, include_s):
        meters, az, _ = gps2dist_azimuth(event["lat"], event["lon"], lat, lon)
        arrivals = model.get_travel_times(
            max(0.001, event["depth_km"]),
            kilometers2degrees(meters / 1000),
            phase_list=["p", "P", "s", "S"] if include_s else ["p", "P"],
        )
        p = next((arrival for arrival in arrivals if arrival.name.lower() == "p"), None)
        s = next((arrival for arrival in arrivals if arrival.name.lower() == "s"), None)
        return float(az), p, s

    pairs, picked_sites, used_s_times = [], set(), {}
    for pick in event.get("residuals", []):
        if refine_onsets and pick.get("used") and pick.get("phase") == "S":
            sid = pick.get("source_id") or pick["id"]
            site = tuple(sid.split(".")[:2])
            used_s_times[site] = min(used_s_times.get(site, float("inf")), pick["time"])
        if (
            not pick.get("used")
            or pick.get("phase") != "P"
            or pick.get("method", "").startswith("adaptive")
        ):
            continue
        sid = pick.get("source_id") or pick["id"]
        site = tuple(sid.split(".")[:2])
        picked_sites.add(site)
        matched = sites.get(site, [])
        pairs.extend((record, pick, sid) for record in matched)
        if refine_onsets and not matched:
            diagnostics.append(
                dict(
                    id=sid,
                    original_time=pick["time"],
                    accepted=False,
                    disposition="no_waveform",
                    reasons=["no_waveform"],
                )
            )
    if refine_onsets:
        pairs.extend(
            (record, None, None)
            for site, rows in sites.items()
            if site not in picked_sites
            for record in rows
            if record.provider == "gdms"
        )

    diagnostic_by_observation = {}
    for record, p, sid in pairs:
        is_gdms = record.provider == "gdms"
        motion = dict(
            id=record.id,
            provider=record.provider,
            timing_source_id=sid,
            original_time=p["time"] if p else None,
            reasons=[],
            accepted=False,
        )
        if refine_onsets:
            diagnostics.append(motion)
        problem = None
        if record.clipped and record.metadata.get("vertical_clipped", True):
            problem = "clipped"
        elif record.metadata.get("response_corrected") is not True:
            problem = "response_uncorrected"
        elif record.metadata.get("vertical_orientation_verified") is not True:
            problem = "vertical_orientation_unverified"
        if problem:
            motion.update(disposition="waveform_quality", reasons=[problem])
            continue
        if refine_onsets:
            from .polarity import refine

            if p:
                motion.update(refine(record, p))
            if is_gdms:
                from .gdms_polarity import measure

                az, arrival, s_arrival = geometry(record.lat, record.lon, True)
                if arrival is None:
                    motion.update(
                        disposition="geometry_rejected",
                        accepted=False,
                        reasons=["no_p_travel_time"],
                    )
                    continue
                predicted = event["origin"] + arrival.time
                motion.update(predicted_time=predicted, takeoff=float(arrival.takeoff_angle))
                center = p["time"] if p else predicted
                site = tuple(record.id.split(".")[:2])
                s_time = used_s_times.get(site)
                if s_time is None and s_arrival:
                    s_time = event["origin"] + s_arrival.time
                latest = s_time - 0.25 if s_time is not None else None
                evidence = measure(record, center, 0.45 if p else 1.0, latest=latest)
                motion["probabilistic"] = evidence
                if not motion["accepted"]:
                    if "baseline_quality" in motion and any(
                        "ramp" in reason or "step" in reason for reason in motion["reasons"]
                    ):
                        motion["disposition"] = "waveform_quality"
                        continue
                    motion["primary_reasons"] = motion["reasons"]
                    motion.update(
                        {
                            key: evidence[key]
                            for key in (
                                "accepted",
                                "method",
                                "reasons",
                                "polarity",
                                "refined_time",
                                "snr",
                            )
                            if key in evidence
                        }
                    )
                elif evidence["accepted"] and evidence["polarity"] != motion["polarity"]:
                    motion.update(accepted=False, reasons=["polarity_methods_disagree"])
            if not motion["accepted"]:
                motion["disposition"] = "measurement_rejected"
                continue
            sign, motion_time = motion["polarity"], motion["refined_time"]
            polarity_snr = motion["snr"]
        else:
            if p.get("sigma_s", 1) > 0.12:
                continue
            motion_time = p["time"]
            i = round((motion_time - record.start) * record.rate)
            z = record.velocity[:, 0]
            n = round(record.rate)
            if i < 2 * n or i + n >= len(z):
                continue
            noise = z[i - 2 * n : i - n]
            onset = z[i - round(0.12 * n) : i + round(0.25 * n)]
            if not np.isfinite(noise).all() or not np.isfinite(onset).all():
                continue
            baseline = float(np.median(noise))
            sigma = max(float(np.std(noise)), 1e-15)
            # Earliest sustained departure, checked against onset timing uncertainty.
            departures = np.flatnonzero(np.abs(onset - baseline) > 6 * sigma)
            if not len(departures):
                continue
            j = int(departures[0])
            width = max(3, round(0.08 * record.rate))
            pulse = onset[j : j + width] - baseline
            sign = int(np.sign(pulse[0]))
            if len(pulse) != width or np.mean(pulse * sign > 3 * sigma) < 0.8:
                continue
            polarity_snr = float(np.max(np.abs(pulse)) / sigma)
        az, arrival, _ = geometry(record.lat, record.lon, refine_onsets and is_gdms)
        if arrival is None:
            motion.update(disposition="geometry_rejected", reasons=["no_p_travel_time"])
            continue
        predicted = event["origin"] + arrival.time
        residual = motion_time - predicted
        motion.update(
            predicted_time=predicted,
            residual_s=residual,
            azimuth=az,
            takeoff=float(arrival.takeoff_angle),
        )
        observation = dict(
            id=record.id,
            timing_source_id=sid,
            polarity=sign,
            azimuth=az,
            takeoff=float(arrival.takeoff_angle),
            time=motion_time,
            lat=record.lat,
            lon=record.lon,
            elevation_m=record.elevation_m,
            provider=record.provider,
            method=motion["method"] if refine_onsets else "automatic-unreviewed",
            snr=polarity_snr,
            residual_s=residual,
        )
        if refine_onsets:
            motion["disposition"] = "not_independent"
            diagnostic_by_observation[id(observation)] = motion
        observations.append(observation)
    observations, residual_offset = residual_screen(
        observations, diagnostic_by_observation if refine_onsets else None
    )
    result = solve(observations, diagnostics=diagnostic_by_observation if refine_onsets else None)
    if refine_onsets:
        from .regional_focal import regional_comparison

        s_picks, by_id = {}, {}
        for record in records:
            by_id.setdefault(record.id, []).append(record)
        for pick in event.get("residuals", []):
            if (
                pick.get("used")
                and pick.get("phase") == "S"
                and not pick.get("method", "").startswith("adaptive")
            ):
                sid = pick.get("source_id") or pick["id"]
                s_picks.setdefault(tuple(sid.split(".")[:2]), []).append(pick["time"])
        result["regional_comparison"] = regional_comparison(
            result["observations"], event, s_picks, by_id, validate_joint=validate_joint
        )
    if event.get("depth_status") != "constrained":
        if result["status"] == "experimental":
            result["status"] = "unconstrained"
        result["reasons"].append("定位深度尚未約束")
    if refine_onsets:
        gdms = [row for row in diagnostics if row.get("provider") == "gdms"]
        if gdms:
            result["gdms_diagnostics"] = dict(
                records=len(gdms),
                stations=len({tuple(row["id"].split(".")[:2]) for row in gdms}),
                dispositions=dict(Counter(row["disposition"] for row in gdms)),
                repick_records=sum(row["original_time"] is None for row in gdms),
                repick_timing_records=sum(
                    row["original_time"] is None
                    and row.get("probabilistic", {}).get("timing_accepted", False)
                    for row in gdms
                ),
                used_repick_stations=sum(
                    row["disposition"] == "used" and row["original_time"] is None for row in gdms
                ),
            )
        result["refinement_diagnostics"] = diagnostics
        result["uncertainty_calibrated"] = False
    if stability and result["status"] == "experimental":
        result["stability"] = stability_test(result, event, model)
        if not result["stability"]["accepted"]:
            result["status"] = "unconstrained"
            result["reasons"].append("定位擾動或站點重抽樣未通過")
    if stability and "tensor_ned" in result:
        result["leave_one_site"] = leave_one_site(result)
        if result["status"] == "experimental" and not result["leave_one_site"]["accepted"]:
            result["status"] = "unconstrained"
            result["reasons"].append("省略單站後機制改變超過 35°")
    result["location"] = {k: event[k] for k in LOCATION_FIELDS}
    result["ray_model"] = model_name
    result["residual_offset_s"] = residual_offset
    result["processing_version"] = FOCAL_VERSION
    result["location_diagnostics"] = {
        k: event.get(k) for k in ("depth_status", "horizontal_sigma_km", "depth_range_km")
    }
    return result


def residual_screen(observations, diagnostics=None):
    """Reject P times inconsistent with the event's other first-motion times.

    One observation per physical site enters the median, so a dense array or
    several instruments at one site cannot set the reference on their own.
    """
    sites = {}
    for row in observations:
        sites.setdefault(tuple(row["id"].split(".")[:2]), row["residual_s"])
    offset = float(np.median(list(sites.values()))) if len(sites) >= 3 else 0.0
    kept = []
    for row in observations:
        if abs(row["residual_s"] - offset) > RESIDUAL_LIMIT_S:
            if diagnostics is not None:
                diagnostics[id(row)].update(
                    disposition="geometry_rejected",
                    reasons=["p_residual_exceeds_one_second"],
                    relative_residual_s=row["residual_s"] - offset,
                )
            continue
        kept.append(row)
    return kept, offset


def stability_test(result, event, model, *, trials=100):
    """Conditional site bootstrap/location perturbation; not HASH or Kagan angles."""
    from obspy.geodetics import gps2dist_azimuth, kilometers2degrees

    horizontal = event.get("horizontal_sigma_km")
    depth_range = event.get("depth_range_km")
    if (
        horizontal is None
        or not np.isfinite(horizontal)
        or horizontal <= 0
        or not depth_range
        or len(depth_range) != 2
        or not np.isfinite(depth_range).all()
        or depth_range[0] <= 0
        or depth_range[1] >= 350
        or depth_range[0] > depth_range[1]
    ):
        return dict(accepted=False, reason="location_diagnostics_unavailable", trials=trials)
    observations = result["observations"]
    if any("lat" not in row or "lon" not in row for row in observations):
        return dict(accepted=False, reason="station_coordinates_unavailable", trials=trials)
    rng = np.random.default_rng(20261121)
    base = np.asarray(result["tensor_ned"])
    differences = []
    _, tensors = grid()
    for _ in range(trials):
        lat = event["lat"] + rng.normal(0, horizontal) / 111.2
        lon = event["lon"] + rng.normal(0, horizontal) / (111.2 * max(0.1, np.cos(np.deg2rad(lat))))
        depth = rng.uniform(*depth_range)
        sample = []
        # Repeated sites do not count as extra independent constraints.
        for index in sorted(set(rng.integers(0, len(observations), len(observations)))):
            row = observations[index]
            meters, az, _ = gps2dist_azimuth(lat, lon, row["lat"], row["lon"])
            arrivals = model.get_travel_times(
                depth, kilometers2degrees(meters / 1000), phase_list=["p", "P"]
            )
            if arrivals:
                sample.append(
                    dict(row, azimuth=az, takeoff=min(arrivals, key=lambda a: a.time).takeoff_angle)
                )
        if len(sample) < 10:
            continue
        loss = polarity_losses(sample).mean(axis=1)
        if loss.min() > 0.15:
            continue
        # All tied minima contribute their worst orientation, not an arbitrary first.
        tied = tensors[loss <= loss.min() + 1e-12]
        cosines = np.einsum("kij,ij->k", tied, base) / 2
        differences.append(float(np.rad2deg(np.arccos(np.clip(cosines, -1, 1))).max()))
    angle95 = float(np.quantile(differences, 0.95)) if differences else None
    return dict(
        accepted=(
            len(differences) >= trials * 0.9
            and angle95 is not None
            and angle95 <= STABILITY_ANGLE_DEG
        ),
        trials=trials,
        solved=len(differences),
        tensor_angle_95_deg=angle95,
        method="site-bootstrap-conditional-location-perturbation-v1",
        calibrated=False,
    )

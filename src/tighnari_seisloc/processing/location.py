"""Independent P/S association, robust inversion, and empirical stability diagnostics."""

import numpy as np
from scipy.optimize import least_squares
from ..legacy.location import distance
from ..location_policy import BOUNDS, SUPPORT_DISTANCE_KM
from .event_identity import same_event, arrival_overlap

NEAR_DISTANCE_KM = 300.0
SUPPORT_WEIGHT = 0.5
SUPPORT_POLICY_VERSION = "near-first-300-support-600-v1"
ASSOCIATION_VERSION = "confidence-hypotheses-v2"
TRUSTED_SCORE = 0.3


def pick_confidence(pick):
    """A bounded evidence weight, not a calibrated arrival probability.

    Older/manual observations without scores retain their original weight.
    SNR is deliberately excluded: the picker measures it on Z even for S.
    """
    score = float(pick.get("score", 1.0))
    return float(np.clip(score, 0.05, 1.0)) if np.isfinite(score) else 0.05


def independent(picks):
    result, sites = [], []
    for p in sorted(picks, key=lambda x: -x.get("score", 1)):
        # Assign one physical-site identity across both phases and all providers.
        # A vertical-only P and another colocated instrument's S are still one site.
        site = next(
            (q for q in sites if distance(p["lat"], p["lon"], q["lat"], q["lon"]) <= 2),
            None,
        )
        if site is None:
            sites.append(p)
            site = p
        row = dict(p)
        if row["id"] != site["id"]:
            row["source_id"] = row["id"]
            row["id"] = site["id"]
        if not any(
            row["id"] == q["id"] and row["phase"] == q["phase"] and abs(row["time"] - q["time"]) < 1
            for q in result
        ):
            result.append(row)
    return result


def invert(picks, model, seed=None, diagnose=False):
    if len(picks) < 4 or len({p["id"] for p in picks}) < 3:
        return None
    lats, lons = np.array([p["lat"] for p in picks]), np.array([p["lon"] for p in picks])
    anchor = min(p["time"] for p in picks)
    times = np.array([p["time"] - anchor for p in picks])
    elev = np.array([p.get("elevation_m", 0) for p in picks])
    phases = np.array([p["phase"] for p in picks])
    sigmas = np.array(
        [max(0.3 if p["phase"] == "P" else 0.6, min(1.0, p.get("sigma_s", 0.3))) for p in picks]
    )

    # Score-squared evidence and auxiliary half weight do not relax outlier limits.
    fit_sigmas = sigmas / np.sqrt(
        np.array([p.get("location_weight", 1.0) * pick_confidence(p) ** 2 for p in picks])
    )

    def theory(x):
        d = distance(x[0], x[1], lats, lons)
        out = np.zeros(len(picks))
        for phase in ("P", "S"):
            mask = phases == phase
            out[mask] = model.times(x[2], d[mask], phase, elev[mask])
        return out

    def residual(x):
        return np.nan_to_num(x[3] + theory(x) - times, nan=1e4)

    seeds = []
    if seed is not None:
        seeds = [np.array([seed["lat"], seed["lon"], seed["depth_km"], seed["origin"] - anchor])]
    else:
        # Entire target region is searched independently of any catalog location.
        la = np.arange(max(20, lats.min() - 0.8), min(27, lats.max() + 0.8) + 0.01, 0.25)
        lo = np.arange(max(117, lons.min() - 0.8), min(125, lons.max() + 0.8) + 0.01, 0.25)
        depths = np.array([1, 5, 10, 20, 35, 60, 100, 180, 300])
        mesh = np.array(np.meshgrid(la, lo, depths, indexing="ij")).reshape(3, -1).T
        dist = distance(mesh[:, 0, None], mesh[:, 1, None], lats[None, :], lons[None, :])
        tt = np.zeros(dist.shape)
        for phase in ("P", "S"):
            mask = phases == phase
            tt[:, mask] = model.times(mesh[:, 2, None], dist[:, mask], phase, elev[None, mask])
        origins = np.median(times[None, :] - tt, axis=1)
        r = (origins[:, None] + tt - times[None, :]) / fit_sigmas[None, :]
        scores = np.mean(np.minimum(r * r, 25), axis=1)
        for i in np.argsort(np.nan_to_num(scores, nan=np.inf))[:80]:
            if not -400 < origins[i] < 10:
                continue
            if all(np.linalg.norm((mesh[i] - s[:3]) * [111, 100, 1]) > 20 for s in seeds):
                seeds.append(np.r_[mesh[i], origins[i]])
            if len(seeds) >= 8:
                break
    fits = []
    for s in seeds:
        try:
            f = least_squares(
                lambda x: residual(x) / fit_sigmas,
                np.clip(s, np.array(BOUNDS[0]) + 1e-5, np.array(BOUNDS[1]) - 1e-5),
                bounds=BOUNDS,
                loss="soft_l1",
                f_scale=1,
                max_nfev=180,
                diff_step=1e-4,
            )
            fits.append(f)
        except ValueError:
            continue
    if not fits:
        return None
    fit = min(fits, key=lambda f: np.sum(np.minimum((residual(f.x) / fit_sigmas) ** 2, 16)))
    mask = np.abs(residual(fit.x)) < np.maximum(1.5, 3 * sigmas)
    if mask.sum() < 4 or len({p["id"] for p, m in zip(picks, mask) if m}) < 3:
        return None
    for _ in range(2):
        fit = least_squares(
            lambda x: residual(x)[mask] / fit_sigmas[mask],
            fit.x,
            bounds=BOUNDS,
            loss="soft_l1",
            max_nfev=150,
            diff_step=1e-4,
        )
        mask = np.abs(residual(fit.x)) < np.maximum(1.5, 3 * sigmas)
    x = fit.x
    used_picks = [p for p, m in zip(picks, mask) if m]
    used = sorted({p["id"] for p in used_picks})
    az = np.sort(
        np.unique(
            np.degrees(
                np.arctan2((lons[mask] - x[1]) * np.cos(np.radians(x[0])), lats[mask] - x[0])
            )
            % 360
        )
    )
    gap = float(np.max(np.diff(np.r_[az, az[0] + 360])))
    pcount = sum(p["phase"] == "P" for p in used_picks)
    scount = sum(p["phase"] == "S" for p in used_picks)
    rms = float(np.sqrt(np.mean(residual(x)[mask] ** 2)))
    published = (
        len(used) >= 4
        and pcount >= 3
        and len(used_picks) >= 6
        and (scount > 0 or len(used) >= 5)
        and rms <= 1.5
    )
    cov = np.linalg.pinv(fit.jac.T @ fit.jac) * max(0.04, rms * rms)
    e = dict(
        lat=float(x[0]),
        lon=float(x[1]),
        depth_km=float(x[2]),
        origin=float(anchor + x[3]),
        rms_s=rms,
        gap_deg=gap,
        count=len(used),
        used=used,
        phase_count=len(used_picks),
        p_count=pcount,
        s_count=scount,
        degrees_of_freedom=max(0, len(used_picks) - 4),
        horizontal_sigma_km=float(np.sqrt(max(0, cov[0, 0] + cov[1, 1])) * 111),
        depth_sigma_km=float(np.sqrt(max(0, cov[2, 2]))),
        travel_model=model.name,
        status="located" if published else "candidate",
        revision=1,
        quality="自動定位" if published else "少站候選",
        quality_grade="C" if published else "D",
        quality_version="2",
        association_version=ASSOCIATION_VERSION,
        quality_reasons=[],
        updated=float(max(p["time"] for p in picks)),
        residuals=[
            dict(
                **p,
                observed=p["time"],
                predicted=float(anchor + x[3] + theory(x)[i]),
                residual_s=float(residual(x)[i]),
                used=bool(mask[i]),
            )
            for i, p in enumerate(picks)
        ],
    )
    if diagnose:
        diagnostics(e, used_picks, model)
    return e


def diagnostics(e, picks, model):
    from scipy.optimize import least_squares

    e["quality_grade"] = "C" if e["status"] == "located" else "D"
    e["quality_version"] = "2"
    # Profile likelihood: other parameters are refitted at each fixed depth.
    depths = sorted(
        set(
            [
                0,
                1,
                3,
                5,
                8,
                10,
                15,
                20,
                25,
                30,
                35,
                50,
                75,
                100,
                150,
                250,
                350,
                round(e["depth_km"], 2),
            ]
        )
    )
    lat = np.array([p["lat"] for p in picks])
    lon = np.array([p["lon"] for p in picks])
    t = np.array([p["time"] - e["origin"] for p in picks])
    phase = np.array([p["phase"] for p in picks])

    def r(x, z, indices=None):
        d = distance(x[0], x[1], lat, lon)
        pred = np.empty(len(picks))
        for ph in ("P", "S"):
            m = phase == ph
            pred[m] = model.times(z, d[m], ph)
        val = pred + x[2] - t
        return val if indices is None else val[indices]

    profile = []
    for z in depths:
        f = least_squares(
            lambda x: r(x, z),
            [e["lat"], e["lon"], 0],
            bounds=([20, 117, -30], [27, 125, 30]),
            loss="soft_l1",
            max_nfev=70,
            diff_step=1e-4,
        )
        profile.append(dict(depth_km=z, rms_s=float(np.sqrt(np.mean(r(f.x, z) ** 2)))))
    best = min(v["rms_s"] for v in profile)
    plausible = [v["depth_km"] for v in profile if v["rms_s"] <= best + 0.3]
    bounded = min(plausible) > 0 and max(plausible) < 350 and max(plausible) - min(plausible) <= 40
    e["depth_profile"] = profile
    e["depth_range_km"] = [min(plausible), max(plausible)]
    e["depth_status"] = "constrained" if bounded else "poorly-constrained"
    if e["depth_km"] < 1 or e["depth_km"] > 349:
        e["depth_status"] = "boundary"
    rng = np.random.default_rng(20261107)
    boot = []
    ids = sorted({p["id"] for p in picks})
    for _ in range(24):
        sampled = rng.choice(ids, len(ids), replace=True)
        idx = [i for sid in sampled for i, p in enumerate(picks) if p["id"] == sid]
        if len(set(sampled)) < 3:
            continue

        def rb(x):
            return r([x[0], x[1], x[3]], x[2], idx)

        f = least_squares(
            rb,
            [e["lat"], e["lon"], e["depth_km"], 0],
            bounds=([20, 117, 0, -30], [27, 125, 350, 30]),
            loss="soft_l1",
            max_nfev=100,
            diff_step=1e-4,
        )
        boot.append(dict(lat=float(f.x[0]), lon=float(f.x[1]), depth_km=float(f.x[2])))
    e["bootstrap"] = boot
    spread = (
        float(np.quantile([distance(e["lat"], e["lon"], b["lat"], b["lon"]) for b in boot], 0.9))
        if boot
        else None
    )
    e["horizontal_resampling_p90_km"] = spread
    stable = spread is not None and spread <= 30
    reliable = [p for p in picks if pick_confidence(p) >= TRUSTED_SCORE]
    reliable_sites = len({p["id"] for p in reliable})
    reliable_phases = len(reliable)
    reliable_p = sum(p["phase"] == "P" for p in reliable)
    reliable_s = sum(p["phase"] == "S" for p in reliable)
    e["reliable_station_count"] = reliable_sites
    e["reliable_phase_count"] = reliable_phases
    # A/B require enough reliable evidence; extra weak picks cannot promote C.
    reliable_location = reliable_phases >= 6 and reliable_p >= 3
    if (
        e["status"] == "located"
        and reliable_sites >= 5
        and reliable_location
        and e["gap_deg"] <= 240
        and e["rms_s"] <= 1.2
        and stable
    ):
        e["quality_grade"] = "B"
    if (
        e["status"] == "located"
        and reliable_sites >= 8
        and reliable_location
        and reliable_s > 0
        and e["gap_deg"] <= 180
        and e["rms_s"] <= 0.8
        and spread is not None
        and spread <= 15
        and bounded
    ):
        e["quality_grade"] = "A"
    reasons = []
    if reliable_sites < 5 or not reliable_location:
        reasons.append("高分震相或獨立站址不足，品質最高為 C")
    if e["count"] < 5:
        reasons.append("獨立站址不足五站")
    if e["gap_deg"] > 180:
        reasons.append("測站方位涵蓋不足")
    if not bounded:
        reasons.append("深度約束不足")
    if not stable:
        reasons.append("測站重抽樣水平解不穩定")
    if e["s_count"] == 0:
        reasons.append("缺少 S 波約束")
    e["quality_reasons"] = reasons


def raw_pick(pick):
    return {
        k: v for k, v in pick.items() if k not in ("observed", "predicted", "residual_s", "used")
    }


def select_arrivals(picks, event, model, max_distance=NEAR_DISTANCE_KM):
    candidates = {}
    for p in picks:
        delta = float(distance(event["lat"], event["lon"], p["lat"], p["lon"]))
        if delta > max_distance:
            continue
        tt = float(model.times(event["depth_km"], delta, p["phase"], p.get("elevation_m", 0)))
        residual = abs(p["time"] - event["origin"] - tt)
        # Arrival compatibility remains mandatory; confidence only breaks the
        # competition between plausible onsets instead of widening the gate.
        merit = residual + 0.6 * -np.log(pick_confidence(p))
        key = (p["id"], p["phase"])
        if np.isfinite(residual) and residual < 3:
            candidates.setdefault(key, []).append((merit, p, tt))
    chosen = {key: min(rows, key=lambda row: row[0]) for key, rows in candidates.items()}
    for key, rows in candidates.items():
        if key[1] != "S" or (key[0], "P") not in chosen:
            continue
        _, pp, tp = chosen[(key[0], "P")]
        consistent = [
            row
            for row in rows
            if row[1]["time"] > pp["time"]
            and abs((row[1]["time"] - pp["time"]) - (row[2] - tp)) < 3
        ]
        if consistent:
            chosen[key] = min(consistent, key=lambda row: row[0])
        else:
            del chosen[key]
    return [raw_pick(v[1]) for v in chosen.values()]


def support_location(near, picks, model):
    """A distant observation can neither establish nor rescue a local solution."""
    result = dict(near)
    result["near_solution"] = {
        k: near.get(k)
        for k in (
            "lat",
            "lon",
            "depth_km",
            "origin",
            "status",
            "count",
            "phase_count",
            "p_count",
            "s_count",
            "rms_s",
            "gap_deg",
        )
    }
    result["support_policy"] = dict(
        version=SUPPORT_POLICY_VERSION,
        near_km=NEAR_DISTANCE_KM,
        support_km=SUPPORT_DISTANCE_KM,
        weight=SUPPORT_WEIGHT,
    )
    result["support_decisions"] = []
    # Inspect far observations, even when the local solution remains a candidate.
    candidates = select_arrivals(
        picks, near, model, max_distance=getattr(model, "max_distance", 2000)
    )
    indexed = {(p["id"], p["phase"]): p for p in candidates}
    additions = []
    for p in candidates:
        delta = float(distance(near["lat"], near["lon"], p["lat"], p["lon"]))
        if delta <= NEAR_DISTANCE_KM:
            continue
        reason = None
        if near["status"] != "located":
            reason = "near-solution-not-qualified"
        elif delta > SUPPORT_DISTANCE_KM:
            reason = "beyond-support-distance-corroboration-only"
        elif p.get("score", 0) < 0.25:
            reason = "low-support-phase-score"
        elif p["phase"] == "S":
            pp = indexed.get((p["id"], "P"))
            if pp is None or pp.get("score", 0) < 0.25 or pp["time"] >= p["time"]:
                reason = "support-S-without-matching-P"
            else:
                tp = float(model.times(near["depth_km"], delta, "P", p.get("elevation_m", 0)))
                ts = float(model.times(near["depth_km"], delta, "S", p.get("elevation_m", 0)))
                if abs((p["time"] - pp["time"]) - (ts - tp)) >= 3:
                    reason = "inconsistent-support-SP-interval"
        result["support_decisions"].append(
            dict(
                id=p["id"],
                phase=p["phase"],
                time=p["time"],
                distance_km=delta,
                accepted=reason is None,
                reason=reason or "support-candidate",
            )
        )
        if reason is None:
            additions.append(dict(p, location_weight=SUPPORT_WEIGHT))
    if not additions:
        return result
    local = [raw_pick(p) for p in near["residuals"] if p["used"]]
    refined = invert(local + additions, model, seed=near)
    good_local = (
        []
        if not refined
        else [
            p
            for p in refined["residuals"]
            if p["used"]
            and distance(refined["lat"], refined["lon"], p["lat"], p["lon"]) <= NEAR_DISTANCE_KM
        ]
    )
    retained = bool(
        refined
        and refined["status"] == "located"
        and len(good_local) >= near["phase_count"]
        and len({p["id"] for p in good_local}) >= near["count"]
        and np.sqrt(np.mean([p["residual_s"] ** 2 for p in good_local])) <= 1.5
        and distance(near["lat"], near["lon"], refined["lat"], refined["lon"]) <= 15
        and abs(near["depth_km"] - refined["depth_km"]) <= 20
        and abs(near["origin"] - refined["origin"]) <= 3
    )
    if retained:
        refined.update(
            {k: result[k] for k in ("near_solution", "support_policy", "support_decisions")}
        )
        adopted = {(p["id"], p["phase"]) for p in refined["residuals"] if p["used"]}
        for decision in refined["support_decisions"]:
            if decision["accepted"] and (decision["id"], decision["phase"]) not in adopted:
                decision.update(accepted=False, reason="support-outlier")
        return refined
    for decision in result["support_decisions"]:
        if decision["accepted"]:
            decision.update(accepted=False, reason="support-fit-degraded-near-solution")
    return result


def solution_rank(event):
    """Prefer reliable evidence over a larger collection of weak arrivals."""
    near = event.get("near_solution", event)
    adopted = [p for p in event.get("residuals", []) if p["used"]]
    evidence = sum(pick_confidence(p) ** 2 * p.get("location_weight", 1.0) for p in adopted)
    return (
        event["status"] == "located",
        evidence,
        near["phase_count"] + SUPPORT_WEIGHT * (event["phase_count"] - near["phase_count"]),
        near["count"] + SUPPORT_WEIGHT * (event["count"] - near["count"]),
        -event["rms_s"],
    )


def association_hypotheses(remaining):
    """Search confident and inclusive pools independently, with bounded work.

    The inclusive pool preserves weak-event sensitivity. The confident pool
    prevents an early noise onset from hiding a later, coherent P/S solution.
    Refinement must stay in its pool until hypotheses compete by evidence.
    """
    trusted = [p for p in remaining if pick_confidence(p) >= TRUSTED_SCORE]
    seen = set()
    pools = (trusted, remaining) if len(trusted) != len(remaining) else (remaining,)
    for pool in pools:
        windows = 0
        for seed in pool:
            if seed["phase"] != "P" or pick_confidence(seed) < 0.25:
                continue
            unique = {}
            for p in pool:
                if 0 <= p["time"] - seed["time"] <= 150:
                    unique.setdefault((p["id"], p["phase"]), p)
            regional = [
                p for p in unique.values() if 20 <= p["lat"] <= 27 and 117 <= p["lon"] <= 125
            ]
            if len(regional) < 4 or len({p["id"] for p in regional}) < 3:
                continue
            signature = tuple((p["id"], p["phase"], p["time"]) for p in regional)
            # The same initial window can refine differently in the two pools.
            key = (pool is remaining, signature)
            if key in seen:
                continue
            seen.add(key)
            yield regional, pool
            windows += 1
            if windows >= 60:
                break


def associate(picks, model, diagnose=True):
    picks = independent(picks)
    remaining = sorted(picks, key=lambda p: p["time"])
    events = []
    for regional, pool in association_hypotheses(remaining):
        e = invert(regional, model)
        if e is None:
            continue
        for _ in range(2):
            chosen = select_arrivals(pool, e, model)
            refined = invert(chosen, model, seed=e)
            if refined:
                e = refined
        # Recheck the radius at the final location before admitting any support.
        local = [
            raw_pick(p)
            for p in e["residuals"]
            if p["used"] and distance(e["lat"], e["lon"], p["lat"], p["lon"]) <= NEAR_DISTANCE_KM
        ]
        e = invert(local, model, seed=e)
        if e is None:
            continue
        e = support_location(e, pool, model)
        duplicate = next(
            (old for old in events if same_event(old, e, origin_window_s=15)),
            None,
        )
        if duplicate is not None:
            if solution_rank(e) > solution_rank(duplicate):
                events.remove(duplicate)
                events.append(e)
        else:
            events.append(e)
    # A phase observation cannot simultaneously support different earthquake
    # hypotheses. Resolve competing locations globally by evidence count first.
    accepted = []
    for e in sorted(events, key=solution_rank, reverse=True):
        if any(
            (shared := arrival_overlap(e, old))["count"] >= 2 and shared["fraction"] >= 0.25
            for old in accepted
        ):
            continue
        accepted.append(e)
    events = sorted(accepted, key=lambda e: e["origin"])
    if diagnose:
        for e in events:
            good = [p for p in e["residuals"] if p["used"]]
            diagnostics(e, good, model)
            # Distant geometry must not improve the quality grade or depth confidence.
            if any(p.get("location_weight", 1) < 1 for p in good):
                near = e["near_solution"]
                local = [
                    p
                    for p in good
                    if distance(near["lat"], near["lon"], p["lat"], p["lon"]) <= NEAR_DISTANCE_KM
                ]
                baseline = dict(near, quality_grade="C")
                diagnostics(baseline, local, model)
                ranks = {"A": 3, "B": 2, "C": 1, "D": 0}
                if ranks[e["quality_grade"]] > ranks[baseline["quality_grade"]]:
                    e["quality_grade"] = baseline["quality_grade"]
                e["near_solution"].update(
                    {
                        k: baseline[k]
                        for k in (
                            "quality_grade",
                            "quality_reasons",
                            "depth_status",
                            "depth_range_km",
                        )
                    }
                )
                e["quality_reasons"] = list(
                    dict.fromkeys(e["quality_reasons"] + baseline["quality_reasons"])
                )
                if baseline["depth_status"] != "constrained":
                    e["depth_status"] = baseline["depth_status"]
    return events

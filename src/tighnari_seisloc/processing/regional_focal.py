"""Regional ray and relative S/P amplitude diagnostics at the adopted hypocenter."""

from pathlib import Path
import numpy as np
from scipy.signal import butter, sosfilt


def measure_ratio(record, pt, st, *, backazimuth=None):
    """Use same-instrument velocity peaks, with optional transverse S."""
    rate = record.rate
    if record.clipped or not all(
        record.metadata.get(k)
        for k in ("zne_rotated", "response_corrected", "vertical_orientation_verified")
    ):
        return None
    if st - pt < 4 or rate < 20:
        return None

    def window(start, end):
        lo, hi = (round((t - record.start) * rate) for t in (start, end))
        if lo < 0 or hi > len(record.velocity):
            return None
        return record.velocity[lo:hi]

    block = window(pt - 4, st + 2)
    if block is None or not np.isfinite(block).all():
        return None
    from .polarity import baseline_quality

    for axis in block[round(rate) : round(5.5 * rate)].T:
        if baseline_quality(axis, rate)[0]:
            return None
    filtered = sosfilt(
        butter(3, [1, 6], btype="bandpass", fs=rate, output="sos"), block - block[0], axis=0
    )
    p_trace = np.linalg.norm(filtered, axis=1)
    s_trace = p_trace
    if backazimuth is not None:
        azimuth = np.radians(backazimuth)
        s_trace = abs(filtered[:, 2] * np.cos(azimuth) - filtered[:, 1] * np.sin(azimuth))

    def peak(trace, a, b):
        return float(np.max(trace[round(a * rate) : round(b * rate)]))

    s = st - pt + 4
    p_amp, s_amp = peak(p_trace, 4, 5.5), peak(s_trace, s, s + 1.9)
    if p_amp <= 4 * max(peak(p_trace, 1, 3), 1e-15) or s_amp <= 3 * max(
        peak(s_trace, 1, 3), peak(s_trace, s - 1.5, s - 0.25), 1e-15
    ):
        return None
    return float(np.log10(s_amp / p_amp))


def rays(azimuth, takeoff):
    az, theta = np.deg2rad([azimuth, takeoff])
    return np.column_stack([np.sin(theta) * np.cos(az), np.sin(theta) * np.sin(az), np.cos(theta)])


def regional_comparison(obs, event, s_picks, records, *, validate_joint=False):
    from obspy.taup import TauPyModel
    from obspy.geodetics import gps2dist_azimuth, kilometers2degrees
    from .focal import solve

    model = TauPyModel(str(Path(__file__).resolve().parents[1] / "assets/taiwan_wu2007.npz"))
    from functools import lru_cache

    @lru_cache(maxsize=256)
    def geometry(lat, lon):
        meters, az, backazimuth = gps2dist_azimuth(event["lat"], event["lon"], lat, lon)
        arrivals = model.get_travel_times(
            max(0.001, event["depth_km"]),
            kilometers2degrees(meters / 1000),
            phase_list=["p", "P", "s", "S"],
        )
        return (
            az,
            backazimuth,
            {
                phase: next(
                    (float(a.takeoff_angle) for a in arrivals if a.name.lower() == phase), None
                )
                for phase in ("p", "s")
            },
        )

    selected = []
    for row in obs:
        az, _, angles = geometry(row["lat"], row["lon"])
        if angles["p"] is not None:
            selected.append(dict(row, azimuth=float(az), takeoff=angles["p"]))
    regional = solve(selected)
    rows, sh_rows = [], []
    used_sites = set()
    for pick in event.get("residuals", []):
        if (
            not pick.get("used")
            or pick.get("phase") != "P"
            or pick.get("method", "").startswith("adaptive")
        ):
            continue
        sid = pick.get("source_id") or pick["id"]
        site = tuple(sid.split(".")[:2])
        picks = s_picks.get(site, [])
        if len(picks) != 1 or site in used_sites:
            continue
        for rec in records.get(sid, []):
            value = measure_ratio(rec, pick["time"], picks[0])
            if value is None:
                continue
            if any(gps2dist_azimuth(rec.lat, rec.lon, r["lat"], r["lon"])[0] < 2000 for r in rows):
                continue
            az, backazimuth, angles = geometry(rec.lat, rec.lon)
            if None in angles.values():
                continue
            rows.append(
                dict(
                    id=sid,
                    lat=rec.lat,
                    lon=rec.lon,
                    elevation_m=rec.elevation_m,
                    azimuth=float(az),
                    takeoff=angles["p"],
                    s_takeoff=angles["s"],
                    log_sp=value,
                )
            )
            if validate_joint:
                sh = measure_ratio(rec, pick["time"], picks[0], backazimuth=backazimuth)
                if sh is not None:
                    sh_rows.append(dict(rows[-1], log_sp=sh))
            used_sites.add(site)
            break
    joint = fit_joint(selected, rows, validate=validate_joint)
    result = dict(
        ray_model="Wu2007-table1-1D", fixed_hypocenter=True, polarity=regional, joint=joint
    )
    if validate_joint:
        result["joint_sh"] = fit_joint(selected, sh_rows, validate=True, transverse=True)
    return result


def fit_joint(selected, rows, *, validate=False, transverse=False):
    from .focal import grid, representative_best

    joint = dict(status="insufficient_data", ratio_count=len(rows), observations=rows)
    sparse = validate and 8 <= len(selected) < 10 and len(rows) >= 6
    if (len(selected) >= 10 and len(rows) >= 5) or sparse:
        planes, tensors = grid()

        def traction(rr, angle):
            azimuth = [
                r.get("s_azimuth", r["azimuth"]) if angle == "s_takeoff" else r["azimuth"]
                for r in rr
            ]
            ray = rays(azimuth, [r[angle] for r in rr])
            tr = np.einsum("kij,nj->kni", tensors, ray)
            radial = np.einsum("kni,ni->kn", tr, ray)
            return radial, tr - radial[:, :, None] * ray

        rad, _ = traction(selected, "takeoff")
        signs = [r["polarity"] for r in selected]
        polarity_loss = np.where(abs(rad) < 0.05, 0.5, rad * signs < 0)
        pol = polarity_loss.mean(axis=1)
        pp, _ = traction(rows, "takeoff")
        _, shear = traction(rows, "s_takeoff")
        if transverse:
            azimuth = np.deg2rad([r.get("s_azimuth", r["azimuth"]) for r in rows])
            ss = abs(-shear[:, :, 0] * np.sin(azimuth) + shear[:, :, 1] * np.cos(azimuth))
        else:
            ss = np.linalg.norm(shear, axis=2)
        residual = np.array([r["log_sp"] for r in rows]) - np.log10(
            np.maximum(ss, 0.01) / np.maximum(abs(pp), 0.01)
        )
        # Profile out a common amplitude scale; station corrections remain uncalibrated.
        raw_residual = residual
        residual = residual - np.median(residual, axis=1)[:, None]
        ratio_loss = np.minimum(abs(residual), 1).mean(axis=1)
        loss = pol + ratio_loss
        best = representative_best(loss, planes, tensors)
        influence = {}
        for index, row in enumerate(rows):
            keep = np.arange(len(rows)) != index
            subset = raw_residual[:, keep]
            centered = subset - np.median(subset, axis=1)[:, None]
            score = pol + np.minimum(abs(centered), 1).mean(axis=1)
            other = representative_best(score, planes, tensors)
            cosine = np.sum(tensors[best] * tensors[other]) / 2
            influence[row["id"]] = float(np.rad2deg(np.arccos(np.clip(cosine, -1, 1))))
        joint.update(
            omit_one_ratio_tensor_change_deg=influence,
            status="sparse_research_candidate" if sparse else "research_candidate",
            plane=planes[best].tolist(),
            polarity_misfit=float(pol[best]),
            median_abs_log_residual=float(np.median(abs(residual[best]))),
            ratio_residuals={r["id"]: float(x) for r, x in zip(rows, residual[best])},
        )
        if validate:
            joint["cross_validation"] = validate_joint_fit(
                selected,
                rows,
                planes,
                tensors,
                best,
                polarity_loss,
                raw_residual,
                minimum_polarities=max(7, min(10, len(selected) - 1)),
                minimum_ratios=max(4, min(5, len(rows) - 1)),
            )
    return joint


def validate_joint_fit(
    polarities,
    ratios,
    planes,
    tensors,
    best,
    polarity_loss,
    raw_residual,
    *,
    minimum_polarities,
    minimum_ratios,
):
    """Hold out complete sites or quadrants, including both measurements at a site."""
    from ..legacy.location import distance
    from .cmt import kagan
    from .focal import representative_best

    measurements = [*polarities, *ratios]
    anchors, groups = [], []
    for row in measurements:
        station = tuple(row["id"].split(".")[:2])
        group = next(
            (
                i
                for i, (sid, anchor) in enumerate(anchors)
                if sid == station
                or distance(row["lat"], row["lon"], anchor["lat"], anchor["lon"]) < 2
            ),
            len(anchors),
        )
        if group == len(anchors):
            anchors.append((station, row))
        groups.append(group)
    groups = np.array(groups)
    quadrants = np.array([int(anchor["azimuth"] % 360 // 90) for _, anchor in anchors])
    summary = {}
    for name, assignments in (("site", groups), ("quadrant", quadrants[groups])):
        folds = []
        for group in np.unique(assignments):
            keep = assignments != group
            p_keep, r_keep = np.split(keep, [len(polarities)])
            if p_keep.sum() < minimum_polarities or r_keep.sum() < minimum_ratios:
                continue
            pol = polarity_loss[:, p_keep].mean(axis=1)
            scale = np.median(raw_residual[:, r_keep], axis=1)
            error = abs(raw_residual - scale[:, None])
            baseline = representative_best(pol, planes, tensors)
            joint = representative_best(
                pol + np.minimum(error[:, r_keep], 1).mean(axis=1), planes, tensors
            )
            folds.append(
                dict(
                    withheld=sorted({r["id"] for r, use in zip(measurements, keep) if not use}),
                    kagan_to_full_deg=kagan(tensors[best], tensors[joint]),
                    polarity_count=int((~p_keep).sum()),
                    ratio_count=int((~r_keep).sum()),
                    polarity_errors={
                        key: float(polarity_loss[i, ~p_keep].sum())
                        for key, i in (("polarity_only", baseline), ("joint", joint))
                    },
                    ratio_abs_log_error={
                        key: error[i, ~r_keep].tolist()
                        for key, i in (("polarity_only", baseline), ("joint", joint))
                    },
                )
            )
        polarity_count = sum(f["polarity_count"] for f in folds)
        metrics = {}
        for method in ("polarity_only", "joint"):
            errors = [error for f in folds for error in f["ratio_abs_log_error"][method]]
            metrics[method] = dict(
                polarity_misfit_pct=100
                * sum(f["polarity_errors"][method] for f in folds)
                / polarity_count
                if polarity_count
                else None,
                ratio_median_abs_log_error=float(np.median(errors)) if errors else None,
            )
        summary[name] = dict(
            eligible_folds=len(folds),
            attempted_folds=len(np.unique(assignments)),
            kagan_p90_deg=float(np.percentile([f["kagan_to_full_deg"] for f in folds], 90))
            if folds
            else None,
            prediction=metrics,
            folds=folds,
        )
    return summary

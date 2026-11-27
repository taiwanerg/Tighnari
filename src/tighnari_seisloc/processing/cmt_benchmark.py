"""Compare finished independent runs to versioned reference products."""

from datetime import datetime, timezone
import json
from pathlib import Path
import re

import numpy as np
from obspy.geodetics import gps2dist_azimuth

from ..acquisition.wphase import RTP
from .cap import VERSION as CAP_VERSION
from .cmt import ENSEMBLE_VERSION, VERSION, kagan, rtp_to_ned
from .cmt_centroid import VERSION as CENTROID_VERSION
from .cmt_moment import VERSION as MOMENT_VERSION
from .focal import tensor


# Release gate for a frozen blind split. Kagan and Mw use AutoBATS, which covers
# small Taiwan events; USGS pairs are checked only when enough of them exist.
GATE = dict(
    calibration_cases=20,
    blind_cases=12,
    coverage=0.60,
    autobats_cases=8,
    kagan_median_deg=30,
    kagan_p90_deg=45,
    abs_mw_median=0.3,
    usgs_pairs=8,
)
GATED_METHODS = (VERSION, ENSEMBLE_VERSION, MOMENT_VERSION, CENTROID_VERSION, CAP_VERSION)


def qualifies(metrics, gate=GATE):
    """Whether blind-split metrics meet the release gate, and the failed conditions."""
    failed = []
    accepted = metrics["accepted_blind_cases"]
    checks = (
        ("method", metrics["method"] in GATED_METHODS and bool(metrics["green_version"])),
        ("diagnostic_grid", not metrics["diagnostic_grid"]),
        ("calibration_cases", metrics["calibration_cases"] >= gate["calibration_cases"]),
        ("blind_cases", metrics["reference_blind_cases"] >= gate["blind_cases"]),
        ("coverage", metrics["coverage"] >= gate["coverage"]),
        (
            "autobats_cases",
            metrics["autobats_accepted_cases"] >= max(gate["autobats_cases"], accepted // 2),
        ),
        (
            "kagan_median",
            metrics["autobats_kagan_median_deg"] is not None
            and metrics["autobats_kagan_median_deg"] <= gate["kagan_median_deg"],
        ),
        (
            "kagan_p90",
            metrics["autobats_kagan_p90_deg"] is not None
            and metrics["autobats_kagan_p90_deg"] <= gate["kagan_p90_deg"],
        ),
        (
            "abs_mw_median",
            metrics["autobats_abs_mw_median"] is not None
            and metrics["autobats_abs_mw_median"] <= gate["abs_mw_median"],
        ),
        # Not worse than first motions alone; skipped when too few USGS pairs exist.
        (
            "usgs_change",
            len(metrics["usgs_pairs"]) < gate["usgs_pairs"]
            or (
                metrics["usgs_paired_median_change_deg"] is not None
                and metrics["usgs_paired_median_change_deg"] <= 0
            ),
        ),
    )
    failed = [name for name, ok in checks if not ok]
    return not failed, failed


def comcat(document):
    event_id = document["id"]
    references = []
    coordinates = (document.get("geometry") or {}).get("coordinates")
    time_ms = document["properties"].get("time")
    origin = (
        dict(lat=coordinates[1], lon=coordinates[0], origin=time_ms / 1000)
        if coordinates and time_ms is not None
        else None
    )
    for product in document["properties"].get("products", {}).get("moment-tensor", []):
        if product.get("status") == "DELETE" or product.get("source") != "us":
            continue
        prop = product.get("properties", {})
        centroid = None
        if prop.get("derived-latitude") is not None and prop.get("derived-longitude") is not None:
            try:
                centroid = dict(
                    lat=float(prop["derived-latitude"]), lon=float(prop["derived-longitude"])
                )
            except (TypeError, ValueError):
                pass
        try:
            references.append(
                dict(
                    source="USGS",
                    origin=origin,
                    centroid=centroid,
                    event_id=event_id,
                    product=product["code"],
                    version=product["updateTime"],
                    method=prop["derived-magnitude-type"].lower(),
                    mw=float(prop["derived-magnitude"]),
                    depth_km=float(prop["derived-depth"]),
                    tensor=rtp_to_ned([float(prop["tensor-m" + component]) for component in RTP]),
                )
            )
        except (KeyError, ValueError):
            continue
    priority = {"mwr": 0, "mww": 1, "mwc": 2, "mwb": 3}
    return sorted(
        references,
        key=lambda row: (priority.get(row["method"], 4), -row["version"], row["product"]),
    )


def autobats(path, code):
    document = path.read_text(encoding="utf-8")
    blocks = re.split(r"(?m)^\s*(\d{14})\s+([AR])\s*$", document)
    candidates = [
        (blocks[i + 1], blocks[i + 2]) for i in range(1, len(blocks), 3) if blocks[i] == code
    ]
    if not candidates:
        raise ValueError(f"Missing AutoBATS event {code}")
    version, block = min(candidates, key=lambda pair: pair[0])
    depth, mw = re.search(r"Centroid Depth\s*=\s*([\d.]+).*?Mw\s*=\s*([\d.]+)", block).groups()
    strike, dip, rake = re.search(
        r"Fault Plane #1\s*:\s*strike\s*=\s*([\d.]+).*?dip\s*=\s*([\d.]+).*?slip\s*=\s*([-\d.]+)",
        block,
    ).groups()
    coords = re.search(r"Lat\.\s*=\s*([\d.]+)\s+Long\.\s*=\s*([\d.]+)", block)
    origin = None
    if coords:
        lat, lon = map(float, coords.groups())
        stamp = datetime.strptime(code, "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
        origin = dict(lat=lat, lon=lon, origin=stamp.timestamp())
    return dict(
        source="AutoBATS",
        origin=origin,
        event_id=code,
        product=version,
        method="regional-cmt",
        mw=float(mw),
        depth_km=float(depth),
        tensor=tensor(float(strike), float(dip), float(rake)),
    )


def gcmt(path, event_id):
    from obspy import read_events

    for event in read_events(str(path), format="NDK"):
        magnitude = event.preferred_magnitude()
        origin = event.preferred_origin()
        mechanism = event.preferred_focal_mechanism()
        product = mechanism.moment_tensor
        identifier = str(str(product.resource_id).split("/")[-1])
        identifiers = {identifier, *(d.text for d in event.event_descriptions)}
        if event_id not in identifiers:
            continue
        return dict(
            source="Global CMT",
            event_id=event_id,
            product=identifier,
            method="cmt",
            mw=float(magnitude.mag),
            depth_km=float(origin.depth) / 1000,
            tensor=rtp_to_ned([getattr(product.tensor, "m_" + c) for c in RTP]),
        )
    raise ValueError(f"Missing Global CMT event {event_id}")


def load_references(root, case):
    references = []
    if case.get("comcat"):
        references.extend(comcat(json.loads((root / case["comcat"]).read_text())))
    for source, loader in (("autobats", autobats), ("gcmt", gcmt)):
        if case.get(source):
            record = case[source]
            references.append(loader(root / record["file"], record["event_id"]))
    if case.get("rmt"):
        spec = case["rmt"]
        references.append(
            dict(
                source="RMT",
                method="regional-cmt",
                event_id=spec["event_id"],
                product=spec["source_url"],
                origin=spec.get("origin"),
                centroid=spec.get("centroid"),
                mw=float(spec["mw"]),
                depth_km=float(spec["depth_km"]),
                tensor=tensor(*map(float, spec["plane"])),
            )
        )
    return references


def compare(result, first_motion, references):
    moment = result.get("tensor_ned_nm")
    first_motion_tensor = first_motion.get("tensor_ned")
    regional = first_motion.get("regional_comparison") or {}
    regional_polarity = (regional.get("polarity") or {}).get("tensor_ned")
    joint = regional.get("joint") or {}
    joint_plane = joint.get("plane")
    joint_tensor = tensor(*joint_plane) if joint_plane else None
    sh_plane = (regional.get("joint_sh") or {}).get("plane")
    rows = []
    for reference in references:
        matrix = reference["tensor"]
        row = {key: value for key, value in reference.items() if key != "tensor"}
        actual, official = result.get("location"), reference.get("origin")
        if actual and official:
            separation_km = (
                gps2dist_azimuth(actual["lat"], actual["lon"], official["lat"], official["lon"])[0]
                / 1000
            )
            delay_s = actual["origin"] - official["origin"]
            matched = abs(delay_s) <= 20 and separation_km <= 80
            row.update(
                matched=matched, origin_difference_s=delay_s, origin_distance_km=separation_km
            )
            if not matched:
                rows.append(row)
                continue
        row.update(
            kagan_deg=kagan(np.asarray(moment), matrix) if moment is not None else None,
            initial_kagan_deg=(
                kagan(np.asarray(first_motion_tensor), matrix)
                if first_motion_tensor is not None
                else None
            ),
            regional_polarity_kagan_deg=(
                kagan(np.asarray(regional_polarity), matrix)
                if regional_polarity is not None
                else None
            ),
            regional_joint_kagan_deg=kagan(joint_tensor, matrix)
            if joint_tensor is not None
            else None,
            **(
                {"regional_joint_sh_kagan_deg": kagan(tensor(*sh_plane), matrix)}
                if sh_plane
                else {}
            ),
            max_omit_one_sp_tensor_angle_deg=max(
                joint.get("omit_one_ratio_tensor_change_deg", {}).values(), default=None
            ),
            mw_delta=result["mw"] - reference["mw"] if "mw" in result else None,
            depth_delta_km=result["centroid_depth_km"] - reference["depth_km"]
            if "centroid_depth_km" in result
            else None,
        )
        actual_centroid, reference_centroid = (
            result.get("centroid_location"),
            reference.get("centroid"),
        )
        if actual_centroid and reference_centroid:
            row["centroid_distance_km"] = (
                gps2dist_azimuth(
                    actual_centroid["lat"],
                    actual_centroid["lon"],
                    reference_centroid["lat"],
                    reference_centroid["lon"],
                )[0]
                / 1000
            )
        rows.append(row)
    return rows


def wphase_comparison(result, references):
    tensor = result["MomentTensor"]
    return compare(
        dict(
            tensor_ned_nm=rtp_to_ned([tensor["tm" + component] for component in RTP]),
            mw=tensor["drmag"],
            centroid_depth_km=tensor["drdepth"],
            location=(result.get("tighnari") or {}).get("location"),
            centroid_location=dict(lat=tensor["drlat"], lon=tensor["drlon"])
            if "drlat" in tensor and "drlon" in tensor
            else None,
        ),
        {},
        [ref for ref in references if ref["source"] == "USGS" and ref["method"] == "mww"][:1],
    )


def magnitude_summary(rows):
    """Use the highest-priority revision per event and reference method."""
    groups = {}
    for row in rows:
        used = set()
        for ref in row["references"]:
            source, method = ref["source"], ref["method"]
            delta = ref.get("mw_delta")
            if (source, method) in used or ref.get("matched") is False or delta is None:
                continue
            used.add((source, method))
            band = "<4" if ref["mw"] < 4 else "4–<6" if ref["mw"] < 6 else "≥6"
            for bounds in ("all", band):
                key = (row["split"], source, method, bounds)
                groups.setdefault(key, []).append(delta)
    return [
        dict(
            split=split,
            source=source,
            method=method,
            reference_mw=band,
            cases=len(deltas),
            signed_mw_median=float(np.median(deltas)),
            abs_mw_median=float(np.median(np.abs(deltas))),
            abs_mw_p90=float(np.percentile(np.abs(deltas), 90)),
        )
        for (split, source, method, band), deltas in sorted(groups.items())
    ]


def benchmark(path):
    """Manifest paths are relative to the manifest. Score only frozen blind cases."""
    path = Path(path)
    manifest = json.loads(path.read_text(encoding="utf-8"))
    rows = []
    versions = set()
    seen = set()
    diagnostic_grid = False
    referenced_events = set()
    for case in manifest["cases"]:
        case_id = case["id"]
        if case_id in seen or case["split"] not in ("calibration", "blind", "diagnostic"):
            raise ValueError("Cases must have unique IDs and a declared split")
        seen.add(case_id)
        result = json.loads((path.parent / case["result"]).read_text())
        first_motion = {}
        if case.get("first_motion"):
            first_motion_file = path.parent / case["first_motion"]
            if first_motion_file.is_file():
                first_motion = json.loads(first_motion_file.read_text())
        refs = load_references(path.parent, case)
        event_keys = {(ref["source"], ref["event_id"]) for ref in refs}
        if referenced_events & event_keys:
            raise ValueError("Reference event used for more than one case")
        referenced_events.update(event_keys)
        if case["split"] != "diagnostic":
            versions.add(
                (result.get("method_version") or result.get("method"), result.get("green_version"))
            )
            diagnostic_grid |= result.get("diagnostic_grid") is not False
        rows.append(
            dict(
                id=case_id,
                split=case["split"],
                status=result["status"],
                first_motion_status=first_motion.get("status"),
                reasons=result.get("reasons", []),
                references=compare(result, first_motion, refs),
            )
        )
        centroid_trials = [
            dict(
                id=trial["id"],
                offset_km=trial["offset_km"],
                centroid_location=trial.get("centroid_location"),
                centroid_location_fitted=trial.get("centroid_location_fitted"),
                centroid_depth_km=trial["centroid_depth_km"],
                mw=trial["mw"],
                validation_vr_pct=trial["validation_vr_pct"],
                reasons=trial["reasons"],
                references=compare(dict(trial, location=result.get("location")), {}, refs),
            )
            for trial in result.get("candidates", [])
            if "offset_km" in trial
        ]
        if centroid_trials:
            rows[-1]["centroid_trials"] = centroid_trials
        if not result.get("tensor_ned_nm"):
            candidates = [
                r for r in result.get("candidates", []) if r.get("validation_vr_pct") is not None
            ]
            if candidates:
                best = max(candidates, key=lambda r: r["validation_vr_pct"])
                candidate = dict(best, location=result.get("location"))
                rows[-1]["diagnostic_candidate"] = dict(
                    id=best["id"],
                    mw=best["mw"],
                    validation_vr_pct=best["validation_vr_pct"],
                    reasons=best["reasons"],
                    references=compare(candidate, {}, refs),
                )
        if case.get("wphase"):
            wphase = json.loads((path.parent / case["wphase"]).read_text())
            rows[-1]["wphase_vs_mww"] = wphase_comparison(wphase, refs)

    def median(values):
        return float(np.median(values)) if values else None

    blind = [row for row in rows if row["split"] == "blind"]
    calibration = [row for row in rows if row["split"] == "calibration"]
    eligible = [row for row in blind if row["references"]]
    accepted = [
        row
        for row in eligible
        if row["status"] == "experimental"
        and any(ref.get("matched") is not False for ref in row["references"])
    ]
    paired = []
    autobats_angle, mw_error, usgs_angle = [], [], []
    for row in accepted:
        ref = next(
            (
                r
                for r in row["references"]
                if r["source"] == "AutoBATS" and r.get("matched") is not False
            ),
            None,
        )
        if ref and ref["kagan_deg"] is not None and ref["mw_delta"] is not None:
            autobats_angle.append(ref["kagan_deg"])
            mw_error.append(abs(ref["mw_delta"]))
        usgs = next(
            (
                r
                for r in row["references"]
                if r["source"] == "USGS" and r.get("kagan_deg") is not None
            ),
            None,
        )
        if usgs:
            usgs_angle.append(usgs["kagan_deg"])
    for row in eligible:
        ref = next(
            (
                r
                for r in row["references"]
                if r["source"] == "USGS"
                and r.get("kagan_deg") is not None
                and r.get("initial_kagan_deg") is not None
            ),
            None,
        )
        if ref:
            paired.append(
                dict(
                    id=row["id"],
                    status=row["status"],
                    method=ref["method"],
                    delta_deg=ref["kagan_deg"] - ref["initial_kagan_deg"],
                )
            )
    coverage = len(accepted) / len(eligible) if eligible else 0
    angle, mw = median(autobats_angle), median(mw_error)
    angle_p90 = float(np.percentile(autobats_angle, 90)) if autobats_angle else None
    delta = median([r["delta_deg"] for r in paired])
    method, green_version = next(iter(versions)) if len(versions) == 1 else (None, None)
    metrics = dict(
        method=method,
        green_version=green_version,
        diagnostic_grid=diagnostic_grid,
        calibration_cases=len(calibration),
        reference_blind_cases=len(eligible),
        accepted_blind_cases=len(accepted),
        coverage=coverage,
        autobats_accepted_cases=len(autobats_angle),
        autobats_kagan_median_deg=angle,
        autobats_kagan_p90_deg=angle_p90,
        autobats_abs_mw_median=mw,
        usgs_paired_median_change_deg=delta,
        usgs_pairs=paired,
    )
    passed, failed = qualifies(metrics)
    metrics.pop("diagnostic_grid")
    return dict(
        passed=passed,
        failed_conditions=failed,
        gate=GATE,
        **metrics,
        usgs_preferred_accepted_cases=len(usgs_angle),
        usgs_preferred_kagan_median_deg=median(usgs_angle),
        magnitude_summary=magnitude_summary(
            [row for row in rows if row["status"] == "experimental"]
        ),
        cases=rows,
    )

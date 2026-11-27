# ruff: noqa: E402
"""Freeze a CWA cohort and replay focal mechanisms without writing to the event store."""

import argparse
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from hashlib import file_digest, sha256
import json
from pathlib import Path
from random import Random
import sys
from xml.etree import ElementTree
from zipfile import ZipFile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import numpy as np
from shapely.geometry import Point

from tighnari_seisloc.acquisition.fdsn import acquire, save_json
from tighnari_seisloc.acquisition.gdms import GdmsUnavailable, acquire_gdms
from tighnari_seisloc.legacy.location import distance
from tighnari_seisloc.processing.cmt_benchmark import compare, load_references
from tighnari_seisloc.processing.focal import first_motions
from tighnari_seisloc.processing.geography import boundaries
from tighnari_seisloc.processing.pipeline import analyze
from tighnari_seisloc.processing.travel import Iasp91
from tighnari_seisloc.services.enrichment import merge_records
from tighnari_seisloc.services.waveforms import load_records, merge_gdms_measurements, save_records


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def checksum(path):
    with path.open("rb") as stream:
        return file_digest(stream, "sha256").hexdigest()


def stratum(row):
    _, towns, tree = boundaries()
    point = Point(row["lon"], row["lat"])
    if any(towns[i].covers(point) for i in tree.query(point)):
        region = "east_land" if row["lon"] >= 121 else "west_land"
    else:
        region = (
            "near_offshore"
            if towns[int(tree.nearest(point))].distance(point) * 111 <= 30
            else "offshore"
        )
    ml = row["ml"]
    magnitude = "3.0–3.4" if ml < 3.5 else "3.5–3.9" if ml < 4 else "4.0–4.9" if ml < 5 else "5+"
    depth = "0–29" if row["depth_km"] < 30 else "30–69" if row["depth_km"] < 70 else "70+"
    gap = "0–160" if row["gap_deg"] <= 160 else ">160"
    return magnitude, region, depth, gap


def catalog_rows(catalog, years):
    if catalog.suffix.lower() == ".zip":
        with ZipFile(catalog) as archive:
            for year in sorted(years):
                with archive.open(f"CWA-EQ-Catalog-{year}.xml") as xml:
                    root = ElementTree.parse(xml)
                for row in root.findall(".//{*}EarthquakeInfo"):
                    origin = datetime.fromisoformat(row.findtext("{*}OriginTime"))
                    if origin.tzinfo is None:
                        raise ValueError("CWA origin time is missing its time zone")
                    case = dict(
                        window_center_utc=origin.astimezone(timezone.utc).isoformat(),
                        ml=float(row.findtext("{*}LocalMagnitude")),
                        lat=float(row.findtext("{*}EpicenterLatitude")),
                        lon=float(row.findtext("{*}EpicenterLongitude")),
                        depth_km=float(row.findtext("{*}FocalDepth")),
                        gap_deg=float(row.findtext("{*}gap")),
                    )
                    identity = json.dumps(
                        [case[k] for k in ("window_center_utc", "lat", "lon", "depth_km")]
                    )
                    yield dict(case, id="cwa-" + sha256(identity.encode()).hexdigest()[:16])
        return
    rows = read_json(catalog)
    if not isinstance(rows, list):
        raise ValueError("Expected an array from the GDMS catalog export")
    for row in rows:
        start = datetime.fromisoformat(f"{row['date']}T{row['time']}").replace(tzinfo=timezone.utc)
        yield dict(
            id=f"cwa-{int(row['event_id'])}",
            window_center_utc=start.isoformat(),
            ml=float(row["ML"]),
            lat=float(row["latitude"]),
            lon=float(row["longitude"]),
            depth_km=float(row["depth"]),
            gap_deg=float(row["gap"]),
        )


def freeze(catalog, *, months, count, seed):
    eligible_months = {m for values in months.values() for m in values}
    if sum(len(values) for values in months.values()) != len(eligible_months):
        raise ValueError("Calibration and blind months must be disjoint")
    groups = defaultdict(list)
    ids = set()
    for case in catalog_rows(catalog, {m[:4] for m in eligible_months}):
        month = case["window_center_utc"][:7]
        if month not in eligible_months or case["ml"] < 3:
            continue
        if case["id"] in ids:
            raise ValueError(f"Duplicate CWA event ID: {case['id']}")
        ids.add(case["id"])
        split = next(key for key, values in months.items() if month in values)
        case.update(split=split, stratum=list(stratum(case)))
        groups[(split, *case["stratum"])].append(case)
    if not any(key[1] == "3.0–3.4" for key in groups) or len(ids) < count:
        raise ValueError("Catalog selection lacks ML 3.0–3.4 events or the requested sample size")
    rng = Random(seed)
    for group in groups.values():
        group.sort(key=lambda case: case["id"])
        rng.shuffle(group)
    keys = sorted(groups)
    low = [key for key in keys if key[1] == "3.0–3.4"]
    # Give the smallest band an additional draw per cycle while retaining other strata.
    rotation = keys + low
    population = {key: len(groups[key]) for key in keys}
    cases = []
    while len(cases) < count:
        for key in rotation:
            if groups[key]:
                cases.append(groups[key].pop())
            if len(cases) == count:
                break
    sampled = Counter((case["split"], *case["stratum"]) for case in cases)
    return dict(
        catalog_sha256=checksum(catalog),
        months=months,
        seed=seed,
        population=[
            dict(split=k[0], stratum=k[1:], population=population[k], sampled=sampled[k])
            for k in keys
        ],
        cases=cases,
    )


def audit(result):
    obs = result["observations"]
    az = np.sort([r["azimuth"] % 360 for r in obs])
    dispositions = Counter(row["disposition"] for row in result.get("refinement_diagnostics", []))
    rejected = Counter(
        reason
        for row in result.get("refinement_diagnostics", [])
        if row["disposition"] != "used"
        for reason in row["reasons"]
    )
    regional = result.get("regional_comparison", {})
    joint = regional.get("joint", {})
    sh = regional.get("joint_sh", {})
    return dict(
        status=result["status"],
        station_count=len(obs),
        reasons=result["reasons"],
        azimuth_gap_deg=float(np.diff(np.r_[az, az[0] + 360]).max()) if len(az) else None,
        takeoff_range_deg=float(np.ptp([r["takeoff"] for r in obs])) if obs else None,
        polarity_by_provider=dict(Counter(r.get("provider", "unknown") for r in obs)),
        leave_one_site=result.get("leave_one_site"),
        location_stability=result.get("stability"),
        dispositions=dict(dispositions),
        rejected=dict(rejected),
        taiwan_1d_status=regional.get("polarity", {}).get("status"),
        ratio_count=joint.get("ratio_count"),
        joint_status=joint.get("status"),
        joint_diagnostics=joint.get("cross_validation"),
        sh_ratio_count=sh.get("ratio_count"),
        sh_joint_status=sh.get("status"),
        sh_joint_diagnostics=sh.get("cross_validation"),
    )


def evaluate(event, waves, output, references):
    result = first_motions(waves, event, refine_onsets=True, stability=True, validate_joint=True)
    save_json(output, result)
    summary = audit(result)
    if references:
        summary["references"] = compare({"location": result["location"]}, result, references)
    return summary


def fixed_location_comparison(event, records, output, references):
    original = [r for r in records if r.provider != "gdms"]
    groups = {"original": original, "with_gdms": merge_gdms_measurements(records)}
    summary = {}
    for name, waveforms in groups.items():
        if name == "with_gdms" and len(original) == len(records):
            summary[name] = dict(status="missing_gdms")
            continue
        if not waveforms:
            summary[name] = dict(status="missing_original")
            continue
        summary[name] = evaluate(event, waveforms, output / f"{name}.json", references)
    return summary


def event_id(case):
    name = case["id"]
    if Path(name).name != name or name in ("", ".", ".."):
        raise ValueError(f"Invalid event ID: {name}")
    return name


def independent_comparison(preparation, path, output, references):
    summary = {}
    for name, status in read_json(preparation)["methods"].items():
        if status != "located":
            summary[name] = dict(status=status)
            continue
        event = path / name / "event.json"
        waves = path / name / "measurements.npz"
        summary[name] = dict(
            evaluate(read_json(event), load_records(waves), output / f"{name}.json", references),
            event_sha256=checksum(event),
            waveform_sha256=checksum(waves),
        )
    return summary


def replay(manifest, output, inputs, reference_manifest=None):
    reference_index = (
        {row["id"]: row for row in read_json(reference_manifest)["cases"]}
        if reference_manifest
        else {}
    )
    rows = []
    for case in read_json(manifest)["cases"]:
        name = event_id(case)
        references = (
            load_references(reference_manifest.parent, reference_index[name])
            if name in reference_index
            else []
        )
        path = inputs / name
        event_path = path / "event.json"
        waves = path / "measurements.npz"
        if not waves.is_file():
            waves = path / "calibrated.npz"
        row = dict(id=name, stratum=case.get("stratum"), split=case.get("split"))
        preparation = path / "preparation.json"
        if preparation.is_file():
            row.update(
                status="evaluated",
                location_policy="independent_location_per_input_group",
                methods=independent_comparison(preparation, path, output / name, references),
            )
        elif not event_path.is_file() or not waves.is_file():
            row["status"] = "missing_event_or_waveforms"
        else:
            event = read_json(event_path)
            row.update(
                status="evaluated",
                location_policy="same_archived_hypocenter_and_picks",
                event_sha256=checksum(event_path),
                waveform_sha256=checksum(waves),
                locating_station_count=event.get("count"),
                depth_status=event.get("depth_status"),
                methods=fixed_location_comparison(
                    event, load_records(waves), output / name, references
                ),
            )
        rows.append(row)
        print(name, row["status"], flush=True)
    statuses = Counter(row["status"] for row in rows)
    methods = defaultdict(Counter)
    for row in rows:
        for name, result in row.get("methods", {}).items():
            methods[name][result["status"]] += 1
    save_json(
        output / "summary.json",
        dict(
            manifest_sha256=checksum(manifest),
            statuses=dict(statuses),
            method_statuses={name: dict(counts) for name, counts in methods.items()},
            cases=rows,
        ),
    )


def prepare_case(case, inputs, travel, gdms_root, station_limit, retry):
    case_dir = inputs / event_id(case)
    origin = datetime.fromisoformat(case["window_center_utc"]).timestamp()
    original_path = case_dir / "original.npz"
    if original_path.is_file() and not retry:
        original = load_records(original_path)
    else:
        original, _ = acquire(
            origin - 60, origin + 240, case_dir / "fdsn", limit=station_limit, retry_failed=retry
        )
        if original:
            save_records(original, original_path)
    inbox = gdms_root / event_id(case) if gdms_root else None
    supplemental, provenance = [], {}
    if inbox and (inbox / "manifest.json").is_file():
        try:
            supplemental, provenance = acquire_gdms(origin - 60, origin + 240, inbox)
        except (GdmsUnavailable, OSError, ValueError) as error:
            provenance = {"error": type(error).__name__}
    states = {}
    for name, waveforms in (("original", original), ("with_gdms", [*original, *supplemental])):
        if (name == "with_gdms" and not supplemental) or not waveforms:
            states[name] = (
                ("gdms_load_failed" if "error" in provenance else "missing_gdms")
                if name == "with_gdms"
                else "missing_original"
            )
            continue
        if name == "with_gdms" and not original:
            name = "gdms_only"
            states["with_gdms"] = "missing_original"
        work = case_dir / name
        merged = merge_records([], waveforms, origin, preferred_provider="gdms")
        report = analyze(
            merged, travel, "phasenet", cache=case_dir / "picks", measurement_records=waveforms
        )
        candidates = [
            event
            for event in report["events"]
            if abs(event["origin"] - origin) <= 15
            and distance(event["lat"], event["lon"], case["lat"], case["lon"]) <= 40
        ]
        if len(candidates) != 1:
            states[name] = "ambiguous_association" if candidates else "not_detected"
            continue
        save_records(waveforms, work / "measurements.npz")
        save_json(work / "event.json", candidates[0])
        states[name] = "located"
    save_json(case_dir / "preparation.json", dict(methods=states, gdms=provenance))
    return states


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    select = commands.add_parser("freeze")
    select.add_argument("catalog", type=Path)
    select.add_argument("output", type=Path)
    select.add_argument("--calibration", nargs="+", required=True, metavar="YYYY-MM")
    select.add_argument("--blind", nargs="+", required=True, metavar="YYYY-MM")
    select.add_argument("--count", type=int, default=150)
    select.add_argument("--seed", type=int, default=20261123)
    run = commands.add_parser("replay")
    run.add_argument("manifest", type=Path)
    run.add_argument("inputs", type=Path)
    run.add_argument("output", type=Path)
    run.add_argument("--references", type=Path, help="CMT benchmark manifest with reference files")
    prepare = commands.add_parser("prepare")
    prepare.add_argument("manifest", type=Path)
    prepare.add_argument("inputs", type=Path)
    prepare.add_argument("--gdms-root", type=Path)
    prepare.add_argument("--travel", type=Path, default=Path("runtime/iasp91.npz"))
    prepare.add_argument("--station-limit", type=int, default=60)
    prepare.add_argument("--workers", type=int, choices=(1, 2), default=1)
    prepare.add_argument("--case", action="append", help="Restrict preparation to selected IDs")
    prepare.add_argument("--retry", action="store_true")
    args = parser.parse_args()
    if args.command == "freeze":
        if args.count < 1:
            parser.error("--count must be positive")
        months = dict(calibration=args.calibration, blind=args.blind)
        save_json(
            args.output, freeze(args.catalog, months=months, count=args.count, seed=args.seed)
        )
    elif args.command == "replay":
        replay(args.manifest, args.output, args.inputs, args.references)
    else:
        if not 1 <= args.station_limit <= 60:
            parser.error("--station-limit must be between 1 and 60")
        cases = read_json(args.manifest)["cases"]
        if args.case:
            requested = set(args.case)
            cases = [case for case in cases if case["id"] in requested]
            if {case["id"] for case in cases} != requested:
                parser.error("--case includes an ID absent from the manifest")
        travel = Iasp91(args.travel)

        def prepare_one(case):
            return prepare_case(
                case, args.inputs, travel, args.gdms_root, args.station_limit, args.retry
            )

        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            for case, states in zip(cases, pool.map(prepare_one, cases)):
                print(case["id"], states, flush=True)


if __name__ == "__main__":
    main()

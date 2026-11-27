"""Reproduce the offline RMT comparison and evaluate the locked choice on a previous cohort."""

import argparse
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path

import numpy as np
from obspy.geodetics import gps2dist_azimuth
from pyproj import Geod

from tighnari_seisloc.acquisition.cmt import DISTANCE_KM, gdms_inputs
from tighnari_seisloc.acquisition.cmt_greens import GreenStores
from tighnari_seisloc.acquisition.cmt_research import prepare_components
from tighnari_seisloc.acquisition.fdsn import save_json
from tighnari_seisloc.processing.cmt import BAND, invert, kagan
from tighnari_seisloc.processing.cmt_benchmark import compare, load_references
from tighnari_seisloc.processing.cmt_research import horizontal_solution, phase_weights
from tighnari_seisloc.processing.focal import tensor

RMT = {
    "20220918-taitung": ((88.1, 51.8, 141.6), 8, 6.95),
    "20240403-hualien": ((32.3, 50.5, 86.5), 42, 7.32),
    "20250121-chianan": ((14.0, 39.0, 85.0), 12, 6.09),
    "20251227-yilan": ((182.1, 67.6, 54.7), 66, 6.69),
}
PROFILES = (
    "cap",
    "archive",
    "archive_cap",
    "archive_anchored",
    "velocity",
    "phase_windows",
    "horizontal",
    "combined",
)
VELOCITY_BAND = (0.02, 0.10)
HORIZONTAL_DEPTHS = (*range(2, 100, 6), 100)
OFFSETS = [(0, 0), (24, 0), (-24, 0), (0, 24), (0, -24), (24, 24), (-24, 24), (24, -24), (-24, -24)]
GEOD = Geod(ellps="WGS84")


def archive_pairs(case):
    candidates = [
        ("earthscope", case / "archive/earthscope.mseed", case / "archive/earthscope.xml"),
        ("locator", case / "locator.mseed", case / "locator.xml"),
    ]
    return [entry for entry in candidates if all(p.is_file() for p in entry[1:])]


def _gap(stations):
    az = np.sort([s["azimuth"] for s in stations])
    return max(np.diff(np.r_[az, az[0] + 360])) if len(az) else 360


def _anchored(
    case, baseline, archive_sites, archive_data, archive_masks, location, greens, diagnostics
):
    if not (case / "fits.npz").is_file():
        return dict(
            status="insufficient_data",
            reasons=["no_baseline_waveforms"],
            archive_diagnostics=diagnostics,
        )
    stations = list(baseline["stations"])
    baseline_count = len(stations)
    with np.load(case / "fits.npz") as fits:
        data = list(fits["observed_m"])
    masks = [np.zeros_like(values) for values in data]
    for s, mask in zip(stations, masks):
        lo, hi = s["window"]
        mask[:, lo:hi] = 1
    available = list(zip(archive_sites, archive_data, archive_masks))
    while available and len(stations) < 12:
        available = [
            row
            for row in available
            if all(
                row[0]["id"] != original["id"]
                and gps2dist_azimuth(
                    row[0]["lat"], row[0]["lon"], original["lat"], original["lon"]
                )[0]
                >= 2000
                for original in stations
            )
        ]
        if not available:
            break
        site, values, mask = min(
            available,
            key=lambda row: (
                _gap([*stations, row[0]]),
                -int(row[2].sum()),
                row[0]["id"],
            ),
        )
        stations.append(site)
        data.append(values)
        masks.append(mask)
    factors = np.ones(len(stations))
    factors[baseline_count:] = np.sqrt(0.5)
    banks = {
        name: values * factors[None, :, None, None, None]
        for name, values in greens.banks(location, stations)
    }
    result, _ = invert(
        np.asarray(data) * factors[:, None, None],
        banks,
        stations,
        sample_weights=np.asarray(masks),
        energy_cap=3,
        research_details=True,
    )
    return dict(result, archive_diagnostics=diagnostics, original_station_count=baseline_count)


def _case(case, greens, profiles, extra=()):
    baseline = json.loads((case / "result.json").read_text())
    if baseline.get("green_version") and baseline["green_version"] != greens.version:
        raise ValueError(f"Green stores differ from baseline for {case.name}")
    location = baseline.get("location")
    results = {"baseline": baseline}
    if not location:
        for name in profiles:
            results[name] = dict(status="insufficient_data", reasons=["origin_unavailable"])
        return results
    if "cap" in profiles:
        if (case / "fits.npz").is_file():
            sites = baseline["stations"]
            with np.load(case / "fits.npz") as fits:
                data = fits["observed_m"]
            banks = dict(greens.banks(location, sites))
            results["cap"], _ = invert(data, banks, sites, energy_cap=3, research_details=True)
        else:
            results["cap"] = dict(status="insufficient_data", reasons=["no_baseline_waveforms"])
    inputs = archive_pairs(case) + list(extra)
    if set(profiles) & {"archive", "archive_cap", "archive_anchored"}:
        sites, data, masks, diag = prepare_components(location, inputs, BAND, "DISP")
        regular = set(profiles) & {"archive", "archive_cap"}
        banks = dict(greens.banks(location, sites)) if regular and len(sites) >= 5 else {}
        for name in regular:
            results[name], _ = invert(
                data,
                banks,
                sites,
                sample_weights=masks,
                energy_cap=3 if name == "archive_cap" else None,
                research_details=True,
            )
            results[name]["archive_diagnostics"] = diag
        if "archive_anchored" in profiles:
            results["archive_anchored"] = _anchored(
                case, baseline, sites, data, masks, location, greens, diag
            )
    if set(profiles) & {"velocity", "phase_windows", "horizontal", "combined"}:
        sites, data, masks, diag = prepare_components(location, inputs, VELOCITY_BAND, "VEL")
        segmented = phase_weights(sites, masks)
        if set(profiles) & {"velocity", "phase_windows"}:
            banks = (
                dict(greens.banks(location, sites, band=VELOCITY_BAND, velocity=True))
                if len(sites) >= 5
                else {}
            )
            for name in set(profiles) & {"velocity", "phase_windows"}:
                results[name], _ = invert(
                    data,
                    banks,
                    sites,
                    band=VELOCITY_BAND,
                    energy_cap=3,
                    research_details=True,
                    sample_weights=segmented if name == "phase_windows" else masks,
                )
                results[name]["archive_diagnostics"] = diag
        if set(profiles) & {"horizontal", "combined"}:
            trials = {p: [] for p in profiles if p in ("horizontal", "combined")}
            for east, north in OFFSETS:
                lon, lat, _ = GEOD.fwd(
                    location["lon"],
                    location["lat"],
                    float(np.degrees(np.arctan2(east, north))),
                    float(np.hypot(east, north)) * 1000,
                )
                point = dict(location, lat=lat, lon=lon)
                distances = [
                    gps2dist_azimuth(lat, lon, s["lat"], s["lon"])[0] / 1000 for s in sites
                ]
                if any(not DISTANCE_KM[0] <= d <= DISTANCE_KM[1] for d in distances):
                    continue
                moved = [
                    dict(s, azimuth=float(gps2dist_azimuth(lat, lon, s["lat"], s["lon"])[1]))
                    for s in sites
                ]
                banks = (
                    dict(
                        greens.banks(
                            point, moved, HORIZONTAL_DEPTHS, band=VELOCITY_BAND, velocity=True
                        )
                    )
                    if len(sites) >= 5
                    else {}
                )
                for name, rows in trials.items():
                    result, _ = invert(
                        data,
                        banks,
                        moved,
                        HORIZONTAL_DEPTHS,
                        band=VELOCITY_BAND,
                        sample_weights=segmented if name == "combined" else masks,
                        energy_cap=3,
                        research_details=True,
                    )
                    rows.append(dict(result, offset_km=[east, north], centroid_location=point))
                if len(sites) < 5:
                    break
            for name, rows in trials.items():
                results[name] = (
                    horizontal_solution(rows)
                    if rows
                    else dict(status="insufficient_data", reasons=["no_supported_horizontal_grid"])
                )
                results[name]["archive_diagnostics"] = diag
    for result in results.values():
        result.setdefault("location", location)
        result["green_version"] = greens.version
    return results


def measure(result, plane, depth, mw):
    mt = result.get("tensor_ned_nm")
    return dict(
        status=result["status"],
        station_count=result.get("station_count", 0),
        holdout_vr_pct=result.get("holdout_vr_pct"),
        depth_km=result.get("centroid_depth_km"),
        mw=result.get("mw"),
        offset_km=result.get("offset_km"),
        kagan_deg=kagan(np.asarray(mt), tensor(*plane)) if mt is not None else None,
        depth_error_km=abs(result["centroid_depth_km"] - depth) if mt is not None else None,
        mw_error=abs(result["mw"] - mw) if mt is not None else None,
        reasons=result.get("reasons", []),
    )


def summarize(rows, names):
    summary = {}
    for name in names:
        comparisons = [r[name] for r in rows]
        angles = [r["kagan_deg"] for r in comparisons if r["kagan_deg"] is not None]
        summary[name] = dict(
            count=len(angles),
            accepted=sum(r["status"] == "experimental" for r in comparisons),
            median_kagan_deg=float(np.median(angles)) if angles else None,
            mean_kagan_deg=float(np.mean(angles)) if angles else None,
            median_depth_error_km=float(
                np.median(
                    [r["depth_error_km"] for r in comparisons if r["depth_error_km"] is not None]
                )
            )
            if angles
            else None,
        )
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--store", type=Path, default=Path("runtime/cmt-greens"))
    parser.add_argument("--workers", type=int, choices=(1, 2, 3, 4), default=2)
    parser.add_argument(
        "--cohort", action="store_true", help="Evaluate the already locked RMT choice"
    )
    parser.add_argument(
        "--resume", action="store_true", help="Reuse existing per-method calibration results"
    )
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    if args.cohort:
        locked = json.loads((args.output / "rmt-selection.json").read_text())
        selected = locked["selected"]
        profiles = (selected,) if selected != "baseline" else ()
        manifest_path = args.archive / "cases-blind.json"
        cases = [
            row for row in json.loads(manifest_path.read_text())["cases"] if row["split"] == "blind"
        ]

        def run(row):
            key = row["id"]
            case = args.archive / "cohort" / key
            results = _case(case, GreenStores(args.store), profiles)
            refs = load_references(manifest_path.parent, row)
            comparisons = {
                name: dict(
                    status=r["status"],
                    reasons=r.get("reasons", []),
                    references=compare(r, {}, refs),
                )
                for name, r in results.items()
            }
            save_json(args.output / "cohort" / key / "result.json", results)
            print(key, comparisons[selected]["status"], flush=True)
            return dict(event=key, **comparisons)
    else:
        profiles = PROFILES
        cases = list(RMT)

        def run(key):
            directory = Path("runtime/four-event-images-20261121") / key / "gdms"
            if key == "20220918-taitung":
                directory = Path("runtime/science-integration-20261121/918-inputs/gdms")
            path = args.output / key / "result.json"
            existing = json.loads(path.read_text()) if args.resume and path.is_file() else {}
            greens = GreenStores(args.store)
            if existing and any(
                result.get("green_version") != greens.version for result in existing.values()
            ):
                raise ValueError("Cached methods used a different Green store")
            missing = tuple(name for name in profiles if name not in existing)
            results = _case(args.archive / key, greens, missing, gdms_inputs(directory))
            results.update(existing)
            save_json(path, results)
            print(
                key,
                {name: measure(r, *RMT[key])["kagan_deg"] for name, r in results.items()},
                flush=True,
            )
            return dict(event=key, **{name: measure(r, *RMT[key]) for name, r in results.items()})

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        rows = list(pool.map(run, cases))
    if args.cohort:
        names = list(dict.fromkeys(("baseline", selected)))
        scores = {}
        for name in names:
            agents = {}
            for source_name in ("AutoBATS", "USGS"):
                refs = []
                for row in rows:
                    source = next(
                        (r for r in row[name]["references"] if r["source"] == source_name), None
                    )
                    if source and source.get("kagan_deg") is not None:
                        refs.append((row["event"], source["kagan_deg"]))
                agents[source_name] = dict(
                    count=len(refs),
                    median_kagan_deg=float(np.median([x[1] for x in refs])) if refs else None,
                    angles=dict(refs),
                )
            scores[name] = dict(
                accepted=sum(row[name]["status"] == "experimental" for row in rows),
                source_metrics=agents,
            )
        paired = {}
        for source in ("AutoBATS", "USGS"):
            before = scores["baseline"]["source_metrics"][source]["angles"]
            after = scores[selected]["source_metrics"][source]["angles"]
            keys = sorted(before.keys() & after.keys())
            changes = [after[key] - before[key] for key in keys]
            paired[source] = dict(
                count=len(keys),
                baseline_median_deg=float(np.median([before[k] for k in keys])) if keys else None,
                selected_median_deg=float(np.median([after[k] for k in keys])) if keys else None,
                median_change_deg=float(np.median(changes)) if changes else None,
                improved=sum(change < -0.05 for change in changes),
                similar=sum(abs(change) <= 0.05 for change in changes),
                worsened=sum(change > 0.05 for change in changes),
            )
        save_json(
            args.output / "cohort-comparison.json",
            dict(selected=selected, scores=scores, paired=paired, cases=rows),
        )
    else:
        names = ("baseline", *profiles)
        summary = summarize(rows, names)
        # Missing all-four coverage cannot improve apparent agreement through omission.
        selected = min(
            (n for n in names if summary[n]["count"] == len(rows)),
            key=lambda n: (
                summary[n]["median_kagan_deg"],
                summary[n]["mean_kagan_deg"],
                summary[n]["median_depth_error_km"],
            ),
        )
        save_json(
            args.output / "rmt-selection.json",
            dict(
                selected=selected,
                selection_rule="all four cases, minimum median Kagan angle; ties by mean angle and median depth error",
                profiles=summary,
                cases=rows,
            ),
        )
        print("Selected", selected, summary[selected], flush=True)


if __name__ == "__main__":
    main()

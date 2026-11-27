# ruff: noqa: E402
"""Compare archived Tighnari P-polarity solutions with an installed SKHASH CLI."""

import argparse
import csv
import json
from pathlib import Path
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import numpy as np

from tighnari_seisloc.processing.focal import tensor


def kagan(left, right):
    def axes(moment):
        _, basis = np.linalg.eigh(moment)
        if np.linalg.det(basis) < 0:
            basis[:, 0] *= -1
        return basis

    relative = axes(left).T @ axes(right)
    diagonal = np.diag(relative)
    trace = max(
        np.dot(diagonal, signs) for signs in ((1, 1, 1), (1, -1, -1), (-1, 1, -1), (-1, -1, 1))
    )
    return round(float(np.degrees(np.arccos(np.clip((trace - 1) / 2, -1, 1)))), 2)


def make_control():
    """Exercise both ray hemispheres and verify that polarity sign is preserved."""
    plane = (37, 58, 73)
    moment = tensor(*plane)
    observations = []
    for index in range(96):
        azimuth, takeoff = (index * 137.508) % 360, 15 + index * 17 % 150
        phi, theta = np.radians([azimuth, takeoff])
        ray = np.array([np.sin(theta) * np.cos(phi), np.sin(theta) * np.sin(phi), np.cos(theta)])
        radiation = float(ray @ moment @ ray)
        if abs(radiation) > 0.15:
            observations.append(
                dict(
                    id=f"XX.S{index:03d}..HH",
                    azimuth=azimuth,
                    takeoff=takeoff,
                    polarity=1 if radiation > 0 else -1,
                )
            )
    return dict(observations=observations, tensor_ned=moment.tolist(), status="synthetic")


def export_inputs(path, events):
    with path.open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            (
                "event_id",
                "network",
                "station",
                "location",
                "channel",
                "p_polarity",
                "azimuth",
                "takeoff",
            )
        )
        for name, result in events.items():
            for row in result["observations"]:
                network, station, location, channel = row["id"].split(".")
                writer.writerow(
                    (
                        name,
                        network,
                        station,
                        location or "--",
                        channel,
                        row["polarity"],
                        row["azimuth"],
                        row["takeoff"],
                    )
                )


def export_amplitudes(path, events):
    ratios = {}
    with path.open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            (
                "event_id",
                "network",
                "station",
                "location",
                "channel",
                "sp_ratio",
                "azimuth",
                "takeoff",
            )
        )
        for name, event in events.items():
            observations = (
                event.get("regional_comparison", {}).get("joint", {}).get("observations", [])
            )
            ratios[name] = {}
            for row in observations:
                net, sta, loc, chan = row["id"].split(".")
                loc = loc or "--"
                value = 10 ** row["log_sp"]
                ratios[name][".".join((net, sta, loc, chan))] = value
                writer.writerow((name, net, sta, loc, chan, value, row["azimuth"], row["takeoff"]))
    return ratios


def verify_observations(path, events, solved, ratios=None):
    """Check receiver identity, polarity, and the round-tripped input geometry."""
    with path.open(newline="") as handle:
        rows = list(csv.DictReader(handle))
    for name in solved:
        expected = {}
        for row in events[name]["observations"]:
            network, station, location, channel = row["id"].split(".")
            expected[".".join((network, station, location or "--", channel))] = row
        used = [row for row in rows if row["event_id"] == name]
        actual = {row["sta_code"]: row for row in used if float(row["p_polarity"] or 0) != 0}
        if set(actual) != set(expected):
            raise RuntimeError(f"SKHASH receiver identities changed for {name}")
        for code, row in expected.items():
            measured = actual[code]
            if float(measured["p_polarity"]) != row["polarity"] or any(
                abs(float(measured[key]) - row[key]) > 0.051 for key in ("azimuth", "takeoff")
            ):
                raise RuntimeError(f"SKHASH polarity or geometry changed for {name}/{code}")
        if ratios is not None:
            measured = {
                row["sta_code"]: float(row["sp_ratio"]) for row in used if row.get("sp_ratio")
            }
            expected_ratios = ratios[name]
            if set(measured) != set(expected_ratios) or any(
                not np.isclose(measured[code], value, atol=5.1e-6)
                for code, value in expected_ratios.items()
            ):
                raise RuntimeError(f"SKHASH S/P measurements changed for {name}")


def run_solver(executable, directory, inputs, settings):
    directory.mkdir(parents=True, exist_ok=True)
    config = dict(
        fpfile=inputs,
        outfile1="preferred.csv",
        outfile2="acceptable.csv",
        outfile_pol_info="used-polarities.csv",
        outfile_pol_agree="polarity-agreement.csv",
        flip_takeoff=True,
        delmax=0,
        output_quality_precision=3,
        **settings,
    )
    (directory / "control.txt").write_text("".join(f"${k}\n{v}\n" for k, v in config.items()))
    with (directory / "run.log").open("w") as log:
        subprocess.run(
            [str(executable), "control.txt"],
            cwd=directory,
            stdout=log,
            stderr=subprocess.STDOUT,
            check=True,
        )
    with (directory / "preferred.csv").open(newline="") as handle:
        return list(csv.DictReader(handle))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--skhash", required=True, type=Path, help="Path to the official SKHASH CLI"
    )
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--include-sp", action="store_true", help="Also check measured vector S/P")
    parser.add_argument("solutions", nargs="+", type=Path, help="Archived first_motions JSON files")
    options = parser.parse_args()
    names = [p.stem for p in options.solutions]
    if len(set(names)) != len(names) or "synthetic-control" in names:
        parser.error("Solution filenames must have unique stems other than synthetic-control")
    events = {p.stem: json.loads(p.read_text()) for p in options.solutions}
    events["synthetic-control"] = make_control()
    output = options.out.resolve()
    output.mkdir(parents=True, exist_ok=True)
    inputs = output / "polarities.csv"
    export_inputs(inputs, events)
    fixed = dict(
        npolmin=10,
        max_agap=360,
        max_pgap=90,
        nmc=1,
        default_takeoff_uncert=0,
        default_azimuth_uncert=0,
    )
    modes = {
        "fixed": fixed,
        "perturbed": dict(fixed, nmc=30, default_takeoff_uncert=10, default_azimuth_uncert=5),
        "standard_geometry_qc": dict(fixed, npolmin=8, max_agap=90, max_pgap=60),
    }
    ratios = None
    if options.include_sp:
        amplitudes = output / "amplitudes.csv"
        ratios = export_amplitudes(amplitudes, events)
        if any(ratios.values()):
            modes["fixed_sp"] = dict(fixed, ampfile=amplitudes)
            modes["perturbed_sp"] = dict(modes["perturbed"], ampfile=amplitudes)
    summary = {
        name: dict(input_stations=len(e["observations"]), tighnari_status=e["status"], modes={})
        for name, e in events.items()
    }
    for mode, settings in modes.items():
        directory = output / mode
        rows = run_solver(options.skhash.resolve(), directory, inputs, settings)
        verify_observations(
            directory / "used-polarities.csv",
            events,
            {row["event_id"] for row in rows},
            ratios=ratios if mode.endswith("_sp") else None,
        )
        for name, original in events.items():
            mechanisms = []
            for row in rows:
                if row["event_id"] != name:
                    continue
                if int(row["num_p_pol"]) != len(original["observations"]):
                    raise RuntimeError(f"{mode}: SKHASH station count changed for {name}")
                plane = [float(row[key]) for key in ("strike", "dip", "rake")]
                angle = (
                    kagan(tensor(*plane), original["tensor_ned"])
                    if "tensor_ned" in original
                    else None
                )
                mechanisms.append(
                    dict(
                        plane=plane,
                        quality=row["quality"],
                        kagan_to_tighnari_deg=angle,
                        polarity_misfit_pct=float(row["polarity_misfit"]),
                        fault_plane_uncertainty_deg=float(row["fault_plane_uncertainty"]),
                        cluster_share_pct=float(row["prob_mech"]),
                        azimuthal_gap_deg=float(row["azimuthal_gap"]),
                        takeoff_gap_deg=float(row["takeoff_gap"]),
                        ratio_count=int(row["num_sp_ratios"]),
                    )
                )
            summary[name]["modes"][mode] = mechanisms
    control = summary["synthetic-control"]["modes"]["fixed"]
    if not control or control[0]["kagan_to_tighnari_deg"] > 10:
        raise RuntimeError("Synthetic control failed: check angle and polarity conventions")
    (output / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")


if __name__ == "__main__":
    main()

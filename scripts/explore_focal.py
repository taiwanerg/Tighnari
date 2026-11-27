"""Inspect first-motion grid ambiguity at fixed or perturbed depth offline."""

import argparse
import json
from pathlib import Path
import sys

import numpy as np
from obspy.geodetics import gps2dist_azimuth, kilometers2degrees
from obspy.taup import TauPyModel
from obspy.imaging.beachball import aux_plane
from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from tighnari_seisloc.processing.focal import grid, representative_best  # noqa: E402
from tighnari_seisloc.services.mechanism_plot import beachball  # noqa: E402
from tighnari_seisloc.services.reporting import font  # noqa: E402


def search(rows):
    planes, tensors = grid()
    az, theta = np.deg2rad([[r[key] for r in rows] for key in ("azimuth", "takeoff")])
    rays = np.column_stack((np.sin(theta) * np.cos(az), np.sin(theta) * np.sin(az), np.cos(theta)))
    amp = np.einsum("ni,kij,nj->kn", rays, tensors, rays)
    loss = np.where(abs(amp) < 0.05, 0.5, amp * np.array([r["polarity"] for r in rows]) < 0).sum(
        axis=1
    )
    tied = np.flatnonzero(loss == loss.min())
    first = representative_best(loss, planes, tensors)
    # Exhibit the most different equally good grid entry as a counterexample.
    cosines = np.einsum("kij,ij->k", tensors[tied], tensors[first]) / 2
    last = tied[np.argmin(cosines)]
    return dict(
        count=len(rows),
        equivalent_errors=float(loss.min()),
        tied_grid_entries=len(tied),
        tensor_angle_deg=float(np.rad2deg(np.arccos(np.clip(cosines.min(), -1, 1)))),
        candidates=[
            dict(
                plane=planes[i].tolist(),
                auxiliary=list(aux_plane(*planes[i])),
                tensor=tensors[i].tolist(),
            )
            for i in (first, last)
        ],
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("result", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument(
        "--model", default="iasp91", help="TauP model name or compiled regional 1-D model"
    )
    args = parser.parse_args()
    source = json.loads(args.result.read_text())
    rows = source["observations"]
    if not rows:
        parser.error("No accepted independent polarities")
    loc = source["location"]
    solution = search(rows)
    model = TauPyModel(args.model)
    depths = source["location_diagnostics"]["depth_range_km"]
    trials = []
    for depth in sorted(set([max(0.01, depths[0]), loc["depth_km"], depths[1]])):
        changed = []
        for r in rows:
            meters, az, _ = gps2dist_azimuth(loc["lat"], loc["lon"], r["lat"], r["lon"])
            arrivals = model.get_travel_times(
                depth, kilometers2degrees(meters / 1000), phase_list=["p", "P"]
            )
            if arrivals:
                changed.append(
                    dict(r, azimuth=az, takeoff=min(arrivals, key=lambda a: a.time).takeoff_angle)
                )
        trials.append(dict(depth_km=depth, solution=search(changed) if changed else None))
    args.output.mkdir(exist_ok=True, parents=True)
    report = dict(
        research_only=True,
        original_status=source["status"],
        reasons=source["reasons"],
        fixed_observed_geometry=solution,
        depth_trial_model=args.model,
        depth_trials=trials,
    )
    (args.output / "forced.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
    img = Image.new("RGB", (980, 440), "#10191b")
    d = ImageDraw.Draw(img)
    d.text((25, 14), "P 初動強制反演｜代表解與最遠同分解（研究用）", font=font(32), fill="white")
    for x, c in zip((120, 600), solution["candidates"]):
        ball = beachball(c["tensor"], 225)
        img.paste(ball, (x, 90), ball)
        d.text((x + 112, 73), "N", font=font(18), fill="white", anchor="mm")
        d.text((x, 325), " / ".join(str(v) + "°" for v in c["plane"]), font=font(23), fill="white")
    d.text(
        (30, 385),
        f"有效 {len(rows)} 站　｜　走向 / 傾角 / 滑移角　｜　紅色為壓縮象限",
        font=font(22),
        fill="#a9c7c7",
    )
    img.save(args.output / "forced.png")


if __name__ == "__main__":
    main()

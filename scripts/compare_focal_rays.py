# ruff: noqa: E402
"""Compare identical P and S/P observations with regional 1D and 3D first arrivals."""

import argparse
from hashlib import file_digest
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import numpy as np
from scipy.interpolate import RegularGridInterpolator

from tighnari_seisloc.acquisition.fdsn import save_json
from tighnari_seisloc.processing.cmt import kagan
from tighnari_seisloc.processing.focal import leave_one_site, solve, tensor
from tighnari_seisloc.processing.regional_focal import fit_joint


def first_arrival(grid, velocity, receiver, source, spacing, march):
    """Trace the direction at the source from a reciprocal first-arrival time gradient."""
    source, receiver = np.asarray(source), np.asarray(receiver)
    source_inside = all(
        axis[0] + spacing <= coord <= axis[-1] - spacing for axis, coord in zip(grid, source)
    )
    # A surface receiver may be on the shallow edge, which clips the initial sphere.
    receiver_inside = all(
        axis[0] + margin <= coord <= axis[-1] - margin
        for axis, coord, margin in zip(grid, receiver, (spacing, spacing, 0))
    )
    if not source_inside or not receiver_inside or np.linalg.norm(source - receiver) < 3 * spacing:
        return None
    speed_at_receiver = RegularGridInterpolator(
        grid, velocity, bounds_error=False, fill_value=np.nan
    )(receiver[None])[0]
    if not np.isfinite(speed_at_receiver):
        return None
    north, east, down = np.ix_(*grid)
    radius = spacing
    phi = (
        np.sqrt((north - receiver[0]) ** 2 + (east - receiver[1]) ** 2 + (down - receiver[2]) ** 2)
        - radius
    )
    travel = march(phi, velocity, dx=spacing)
    sample = RegularGridInterpolator(
        grid, np.ma.filled(travel, np.nan), bounds_error=False, fill_value=np.nan
    )
    offset = np.eye(3) * (spacing / 2)
    direction = sample(source - offset) - sample(source + offset)
    speed_norm = np.linalg.norm(direction)
    if not np.isfinite(speed_norm) or speed_norm == 0:
        return None
    return dict(
        azimuth=float(np.degrees(np.arctan2(direction[1], direction[0])) % 360),
        takeoff=float(np.degrees(np.arccos(np.clip(direction[2] / speed_norm, -1, 1)))),
        time_s=float(sample(source[None])[0] + radius / speed_at_receiver),
    )


class TaiwanGrid:
    def __init__(self, path, spacing):
        import h5py
        import skfmm

        self.march = skfmm.travel_time
        self.spacing = spacing
        with h5py.File(path, "r") as model:
            lat, lon, depth = (
                np.asarray(model[name]) for name in ("latitude", "longitude", "depth")
            )
            self.lat0, self.lon0 = float(np.mean(lat)), float(np.mean(lon))
            north, east, _ = self.project(lat, lon, depth)
            # The supplied NetCDF fields are ordered depth, latitude, longitude.
            self.grid = tuple(
                axis[0] + np.arange(int((axis[-1] - axis[0]) / spacing) + 1) * spacing
                for axis in (north, east, depth)
            )
            points = np.stack(np.meshgrid(*self.grid, indexing="ij"), axis=-1)
            self.velocities = {}
            for phase, key in (("P", "vp"), ("S", "vs")):
                values = np.asarray(model[key]).transpose(1, 2, 0)
                if not np.isfinite(values).all() or not np.all((values > 0) & (values < 20)):
                    raise ValueError(f"{key} contains missing or invalid velocities")
                interpolate = RegularGridInterpolator((north, east, depth), values)
                self.velocities[phase] = interpolate(points)

    def project(self, lat, lon, depth):
        return (
            (lat - self.lat0) * 111.195,
            (lon - self.lon0) * 111.195 * np.cos(np.radians(self.lat0)),
            depth,
        )

    def ray(self, station, source, phase):
        receiver = self.project(
            station["lat"], station["lon"], -float(station.get("elevation_m") or 0) / 1000
        )
        return first_arrival(
            self.grid, self.velocities[phase], receiver, source, self.spacing, self.march
        )


def compare(result, model):
    loc = result["location"]
    source = model.project(loc["lat"], loc["lon"], loc["depth_km"])
    regional = result["regional_comparison"]
    polarities_1d, polarities_3d, ratios_1d, ratios_3d = [], [], [], []
    ray_cache = {}
    excluded = {"polarity": [], "ratio": []}

    def ray(row, phase):
        key = (row["lat"], row["lon"], row.get("elevation_m"), phase)
        if key not in ray_cache:
            ray_cache[key] = model.ray(row, source, phase)
        return ray_cache[key]

    for row in regional["polarity"]["observations"]:
        p = ray(row, "P")
        if p is None:
            excluded["polarity"].append(row["id"])
            continue
        polarities_1d.append(row)
        polarities_3d.append(
            dict(row, **p, arrival_residual_s=row["time"] - loc["origin"] - p["time_s"])
        )
    for row in regional["joint"]["observations"]:
        p, s = ray(row, "P"), ray(row, "S")
        if p is None or s is None:
            excluded["ratio"].append(row["id"])
            continue
        ratios_1d.append(row)
        ratios_3d.append(
            dict(row, **p, s_azimuth=s["azimuth"], s_takeoff=s["takeoff"], s_time_s=s["time_s"])
        )
    p1, p3 = solve(polarities_1d), solve(polarities_3d)
    for fit in (p1, p3):
        if "tensor_ned" in fit:
            fit["leave_one_site"] = leave_one_site(fit)
    angle = (
        kagan(np.asarray(p1["tensor_ned"]), np.asarray(p3["tensor_ned"]))
        if "tensor_ned" in p1 and "tensor_ned" in p3
        else None
    )
    joint_1d = fit_joint(polarities_1d, ratios_1d, validate=True)
    joint_3d = fit_joint(polarities_3d, ratios_3d, validate=True)
    joint_angle = (
        kagan(tensor(*joint_1d["plane"]), tensor(*joint_3d["plane"]))
        if "plane" in joint_1d and "plane" in joint_3d
        else None
    )
    return dict(
        location=loc,
        excluded=excluded,
        p_1d=p1,
        p_3d=p3,
        p_kagan_deg=angle,
        joint_1d=joint_1d,
        joint_3d=joint_3d,
        joint_kagan_deg=joint_angle,
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("model", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("solutions", nargs="+", type=Path)
    parser.add_argument("--spacing", type=float, default=8, help="Cartesian spacing in kilometres")
    args = parser.parse_args()
    if not 4 <= args.spacing <= 12:
        parser.error("--spacing must be between 4 and 12 km")
    model = TaiwanGrid(args.model, args.spacing)
    with args.model.open("rb") as file:
        digest = file_digest(file, "sha256").hexdigest()
    for path in args.solutions:
        result = compare(json.loads(path.read_text(encoding="utf-8")), model)
        save_json(
            args.output / path.parent.name / path.name,
            dict(result, model_sha256=digest, spacing_km=args.spacing),
        )
        print(path.parent.name, result["p_3d"]["station_count"], result["p_kagan_deg"], flush=True)


if __name__ == "__main__":
    main()

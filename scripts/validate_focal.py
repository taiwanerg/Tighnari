# ruff: noqa: E402
"""Reproducible synthetic focal control; not real-event accuracy calibration."""

import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import numpy as np
from obspy.geodetics import gps2dist_azimuth, kilometers2degrees
from obspy.taup import TauPyModel
from pyproj import Geod

from tighnari_seisloc.models import Record
from tighnari_seisloc.processing.focal import first_motions, tensor
from tighnari_seisloc.acquisition.fdsn import save_json


def control(kind):
    model, geod = TauPyModel("iasp91"), Geod(ellps="WGS84")
    reference = (40, 80, 10) if kind == "strike-slip" else (40, 50, 90)
    target = tensor(*reference)
    rng = np.random.default_rng(11)
    records, picks = [], []
    event = dict(
        origin=1000,
        lat=24,
        lon=121,
        depth_km=20,
        depth_status="constrained",
        horizontal_sigma_km=0.3,
        depth_range_km=[19, 21],
    )
    for index in range(80):
        azimuth, distance_km = index * 137.508 % 360, 20 + index % 11 * 25
        lon, lat, _ = geod.fwd(121, 24, azimuth, distance_km * 1000)
        meters, azimuth, _ = gps2dist_azimuth(24, 121, lat, lon)
        arrival = min(
            model.get_travel_times(20, kilometers2degrees(meters / 1000), phase_list=["p", "P"]),
            key=lambda a: a.time,
        )
        theta, az = np.deg2rad([arrival.takeoff_angle, azimuth])
        ray = np.array([np.sin(theta) * np.cos(az), np.sin(theta) * np.sin(az), np.cos(theta)])
        amplitude = ray @ target @ ray
        if abs(amplitude) < 0.12:
            continue
        rate, start = 100, 970
        times = np.arange(18000) / rate
        values = rng.normal(0, 1e-10, (len(times), 3))
        x = times - 30 - arrival.time
        values[:, 0] += np.sign(amplitude) * np.where(
            x >= 0, 1e-5 * np.sin(2 * np.pi * 5 * x) * np.exp(-np.maximum(x, 0) * 8), 0
        )
        sid = f"XX.S{index:03d}..HH"
        records.append(
            Record(
                sid,
                lat,
                lon,
                start,
                rate,
                values,
                provider="synthetic-control",
                metadata=dict(response_corrected=True, vertical_orientation_verified=True),
            )
        )
        picks.append(dict(id=sid, phase="P", used=True, time=1000 + arrival.time, sigma_s=0.1))
    event["residuals"] = picks
    result = first_motions(records, event, refine_onsets=True, stability=True)
    cosine = float(np.sum(np.asarray(result.get("tensor_ned", np.zeros((3, 3)))) * target) / 2)
    return dict(
        synthetic=True,
        accuracy_calibration=False,
        reference_plane=reference,
        candidate_tensor_error_deg=float(np.rad2deg(np.arccos(np.clip(cosine, -1, 1)))),
        result=result,
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kind", choices=["strike-slip", "thrust"], default="strike-slip")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    result = control(args.kind)
    save_json(args.output, result)
    print(args.kind, result["result"]["status"], result["result"].get("stability"))


if __name__ == "__main__":
    main()

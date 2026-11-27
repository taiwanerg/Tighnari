"""Offline deviatoric CMT depth search on aligned displacement and Green functions.

Both NPZs require channel_ids (NET.STA.LOC.CHA), dt, start_utc (epoch seconds)
and band_hz. Observations: displacement_m[channel,time]. Green bank:
depth_km[depth], basis=['NN-DD','EE-DD','NE','ND','ED'] (symmetric NED),
displacement_per_nm[depth,channel,time,basis] in metres per newton-metre.
Each column is the full response to that unit source, with both off-diagonal
entries present. Inputs must use matching processing, source time function,
station epochs, absolute polarity, phase alignment and filter band.
"""

import argparse
import json
from pathlib import Path
import numpy as np


def invert(obs, bank):
    for key in ("channel_ids", "dt", "start_utc", "band_hz"):
        if not np.array_equal(obs[key], bank[key]):
            raise ValueError("Incompatible inputs: " + key)
    ids = obs["channel_ids"].tolist()
    sites = {tuple(i.split(".")[:2]) for i in ids}
    if len(sites) < 5 or len(ids) != len(set(ids)):
        raise ValueError("Need at least five independent station codes and unique channels")
    if list(bank["basis"]) != ["NN-DD", "EE-DD", "NE", "ND", "ED"]:
        raise ValueError("Unsupported tensor basis")
    data = obs["displacement_m"]
    greens = bank["displacement_per_nm"]
    depths = bank["depth_km"]
    if data.ndim != 2 or data.shape[0] != len(ids) or greens.shape != (len(depths), *data.shape, 5):
        raise ValueError("Incompatible waveform dimensions")
    energy = float(np.sum(data**2))
    if (
        not energy
        or not len(depths)
        or not all(np.isfinite(x).all() for x in (data, greens, depths))
    ):
        raise ValueError("Need finite waveforms, energy and depths")
    trials = []
    for depth, design in zip(depths, greens):
        coefficients, _, rank, _ = np.linalg.lstsq(design.reshape(-1, 5), data.ravel(), rcond=1e-8)
        if rank < 5:
            continue
        predicted = design @ coefficients
        vr = float(100 * (1 - np.sum((data - predicted) ** 2) / energy))
        n, e, ne, nd, ed = coefficients
        tensor = np.array([[n, ne, nd], [ne, e, ed], [nd, ed, -n - e]])
        m0 = float(np.linalg.norm(tensor) / np.sqrt(2))
        if m0 <= 0:
            continue
        trials.append(
            dict(
                depth_km=float(depth),
                variance_reduction_pct=vr,
                tensor_ned_nm=tensor.tolist(),
                mw=float((np.log10(m0) - 9.1) * 2 / 3),
                correlation={
                    i: (float(np.corrcoef(a, b)[0, 1]) if np.std(a) > 0 and np.std(b) > 0 else None)
                    for i, a, b in zip(ids, data, predicted)
                },
            )
        )
    if not trials:
        raise ValueError("No resolved tensor")
    return dict(
        status="offline_research",
        stations=len(sites),
        preferred=max(trials, key=lambda r: r["variance_reduction_pct"]),
        depth_trials=trials,
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("observations", type=Path)
    parser.add_argument("greens", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    with np.load(args.observations) as obs, np.load(args.greens) as bank:
        result = invert(obs, bank)
    args.output.write_text(json.dumps(result, indent=2, allow_nan=False))

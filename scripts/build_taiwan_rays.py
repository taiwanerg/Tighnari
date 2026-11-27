"""Build the Wu et al. (2007), Table 1 Taiwan starting 1-D model for TauP.

Linear interpolation between tabulated depths; below 700 km append iasp91.
Density is interpolated from iasp91 and unused for the travel-time calculation.
Source: https://doi.org/10.1029/2007JB004983
"""

from pathlib import Path
import tempfile
import numpy as np
import obspy.taup
from obspy.taup.taup_create import build_taup_model

PROFILE = [
    (0, 3.90, 1.87),
    (2, 4.64, 1.76),
    (4, 5.17, 1.72),
    (6, 5.22, 1.71),
    (9, 5.64, 1.71),
    (13, 6.02, 1.73),
    (17, 6.30, 1.74),
    (21, 6.58, 1.74),
    (25, 6.74, 1.74),
    (30, 7.11, 1.74),
    (35, 7.52, 1.74),
    (50, 7.98, 1.73),
    (70, 8.25, 1.75),
    (90, 8.28, 1.72),
    (110, 8.38, 1.73),
    (140, 8.40, 1.74),
    (200, 8.70, 1.74),
    (700, 9.00, 1.73),
]
if __name__ == "__main__":
    base = np.loadtxt(Path(obspy.taup.__file__).parent / "data/iasp91.tvel", skiprows=2)
    rows = [(d, v, v / r, np.interp(d, base[:, 0], base[:, 3])) for d, v, r in PROFILE]
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "taiwan_wu2007.npz"
        np.savetxt(
            path.with_suffix(".tvel"),
            np.vstack((rows, base[base[:, 0] > 700])),
            header="Wu 2007 starting model\nregional travel times",
            comments="",
        )
        build_taup_model(
            str(path.with_suffix(".tvel")),
            str(Path(__file__).resolve().parents[1] / "src/tighnari_seisloc/assets"),
            verbose=False,
        )

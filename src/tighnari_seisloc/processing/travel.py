"""Versioned, atomic first-arrival P/S tables from the published iasp91 model."""

from pathlib import Path
import logging
import os
import tempfile
import threading
import numpy as np
from scipy.interpolate import RegularGridInterpolator

LOG = logging.getLogger(__name__)
_BUILD_LOCK = threading.Lock()
TABLE_VERSION = "taiwan-350km-2000km-v3"


class Iasp91:
    name = "iasp91-first-arrival-v1"
    max_depth = 350.0
    max_distance = 2000.0

    def __init__(self, cache="runtime/iasp91.npz"):
        cache = Path(cache)
        with _BUILD_LOCK:
            current = False
            if cache.exists():
                with np.load(cache) as data:
                    current = str(data.get("table_version", "")) == TABLE_VERSION
            if not current:
                self.build(cache)
        with np.load(cache) as data:
            self.depths, self.distances = data["depth"], data["distance"]
            self.p, self.s = data["p"], data["s"]
        self.interp = {
            phase: RegularGridInterpolator(
                (self.depths, self.distances), values, bounds_error=False, fill_value=np.nan
            )
            for phase, values in [("P", self.p), ("S", self.s)]
        }

    @staticmethod
    def build(cache):
        from obspy.taup import TauPyModel

        cache = Path(cache)
        model = TauPyModel("iasp91")
        depths = np.array(
            [
                0,
                1,
                3,
                5,
                8,
                10,
                15,
                20,
                25,
                30,
                35,
                40,
                50,
                60,
                75,
                100,
                125,
                150,
                200,
                250,
                300,
                350,
            ]
        )
        distances = np.arange(0, 2001, 5.0)
        p = np.full((len(depths), len(distances)), np.nan)
        s = p.copy()
        # Old tables remain physically valid inside their previous extent.
        old = {}
        if cache.exists():
            with np.load(cache) as data:
                if {"depth", "distance", "p", "s"} <= set(data.files):
                    for i, z in enumerate(data["depth"]):
                        for j, r in enumerate(data["distance"]):
                            old[(float(z), float(r))] = (data["p"][i, j], data["s"][i, j])
        for i, depth in enumerate(depths):
            LOG.info("iasp91 table: depth %.0f km", depth)
            for j, dist in enumerate(distances):
                if (depth, dist) in old:
                    p[i, j], s[i, j] = old[(depth, dist)]
                    continue
                arrivals = model.get_travel_times(
                    max(0.001, depth), max(0.00001, dist / 111.195), phase_list=["P", "p", "S", "s"]
                )
                for phase, target in [("P", p), ("S", s)]:
                    values = [a.time for a in arrivals if a.name[0].upper() == phase]
                    target[i, j] = min(values) if values else np.nan
                if depth == 0 and dist == 0:
                    p[i, j] = s[i, j] = 0
        cache.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=cache.parent, suffix=".npz", delete=False) as tmp:
            temporary = Path(tmp.name)
        try:
            np.savez(
                temporary, table_version=TABLE_VERSION, depth=depths, distance=distances, p=p, s=s
            )
            os.replace(temporary, cache)
        finally:
            temporary.unlink(missing_ok=True)

    def times(self, depth_km, epicentral_km, phase="P", elevation_m=0):
        z, r = np.broadcast_arrays(np.asarray(depth_km, float), np.asarray(epicentral_km, float))
        return self.interp[phase](np.column_stack([z.ravel(), r.ravel()])).reshape(
            z.shape
        ) + np.asarray(elevation_m) / 1000 / (5.8 if phase == "P" else 3.4)

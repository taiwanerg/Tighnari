"""Prepare IRIS and GDMS broadband records for the short-period CAP search.

Both providers enter on equal terms. Each physical site appears once, preferring
the record with more usable components. Horizontal channels are rotated to
radial and transverse with their documented azimuths, so a site needs both
horizontals for Love and radial windows and only its vertical otherwise.
"""

import numpy as np

from ..processing.cap import group_windows
from .cmt import geometry
from .cmt_research import prepare_components

STATION_LIMIT = 20
# GDMS HN accelerometers fill sites without a usable broadband record, for
# example near a moderate event where the broadband sensor clips.
CHANNELS = ("BH", "HH", "HN")


def rotate(site, data, mask):
    """Return (Z, R, T) data and masks from the archive's Z/N/E slots."""
    axes = site["target_axes"]
    out = np.zeros_like(data)
    valid = np.zeros_like(mask)
    # Radial points away from the source: the station-to-event azimuth plus 180.
    _, toward_event, _ = geometry(site, *site["event_location"])
    radial = np.radians((toward_event + 180) % 360)
    # A vertical with positive dip points down; flip it to the upward convention.
    out[0] = data[0] * (1 if axes[0][2] <= 0 else -1)
    valid[0] = mask[0]
    if mask[1].any() and mask[2].any():
        a1, a2 = np.radians([axes[1][1], axes[2][1]])
        matrix = np.array([[np.cos(a1), np.sin(a1)], [np.cos(a2), np.sin(a2)]])
        if abs(np.linalg.det(matrix)) > 0.5:
            north, east = np.linalg.solve(matrix, data[1:3])
            out[1] = north * np.cos(radial) + east * np.sin(radial)
            out[2] = -north * np.sin(radial) + east * np.cos(radial)
            both = mask[1] * mask[2]
            valid[1] = valid[2] = both
    return out, valid, float(np.degrees(radial))


def prepare_cap(event, inputs, profile, *, station_limit=STATION_LIMIT):
    """Return stations, {"pnl","surface"} data and masks, and an audit record."""
    sites, surface, surface_mask, audit = prepare_components(
        event,
        inputs,
        profile["surface"],
        "DISP",
        station_limit=station_limit,
        distance_km=profile["distance_km"],
        channels=CHANNELS,
    )
    pnl_sites, pnl, pnl_mask, _ = prepare_components(
        event,
        inputs,
        profile["pnl"],
        "DISP",
        station_limit=10 * station_limit,
        distance_km=profile["distance_km"],
        channels=CHANNELS,
    )
    by_source = {site["source_id"]: i for i, site in enumerate(pnl_sites)}
    location = (event["lat"], event["lon"])
    stations = []
    data = {"pnl": np.zeros_like(surface), "surface": np.zeros_like(surface)}
    masks = {"pnl": np.zeros_like(surface), "surface": np.zeros_like(surface)}
    for i, site in enumerate(sites):
        site = dict(site, event_location=location)
        data["surface"][i], masks["surface"][i], radial = rotate(site, surface[i], surface_mask[i])
        j = by_source.get(site["source_id"])
        if j is not None:
            data["pnl"][i], masks["pnl"][i], _ = rotate(site, pnl[j], pnl_mask[j])
        windows = group_windows(event["depth_km"], site["distance_km"])
        if j is None:
            windows.pop("pnl", None)
        stations.append(
            {
                key: site[key]
                for key in (
                    "id",
                    "source_id",
                    "provider",
                    "instrument",
                    "lat",
                    "lon",
                    "azimuth",
                    "distance_km",
                )
            }
            | dict(radial_azimuth=radial, windows=windows)
        )
    audit = dict(
        audit,
        profile=profile["name"],
        station_count=len(stations),
        providers=sorted({site["provider"] for site in stations}),
        instruments=sorted({site["instrument"] for site in stations}),
    )
    return stations, data, masks, audit

"""Taiwan-first station selection, with bounded Japanese and Korean support."""

import math

from ..legacy.location import distance
from ..location_policy import BOUNDS, SUPPORT_DISTANCE_KM


def distance_to_location_region(lat, lon):
    """Minimum spherical distance to the regional latitude/longitude rectangle.

    Bounds are regional and do not cross the date line. On the closest longitude,
    the nearest latitude may be poleward of the station's own latitude.
    """
    south, west = BOUNDS[0][:2]
    north, east = BOUNDS[1][:2]
    closest_lon = min(east, max(west, lon))
    phi = math.radians(lat)
    delta_lon = math.radians(lon - closest_lon)
    closest_lat = math.degrees(math.atan2(math.sin(phi), math.cos(phi) * math.cos(delta_lon)))
    closest_lat = min(north, max(south, closest_lat))
    return float(distance(lat, lon, closest_lat, closest_lon))


# Inventory coverage does not define the earthquake search region.
INVENTORY_BOUNDS = (18, 47, 115, 150)


def select_stations(rows, limit=60):
    """Reserve local coverage and at most eight auxiliary sites; collapse colocations."""
    # Apply before quotas and fallback filling, including when rows came from cache.
    # A JP station outside the rectangle may still support an event near its edge.
    rows = [
        r
        for r in rows
        if r["network"] != "JP"
        or distance_to_location_region(r["lat"], r["lon"]) <= SUPPORT_DISTANCE_KM
    ]
    rows = sorted(
        rows, key=lambda r: (r.get("provider") != "earthscope", r["network"], r["station"])
    )
    unique = {}
    for row in rows:
        unique.setdefault((row["network"], row["station"]), row)
    rows = list(unique.values())
    local = [r for r in rows if 18 <= r["lat"] <= 31 and 115 <= r["lon"] <= 132]
    korean = [
        r
        for r in rows
        if (
            r["network"] in ("KG", "KS")
            or (r["network"], r["station"]) in (("IU", "INCN"), ("PS", "TJN"))
        )
        and 32 <= r["lat"] <= 40.5
        and 123 <= r["lon"] <= 132
    ]
    japanese = [
        r
        for r in rows
        if r not in local
        and (r["network"] == "JP" or (r["network"], r["station"]) == ("IU", "MAJO"))
        and 20 <= r["lat"] <= 47
        and 122 <= r["lon"] <= 150
    ]
    chosen = []

    def add(row):
        if len(chosen) < limit and all(
            distance(row["lat"], row["lon"], old["lat"], old["lon"]) > 2 for old in chosen
        ):
            chosen.append(row)

    def nearby(items):
        return sorted(
            items,
            key=lambda r: (
                float(distance(24, 121, r["lat"], r["lon"])),
                r["network"],
                r["station"],
            ),
        )

    support = []
    for items, quota in ((korean, min(2, limit // 8)), (japanese, min(6, limit // 4))):
        for row in nearby(items):
            if sum(r in items for r in support) >= quota:
                break
            if all(distance(row["lat"], row["lon"], r["lat"], r["lon"]) > 2 for r in support):
                support.append(row)
    local_limit = limit - len(support)
    priority = [
        r
        for r in local
        if r["network"] in ("TW", "TQ", "IU") and 21 <= r["lat"] <= 26.5 and 118 <= r["lon"] <= 123
    ]
    for row in [r for r in local if (r["network"], r["station"]) == ("JP", "YOJ")] + nearby(
        priority
    ):
        if len(chosen) < local_limit:
            add(row)
    remaining = [r for r in nearby(local) if r not in chosen]
    while remaining and len(chosen) < local_limit:
        row = (
            max(
                remaining,
                key=lambda r: min(
                    float(distance(r["lat"], r["lon"], old["lat"], old["lon"])) for old in chosen
                ),
            )
            if chosen
            else remaining[0]
        )
        add(row)
        remaining.remove(row)
    for row in support:
        add(row)
    for row in remaining:
        add(row)
    return chosen

"""Offline administrative point-in-polygon naming, never nearest-town land guessing."""

from functools import lru_cache
import gzip
import json
from pathlib import Path
import math
from shapely.geometry import shape, Point
from shapely.strtree import STRtree

ASSETS = Path(__file__).resolve().parents[1] / "assets"


@lru_cache(maxsize=3)
def geography_document(name):
    with gzip.open(ASSETS / (name + ".gz"), "rt", encoding="utf-8") as source:
        return json.load(source)


@lru_cache(maxsize=1)
def boundaries():
    doc = geography_document("taiwan_towns.geojson")
    geom = [shape(f["geometry"]) for f in doc["features"]]
    return doc["features"], geom, STRtree(geom)


# Natural Earth names are Simplified Chinese; show Traditional names instead.
LAND_NAMES = {
    "CHN": "中國",
    "KOR": "南韓",
    "PRK": "北韓",
    "MNG": "蒙古",
    "RUS": "俄羅斯",
    "HKG": "香港",
    "MAC": "澳門",
    "PHL": "菲律賓",
    "JPN": "日本",
    "GUM": "關島",
    "MNP": "北馬里亞納群島",
    "PGA": "南沙群島",
    "SCR": "黃岩島",
}
# Taiwan land is named only by the official town polygons. The coarse Natural
# Earth outline also covers near-shore sea, which must be named as 近海.
TOWN_COVERED = {"TWN"}


def place_name(lat, lon):
    features, geom, tree = boundaries()
    p = Point(lon, lat)
    for i in tree.query(p):
        if geom[i].covers(p):
            return features[i]["properties"]["name"]
    land = geography_document("regional_land.geojson")
    for f in land["features"]:
        code = f["properties"].get("ADM0_A3")
        if code in TOWN_COVERED:
            continue
        if shape(f["geometry"]).covers(p):
            return LAND_NAMES.get(code) or f["properties"].get("ADMIN") or "陸地"
    nearest = int(tree.nearest(p))
    coast_distance = geom[nearest].distance(p) * 111
    if coast_distance <= 30:
        return features[nearest]["properties"]["COUNTYNAME"] + "近海"
    angle = math.atan2(lat - 23.97, (lon - 120.98) * math.cos(math.radians(24)))
    direction = ["東部", "東北部", "北部", "西北部", "西部", "西南部", "南部", "東南部"][
        round(angle / (math.pi / 4)) % 8
    ]
    return "臺灣" + direction + "海域"

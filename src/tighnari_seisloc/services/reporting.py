"""Shared dark map report styling and explicitly labelled research estimates."""

from datetime import datetime
from dataclasses import dataclass
from zoneinfo import ZoneInfo
from pathlib import Path
from functools import lru_cache
import math
import threading
import tempfile
import numpy as np
from PIL import Image, ImageDraw, ImageFont
from pyproj import Geod
from shapely.geometry import shape, mapping
from .records import VERY_SHALLOW, depth_label, finite

ROOT = Path(__file__).resolve().parents[1]
TZ = ZoneInfo("Asia/Taipei")
REPORT_STYLE_VERSION = "29"
CMT_REPORT_MARKER = "-regional-cmt-"
REPORT_SIZE = (1440, 1140)
_CIRCLE_ANTIALIAS = 4
_CIRCLE_STROKE = 6  # 1.5 px at the output resolution
_REPORT_LOCKS = [threading.Lock() for _ in range(32)]
COLORS = dict(
    ocean="#0c171c",
    land="#263a3d",
    border="#607b7c",
    text="#e8eeeb",
    muted="#91a6a4",
    accent="#8edbc3",
)


def depth_display(event):
    label = depth_label(event)
    if label == VERY_SHALLOW:
        depth = event.get("depth_km")
        return f"{label} · 最佳擬合 {max(depth, 0.0):.1f} km" if finite(depth) else label
    if label != "不明":
        return f"{label} · 有約束"
    depth = event.get("depth_km")
    reasons = event.get("quality_reasons") or []
    reason = next((value for value in reasons if "深度" in value), None)
    if reason is None:
        reason = (
            "深度約束不足"
            if isinstance(depth, (int, float)) and math.isfinite(depth)
            else "深度資料不足"
        )
    return f"{label} · {reason}"


@dataclass(frozen=True)
class MapStyle:
    circle_opacity: float = 0.60
    other_station_color: str = "#b3bec7"
    other_station_size: int = 7
    epicenter_halo: bool = False


def draw_epicenter(image, center, radius=20, *, outline="white", halo=False):
    """Round each star corner with a quadratic curve, then supersample its edges."""
    antialias = 4
    half = math.ceil(radius + 6)
    size = half * 2
    marker = Image.new("RGBA", (size * antialias, size * antialias))
    vertices = [
        np.array((math.sin(i * math.pi / 5), -math.cos(i * math.pi / 5)))
        * radius
        * (1 if i % 2 == 0 else 0.4)
        for i in range(10)
    ]
    path = []
    for i, vertex in enumerate(vertices):
        before = vertices[(i - 1) % 10] - vertex
        after = vertices[(i + 1) % 10] - vertex
        start = vertex + before / np.linalg.norm(before) * radius * 0.10
        end = vertex + after / np.linalg.norm(after) * radius * 0.10
        for t in np.linspace(0, 1, 9):
            point = (1 - t) ** 2 * start + 2 * (1 - t) * t * vertex + t**2 * end
            path.append(tuple((point + half) * antialias))
    draw = ImageDraw.Draw(marker)
    if halo:
        draw.line(path + [path[0]], fill=COLORS["ocean"], width=7 * antialias, joint="curve")
    draw.polygon(path, fill="#fb876e")
    if outline:
        draw.line(path + [path[0]], fill=outline, width=antialias, joint="curve")
    marker = marker.resize((size, size), Image.Resampling.LANCZOS)
    image.paste(marker, (round(center[0] - half), round(center[1] - half)), marker)


def local_time(ts):
    return datetime.fromtimestamp(ts, TZ).strftime("%Y-%m-%d %H:%M:%S")


def research_tag(research):
    """Short filename tag of an automatic research pick, so a new pick re-renders the map."""
    if not research or research.get("tensor_ned_nm") is None:
        return ""
    import hashlib
    import json

    key = json.dumps(research, sort_keys=True, default=str).encode()
    return "-research-" + hashlib.sha256(key).hexdigest()[:12]


def report_image(
    event, directory, *, travel_cache=None, view="overview", approved_cmt=None, research=None
):
    """One versioned report artifact shared by the website and Discord.

    ``research`` is the automatic research pick of the latest revision; it adds a
    second beach ball to the footer and its own tag to the filename.
    """
    if view not in ("overview", "closeup"):
        raise ValueError("Unknown map view")
    radius = float(event.get("map_radius_km", 150))
    suffix = f"closeup-radius{radius:g}" if view == "closeup" else "overview"
    separator = (
        CMT_REPORT_MARKER
        if approved_cmt and approved_cmt.get("status") in ("experimental", "ambiguous")
        else "-"
    )
    target = (
        Path(directory)
        / f"{event['id']}-r{event['revision']}-style{REPORT_STYLE_VERSION}{separator}"
        f"{suffix}{research_tag(research)}.png"
    )
    # Bound lock memory and coalesce website/Discord requests for the same artifact.
    with _REPORT_LOCKS[hash(str(target)) % len(_REPORT_LOCKS)]:
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=target.parent, suffix=".png", delete=False) as f:
                temporary = Path(f.name)
            try:
                render_map(
                    event,
                    event.get("stations", []),
                    temporary,
                    event.get("window_taipei"),
                    travel_cache=travel_cache or Path(directory).parent / "iasp91.npz",
                    view=view,
                    approved_cmt=approved_cmt,
                    research=research,
                )
                temporary.replace(target)
            finally:
                temporary.unlink(missing_ok=True)
    return target


def report_images(event, directory, *, travel_cache=None, approved_cmt=None, research=None):
    """Both views belong to the same scientific revision; overview is the default."""
    return {
        view: report_image(
            event,
            directory,
            travel_cache=travel_cache,
            view=view,
            approved_cmt=approved_cmt,
            research=research,
        )
        for view in ("overview", "closeup")
    }


@lru_cache(maxsize=32)
def font(size):
    return ImageFont.truetype(str(ROOT / "legacy/assets/NotoSansTC-Regular.ttf"), size)


@lru_cache(maxsize=4)
def brand_logo(path, height=125, alpha=0.24):
    """Same silhouette treatment as TWERG-Bot map_base.logo/mono_logo (read-only reference)."""
    with Image.open(path) as source:
        arr = np.asarray(source.convert("RGBA"), dtype=float) / 255
    r, g, b, a = np.moveaxis(arr, -1, 0)
    lum = 0.299 * r + 0.587 * g + 0.114 * b
    orange = (a > 0.5) & (r > 0.8) & (g > 0.5) & (g < 0.85) & (b < 0.5)
    rows, cols = np.zeros_like(orange), np.zeros_like(orange)
    for i, row in enumerate(orange):
        indices = np.flatnonzero(row)
        if len(indices):
            rows[i, indices[0] : indices[-1] + 1] = True
    for i, col in enumerate(orange.T):
        indices = np.flatnonzero(col)
        if len(indices):
            cols[indices[0] : indices[-1] + 1, i] = True
    cut = np.where(rows & cols, np.clip((0.6 - lum) / 0.3, 0, 1), 0)
    output = np.ones_like(arr)
    output[..., 3] = a * (1 - cut) * alpha
    logo = Image.fromarray((output * 255).round().astype(np.uint8))
    return logo.resize(
        (max(1, round(height * logo.width / logo.height)), height), Image.Resampling.LANCZOS
    )


@lru_cache(maxsize=1)
def geography():
    features = []
    for name in ("regional_land.geojson", "taiwan_towns.geojson"):
        from ..processing.geography import geography_document

        doc = geography_document(name)
        features.append(
            [
                mapping(shape(f["geometry"]).simplify(0.001, preserve_topology=True))
                for f in doc["features"]
            ]
        )
    return features


def map_extent(event, stations, view="overview"):
    if view == "overview":
        if not event:
            return (118, 125, 20.5, 27)
        used = set(event.get("used", []))
        points = [(s["lon"], s["lat"]) for s in stations if s["id"] in used]
        if not points:
            points = [(118.8, 21.8), (122.4, 25.4)]
        points.append((event["lon"], event["lat"]))
        longitudes, latitudes = zip(*points)
        return (
            min(longitudes) - 0.4,
            max(longitudes) + 0.4,
            min(latitudes) - 0.3,
            max(latitudes) + 0.3,
        )
    if not event:
        return (118, 125, 20.5, 27)
    radius = max(25, min(500, float(event.get("map_radius_km", 150)))) * 1000
    geod = Geod(ellps="WGS84")
    lon, lat = event["lon"], event["lat"]
    west = geod.fwd(lon, lat, 270, radius)[0]
    east = geod.fwd(lon, lat, 90, radius)[0]
    south = geod.fwd(lon, lat, 180, radius)[1]
    north = geod.fwd(lon, lat, 0, radius)[1]
    return west, east, south, north


def arrival_circles(event, model):
    """Invert observed P travel times at the fitted origin/depth, never epicentral distance."""
    distances = np.arange(0, model.max_distance + 0.01, 5.0)
    circles = []
    seen = set()
    for pick in event.get("residuals", []):
        if not pick.get("used") or pick.get("phase") != "P" or pick["id"] in seen:
            continue
        seen.add(pick["id"])
        observed = pick.get("observed", pick.get("time"))
        if observed is None or not math.isfinite(observed):
            continue
        elapsed = observed - event["origin"]
        times = model.times(event["depth_km"], distances, "P", pick.get("elevation_m", 0))
        for i in range(len(distances) - 1):
            a, b = float(times[i]), float(times[i + 1])
            if not (math.isfinite(a) and math.isfinite(b)) or a == b:
                continue
            fraction = (elapsed - a) / (b - a)
            if 0 <= fraction < 1:
                circles.append(
                    dict(
                        id=pick["id"],
                        lat=pick["lat"],
                        lon=pick["lon"],
                        radius_km=float(distances[i] + fraction * 5),
                    )
                )
    return circles


def event_circles(event, travel_cache):
    """Shared observed-arrival geometry for web and both report views."""
    if not event or not event.get("travel_model", "").startswith("iasp91-first-arrival"):
        return []
    if not any(p.get("used") and p.get("phase") == "P" for p in event.get("residuals", [])):
        return []
    from ..processing.travel import Iasp91

    return arrival_circles(event, Iasp91(travel_cache))


def circle_points(circle):
    """WGS84 geodesic rings, shared by SVG and PNG renderers."""
    azimuths = np.linspace(0, 360, 721)
    lon, lat, _ = Geod(ellps="WGS84").fwd(
        np.full(azimuths.shape, circle["lon"]),
        np.full(azimuths.shape, circle["lat"]),
        azimuths,
        np.full(azimuths.shape, circle["radius_km"] * 1000),
    )
    return list(zip(lon.tolist(), lat.tolist()))


def render_map(
    event,
    stations,
    target,
    window=None,
    message=None,
    *,
    travel_cache=None,
    view="overview",
    style=MapStyle(),
    approved_cmt=None,
    research=None,
):
    img = Image.new("RGB", REPORT_SIZE, "#10191b")
    d = ImageDraw.Draw(img)
    left, right, low, high = map_extent(event, stations, view)
    cx, cy = (
        (event["lon"], event["lat"])
        if event and view == "closeup"
        else ((left + right) / 2, (low + high) / 2)
    )
    longitude_factor = math.cos(math.radians(cy))
    scale = min(900 / ((right - left) * longitude_factor), 830 / (high - low))

    def xy(lon, lat):
        return (932.5 + (lon - cx) * scale * longitude_factor, 487.5 - (lat - cy) * scale)

    d.rectangle((455, 30, 1410, 945), fill=COLORS["ocean"])
    for lat in range(math.floor(low), math.ceil(high) + 1):
        y = xy(cx, lat)[1]
        d.line((465, y, 1400, y), fill="#203036")
        d.text((1370, y + 3), f"{lat}N", font=font(13), fill=COLORS["muted"])
    for lon in range(math.floor(left), math.ceil(right) + 1):
        x = xy(lon, cy)[0]
        d.line((x, 45, x, 930), fill="#203036")
    for group in geography():
        for geom in group:
            polys = geom["coordinates"] if geom["type"] == "MultiPolygon" else [geom["coordinates"]]
            for polygon in polys:
                for i, ring in enumerate(polygon):
                    points = [xy(*p[:2]) for p in ring]
                    d.polygon(
                        points,
                        fill=COLORS["land"] if i == 0 else COLORS["ocean"],
                        outline=COLORS["border"],
                    )
    circles = event_circles(event, travel_cache or "runtime/iasp91.npz")
    if circles:
        # Supersample only the circle mask to keep text and map boundaries crisp.
        antialias = _CIRCLE_ANTIALIAS
        circle_mask = Image.new("L", (img.width * antialias, img.height * antialias))
        circle_draw = ImageDraw.Draw(circle_mask)
        for circle in circles:
            ring = [xy(*point) for point in circle_points(circle)]
            circle_draw.line(
                [(x * antialias, y * antialias) for x, y in ring],
                fill=255,
                width=_CIRCLE_STROKE,
                joint="curve",
            )
        img.paste(
            "#6da7ff",
            (0, 0, img.width, img.height),
            circle_mask.resize(img.size, Image.Resampling.LANCZOS).point(
                lambda value: round(value * style.circle_opacity)
            ),
        )
    used = set(event.get("used", [])) if event else set()
    for s in stations:
        x, y = xy(s["lon"], s["lat"])
        if not 460 < x < 1405 or not 35 < y < 935:
            continue
        triangle = [(x, y - 7), (x - 7, y + 6), (x + 7, y + 6)]
        if s["id"] in used:
            d.polygon(triangle, fill="#ffc857", outline="#ffc857")
        else:
            size = style.other_station_size
            triangle = [(x, y - size), (x - size, y + size - 1), (x + size, y + size - 1)]
            d.polygon(triangle, outline=style.other_station_color, width=2)
    if event:
        x, y = xy(event["lon"], event["lat"])
        draw_epicenter(img, (x, y), halo=style.epicenter_halo)
    if event and view == "closeup":
        bar = 50 / 111.195 * scale
        d.rectangle((485, 875, 505 + bar, 925), fill=COLORS["ocean"])
        d.line((495, 912, 495 + bar, 912), fill=COLORS["text"], width=4)
        d.text((495, 880), "50 km", font=font(18), fill=COLORS["text"])
    # Clip geography to the map panel so it cannot cover the report or legend.
    panel = (455, 30, 1410, 945)
    map_image = img.crop(panel)
    img.paste("#10191b", (0, 0, *REPORT_SIZE))
    img.paste(map_image, panel[:2])
    d = ImageDraw.Draw(img)

    def fitted_text(position, text, size, fill, width=390):
        while size > 16 and d.textlength(text, font=font(size)) > width:
            size -= 1
        d.text(position, text, font=font(size), fill=fill)

    fitted_text((22, 20), "Tighnari SeisLOC", 48, COLORS["text"], 408)
    if event:
        d.text((30, 100), "發震時間 UTC+8", font=font(20), fill=COLORS["muted"])
        d.text((30, 140), local_time(event["origin"]), font=font(21), fill=COLORS["text"])
        mags = {m["type"]: m for m in event.get("magnitudes", [])}
        for kind, x, color in (("ML", 15, COLORS["accent"]), ("Mwp", 220, "#b9bbeb")):
            m = mags.get(kind, {})
            value = m.get("value")
            d.rectangle((x, 182, x + (186 if kind == "ML" else 202), 380), outline=color, width=3)
            label_baseline = 235
            d.text((x + 13, label_baseline), kind, font=font(31), fill=color, anchor="ls")
            label_bottom = d.textbbox((x + 13, label_baseline), "ML", font=font(31), anchor="ls")[3]
            label_width = d.textlength(kind, font=font(31))
            d.text(
                (x + 23 + label_width, label_bottom),
                f"{m.get('count', 0)} 個",
                font=font(20),
                fill=COLORS["muted"],
                anchor="lb",
            )
            d.text(
                (x + 13, 254),
                f"{value:.2f}" if value is not None else "-",
                font=font(60),
                fill=color,
            )
            if value is None or m.get("saturation_risk"):
                note = "資料不足" if value is None else "可能飽和"
                d.text((x + 13, 346), note, font=font(18), fill=COLORS["muted"])
        fitted_text((30, 410), event.get("place", "推估震央"), 40, COLORS["text"])
        fields = [
            (
                "深度",
                depth_display(event),
            ),
            ("經緯度", f"{event['lat']:.4f}°N  {event['lon']:.4f}°E"),
            ("可信度 / 品質", event.get("quality_grade", "D") + " · 非機率"),
            ("定位資料", f"{event['count']} 站 / {event.get('phase_count', event['count'])} 相位"),
            ("到時 RMS", f"{event['rms_s']:.3f} s"),
            ("方位缺口", f"{event['gap_deg']:.0f}°"),
        ]
        y = 480
        for label, value in fields:
            d.text((30, y - 6), label, font=font(20), fill=COLORS["muted"])
            fitted_text((30, y + 32), value, 30 if label == "深度" else 23, COLORS["text"])
            y += 80

    else:
        d.text((30, 145), "未取得可靠定位", font=font(30), fill="#e2bc84")
        d.multiline_text(
            (30, 220),
            message or "多站相位不足或到時不相容\n無法估算震央、深度及規模",
            font=font(20),
            fill=COLORS["text"],
            spacing=16,
        )
        d.text((30, 420), f"已取得 {len(stations)} 筆測站資料", font=font(23), fill=COLORS["muted"])
        d.text((30, 855), "未取得定位不代表沒有地震", font=font(19), fill="#e2bc84")
    fitted_text((30, 1092), "僅供參考，請以氣象署為準", 27, "#ff793d", 405)
    if window:
        d.text((455, 947), "實測回放", font=font(15), fill=COLORS["muted"])
    legend_y = 1035
    if circles:
        size = 20
        antialias = _CIRCLE_ANTIALIAS
        legend_mask = Image.new("L", (size * antialias, size * antialias))
        ImageDraw.Draw(legend_mask).ellipse(
            tuple(value * antialias for value in (2, 2, 18, 18)),
            outline=255,
            width=_CIRCLE_STROKE,
        )
        img.paste(
            "#6da7ff",
            (195, 1062, 215, 1082),
            legend_mask.resize((size, size), Image.Resampling.LANCZOS),
        )
        d.text((225, 1072), "P 走時圈", font=font(20), fill=COLORS["text"], anchor="lm")
    draw_epicenter(img, (43, legend_y), radius=11, outline=None)
    d.text((67, legend_y), "震央", font=font(20), fill=COLORS["text"], anchor="lm")
    for x, y, label, color, filled in (
        (205, 1035, "定位測站", "#ffc857", True),
        (43, 1072, "其他測站", style.other_station_color, False),
    ):
        d.polygon(
            [(x, y - 8), (x - 8, y + 8), (x + 8, y + 8)],
            fill=color if filled else None,
            outline=color,
            width=2,
        )
        d.text((x + 24, y), label, font=font(20), fill=COLORS["text"], anchor="lm")
    from .mechanism_plot import draw_footer

    draw_footer(img, event, font, COLORS, ROOT, approved_cmt=approved_cmt, research=research)
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    img.save(target)
    return target

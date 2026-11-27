"""Station-by-station waveform fit image of a CAP mechanism for research review."""

from PIL import Image, ImageDraw

from .mechanism_plot import beachball
from .reporting import font

COLUMNS = ("Pnl Z", "Pnl R", "Z", "R", "T")
LABEL_WIDTH, CELL_WIDTH, ROW_HEIGHT, HEADER = 210, 230, 62, 172
OBSERVED, SYNTHETIC = "#111111", "#d8443c"


def _trace(draw, box, observed, synthetic):
    x0, y0, x1, y1 = box
    peak = max(max(map(abs, observed), default=0), max(map(abs, synthetic), default=0))
    if peak <= 0 or len(observed) < 2:
        return
    middle, half = (y0 + y1) / 2, (y1 - y0) / 2 * 0.92
    step = (x1 - x0) / (len(observed) - 1)
    draw.line([(x0, middle), (x1, middle)], fill="#e4e4e0", width=1)
    for values, color in ((observed, OBSERVED), (synthetic, SYNTHETIC)):
        draw.line(
            [(x0 + i * step, middle - v / peak * half) for i, v in enumerate(values)],
            fill=color,
            width=2,
        )


def fit_image(row, path, *, limit=20):
    """Write the fit image, or remove a stale one when the row has no fits."""
    fits = sorted(row.get("waveform_fits") or [], key=lambda f: f["distance_km"])[:limit]
    if not fits or not row.get("tensor_ned_nm"):
        path.unlink(missing_ok=True)
        return False
    width = LABEL_WIDTH + CELL_WIDTH * len(COLUMNS) + 20
    image = Image.new("RGB", (width, HEADER + ROW_HEIGHT * len(fits) + 40), "#fbfbf9")
    draw = ImageDraw.Draw(image)
    ball = beachball(row["tensor_ned_nm"], 110)
    image.paste(ball, (width - 130, 14), ball)
    planes = row.get("nodal_planes") or []
    plane = " / ".join(f"{v:.0f}°" for v in planes[0]) if planes else "—"
    draw.text((20, 16), "CAP 波形擬合（研究診斷）", font=font(26), fill="#111111")
    bands = []
    if row.get("pnl_band_hz"):
        bands.append("Pnl " + "–".join(f"{v:g}" for v in row["pnl_band_hz"]) + " Hz")
    if row.get("band_hz"):
        bands.append("面波 " + "–".join(f"{v:g}" for v in row["band_hz"]) + " Hz")
    lines = [
        f"{row['id']} · {row.get('status', '')} · 節面 {plane}",
        " · ".join(
            part
            for part in (
                f"Mw {row['mw']:.2f}" if row.get("mw") is not None else None,
                f"矩心深度 {row['centroid_depth_km']:.0f} km"
                if row.get("centroid_depth_km") is not None
                else None,
                f"VR {row['vr_pct']:.0f}%" if row.get("vr_pct") is not None else None,
                f"留一站 VR {row['holdout_vr_pct']:.0f}%"
                if row.get("holdout_vr_pct") is not None
                else None,
                f"{len(fits)} 站",
            )
            if part
        ),
        " · ".join(bands) + " · 黑：觀測位移  紅：理論（逐窗時移後，絕對振幅）",
    ]
    for i, line in enumerate(lines):
        draw.text((20, 56 + 26 * i), line, font=font(17), fill="#444444")
    for c, name in enumerate(COLUMNS):
        draw.text(
            (LABEL_WIDTH + CELL_WIDTH * c + CELL_WIDTH / 2, HEADER - 14),
            name,
            font=font(16),
            fill="#555555",
            anchor="mm",
        )
    for r, fit in enumerate(fits):
        y = HEADER + ROW_HEIGHT * r
        draw.text((20, y + 8), fit["id"], font=font(16), fill="#111111")
        detail = f"{fit['distance_km']:.0f} km · {fit['azimuth']:.0f}°"
        if fit.get("vr_pct") is not None:
            detail += f" · VR {fit['vr_pct']:.0f}%"
        draw.text((20, y + 32), detail, font=font(14), fill="#777777")
        for c, name in enumerate(COLUMNS):
            trace = fit["traces"].get(name)
            if trace is None:
                continue
            x = LABEL_WIDTH + CELL_WIDTH * c
            _trace(
                draw,
                (x + 8, y + 4, x + CELL_WIDTH - 8, y + ROW_HEIGHT - 16),
                trace["observed"],
                trace["synthetic"],
            )
            draw.text(
                (x + CELL_WIDTH - 10, y + ROW_HEIGHT - 6),
                f"{trace['shift_s']:+.0f} s",
                font=font(12),
                fill="#888888",
                anchor="rb",
            )
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp.png")
    image.save(temp, format="PNG")
    temp.replace(path)
    return True


def fit_name(candidate_id):
    """File name for a candidate's fit image; candidate ids may contain ':'."""
    safe = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in candidate_id)
    return f"fits-{safe}.png"

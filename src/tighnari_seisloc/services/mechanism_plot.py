"""Research mechanism card and the original 1440 x 1140 report footer."""

import numpy as np
from PIL import Image, ImageDraw
from scipy.ndimage import binary_dilation


def beachball(tensor, size=130):
    """Render NED q=r'Mr, red=compression. No raster-generative model is used."""
    tensor = np.asarray(tensor, float)
    if tensor.shape != (3, 3) or not np.isfinite(tensor).all():
        raise ValueError("Beachball requires a finite 3x3 NED tensor")
    aa = 4
    n = size * aa
    y, x = np.mgrid[:n, :n]
    east, north = (x + 0.5 - n / 2) / (n / 2 - aa), -(y + 0.5 - n / 2) / (n / 2 - aa)
    r2 = east**2 + north**2
    inside = r2 <= 1
    factor = np.sqrt(np.maximum(0, 2 - r2))
    rays = np.stack([north * factor, east * factor, 1 - r2], axis=-1)
    q = np.einsum("...i,ij,...j->...", rays, tensor, rays)
    positive = q >= 0
    pixels = np.zeros((n, n, 4), dtype=np.uint8)
    pixels[..., :3] = np.where(positive[..., None], [232, 67, 67], [255, 255, 255])
    pixels[..., 3] = inside * 255
    edges = np.zeros_like(inside)
    edges[1:] |= positive[1:] != positive[:-1]
    edges[:, 1:] |= positive[:, 1:] != positive[:, :-1]
    edges = binary_dilation(edges, iterations=aa // 2) & inside
    pixels[edges, :3] = [12, 18, 20]
    image = Image.fromarray(pixels)
    draw = ImageDraw.Draw(image)
    draw.ellipse((aa, aa, n - aa - 1, n - aa - 1), outline="#070c0e", width=aa * 2)
    return image.resize((size, size), Image.Resampling.LANCZOS)


def mechanism_image(event, path):
    from .reporting import font, local_time

    image = Image.new("RGB", (1200, 520), "#10191b")
    draw = ImageDraw.Draw(image)
    fm = event.get("focal_mechanism") or {}
    draw.text((30, 22), "震源機制｜P 波初動 DC（實驗）", font=font(32), fill="#8edbc3")
    draw.text((30, 76), local_time(event["origin"]) + " UTC+8", font=font(23), fill="white")
    if fm.get("status") == "experimental":
        ball = beachball(fm["tensor_ned"], 260)
        image.paste(ball, (40, 150), ball)
        draw.text((170, 119), "N", font=font(20), fill="#d1dfdf", anchor="mm")
        lines = ["候選節面：走向 / 傾角 / 滑移角"]
        for plane in fm.get("nodal_planes", [fm["plane"]]):
            lines.append(f"{plane['strike']:.0f}° / {plane['dip']:.0f}° / {plane['rake']:.0f}°")
        lines += [
            "兩組節面均可能，無法判定實際斷層面",
            f"不符 {fm['misfit']:.0%} · 近最佳解分散 {fm['tensor_spread_deg']:.0f}°",
        ]
    else:
        draw.ellipse((40, 150, 300, 410), outline="#607777", width=3)
        lines = ["資料不足或機制未約束，暫不繪製候選解", *fm.get("reasons", [])]
    if fm.get("status") != "stale":
        for obs in fm.get("observations", []):
            az, theta = np.deg2rad([obs["azimuth"], obs["takeoff"]])
            if theta > np.pi / 2:
                theta, az = np.pi - theta, az + np.pi
            radius = 126 * np.sqrt(2) * np.sin(theta / 2)
            x, y = 170 + radius * np.sin(az), 280 - radius * np.cos(az)
            draw.ellipse(
                (x - 4, y - 4, x + 4, y + 4),
                fill="black" if obs["polarity"] > 0 else "white",
                outline="#888888",
            )
        if fm.get("observations"):
            lines.append("站點：黑＝向上，白＝向下（下半球）")
    lines += [
        f"有效初動 {fm.get('station_count', 0)} 站；自動判讀尚未人工覆核",
        "僅為雙力偶極性試算；不估計 Mw、ISO 或 CLVD",
    ]
    for i, line in enumerate(lines):
        draw.text((340, 150 + 36 * i), line, font=font(22), fill="#d1dfdf")
    draw.text(
        (30, 470),
        "TWERG Tighnari-SeisLOC · 僅供參考，此系統仍在實驗中",
        font=font(22),
        fill="#9bb3b3",
    )
    image.save(path)
    return path


STATUSES = {
    "not_computed": "尚未計算",
    "pending": "等待波形",
    "running": "計算中",
    "insufficient_data": "可用初動資料不足",
    "unconstrained": "機制解約束不足",
    "failed": "計算失敗",
    "stale": "定位已更新，等待重算",
    "experimental": "通過研究門檻（實驗）",
}
REASONS = {
    "too-few-independent-polarities": "有效極性站數不足",
    "large-azimuthal-gap": "方位覆蓋不足",
    "limited-takeoff-coverage": "出射角覆蓋不足",
    "excess-polarity-misfit": "極性不符比例偏高",
    "ambiguous-or-unstable-mechanism": "存在多解或擾動不穩定",
}


def public_mechanism(event, approved_cmt=None):
    """Choose a public representative after checking agreement between eligible methods."""
    from ..processing.cmt import kagan
    from ..processing.focal import polarity_agreement

    first_motion = (event or {}).get("focal_mechanism")
    tensors = {}
    for method, result, field in (
        ("regional_cmt", approved_cmt, "tensor_ned_nm"),
        ("first_motion", first_motion, "tensor_ned"),
    ):
        if not result or result.get("status") != "experimental":
            continue
        try:
            moment = np.asarray(result.get(field), dtype=float)
        except (TypeError, ValueError):
            continue
        if moment.shape == (3, 3) and np.isfinite(moment).all() and moment.any():
            tensors[method] = moment
    if "regional_cmt" in tensors:
        # First motions test the waveform tensor even when they cannot solve on their own.
        check = polarity_agreement(tensors["regional_cmt"], first_motion, event or {})
        if check and check["status"] == "conflict":
            return None, {
                "kagan_deg": None,
                "polarity_misfit": check["misfit"],
                "polarity_count": check["count"],
            }
    if len(tensors) == 2:
        angle = kagan(tensors["regional_cmt"], tensors["first_motion"])
        if angle is None or angle > 35:
            return None, {"kagan_deg": angle}
    return next(iter(tensors), None), None


def representative_mechanism(event, approved_cmt=None):
    return public_mechanism(event, approved_cmt)[0]


RESEARCH_NAMES = {
    "iris_cap": "IRIS 長週期 CMT",
    "iris_gdms_anchored": "IRIS＋GDMS CMT",
    "regional_multiband": "區域多頻帶 CMT",
    "regional_short": "區域短週期 CMT",
    "regional_phase_windows": "區域分波窗 CMT",
    "wphase": "GA W-phase",
    "global": "全球長週期 CMT",
    "global_long": "全球長週期 CMT",
    "regional_cap": "區域短週期 CAP",
    "regional_cap_joint": "區域 CAP＋初動",
    "first_motion": "P 波初動",
}
POLARITY_CHECKS = {"consistent": "初動一致", "mixed": "初動部分不符", "conflict": "與初動衝突"}


def _official(event, fm, approved_cmt, preferred):
    """Title, tensor, nodal planes, Mw, depth summary, stations and metrics of the public pick."""
    from .records import depth_label

    if preferred == "regional_cmt":
        solution = approved_cmt
        depth = solution.get("centroid_depth_km")
        summary = f"矩心深度 {depth:.0f} km" if depth is not None else "矩心深度 -"
        if depth is not None and solution.get("centroid_depth_resolved") is False:
            summary += "（未約束）"
        metrics = [
            (label, f"{solution[field]:.0f}%")
            for field, label in (
                ("vr_pct", "波形 VR"),
                ("holdout_vr_pct", "留一站 VR"),
                ("validation_vr_pct", "共同留站 VR"),
            )
            if solution.get(field) is not None
            and not (
                field == "validation_vr_pct"
                and solution.get("validation_method") == "leave_one_station_out"
            )
        ]
        return dict(
            title="代表震源機制｜臺灣區域 CMT（實驗）",
            short_title="代表機制｜臺灣區域 CMT（實驗）",
            tensor=solution["tensor_ned_nm"],
            planes=solution.get("nodal_planes", [])[:2],
            mw=solution.get("mw"),
            summary=summary,
            stations=f"反演 {solution.get('station_count', 0)} 站",
            metrics=metrics,
        )
    stations = f"初動 {fm['station_count']} 站"
    if fm.get("quality_grade"):
        stations += f" · 品質 {fm['quality_grade']}"
    metrics = [
        ("極性不符", f"{fm['misfit']:.0%}"),
        ("解分散", f"{fm['tensor_spread_deg']:.0f}°"),
    ]
    spread = (fm.get("stability") or {}).get("tensor_angle_95_deg")
    if spread is not None:
        metrics.append(("擾動分散", f"{spread:.0f}°"))
    return dict(
        title="代表震源機制｜初動極性 DC（實驗）",
        short_title="代表機制｜初動極性 DC（實驗）",
        tensor=fm["tensor_ned"],
        planes=[(p["strike"], p["dip"], p["rake"]) for p in fm["nodal_planes"][:2]],
        mw=None,
        summary=f"定位深度 {depth_label(event)}",
        stations=stations,
        metrics=metrics,
    )


def _empty_label(fm, conflict, approved_cmt):
    status = fm.get("status", "not_computed")
    if conflict is not None:
        return "初動與區域 CMT 解有歧異"
    if (approved_cmt or {}).get("status") == "ambiguous":
        return "區域 CMT 有多組候選解"
    if status != "experimental":
        return STATUSES.get(status, "尚無可發布機制解")
    return "尚無可發布機制解"


def _conflict_line(conflict):
    if conflict is None:
        return None
    if conflict.get("kagan_deg") is not None:
        return f"Kagan 角 {conflict['kagan_deg']:.0f}°"
    if conflict.get("polarity_misfit") is not None:
        return (
            f"CMT 預測初動不符 {conflict['polarity_misfit'] * 100:.0f}%"
            f"（{conflict['polarity_count']} 站）"
        )
    return None


def _fit_text(draw, xy, text, size, fill, width, font):
    """Shrink a line until it fits the column; footer panels are narrow."""
    while size > 11 and draw.textlength(text, font=font(size)) > width:
        size -= 1
    draw.text(xy, text, font=font(size), fill=fill, anchor="lm")


def _plane_lines(planes):
    rows = []
    for plane in planes[:2]:
        if isinstance(plane, dict):
            plane = (plane["strike"], plane["dip"], plane["rake"])
        rows.append(" / ".join(f"{angle:.0f}°" for angle in plane))
    return rows


def _draw_panel(image, draw, x0, font, colors, *, title, tensor, lines):
    """One half of the split footer: beach ball (or empty circle) and a text column."""
    text, muted = colors["text"], colors["muted"]
    _fit_text(draw, (x0 + 16, 992), title, 18, text, 366, font)
    if tensor is not None:
        ball = beachball(tensor, 100)
        image.paste(ball, (x0 + 14, 1012), ball)
    else:
        draw.ellipse((x0 + 15, 1013, x0 + 113, 1111), outline="#405459", width=2)
        draw.text((x0 + 64, 1062), "—", font=font(26), fill=muted, anchor="mm")
    y = 1026
    for line, size, color in lines[:5]:
        _fit_text(draw, (x0 + 128, y), line, size, color, 256, font)
        y += 21


def _draw_split(image, draw, event, font, colors, fm, preferred, conflict, approved_cmt, research):
    """Official representative on the left; the automatic research pick on the right."""
    text, muted = colors["text"], colors["muted"]
    draw.line((853, 983, 853, 1114), fill="#293c40")

    if preferred:
        pick = _official(event, fm, approved_cmt, preferred)
        lines = [
            (
                (f"Mw {pick['mw']:.2f} · " if pick["mw"] is not None else "") + pick["summary"],
                18,
                "#fb876e" if pick["mw"] is not None else text,
            ),
            *[(f"節面 {row}", 16, text) for row in _plane_lines(pick["planes"])],
            (pick["stations"], 14, muted),
            (" · ".join(f"{label} {value}" for label, value in pick["metrics"][:2]), 14, muted),
        ]
        _draw_panel(
            image,
            draw,
            455,
            font,
            colors,
            title=pick["short_title"],
            tensor=pick["tensor"],
            lines=lines,
        )
    else:
        lines = [(_empty_label(fm, conflict, approved_cmt), 18, muted)]
        if (detail := _conflict_line(conflict)) is not None:
            lines.append((detail, 14, muted))
        if conflict is None:
            lines += [(REASONS.get(r, r), 14, muted) for r in fm.get("reasons", [])]
        title = "代表震源機制"
        if "station_count" in fm:
            title += f"（有效初動 {fm['station_count']} 站）"
        _draw_panel(image, draw, 455, font, colors, title=title, tensor=None, lines=lines)

    name = RESEARCH_NAMES.get(research["id"].split(":", 1)[0], research["id"])
    depth = research.get("centroid_depth_km")
    head = [f"Mw {research['mw']:.2f}" if research.get("mw") is not None else None]
    if depth is not None:
        head.append(
            f"矩心 {depth:.0f} km"
            + ("（未約束）" if research.get("centroid_depth_resolved") is False else "")
        )
    fit = [f"{research['station_count']} 站" if research.get("station_count") else None]
    if research.get("holdout_vr_pct") is not None:
        fit.append(f"留一站 VR {research['holdout_vr_pct']:.0f}%")
    elif research.get("validation_vr_pct") is not None:
        fit.append(f"共同留站 VR {research['validation_vr_pct']:.0f}%")
    check = research.get("first_motion_check") or {}
    polarity = POLARITY_CHECKS.get(check.get("status"))
    if polarity and check.get("misfit") is not None and check["status"] != "consistent":
        polarity += f" {check['misfit']:.0%}"
    lines = [
        (" · ".join(filter(None, head)) or "Mw -", 18, "#fb876e" if research.get("mw") else text),
        *[(f"節面 {row}", 16, text) for row in _plane_lines(research.get("nodal_planes") or [])],
        (" · ".join(filter(None, fit)), 14, muted),
        (" · ".join(filter(None, ["自動計算、未核准", polarity])), 14, "#e2bc84"),
    ]
    _draw_panel(
        image,
        draw,
        853,
        font,
        colors,
        title=f"研究首選候選｜{name}",
        tensor=research["tensor_ned_nm"],
        lines=lines,
    )


def draw_footer(image, event, font, colors, root, *, approved_cmt=None, research=None):
    """``research`` is the automatic research pick; given, the footer shows two balls."""
    from .reporting import brand_logo

    draw = ImageDraw.Draw(image)
    fm = (event or {}).get("focal_mechanism") or {}
    preferred, conflict = public_mechanism(event, approved_cmt)
    text, muted = colors["text"], colors["muted"]
    border = "#293c40"
    draw.rectangle((455, 971, 1251, 1126), fill="#142124", outline=border)

    if research is not None and research.get("tensor_ned_nm") is not None:
        _draw_split(
            image, draw, event, font, colors, fm, preferred, conflict, approved_cmt, research
        )
    elif preferred:
        regional = preferred == "regional_cmt"
        pick = _official(event, fm, approved_cmt, preferred)
        title, tensor, planes = pick["title"], pick["tensor"], pick["planes"]
        mw, summary, stations, metrics = (
            pick["mw"],
            pick["summary"],
            pick["stations"],
            pick["metrics"],
        )

        ball = beachball(tensor)
        image.paste(ball, (477, 983), ball)
        draw.text((633, 994), title, font=font(21), fill=text, anchor="lm")
        if not regional:
            draw.line((633, 1013, 1227, 1013), fill=border)
        for x in (796, 1029):
            draw.line((x, 1025, x, 1110), fill=border)

        magnitude_color = "#fb876e" if mw is not None else muted
        draw.text((633, 1034), "Mw", font=font(18), fill=magnitude_color, anchor="lm")
        draw.text(
            (633, 1083),
            f"{mw:.2f}" if mw is not None else "-",
            font=font(48),
            fill=magnitude_color,
            anchor="ls",
        )
        draw.text((633, 1103), summary, font=font(16), fill=muted, anchor="lm")

        angle_columns = (858, 923, 989)
        for x, label in zip(angle_columns, ("走向", "傾角", "滑移角")):
            draw.text((x, 1036), label, font=font(15), fill=muted, anchor="mm")
        for i, plane in enumerate(planes, start=1):
            y = 1036 + 24 * i
            draw.text((810, y), str(i), font=font(15), fill=muted, anchor="lm")
            for x, angle in zip(angle_columns, plane):
                draw.text((x, y), f"{angle:.0f}°", font=font(19), fill=text, anchor="mm")
        if len(planes) == 2:
            draw.text((810, 1107), "兩組節面均為候選斷層面", font=font(14), fill=muted, anchor="lm")

        draw.text((1047, 1036), stations, font=font(15), fill=muted, anchor="lm")
        for i, (label, value) in enumerate(metrics, start=1):
            y = 1036 + 22 * i
            draw.text((1047, y), label, font=font(16), fill=muted, anchor="lm")
            draw.text((1227, y), value, font=font(18), fill=text, anchor="rm")
    else:
        draw.ellipse((479, 985, 605, 1111), outline="#405459", width=2)
        draw.text((542, 1048), "—", font=font(30), fill=muted, anchor="mm")
        draw.text((633, 994), "震源機制", font=font(21), fill=text, anchor="lm")
        draw.line((633, 1013, 1227, 1013), fill=border)
        label = _empty_label(fm, conflict, approved_cmt)
        draw.text((633, 1037), label, font=font(22), fill=muted, anchor="lm")
        if (detail := _conflict_line(conflict)) is not None:
            draw.text((633, 1063), detail, font=font(15), fill=muted, anchor="lm")
        if "station_count" in fm:
            draw.text(
                (1227, 1037),
                f"有效初動 {fm['station_count']} 站",
                font=font(16),
                fill=muted,
                anchor="rm",
            )
        reasons = (
            [REASONS.get(reason, reason) for reason in fm.get("reasons", [])]
            if conflict is None
            else []
        )
        for i in range(0, len(reasons), 2):
            y = 1063 + (i // 2) * 19
            draw.text(
                (633, y), "、".join(reasons[i : i + 2]), font=font(15), fill=muted, anchor="lm"
            )

    logo = brand_logo(str(root / "assets/twerg_logo_clear.png"))
    if logo.width > 130:
        logo = logo.resize(
            (130, max(1, round(130 * logo.height / logo.width))), Image.Resampling.LANCZOS
        )
    image.paste(logo, (1270 + (130 - logo.width) // 2, 970 + (125 - logo.height) // 2), logo)
    draw.text((1335, 1110), "@taiwanerg", font=font(20), fill="#687175", anchor="mm")

"""Compact earthquake catalogue and Taiwan-calendar detection statistics."""

from collections import Counter
from datetime import datetime, time, timedelta
import json
import math
from zoneinfo import ZoneInfo
from .review import visible_sql

TZ = ZoneInfo("Asia/Taipei")


def finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


# Like JMA's ごく浅い: a focus the data place near the surface without resolving a value.
VERY_SHALLOW = "很淺"
VERY_SHALLOW_MAX_KM = 10


def shallow_range(value):
    """Depth profile whose plausible range reaches the surface and stays above the limit."""
    return (
        isinstance(value, (list, tuple))
        and len(value) == 2
        and all(finite(v) for v in value)
        and value[0] <= 0
        and value[1] <= VERY_SHALLOW_MAX_KM
    )


def very_shallow(event):
    if event.get("depth_status") == "constrained" or not shallow_range(event.get("depth_range_km")):
        return False
    # A near-station-only check that downgraded the depth must agree as well.
    near = event.get("near_solution")
    if isinstance(near, dict) and "depth_range_km" in near:
        return shallow_range(near["depth_range_km"])
    return True


def depth_label(event):
    depth = event.get("depth_km")
    if event.get("depth_status") == "constrained" and finite(depth) and depth > 0:
        return f"{depth:.1f} km"
    if very_shallow(event):
        return VERY_SHALLOW
    return "不明"


def preferred_magnitude(event):
    values = [m for m in event.get("magnitudes", []) if finite(m.get("value"))]
    ml = next((m for m in values if m.get("type") == "ML"), None)
    if ml is not None:
        return ml["value"], "ML"
    if event.get("magnitude_type") == "ML" and finite(event.get("magnitude")):
        return event["magnitude"], "ML"
    if finite(event.get("magnitude")):
        return event["magnitude"], event.get("magnitude_type") or "M"
    if values:
        return values[0]["value"], values[0].get("type") or "M"
    return None, None


def data_sources(event):
    """Waveform providers and networks behind a solution, kept after details expire."""
    if event.get("summary_only"):
        return event.get("data_sources")
    providers, networks = set(), set()
    for station in event.get("stations") or []:
        if not isinstance(station, dict):
            continue
        if station.get("source"):
            providers.add(str(station["source"]))
        network = str(station.get("id") or "").split(".")[0]
        if network:
            networks.add(network)
    if isinstance(event.get("gdms_enrichment"), dict):
        providers.add("gdms")
    if not providers and not networks:
        return None
    return dict(providers=sorted(providers), networks=sorted(networks))


def compact_event(event):
    result = {
        k: event[k]
        for k in (
            "id",
            "origin",
            "lat",
            "lon",
            "place",
            "quality_grade",
            "mode",
            "status",
            "revision",
            "review",
            "automatic_magnitude",
            "magnitude_method",
            "depth_status",
            "quality_reasons",
            "count",
            "phase_count",
            "p_count",
            "s_count",
            "gap_deg",
            "travel_model",
        )
        if k in event
    }
    result["summary_only"] = True
    if finite(event.get("rms_s")):
        result["rms_s"] = event["rms_s"]
    magnitudes = [
        {
            key: (m[key] if key not in ("value", "uncertainty") or finite(m[key]) else None)
            for key in (
                "type",
                "value",
                "count",
                "method",
                "uncertainty",
                "saturation_risk",
                "reason",
            )
            if key in m
        }
        for m in event.get("magnitudes") or []
        if isinstance(m, dict)
    ]
    if magnitudes:
        result["magnitudes"] = magnitudes
    sources = data_sources(event)
    if sources:
        result["data_sources"] = sources
    enrichment = event.get("enrichment")
    if isinstance(enrichment, dict):
        result["enrichment"] = {
            key: enrichment[key]
            for key in ("source", "updated_at", "adopted_stations")
            if key in enrichment
        }
    result["depth_display"] = depth_label(event)
    gdms = event.get("gdms_enrichment")
    if isinstance(gdms, dict):
        result["gdms_enrichment"] = {
            key: gdms[key]
            for key in (
                "source",
                "updated_at",
                "previous_revision",
                "station_count",
                "files",
                "window",
                "location_changed",
            )
            if key in gdms
        }
    value, kind = preferred_magnitude(event)
    if value is not None:
        result.update(magnitude=value, magnitude_type=kind)
    if finite(event.get("depth_km")):
        result["depth_km"] = event["depth_km"]
    if shallow_range(event.get("depth_range_km")):
        result["depth_range_km"] = list(event["depth_range_km"])
    near = event.get("near_solution")
    if isinstance(near, dict) and "depth_range_km" in near:
        result["near_solution"] = {"depth_range_km": near["depth_range_km"]}
    return result


def catalogue(store, start, end, mode="live", status="located", exclude_no_magnitude=False):
    since = datetime.combine(start, time.min, TZ).timestamp()
    until = datetime.combine(end + timedelta(days=1), time.min, TZ).timestamp()
    clauses, params = ["origin>=?", "origin<?", visible_sql()], [since, until]
    if mode != "all":
        clauses.append("mode=?")
        params.append(mode)
    if status != "all":
        clauses.append("status=?")
        params.append(status)
    items = []
    daily, quality = Counter(), Counter()
    distribution = dict(under3=0, from3to4=0, from4to5=0, at_least5=0, unknown=0)
    largest = None
    summary_count = 0
    with store.connect() as db:
        for row in db.execute(
            "SELECT payload FROM events WHERE "
            + " AND ".join(clauses)
            + " ORDER BY origin DESC,id",
            params,
        ):
            event = json.loads(row["payload"])
            value, kind = preferred_magnitude(event)
            if exclude_no_magnitude and value is None:
                continue
            item = {
                k: event.get(k)
                for k in (
                    "id",
                    "origin",
                    "lat",
                    "lon",
                    "place",
                    "quality_grade",
                    "status",
                    "mode",
                    "review",
                    "depth_status",
                    "quality_reasons",
                )
            }
            item.update(
                magnitude=value,
                magnitude_type=kind,
                depth_km=event.get("depth_km"),
                depth_display=depth_label(event),
                summary_only=bool(event.get("summary_only")),
                # Published GDMS revision; summaries keep the provenance block.
                gdms_enriched=isinstance(event.get("gdms_enrichment"), dict),
            )
            items.append(item)
            day = datetime.fromtimestamp(event["origin"], TZ).date().isoformat()
            daily[day] += 1
            quality[event.get("quality_grade") or "unknown"] += 1
            summary_count += int(item["summary_only"])
            if value is None:
                distribution["unknown"] += 1
            else:
                key = (
                    "under3"
                    if value < 3
                    else "from3to4"
                    if value < 4
                    else "from4to5"
                    if value < 5
                    else "at_least5"
                )
                distribution[key] += 1
                if largest is None or value > largest["magnitude"]:
                    largest = item
    return dict(
        start=start.isoformat(),
        end=end.isoformat(),
        timezone="Asia/Taipei",
        mode=mode,
        status=status,
        total=len(items),
        with_magnitude=len(items) - distribution["unknown"],
        without_magnitude=distribution["unknown"],
        summary_count=summary_count,
        largest=largest,
        magnitude_counts=distribution,
        quality_counts=dict(quality),
        daily=[dict(date=day, count=count) for day, count in sorted(daily.items())],
        items=items,
    )

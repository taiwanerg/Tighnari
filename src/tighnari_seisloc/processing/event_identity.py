"""Identify repeated solutions by adopted observations, with legacy geometry fallback."""

from ..legacy.location import distance

ARRIVAL_TOLERANCE_S = 0.5


def observations(event):
    return [
        p
        for p in event.get("residuals", [])
        if p.get("used") and p.get("observed", p.get("time")) is not None
    ]


def arrival_overlap(first, second):
    """One-to-one phase matches, tolerant of peak jitter and colocated instruments."""
    a, b = observations(first), observations(second)
    matched, sites = set(), []
    for p in a:
        stamp = p.get("observed", p.get("time"))
        choices = []
        for i, q in enumerate(b):
            if i in matched or p["phase"] != q["phase"]:
                continue
            same_site = p["id"] == q["id"]
            if not same_site and all(k in p and k in q for k in ("lat", "lon")):
                same_site = distance(p["lat"], p["lon"], q["lat"], q["lon"]) <= 2
            delta = abs(stamp - q.get("observed", q.get("time")))
            if same_site and delta <= ARRIVAL_TOLERANCE_S:
                choices.append((delta, i))
        if choices:
            matched.add(min(choices)[1])
            if not any(
                p["id"] == q["id"]
                or (
                    all(k in p and k in q for k in ("lat", "lon"))
                    and distance(p["lat"], p["lon"], q["lat"], q["lon"]) <= 2
                )
                for q in sites
            ):
                sites.append(p)
    return dict(
        count=len(matched), sites=len(sites), fraction=len(matched) / max(1, min(len(a), len(b)))
    )


def same_event(first, second, origin_window_s=20):
    if abs(first["origin"] - second["origin"]) >= origin_window_s:
        return False
    close = distance(first["lat"], first["lon"], second["lat"], second["lon"]) < 40
    if not observations(first) or not observations(second):
        return bool(close)  # Compatibility with old events without arrival evidence.
    shared = arrival_overlap(first, second)
    strong = shared["sites"] >= 3 and shared["fraction"] >= 0.5
    nearby_match = close and shared["sites"] >= 2 and shared["fraction"] >= 0.25
    return bool(strong or nearby_match)

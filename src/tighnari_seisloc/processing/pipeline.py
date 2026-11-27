"""One source-independent processing path shared by live and historical analysis."""

import logging
from pathlib import Path
import json
from .picking import pick
from .location import associate
from .magnitude import taiwan_ml
from .geography import place_name
from ..acquisition.fdsn import save_json

LOG = logging.getLogger(__name__)


def analyze(records, model, backend="auto", cache=None, *, picked=None, measurement_records=None):
    picks = []
    methods = {}
    cache = Path(cache) if cache else None
    # Cache includes physical-trace identity and picker revision, not just station ID.
    import hashlib

    for r in () if picked is not None else records:
        fingerprint = hashlib.sha256(r.velocity.tobytes()).hexdigest()[:16]
        key = f"{r.id}-{r.start:.3f}-{fingerprint}-{backend}-v1.json"
        target = cache / key if cache else None
        if target and target.exists():
            row = json.loads(target.read_text())
            found, method = row["picks"], row["method"]
        else:
            found, method = pick(r, backend)
            if target:
                save_json(target, dict(picks=found, method=method))
        LOG.debug("picker %s: %s phases (%s)", r.id, len(found), method)
        picks.extend(found)
        methods[r.id] = method
    if picked is not None:
        picks, methods = picked
    # Automatic energy-detector fallback is a trigger, not a classified P/S phase.
    # Keep those diagnostics visible without fitting noise into earthquake locations.
    location_picks = (
        [p for p in picks if not p.get("method", "").startswith("adaptive")]
        if backend == "auto"
        else picks
    )
    events = associate(location_picks, model) if location_picks else []
    for event in events:
        event["place"] = place_name(event["lat"], event["lon"])
        ml = taiwan_ml(
            records if measurement_records is None else measurement_records, event, model
        )
        event["magnitudes"] = [ml]
        event["magnitude"] = ml["value"]
        event["magnitude_type"] = "ML"
        event["magnitude_count"] = ml["count"]
        event["magnitude_method"] = ml["method"]
        event["magnitude_saturation"] = ml["saturation_risk"]
        event["picker_methods"] = methods
        event["stations"] = [r.station() for r in records]
        event["picks"] = picks
    return dict(
        events=events,
        picks=picks,
        methods=methods,
        stations=[r.station() for r in records],
        station_count=len({r.id for r in records}),
        travel_model=model.name,
    )

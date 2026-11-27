"""Bounded regional moment-tensor backfill using the event revision store."""

import asyncio
import hashlib
import json
import logging
from pathlib import Path
import time

import numpy as np

from ..acquisition.cmt import gdms_inputs, prepare
from ..acquisition.cmt_greens import GreenStores
from ..acquisition.cmt_research import centroid_nodes, prepare_components
from ..acquisition.fdsn import save_json
from ..processing.cmt import DT, ENSEMBLE_VERSION, LIMITS, VERSION, invert
from ..processing.cmt_ensemble import MIN_POOL_SIZE, POOL_LIMIT, invert_ensemble, split_stations
from ..processing.cmt_moment import VALIDATION_BAND, VERSION as MOMENT_VERSION, invert_moment
from .review import magnitude_priority_sql, visible_sql

LOG = logging.getLogger(__name__)
SCHEDULE = (600, 1200, 2400, 7200)
PUBLIC_FIELDS = (
    "method",
    "status",
    "station_count",
    "reasons",
    "band_hz",
    "model",
    "centroid_depth_km",
    "centroid_depth_range_km",
    "centroid_delay_s",
    "mw",
    "scalar_moment_nm",
    "tensor_ned_nm",
    "nodal_planes",
    "vr_pct",
    "holdout_vr_pct",
    "kagan_p90_deg",
    "azimuth_gap_deg",
    "fit_stations",
    "validation_vr_pct",
    "validation_stations",
    "selected_candidate",
    "pool_station_count",
    "source_duration_s",
    "surface_delay_offset_s",
    "mw_sensitivity_range",
    "centroid_depth_resolved",
    "depth_mechanism_kagan_max_deg",
    "validation_method",
)
SOLUTION_FIELDS = (
    "id",
    "strategy",
    "model",
    "station_count",
    "nodal_planes",
    "tensor_ned_nm",
    "mw",
    "centroid_depth_km",
    "validation_vr_pct",
    "supporting_candidates",
)


def event_inputs(event):
    return {
        key: event.get(key)
        for key in (
            "origin",
            "lat",
            "lon",
            "depth_km",
            "region",
            "waveform_file",
            "gdms_enrichment",
        )
    }


def public_cmt(result):
    allowed = PUBLIC_FIELDS if result.get("status") == "experimental" else PUBLIC_FIELDS[:5]
    public = {key: result[key] for key in allowed if key in result}
    if result.get("status") in ("experimental", "ambiguous") and result.get("solutions"):
        if "validation_stations" in result:
            public["validation_stations"] = result["validation_stations"]
        public["solutions"] = [
            {key: row[key] for key in SOLUTION_FIELDS if key in row} for row in result["solutions"]
        ]
    return public


def candidate_image(result, path):
    from PIL import Image, ImageDraw

    from .mechanism_plot import beachball
    from .reporting import font

    rows = result["candidates"]
    if not rows:
        path.unlink(missing_ok=True)
        return
    width, height = 270, 232
    columns = min(len(rows), 3)
    image = Image.new(
        "RGB", (width * columns, 50 + height * ((len(rows) + columns - 1) // columns)), "#10191b"
    )
    draw = ImageDraw.Draw(image)
    draw.text((14, 15), "區域 CMT 候選解（研究診斷）", font=font(20), fill="white")
    for i, row in enumerate(rows):
        x, y = i % columns * width, 50 + i // columns * height
        description = row["id"].replace("taiwan_wu2007", "Wu2007")
        draw.text((x + 12, y + 8), description, font=font(15), fill="white")
        if row.get("tensor_ned_nm"):
            ball = beachball(row["tensor_ned_nm"], 124)
            image.paste(ball, (x + 73, y + 33), ball)
            draw.text(
                (x + 12, y + 164),
                f"Mw {row['mw']:.2f} · {row['centroid_depth_km']:.0f} km",
                font=font(15),
                fill="white",
            )
        vr = row["validation_vr_pct"]
        draw.text(
            (x + 12, y + 186),
            "共同留站 VR " + (f"{vr:.0f}%" if vr is not None else "—"),
            font=font(15),
            fill="white",
        )
        draw.text(
            (x + 12, y + 207),
            "通過" if not row["reasons"] else "品質未通過",
            font=font(15),
            fill="#8edbc3" if not row["reasons"] else "#e9b493",
        )
    temp = path.with_suffix(".tmp.png")
    image.save(temp)
    temp.replace(path)


def analyze(
    event,
    greens,
    inputs,
    output,
    *,
    download=False,
    bats_url=None,
    ensemble=False,
    moment=False,
    centroid=False,
):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    inputs = list(inputs)
    use_moment_waveforms = moment or centroid
    expanded_pool = ensemble or use_moment_waveforms
    stations, data, provenance = prepare(
        event,
        inputs,
        output / "archive" if download else None,
        bats_url=bats_url,
        **({"station_limit": POOL_LIMIT} if expanded_pool else {}),
        **({"band": VALIDATION_BAND} if use_moment_waveforms else {}),
    )
    mask = None
    if use_moment_waveforms and (len(stations) < MIN_POOL_SIZE or not split_stations(stations)[1]):
        archives = inputs.copy()
        if download:
            for label in ("earthscope", "bats") if bats_url else ("earthscope",):
                wave, xml = (output / "archive" / f"{label}.{ext}" for ext in ("mseed", "xml"))
                if wave.is_file() and xml.is_file():
                    archives.append((label, wave, xml))
        sites, partial, usable, audit = prepare_components(
            event,
            archives,
            VALIDATION_BAND,
            "DISP",
            station_limit=POOL_LIMIT,
        )
        provenance["component_fallback"] = dict(audit, station_count=len(sites), used=False)
        if len(sites) >= MIN_POOL_SIZE and split_stations(sites)[1]:
            stations, data, mask = sites, partial, usable
            provenance["component_fallback"]["used"] = True
    if centroid:
        from ..processing.cmt_centroid import invert_centroid

        result, prediction = invert_centroid(
            data, stations, centroid_nodes(event, greens, stations), mask=mask
        )
    else:
        minimum = MIN_POOL_SIZE if ensemble or moment else LIMITS["stations"]
        banks = {}
        if len(stations) >= minimum:
            banks = dict(
                greens.banks(event, stations, **({"band": VALIDATION_BAND} if moment else {}))
            )
        method = invert_moment if moment else invert_ensemble if ensemble else invert
        result, prediction = method(
            data, banks, stations, **({"mask": mask} if mask is not None else {})
        )
    result.update(
        provenance=provenance,
        green_version=greens.version,
        diagnostic_grid=greens.diagnostic_grid,
        location={key: event[key] for key in ("lat", "lon", "origin")},
    )
    if prediction is not None:
        temp = output / "fits.tmp.npz"
        indices = {station["id"]: i for i, station in enumerate(stations)}
        adopted = [indices[station["id"]] for station in result["stations"]]
        np.savez_compressed(
            temp,
            observed_m=data[adopted],
            synthetic_m=prediction[adopted],
            dt=DT,
            **({"observed_mask": mask[adopted]} if mask is not None else {}),
        )
        temp.replace(output / "fits.npz")
        result["fit_stations"] = [
            dict(
                id=station["id"],
                distance_km=station["distance_km"],
                snr=station["snr"],
                holdout_vr_pct=next(
                    (r["vr_pct"] for r in result["holdout"] if r["id"] == station["id"]), None
                ),
            )
            for station in result["stations"]
        ]
    else:
        (output / "fits.npz").unlink(missing_ok=True)
    save_json(output / "result.json", result)
    if expanded_pool:
        try:
            candidate_image(result, output / "candidates.png")
        except (OSError, ValueError):
            LOG.exception("Could not render CMT candidate comparison")
    return result


class RegionalCMT:
    def __init__(self, settings, store, publication_lock):
        self.settings, self.store, self.publication_lock = settings, store, publication_lock
        self.greens = None
        self.stopping = False
        self.status = {"enabled": settings.regional_cmt_enabled, "state": "waiting"}
        if settings.regional_cmt_enabled:
            with store.connect() as db:
                db.execute("""CREATE TABLE IF NOT EXISTS regional_cmt_jobs (
                    event_id TEXT PRIMARY KEY, signature TEXT NOT NULL, stage INTEGER NOT NULL
                )""")

    def publishable(self, result):
        if (
            not self.settings.regional_cmt_enabled
            or not self.settings.regional_cmt_publish
            or (self.greens is not None and self.greens.diagnostic_grid)
        ):
            return False
        try:
            path = self.settings.root / self.settings.regional_cmt_store / "validation.json"
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        if not isinstance(record, dict):
            return False
        return (
            record.get("passed") is True
            and result.get("method") in (VERSION, ENSEMBLE_VERSION, MOMENT_VERSION)
            and result["method"] == record.get("method")
            and bool(result.get("green_version"))
            and result["green_version"] == record.get("green_version")
        )

    def _job(self, row, now):
        event = json.loads(row[0])
        eid = event["id"]
        if (
            event.get("summary_only")
            or event.get("region", "tw") != "tw"
            or not all(event.get(k) is not None for k in ("lat", "lon"))
        ):
            return False
        folder_id = hashlib.sha256(eid.encode()).hexdigest()
        inputs = []
        if Path(eid).name == eid and eid not in (".", ".."):
            try:
                inputs = gdms_inputs(self.settings.root / "runtime/gdms/inbox" / eid)
            except (OSError, ValueError, KeyError):
                LOG.info("GDMS CMT input unavailable for %s", eid)
        inputs_metadata = []
        for _, wave, xml in inputs:
            try:
                inputs_metadata.append(
                    [(p.name, p.stat().st_size, p.stat().st_mtime_ns) for p in (wave, xml)]
                )
            except OSError:
                continue
        basis = event_inputs(event)
        ensemble = self.settings.regional_cmt_ensemble
        moment = self.settings.regional_cmt_moment
        version = MOMENT_VERSION if moment else ENSEMBLE_VERSION if ensemble else VERSION
        signature = hashlib.sha256(
            json.dumps(
                [version, self.greens.version, basis, inputs_metadata],
                sort_keys=True,
            ).encode()
        ).hexdigest()
        stage = max(i for i, age in enumerate(SCHEDULE) if now - event["origin"] >= age)
        with self.store.connect() as db:
            prior = db.execute(
                "SELECT * FROM regional_cmt_jobs WHERE event_id=?", (eid,)
            ).fetchone()
        old_result = event.get("regional_cmt") or {}
        if (
            prior
            and prior["signature"] == signature
            and (prior["stage"] >= stage or old_result.get("status") == "experimental")
        ):
            return False
        data_key = hashlib.sha256(
            json.dumps(
                [event["origin"], event["lat"], event["lon"]]
                + ([version] if ensemble or moment else [])
            ).encode()
        ).hexdigest()[:16]
        directory = self.settings.root / "runtime/cmt" / folder_id / data_key
        self.status.update(
            state="inverting",
            event_id=eid,
            event_origin=event["origin"],
            event_place=event.get("place"),
            station_count=None,
            active=True,
        )
        try:
            result = analyze(
                event,
                self.greens,
                inputs,
                directory,
                download=True,
                bats_url=(
                    self.settings.regional_cmt_bats_url
                    or (self.settings.bats_url if self.settings.bats_enabled else None)
                ),
                ensemble=ensemble,
                moment=moment,
            )
        except Exception:
            LOG.exception("Regional CMT evaluation failed for %s", eid)
            result = dict(
                method=version,
                status="failed",
                reasons=["calculation_failed"],
                green_version=self.greens.version,
            )
        summary = {key: result[key] for key in PUBLIC_FIELDS if key in result}
        if result.get("solutions"):
            summary["solutions"] = [
                {key: row[key] for key in SOLUTION_FIELDS if key in row}
                for row in result["solutions"]
            ]
        summary.update(
            green_version=result["green_version"], input_signature=signature, attempt_stage=stage
        )
        with self.publication_lock:
            latest = self.store.get(eid)
            if (
                self.stopping
                or not latest
                or latest.get("summary_only")
                or latest.get("status") != "located"
                or event_inputs(latest) != basis
            ):
                self.status.update(state="stale", active=False)
                return True
            latest_result = latest.get("regional_cmt") or {}
            if (
                latest_result.get("input_signature") == signature
                and latest_result.get("attempt_stage", -1) >= stage
            ):
                self.status.update(state=latest_result["status"], active=False)
                return True
            saved = self.store.put(
                dict(latest, regional_cmt=summary),
                "live",
                eid,
                expected_revision=latest["revision"],
                notify=False,
            )
            if saved is not None:
                with self.store.connect() as db:
                    db.execute(
                        "INSERT OR REPLACE INTO regional_cmt_jobs VALUES (?,?,?)",
                        (eid, signature, stage),
                    )
        self.status.update(
            state=result["status"], station_count=result.get("station_count"), active=False
        )
        return True

    def tick(self):
        if self.stopping or not self.settings.regional_cmt_enabled:
            return
        if self.greens is None:
            try:
                self.greens = GreenStores(self.settings.root / self.settings.regional_cmt_store)
            except (OSError, ImportError):
                self.status.update(state="waiting_for_greens")
                return
        now = time.time()
        with self.store.connect() as db:
            events = db.execute(
                "SELECT payload FROM events WHERE mode='live' AND status='located' AND "
                + visible_sql()
                + " AND origin BETWEEN ? AND ? ORDER BY "
                + magnitude_priority_sql()
                + ",origin DESC",
                (now - 86400, now - SCHEDULE[0]),
            ).fetchall()
        for row in events:
            if self.stopping or self._job(row, now):
                break

    async def run(self):
        while not self.stopping:
            try:
                await asyncio.to_thread(self.tick)
            except Exception:
                LOG.exception("Regional CMT background job failed")
            await asyncio.sleep(60)

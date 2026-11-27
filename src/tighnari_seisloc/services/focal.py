"""Bounded post-event focal evaluation on local calibrated waveforms only."""

import asyncio
import hashlib
import json
from pathlib import Path
import threading
import time

from ..processing.focal import FOCAL_VERSION, LOCATION_FIELDS, first_motions, gdms_mechanism_check
from .waveforms import load_records, merge_gdms_measurements
from .reporting import report_images
from .review import magnitude_priority_sql


class FocalEnrichment:
    def __init__(self, settings, store, publication_lock=None):
        self.settings, self.store = settings, store
        self.publication_lock = publication_lock or threading.Lock()
        self.stopping = False
        self.status = dict(
            state="等待需要評估的事件" if settings.gdms_mechanism_publish else "已停用",
            enabled=settings.gdms_mechanism_publish,
            active=False,
        )
        with store.connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS focal_jobs (
                event_id TEXT PRIMARY KEY, signature TEXT NOT NULL,
                updated REAL NOT NULL, error TEXT)""")

    def tick(self):
        if self.stopping or not self.settings.gdms_mechanism_publish:
            return
        now = time.time()
        with self.store.connect() as db:
            rows = db.execute(
                """SELECT payload FROM events WHERE mode='live'
                AND status='located' AND origin BETWEEN ? AND ? ORDER BY """
                + magnitude_priority_sql()
                + ",origin DESC",
                (now - 86400, now - 300),
            ).fetchall()
        processed = 0
        validate_joint = self.settings.adaptive_cmt_enabled
        for row in rows:
            if self.stopping or processed >= 8:
                break
            event = json.loads(row[0])
            if event.get("summary_only") or not event.get("waveform_file"):
                continue
            location = {key: event[key] for key in LOCATION_FIELDS}
            fm = event.get("focal_mechanism") or {}
            if (
                fm.get("processing_version") == FOCAL_VERSION
                and (not validate_joint or "joint_sh" in (fm.get("regional_comparison") or {}))
                and fm.get("location") == location
                and fm.get("location_diagnostics")
                == {
                    k: event.get(k)
                    for k in ("depth_status", "horizontal_sigma_km", "depth_range_km")
                }
                and fm.get("status") != "stale"
            ):
                continue
            path = Path(event["waveform_file"])
            reports = (self.settings.root / "runtime/reports").resolve()
            if (
                path.is_symlink()
                or not path.resolve().is_relative_to(reports)
                or not path.is_file()
            ):
                continue
            try:
                with path.open("rb") as handle:
                    signature = hashlib.file_digest(handle, "sha256")
                signature.update(
                    json.dumps(
                        dict(
                            version=FOCAL_VERSION,
                            validate_joint=validate_joint,
                            location=location,
                            residuals=event.get("residuals", []),
                            depth_status=event.get("depth_status"),
                            horizontal_sigma_km=event.get("horizontal_sigma_km"),
                            depth_range_km=event.get("depth_range_km"),
                        ),
                        sort_keys=True,
                    ).encode()
                )
                signature = signature.hexdigest()
                with self.store.connect() as db:
                    prior = db.execute(
                        "SELECT * FROM focal_jobs WHERE event_id=?", (event["id"],)
                    ).fetchone()
                if prior and (
                    prior["signature"] == signature
                    or (prior["error"] and prior["updated"] > now - 300)
                ):
                    continue
                processed += 1
                self.status.update(
                    state="評估初動與機制穩定度",
                    event_id=event["id"],
                    error=None,
                    event_origin=event["origin"],
                    event_place=event.get("place"),
                    station_count=None,
                    mechanism_guard=None,
                    active=True,
                    stage_started_at=time.time(),
                    updated=time.time(),
                )
                records = merge_gdms_measurements(load_records(path))
                mechanism = first_motions(
                    records,
                    event,
                    refine_onsets=True,
                    stability=True,
                    validate_joint=validate_joint,
                )
                with self.publication_lock:
                    latest = self.store.get(event["id"])
                    if (
                        self.stopping
                        or not latest
                        or latest.get("summary_only")
                        or latest.get("status") != "located"
                        or latest["revision"] != event["revision"]
                    ):
                        self.status.update(state="等待最新定位後重算", updated=time.time())
                        continue
                    guard = (
                        gdms_mechanism_check(latest, mechanism)
                        if any(record.provider == "gdms" for record in records)
                        else None
                    )
                    if guard:
                        self.record(event["id"], signature)
                        self.status.update(
                            state="保留原機制",
                            station_count=mechanism["station_count"],
                            mechanism_guard=guard,
                            updated=time.time(),
                        )
                        continue
                    candidate = dict(
                        latest, focal_mechanism=mechanism, revision=latest["revision"] + 1
                    )
                    images = report_images(
                        candidate, reports, travel_cache=self.settings.root / "runtime/iasp91.npz"
                    )
                    candidate.update(
                        image_file=str(images["overview"]),
                        closeup_image_file=str(images["closeup"]),
                    )
                    saved = self.store.put(
                        candidate,
                        "live",
                        event["id"],
                        expected_revision=event["revision"],
                        notify=False,
                    )
                    if saved is None:
                        self.status.update(state="等待最新定位後重算", updated=time.time())
                        continue
                self.record(event["id"], signature)
                self.status.update(
                    state=mechanism["status"],
                    station_count=mechanism["station_count"],
                    updated=time.time(),
                )
            except Exception as exc:
                self.record(event["id"], "", type(exc).__name__)
                self.status.update(state="retry", error=type(exc).__name__, updated=time.time())
            finally:
                self.status["active"] = False

    def record(self, eid, signature, error=None):
        with self.store.connect() as db:
            db.execute(
                "INSERT OR REPLACE INTO focal_jobs VALUES (?,?,?,?)",
                (eid, signature, time.time(), error),
            )

    async def run(self):
        try:
            while True:
                await asyncio.to_thread(self.tick)
                await asyncio.sleep(60)
        finally:
            self.stopping = True

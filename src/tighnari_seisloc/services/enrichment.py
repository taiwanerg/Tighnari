"""Persistent, fault-isolated post-event BATS revisions of existing live events."""

import asyncio
import threading
import time
from collections import defaultdict
from pathlib import Path

import numpy as np

from ..acquisition.bats import acquire_bats
from ..legacy.location import distance
from ..processing.pipeline import analyze
from ..processing.travel import Iasp91
from .reporting import report_images
from .waveforms import load_records, save_records
from .review import magnitude_priority_sql

WINDOW_BEFORE = 60
WINDOW_AFTER = 240
MAX_AGE = 24 * 3600
MAX_ATTEMPTS = 6


def merge_records(original, supplemental, origin, preferred_provider="bats"):
    """Choose one instrument/provider per network station; preserve its gap segments."""
    groups = defaultdict(list)
    for record in [*original, *supplemental]:
        groups[(tuple(record.id.split(".")[:2]), record.provider, record.id)].append(record)
    sites = defaultdict(list)
    for (site, provider, sid), rows in groups.items():
        # Sub-sample endpoint differences must not select a noisier instrument.
        # Half-second buckets preserve meaningful gaps; clipping always wins.
        coverage = sum(
            max(0, min(r.end, origin + WINDOW_AFTER) - max(r.start, origin - WINDOW_BEFORE))
            for r in rows
        )
        full = sum(np.isfinite(r.velocity[:, 1:]).all(axis=1).sum() / r.rate for r in rows)
        sites[site].append(
            (
                (
                    not any(r.clipped for r in rows),
                    round(min(full, coverage) * 2),
                    round(coverage * 2),
                    {"HH": 4, "BH": 3, "EH": 2, "HN": 1}.get(rows[0].family, 0),
                    provider == preferred_provider,
                    sid,
                ),
                rows,
            )
        )
    return sorted(
        [r for choices in sites.values() for r in max(choices, key=lambda v: v[0])[1]],
        key=lambda r: (r.id, r.start),
    )


def acceptable(prior, candidate):
    """More observations must still support the same event and a usable solution."""
    if candidate.get("status") != "located":
        return False
    if abs(candidate["origin"] - prior["origin"]) >= 20:
        return False
    if distance(prior["lat"], prior["lon"], candidate["lat"], candidate["lon"]) >= 40:
        return False
    if prior.get("status") == "located":
        ranks = {"A": 3, "B": 2, "C": 1, "D": 0}
        if ranks.get(candidate.get("quality_grade"), 0) < ranks.get(prior.get("quality_grade"), 0):
            return False
        if candidate["count"] < prior["count"] or candidate["phase_count"] < prior.get(
            "phase_count", 0
        ):
            return False
        if candidate["rms_s"] > max(1.2, prior["rms_s"] + 0.3):
            return False
        if (
            prior.get("depth_status") == "constrained"
            and candidate.get("depth_status") != "constrained"
        ):
            return False
        if prior.get("magnitude") is not None and candidate.get("magnitude") is None:
            return False
    return True


class BatsEnrichment:
    def __init__(self, settings, store, publication_lock=None):
        self.settings, self.store = settings, store
        self.publication_lock = publication_lock or threading.Lock()
        self.stopping = False
        self.status = dict(state="等待震後資料" if settings.bats_enabled else "已停用", count=0)
        self.model = None
        with store.connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS enrichment_jobs (
                event_id TEXT PRIMARY KEY, attempts INTEGER NOT NULL DEFAULT 0,
                retry_at REAL NOT NULL DEFAULT 0, state TEXT NOT NULL DEFAULT 'pending',
                error TEXT, updated REAL NOT NULL DEFAULT 0)""")

    def finish(self, eid, state, error=None, retry=False):
        with self.store.connect() as db:
            db.execute(
                """UPDATE enrichment_jobs SET state=?,error=?,updated=?,retry_at=? WHERE event_id=?""",
                (
                    state,
                    error,
                    time.time(),
                    time.time() + self.settings.bats_poll_s if retry else 0,
                    eid,
                ),
            )
        label = {
            "retry": "補強稍後重試",
            "retained": "已檢查，保留原定位",
            "completed": "已完成震後補強",
        }.get(state, state)
        self.status.update(state=label, error=error, updated=time.time())

    def tick(self):
        if not self.settings.bats_enabled or self.stopping:
            return
        now = time.time()
        ready_age = self.settings.bats_delay_s + WINDOW_AFTER + 60
        with self.store.connect() as db:
            db.execute(
                """INSERT OR IGNORE INTO enrichment_jobs(event_id)
                SELECT id FROM events WHERE mode='live' AND origin>? AND origin<? AND coalesce(json_extract(payload,'$.region'),'tw')='tw'""",
                (now - MAX_AGE, now - ready_age),
            )
            jobs = db.execute(
                """SELECT j.event_id FROM enrichment_jobs j JOIN events e ON e.id=j.event_id
                WHERE j.state IN ('pending','retry') AND j.retry_at<=? AND j.attempts<?
                AND e.mode='live' AND coalesce(json_extract(e.payload,'$.region'),'tw')='tw' AND e.origin>? AND e.origin<? ORDER BY """
                + magnitude_priority_sql("e")
                + """,e.origin DESC LIMIT 1""",
                (now, MAX_ATTEMPTS, now - MAX_AGE, now - ready_age),
            ).fetchall()
        for job in jobs:
            if self.stopping:
                return
            eid = job["event_id"]
            with self.store.connect() as db:
                db.execute(
                    "UPDATE enrichment_jobs SET attempts=attempts+1 WHERE event_id=?", (eid,)
                )
            try:
                self.process(eid)
            except Exception as exc:
                # Error types are sufficient for diagnosis; never publish URLs or HTTP bodies.
                self.finish(eid, "retry", type(exc).__name__, retry=True)
            with self.store.connect() as db:
                changed = db.execute(
                    """UPDATE enrichment_jobs SET state='unavailable'
                    WHERE event_id=? AND attempts>=? AND state='retry'""",
                    (eid, MAX_ATTEMPTS),
                ).rowcount
                if changed:
                    self.status["state"] = "補強資料暫不可用，保留原定位"

    def process(self, eid):
        initial = self.store.get(eid)
        if not initial or initial.get("mode") != "live" or initial.get("region", "tw") != "tw":
            return
        origin = initial["origin"]
        directory = self.settings.root / "runtime/enrichment" / eid
        self.status.update(state="取得 BATS 震後波形", event_id=eid, error=None)
        extra, manifest = acquire_bats(
            origin - WINDOW_BEFORE,
            origin + WINDOW_AFTER,
            directory,
            self.settings.bats_url,
            self.settings.max_stations,
        )
        self.status["count"] = manifest["station_count"]
        if not extra:
            self.finish(eid, "retry", "未取得可校正波形", retry=True)
            return
        if self.stopping:
            return
        # Work outside the publication lock; only commit against the exact revision analyzed.
        prior = self.store.get(eid)
        archive = Path(prior["waveform_file"]) if prior.get("waveform_file") else None
        archived = load_records(archive) if archive and archive.is_file() else []
        records = merge_records(archived, extra, origin)
        if self.model is None:
            self.model = Iasp91(self.settings.root / "runtime/iasp91.npz")
        report = analyze(
            records,
            self.model,
            self.settings.picker,
            cache=directory / "picks",
            measurement_records=archived + extra,
        )
        candidates = [e for e in report["events"] if acceptable(prior, e)]
        candidates.sort(key=lambda e: (-e["count"], -e["phase_count"], e["rms_s"]))
        if not candidates:
            self.finish(
                eid,
                "retry" if manifest["errors"] else "retained",
                "新增資料未形成較完整的同事件定位，保留原結果",
                retry=bool(manifest["errors"]),
            )
            return
        candidate = candidates[0]
        # Include genuinely adopted BATS evidence; do not publish a revision based only on old data.
        supplemental_ids = {r.id for r in records if r.provider == "bats"}
        adopted = set(candidate.get("used", [])) & supplemental_ids
        if not adopted:
            self.finish(eid, "retained", "BATS 到時未獲定位採用，保留原結果")
            return
        if self.settings.gdms_mechanism_publish:
            from ..processing.focal import first_motions

            try:
                candidate["focal_mechanism"] = first_motions(
                    archived + extra, candidate, refine_onsets=True, stability=True
                )
            except Exception as exc:
                candidate["focal_mechanism"] = dict(
                    status="failed",
                    experimental=True,
                    station_count=0,
                    reasons=["機制計算暫不可用"],
                    error=type(exc).__name__,
                )
        else:
            candidate.pop("focal_mechanism", None)
        with self.publication_lock:
            if self.stopping:
                return
            latest = self.store.get(eid)
            if latest["revision"] != prior["revision"]:
                self.finish(eid, "retry", "事件已修訂，稍後以最新版本重算", retry=True)
                return
            candidate["magnitudes"] += [
                m for m in latest.get("magnitudes", []) if m["type"] != "ML"
            ]
            candidate.update(
                id=eid,
                revision=latest["revision"] + 1,
                enrichment=dict(
                    source="BATS / IESDMC",
                    updated_at=time.time(),
                    previous_revision=latest["revision"],
                    station_count=manifest["station_count"],
                    adopted_stations=sorted(adopted),
                    window=[origin - WINDOW_BEFORE, origin + WINDOW_AFTER],
                ),
            )
            reports = self.settings.root / "runtime/reports"
            wavefile = reports / f"{eid}-r{candidate['revision']}-waveforms.npz"
            save_records(archived + extra, wavefile)
            candidate["waveform_file"] = str(wavefile)
            candidate["map_radius_km"] = self.settings.map_radius_km
            images = report_images(
                candidate, reports, travel_cache=self.settings.root / "runtime/iasp91.npz"
            )
            candidate["image_file"] = str(images["overview"])
            candidate["closeup_image_file"] = str(images["closeup"])
            saved = self.store.put(candidate, "live", eid, expected_revision=latest["revision"])
            if saved is None:
                self.finish(eid, "retry", "事件已修訂，稍後重算", retry=True)
                return
        self.finish(eid, "completed")

    async def run(self):
        try:
            while True:
                await asyncio.to_thread(self.tick)
                await asyncio.sleep(self.settings.bats_poll_s)
        finally:
            self.stopping = True

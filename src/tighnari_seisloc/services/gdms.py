"""Durable delayed GDMS revisions for existing located live events."""

import asyncio
from pathlib import Path
import threading
import time
import random
import hashlib
import json
import logging

from ..acquisition.gdms import acquire_gdms, GdmsUnavailable
from ..processing.focal import FOCAL_VERSION, LOCATION_FIELDS, first_motions, gdms_mechanism_check
from ..processing.pipeline import analyze
from ..processing.travel import Iasp91
from .enrichment import acceptable, merge_records, WINDOW_BEFORE, WINDOW_AFTER
from .reporting import report_images
from .notifications import ml_value
from .waveforms import load_records, merge_gdms_measurements, save_records
from .gdms_alerts import GdmsAlerts, AUTH_ERRORS, ATTENTION_RETRY_S
from ..acquisition.gdms_portal import GdmsPortal, PortalError, NeedsAttention
from .gdms_downloads import GdmsDownloads
from .review import magnitude_priority_sql
from ..acquisition.fdsn import save_json


LOG = logging.getLogger(__name__)


def broadband_window(settings, origin):
    """Return the longer HH request needed by any enabled waveform-inversion worker.

    The default request (-60/+240 s) cuts surface waves beyond about 250 km once the
    long-period filter margin is removed, and leaves little pre-event noise at 0.02 Hz.
    """
    if not (settings.regional_cmt_enabled or settings.adaptive_cmt_enabled):
        return None
    from ..processing.cmt import AFTER, BEFORE

    return (origin - BEFORE - 20, origin + AFTER + 20)


def request_tail_s(settings):
    """Seconds after origin that the latest requested sample needs."""
    window = broadband_window(settings, 0.0)
    return WINDOW_AFTER if window is None else max(WINDOW_AFTER, window[1])


class GdmsEnrichment:
    def __init__(self, settings, store, publication_lock=None):
        self.settings, self.store = settings, store
        self.publication_lock = publication_lock or threading.Lock()
        self.stopping = False
        self.model = None
        self.next_cleanup = 0.0
        self.alerts = GdmsAlerts(store, settings.gdms_attention_webhook)
        self.alerts.clear_non_auth_block()
        self.downloads = None
        if settings.gdms_auto_download:
            portal = GdmsPortal(profile=settings.root / "runtime/gdms/edge-profile")
            self.downloads = GdmsDownloads(settings.root, store, portal)
            self.downloads.on_progress = self.progress
            self.downloads.recover()
        self.status = dict(
            state="等待 GDMS 工作" if settings.gdms_enabled else "已停用",
            auto_download=settings.gdms_auto_download,
            mechanism_publish=settings.gdms_mechanism_publish,
            scheduled_delay_s=[
                settings.gdms_delay_s + request_tail_s(settings),
                settings.gdms_delay_s + request_tail_s(settings) + settings.gdms_schedule_jitter_s,
            ],
        )
        with store.connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS gdms_jobs (
                event_id TEXT PRIMARY KEY, attempts INTEGER NOT NULL DEFAULT 0,
                retry_at REAL NOT NULL DEFAULT 0, state TEXT NOT NULL DEFAULT 'pending',
                error TEXT, updated REAL NOT NULL DEFAULT 0)""")
            columns = {row[1] for row in db.execute("PRAGMA table_info(gdms_jobs)")}
            if "ready_at" not in columns:
                db.execute("ALTER TABLE gdms_jobs ADD COLUMN ready_at REAL")
            if "input_signature" not in columns:
                db.execute("ALTER TABLE gdms_jobs ADD COLUMN input_signature TEXT")
        self.refresh_queue()

    def refresh_queue(self):
        """Cache queue counts in the worker, never scan jobs for each web request."""
        with self.store.connect() as db:
            counts = {
                r[0]: r[1]
                for r in db.execute(
                    "SELECT j.state,count(*) FROM gdms_jobs j JOIN events e ON e.id=j.event_id "
                    "WHERE e.mode='live' AND e.status='located' AND e.origin>? "
                    "AND coalesce(json_extract(e.payload,'$.summary_only'),0)=0 "
                    "AND coalesce(json_extract(e.payload,'$.region'),'tw')='tw' GROUP BY j.state",
                    (time.time() - 86400,),
                )
            }
        waiting = counts.get("pending", 0) + counts.get("retry", 0)
        self.status["queue"] = dict(
            waiting=max(0, waiting - int(bool(self.status.get("active")))),
            completed=counts.get("completed", 0),
            retained=counts.get("retained", 0),
        )

    def progress(self, state, **fields):
        now = time.time()
        if self.status.get("state") != state:
            self.status["stage_started_at"] = now
        self.status.update(state=state, error=None, updated=now, **fields)

    def input_signature(self, inbox, prior):
        """Avoid rerunning identical rejected inputs while waiting for replacements."""
        inbox = inbox.resolve()
        ready = (inbox / "manifest.json").read_bytes()
        fingerprint = hashlib.sha256(ready)
        fingerprint.update(f"{prior['revision']}:{self.settings.picker}:{FOCAL_VERSION}".encode())
        if self.settings.focal_joint_shadow or self.settings.adaptive_cmt_enabled:
            fingerprint.update(b":joint-validation")
        seen = set()
        for entry in json.loads(ready).get("files", []):
            for name in (entry["waveform"], entry["inventory"]):
                path = (inbox / name).resolve()
                if not path.is_relative_to(inbox):
                    raise ValueError("GDMS export must stay inside event inbox")
                if path in seen:
                    continue
                seen.add(path)
                with path.open("rb") as handle:
                    fingerprint.update(hashlib.file_digest(handle, "sha256").digest())
        return fingerprint.hexdigest()

    def finish(self, eid, state, error=None):
        now = time.time()
        with self.store.connect() as db:
            db.execute(
                """UPDATE gdms_jobs SET state=?,error=?,updated=?,retry_at=?
                WHERE event_id=?""",
                (state, error, now, now + self.settings.gdms_poll_s, eid),
            )
        self.status.update(
            state=state,
            error=error,
            updated=now,
            active=False,
            retry_at=max(now + self.settings.gdms_poll_s, self.status.get("download_retry_at", 0))
            if state == "retry"
            else 0,
        )

    def authentication_ready(self):
        resume = self.settings.root / "runtime/gdms/resume-auth"
        session = self.settings.root / "runtime/gdms/manual-session"
        state = self.alerts.state()
        requested = resume.is_file() or session.is_file()
        timed_retry = False
        if not self.downloads:
            return True
        if state["attention"] and not requested:
            # Older databases may have a short retry_at; use the persisted time
            # the attention state began so upgrading/restarting does not reset it.
            retry_at = max(state["retry_at"], state["updated"] + ATTENTION_RETRY_S)
            if time.time() < retry_at:
                self.status.update(
                    state="needs_attention", retry_at=retry_at, error=state["reason"]
                )
                return False
            timed_retry = True
        if (
            not requested
            and not timed_retry
            and not state["auto_attempts"]
            and not state["retry_at"]
        ):
            return True
        if not requested and not timed_retry and state["retry_at"] > time.time():
            self.status.update(state="auth_retry", retry_at=state["retry_at"])
            return False
        resume.unlink(missing_ok=True)
        if requested or timed_retry:
            # A manual login/resume and the four-hour session check release the pause.
            with self.store.connect() as db:
                db.execute(
                    "UPDATE gdms_auth_state SET attention=0,auto_attempts=0,retry_at=0 "
                    "WHERE singleton=1"
                )
                db.execute(
                    "DELETE FROM gdms_alert_outbox WHERE sent IS NULL "
                    "AND kind='needs_attention' AND episode=?",
                    (state["episode"],),
                )
        try:
            self.progress("authenticating", active=False)
            if not self.downloads.portal.import_session(session):
                # Only verifies a session a human already provided; never logs in.
                self.downloads.portal.ensure_login()
            self.downloads.recover()
            self.alerts.recovered()
            self.status.update(state="auth_recovered", error=None, retry_at=0)
            return True
        except PortalError as exc:
            if timed_retry:
                # One session check per four-hour cycle. Even a network failure
                # returns to the pause; keep the original auth reason.
                reason = str(exc) if str(exc) in AUTH_ERRORS else state["reason"]
                self.alerts.attention(reason)
            elif str(exc) in AUTH_ERRORS:
                self.alerts.login_failed(str(exc))
            else:
                # Network trouble is not evidence that manual login is required.
                with self.store.connect() as db:
                    db.execute(
                        "UPDATE gdms_auth_state SET retry_at=? WHERE singleton=1",
                        (time.time() + 300,),
                    )
            self.status.update(
                state="needs_attention" if self.alerts.blocked else "auth_retry",
                error=str(exc),
                retry_at=self.alerts.state()["retry_at"],
            )
            return False

    def tick(self):
        if not self.settings.gdms_enabled or self.stopping:
            return
        if self.downloads is not None and time.time() >= self.next_cleanup:
            with self.publication_lock:
                self.status["cleanup"] = self.downloads.cleanup(
                    minimum_age_s=max(86400, self.settings.waveform_retention_hours * 3600)
                )
            self.next_cleanup = time.time() + 900
        ready_to_download = self.authentication_ready()
        self.refresh_queue()
        self.alerts.deliver()
        if not ready_to_download:
            return
        now = time.time()
        ready = now - self.settings.gdms_delay_s - request_tail_s(self.settings)
        with self.store.connect() as db:
            if self.downloads is not None:
                db.execute(
                    """UPDATE gdms_downloads SET state='expired' WHERE kind!='inventory'
                    AND state NOT IN ('done','expired') AND event_id IN
                    (SELECT id FROM events WHERE origin<=? OR mode!='live' OR status!='located'
                    OR coalesce(json_extract(payload,'$.summary_only'),0)!=0)""",
                    (now - 86400,),
                )
            db.execute(
                """INSERT OR IGNORE INTO gdms_jobs(event_id)
                SELECT id FROM events WHERE mode='live' AND status='located'
                AND origin>? AND origin<?
                AND coalesce(json_extract(payload,'$.region'),'tw')='tw'
                AND coalesce(json_extract(payload,'$.summary_only'),0)=0""",
                (now - 86400, ready),
            )
            for row in db.execute(
                "SELECT j.event_id,e.origin FROM gdms_jobs j JOIN events e ON e.id=j.event_id WHERE j.ready_at IS NULL"
            ).fetchall():
                # Draw once per event; persistence prevents restart-driven re-jitter.
                delay = (
                    self.settings.gdms_delay_s
                    + request_tail_s(self.settings)
                    + random.SystemRandom().uniform(0, self.settings.gdms_schedule_jitter_s)
                )
                db.execute(
                    "UPDATE gdms_jobs SET ready_at=? WHERE event_id=?",
                    (row["origin"] + delay, row["event_id"]),
                )
            db.execute(
                """UPDATE gdms_jobs SET state='expired' WHERE state IN ('pending','retry')
                AND event_id IN (SELECT id FROM events WHERE origin<=?)""",
                (now - 86400,),
            )
            # Larger earthquakes first among due jobs.
            order = magnitude_priority_sql("e") + ",j.retry_at,e.origin"
            if self.downloads is not None:
                # A backlog must not starve the job already occupying the portal.
                order = (
                    """CASE WHEN EXISTS(SELECT 1 FROM gdms_downloads d
                    WHERE d.event_id=j.event_id AND d.state IN
                    ('submitting','submitted','processing','unknown')) THEN 0 ELSE 1 END,"""
                    + order
                )
            rows = db.execute(
                f"""SELECT j.event_id FROM gdms_jobs j JOIN events e ON e.id=j.event_id
                WHERE j.state IN ('pending','retry') AND j.retry_at<=? AND j.ready_at<=?
                AND e.mode='live' AND e.status='located' AND e.origin>? AND e.origin<?
                AND coalesce(json_extract(e.payload,'$.summary_only'),0)=0
                ORDER BY {order} LIMIT 1""",
                (now, now, now - 86400, ready),
            ).fetchall()
        for row in rows:
            eid = row["event_id"]
            with self.store.connect() as db:
                db.execute("UPDATE gdms_jobs SET attempts=attempts+1 WHERE event_id=?", (eid,))
            try:
                self.process(eid)
            except NeedsAttention as exc:
                if str(exc) in AUTH_ERRORS:
                    self.alerts.login_failed(str(exc))
                self.finish(eid, "retry", str(exc))
                if str(exc) in AUTH_ERRORS:
                    self.status["state"] = (
                        "needs_attention" if self.alerts.blocked else "auth_retry"
                    )
                    self.status["retry_at"] = self.alerts.state()["retry_at"]
                self.alerts.deliver()
            except PortalError as exc:
                if str(exc) in AUTH_ERRORS:
                    self.alerts.login_failed(str(exc))
                self.finish(eid, "retry", str(exc))
            except GdmsUnavailable as exc:
                self.finish(eid, "retry", str(exc))
            except Exception as exc:
                # Do not expose private filesystem paths, credentials, URLs or HTTP bodies.
                self.finish(eid, "retry", type(exc).__name__)
            finally:
                self.status["active"] = False
                self.refresh_queue()
        if not rows and self.status.get("state") in {"等待 GDMS 工作", "idle"}:
            self.progress("idle")

    def process(self, eid):
        prior = self.store.get(eid)
        if (
            not prior
            or prior.get("mode") != "live"
            or prior.get("status") != "located"
            or prior.get("summary_only")
            or prior.get("region", "tw") != "tw"
        ):
            return
        origin = prior["origin"]
        self.progress(
            "preparing",
            event_id=eid,
            event_origin=origin,
            event_place=prior.get("place"),
            active=True,
            station_count=None,
            download_kind=None,
            retry_at=0,
            download_stage=None,
            download_completed=None,
            download_total=None,
            download_retry_at=0,
        )
        self.refresh_queue()
        # Event IDs are also used as directory names: reject any path separators.
        if Path(eid).name != eid or eid in (".", "..") or "\\" in eid:
            raise ValueError("Invalid event ID")
        inbox = self.settings.root / "runtime/gdms/inbox" / eid
        if not (inbox / "manifest.json").is_file():
            if self.downloads is None:
                raise GdmsUnavailable("等待 GDMS 匯出波形與完整儀器響應")
            self.progress("preparing_download")
            options = {}
            window = broadband_window(self.settings, origin)
            if window is not None:
                options["hh_window"] = window
            if not self.downloads.prepare(
                eid, origin - WINDOW_BEFORE, origin + WINDOW_AFTER, **options
            ):
                self.alerts.recovered()
                self.finish(eid, "retry", "GDMS 已排入下載佇列，等待取件")
                return
            self.alerts.recovered()
        signature = self.input_signature(inbox, prior)
        with self.store.connect() as db:
            previous = db.execute(
                "SELECT input_signature FROM gdms_jobs WHERE event_id=?", (eid,)
            ).fetchone()
        if previous and previous["input_signature"] == signature:
            self.finish(eid, "retry", "原解已保留，等待更新的 GDMS 波形或響應")
            return
        self.progress("calibrating")
        extra, manifest = acquire_gdms(origin - WINDOW_BEFORE, origin + WINDOW_AFTER, inbox)
        self.status["station_count"] = manifest.get("station_count")
        directory = self.settings.root / "runtime/gdms/analysis" / eid
        archive = Path(prior["waveform_file"]) if prior.get("waveform_file") else None
        original = load_records(archive) if archive and archive.is_file() else []
        measurements = merge_gdms_measurements(original, extra)
        records = merge_records([], measurements, origin, preferred_provider="gdms")
        if self.model is None:
            self.model = Iasp91(self.settings.root / "runtime/iasp91.npz")
        self.progress("analyzing")
        report = analyze(
            records,
            self.model,
            self.settings.picker,
            cache=directory / "picks",
            measurement_records=measurements,
        )
        extra_ids = {r.id for r in records if r.provider == "gdms"}
        candidates = [
            e
            for e in report["events"]
            if acceptable(prior, e) and extra_ids.intersection(e.get("used", []))
        ]
        candidates.sort(key=lambda e: (-e["count"], -e["phase_count"], e["rms_s"]))
        location_changed = bool(candidates)
        candidate = dict(prior, **candidates[0]) if location_changed else dict(prior)
        if location_changed:
            candidate["magnitudes"] = candidates[0]["magnitudes"] + [
                m for m in prior.get("magnitudes", []) if m["type"] != "ML"
            ]
        candidate.pop("waveform_image_file", None)
        self.progress("mechanism")
        try:
            mechanism_event = candidate
            if not location_changed:
                from ..processing.location import select_arrivals, independent

                compatible = select_arrivals(
                    independent(report.get("picks", [])),
                    prior,
                    self.model,
                    max_distance=self.model.max_distance,
                )
                mechanism_event = dict(prior, residuals=[dict(p, used=True) for p in compatible])
                from ..processing.magnitude import taiwan_ml

                sites = {tuple((p.get("source_id") or p["id"]).split(".")[:2]) for p in compatible}
                ml = taiwan_ml(
                    [r for r in measurements if tuple(r.id.split(".")[:2]) in sites],
                    prior,
                    self.model,
                )
                previous_ml = next(
                    (m for m in prior.get("magnitudes", []) if m["type"] == "ML"), {}
                )
                if ml["value"] is not None and ml["count"] >= int(previous_ml.get("count") or 0):
                    candidate.update(
                        magnitude=ml["value"],
                        magnitude_type="ML",
                        magnitude_count=ml["count"],
                        magnitude_method=ml["method"],
                        magnitude_saturation=ml["saturation_risk"],
                    )
                    candidate["magnitudes"] = [ml] + [
                        m for m in prior.get("magnitudes", []) if m["type"] != "ML"
                    ]
            candidate["focal_mechanism"] = first_motions(
                measurements,
                mechanism_event,
                refine_onsets=True,
                stability=True,
                validate_joint=self.settings.adaptive_cmt_enabled
                or self.settings.focal_joint_shadow,
            )
            save_json(directory / "mechanism-diagnostics.json", candidate["focal_mechanism"])
            if not self.settings.gdms_mechanism_publish:
                candidate["focal_mechanism"]["shadow_status"] = candidate["focal_mechanism"][
                    "status"
                ]
                candidate["focal_mechanism"]["status"] = "shadow"
                candidate["focal_mechanism"].setdefault("reasons", []).append("機制發布尚未啟用")
        except Exception as exc:
            # An experimental inversion failure must not discard a valid location improvement.
            candidate["focal_mechanism"] = dict(
                status="failed",
                experimental=True,
                station_count=0,
                reasons=["機制計算失敗，定位補強仍保留"],
                error=type(exc).__name__,
            )
        if self.settings.focal_joint_shadow and candidate["focal_mechanism"]["status"] != "failed":
            try:
                shadow = candidate["focal_mechanism"]
                save_json(
                    directory / "joint-mechanism-shadow.json",
                    dict(
                        event_id=eid,
                        base_revision=prior["revision"],
                        input_signature=signature,
                        evaluated_at=time.time(),
                        location=shadow["location"],
                        results=shadow["regional_comparison"],
                    ),
                )
            except Exception:
                LOG.exception("Joint mechanism shadow evaluation failed for %s", eid)
        previous = prior.get("focal_mechanism") or {}
        attempted = candidate["focal_mechanism"]
        kept_mechanism = (
            gdms_mechanism_check(prior, attempted)
            if self.settings.gdms_mechanism_publish
            and all(prior.get(key) == candidate.get(key) for key in LOCATION_FIELDS)
            else None
        )
        if kept_mechanism:
            candidate["focal_mechanism"] = previous
        if not location_changed and attempted["status"] != "experimental":
            methods = report.get("methods", {})
            classified = self.settings.picker == "phasenet" or (
                bool(methods) and all(method.startswith("PhaseNet/") for method in methods.values())
            )
            if classified:
                # A temporary picker failure must still be retried after recovery.
                with self.store.connect() as db:
                    db.execute(
                        "UPDATE gdms_jobs SET input_signature=? WHERE event_id=?", (signature, eid)
                    )
            if not classified or not measurements:
                self.finish(
                    eid,
                    "retry" if manifest["errors"] or not classified else "retained",
                    "定位與機制未通過發布條件，保留原結果",
                )
                return
            # Publish the evaluated mechanism status, even when location
            # remains unchanged. Rejection reasons are scientific results too.
        candidate["gdms_enrichment"] = dict(
            source="GDMS / CWA",
            updated_at=time.time(),
            previous_revision=prior["revision"],
            station_count=manifest["station_count"],
            adopted_stations=sorted(extra_ids.intersection(candidate["used"])),
            files=manifest["files"],
            window=[origin - WINDOW_BEFORE, origin + WINDOW_AFTER],
            location_changed=location_changed,
            previous_mechanism_retained=bool(kept_mechanism),
            mechanism_guard=kept_mechanism,
            mechanism_attempt_status=attempted["status"],
            mechanism_stations=[
                p["id"] for p in candidate["focal_mechanism"].get("observations", [])
            ],
        )
        self.progress("publishing")
        with self.publication_lock:
            if self.stopping:
                return
            latest = self.store.get(eid)
            if not latest or latest.get("summary_only") or latest["revision"] != prior["revision"]:
                self.finish(eid, "retry", "事件已修訂，稍後以最新版本重算")
                return
            candidate.update(
                id=eid, revision=prior["revision"] + 1, map_radius_km=self.settings.map_radius_km
            )
            reports = self.settings.root / "runtime/reports"
            wavefile = reports / f"{eid}-r{candidate['revision']}-waveforms.npz"
            save_records(measurements, wavefile)
            candidate["waveform_file"] = str(wavefile)
            images = report_images(
                candidate, reports, travel_cache=self.settings.root / "runtime/iasp91.npz"
            )
            candidate["image_file"] = str(images["overview"])
            candidate["closeup_image_file"] = str(images["closeup"])
            # Follow the last notification-eligible version, not a magnitude first
            # supplied by a silent GDMS revision. This also keeps repeated GDMS
            # recalculations of originally magnitude-less events silent.
            notification_revision = prior.get("notification_revision", prior["revision"])
            notification_event = (
                prior
                if notification_revision == prior["revision"]
                else self.store.get(eid, notification_revision)
            )
            saved = self.store.put(
                candidate,
                "live",
                eid,
                expected_revision=prior["revision"],
                notify=ml_value(notification_event or {}) is not None,
            )
            if saved is None:
                self.finish(eid, "retry", "事件已修訂，稍後重算")
                return
        self.finish(eid, "completed")

    async def run(self):
        try:
            while True:
                await asyncio.to_thread(self.tick)
                await asyncio.sleep(min(60, self.settings.gdms_poll_s))
        finally:
            self.stopping = True
            if self.downloads is not None:
                self.downloads.portal.close()

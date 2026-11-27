"""Restart-safe GDMS HTTP download queue. Does not publish earthquake revisions."""

from datetime import datetime
import hashlib
import json
import logging
from pathlib import Path
import time
import uuid
from zoneinfo import ZoneInfo

from ..acquisition.gdms_portal import PortalError, NeedsAttention
from ..acquisition.fdsn import checksum
from ..acquisition.gdms import event_inventory
from .gdms_alerts import AUTH_ERRORS

LOG = logging.getLogger(__name__)
# A rejected daily StationXML request blocks every event of that day, so it
# retries sooner than waveform requests and may fall back to cached metadata.
INVENTORY_BACKOFF_CAP_S = 900
WAVEFORM_BACKOFF_CAP_S = 3600
# Station metadata rarely changes; older cached responses are not trusted.
INVENTORY_FALLBACK_MAX_AGE_S = 14 * 86400


class GdmsDownloads:
    def __init__(self, root, store, portal):
        self.root, self.store, self.portal = Path(root), store, portal
        self.directory = self.root / "runtime/gdms"
        self.on_progress = None
        with store.connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS gdms_downloads (
                key TEXT PRIMARY KEY, event_id TEXT NOT NULL, kind TEXT NOT NULL,
                start REAL NOT NULL,end REAL NOT NULL,stations TEXT NOT NULL,
                label TEXT NOT NULL,state TEXT NOT NULL DEFAULT 'new', external_id TEXT,
                created REAL NOT NULL,updated REAL NOT NULL,retry_at REAL NOT NULL DEFAULT 0,
                file TEXT,sha256 TEXT,error TEXT)""")
            columns = {r[1] for r in db.execute("PRAGMA table_info(gdms_downloads)")}
            if "rejections" not in columns:
                db.execute(
                    "ALTER TABLE gdms_downloads ADD COLUMN rejections INTEGER NOT NULL DEFAULT 0"
                )
            if "reject_detail" not in columns:
                # Private diagnostics only: the refused server reply, URL-redacted.
                db.execute("ALTER TABLE gdms_downloads ADD COLUMN reject_detail TEXT")

    @property
    def inventory_cache(self):
        return self.directory / "inventory-latest.xml"

    def report_progress(self, job, state):
        if self.on_progress is None:
            return
        with self.store.connect() as db:
            row = db.execute(
                "SELECT count(*),coalesce(sum(state='done'),0) "
                "FROM gdms_downloads WHERE event_id=?",
                (job["event_id"],),
            ).fetchone()
        self.on_progress(
            state,
            download_stage=state,
            download_kind=job["kind"],
            download_completed=row[1],
            download_total=row[0],
            download_retry_at=job["retry_at"],
        )

    def recover(self):
        """Release legacy auth/rejected jobs without replaying ambiguous submissions."""
        with self.store.connect() as db:
            rows = db.execute(
                "SELECT key,error FROM gdms_downloads WHERE state='needs_attention'"
            ).fetchall()
            for row in rows:
                reason = row["error"] or ""
                if (
                    reason in AUTH_ERRORS
                    or reason in {"session_expired", "portal_busy"}
                    or reason.startswith("submission_rejected")
                ):
                    db.execute(
                        "UPDATE gdms_downloads SET state='new',retry_at=0,error=NULL WHERE key=?",
                        (row["key"],),
                    )
                elif reason == "submission_not_reconciled":
                    db.execute(
                        "UPDATE gdms_downloads SET state='unknown',retry_at=0 WHERE key=?",
                        (row["key"],),
                    )

    def enqueue(self, eid, kind, start, end, stations="all"):
        if Path(eid).name != eid or eid in (".", "..") or "\\" in eid:
            raise ValueError("Invalid event ID")
        day = datetime.now(ZoneInfo("Asia/Taipei")).strftime("%Y%m%d")
        identity = ["inventory", day] if kind == "inventory" else [eid, kind, start, end, stations]
        key = hashlib.sha256(json.dumps(identity).encode()).hexdigest()
        label = "Tighnari" + key[:24]
        with self.store.connect() as db:
            db.execute(
                """INSERT OR IGNORE INTO gdms_downloads
                (key,event_id,kind,start,end,stations,label,created,updated)
                VALUES(?,?,?,?,?,?,?,?,?)""",
                (key, eid, kind, start, end, stations, label, time.time(), time.time()),
            )
        return key

    def row(self, key):
        with self.store.connect() as db:
            return dict(db.execute("SELECT * FROM gdms_downloads WHERE key=?", (key,)).fetchone())

    def update(self, key, **values):
        values["updated"] = time.time()
        with self.store.connect() as db:
            db.execute(
                "UPDATE gdms_downloads SET " + ",".join(f"{k}=?" for k in values) + " WHERE key=?",
                (*values.values(), key),
            )

    def matches(self, job, rows):
        found = []
        for row in rows:
            try:
                detail = json.loads(row["para_detail"])
            except (ValueError, KeyError, TypeError):
                raise PortalError("job_detail_protocol_changed") from None
            if detail.get("network") != "CWASN":
                continue
            if job["kind"] == "inventory":
                # StationXML ignores submitted labels: observed label is always CWASN.
                try:
                    created = (
                        datetime.fromisoformat(row["datetime"])
                        .replace(tzinfo=ZoneInfo("Asia/Taipei"))
                        .timestamp()
                    )
                except (ValueError, TypeError, KeyError):
                    continue
                if detail.get("output_fmt") == "staxml" and created >= job["created"] - 86400:
                    found.append(row)
            elif detail.get("label") == job["label"]:
                from datetime import timezone

                def stamp(value):
                    return datetime.fromtimestamp(value, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")

                if (
                    detail.get("startTime") != stamp(job["start"])
                    or detail.get("endTime") != stamp(job["end"])
                    or detail.get("output_fmt") != "mseed"
                ):
                    raise NeedsAttention("download_label_collision")
                found.append(row)
        if job["kind"] != "inventory" and len(found) > 1:
            raise NeedsAttention("duplicate_external_jobs")
        return sorted(found, key=lambda r: r.get("datetime", ""), reverse=True)

    def validate(self, path, job):
        from obspy import read

        try:
            if job["kind"] == "inventory":
                inventory = event_inventory(path, job["start"], job["end"])
                if not any(
                    c.response and c.response.response_stages
                    for n in inventory
                    for s in n
                    for c in s
                ):
                    raise ValueError
            else:
                stream = read(str(path), headonly=True)
                if not stream or not any(
                    float(t.stats.starttime) <= job["start"] + 2
                    and float(t.stats.endtime) >= job["end"] - 2
                    for t in stream
                ):
                    raise ValueError
        except Exception:
            raise PortalError("download_validation_failed") from None

    def step(self, key):
        job = self.row(key)
        self.report_progress(job, "checking_download")
        if job["state"] == "purged":
            raise PortalError("export_already_archived")
        if job["state"] == "done":
            path = self.directory / job["file"]
            if path.is_file() and checksum(path) == job["sha256"]:
                return path
            self.update(key, state="download_retry", retry_at=0)
            job = self.row(key)
        if job["state"] == "needs_attention":
            # A data/submission problem must not turn into a global login prompt.
            raise PortalError(job["error"] or "download_needs_attention")
        if job["retry_at"] > time.time():
            self.report_progress(job, "download_backoff")
            return None
        # Always reconcile before issuing a request, including after a process crash.
        rows = self.portal.list_jobs()
        found = self.matches(job, rows)
        if found:
            external = found[0]
            self.update(
                key, external_id=str(external["id"]), state="processing", retry_at=time.time() + 60
            )
            links = self.portal.job_links(external)
            if not links:
                if external.get("error_message"):
                    self.update(key, state="needs_attention", error="external_job_failed")
                    raise PortalError("external_job_failed")
                self.report_progress(self.row(key), "waiting_portal")
                return None
            if len(links) != 1:
                raise PortalError("download_links_ambiguous")
            suffix = ".xml" if job["kind"] == "inventory" else ".mseed"
            relative = Path("downloads") / (key + suffix)
            path = self.directory / relative
            self.report_progress(job, "downloading")
            self.portal.download(links[0], path)
            try:
                self.report_progress(job, "validating_download")
                self.validate(path, job)
            except PortalError:
                path.unlink(missing_ok=True)
                raise
            self.update(key, state="done", file=str(relative), sha256=checksum(path), error=None)
            if job["kind"] == "inventory":
                self.remember_inventory(path)
            self.report_progress(self.row(key), "download_ready")
            return path
        if job["state"] in ("submitting", "unknown", "processing", "submitted", "download_retry"):
            self.report_progress(job, "waiting_portal")
            # Accepted POST may take time to appear. Never silently issue another.
            if time.time() - job["updated"] > 600:
                # Keep reconciling this label, but let other events use the portal.
                # Never resubmit a POST whose outcome is still unknown.
                self.update(
                    key,
                    state="reconciling",
                    error="submission_not_reconciled",
                    retry_at=time.time() + 300,
                )
                raise PortalError("submission_not_reconciled")
            return None
        if job["state"] == "reconciling":
            self.report_progress(job, "reconciling_download")
            self.update(key, retry_at=time.time() + 300)
            return None
        # One outstanding server job at a time, across event IDs and channel families.
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            busy = db.execute(
                "SELECT 1 FROM gdms_downloads WHERE key!=? AND state IN ('submitting','unknown','processing','submitted') LIMIT 1",
                (key,),
            ).fetchone()
            if busy:
                # Leave this event queued while a different event owns the portal.
                self.report_progress(job, "waiting_turn")
                return None
            claimed = db.execute(
                """UPDATE gdms_downloads SET state='submitting',retry_at=?,updated=?
                WHERE key=? AND state='new'""",
                (time.time() + 60, time.time(), key),
            ).rowcount
            if not claimed:
                return None
        label, end = job["label"], job["end"]
        if job["kind"] == "inventory" and job["rejections"]:
            # After a refusal, retry with a fresh label and alternate the date
            # range between [day, day] and [day, day + 1]. Reconciliation of
            # StationXML jobs does not depend on the label.
            label = job["label"][:40] + "R" + str(job["rejections"])
            end = job["end"] + 86400 * (job["rejections"] % 2)
        try:
            self.report_progress(job, "submitting_download")
            self.portal.submit(
                job["start"],
                end,
                label,
                family="HH" if job["kind"] == "inventory" else job["kind"],
                stations=job["stations"],
                inventory=job["kind"] == "inventory",
            )
        except PortalError as exc:
            if isinstance(exc, NeedsAttention) and str(exc) in AUTH_ERRORS:
                self.update(key, state="new", error=str(exc), retry_at=time.time() + 60)
                raise
            if str(exc) in ("session_expired", "portal_busy"):
                self.update(
                    key,
                    state="new",
                    retry_at=time.time() + (300 if str(exc) == "portal_busy" else 60),
                    error=str(exc),
                )
                return None
            if str(exc).startswith("submission_rejected"):
                # This is an explicit refusal, not an ambiguous POST. Reconcile
                # before retrying the same label, with durable capped backoff.
                rejections = job["rejections"] + 1
                self.portal.authenticated_at = 0
                cap = (
                    INVENTORY_BACKOFF_CAP_S
                    if job["kind"] == "inventory"
                    else WAVEFORM_BACKOFF_CAP_S
                )
                detail = getattr(exc, "detail", None)
                self.update(
                    key,
                    state="new",
                    error=str(exc),
                    rejections=rejections,
                    reject_detail=detail,
                    retry_at=time.time() + min(cap, 60 * 2 ** min(rejections, 6)),
                )
                # Public log: stable code only; the server reply stays in the database.
                LOG.warning(
                    "GDMS 拒絕下載申請：%s %s（第 %d 次），伺服器回覆已記錄於 gdms_downloads.reject_detail",
                    job["kind"],
                    str(exc),
                    rejections,
                )
                return None
            self.update(
                key,
                state="unknown" if str(exc) == "submission_outcome_unknown" else "needs_attention",
                error=str(exc),
            )
            if str(exc) != "submission_outcome_unknown":
                raise PortalError(str(exc)) from None
            return None
        self.update(key, state="submitted")
        return None

    def prepare(self, eid, start, end, *, hh_window=None):
        """Publish only a validated complete handoff, never partially downloaded files."""
        wave = self.step(self.enqueue(eid, "HH", *(hh_window or (start, end))))
        if wave is None:
            return False
        inventory = self.event_inventory_file(eid, start, end)
        if inventory is None:
            return False
        from obspy import read, UTCDateTime
        from ..acquisition.fdsn import calibrated_records

        inv = event_inventory(inventory, start, end)
        records, _ = calibrated_records(
            read(str(wave), starttime=UTCDateTime(start - 2), endtime=UTCDateTime(end + 2)),
            inv,
            "gdms",
        )
        acquired = {r.id.split(".")[1] for r in records if not r.clipped}
        files = [wave]
        eligible = self.portal.station_codes()
        for family in ("EH", "HN"):
            # Request only missing sites whose metadata are valid for this event.
            wanted = sorted(
                {
                    s.code
                    for n in inv
                    for s in n
                    if s.code in eligible
                    and s.code not in acquired
                    and any(
                        c.code.startswith(family) and c.is_active(time=UTCDateTime(start))
                        for c in s
                    )
                }
            )
            if not wanted:
                continue
            # The official multi-station field accepts at most 30 explicit stations.
            for offset in range(0, len(wanted), 30):
                supplemental = self.step(
                    self.enqueue(eid, family, start, end, ",".join(wanted[offset : offset + 30]))
                )
                if supplemental is None:
                    return False
                files.append(supplemental)
                extra, _ = calibrated_records(read(str(supplemental)), inv, "gdms")
                acquired.update(r.id.split(".")[1] for r in extra if not r.clipped)
        if not records:
            raise PortalError("no_calibrated_gdms_stations")
        import shutil

        inbox = self.directory / "inbox" / eid
        inbox.mkdir(parents=True, exist_ok=True)
        entries = []
        inventory_name = "inventory-" + checksum(inventory)[:20] + ".xml"

        def publish(source, destination):
            temporary = destination.with_suffix(
                destination.suffix + "." + uuid.uuid4().hex + ".part"
            )
            try:
                shutil.copyfile(source, temporary)
                temporary.replace(destination)
            finally:
                temporary.unlink(missing_ok=True)

        for source in files:
            name = source.name
            publish(source, inbox / name)
            entries.append(dict(waveform=name, inventory=inventory_name))
        publish(inventory, inbox / inventory_name)
        temporary = inbox / ("manifest-" + uuid.uuid4().hex + ".tmp")
        try:
            temporary.write_text(json.dumps(dict(files=entries), allow_nan=False), encoding="utf-8")
            temporary.replace(inbox / "manifest.json")
        finally:
            temporary.unlink(missing_ok=True)
        return True

    def remember_inventory(self, path):
        """Keep the newest validated StationXML outside per-event cleanup."""
        temporary = self.inventory_cache.with_suffix(".xml." + uuid.uuid4().hex + ".part")
        try:
            import shutil

            shutil.copyfile(path, temporary)
            temporary.replace(self.inventory_cache)
        except OSError:
            LOG.warning("無法更新 GDMS 儀器響應快取")
        finally:
            temporary.unlink(missing_ok=True)

    def fallback_inventory(self, start, end, *, now=None):
        """Return recent validated StationXML that covers this event, if any."""
        now = time.time() if now is None else now
        candidates = [self.inventory_cache]
        with self.store.connect() as db:
            for row in db.execute(
                "SELECT file FROM gdms_downloads WHERE kind='inventory' AND state='done' "
                "AND file IS NOT NULL ORDER BY updated DESC LIMIT 5"
            ):
                candidates.append(self.directory / row["file"])
        # Published per-event copies survive until that event's cleanup.
        inbox = self.directory / "inbox"
        if inbox.is_dir() and not inbox.is_symlink():
            copies = []
            for path in inbox.glob("*/inventory-*.xml"):
                try:
                    copies.append((path.stat().st_mtime, path))
                except OSError:
                    continue
            candidates.extend(path for _, path in sorted(copies, reverse=True)[:5])
        base = self.directory.resolve()
        probe = dict(kind="inventory", start=start, end=end)
        for path in candidates:
            try:
                if (
                    path.is_symlink()
                    or not path.is_file()
                    or not path.resolve().is_relative_to(base)
                    or now - path.stat().st_mtime > INVENTORY_FALLBACK_MAX_AGE_S
                ):
                    continue
                self.validate(path, probe)
            except (OSError, PortalError):
                continue
            return path
        return None

    def event_inventory_file(self, eid, start, end):
        """Today's StationXML, or a recent cached one while today's is refused."""
        key = self.enqueue(eid, "inventory", start, end)
        try:
            inventory = self.step(key)
        except NeedsAttention as exc:
            if str(exc) in AUTH_ERRORS:
                raise
            inventory, failure = None, exc
        except PortalError as exc:
            inventory, failure = None, exc
        else:
            failure = None
        if inventory is not None:
            return inventory
        job = self.row(key)
        refused = failure is not None or job["rejections"] or job["state"] == "needs_attention"
        if not refused:
            return None  # Normal portal processing: wait for today's export.
        fallback = self.fallback_inventory(start, end)
        if fallback is None:
            if failure is not None:
                raise failure
            return None
        LOG.warning("今日 GDMS 儀器響應申請未成功，暫用最近一份已驗證的儀器響應檔")
        self.report_progress(job, "inventory_fallback")
        return fallback

    def cleanup(self, *, now=None, minimum_age_s=86400):
        """Remove known automated raw exports only after verified event archival.

        Download completion time and filesystem age both have a 24-hour floor.
        Research PoCs have no matching archived live events and are not eligible.
        Scientific diagnostics, queue records and SHA-256 provenance are retained.
        """
        from PIL import Image

        now = time.time() if now is None else now
        cutoff = now - max(86400, minimum_age_s)
        result = dict(removed_files=0, removed_bytes=0, purged_jobs=0, errors=0)
        base = self.directory.resolve()
        if not base.is_dir() or self.directory.is_symlink():
            return result
        with self.store.connect() as db:
            events = db.execute("""SELECT DISTINCT e.id,e.payload FROM events e
                JOIN gdms_downloads d ON d.event_id=e.id WHERE e.mode='live'
                AND d.state!='purged'""").fetchall()
        for row in events:
            eid, event = row["id"], json.loads(row["payload"])
            if Path(eid).name != eid or eid in (".", "..") or "/" in eid or "\\" in eid:
                continue
            if not event.get("summary_only"):
                archive = event.get("waveform_archive")
                image = Path(event.get("waveform_image_file", ""))
                reports = (self.root / "runtime/reports").resolve()
                if (
                    not isinstance(archive, dict)
                    or archive.get("format") != "png"
                    or event.get("waveform_file")
                    or not image.is_file()
                    or not image.resolve().is_relative_to(reports)
                ):
                    continue
                try:
                    with Image.open(image) as rendered:
                        rendered.verify()
                except (OSError, ValueError):
                    result["errors"] += 1
                    continue
            with self.store.connect() as db:
                # All revision archives must have committed before deleting inputs.
                referenced = db.execute(
                    """SELECT 1 FROM revisions WHERE event_id=?
                    AND json_extract(payload,'$.waveform_file') IS NOT NULL LIMIT 1""",
                    (eid,),
                ).fetchone()
                jobs = [
                    dict(r)
                    for r in db.execute("SELECT * FROM gdms_downloads WHERE event_id=?", (eid,))
                ]
            if referenced or not jobs or any(job["updated"] > cutoff for job in jobs):
                continue
            if any(
                job["state"] in ("submitting", "submitted", "processing", "unknown") for job in jobs
            ):
                continue
            managed_names = {job["key"] + ".mseed" for job in jobs if job["kind"] != "inventory"}
            inbox = self.directory / "inbox" / eid
            if not inbox.resolve().is_relative_to(base):
                continue
            manifest = inbox / "manifest.json"
            inbox_files = []
            if manifest.is_file() and not inbox.is_symlink() and not manifest.is_symlink():
                try:
                    entries = json.loads(manifest.read_bytes())["files"]
                    if not entries or any(e["waveform"] not in managed_names for e in entries):
                        continue  # A different/manual export is not owned by this queue.
                    for entry in entries:
                        for name in (entry["waveform"], entry["inventory"]):
                            path = inbox / name
                            if path.is_symlink() or not path.resolve().is_relative_to(
                                inbox.resolve()
                            ):
                                raise ValueError("Unmanaged export path")
                            if path.exists() and path.stat().st_mtime > cutoff:
                                raise ValueError("Export not old enough")
                            inbox_files.append(path)
                    if manifest.stat().st_mtime > cutoff:
                        continue
                    inbox_files.append(manifest)
                except (OSError, ValueError, KeyError, TypeError):
                    continue
            elif inbox.is_symlink() or manifest.exists():
                continue

            def remove(path):
                if (
                    path.is_symlink()
                    or not path.resolve().is_relative_to(base)
                    or not path.is_file()
                    or path.stat().st_mtime > cutoff
                ):
                    return False
                size = path.stat().st_size
                path.unlink()
                result["removed_files"] += 1
                result["removed_bytes"] += size
                return True

            try:
                for path in set(inbox_files):
                    remove(path)
                for job in jobs:
                    if job["state"] == "purged":
                        continue
                    if not job["file"]:
                        with self.store.connect() as db:
                            db.execute(
                                "UPDATE gdms_downloads SET state='purged' WHERE key=?",
                                (job["key"],),
                            )
                        result["purged_jobs"] += 1
                        continue
                    relative = Path(job["file"])
                    expected = job["key"] + (".xml" if job["kind"] == "inventory" else ".mseed")
                    if relative.parent != Path("downloads") or relative.name != expected:
                        continue
                    path = self.directory / relative
                    if path.is_symlink() or not path.resolve().is_relative_to(base):
                        continue
                    if path.exists() and not remove(path):
                        continue
                    with self.store.connect() as db:
                        db.execute(
                            "UPDATE gdms_downloads SET state='purged',file=NULL WHERE key=?",
                            (job["key"],),
                        )
                    result["purged_jobs"] += 1
                if inbox.is_dir() and not inbox.is_symlink():
                    try:
                        inbox.rmdir()  # Unknown files are deliberately preserved.
                    except OSError:
                        pass
            except OSError:
                result["errors"] += 1
        return result

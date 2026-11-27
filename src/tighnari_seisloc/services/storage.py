"""Age-based waveform images; scientific event and revision records are retained."""

import asyncio
from contextlib import closing, nullcontext
import hashlib
import json
import logging
import math
import os
import sqlite3
from pathlib import Path
import tempfile
import time

import numpy as np
from PIL import Image, ImageDraw

from .records import compact_event, preferred_magnitude
from .reporting import font, local_time
from .review import review_of
from .waveforms import envelope, load_records

LOG = logging.getLogger(__name__)
ARCHIVE_VERSION = 1


def select_stations(records, event, limit=8):
    """Prefer adopted, low-residual/high-score arrivals, then finite data coverage."""
    arrivals = {}
    for pick in event.get("residuals", []):
        sid = pick.get("source_id") or pick.get("id")
        if sid:
            arrivals.setdefault(sid, []).append(pick)
    stations = {}
    for record in records:
        if len(record.velocity):
            stations.setdefault(record.id, []).append(record)

    def rank(sid):
        traces = stations[sid]
        picks = arrivals.get(sid, [])
        adopted = [p for p in picks if p.get("used")]
        residuals = [abs(p["residual_s"]) for p in adopted if p.get("residual_s") is not None]
        scores = [p["score"] for p in adopted if p.get("score") is not None]
        finite = sum(int(np.isfinite(r.velocity[:, 0]).sum()) for r in traces)
        total = sum(len(r.velocity) for r in traces)
        return (
            not bool(adopted),
            sum(residuals) / len(residuals) if residuals else math.inf,
            -sum(scores) / len(scores) if scores else 0,
            -finite / max(1, total),
            -finite,
            sid,
        )

    available = [
        sid
        for sid, rows in stations.items()
        if any(np.isfinite(r.velocity[:, 0]).any() for r in rows)
    ]
    selected = []
    sites = set()
    for sid in sorted(available, key=rank):
        site = ".".join(sid.split(".")[:2])
        if site not in sites:
            selected.append(sid)
            sites.add(site)
        if len(selected) >= limit:
            break
    return selected


def render_archive(records, event, target, limit=8):
    selected = select_stations(records, event, limit)
    traces = [r for r in records if r.id in selected]
    if not traces:
        raise ValueError("No finite waveform samples to archive")
    start = min(r.start for r in traces)
    end = max(r.end for r in traces)
    picks = [
        dict(
            p, id=p.get("source_id") or p["id"], time=p.get("observed", p.get("time")), adopted=True
        )
        for p in event.get("residuals", [])
        if p.get("used") and p.get("observed", p.get("time")) is not None
    ]
    frame = envelope(traces, selected, end - start, end, 1200, picks)
    rows = {r["id"]: r for r in frame["rows"]}
    selected = [sid for sid in selected if sid in rows]
    if not selected:
        raise ValueError("No visible waveform rows to archive")
    width, top, height, left, right = 1440, 110, 100, 35, 1405
    image = Image.new("RGB", (width, top + height * len(selected) + 90), "#10191b")
    draw = ImageDraw.Draw(image)
    draw.text((35, 15), "事件波形 · 圖片封存", font=font(25), fill="#8edbc3")
    draw.text(
        (35, 55),
        f"{event['id']} · 第 {event['revision']} 次修訂 · 最佳 {len(selected)} 站",
        font=font(18),
        fill="#d4dce4",
    )
    for index, sid in enumerate(selected):
        row = rows[sid]
        y = top + index * height
        center = y + 58
        amplitude = max(
            [0.001] + [max(abs(p[1]), abs(p[2])) for seg in row["segments"] for p in seg]
        )
        draw.text((left, y), f"{sid} · Z · ±{amplitude:.4g} µm/s", font=font(18), fill="#d4dce4")
        draw.line((left, center, right, center), fill="#253b42")
        for tick in range(7):
            x = left + tick * (right - left) / 6
            draw.line((x, y + 25, x, y + 92), fill="#253b42")
        for segment in row["segments"]:
            previous = None
            for t, low, high in segment:
                x = left + (t - start) / (end - start) * (right - left)
                mid = center - (low + high) / 2 / amplitude * 30
                if previous and t - previous[0] <= frame["bin_s"] * 2.1:
                    draw.line((previous[1], previous[2], x, mid), fill="#7ab8d6")
                draw.line(
                    (x, center - low / amplitude * 30, x, center - high / amplitude * 30),
                    fill="#7ab8d6",
                )
                previous = (t, x, mid)
        for pick in row["picks"]:
            x = left + (pick["time"] - start) / (end - start) * (right - left)
            color = "#e4bd82" if pick["phase"] == "P" else "#b8b8e6"
            draw.line((x, y + 23, x, y + 94), fill=color, width=2)
            draw.text((x + 3, y + 23), pick["phase"], font=font(16), fill=color)
    footer = image.height - 80
    for tick in range(7):
        x = left + tick * (right - left) / 6
        label = local_time(start + tick * (end - start) / 6).split()[-1]
        draw.text((x, footer), label, font=font(16), fill="#b3bec7", anchor="mt")
    draw.text(
        (35, image.height - 45),
        f"{local_time(start)} — {local_time(end)} UTC+8 · 各站獨立縮放 · P/S 為採用相位 · 圖片無法還原數值",
        font=font(17),
        fill="#b3bec7",
    )
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=target.parent, suffix=".tmp", delete=False) as handle:
        temporary = Path(handle.name)
    try:
        image.save(temporary, format="PNG", optimize=True)
        with Image.open(temporary) as check:
            check.verify()
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return dict(
        version=ARCHIVE_VERSION,
        format="png",
        start=start,
        end=end,
        stations=selected,
        selection="adopted-arrivals/residual/score/finite-coverage",
        archived_at=time.time(),
    )


def _archive_waveforms(
    settings, store, *, publication_lock=None, stop=None, max_files=None, now=None
):
    """Only remove an NPZ after every referencing row has a verified image committed."""
    cutoff = (time.time() if now is None else now) - settings.waveform_retention_hours * 3600
    folder = (settings.root / "runtime/reports").resolve()
    with store.connect() as db:
        payloads = db.execute(
            "SELECT payload FROM events UNION ALL SELECT payload FROM revisions"
        ).fetchall()
    groups = {}
    protected = set()
    for row in payloads:
        event = json.loads(row[0])
        raw = event.get("waveform_file")
        if not raw:
            continue
        if event["origin"] >= cutoff:
            protected.add(raw)
        else:
            groups.setdefault(raw, {})[(event["id"], event["revision"])] = event
    result = dict(files=0, images=0, removed_bytes=0, image_bytes=0, missing=0, errors=0)
    for raw, events in groups.items():
        if stop and stop.is_set():
            break
        if max_files is not None and result["files"] >= max_files:
            break
        path = Path(raw)
        if raw in protected or path.suffix != ".npz" or not path.resolve().is_relative_to(folder):
            continue
        if not path.is_file():
            result["missing"] += 1
            continue
        try:
            with publication_lock if publication_lock is not None else nullcontext():
                stat = path.stat()
                source_hash = hashlib.sha256(path.read_bytes()).hexdigest()
                records = load_records(path)
                updates = []
                for event in events.values():
                    target = folder / f"{event['id']}-r{event['revision']}-waveform.png"
                    metadata = render_archive(
                        records, event, target, settings.waveform_archive_stations
                    )
                    metadata.update(source_name=path.name, source_sha256=source_hash)
                    updates.append((event, target, metadata))
                if path.stat().st_mtime_ns != stat.st_mtime_ns:
                    continue
                with store.connect() as db:
                    db.execute("BEGIN IMMEDIATE")
                    for event, target, metadata in updates:
                        for table, condition, params in (
                            ("events", "id=? AND revision=?", (event["id"], event["revision"])),
                            (
                                "revisions",
                                "event_id=? AND revision=?",
                                (event["id"], event["revision"]),
                            ),
                        ):
                            row = db.execute(
                                f"SELECT payload FROM {table} WHERE {condition}", params
                            ).fetchone()
                            if row is None:
                                continue
                            current = json.loads(row[0])
                            if current.get("waveform_file") != raw or current["origin"] >= cutoff:
                                continue
                            current.pop("waveform_file")
                            current["waveform_image_file"] = str(target)
                            current["waveform_archive"] = metadata
                            db.execute(
                                f"UPDATE {table} SET payload=? WHERE {condition}",
                                (json.dumps(current, ensure_ascii=False, allow_nan=False), *params),
                            )
                            if table == "events":
                                store.update_summary(db, current)
                    referenced = any(
                        db.execute(
                            f"SELECT 1 FROM {table} WHERE json_extract(payload,'$.waveform_file')=? LIMIT 1",
                            (raw,),
                        ).fetchone()
                        for table in ("events", "revisions")
                    )
                result["images"] += len(updates)
                result["image_bytes"] += sum(target.stat().st_size for _, target, _ in updates)
                if not referenced:
                    path.unlink()
                    result["files"] += 1
                    result["removed_bytes"] += stat.st_size
        except Exception:
            result["errors"] += 1
            LOG.exception("Waveform archive failed: %s", path.name)
    if result["files"] or result["errors"]:
        LOG.info("Waveform image retention: %s", result)
    return result


def compact_old_events(
    settings, store, *, publication_lock=None, stop=None, now=None, max_events=50
):
    """Compact unreviewed small/unknown events after 30 days."""
    now = time.time() if now is None else now
    cutoff = now - 30 * 86400
    root = settings.root.resolve()
    reports = root / "runtime/reports"
    result = dict(
        events_compacted=0, detail_files_deleted=0, detail_bytes_removed=0, detail_cleanup_errors=0
    )
    with publication_lock if publication_lock is not None else nullcontext():
        with store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute(
                "SELECT payload FROM events WHERE origin<? ORDER BY origin", (cutoff,)
            ).fetchall()
            for row in rows:
                if (stop and stop.is_set()) or (
                    max_events is not None and result["events_compacted"] >= max_events
                ):
                    break
                event = json.loads(row[0])
                if event.get("summary_only") or review_of(event)["version"] > 0:
                    continue
                magnitude, _ = preferred_magnitude(event)
                if magnitude is not None and magnitude >= 5:
                    continue
                eid = event["id"]
                safe_id = (
                    Path(eid).name == eid
                    and eid not in (".", "..")
                    and "/" not in eid
                    and "\\" not in eid
                )
                old = [event] + [
                    json.loads(r[0])
                    for r in db.execute("SELECT payload FROM revisions WHERE event_id=?", (eid,))
                ]
                candidates = set()
                for revision in old:
                    for key in (
                        "image_file",
                        "closeup_image_file",
                        "waveform_file",
                        "waveform_image_file",
                        "legacy_image",
                    ):
                        if revision.get(key):
                            candidates.add(Path(revision[key]))
                if safe_id:
                    candidates.update(p for p in reports.glob(eid + "-*") if p.is_file())
                    name = hashlib.sha256(eid.encode()).hexdigest()
                    candidates.update(
                        p for p in reports.glob(name + "-r*-mechanism.png") if p.is_file()
                    )
                    for enrichment in (
                        root / "runtime/cmt" / name,
                        root / "runtime/adaptive-cmt" / name,
                        root / "runtime/enrichment" / eid,
                        root / "runtime/gdms/inbox" / eid,
                        root / "runtime/gdms/analysis" / eid,
                    ):
                        if enrichment.is_dir():
                            candidates.update(p for p in enrichment.rglob("*") if p.is_file())
                for path in candidates:
                    resolved = path.resolve()
                    if resolved.is_relative_to(reports) or (
                        safe_id
                        and any(
                            resolved.is_relative_to(folder)
                            for folder in (
                                root / "runtime/cmt" / name,
                                root / "runtime/adaptive-cmt" / name,
                                root / "runtime/enrichment" / eid,
                                root / "runtime/gdms/inbox" / eid,
                                root / "runtime/gdms/analysis" / eid,
                            )
                        )
                    ):
                        db.execute(
                            "INSERT OR IGNORE INTO retention_deletions VALUES (?)", (str(resolved),)
                        )
                db.execute(
                    "UPDATE events SET payload=?,updated=? WHERE id=?",
                    (
                        json.dumps(compact_event(event), ensure_ascii=False, allow_nan=False),
                        now,
                        eid,
                    ),
                )
                store.update_summary(db, compact_event(event))
                db.execute("DELETE FROM revisions WHERE event_id=?", (eid,))
                for table in (
                    "deliveries",
                    "webhook_deliveries",
                    "enrichment_jobs",
                    "gdms_jobs",
                    "focal_jobs",
                    "regional_cmt_jobs",
                    "adaptive_cmt_jobs",
                ):
                    if db.execute(
                        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
                    ).fetchone():
                        db.execute(f"DELETE FROM {table} WHERE event_id=?", (eid,))
                result["events_compacted"] += 1
        # A shared waveform/image can still belong to a retained event or revision.
        with store.connect() as db:
            referenced = set()
            for row in db.execute(
                "SELECT payload FROM events UNION ALL SELECT payload FROM revisions"
            ):
                event = json.loads(row[0])
                for key in (
                    "image_file",
                    "closeup_image_file",
                    "waveform_file",
                    "waveform_image_file",
                    "legacy_image",
                ):
                    if event.get(key):
                        referenced.add(os.path.normcase(str(Path(event[key]).resolve())))
            pending = [r[0] for r in db.execute("SELECT path FROM retention_deletions")]
        for filename in pending:
            if stop and stop.is_set():
                break
            path = Path(filename)
            resolved = path.resolve()
            if not (
                resolved.is_relative_to(reports)
                or resolved.is_relative_to(root / "runtime/cmt")
                or resolved.is_relative_to(root / "runtime/adaptive-cmt")
                or resolved.is_relative_to(root / "runtime/enrichment")
                or resolved.is_relative_to(root / "runtime/gdms/inbox")
                or resolved.is_relative_to(root / "runtime/gdms/analysis")
            ):
                continue
            try:
                if (
                    resolved.is_relative_to(root / "runtime/gdms")
                    and path.is_file()
                    and path.stat().st_mtime
                    > now - max(86400, settings.waveform_retention_hours * 3600)
                ):
                    continue  # A recently supplied GDMS export has its own 24-hour floor.
                if os.path.normcase(str(resolved)) not in referenced and path.is_file():
                    size = path.stat().st_size
                    path.unlink()
                    result["detail_files_deleted"] += 1
                    result["detail_bytes_removed"] += size
                with store.connect() as db:
                    db.execute("DELETE FROM retention_deletions WHERE path=?", (filename,))
            except OSError:
                result["detail_cleanup_errors"] += 1
                LOG.warning("Deferred expired detail cleanup: %s", path.name)
    if result["events_compacted"]:
        LOG.info("Thirty-day detail retention: %s", result)
    return result


def archive_waveforms(settings, store, **options):
    # SQLite releases this process-wide lease on crashes as well as clean exits.
    lease_path = settings.root / "runtime/waveform-retention.lock.sqlite3"
    lease_path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(lease_path, timeout=0)) as lease:
        try:
            lease.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as exc:
            if "locked" in str(exc):
                return dict(
                    files=0,
                    images=0,
                    removed_bytes=0,
                    image_bytes=0,
                    missing=0,
                    errors=0,
                    busy=True,
                )
            raise
        try:
            details = compact_old_events(
                settings,
                store,
                publication_lock=options.get("publication_lock"),
                stop=options.get("stop"),
                now=options.get("now"),
                max_events=options.get("max_files"),
            )
            return dict(_archive_waveforms(settings, store, **options), **details)
        finally:
            lease.rollback()


async def retention_loop(settings, store, publication_lock, stop):
    while not stop.is_set():
        try:
            await asyncio.to_thread(
                archive_waveforms,
                settings,
                store,
                publication_lock=publication_lock,
                stop=stop,
                max_files=50,
            )
        except Exception:
            LOG.exception("Waveform retention sweep failed")
        await asyncio.sleep(900)

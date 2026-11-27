"""Transactional event history and notification state, using standard-library SQLite."""

import json
from pathlib import Path
import sqlite3
import time
import uuid
from .review import ReviewStoreMixin, automatic_event, review_of, visible_sql, with_review
from .records import depth_label


# Keep the legacy SQL semantics, including fallback to explicitly typed ML.
ML_SQL = """typeof(coalesce(
    (SELECT json_extract(m.value, '$.value')
     FROM json_each(events.payload, '$.magnitudes') AS m
     WHERE json_extract(m.value, '$.type') = 'ML' LIMIT 1),
    CASE WHEN json_extract(payload, '$.magnitude_type') = 'ML'
         THEN json_extract(payload, '$.magnitude') END
)) IN ('integer', 'real')"""


class EventStore(ReviewStoreMixin):
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS events (
                    id TEXT PRIMARY KEY, origin REAL NOT NULL, mode TEXT NOT NULL,
                    status TEXT NOT NULL, revision INTEGER NOT NULL, payload TEXT NOT NULL,
                    updated REAL NOT NULL);
                CREATE INDEX IF NOT EXISTS events_origin ON events(origin DESC);
                CREATE INDEX IF NOT EXISTS events_mode_origin ON events(mode, origin DESC);
                CREATE TABLE IF NOT EXISTS revisions (
                    event_id TEXT NOT NULL, revision INTEGER NOT NULL, payload TEXT NOT NULL,
                    PRIMARY KEY(event_id,revision));
                CREATE TABLE IF NOT EXISTS deliveries (
                    event_id TEXT PRIMARY KEY, sent_revision INTEGER NOT NULL DEFAULT 0,
                    message_id TEXT, retry_at REAL NOT NULL DEFAULT 0, attempts INTEGER NOT NULL DEFAULT 0,
                    error TEXT);
                CREATE TABLE IF NOT EXISTS retention_deletions (path TEXT PRIMARY KEY);
                CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY,value TEXT);
                CREATE TABLE IF NOT EXISTS admin_audit (
                    id INTEGER PRIMARY KEY, event_id TEXT NOT NULL, created_at REAL NOT NULL,
                    action TEXT NOT NULL, reason TEXT NOT NULL, before_json TEXT NOT NULL,
                    after_json TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS admin_audit_event ON admin_audit(event_id,id);
                CREATE TABLE IF NOT EXISTS webhook_deliveries (
                    event_id TEXT NOT NULL, destination TEXT NOT NULL,
                    sent_revision INTEGER NOT NULL DEFAULT 0, message_id TEXT,
                    retry_at REAL NOT NULL DEFAULT 0, attempts INTEGER NOT NULL DEFAULT 0,
                    error TEXT, PRIMARY KEY(event_id,destination));
            """)
            # Fingerprint of the research pick drawn in the sent Discord image.
            columns = {row[1] for row in db.execute("PRAGMA table_info(webhook_deliveries)")}
            if "research_tag" not in columns:
                db.execute("ALTER TABLE webhook_deliveries ADD COLUMN research_tag TEXT")
        self._migrate_summaries()

    def _migrate_summaries(self):
        # DDL, backfill and version marker commit together; failed migrations retry.
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("""CREATE TABLE IF NOT EXISTS event_summaries (
                id TEXT PRIMARY KEY, origin REAL NOT NULL, mode TEXT NOT NULL,
                visible INTEGER NOT NULL, has_ml INTEGER NOT NULL,
                event_order INTEGER NOT NULL, payload TEXT NOT NULL)""")
            for name, columns in (
                ("origin", "visible,origin DESC,event_order"),
                ("mode_origin", "visible,mode,origin DESC,event_order"),
                ("ml_origin", "visible,has_ml,origin DESC,event_order"),
                ("mode_ml_origin", "visible,mode,has_ml,origin DESC,event_order"),
            ):
                db.execute(
                    f"CREATE INDEX IF NOT EXISTS summaries_{name} ON event_summaries({columns})"
                )
            version = db.execute(
                "SELECT value FROM metadata WHERE key='event_summaries_version'"
            ).fetchone()
            if version is None or version[0] != "2":
                db.execute("DELETE FROM event_summaries")
                for row in db.execute("SELECT payload FROM events ORDER BY rowid"):
                    self.update_summary(db, json.loads(row[0]))
                db.execute("INSERT OR REPLACE INTO metadata VALUES ('event_summaries_version','2')")

    def update_summary(self, db, event):
        """Update the public projection inside the caller's event write transaction."""
        payload = json.dumps(
            self.summary(event), ensure_ascii=False, allow_nan=False, separators=(",", ":")
        )
        db.execute(
            "INSERT OR REPLACE INTO event_summaries "
            "SELECT id,origin,mode,(" + visible_sql() + "),(" + ML_SQL + "),rowid,? "
            "FROM events WHERE id=?",
            (payload, event["id"]),
        )

    def connect(self):
        db = sqlite3.connect(self.path, timeout=20)
        db.row_factory = sqlite3.Row
        return db

    def put(self, event, mode="live", event_id=None, *, expected_revision=None, notify=True):
        event = automatic_event(event)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if event_id is None and mode == "live":
                from ..processing.event_identity import same_event, arrival_overlap

                candidates = db.execute(
                    "SELECT id,payload FROM events WHERE mode=? AND abs(origin-?)<20",
                    (mode, event["origin"]),
                ).fetchall()
                matches = []
                for row in candidates:
                    old = json.loads(row["payload"])
                    if same_event(old, event):
                        shared = arrival_overlap(old, event)
                        matches.append((shared["sites"], shared["count"], row["id"]))
                if matches:
                    event_id = max(matches)[2]
            event_id = event_id or str(uuid.uuid4())
            previous = db.execute(
                "SELECT revision,status,payload FROM events WHERE id=?", (event_id,)
            ).fetchone()
            if previous is not None and json.loads(previous["payload"]).get("summary_only"):
                return None
            if expected_revision is not None and (
                previous is None or previous["revision"] != expected_revision
            ):
                return None
            if (
                mode == "live"
                and previous is not None
                and previous["status"] == "located"
                and event.get("status") != "located"
            ):
                return None
            revision = previous["revision"] + 1 if previous else 1
            # Silent scientific revisions must never make a new/updated Discord message eligible.
            prior = json.loads(previous["payload"]) if previous else {}
            if review_of(prior)["deleted"]:
                return None
            if prior.get("review"):
                event = with_review(event, prior["review"])
            prior_cmt = prior.get("regional_cmt")
            if prior_cmt and (
                not event.get("regional_cmt")
                or event["regional_cmt"].get("input_signature") == prior_cmt.get("input_signature")
            ):
                from .regional_cmt import event_inputs

                if event_inputs(event) == event_inputs(prior):
                    event["regional_cmt"] = prior_cmt
                else:
                    event["regional_cmt"] = {
                        "method": prior_cmt["method"],
                        "status": "stale",
                        "green_version": prior_cmt.get("green_version"),
                        "reasons": ["inputs_updated"],
                    }
            notify = notify and not review_of(event)["error"]
            event["notification_revision"] = (
                revision if notify else prior.get("notification_revision", revision - 1)
            )
            event.update(id=event_id, revision=revision, mode=mode, saved_at=time.time())
            payload = json.dumps(event, ensure_ascii=False, allow_nan=False)
            db.execute(
                "INSERT OR REPLACE INTO events VALUES (?,?,?,?,?,?,?)",
                (
                    event_id,
                    event["origin"],
                    mode,
                    event.get("status", "candidate"),
                    revision,
                    payload,
                    time.time(),
                ),
            )
            self.update_summary(db, event)
            db.execute("INSERT INTO revisions VALUES (?,?,?)", (event_id, revision, payload))
            db.execute("INSERT OR IGNORE INTO deliveries(event_id) VALUES (?)", (event_id,))
        return event

    def latest_revision(self, event_id):
        with self.connect() as db:
            row = db.execute("SELECT revision FROM events WHERE id=?", (event_id,)).fetchone()
        return row["revision"] if row else None

    def revision_state(self, event_id):
        with self.connect() as db:
            row = db.execute(
                "SELECT revision,json_extract(payload,'$.summary_only') AS summary_only FROM events WHERE id=? AND coalesce(json_extract(payload,'$.review.deleted'),0)=0",
                (event_id,),
            ).fetchone()
        return dict(
            revision=row["revision"] if row else None,
            summary_only=bool(row["summary_only"]) if row else False,
        )

    def get(self, event_id, revision=None, *, include_deleted=False):
        with self.connect() as db:
            current = db.execute("SELECT payload FROM events WHERE id=?", (event_id,)).fetchone()
            if current and not include_deleted and review_of(json.loads(current[0]))["deleted"]:
                return None
            row = (
                db.execute("SELECT payload FROM events WHERE id=?", (event_id,)).fetchone()
                if revision is None
                else db.execute(
                    "SELECT payload FROM revisions WHERE event_id=? AND revision=?",
                    (event_id, revision),
                ).fetchone()
            )
        if row:
            return json.loads(row["payload"])
        if revision is not None:
            current = self.get(event_id)
            if current and current.get("summary_only") and current["revision"] == revision:
                return current
        return None

    def sitemap_count(self):
        with self.connect() as db:
            return db.execute("SELECT count(*) FROM events WHERE " + visible_sql()).fetchone()[0]

    def sitemap_ids(self, limit, offset):
        with self.connect() as db:
            return [
                row["id"]
                for row in db.execute(
                    "SELECT id FROM events WHERE "
                    + visible_sql()
                    + " ORDER BY id LIMIT ? OFFSET ?",
                    (limit, offset),
                )
            ]

    @staticmethod
    def _summary_filter(mode, require_ml):
        clauses = ["visible=1"]
        params = []
        if mode != "all":
            clauses.append("mode=?")
            params.append(mode)
        if require_ml:
            clauses.append("has_ml=1")
        return " AND ".join(clauses), params

    @staticmethod
    def _summary_page(db, condition, params, limit, offset):
        return [
            json.loads(row[0])
            for row in db.execute(
                "SELECT payload FROM event_summaries WHERE "
                + condition
                + " ORDER BY origin DESC,event_order LIMIT ? OFFSET ?",
                (*params, limit, offset),
            )
        ]

    def summary_items(self, mode="all", limit=20, offset=0, *, require_ml=False):
        """Read a bounded page without scanning/counting the event catalogue."""
        condition, params = self._summary_filter(mode, require_ml)
        with self.connect() as db:
            return self._summary_page(db, condition, params, limit, offset)

    def latest_summary(self, mode="live"):
        items = self.summary_items(mode, 1)
        return items[0] if items else None

    def listing(self, mode="all", limit=20, offset=0, *, require_ml=False):
        condition, params = self._summary_filter(mode, require_ml)
        with self.connect() as db:
            # A consistent snapshot keeps total and page aligned during live writes.
            db.execute("BEGIN")
            total = db.execute(
                "SELECT count(*) FROM event_summaries WHERE " + condition, params
            ).fetchone()[0]
            items = self._summary_page(db, condition, params, limit, offset)
        return dict(total=total, limit=limit, offset=offset, items=items)

    @staticmethod
    def summary(event):
        keys = (
            "id",
            "summary_only",
            "origin",
            "lat",
            "lon",
            "depth_km",
            "magnitude",
            "magnitude_type",
            "magnitudes",
            "place",
            "quality_grade",
            "quality_reasons",
            "status",
            "mode",
            "revision",
            "count",
            "phase_count",
            "rms_s",
            "depth_status",
            "saved_at",
            "enrichment",
            "map_radius_km",
            "review",
            "magnitude_method",
        )
        result = {k: event.get(k) for k in keys}
        result["depth_display"] = depth_label(event)
        if "magnitudes" in result and result["magnitudes"]:
            result["magnitudes"] = [
                {k: m.get(k) for k in ("type", "value", "count", "saturation_risk", "reason")}
                for m in result["magnitudes"]
            ]
        return result

    def revisions(self, event_id):
        with self.connect() as db:
            rows = db.execute(
                "SELECT payload FROM revisions WHERE event_id=? ORDER BY revision", (event_id,)
            ).fetchall()
        return [self.summary(json.loads(r["payload"])) for r in rows]

    def migrate(self, ledger):
        ledger = Path(ledger)
        if not ledger.exists():
            return
        key = "ledger:" + str(ledger.resolve())
        with self.connect() as db:
            if db.execute("SELECT 1 FROM metadata WHERE key=?", (key,)).fetchone():
                return
        for row in json.loads(ledger.read_text()):
            event = dict(row["event"])
            event.setdefault("status", "located")
            event.setdefault("quality_grade", "C")
            event["legacy_image"] = row.get("image")
            eid = str(
                uuid.uuid5(uuid.NAMESPACE_URL, "legacy:" + str(event.get("id", event["origin"])))
            )
            if self.get(eid):
                continue
            result = self.put(event, "live", eid)
            if row.get("sent"):
                with self.connect() as db:
                    db.execute(
                        "UPDATE deliveries SET sent_revision=? WHERE event_id=?",
                        (result["revision"], eid),
                    )
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO metadata VALUES (?,?)", (key, "done"))

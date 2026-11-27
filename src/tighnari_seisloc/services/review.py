"""Durable human review, reversible deletion, and audit history.

Review metadata is kept across automatic revisions. Administrative changes do
not make notifications eligible and never remove scientific files or history.
"""

from copy import deepcopy
import json
import time

MAGNITUDE_FIELDS = (
    "magnitude",
    "magnitude_type",
    "magnitudes",
    "magnitude_count",
    "magnitude_method",
)


def magnitude_priority_sql(alias=""):
    """ORDER BY term: larger earthquakes first, unknown magnitudes last."""
    value = f"json_extract({alias + '.' if alias else ''}payload,'$.magnitude')"
    return f"CASE WHEN typeof({value}) IN ('integer','real') THEN {value} ELSE -99 END DESC"


def visible_sql(alias=""):
    payload = f"{alias}.payload" if alias else "payload"
    return (
        f"coalesce(json_extract({payload},'$.review.deleted'),0)=0 AND "
        f"coalesce(json_extract({payload},'$.review.error'),0)=0"
    )


def review_of(event):
    return {
        **dict(
            status="pending" if event.get("status") == "candidate" else "automatic",
            comment="",
            deleted=False,
            error=False,
            magnitude_override=None,
            version=0,
        ),
        **(event.get("review") or {}),
    }


def automatic_event(event):
    event = deepcopy(event)
    if event.get("magnitude_method") == "manual-review" and event.get("automatic_magnitude"):
        for key, value in event["automatic_magnitude"].items():
            event[key] = value
    event.pop("automatic_magnitude", None)
    event.pop("review", None)
    return event


def with_review(event, review):
    event = automatic_event(event)
    event["review"] = dict(review)
    value = review.get("magnitude_override")
    if value is not None:
        event["automatic_magnitude"] = {key: deepcopy(event.get(key)) for key in MAGNITUDE_FIELDS}
        event.update(
            magnitude=value,
            magnitude_type="ML",
            magnitude_method="manual-review",
            magnitude_count=0,
        )
        event["magnitudes"] = [
            m for m in (event.get("magnitudes") or []) if m.get("type") != "ML"
        ] + [dict(type="ML", value=value, method="manual-review", count=0, stations=[])]
    return event


class ReviewConflict(ValueError):
    pass


class ReviewStoreMixin:
    def review_edit(self, eid, changes, expected_revision, reason):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT payload FROM events WHERE id=?", (eid,)).fetchone()
            if row is None:
                raise KeyError(eid)
            before = json.loads(row[0])
            if before["revision"] != expected_revision:
                raise ReviewConflict("事件已被更新，請重新載入後再儲存")
            review = review_of(before)
            review.update(changes)
            review.update(version=review["version"] + 1, updated_at=time.time())
            event = with_review(before, review)
            event.update(revision=before["revision"] + 1, saved_at=time.time())
            event["notification_revision"] = before.get("notification_revision", before["revision"])
            payload = json.dumps(event, ensure_ascii=False, allow_nan=False)
            db.execute(
                "UPDATE events SET revision=?,payload=?,updated=? WHERE id=?",
                (event["revision"], payload, time.time(), eid),
            )
            self.update_summary(db, event)
            db.execute("INSERT INTO revisions VALUES (?,?,?)", (eid, event["revision"], payload))
            db.execute(
                "INSERT INTO admin_audit(event_id,created_at,action,reason,before_json,after_json) VALUES (?,?,?,?,?,?)",
                (
                    eid,
                    time.time(),
                    "review",
                    reason,
                    json.dumps(review_of(before), ensure_ascii=False),
                    json.dumps(review, ensure_ascii=False),
                ),
            )
        return event

    def admin_listing(self, query="", review_status="all", limit=30, offset=0):
        clauses, params = [], []
        if query:
            clauses.append(
                "(id LIKE ? ESCAPE '\\' OR json_extract(payload,'$.place') LIKE ? ESCAPE '\\')"
            )
            query = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            params += [f"%{query}%"] * 2
        if review_status == "deleted":
            clauses.append("coalesce(json_extract(payload,'$.review.deleted'),0)=1")
        elif review_status == "error":
            clauses.append("coalesce(json_extract(payload,'$.review.error'),0)=1")
            clauses.append("coalesce(json_extract(payload,'$.review.deleted'),0)=0")
        elif review_status != "all":
            clauses.append("coalesce(json_extract(payload,'$.review.deleted'),0)=0")
            clauses.append(
                "coalesce(json_extract(payload,'$.review.status'), CASE WHEN status='candidate' THEN 'pending' ELSE 'automatic' END)=?"
            )
            params.append(review_status)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self.connect() as db:
            total = db.execute("SELECT count(*) FROM events" + where, params).fetchone()[0]
            rows = db.execute(
                "SELECT payload FROM events" + where + " ORDER BY origin DESC,id LIMIT ? OFFSET ?",
                (*params, limit, offset),
            ).fetchall()
        return dict(
            total=total,
            limit=limit,
            offset=offset,
            items=[self.summary(json.loads(row[0])) for row in rows],
        )

    def admin_audit(self, eid, limit=100):
        with self.connect() as db:
            rows = db.execute(
                "SELECT id,created_at,action,reason,before_json,after_json FROM admin_audit "
                "WHERE event_id=? ORDER BY id DESC LIMIT ?",
                (eid, limit),
            ).fetchall()
        return [
            dict(
                id=r[0],
                created_at=r[1],
                action=r[2],
                reason=r[3],
                before=json.loads(r[4]),
                after=json.loads(r[5]),
            )
            for r in rows
        ]

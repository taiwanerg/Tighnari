"""Persisted Discord outbox. Historical events are never eligible."""

import asyncio
import json
import hashlib
import math
import logging
from pathlib import Path
import time
import httpx
from ..config import webhook_urls
from .records import depth_label
from .reporting import local_time, report_images, research_tag
from .review import visible_sql

LOG = logging.getLogger(__name__)


def ml_value(event):
    """Return a finite ML, including valid zero / negative magnitudes."""
    ml = next((m.get("value") for m in event.get("magnitudes", []) if m.get("type") == "ML"), None)
    if ml is None and event.get("magnitude_type") == "ML":
        ml = event.get("magnitude")
    return (
        ml
        if isinstance(ml, (int, float)) and not isinstance(ml, bool) and math.isfinite(ml)
        else None
    )


def content(event):
    """Use ML only: other magnitude scales must not be labelled ML."""
    ml = ml_value(event)
    magnitude = f"ML {ml:.1f}" if ml is not None else "ML 資料不足"
    place = event.get("place")
    if not place:
        from ..processing.geography import place_name

        place = place_name(event["lat"], event["lon"])
    return magnitude + " " + place


def payload(event, public_url=""):
    """Build the Discord message; the detail link needs TIGHNARI_PUBLIC_URL."""
    mag = (
        " / ".join(
            f"{m['type']} {m['value']:.2f}"
            for m in event.get("magnitudes", [])
            if m.get("value") is not None
        )
        or "規模資料不足"
    )
    quality_reasons = event.get("quality_reasons", [])
    other_reasons = [reason for reason in quality_reasons if "深度" not in reason]
    if other_reasons:
        explanation = "、".join(other_reasons)
    elif quality_reasons:
        explanation = "品質原因請見事件詳情"
    else:
        explanation = "通過目前品質條件"
    link = f"\n[詳細資訊]({public_url.rstrip('/')}/events/{event['id']})" if public_url else ""
    return dict(
        username="Tighnari-SeisLOC",
        content=content(event),
        allowed_mentions={"parse": []},
        attachments=[{"id": 0, "filename": "location.png"}],
        embeds=[
            dict(
                title=event.get("place", "推估震央"),
                color=0x8EDBC3,
                description=(mag + "\n" + local_time(event["origin"]) + "（UTC+8）" + link),
                fields=[
                    dict(
                        name="震央",
                        value=f"{event['lat']:.4f}°N {event['lon']:.4f}°E",
                    ),
                    dict(
                        name="深度",
                        value=depth_label(event),
                    ),
                    dict(
                        name="定位品質",
                        value=f"{event.get('quality_grade', 'D')}（品質等級，非機率） · {event['count']} 站 · RMS {event['rms_s']:.3f}s",
                    ),
                    dict(
                        name="說明",
                        value=explanation
                        + ("\n已納入 BATS 震後波形補強" if event.get("enrichment") else ""),
                    ),
                ],
                image={"url": "attachment://location.png"},
                footer={
                    "text": "TWERG Tighnari-SeisLOC 小提自動定位\n僅供初步參考，地震資訊應以氣象署為準"
                },
            )
        ],
    )


# Earthquakes older than this are never posted or edited on Discord.
MAX_NOTIFY_AGE_S = 86400


def send_report(event, webhook_url, image, message_id=None, public_url=""):
    """Send one real report, also used for explicitly requested one-off deliveries."""
    if ml_value(event) is None:
        raise ValueError("Discord delivery requires a finite ML")
    method = "PATCH" if message_id else "POST"
    url = httpx.URL(webhook_url)
    if message_id:
        url = url.copy_with(path=url.path.rstrip("/") + "/messages/" + message_id)
    url = url.copy_merge_params({"wait": "true"})
    with Path(image).open("rb") as file:
        return httpx.request(
            method,
            url,
            timeout=30,
            data={"payload_json": json.dumps(payload(event, public_url), ensure_ascii=False)},
            files={"files[0]": ("location.png", file, "image/png")},
        )


def deliver(store, webhook, reports_dir=None, *, approved_cmt=None, research=None, public_url=""):
    """``research(event)`` returns (research pick or None, settled); see app.research_state."""
    destinations = webhook_urls(webhook)
    if not destinations:
        return
    # Only hashes are persisted; tokens never enter the database or public API.
    targets = [(hashlib.sha256(url.encode()).hexdigest(), url) for url in destinations]
    with store.connect() as db:
        # Carry the old single-destination ledger forward for the first configured URL.
        # Imported sent events without message IDs must never be sent again.
        db.execute(
            "INSERT OR IGNORE INTO metadata VALUES (?,?)",
            ("legacy-webhook-destination", targets[0][0]),
        )
        legacy_key = db.execute(
            "SELECT value FROM metadata WHERE key='legacy-webhook-destination'"
        ).fetchone()[0]
        for key, _ in targets:
            index = 0 if key == legacy_key else 1
            db.execute(
                """INSERT OR IGNORE INTO webhook_deliveries
                (event_id,destination,sent_revision,message_id,retry_at,attempts,error)
                SELECT d.event_id,?,
                    CASE WHEN ?=0 OR (d.sent_revision>0 AND d.message_id IS NULL)
                         THEN d.sent_revision ELSE 0 END,
                    CASE WHEN ?=0 THEN d.message_id ELSE NULL END,
                    CASE WHEN ?=0 THEN d.retry_at ELSE 0 END,
                    CASE WHEN ?=0 THEN d.attempts ELSE 0 END,
                    CASE WHEN ?=0 THEN d.error ELSE NULL END
                FROM deliveries d JOIN events e ON e.id=d.event_id
                WHERE e.mode='live' AND e.status='located'""",
                (key, index, index, index, index, index),
            )
    for key, webhook_url in targets:
        with store.connect() as db:
            rows = db.execute(
                """SELECT e.payload,d.* FROM events e JOIN webhook_deliveries d ON e.id=d.event_id
                WHERE e.mode='live' AND e.status='located' AND d.destination=?
                AND d.sent_revision<coalesce(json_extract(e.payload,'$.notification_revision'),e.revision)
                AND d.retry_at<? AND e.origin>? AND """
                + visible_sql("e"),
                (key, time.time(), time.time() - MAX_NOTIFY_AGE_S),
            ).fetchall()
        for row in rows:
            event = json.loads(row["payload"])
            eligible_revision = event.get("notification_revision", event["revision"])
            if eligible_revision != event["revision"]:
                event = store.get(event["id"], eligible_revision)
                if not event:
                    continue
            if ml_value(event) is None:
                # Leave it pending: a later ML/BATS revision may become eligible.
                continue
            age = time.time() - event["origin"]
            if event.get("summary_only") or age < 90 or age > MAX_NOTIFY_AGE_S:
                continue
            if row["sent_revision"] and not row["message_id"]:
                continue
            try:
                pick = research(event)[0] if research else None
            except Exception as exc:
                # The research pick is optional; the alert itself must still go out.
                LOG.warning("Research pick of %s failed (%s)", event["id"], type(exc).__name__)
                pick = None
            _send(store, key, webhook_url, event, row, reports_dir, approved_cmt, pick, public_url)
        if research:
            _refresh_research(
                store, key, webhook_url, reports_dir, approved_cmt, research, public_url
            )


def _refresh_research(store, key, webhook_url, reports_dir, approved_cmt, research, public_url=""):
    """Patch sent messages whose research pick changed once its jobs have settled."""
    from .adaptive_cmt import MAX_AGE

    now = time.time()
    with store.connect() as db:
        rows = db.execute(
            """SELECT e.payload,d.* FROM events e JOIN webhook_deliveries d ON e.id=d.event_id
            WHERE e.mode='live' AND e.status='located' AND d.destination=?
            AND d.message_id IS NOT NULL AND d.sent_revision=e.revision
            AND d.sent_revision=coalesce(json_extract(e.payload,'$.notification_revision'),e.revision)
            AND e.origin>? AND d.retry_at<? AND """
            + visible_sql("e"),
            (key, now - min(MAX_AGE, MAX_NOTIFY_AGE_S), now),
        ).fetchall()
    for row in rows:
        event = json.loads(row["payload"])
        if event.get("summary_only") or ml_value(event) is None:
            continue
        try:
            pick, settled = research(event)
        except Exception as exc:
            LOG.warning("Research pick of %s failed (%s)", event["id"], type(exc).__name__)
            continue
        # A running job briefly hides its candidates; wait rather than flicker the ball.
        if settled and research_tag(pick) != (row["research_tag"] or ""):
            _send(store, key, webhook_url, event, row, reports_dir, approved_cmt, pick, public_url)


def _send(store, key, webhook_url, event, row, reports_dir, approved_cmt, pick, public_url=""):
    try:
        directory = (
            Path(reports_dir)
            if reports_dir
            else Path(event["image_file"]).parent
            if event.get("image_file")
            else store.path.parent / "reports"
        )
        directory.mkdir(parents=True, exist_ok=True)
        image = report_images(
            event,
            directory,
            travel_cache=directory.parent / "iasp91.npz",
            approved_cmt=approved_cmt(event) if approved_cmt else None,
            research=pick,
        )["overview"]
        response = send_report(event, webhook_url, image, row["message_id"], public_url)
        if response.status_code == 404 and row["message_id"]:
            # A deleted Discord message cannot be patched. Recreate it on the next pass.
            with store.connect() as db:
                db.execute(
                    "UPDATE webhook_deliveries SET message_id=NULL,sent_revision=0 "
                    "WHERE event_id=? AND destination=?",
                    (event["id"], key),
                )
            return
        if response.status_code == 429:
            retry = time.time() + max(1, min(float(response.json().get("retry_after", 60)), 3600))
            with store.connect() as db:
                db.execute(
                    "UPDATE webhook_deliveries SET retry_at=? WHERE event_id=? AND destination=?",
                    (retry, event["id"], key),
                )
            return
        if not response.is_success:
            raise ValueError("Discord HTTP " + str(response.status_code))
        message = response.json()
        if not isinstance(message, dict) or not message.get("id"):
            raise ValueError("Discord response missing message ID")
        with store.connect() as db:
            db.execute(
                """UPDATE webhook_deliveries
                SET sent_revision=?,message_id=?,retry_at=?,error=NULL,research_tag=?
                WHERE event_id=? AND destination=?""",
                (
                    event["revision"],
                    message.get("id"),
                    time.time() + 60,
                    research_tag(pick),
                    event["id"],
                    key,
                ),
            )
    except Exception as exc:
        # One broken artifact must not stop deliveries for every other earthquake.
        # Do not log URLs or exception text, which may contain webhook tokens.
        LOG.warning("Discord report %s failed (%s)", event["id"], type(exc).__name__)
        with store.connect() as db:
            db.execute(
                """UPDATE webhook_deliveries SET attempts=attempts+1,retry_at=?,error=?
                WHERE event_id=? AND destination=?""",
                (
                    time.time() + min(3600, 30 * 2 ** min(row["attempts"], 6)),
                    "傳送失敗，等待重試",
                    event["id"],
                    key,
                ),
            )


async def outbox(
    store, webhook, reports_dir=None, *, approved_cmt=None, research=None, public_url=""
):
    while True:
        try:
            await asyncio.to_thread(
                deliver,
                store,
                webhook,
                reports_dir,
                approved_cmt=approved_cmt,
                research=research,
                public_url=public_url,
            )
        except Exception as exc:
            LOG.warning("Discord outbox failed (%s); retrying", type(exc).__name__)
        await asyncio.sleep(15)

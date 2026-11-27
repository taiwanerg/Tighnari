"""Durable, deduplicated GDMS operational alerts, separate from earthquake outbox."""

import time
import httpx

ATTENTION_RETRY_S = 4 * 3600

AUTH_ERRORS = frozenset(
    {
        "manual_login_required",
        "edge_profile_busy",
        "edge_cookie_handoff_failed",
    }
)


class GdmsAlerts:
    def __init__(self, store, webhook=""):
        self.store, self.webhook = store, webhook
        with store.connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS gdms_auth_state (
                singleton INTEGER PRIMARY KEY CHECK(singleton=1), attention INTEGER NOT NULL,
                episode INTEGER NOT NULL, reason TEXT, updated REAL NOT NULL)""")
            db.execute(
                "INSERT OR IGNORE INTO gdms_auth_state "
                "(singleton,attention,episode,reason,updated) VALUES(1,0,0,NULL,0)"
            )
            columns = {r[1] for r in db.execute("PRAGMA table_info(gdms_auth_state)")}
            for name, definition in (
                ("auto_attempts", "INTEGER NOT NULL DEFAULT 0"),
                ("retry_at", "REAL NOT NULL DEFAULT 0"),
            ):
                if name not in columns:
                    db.execute(f"ALTER TABLE gdms_auth_state ADD COLUMN {name} {definition}")
            db.execute("""CREATE TABLE IF NOT EXISTS gdms_alert_outbox (
                id INTEGER PRIMARY KEY, episode INTEGER NOT NULL, kind TEXT NOT NULL,
                reason TEXT, sent REAL, retry_at REAL NOT NULL DEFAULT 0,
                UNIQUE(episode,kind))""")

    @property
    def blocked(self):
        with self.store.connect() as db:
            return bool(
                db.execute("SELECT attention FROM gdms_auth_state WHERE singleton=1").fetchone()[0]
            )

    def state(self):
        with self.store.connect() as db:
            return dict(db.execute("SELECT * FROM gdms_auth_state WHERE singleton=1").fetchone())

    def login_failed(self, reason):
        """Persist a bounded retry budget so restarts cannot hammer the portal."""
        with self.store.connect() as db:
            db.execute(
                "UPDATE gdms_auth_state SET auto_attempts=auto_attempts+1,"
                "reason=?,updated=? WHERE singleton=1",
                (reason, time.time()),
            )
            attempts = db.execute("SELECT auto_attempts FROM gdms_auth_state").fetchone()[0]
            db.execute(
                "UPDATE gdms_auth_state SET retry_at=? WHERE singleton=1",
                (time.time() + min(900, 60 * 5 ** min(attempts - 1, 2)),),
            )
        # Only a person can log in again; ask at once instead of retrying.
        if attempts >= 3 or reason == "manual_login_required":
            self.attention(reason)

    def clear_non_auth_block(self):
        """Upgrade legacy global download errors into per-job recovery."""
        row = self.state()
        if row["attention"] and row["reason"] and row["reason"] not in AUTH_ERRORS:
            with self.store.connect() as db:
                db.execute(
                    "UPDATE gdms_auth_state SET attention=0,reason=NULL,"
                    "auto_attempts=0,retry_at=0 WHERE singleton=1"
                )
                db.execute(
                    "DELETE FROM gdms_alert_outbox WHERE sent IS NULL "
                    "AND kind='needs_attention' AND episode=?",
                    (row["episode"],),
                )

    def attention(self, reason):
        # Callers pass an enumerated code, never exception text from dependencies.
        with self.store.connect() as db:
            row = db.execute("SELECT * FROM gdms_auth_state WHERE singleton=1").fetchone()
            if row["attention"]:
                return
            episode = row["episode"] + 1
            db.execute(
                "UPDATE gdms_auth_state SET attention=1,episode=?,reason=?,updated=?,retry_at=? "
                "WHERE singleton=1",
                (episode, reason, time.time(), time.time() + ATTENTION_RETRY_S),
            )
            db.execute(
                "INSERT OR IGNORE INTO gdms_alert_outbox(episode,kind,reason) VALUES(?,?,?)",
                (episode, "needs_attention", reason),
            )

    def recovered(self):
        with self.store.connect() as db:
            row = db.execute("SELECT * FROM gdms_auth_state WHERE singleton=1").fetchone()
            db.execute(
                "UPDATE gdms_auth_state SET attention=0,reason=NULL,auto_attempts=0,"
                "retry_at=0,updated=? WHERE singleton=1",
                (time.time(),),
            )
            db.execute(
                "DELETE FROM gdms_alert_outbox WHERE episode=? "
                "AND kind='needs_attention' AND sent IS NULL",
                (row["episode"],),
            )
            notified = db.execute(
                "SELECT 1 FROM gdms_alert_outbox WHERE episode=? "
                "AND kind='needs_attention' AND sent IS NOT NULL",
                (row["episode"],),
            ).fetchone()
            if not row["attention"] and not notified:
                return
            db.execute(
                "INSERT OR IGNORE INTO gdms_alert_outbox(episode,kind) VALUES(?,?)",
                (row["episode"], "recovered"),
            )

    def deliver(self, client=None):
        if not self.webhook:
            return
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                """SELECT * FROM gdms_alert_outbox WHERE sent IS NULL
                AND retry_at<=? ORDER BY id LIMIT 1""",
                (time.time(),),
            ).fetchone()
            if row is not None:
                db.execute(
                    "UPDATE gdms_alert_outbox SET retry_at=? WHERE id=?",
                    (time.time() + 60, row["id"]),
                )
        if row is None:
            return
        text = (
            (
                "GDMS 登入已失效，需要手動登入。下載佇列已保留，未影響即時定位。\n"
                f"原因代碼：{row['reason']}\n"
                "請在運行 Tighnari 的電腦，以相同系統使用者執行：\n"
                "python scripts/gdms_control.py login\n"
                "在開啟的專用 Edge 視窗完成登入；程式會驗證並自動恢復佇列。\n"
                "登入入口：https://gdms.cwa.gov.tw/login.php"
            )
            if row["kind"] == "needs_attention"
            else ("GDMS 登入已恢復，下載佇列繼續執行。")
        )
        payload = dict(username="Tighnari GDMS", content=text, allowed_mentions={"parse": []})
        own = client is None
        client = client or httpx.Client(timeout=15)
        sent, delay = None, 60
        try:
            response = client.post(self.webhook, json=payload)
            if response.is_success:
                sent = time.time()
            elif response.status_code == 429:
                try:
                    delay = max(60, min(3600, float(response.json().get("retry_after", 60))))
                except (ValueError, TypeError):
                    pass
        except httpx.HTTPError:
            pass  # Never log the exception: httpx exceptions include the secret URL.
        finally:
            if own:
                client.close()
        with self.store.connect() as db:
            db.execute(
                "UPDATE gdms_alert_outbox SET sent=?,retry_at=? WHERE id=?",
                (sent, time.time() + delay, row["id"]),
            )

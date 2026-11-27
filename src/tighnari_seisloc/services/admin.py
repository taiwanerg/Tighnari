"""Single-operator Hoshi backend. Credentials never enter public responses.

Reuses the local GDMS credential file. No account creation or remote GDMS login.
Sessions are opaque, persisted as hashes, expire, and invalidate on credential
rotation. Turnstile is verified server-side and fails closed when configured.
"""

import hashlib
import ipaddress
from pathlib import Path
import secrets
import time
from urllib.parse import urlsplit

import httpx
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field, StrictBool
from typing import Literal

from ..config import load_settings
from .review import ReviewConflict, review_of

COOKIE = "hoshi_session"
SESSION_SECONDS = 8 * 3600
WINDOW_SECONDS = 15 * 60
MAX_ATTEMPTS = 5
ROOT = Path(__file__).resolve().parents[1]


class LoginBody(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    username: str = Field(min_length=1, max_length=256)
    password: str = Field(min_length=1, max_length=512)
    turnstile_token: str = Field(default="", max_length=2048)


class EditBody(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    expected_revision: int = Field(ge=1)
    reason: str = Field(min_length=1, max_length=1000)
    status: Literal["pending", "automatic", "confirmed"] | None = None
    comment: str | None = Field(default=None, max_length=5000)
    magnitude_override: float | None = Field(
        default=None, ge=-3, le=10, allow_inf_nan=False, strict=True
    )
    error: StrictBool | None = None
    deleted: StrictBool | None = None


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def local_host(host):
    if host == "localhost":
        return True
    try:
        ip = ipaddress.ip_address(host)
        return (
            ip.is_loopback
            or ip in ipaddress.ip_network("192.168.0.0/16")
            or ip in ipaddress.ip_network("10.0.0.0/8")
            or ip in ipaddress.ip_network("172.16.0.0/12")
        )
    except ValueError:
        return False


class AdminAuth:
    def __init__(self, settings, store):
        self.settings, self.store = settings, store
        self.config = load_settings(settings.root)
        self.site_key = self.config.get("TURNSTILE_SITE_KEY", "")
        self.secret_key = self.config.get("TURNSTILE_SECRET_KEY", "")
        self.frame_origin = self.config.get("TURNSTILE_FRAME_ORIGIN", "").rstrip("/")
        self.lan_origins = self.config.get("ADMIN_LAN_ORIGINS", "").split()
        if self.frame_origin:
            parsed = urlsplit(self.frame_origin)
            if (
                parsed.scheme != "https"
                or not parsed.hostname
                or parsed.username
                or parsed.password
                or parsed.path
                or parsed.query
                or parsed.fragment
            ):
                raise ValueError("TURNSTILE_FRAME_ORIGIN must be an HTTPS origin")
        for origin in self.lan_origins:
            parsed = urlsplit(origin)
            if (
                parsed.scheme not in ("http", "https")
                or not local_host(parsed.hostname)
                or parsed.username
                or parsed.password
                or parsed.path
                or parsed.query
                or parsed.fragment
            ):
                raise ValueError("ADMIN_LAN_ORIGINS must contain local HTTP(S) origins")
        self.cache = None
        with store.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS admin_sessions (
                    token_hash TEXT PRIMARY KEY, csrf TEXT NOT NULL, credential_version TEXT NOT NULL,
                    expires REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS admin_attempts (client TEXT NOT NULL, created_at REAL NOT NULL);
                CREATE INDEX IF NOT EXISTS admin_attempt_time ON admin_attempts(client,created_at);
                CREATE TABLE IF NOT EXISTS admin_challenges (token_hash TEXT PRIMARY KEY, expires REAL NOT NULL);
            """)
            db.execute(
                "INSERT OR IGNORE INTO metadata VALUES ('hoshi_credential_salt',?)",
                (secrets.token_hex(32),),
            )
            self.salt = bytes.fromhex(
                db.execute(
                    "SELECT value FROM metadata WHERE key='hoshi_credential_salt'"
                ).fetchone()[0]
            )

    def credentials(self):
        path = Path(self.config.get("ADMIN_CREDENTIALS_FILE", "login.txt"))
        if not path.is_absolute():
            path = self.settings.root / path
        try:
            raw = path.read_bytes()
            lines = raw.decode("utf-8-sig").splitlines()
            if len(lines) != 2 or not all(lines):
                raise ValueError()
        except (OSError, UnicodeError, ValueError):
            raise HTTPException(503, "管理員憑證尚未設定或格式無效") from None
        fingerprint = hashlib.sha256(raw).digest()
        if self.cache is None or self.cache[0] != fingerprint:
            version = hashlib.scrypt(
                (lines[0] + "\0" + lines[1]).encode(), salt=self.salt, n=16384, r=8, p=1
            ).hex()
            self.cache = (fingerprint, version)
        return self.cache[1]

    def same_origin(self, request):
        origin = request.headers.get("origin")
        if not origin or origin != str(request.base_url).rstrip("/"):
            raise HTTPException(403, "請從同一網站操作後台")
        if request.url.scheme != "https" and not local_host(request.url.hostname):
            raise HTTPException(403, "公開網域的後台登入必須使用 HTTPS")

    def reserve_attempt(self, request):
        # Use the ASGI client's address. Never trust arbitrary forwarded headers.
        client = digest(request.client.host if request.client else "unknown")
        now = time.time()
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM admin_attempts WHERE created_at<?", (now - WINDOW_SECONDS,))
            count = db.execute(
                "SELECT count(*) FROM admin_attempts WHERE client=?", (client,)
            ).fetchone()[0]
            total = db.execute("SELECT count(*) FROM admin_attempts").fetchone()[0]
            if count >= MAX_ATTEMPTS or total >= 100:
                raise HTTPException(
                    429, "登入嘗試過多，請於 15 分鐘後再試", headers={"Retry-After": "900"}
                )
            db.execute("INSERT INTO admin_attempts VALUES (?,?)", (client, now))
        return client

    def session(self, request, write=False):
        token = request.cookies.get(COOKIE, "")
        if not token or len(token) > 128:
            raise HTTPException(401, "請先登入後台")
        with self.store.connect() as db:
            db.execute("DELETE FROM admin_sessions WHERE expires<=?", (time.time(),))
            row = db.execute(
                "SELECT csrf,credential_version,expires FROM admin_sessions WHERE token_hash=?",
                (digest(token),),
            ).fetchone()
        if not row or not secrets.compare_digest(row[1], self.credentials()):
            raise HTTPException(401, "登入已到期，請重新登入")
        if write:
            self.same_origin(request)
            if not secrets.compare_digest(request.headers.get("x-csrf-token", ""), row[0]):
                raise HTTPException(403, "操作驗證失敗，請重新登入")
        return row

    async def captcha(self, token, request):
        if not (self.site_key or self.secret_key):
            return
        if not (self.site_key and self.secret_key):
            raise HTTPException(503, "Turnstile 尚未完成設定")
        if not token:
            raise HTTPException(403, "請先完成人機驗證")
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                response = await client.post(
                    "https://challenges.cloudflare.com/turnstile/v0/siteverify",
                    data={"secret": self.secret_key, "response": token},
                )
                response.raise_for_status()
                result = response.json()
        except (httpx.HTTPError, ValueError):
            raise HTTPException(503, "人機驗證服務暫時無法使用，請稍後重試") from None
        expected_hostname = request.url.hostname
        if self.frame_origin and request.headers.get("origin") in self.lan_origins:
            expected_hostname = urlsplit(self.frame_origin).hostname
        if (
            not isinstance(result, dict)
            or result.get("success") is not True
            or result.get("action") != "hoshi_login"
            or result.get("hostname") != expected_hostname
        ):
            raise HTTPException(403, "人機驗證失敗，請重新驗證")
        # Also reject concurrent replay locally, before a session can be created.
        with self.store.connect() as db:
            db.execute("DELETE FROM admin_challenges WHERE expires<?", (time.time(),))
            row = db.execute(
                "INSERT OR IGNORE INTO admin_challenges VALUES (?,?)",
                (digest(token), time.time() + 300),
            )
            if not row.rowcount:
                raise HTTPException(403, "人機驗證已使用，請重新驗證")


def install_admin(app, settings, store, publication_lock):
    auth = AdminAuth(settings, store)
    app.state.admin_auth = auth
    router = APIRouter(prefix="/hoshi", include_in_schema=False)

    @app.middleware("http")
    async def admin_headers(request, call_next):
        response = await call_next(request)
        if request.url.path == "/hoshi" or request.url.path.startswith("/hoshi/"):
            response.headers.update(
                {
                    "Cache-Control": "no-store",
                    "X-Robots-Tag": "noindex, nofollow",
                    "X-Content-Type-Options": "nosniff",
                    "Referrer-Policy": "same-origin",
                    "Content-Security-Policy": "default-src 'self'; script-src 'self' https://challenges.cloudflare.com; frame-src https://challenges.cloudflare.com; connect-src 'self' https://challenges.cloudflare.com; style-src 'self'; img-src 'self' data:; base-uri 'none'; form-action 'self'; frame-ancestors 'none'",
                }
            )
            if request.url.path == "/hoshi/challenge":
                response.headers["Content-Security-Policy"] = response.headers[
                    "Content-Security-Policy"
                ].replace(
                    "frame-ancestors 'none'", "frame-ancestors 'self' " + " ".join(auth.lan_origins)
                )
            elif auth.frame_origin:
                response.headers["Content-Security-Policy"] = response.headers[
                    "Content-Security-Policy"
                ].replace(
                    "frame-src https://challenges.cloudflare.com;",
                    "frame-src https://challenges.cloudflare.com " + auth.frame_origin + ";",
                )
        return response

    @router.get("")
    @router.get("/")
    def page():
        return HTMLResponse((ROOT / "web/hoshi.html").read_text(encoding="utf-8"))

    @router.get("/api/config")
    def config():
        return dict(
            turnstile_site_key=auth.site_key,
            turnstile_required=bool(auth.site_key or auth.secret_key),
            turnstile_frame_origin=auth.frame_origin,
        )

    @router.get("/challenge")
    def challenge(request: Request, parent_origin: str = ""):
        if (
            not auth.frame_origin
            or str(request.base_url).rstrip("/") != auth.frame_origin
            or parent_origin not in auth.lan_origins
        ):
            raise HTTPException(403, "不允許此驗證來源")
        return HTMLResponse((ROOT / "web/hoshi-challenge.html").read_text(encoding="utf-8"))

    @router.post("/api/login")
    async def login(request: Request):
        auth.same_origin(request)
        # Do not use automatic FastAPI validation here: its default 422 response
        # includes rejected inputs, which can contain a password.
        raw = bytearray()
        async for chunk in request.stream():
            if len(raw) + len(chunk) > 8192:
                raise HTTPException(413, "登入資料過大")
            raw.extend(chunk)
        try:
            body = LoginBody.model_validate_json(bytes(raw))
        except ValueError:
            raise HTTPException(422, "登入資料格式無效") from None
        version = auth.credentials()
        client = auth.reserve_attempt(request)
        await auth.captcha(body.turnstile_token, request)
        supplied = hashlib.scrypt(
            (body.username + "\0" + body.password).encode(), salt=auth.salt, n=16384, r=8, p=1
        ).hex()
        if not secrets.compare_digest(version, supplied):
            raise HTTPException(401, "帳號或密碼錯誤")
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        with store.connect() as db:
            db.execute("DELETE FROM admin_attempts WHERE client=?", (client,))
            db.execute("DELETE FROM admin_sessions WHERE expires<=?", (time.time(),))
            db.execute(
                "INSERT INTO admin_sessions VALUES (?,?,?,?)",
                (digest(token), csrf, version, time.time() + SESSION_SECONDS),
            )
        response = JSONResponse(dict(authenticated=True, csrf_token=csrf))
        response.set_cookie(
            COOKIE,
            token,
            max_age=SESSION_SECONDS,
            httponly=True,
            secure=request.url.scheme == "https",
            samesite="strict",
            path="/hoshi",
        )
        return response

    @router.get("/api/session")
    def session(request: Request):
        row = auth.session(request)
        return dict(authenticated=True, csrf_token=row[0], expires=row[2])

    @router.post("/api/logout")
    def logout(request: Request):
        auth.session(request, write=True)
        with store.connect() as db:
            db.execute(
                "DELETE FROM admin_sessions WHERE token_hash=?", (digest(request.cookies[COOKIE]),)
            )
        response = JSONResponse(dict(authenticated=False))
        response.delete_cookie(
            COOKIE,
            path="/hoshi",
            secure=request.url.scheme == "https",
            httponly=True,
            samesite="strict",
        )
        return response

    @router.get("/api/events")
    def events(
        request: Request,
        q: str = Query("", max_length=256),
        review: Literal["all", "pending", "automatic", "confirmed", "error", "deleted"] = "all",
        limit: int = Query(30, ge=1, le=100),
        offset: int = Query(0, ge=0),
    ):
        auth.session(request)
        result = store.admin_listing(q, review, limit, offset)
        for item in result["items"]:
            item["review"] = review_of(item)
        return result

    @router.get("/api/events/{eid}")
    def detail(eid: str, request: Request):
        auth.session(request)
        event = store.get(eid, include_deleted=True)
        if event is None:
            raise HTTPException(404, "找不到事件")
        return dict(
            event={**store.summary(event), "review": review_of(event)}, audit=store.admin_audit(eid)
        )

    @router.get("/api/events/{eid}/mechanisms")
    def mechanisms(eid: str, request: Request):
        auth.session(request)
        event = store.get(eid, include_deleted=True)
        if event is None:
            raise HTTPException(404, "找不到事件")
        return app.state.adaptive_cmt.diagnostics(event)

    @router.get("/api/events/{eid}/mechanisms/{candidate_id}.png")
    def mechanism_card(eid: str, candidate_id: str, request: Request):
        from io import BytesIO

        from .mechanism_plot import beachball

        result = mechanisms(eid, request) or {}
        candidate = next(
            (
                row
                for row in result.get("candidates", [])
                if row["id"] == candidate_id and row.get("tensor_ned_nm")
            ),
            None,
        )
        if candidate is None:
            raise HTTPException(404, "找不到機制候選")
        image = BytesIO()
        beachball(candidate["tensor_ned_nm"], 120).save(image, format="PNG")
        return Response(image.getvalue(), media_type="image/png")

    @router.get("/api/events/{eid}/mechanisms/{candidate_id}/fits.png")
    def mechanism_fits(eid: str, candidate_id: str, request: Request):
        result = mechanisms(eid, request) or {}
        candidate = next(
            (
                row
                for row in result.get("candidates", [])
                if row["id"] == candidate_id and row.get("fit_image")
            ),
            None,
        )
        event = store.get(eid, include_deleted=True)
        path = candidate and app.state.adaptive_cmt.fit_path(event, candidate_id)
        if not path:
            raise HTTPException(404, "找不到波形擬合圖")
        return Response(path.read_bytes(), media_type="image/png")

    @router.patch("/api/events/{eid}")
    def edit(eid: str, body: EditBody, request: Request):
        auth.session(request, write=True)
        changes = body.model_dump(exclude_unset=True, exclude={"expected_revision", "reason"})
        if (
            not changes
            or any(v is None for k, v in changes.items() if k != "magnitude_override")
            or not body.reason.strip()
        ):
            raise HTTPException(422, "請提供有效的變更及操作原因")
        with publication_lock:
            try:
                event = store.review_edit(eid, changes, body.expected_revision, body.reason.strip())
            except KeyError:
                raise HTTPException(404, "找不到事件") from None
            except ReviewConflict as exc:
                raise HTTPException(409, str(exc)) from None
        return dict(
            event={**store.summary(event), "review": review_of(event)}, audit=store.admin_audit(eid)
        )

    app.include_router(router)

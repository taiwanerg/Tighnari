"""GDMS's observed web protocol over HTTP, using a session from a human login.

Tighnari never submits credentials or solves the portal's captcha. A person logs in
through a visible dedicated Edge window (``manual_login``); the verified session
cookies are then handed to the worker. Portal URLs, cookies and response bodies
must never enter public logs.
"""

from contextlib import contextmanager
import json
import uuid
from pathlib import Path
import re
import time
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

import httpx

BASE = "https://gdms.cwa.gov.tw/"


class PortalError(RuntimeError):
    """A stable public error code, never a response body or private URL.

    ``detail`` may carry a short, URL-redacted server reply for private
    diagnostics (the queue database). It must never reach public status.
    """

    detail = None


def rejection_detail(result, limit=500):
    """Summarize a refused JSON reply for private diagnostics only."""
    if not isinstance(result, dict):
        text = repr(result)
    else:
        text = json.dumps(
            {str(k): result[k] for k in sorted(result, key=str)},
            ensure_ascii=False,
            default=str,
        )
    text = re.sub(r"https?://\S+", "<url>", text)
    text = re.sub(r"[\x00-\x1f\x7f]", " ", text)
    return text[:limit]


class NeedsAttention(PortalError):
    pass


@contextmanager
def profile_lock(directory):
    """Only one Tighnari browser may own this profile; never attach to Chrome."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    lock = directory / "tighnari.lock"
    try:
        handle = lock.open("x")
    except FileExistsError:
        raise NeedsAttention("edge_profile_busy") from None
    try:
        handle.close()
        yield
    finally:
        lock.unlink(missing_ok=True)


class GdmsPortal:
    def __init__(self, *, client=None, profile=None):
        self.client = client or httpx.Client(
            timeout=45, follow_redirects=False, headers={"User-Agent": "Tighnari-SeisLOC/GDMS"}
        )
        self.profile = Path(profile or "runtime/gdms/edge-profile").resolve()
        self.authenticated_at = 0.0
        self.last_auth = {}

    def close(self):
        self.client.close()

    def url(self, path):
        url = urljoin(BASE, path)
        parsed = urlparse(url)
        if parsed.scheme != "https" or parsed.hostname != "gdms.cwa.gov.tw" or parsed.port:
            raise PortalError("unexpected_portal_origin")
        return url

    def request(self, method, path, **kwargs):
        try:
            response = self.client.request(method, self.url(path), **kwargs)
            if response.is_redirect:
                location = response.headers.get("location", "")
                if "login.php" in location:
                    self.authenticated_at = 0
                    raise PortalError("session_expired")
                # Follow only validated same-origin HTTPS redirects.
                if method == "GET":
                    response = self.client.get(self.url(location))
            response.raise_for_status()
            return response
        except httpx.HTTPError:
            raise PortalError("portal_network_error") from None

    def is_authenticated(self):
        try:
            response = self.request("GET", "index.php")
        except PortalError as exc:
            if str(exc) == "session_expired":
                return False
            raise
        return bool(re.search(r"href=[\"\'][^\"\']*logout\.php", response.text, re.I))

    def login(self):
        """No automatic login: an expired session waits for a human (manual_login)."""
        raise NeedsAttention("manual_login_required")

    def ensure_login(self):
        if time.time() - self.authenticated_at < 55 * 60:
            return
        if self.is_authenticated():
            self.authenticated_at = time.time()
            if self.last_auth.get("mode") == "manual-session-pending":
                self.last_auth = dict(mode="manual-edge-http-data")
            return
        self.login()

    def import_session(self, path):
        from .gdms_session import take_session

        cookies = take_session(path)
        if not cookies:
            return False
        previous = list(self.client.cookies.jar)
        self.client.cookies.clear()
        accepted = False
        pending = False
        try:
            self.set_browser_cookies(cookies)
            if not self.is_authenticated():
                return False
            self.authenticated_at = time.time()
            self.last_auth = dict(mode="manual-edge-http-data")
            accepted = True
            return True
        except PortalError:
            # A temporary network failure must not discard a human's login.
            # Keep it in this client and verify it again on the next poll.
            pending = True
            self.authenticated_at = 0
            self.last_auth = dict(mode="manual-session-pending")
            raise
        finally:
            if not accepted and not pending:
                self.client.cookies.clear()
                for cookie in previous:
                    self.client.cookies.jar.set_cookie(cookie)

    def set_browser_cookies(self, cookies):
        for cookie in cookies:
            if cookie.get("domain", "").lstrip(".") == "gdms.cwa.gov.tw":
                self.client.cookies.set(
                    cookie["name"],
                    cookie["value"],
                    domain=cookie["domain"],
                    path=cookie.get("path", "/"),
                )

    def manual_login(self, session_path, timeout=300):
        """Visible dedicated Edge window; hand off only a verified GDMS session."""
        from selenium import webdriver
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support.ui import WebDriverWait
        from .gdms_session import save_session

        with profile_lock(self.profile):
            driver = None
            try:
                options = webdriver.EdgeOptions()
                options.add_argument(f"--user-data-dir={self.profile}")
                options.add_argument("--remote-debugging-port=0")
                options.add_argument("--no-first-run")
                driver = webdriver.Edge(options=options)
                driver.get(BASE + "login.php")
                WebDriverWait(driver, timeout).until(
                    lambda d: bool(d.find_elements(By.CSS_SELECTOR, "a[href*='logout.php']"))
                )
                cookies = driver.get_cookies()
                self.set_browser_cookies(cookies)
                if not self.is_authenticated():
                    raise NeedsAttention("edge_cookie_handoff_failed")
                save_session(
                    session_path,
                    [c for c in cookies if c.get("domain", "").lstrip(".") == "gdms.cwa.gov.tw"],
                )
            except NeedsAttention:
                raise
            except Exception:
                raise NeedsAttention("manual_login_failed") from None
            finally:
                if driver is not None:
                    driver.quit()

    def authenticated_request(self, method, path, **kwargs):
        self.ensure_login()
        try:
            return self.request(method, path, **kwargs)
        except PortalError as exc:
            if str(exc) != "session_expired":
                raise
            self.login()
            # Only reads may be replayed automatically; POST outcome is ambiguous.
            if method != "GET":
                raise PortalError("submission_outcome_unknown") from None
            return self.request(method, path, **kwargs)

    def list_jobs(self):
        """Observed read-only POST; safe to repeat following reauthentication."""
        self.ensure_login()
        for attempt in range(2):
            try:
                data = self.request("POST", "php/dbconnect/getMemberDownList.php").json()
            except ValueError:
                raise PortalError("job_list_protocol_changed") from None
            except PortalError as exc:
                if str(exc) != "session_expired" or attempt:
                    raise
                data = None
            if isinstance(data, list):
                return data
            if attempt == 0:
                self.login()
        raise PortalError("job_list_protocol_changed")

    def station_codes(self):
        self.ensure_login()
        try:
            rows = self.request(
                "POST",
                "php/dbconnect/getStationList.php",
                data={"network_code": "CWASN", "col": "cd"},
            ).json()
        except ValueError:
            raise PortalError("station_list_protocol_changed") from None
        if not isinstance(rows, list) or not all(isinstance(row, str) for row in rows):
            raise PortalError("station_list_protocol_changed")
        return set(rows)

    def submit(self, start, end, label, *, family="HH", stations="all", inventory=False):
        from datetime import datetime, timezone

        if not re.fullmatch(r"[A-Za-z0-9]{1,50}", label):
            raise ValueError("Invalid GDMS label")
        if not inventory and (start >= end or end - start > 86400):
            raise ValueError("Invalid GDMS window")
        if family not in ("HH", "EH", "HN"):
            raise ValueError("Unsupported channel family")

        def stamp(value):
            pattern = "%Y-%m-%d" if inventory else "%Y-%m-%dT%H:%M:%S"
            return datetime.fromtimestamp(value, timezone.utc).strftime(pattern)

        # Match observed page fields. The portal expands 'all' and wildcards itself.
        payload = dict(
            output="staxml" if inventory else "mseed",
            network="CWASN",
            station=stations,
            channel=family + "?",
            location="all",
            stDatetime=stamp(start),
            edDatetime=stamp(end),
            label=label,
        )
        self.ensure_login()
        try:
            result = self.request(
                "POST",
                "php/sendRespDownload.php" if inventory else "php/sendEqdownload.php",
                data=payload,
                headers={
                    "Origin": BASE.rstrip("/"),
                    "Referer": BASE + ("responseDownload.php" if inventory else "eqDownload.php"),
                },
            ).json()
        except (PortalError, ValueError):
            # The server may have accepted the POST before a timeout/invalid reply.
            raise PortalError("submission_outcome_unknown") from None
        if not isinstance(result, dict) or str(result.get("status")) != "1":
            error = str(result.get("error_code", "")) if isinstance(result, dict) else ""
            if error == "201":
                self.authenticated_at = 0
                raise PortalError("session_expired")
            if error == "301":
                raise PortalError("portal_busy")
            public = "submission_rejected" + (
                "_" + error if re.fullmatch(r"[0-9]{1,6}", error) else ""
            )
            exc = PortalError(public)
            exc.detail = rejection_detail(result)
            raise exc
        return result

    def job_links(self, row):
        parser = _Links()
        parser.feed(row.get("show_status", ""))
        links = []
        for href in parser.links:
            url = self.url(href)
            if not urlparse(url).path.startswith("/userdata/"):
                raise PortalError("unexpected_download_path")
            links.append(url)
        return links

    def download(self, url, destination, *, maximum_bytes=512 * 1024 * 1024):
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix(destination.suffix + "." + uuid.uuid4().hex + ".part")
        self.ensure_login()
        try:
            with self.client.stream("GET", self.url(url)) as response:
                if response.is_redirect:
                    self.authenticated_at = 0
                    raise PortalError("session_expired")
                response.raise_for_status()
                size = 0
                with temporary.open("wb") as handle:
                    for chunk in response.iter_bytes():
                        size += len(chunk)
                        if size > maximum_bytes:
                            raise PortalError("download_size_exceeded")
                        handle.write(chunk)
                if size == 0:
                    raise PortalError("empty_download")
            temporary.replace(destination)
        except httpx.HTTPError:
            raise PortalError("download_network_error") from None
        finally:
            temporary.unlink(missing_ok=True)


class _Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            href = dict(attrs).get("href")
            if href:
                self.links.append(href)

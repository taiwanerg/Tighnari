"""HTTP compression negotiation and conditional file responses."""

from fastapi.responses import FileResponse
from starlette.datastructures import Headers
from starlette.middleware.gzip import GZipMiddleware
from starlette.staticfiles import StaticFiles, NotModifiedResponse
import asyncio
import time
from collections import OrderedDict


class SharedTTLCache:
    """Bounded async cache that coalesces concurrent requests for the same data."""

    def __init__(self, ttl=1, maxsize=128):
        self.ttl, self.maxsize = ttl, maxsize
        self.entries = OrderedDict()
        self.lock = asyncio.Lock()

    async def get(self, key, loader):
        async with self.lock:
            entry = self.entries.get(key)
            if entry is None or time.monotonic() - entry[0] >= self.ttl:
                value = await loader()
                self.entries[key] = (time.monotonic(), value)
                if len(self.entries) > self.maxsize:
                    self.entries.popitem(last=False)
            self.entries.move_to_end(key)
            return self.entries[key][1]


class CachedStaticFiles(StaticFiles):
    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = (
            "public, max-age=86400"
            if b"v=" in scope.get("query_string", b"")
            else "public, no-cache"
        )
        return response


def accepts_gzip(value):
    encodings = {}
    for part in value.lower().split(","):
        coding, *parameters = part.strip().split(";")
        try:
            encodings[coding] = float(
                next((p.strip()[2:] for p in parameters if p.strip().startswith("q=")), "1")
            )
        except ValueError:
            encodings[coding] = 0
    return encodings.get("gzip", encodings.get("*", 0)) > 0


class NegotiatedGZipMiddleware(GZipMiddleware):
    async def __call__(self, scope, receive, send):
        # Some Starlette versions only check for the substring "gzip", ignoring q=0.
        if scope["type"] == "http" and scope.get("path") in ("/api/stream", "/api/events"):
            await self.app(scope, receive, send)
            return
        if scope["type"] == "http" and accepts_gzip(
            Headers(scope=scope).get("accept-encoding", "")
        ):
            await super().__call__(scope, receive, send)
        else:
            await self.app(scope, receive, send)


def cached_file(request, path, *, media_type=None, headers=None, filename=None):
    response = FileResponse(
        path,
        media_type=media_type,
        filename=filename,
        stat_result=path.stat(),
        headers={"Cache-Control": "public, no-cache", **(headers or {})},
    )
    if StaticFiles().is_not_modified(response.headers, request.headers):
        return NotModifiedResponse(response.headers)
    return response

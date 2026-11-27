"""Short-lived, one-use local cookie handoff from interactive login to the worker."""

import base64
import ctypes
import json
import os
from pathlib import Path
import time
import uuid


def windows_protect(data, decrypt=False):
    """DPAPI binds the handoff to the same Windows user as the live service."""
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]

    buffer = ctypes.create_string_buffer(data)
    source = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    result = Blob()
    crypt = ctypes.windll.crypt32
    kernel = ctypes.windll.kernel32
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    if decrypt:
        ok = crypt.CryptUnprotectData(
            ctypes.byref(source), None, None, None, None, 1, ctypes.byref(result)
        )
    else:
        ok = crypt.CryptProtectData(
            ctypes.byref(source), None, None, None, None, 1, ctypes.byref(result)
        )
    if not ok:
        raise OSError("session_protection_failed")
    try:
        return ctypes.string_at(result.data, result.size)
    finally:
        kernel.LocalFree(ctypes.cast(result.data, ctypes.c_void_p))


def save_session(path, cookies):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(dict(created=time.time(), cookies=cookies)).encode()
    if os.name == "nt":
        data = b"dpapi:" + base64.b64encode(windows_protect(data))
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex)
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def take_session(path):
    """Consume even invalid/expired handoffs; never expose cookie data in errors."""
    path = Path(path)
    claimed = path.with_name(path.name + "." + uuid.uuid4().hex)
    try:
        path.rename(claimed)
    except FileNotFoundError:
        return None
    try:
        data = claimed.read_bytes()
        if data.startswith(b"dpapi:"):
            data = windows_protect(base64.b64decode(data[6:]), decrypt=True)
        result = json.loads(data)
        if not 0 <= time.time() - result["created"] <= 600:
            return None
        cookies = result["cookies"]
        if not isinstance(cookies, list) or not cookies or len(cookies) > 100:
            return None
        for cookie in cookies:
            if cookie.get("domain", "").lstrip(".") != "gdms.cwa.gov.tw" or not all(
                isinstance(cookie.get(k), str) for k in ("name", "value", "path")
            ):
                return None
        return cookies
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None
    finally:
        claimed.unlink(missing_ok=True)

"""Minimal HTTP/FTP fetch helper for metadata and checksum manifests."""

from __future__ import annotations

import ssl
import time
import urllib.request
from typing import Optional

UA = "seqfetch/0.1 (+https://github.com/)"


def _context(insecure: bool) -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    if insecure:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


def get_bytes(
    url: str,
    timeout: int = 180,
    retries: int = 3,
    insecure: bool = True,
    headers: Optional[dict] = None,
) -> bytes:
    last: Optional[Exception] = None
    for attempt in range(max(1, retries)):
        try:
            request = urllib.request.Request(url, headers={"User-Agent": UA, **(headers or {})})
            with urllib.request.urlopen(request, timeout=timeout, context=_context(insecure)) as response:
                return response.read()
        except Exception as exc:  # noqa: BLE001 - urllib raises many types
            last = exc
            if attempt + 1 < retries:
                time.sleep(1.5 * (attempt + 1))
    assert last is not None
    raise last


def head_size(
    url: str,
    timeout: int = 60,
    insecure: bool = True,
) -> Optional[int]:
    """Return Content-Length for http(s) URLs, or None when unavailable."""
    request = urllib.request.Request(url, method="HEAD", headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(request, timeout=timeout, context=_context(insecure)) as response:
            value = response.headers.get("Content-Length")
            return int(value) if value else None
    except Exception:  # noqa: BLE001
        return None

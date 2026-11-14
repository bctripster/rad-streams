"""HTTP session and small on-disk JSON helpers shared by the data sources."""
from __future__ import annotations

import json
import math
import os
import tempfile
import threading
import time
from dataclasses import dataclass
from typing import Any, Optional

import requests

USER_AGENT = "RadStreams/0.1 (Dispatcharr plugin; internet radio TV channels)"
TIMEOUT = (6, 20)  # connect, read
API_TIMEOUT = (6, 8)  # JSON APIs answer fast or not at all; retry instead of waiting
MAX_RETRY_WAIT = 30.0

# Upper limits for bodies that are read whole. A URL that looks like a
# playlist can turn out to be a live stream that never ends, so nothing is
# ever read without a size cap and an overall deadline.
MAX_PLAYLIST_BYTES = 256 * 1024          # .pls / .m3u lists of stream URLs
MAX_HLS_PLAYLIST_BYTES = 2 * 1024 * 1024  # live HLS playlists can list hours of segments
MAX_SEGMENT_BYTES = 32 * 1024 * 1024      # one HLS audio segment

_local = threading.local()


def session() -> requests.Session:
    """One requests.Session per thread (Session is not thread-safe)."""
    s = getattr(_local, "session", None)
    if s is None:
        s = requests.Session()
        s.headers["User-Agent"] = USER_AGENT
        _local.session = s
    return s


class TooLarge(ValueError):
    """A response body went past its size cap (it may be an endless stream)."""


@dataclass
class Fetched:
    """The parts of a fully read, size-capped response that callers use."""
    status_code: int
    url: str            # the final URL, after redirects
    content: bytes

    @property
    def text(self) -> str:
        return self.content.decode("utf-8", "replace")


def fetch_limited(url: str, max_bytes: int, *, timeout=TIMEOUT, deadline: float = 30.0,
                  ok_statuses: tuple = ()) -> Fetched:
    """GET ``url`` and return its body, reading at most ``max_bytes``.

    Raises ``TooLarge`` past the cap and ``TimeoutError`` when the whole
    download takes longer than ``deadline`` seconds (the per-read timeout only
    limits the gap between bytes, so a slow endless stream would never trip
    it). HTTP errors raise as usual, except statuses in ``ok_statuses``, which
    are returned with an empty body for the caller to handle.
    """
    started = time.monotonic()
    # The ``with`` block always closes the connection, including on the error paths.
    with session().get(url, timeout=timeout, stream=True) as r:
        if r.status_code in ok_statuses:
            return Fetched(r.status_code, r.url, b"")
        r.raise_for_status()
        declared = r.headers.get("Content-Length", "")
        if declared.isdigit() and int(declared) > max_bytes:
            raise TooLarge(f"response is {int(declared)} bytes (limit {max_bytes})")
        body = bytearray()
        for chunk in r.iter_content(65536):
            body += chunk
            if len(body) > max_bytes:
                raise TooLarge(f"response is larger than {max_bytes} bytes; "
                               "it may be a live stream, not a file")
            if time.monotonic() - started > deadline:
                raise TimeoutError(f"download took longer than {deadline:.0f}s")
        return Fetched(r.status_code, r.url, bytes(body))


def get_json(url: str, params: Optional[dict] = None, tries: int = 4) -> Any:
    """GET JSON, retrying hangs, 429s and 5xx (Commons rate-limits, Open-Meteo
    geocoding sometimes stalls). 429/503 honor Retry-After, capped."""
    for attempt in range(tries):
        last = attempt == tries - 1
        try:
            r = session().get(url, params=params, timeout=API_TIMEOUT)
        except (requests.ConnectionError, requests.Timeout):
            if last:
                raise
            time.sleep(1 + attempt)
            continue
        if r.status_code == 429 or r.status_code >= 500:
            if last:
                r.raise_for_status()
            try:
                wait = float(r.headers.get("Retry-After", ""))
            except ValueError:
                wait = float("nan")
            if not math.isfinite(wait):     # missing, an HTTP date, or "nan"/"inf"
                wait = 2.0 * (attempt + 1)
            time.sleep(min(max(wait, 1.0), MAX_RETRY_WAIT))
            continue
        r.raise_for_status()
        return r.json()


def read_json(path: str) -> Optional[Any]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def write_json(path: str, data: Any) -> None:
    """Write atomically so a reader never sees a half-written file."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise

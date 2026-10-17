"""HTTP session and small on-disk JSON helpers shared by the data sources."""
from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from typing import Any, Optional

import requests

USER_AGENT = "RadStreams/0.1 (Dispatcharr plugin; internet radio TV channels)"
TIMEOUT = (6, 20)  # connect, read
API_TIMEOUT = (6, 8)  # JSON APIs answer fast or not at all; retry instead of waiting
MAX_RETRY_WAIT = 30.0

_local = threading.local()


def session() -> requests.Session:
    """One requests.Session per thread (Session is not thread-safe)."""
    s = getattr(_local, "session", None)
    if s is None:
        s = requests.Session()
        s.headers["User-Agent"] = USER_AGENT
        _local.session = s
    return s


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

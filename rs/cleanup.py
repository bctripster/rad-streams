"""Removes cache files nothing uses any more, so the photo cache can't grow forever.

Each city keeps only its current set of photos (``ImageLibrary`` prunes the
rest on every refresh). This sweep handles what that can't see:

- whole city folders no station has opened for ``max_age_days`` (a station
  that was removed, renamed, or never watched; it refetches if it comes back),
- splash images from older drawings or resolutions no longer in use,
- temporary files left behind by an interrupted download.
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import time
from typing import Iterable

from . import images, net

log = logging.getLogger("rad_streams.cleanup")

MAX_AGE_DAYS = 30
TMP_AGE_SECONDS = 3600
SPLASH_RE = re.compile(r"^splash-v\d+-\d+x\d+\.png$")


def _age(path: str, now: float) -> float:
    try:
        return now - os.stat(path).st_mtime
    except OSError:
        return 0.0


def sweep(cache_dir: str, keep_splash: Iterable[str] = (), max_age_days: float = MAX_AGE_DAYS,
          now: float | None = None) -> int:
    """Delete stale cache entries under ``cache_dir``. Returns how many were removed."""
    now = time.time() if now is None else now
    removed = 0
    keep_splash = set(keep_splash)
    try:
        names = os.listdir(cache_dir)
    except OSError:
        return 0
    for name in names:
        path = os.path.join(cache_dir, name)
        stale_splash = SPLASH_RE.match(name) and name not in keep_splash
        stale_tmp = name.endswith(".tmp") and _age(path, now) > TMP_AGE_SECONDS
        if (stale_splash or stale_tmp) and os.path.isfile(path):
            try:
                os.unlink(path)
                removed += 1
            except OSError:
                pass

    root = os.path.join(cache_dir, "images")
    try:
        cities = os.listdir(root)
    except OSError:
        cities = []
    for city in cities:
        d = os.path.join(root, city)
        if not os.path.isdir(d):
            continue
        lock = images._dir_lock(d)
        if not lock.acquire(blocking=False):
            continue  # a refresh is writing here right now
        try:
            index = os.path.join(d, "index.json")
            # index.json is touched whenever a station opens the city and rewritten on refresh.
            marker = index if os.path.exists(index) else d
            if _age(marker, now) > max_age_days * 86400:
                shutil.rmtree(d, ignore_errors=True)
                removed += 1
                log.info("removed photos for %s (unused for %d days)", city, max_age_days)
                continue
            keep = {e.get("file") for e in (net.read_json(index) or {}).get("images", [])}
            for f in os.listdir(d):
                p = os.path.join(d, f)
                if f == "index.json" or f in keep or not os.path.isfile(p):
                    continue
                # Leftovers (an interrupted download, an older set); give a running download time.
                if _age(p, now) > TMP_AGE_SECONDS:
                    try:
                        os.unlink(p)
                        removed += 1
                    except OSError:
                        pass
        finally:
            lock.release()
    return removed

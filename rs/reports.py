"""Saved Sync and Validate reports.

Dispatcharr's pop-up holds about 280 characters, so every Sync and Validate
writes its full report to a file. Each run gets its own timestamped file in
``<data>/reports/`` (the newest ``KEEP`` per kind are kept), and the latest
one is also copied to ``<data>/logs/<kind>.log``, where earlier versions put
it, so existing bookmarks and habits keep working.

Report text can include stream URLs, which sometimes carry a listener token,
so the files are created owner-read/write only.
"""
from __future__ import annotations

import os
import re
import tempfile
import time
from pathlib import Path
from typing import Optional

KEEP = 20                       # reports kept per kind
KINDS = ("sync", "validate")    # the only names a report may have
# kind-YYYYmmdd-HHMMSS[-n].txt; n counts runs within the same second.
_NAME_RE = re.compile(r"^(sync|validate)-(\d{8}-\d{6})(?:-(\d{1,6}))?\.txt$")


def _order(path: Path) -> tuple:
    """Sort key: timestamp, then the same-second counter (as a number)."""
    m = _NAME_RE.match(path.name)
    return (m.group(2), int(m.group(3) or 0)) if m else ("", 0)


def _write_private(path: Path, text: str) -> None:
    """Write ``text`` atomically with 0600 permissions (temp file + rename)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".report-", suffix=".tmp")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text if text.endswith("\n") else text + "\n")
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def save(data_dir: str, kind: str, text: str, now: Optional[float] = None) -> Path:
    """Save one report and return the path of its timestamped copy.

    ``kind`` must be one of ``KINDS``: it becomes part of a file name, so it is
    never taken from user input. Older reports beyond ``KEEP`` are deleted.
    """
    if kind not in KINDS:
        raise ValueError(f"unknown report kind {kind!r}")
    base = Path(data_dir)
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(now if now is not None else time.time()))
    folder = base / "reports"
    # Two runs in the same second: count on from the highest number used in
    # that second, so a name freed by pruning is never reused for a newer run.
    same = [_order(p)[1] for p in listing(folder, kind) if _order(p)[0] == stamp]
    path = folder / (f"{kind}-{stamp}-{max(same) + 1}.txt" if same else f"{kind}-{stamp}.txt")
    _write_private(path, text)
    _write_private(base / "logs" / f"{kind}.log", text)
    prune(folder, kind)
    return path


def listing(folder: Path, kind: str) -> list[Path]:
    """This kind's saved reports, oldest first (the names sort by time)."""
    try:
        names = [p for p in folder.iterdir()
                 if p.is_file() and _NAME_RE.match(p.name) and p.name.startswith(kind + "-")]
    except OSError:
        return []
    return sorted(names, key=_order)


def prune(folder: Path, kind: str, keep: int = KEEP) -> int:
    """Delete all but the newest ``keep`` reports of ``kind``; returns how many went.

    Only files whose names match the report pattern are ever touched.
    """
    old = listing(folder, kind)[:-keep] if keep > 0 else listing(folder, kind)
    gone = 0
    for path in old:
        try:
            path.unlink()
            gone += 1
        except OSError:
            pass
    return gone


def latest(data_dir: str, kind: str) -> Optional[Path]:
    """The newest saved report of ``kind``, or None."""
    found = listing(Path(data_dir) / "reports", kind)
    return found[-1] if found else None

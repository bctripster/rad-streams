"""Checks behind the plugin's Validate action.

For each station: is the stream reachable, does it send ICY titles, does the
city resolve, and are there usable photos. Stations are checked in parallel
with hard time limits. Since v0.6.0 Validate runs as a Dispatcharr background
task, so the overall limit grows with the number of stations instead of being
squeezed into the few seconds an awaited button press allows.
"""
from __future__ import annotations

import concurrent.futures as cf
import math
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

import requests

from . import audio, geo, hls, images, net, themes
from .config import Station

STREAM_SECONDS = 12      # max time listening for an ICY title
TOTAL_SECONDS = 40       # whole-run budget floor (and the old fixed budget)
STATION_SECONDS = 45     # budget per round of WORKERS stations checked in parallel
MAX_TOTAL_SECONDS = 900  # never let one Validate run longer than this
WORKERS = 8


def budget_for(count: int) -> float:
    """Seconds a Validate of ``count`` stations may take in total.

    Stations are checked ``WORKERS`` at a time, and one station's checks
    (stream, title, city, photos) take up to about ``STATION_SECONDS``.
    """
    rounds = math.ceil(max(count, 1) / WORKERS)
    return float(min(MAX_TOTAL_SECONDS, max(TOTAL_SECONDS, STATION_SECONDS * rounds)))


@dataclass
class StationReport:
    station: Station
    ok: bool = True
    problems: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def problem(self, text: str) -> None:
        self.ok = False
        self.problems.append(text)


def check_stream(url: str, station_name: str, seconds: float = STREAM_SECONDS) -> dict:
    """Connect, read headers and a few metadata blocks. Never raises."""
    out = {"reachable": False, "content_type": None, "metaint": 0, "title": None,
           "raw_title": None, "resolved_url": url, "error": None}
    try:
        resolved, is_hls = audio.resolve_stream(url)
        out["resolved_url"] = resolved
        if is_hls:
            return _check_hls(resolved, station_name, out)
        headers = {"Icy-MetaData": "1", "User-Agent": net.USER_AGENT}
        with requests.get(resolved, headers=headers, stream=True, timeout=(8, 10)) as r:
            r.raise_for_status()
            out["reachable"] = True
            out["content_type"] = r.headers.get("Content-Type", "")
            if "mpegurl" in out["content_type"].lower() and hls.looks_like_hls(
                    r.raw.read(65536).decode("utf-8", "replace")):   # capped: it may be a live stream
                return _check_hls(r.url, station_name, out)
            metaint = int(r.headers.get("icy-metaint") or 0)
            out["metaint"] = metaint
            if not metaint:
                return out
            raw, deadline = r.raw, time.monotonic() + seconds
            while time.monotonic() < deadline:
                audio._read_exact(raw, metaint)
                length = audio._read_exact(raw, 1)[0] * 16
                if length:
                    title = audio.parse_stream_title(audio.decode_meta(audio._read_exact(raw, length)))
                    if title is not None:
                        out["raw_title"] = title
                        out["title"] = audio.clean_title(title, station_name)
                        if out["title"]:
                            break
    except Exception as exc:
        out["error"] = f"{type(exc).__name__}: {exc}"
    return out


def _check_hls(url: str, station_name: str, out: dict) -> dict:
    """HLS: fetch up to two segments, looking for a title in the playlist or ID3."""
    import threading
    out["content_type"] = "HLS"
    stop = threading.Event()
    try:
        for i, (body, title) in enumerate(hls.segments(url, stop)):
            out["reachable"] = True
            out["metaint"] = -1          # titles come from HLS metadata, not ICY
            if title is not None:
                out["raw_title"] = title
                out["title"] = audio.clean_title(title, station_name)
            if out["title"] or i >= 1:
                break
    except Exception as exc:
        out["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        stop.set()
    return out


def validate_station(station: Station, cache_dir: Optional[str]) -> StationReport:
    rep = StationReport(station)

    s = check_stream(station.stream_url, station.name)
    if not s["reachable"]:
        rep.problem(f"stream unreachable ({s['error']})")
    else:
        rep.notes.append(f"stream OK: {s['content_type'] or 'unknown type'}"
                         + (f" via {s['resolved_url']}" if s["resolved_url"] != station.stream_url else ""))
        if not s["metaint"]:
            rep.problem("no ICY metadata; the station name will show instead of track titles")
        elif s["metaint"] < 0 and not s["title"] and s["raw_title"] is None:
            rep.problem("HLS stream sends no track titles; the station name will show")
        elif s["title"]:
            rep.notes.append(f"now playing: {s['title']}")
        elif s["raw_title"] is not None:
            rep.problem(f"track title is generic ({s['raw_title']!r}); the station name will show")
        else:
            rep.problem(f"ICY metadata enabled but no title seen in {STREAM_SECONDS}s")
        if s["error"]:
            rep.notes.append(f"stream read stopped early: {s['error']}")

    theme = themes.parse(station.city)
    if theme:
        rep.notes.append(f"theme: {theme.label} (no city or weather)")
        try:
            found, tiers = images.find_candidates(theme.label, None, None, 1920, want=10,
                                                  queries=theme.queries(), theme_filters=theme.filters())
            if found:
                rep.notes.append(f"photos: {len(found)} usable ({', '.join(tiers)})")
            else:
                rep.problem(f"no usable Wikimedia Commons photos for theme '{theme.label}'; "
                            "try a different word")
        except Exception as exc:
            rep.problem(f"photo search failed ({exc})")
        return rep

    place, lookup_failed = None, False
    try:
        place = geo.geocode(station.city, cache_dir)
    except Exception as exc:
        lookup_failed = True
        rep.problem(f"city lookup failed ({exc})")
    if place:
        rep.notes.append(f"city: {place.label} ({place.lat:.3f}, {place.lon:.3f}, {place.timezone})")
    elif not lookup_failed:
        rep.problem(f"city '{station.city}' not found; try 'City, ST' or 'City, Country'")

    try:
        found, tiers = images.find_candidates(place.label if place else station.city,
                                              place.lat if place else None,
                                              place.lon if place else None, 1920, want=10,
                                              region=(place.admin1 or place.country) if place else None)
        if found:
            rep.notes.append(f"photos: {len(found)} usable ({', '.join(tiers)})")
        else:
            rep.problem("no usable Wikimedia Commons photos; the Rad Streams splash will show instead")
    except Exception as exc:
        rep.problem(f"photo search failed ({exc})")
    return rep


def validate_all(stations: list[Station], cache_dir: Optional[str],
                 budget: Optional[float] = None,
                 on_done: Optional[Callable[[int, int, StationReport], None]] = None
                 ) -> list[StationReport]:
    """Check every station, ``WORKERS`` at a time, within ``budget`` seconds.

    ``budget`` defaults to ``budget_for(len(stations))``. ``on_done(done,
    total, report)`` is called in the caller's thread as each station
    finishes, for a progress bar; an error in it is ignored. A station still
    running when the budget ends gets a "did not finish" problem.
    """
    if budget is None:
        budget = budget_for(len(stations))
    reports: dict[int, StationReport] = {}
    if not stations:
        return []
    pool = cf.ThreadPoolExecutor(max_workers=min(WORKERS, len(stations)))
    futures = {pool.submit(validate_station, s, cache_dir): i for i, s in enumerate(stations)}
    try:
        for fut in cf.as_completed(futures, timeout=budget):
            i = futures[fut]
            try:
                reports[i] = fut.result()
            except Exception as exc:          # validate_station catches its own; belt and braces
                reports[i] = StationReport(stations[i])
                reports[i].problem(f"check failed ({type(exc).__name__}: {exc})")
            if on_done is not None:
                try:
                    on_done(len(reports), len(stations), reports[i])
                except Exception:
                    pass                      # a progress bar never fails a Validate
    except cf.TimeoutError:
        pass
    # Don't wait for stragglers: their sockets have their own timeouts.
    pool.shutdown(wait=False, cancel_futures=True)
    out = []
    for i, s in enumerate(stations):
        rep = reports.get(i)
        if rep is None:
            rep = StationReport(s)
            rep.problem(f"checks did not finish within {budget:.0f}s")
        out.append(rep)
    return out


def as_dicts(reports: list[StationReport]) -> list[dict]:
    """JSON-ready reports, kept in Dispatcharr's task history with the result."""
    return [{"name": r.station.name, "city": r.station.city, "stream_url": r.station.stream_url,
             "row": r.station.line_no, "ok": r.ok, "problems": list(r.problems),
             "notes": list(r.notes)} for r in reports]


def format_report(reports: list[StationReport], parse_errors: list[str]) -> str:
    lines = [f"Rad Streams validation, {time.strftime('%Y-%m-%d %H:%M:%S')}", ""]
    for e in parse_errors:
        lines.append(f"Station list: {e}")
    if parse_errors:
        lines.append("")
    for r in reports:
        lines.append(f"[{'OK' if r.ok else 'PROBLEM'}] {r.station.name}  ({r.station.city})")
        lines.append(f"    {r.station.stream_url}")
        for p in r.problems:
            lines.append(f"    ! {p}")
        for n in r.notes:
            lines.append(f"    - {n}")
        lines.append("")
    return "\n".join(lines)


def summarize(reports: list[StationReport], parse_errors: list[str], log_path: str,
              limit: int = 280) -> str:
    """Short message for the plugin pop-up (about 280 characters)."""
    bad = [r for r in reports if not r.ok]
    head = f"{len(reports) - len(bad)}/{len(reports)} stations OK"
    if parse_errors:
        head += f", {len(parse_errors)} bad line(s)"
    detail = "; ".join(f"{r.station.name}: {r.problems[0]}" for r in bad)
    tail = f" Full report: {log_path}"
    msg = head + (f". {detail}" if detail else ".")
    room = limit - len(tail)
    if len(msg) > room:
        msg = msg[: max(room - 1, 0)].rstrip() + "…"
    return msg + tail

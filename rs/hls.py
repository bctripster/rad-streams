"""Minimal live HLS audio client (``.m3u8`` radio streams).

Follows a master playlist to one audio variant, polls the media playlist,
and yields each new segment's bytes in order, with any track title found in
the playlist (``#EXTINF:...,title="...",artist="..."`` as Triton/StreamTheWorld
and others send) or in the segment's ID3 tag (packed AAC). The caller feeds
the bytes to the same ffmpeg decoder used for Icecast streams.

Not supported: encrypted (#EXT-X-KEY) and fMP4 (#EXT-X-MAP) streams.
"""
from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Iterator, Optional
from urllib.parse import urljoin

import requests

from . import net

log = logging.getLogger("rad_streams.hls")

LIVE_EDGE_SEGMENTS = 3   # start this many segments behind the newest one


def is_hls(url: str, content_type: str = "") -> bool:
    ct = (content_type or "").split(";")[0].strip().lower()
    return (url.lower().split("?")[0].endswith(".m3u8")
            or ct in ("application/vnd.apple.mpegurl", "application/x-mpegurl"))


def looks_like_hls(text: str) -> bool:
    """A playlist body that is HLS rather than a plain .m3u list of streams."""
    return text.lstrip().startswith("#EXTM3U") and ("#EXT-X-TARGETDURATION" in text
                                                    or "#EXT-X-STREAM-INF" in text)


@dataclass
class Segment:
    seq: int
    url: str
    duration: float
    title: Optional[str] = None


@dataclass
class Playlist:
    variants: list[tuple[int, str]] = field(default_factory=list)   # (bandwidth, url)
    segments: list[Segment] = field(default_factory=list)
    target: float = 6.0
    ended: bool = False
    encrypted: bool = False
    fmp4: bool = False


_ATTR = re.compile(r'([A-Za-z_-]+)\s*=\s*"((?:[^"\\]|\\.)*)"')


def extinf_title(info: str) -> Optional[str]:
    """Title from the part of an #EXTINF line after the duration.

    Handles ``title="Song",artist="Band"`` attributes and plain ``Band - Song``.
    """
    rest = info.split(",", 1)[1].strip() if "," in info else ""
    if not rest:
        return None
    attrs = {k.lower(): v.strip() for k, v in _ATTR.findall(rest)}
    if attrs:
        title, artist = attrs.get("title", ""), attrs.get("artist", "")
        if title and artist:
            return f"{artist} - {title}"
        return title or artist or None
    return rest


def parse_playlist(text: str, base: str) -> Playlist:
    pl = Playlist()
    if not text.lstrip().startswith("#EXTM3U"):
        raise ValueError("not an HLS playlist")
    seq = 0
    pending_dur, pending_title, pending_bw = None, None, None
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#EXT-X-STREAM-INF"):
            m = re.search(r"BANDWIDTH=(\d+)", line)
            pending_bw = int(m.group(1)) if m else 0
        elif line.startswith("#EXT-X-MEDIA-SEQUENCE:"):
            seq = int(line.split(":", 1)[1])
        elif line.startswith("#EXT-X-TARGETDURATION:"):
            pl.target = float(line.split(":", 1)[1])
        elif line.startswith("#EXT-X-ENDLIST"):
            pl.ended = True
        elif line.startswith("#EXT-X-KEY") and "METHOD=NONE" not in line:
            pl.encrypted = True
        elif line.startswith("#EXT-X-MAP"):
            pl.fmp4 = True
        elif line.startswith("#EXTINF:"):
            info = line[len("#EXTINF:"):]
            try:
                pending_dur = float(info.split(",", 1)[0])
            except ValueError:
                pending_dur = pl.target
            pending_title = extinf_title(info)
        elif not line.startswith("#"):
            url = urljoin(base, line)
            if pending_bw is not None:
                pl.variants.append((pending_bw, url))
                pending_bw = None
            else:
                pl.segments.append(Segment(seq, url, pending_dur or pl.target, pending_title))
                seq += 1
                pending_dur, pending_title = None, None
    return pl


# ---- ID3 (packed audio segments carry their metadata in an ID3v2 header) ----
def _syncsafe(b: bytes) -> int:
    return (b[0] << 21) | (b[1] << 14) | (b[2] << 7) | b[3]


def _id3_text(payload: bytes) -> str:
    if not payload:
        return ""
    enc, data = payload[0], payload[1:]
    codec = {0: "latin-1", 1: "utf-16", 2: "utf-16-be", 3: "utf-8"}.get(enc, "latin-1")
    try:
        return data.decode(codec).strip("\x00 ").replace("\x00", " / ")
    except UnicodeDecodeError:
        return ""


def split_id3(data: bytes) -> tuple[dict, bytes]:
    """Strip a leading ID3v2 tag. Returns ({frame id: text}, remaining bytes)."""
    if len(data) < 10 or data[:3] != b"ID3":
        return {}, data
    version, flags = data[3], data[5]
    size = _syncsafe(data[6:10])
    end = 10 + size + (10 if flags & 0x10 else 0)
    frames: dict = {}
    pos = 10
    while pos + 10 <= min(end, len(data)):
        fid = data[pos:pos + 4]
        if not fid.strip(b"\x00"):
            break
        fsize = _syncsafe(data[pos + 4:pos + 8]) if version == 4 else int.from_bytes(data[pos + 4:pos + 8], "big")
        payload = data[pos + 10:pos + 10 + fsize]
        name = fid.decode("latin-1", "replace")
        if name.startswith("T") and name != "TXXX":
            frames[name] = _id3_text(payload)
        elif name == "TXXX":
            text = _id3_text(payload)
            desc, _, value = text.partition(" / ")
            frames[f"TXXX:{desc}"] = value
        pos += 10 + fsize
    return frames, data[end:]


def id3_title(frames: dict) -> Optional[str]:
    title, artist = frames.get("TIT2", ""), frames.get("TPE1", "")
    if title and artist:
        return f"{artist} - {title}"
    return title or artist or frames.get("TXXX:StreamTitle") or None


# ---- the client --------------------------------------------------------------
class HlsError(Exception):
    pass


def _get_text(url: str) -> tuple[str, str]:
    r = net.session().get(url, timeout=(8, 10))
    r.raise_for_status()
    return r.text, r.url


def open_media_playlist(url: str) -> tuple[str, Playlist]:
    """Resolve redirects and a master playlist to (media playlist URL, parsed)."""
    for _ in range(3):
        text, final = _get_text(url)
        pl = parse_playlist(text, final)
        if not pl.variants:
            if pl.encrypted:
                raise HlsError("encrypted HLS streams are not supported")
            if pl.fmp4:
                raise HlsError("fMP4 HLS streams are not supported")
            return final, pl
        # Audio-only radio: the highest bandwidth variant is still small.
        url = max(pl.variants)[1]
    raise HlsError("too many nested HLS playlists")


def segments(url: str, stop: threading.Event) -> Iterator[tuple[bytes, Optional[str]]]:
    """Yield (segment bytes without ID3, title or None) until ``stop`` is set."""
    media_url, pl = open_media_playlist(url)
    last_seq = None
    stale_polls = 0
    sent = 0
    clock = time.monotonic()      # for pacing a VOD (ENDLIST) playlist in real time
    while not stop.is_set():
        if pl.segments:
            newest = pl.segments[-1].seq
            if last_seq is None or newest < last_seq - LIVE_EDGE_SEGMENTS:
                # First look, or the media sequence restarted (encoder restart): go to the live edge.
                if last_seq is not None:
                    log.info("HLS media sequence restarted (%s -> %s)", last_seq, newest)
                start = 0 if pl.ended else max(0, len(pl.segments) - LIVE_EDGE_SEGMENTS)
                last_seq = pl.segments[start].seq - 1
        new = [s for s in pl.segments if last_seq is not None and s.seq > last_seq]
        for seg in new:
            if stop.is_set():
                return
            if pl.ended and sent >= LIVE_EDGE_SEGMENTS:
                # Not live: fetch no faster than it plays, keeping a few segments ahead.
                ahead = clock - time.monotonic()
                if ahead > LIVE_EDGE_SEGMENTS * pl.target and stop.wait(ahead - LIVE_EDGE_SEGMENTS * pl.target):
                    return
            body = _get_segment(seg.url, stop)
            last_seq = seg.seq
            if body is None:       # gone or failing: skip it rather than restart the session
                continue
            frames, body = split_id3(body)
            clock = max(clock, time.monotonic()) + (seg.duration or pl.target)
            sent += 1
            yield body, (id3_title(frames) or seg.title)
        if pl.ended and not new:
            raise EOFError("HLS playlist ended")
        stale_polls = 0 if new else stale_polls + 1
        if stale_polls > 6:
            raise HlsError("HLS playlist stopped updating")
        # Poll about every half target duration, as the HLS spec suggests.
        if stop.wait(max(1.0, pl.target / 2)):
            return
        text, final = _get_text(media_url)
        pl = parse_playlist(text, final)


def _get_segment(url: str, stop: threading.Event, tries: int = 3) -> Optional[bytes]:
    """Fetch one segment, retrying briefly. None if it can't be had."""
    for attempt in range(tries):
        try:
            r = net.session().get(url, timeout=(8, 20))
            if r.status_code in (404, 410):
                log.warning("HLS segment missing (%s): %s", r.status_code, url)
                return None
            r.raise_for_status()
            return r.content
        except requests.RequestException as exc:
            log.warning("HLS segment fetch failed (%s): %s", exc, url)
            if attempt == tries - 1 or stop.wait(1.0 + attempt):
                return None
    return None

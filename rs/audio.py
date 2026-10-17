"""Radio stream input: ICY metadata parsing and audio decoding.

One HTTP connection per station. The ICY reader strips the interleaved
metadata blocks out of the stream, publishes the track title, and feeds the
clean audio bytes to a small ffmpeg decoder that turns them into PCM. The
PCM lands in a jitter buffer that the encoder drains at a steady rate, padding
with silence when the station stalls or drops, so the video never freezes
because the audio did.
"""
from __future__ import annotations

import html
import logging
import re
import subprocess
import threading
import time
from typing import Callable, Optional
from urllib.parse import urljoin

import requests

from . import hls, net

SAMPLE_RATE = 48000
CHANNELS = 2
BYTES_PER_SECOND = SAMPLE_RATE * CHANNELS * 2  # s16le

NO_AUDIO_SECONDS = 15     # connected but nothing decoded this long: reconnect

log = logging.getLogger("rad_streams.audio")

_GENERIC = re.compile(
    r"^(unknown|untitled|n/?a|none|null|live|on air|advert(isement)?s?|commercial( break)?|"
    r"ad break|station id|jingle|stream|radio|music|no desc(ription)?|no title|-+|\.+|\d+)$", re.I)
_TITLE_RE = re.compile(r"StreamTitle='(.*?)';(?=[A-Za-z_][\w-]*=|\s*$|\x00)", re.S)


def decode_meta(raw: bytes) -> str:
    raw = raw.rstrip(b"\x00")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("latin-1")


def parse_stream_title(meta: str) -> Optional[str]:
    """Pull StreamTitle out of an ICY metadata block. None if absent."""
    m = _TITLE_RE.search(meta)
    if not m:
        m = re.search(r"StreamTitle='(.*)'", meta, re.S)
    return m.group(1).strip() if m else None


_ATTR = re.compile(r'([A-Za-z][\w-]*)\s*=\s*"((?:[^"\\]|\\.)*)"')
_BARE_ATTR = re.compile(r'\b(title|artist|song|text)\s*=\s*([^,;"]*)', re.I)


def unpack_title(t: str) -> str:
    """Turn key/value metadata into "Artist - Title".

    Some servers put attributes in the title, e.g. ``title="American Woman",artist="Guess Who"``
    or iHeart's ``Guess Who - text="American Woman" song_spot="M" ...``.
    """
    pairs = _ATTR.findall(t)
    if not pairs:
        if not re.match(r"\s*(title|artist|song|text)\s*=", t, re.I):
            return t
        pairs = [(k, v.strip()) for k, v in _BARE_ATTR.findall(t)]
        if not pairs:
            return t
    attrs = {k.lower(): v.strip() for k, v in pairs}
    song = attrs.get("title") or attrs.get("text") or attrs.get("song") or ""
    artist = attrs.get("artist", "")
    prefix = t[:t.find(pairs[0][0])].strip(" -,;") if t.find(pairs[0][0]) > 0 else ""
    if not artist and prefix:
        artist = prefix                      # iHeart: the artist comes before the attributes
    if song and artist:
        return f"{artist} - {song}"
    return song or artist or prefix


_EDGE_JUNK = r"[\s.\-–—~*|•·_:;,=+#>]"


# Codes missing their ";". "&apos"/"&quot" are never the start of a real word, so also "Sam&aposs".
_ENTITY_NO_SEMI = re.compile(r"&(?:(apos|quot)(?!;)|(amp|lt|gt|nbsp|#\d+|#x[0-9a-fA-F]+)(?![;\w]))")

_ENTITY = re.compile(r"&(?:[A-Za-z][A-Za-z0-9]{1,31}|#\d{1,7}|#[xX][0-9a-fA-F]{1,6});")


def decode_entities(t: str) -> str:
    """Turn HTML codes some stations send ("Don&apos;t", "&amp;apos;", "&apos") into characters."""
    for _ in range(3):        # "&amp;apos;" is encoded twice
        if "&" not in t:
            break
        new = _ENTITY_NO_SEMI.sub(lambda m: f"&{m.group(1) or m.group(2)};", t)
        # Only whole codes ("&...;"): html.unescape alone would also turn "&ampersand" into "&ersand".
        new = _ENTITY.sub(lambda m: html.unescape(m.group(0)), new)
        if new == t:
            break
        t = new
    return t.replace("\xa0", " ")


def tidy_title(t: str) -> str:
    """Trim stray punctuation some stations wrap titles in, e.g. ". - Junkhouse - Shine ~ "."""
    t = re.sub(r"\s+", " ", t)
    while re.search(r" [-–—] [-–—] ", f" {t} "):
        t = re.sub(r"(^| )[-–—] [-–—]( |$)", r"\1-\2", t)
    # empty "Artist -  - Title" segments, then junk at either end
    parts = [p for p in re.split(r"\s+[-–—]\s+", f" {t} ") if re.sub(_EDGE_JUNK, "", p)]
    t = " - ".join(p.strip() for p in parts)
    t = re.sub(rf"^{_EDGE_JUNK}+", "", t)
    t = re.sub(r"[\s\-–—~*|•·_:;,=+#>]+$", "", t)     # keep a trailing "." or "!" on a word
    return re.sub(r"\s+\.$", "", t).strip()


def clean_title(title: Optional[str], station_name: str) -> Optional[str]:
    """Return a displayable title, or None when it's blank or generic."""
    if not title:
        return None
    # Decode first: some stations send title=&quot;...&quot;, which unpack_title needs as real quotes.
    t = tidy_title(unpack_title(decode_entities(title)))
    if not t or _GENERIC.match(t):
        return None
    low = t.lower()
    if low == station_name.strip().lower() or low.startswith(("http://", "https://", "www.")):
        return None
    if len(t) > 160:
        t = t[:159].rstrip() + "…"
    return t


def split_artist_title(title: str) -> tuple[str, str]:
    """'Artist - Title' -> (artist, title); otherwise ('', title)."""
    for sep in (" - ", " – ", " — ", " / "):
        if sep in title:
            a, b = title.split(sep, 1)
            if a.strip() and b.strip():
                return a.strip(), b.strip()
    return "", title


_PLAYLIST_TYPES = ("audio/x-scpls", "audio/scpls", "audio/x-mpegurl", "audio/mpegurl",
                   "application/pls+xml")


def resolve_playlist(url: str, depth: int = 0) -> str:
    """Follow .pls/.m3u playlist URLs to the first actual stream URL."""
    return resolve_stream(url, depth)[0]


def resolve_stream(url: str, depth: int = 0) -> tuple[str, bool]:
    """Like resolve_playlist, also saying whether the result is an HLS playlist."""
    path = url.lower().split("?")[0]
    if hls.is_hls(url):
        return url, True
    if depth > 2 or not path.endswith((".pls", ".m3u")):
        return url, False
    r = net.session().get(url, timeout=net.TIMEOUT)
    r.raise_for_status()
    if hls.looks_like_hls(r.text):
        return r.url, True  # an HLS playlist saved as .m3u; the HLS client takes it from here
    for line in r.text.splitlines():
        line = line.strip()
        if line.lower().startswith("file") and "=" in line:
            line = line.split("=", 1)[1].strip()
        if line.lower().startswith(("http://", "https://")):
            return resolve_stream(urljoin(url, line), depth + 1)
    raise ValueError("playlist has no stream URL")


def _ffmpeg_format(content_type: str) -> Optional[str]:
    ct = (content_type or "").split(";")[0].strip().lower()
    return {
        "audio/mpeg": "mp3", "audio/mp3": "mp3",
        "audio/aac": "aac", "audio/aacp": "aac", "audio/x-aac": "aac",
        "audio/ogg": "ogg", "application/ogg": "ogg", "audio/opus": "ogg",
        "audio/flac": "flac", "audio/x-flac": "flac",
    }.get(ct)


class PcmBuffer:
    """Thread-safe PCM jitter buffer. ``read`` always returns ``n`` bytes."""

    def __init__(self, prime_seconds: float = 1.0, max_seconds: float = 6.0):
        self._buf = bytearray()
        self._lock = threading.Lock()
        self._prime = int(prime_seconds * BYTES_PER_SECOND) & ~3
        self._max = int(max_seconds * BYTES_PER_SECOND) & ~3
        self._primed = False
        self.underruns = 0

    def write(self, data: bytes) -> None:
        with self._lock:
            self._buf += data
            if len(self._buf) > self._max:  # drop oldest, keep frame alignment
                del self._buf[: (len(self._buf) - self._max) & ~3]

    def read(self, n: int) -> bytes:
        with self._lock:
            if not self._primed:
                if len(self._buf) < self._prime:
                    return bytes(n)
                self._primed = True
            if len(self._buf) >= n:
                out = bytes(self._buf[:n])
                del self._buf[:n]
                return out
            # Underrun: play what we have, pad with silence, and re-prime.
            out = bytes(self._buf) + bytes(n - len(self._buf))
            self._buf.clear()
            self._primed = False
            self.underruns += 1
            return out

    def configure(self, prime_seconds: float, max_seconds: float) -> None:
        """HLS delivers audio a whole segment at a time, so it needs more room."""
        with self._lock:
            self._prime = int(prime_seconds * BYTES_PER_SECOND) & ~3
            self._max = int(max_seconds * BYTES_PER_SECOND) & ~3

    def clear(self) -> None:
        with self._lock:
            self._buf.clear()
            self._primed = False

    @property
    def seconds(self) -> float:
        return len(self._buf) / BYTES_PER_SECOND


class RadioSource:
    """Reads one station: publishes ``title`` and fills ``pcm`` with audio."""

    def __init__(self, url: str, station_name: str, *, ffmpeg: str = "ffmpeg",
                 on_title: Optional[Callable[[Optional[str]], None]] = None):
        self.url = url
        self.station_name = station_name
        self.ffmpeg = ffmpeg
        self.on_title = on_title
        self.pcm = PcmBuffer()
        self.raw_title: Optional[str] = None     # last StreamTitle as sent
        self.title: Optional[str] = None         # cleaned, or None for fallback
        self.connected = False
        self.has_icy = False
        self.is_hls = False
        self.last_error: Optional[str] = None
        self.last_audio = 0.0
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._decoder: Optional[subprocess.Popen] = None
        self._decoder_started = 0.0
        self._probe_next = False

    # ---- lifecycle -----------------------------------------------------
    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name=f"rs-radio-{self.station_name}",
                                        daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._kill_decoder()
        if self._thread:
            self._thread.join(timeout=5)

    @property
    def is_live(self) -> bool:
        """Audio arrived within the last few seconds."""
        return self.connected and (time.time() - self.last_audio < 5 or self.pcm.seconds > 0.5)

    # ---- internals -----------------------------------------------------
    def _set_title(self, raw: Optional[str]) -> None:
        self.raw_title = raw
        cleaned = clean_title(raw, self.station_name)
        if cleaned != self.title:
            self.title = cleaned
            log.info("%s: now playing %r", self.station_name, cleaned or "(station name)")
            if self.on_title:
                self.on_title(cleaned)

    def _run(self) -> None:
        backoff = 2.0
        while not self._stop.is_set():
            started = time.time()
            try:
                self._session_once()
            except Exception as exc:
                if self._stop.is_set():
                    break  # shutting down; the decoder was closed on purpose
                self.last_error = f"{type(exc).__name__}: {exc}"
                log.warning("%s: stream error: %s", self.station_name, self.last_error)
            finally:
                self.connected = False
                self._kill_decoder()
            if self._stop.is_set():
                break
            if time.time() - started > 60:
                backoff = 2.0  # it ran fine for a while; retry quickly
            self._stop.wait(backoff)
            backoff = min(backoff * 2, 30.0)

    def _session_once(self) -> None:
        url, is_hls = resolve_stream(self.url)
        if is_hls:
            return self._hls_session(url)
        headers = {"Icy-MetaData": "1", "User-Agent": net.USER_AGENT, "Accept": "*/*"}
        try:
            resp = requests.get(url, headers=headers, stream=True, timeout=(8, 15))
        except requests.ConnectionError as exc:
            if "ICY" not in str(exc):
                raise
            # Old Shoutcast servers answer "ICY 200 OK", which http.client rejects.
            resp = _IcyResponse(url, headers, timeout=(8, 15))
        with resp:
            resp.raise_for_status()
            ctype = resp.headers.get("Content-Type", "")
            if "mpegurl" in ctype.lower():
                body = resp.raw.read(65536).decode("utf-8", "replace")
                if hls.looks_like_hls(body):
                    return self._hls_session(resp.url)
                raise ValueError(f"unexpected playlist ({ctype}); use the stream URL inside it")
            metaint = int(resp.headers.get("icy-metaint") or 0)
            self.has_icy = metaint > 0
            # If the last attempt decoded nothing, the Content-Type may be wrong: let ffmpeg probe.
            fmt = None if self._probe_next else _ffmpeg_format(ctype)
            self._start_decoder(fmt)
            self.connected = True
            self.last_error = None
            log.info("%s: connected (%s, metaint=%s)", self.station_name,
                     resp.headers.get("Content-Type", "?"), metaint or "none")
            raw = resp.raw
            if metaint:
                while not self._stop.is_set():
                    audio = _read_exact(raw, metaint)
                    self._feed(audio)
                    length = _read_exact(raw, 1)[0] * 16
                    if length:
                        title = parse_stream_title(decode_meta(_read_exact(raw, length)))
                        if title is not None:
                            self._set_title(title)
            else:
                while not self._stop.is_set():
                    chunk = raw.read(16384)
                    if not chunk:
                        raise EOFError("stream ended")
                    self._feed(chunk)

    def _hls_session(self, url: str) -> None:
        self.is_hls = True
        self.has_icy = False
        self.pcm.configure(prime_seconds=2.0, max_seconds=90.0)
        for body, title in hls.segments(url, self._stop):
            if self._decoder is None:
                self._start_decoder(_sniff_format(body))
                if not self.connected:
                    log.info("%s: connected (HLS)", self.station_name)
                self.connected = True
                self.last_error = None
            self._feed(body)
            if title is not None:
                self._set_title(title)

    def _feed(self, audio: bytes) -> None:
        dec = self._decoder
        if dec is None or dec.poll() is not None:
            raise RuntimeError("audio decoder exited")
        if time.time() - max(self.last_audio, self._decoder_started) > NO_AUDIO_SECONDS:
            self._probe_next = not self._probe_next
            raise RuntimeError(f"no audio decoded for {NO_AUDIO_SECONDS}s")
        try:
            dec.stdin.write(audio)
            dec.stdin.flush()
        except (BrokenPipeError, ValueError, OSError) as exc:
            raise RuntimeError("audio decoder pipe closed") from exc

    def _start_decoder(self, fmt: Optional[str]) -> None:
        cmd = [self.ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin",
               "-probesize", "32k", "-analyzeduration", "500000"]
        if fmt:
            cmd += ["-f", fmt]
        cmd += ["-i", "pipe:0", "-vn", "-f", "s16le", "-ac", str(CHANNELS),
                "-ar", str(SAMPLE_RATE), "pipe:1"]
        self._decoder = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                         stderr=subprocess.DEVNULL, bufsize=0)
        dec = self._decoder
        self._decoder_started = time.time()

        def pump():
            while True:
                data = dec.stdout.read(8192)
                if not data:
                    break
                self.pcm.write(data)
                self.last_audio = time.time()

        threading.Thread(target=pump, name=f"rs-pcm-{self.station_name}", daemon=True).start()

    def _kill_decoder(self) -> None:
        dec, self._decoder = self._decoder, None
        if dec is None:
            return
        try:
            dec.stdin.close()
        except Exception:
            pass
        try:
            dec.wait(timeout=2)
        except subprocess.TimeoutExpired:
            dec.kill()
            try:
                dec.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass


def _sniff_format(data: bytes) -> Optional[str]:
    """ffmpeg demuxer for an HLS segment: MPEG-TS, raw ADTS AAC, or let it probe."""
    if len(data) > 376 and data[0] == 0x47 and data[188] == 0x47:
        return "mpegts"
    if len(data) > 2 and data[0] == 0xFF and (data[1] & 0xF6) == 0xF0:
        return "aac"
    return None


def _read_exact(raw, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = raw.read(n - len(buf))
        if not chunk:
            raise EOFError("stream ended")
        buf += chunk
    return bytes(buf)


class _IcyResponse:
    """Minimal stand-in for a requests response, for servers that reply "ICY 200 OK"."""

    def __init__(self, url: str, headers: dict, timeout=(8, 15)):
        import socket
        from urllib.parse import urlsplit
        u = urlsplit(url)
        if u.scheme != "http":
            raise ValueError("ICY status line over HTTPS is not supported")
        port = u.port or 80
        self._sock = socket.create_connection((u.hostname, port), timeout=timeout[0])
        self._sock.settimeout(timeout[1])
        path = (u.path or "/") + (f"?{u.query}" if u.query else "")
        lines = [f"GET {path} HTTP/1.0", f"Host: {u.hostname}"] + [f"{k}: {v}" for k, v in headers.items()]
        self._sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode("latin-1"))
        self.raw = self._sock.makefile("rb")
        status = self.raw.readline(1024).decode("latin-1").strip()
        parts = status.split()
        if len(parts) < 2 or not parts[1].isdigit():
            self.close()
            raise ConnectionError(f"bad status line {status!r}")
        self.status_code = int(parts[1])
        self.headers = requests.structures.CaseInsensitiveDict()
        for _ in range(100):
            line = self.raw.readline(8192).decode("latin-1").strip()
            if not line:
                break
            k, _, v = line.partition(":")
            self.headers[k.strip()] = v.strip()
        self.url = url

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code} from {self.url}")

    @property
    def text(self) -> str:
        return self.raw.read(65536).decode("utf-8", "replace")

    def close(self) -> None:
        for c in (self.raw if hasattr(self, "raw") else None, self._sock):
            try:
                if c is not None:
                    c.close()
            except OSError:
                pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

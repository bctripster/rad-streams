"""Local stand-ins for the network: an ICY radio server, synthetic photos,
and canned geocoding/weather. Used by the tests and the offline demo."""
from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import numpy as np
from PIL import Image, ImageFilter

from rs import geo, weather
from rs.net import write_json

HERE = os.path.dirname(os.path.abspath(__file__))


def make_tone_mp3(path: str, seconds: int = 20) -> str:
    if not os.path.exists(path):
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
                        "-i", f"sine=frequency=440:duration={seconds}", "-ac", "2", "-ar", "44100",
                        "-c:a", "libmp3lame", "-b:a", "128k", path], check=True)
    return path


def icy_block(title: str) -> bytes:
    meta = f"StreamTitle='{title}';".encode("utf-8")
    n = (len(meta) + 15) // 16
    return bytes([n]) + meta.ljust(n * 16, b"\x00")


class FakeIcyServer:
    """Serves an mp3 in a loop, Shoutcast-style, paced at its bitrate.

    ``titles`` rotate every ``title_every`` seconds. ``drop_after`` closes
    each connection after that many seconds to exercise reconnects.
    """

    def __init__(self, mp3: str, titles=("Artist One - First Song",), *, metaint: int = 8192,
                 bitrate_bytes: int = 16000, title_every: float = 3.0, drop_after: float = 0,
                 send_icy: bool = True, content_type: str = "audio/mpeg"):
        self.data = open(mp3, "rb").read()
        self.titles = list(titles)
        self.metaint, self.rate = metaint, bitrate_bytes
        self.title_every, self.drop_after = title_every, drop_after
        self.send_icy, self.content_type = send_icy, content_type
        self.connections = 0
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def log_message(self, *a):
                pass

            def do_GET(self):
                outer.connections += 1
                want_meta = self.headers.get("Icy-MetaData") == "1" and outer.send_icy
                self.send_response(200)
                self.send_header("Content-Type", outer.content_type)
                self.send_header("icy-name", "Fake Radio")
                if want_meta:
                    self.send_header("icy-metaint", str(outer.metaint))
                self.end_headers()
                start = time.time()
                pos = sent = 0
                last_title = None
                try:
                    while True:
                        elapsed = time.time() - start
                        if outer.drop_after and elapsed > outer.drop_after:
                            return
                        # pace: stay at most half a second ahead of real time
                        if sent > (elapsed + 0.5) * outer.rate:
                            time.sleep(0.02)
                            continue
                        n = outer.metaint if want_meta else 4096
                        chunk = bytearray()
                        while len(chunk) < n:
                            take = outer.data[pos:pos + n - len(chunk)]
                            chunk += take
                            pos = (pos + len(take)) % len(outer.data)
                        self.wfile.write(chunk)
                        sent += len(chunk)
                        if want_meta:
                            title = outer.titles[int(elapsed / outer.title_every) % len(outer.titles)]
                            if title != last_title:
                                self.wfile.write(icy_block(title))
                                last_title = title
                            else:
                                self.wfile.write(b"\x00")
                except (BrokenPipeError, ConnectionResetError):
                    return

        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        self.port = sock.getsockname()[1]
        sock.close()
        self.httpd = ThreadingHTTPServer(("127.0.0.1", self.port), Handler)
        self.httpd.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.port}/stream"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def make_scenic(path: str, seed: int, size=(1920, 1080)) -> str:
    """A synthetic landscape: sky gradient, sun, layered ridgelines."""
    w, h = size
    rng = np.random.default_rng(seed)
    hue = rng.uniform(0, 1)
    top = np.array([40, 80, 160]) * (0.6 + 0.4 * hue)
    bottom = np.array([250, 180, 120]) * (0.7 + 0.3 * (1 - hue))
    t = np.linspace(0, 1, h)[:, None, None]
    img = (top * (1 - t) + bottom * t).repeat(w, axis=1)
    yy, xx = np.mgrid[0:h, 0:w]
    sx, sy, r = rng.uniform(0.2, 0.8) * w, rng.uniform(0.2, 0.45) * h, h * 0.06
    sun = np.clip(1 - np.hypot(xx - sx, yy - sy) / r, 0, 1)[..., None]
    img = img * (1 - sun) + np.array([255, 240, 210]) * sun
    x = np.arange(w)
    for layer in range(4):
        base = h * (0.55 + layer * 0.1)
        ridge = base + sum(rng.uniform(10, 60) * np.sin(x / rng.uniform(80, 400) + rng.uniform(0, 6))
                           for _ in range(4))
        mask = (yy > ridge[None, :])[..., None]
        shade = np.array([30, 45, 60]) * (1.0 - layer * 0.2) + 20 * layer
        img = np.where(mask, shade, img)
    Image.fromarray(np.clip(img, 0, 255).astype("uint8")).filter(
        ImageFilter.GaussianBlur(1.2)).save(path, quality=88)
    return path


def seed_image_cache(cache_dir: str, city: str, count: int = 4, size=(1920, 1080)) -> str:
    """Write a ready image cache for ``city`` the way ImageLibrary would."""
    d = os.path.join(cache_dir, "images", geo.slug(city))
    os.makedirs(d, exist_ok=True)
    entries = []
    for i in range(count):
        name = f"scene_{i}.jpg"
        make_scenic(os.path.join(d, name), seed=i + 7, size=size)
        entries.append({"file": name, "title": f"Scene {i}.jpg", "artist": f"Test Photographer {i}",
                        "license": "CC BY-SA 4.0", "page": "", "tier": "test"})
    write_json(os.path.join(d, "index.json"), {"fetched_at": time.time(), "city": city,
                                               "tiers": ["test"], "images": entries})
    return d


def load_fixture(name: str):
    with open(os.path.join(HERE, "fixtures", name), encoding="utf-8") as f:
        return json.load(f)


def fake_geocoder(city, cache_dir=None):
    return geo.Place(name="Burlington", lat=44.4759, lon=-73.2121, country="United States",
                     country_code="US", admin1="Vermont", timezone="America/New_York",
                     population=44743)


def fake_weather(lat, lon, units="F"):
    return weather.parse(load_fixture("forecast.json"), units)


def id3_tag(title: str, artist: str = "") -> bytes:
    """A minimal ID3v2.4 tag with TIT2/TPE1, like packed-audio HLS segments carry."""
    def frame(fid, text):
        body = b"\x03" + text.encode("utf-8")
        n = len(body)
        size = bytes([(n >> 21) & 0x7F, (n >> 14) & 0x7F, (n >> 7) & 0x7F, n & 0x7F])
        return fid + size + b"\x00\x00" + body
    frames = frame(b"TIT2", title) + (frame(b"TPE1", artist) if artist else b"")
    n = len(frames)
    size = bytes([(n >> 21) & 0x7F, (n >> 14) & 0x7F, (n >> 7) & 0x7F, n & 0x7F])
    return b"ID3\x04\x00\x00" + size + frames


class FakeHlsServer:
    """Live HLS radio: a master playlist, a sliding media playlist, 2 s segments.

    kind="ts": MPEG-TS segments, titles in #EXTINF attributes (Triton style).
    kind="aac": packed ADTS segments, titles in each segment's ID3 tag.
    """

    def __init__(self, workdir: str, titles=("Artist - Song",), kind: str = "ts",
                 seg_seconds: int = 2, total: int = 30):
        import glob
        self.kind, self.seg_seconds, self.titles = kind, seg_seconds, list(titles)
        d = os.path.join(workdir, f"hls-{kind}")
        os.makedirs(d, exist_ok=True)
        if not glob.glob(os.path.join(d, "seg*")):
            src = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
                   "-i", f"sine=frequency=330:duration={total}", "-ac", "2", "-ar", "44100",
                   "-c:a", "aac", "-b:a", "96k"]
            if kind == "ts":
                cmd = src + ["-f", "hls", "-hls_time", str(seg_seconds), "-hls_list_size", "0",
                             "-hls_segment_filename", os.path.join(d, "seg%03d.ts"),
                             os.path.join(d, "index.m3u8")]
            else:
                cmd = src + ["-f", "segment", "-segment_time", str(seg_seconds),
                             "-segment_format", "adts", os.path.join(d, "seg%03d.aac")]
            subprocess.run(cmd, check=True)
        self.files = sorted(f for f in glob.glob(os.path.join(d, "seg*")) if not f.endswith(".m3u8"))
        self.start = time.time()
        self.requests = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                outer.requests.append(self.path)
                if self.path.startswith("/redirect"):
                    self.send_response(302)
                    self.send_header("Location", "/master.m3u8")
                    self.end_headers()
                    return
                if self.path == "/master.m3u8":
                    body = ("#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=64000\nlow/live.m3u8\n"
                            "#EXT-X-STREAM-INF:BANDWIDTH=128000\nhigh/live.m3u8\n").encode()
                    return self._send(body, "application/vnd.apple.mpegurl")
                if self.path.endswith("live.m3u8"):
                    return self._send(outer.playlist().encode(), "application/vnd.apple.mpegurl")
                name = self.path.rsplit("/", 1)[-1]
                idx = int(re.sub(r"\D", "", name) or 0) % len(outer.files)
                data = open(outer.files[idx], "rb").read()
                if outer.kind == "aac":
                    artist, _, title = outer.title_for(int(re.sub(r"\D", "", name))).partition(" - ")
                    data = id3_tag(title or artist, artist if title else "") + data
                self._send(data, "video/mp2t" if outer.kind == "ts" else "audio/aac")

            def _send(self, body, ctype):
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        self.port = sock.getsockname()[1]
        sock.close()
        self.httpd = ThreadingHTTPServer(("127.0.0.1", self.port), Handler)
        self.httpd.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.port}/master.m3u8"
        self.redirect_url = f"http://127.0.0.1:{self.port}/redirect/STATION.m3u8"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def title_for(self, seq: int) -> str:
        return self.titles[(seq // 3) % len(self.titles)]

    def playlist(self) -> str:
        newest = 5 + int((time.time() - self.start) / self.seg_seconds)
        first = max(0, newest - 5)
        ext = "ts" if self.kind == "ts" else "aac"
        lines = ["#EXTM3U", "#EXT-X-VERSION:3", f"#EXT-X-TARGETDURATION:{self.seg_seconds}",
                 f"#EXT-X-MEDIA-SEQUENCE:{first}"]
        for seq in range(first, newest + 1):
            if self.kind == "ts":
                artist, _, title = self.title_for(seq).partition(" - ")
                lines.append(f'#EXTINF:{self.seg_seconds}.0,title="{title}",artist="{artist}"')
            else:
                lines.append(f"#EXTINF:{self.seg_seconds}.0,")
            lines.append(f"seg{seq}.{ext}")
        return "\n".join(lines) + "\n"

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()

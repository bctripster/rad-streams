"""On-demand server: start on first viewer, share one renderer, stop after the last."""
import json
import os
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request

import pytest

from rs.config import RenderSettings, Station
from rs.server import RadioServer
from rs.station import StationRunner

from fakes import FakeIcyServer, fake_geocoder, fake_weather, make_tone_mp3, seed_image_cache


@pytest.fixture
def env(tmp_path):
    mp3 = make_tone_mp3(str(tmp_path / "tone.mp3"))
    icy = FakeIcyServer(mp3, ["Artist - Song"])
    settings = RenderSettings(width=640, height=360, data_dir=str(tmp_path / "data"),
                              video_kbps=800, image_seconds=5)
    seed_image_cache(settings.cache_dir, "Burlington, Vermont", count=2, size=(640, 360))
    started = []

    def factory(station, s):
        started.append(station.station_id)
        return StationRunner(station, s, "-", geocoder=fake_geocoder, weather_fetch=fake_weather)

    stations = [Station("Test FM", icy.url, "Burlington, VT"), Station("Other FM", icy.url, "Burlington, VT")]
    # Padding off here so these tests time the real stream, not the null prelude
    # (that has its own test below).
    srv = RadioServer(stations, settings, port=0, linger_seconds=1.5, runner_factory=factory,
                      startup_padding_bytes=0, startup_boost_seconds=0)
    srv.start()
    yield srv, stations, started, tmp_path
    srv.stop()
    icy.close()


class Viewer:
    """Reads a station route in a thread, recording when the first bytes arrived."""

    def __init__(self, url):
        self.data = bytearray()
        self.t0 = time.monotonic()
        self.first_byte = None
        self.status = None
        self.closed_by_server = False
        self._stop = threading.Event()
        self.thread = threading.Thread(target=self._run, args=(url,), daemon=True)
        self.thread.start()

    def _run(self, url):
        resp = urllib.request.urlopen(url, timeout=10)
        self.status = resp.status
        self.headers_at = time.monotonic() - self.t0
        while not self._stop.is_set():
            # read1: whatever has arrived (read(n) would wait for a full n bytes,
            # hiding the timing of the null-packet prelude)
            chunk = resp.read1(65536)
            if not chunk:
                self.closed_by_server = True
                break
            if self.first_byte is None:
                self.first_byte = time.monotonic() - self.t0
            self.data += chunk
        resp.close()

    def close(self):
        self._stop.set()
        self.thread.join(timeout=5)


def wait_for(cond, timeout=10.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.1)
    return False


def test_shared_renderer_fast_start_and_linger(env):
    srv, stations, started, tmp = env
    url = srv.url_for(stations[0])
    a = Viewer(url)
    assert wait_for(lambda: a.first_byte is not None, 5), "no data within 5 s"
    assert a.status == 200 and a.headers_at < 1.0
    assert a.first_byte < 3.0, f"first byte after {a.first_byte:.2f}s"
    b = Viewer(url)
    assert wait_for(lambda: len(b.data) > 50_000, 8)
    assert started == [stations[0].station_id]          # one renderer for both viewers
    st = {s["id"]: s for s in srv.status()["stations"]}[stations[0].station_id]
    assert st["running"] and st["viewers"] == 2

    time.sleep(3)
    a.close()
    b.close()
    out = tmp / "capture.ts"
    out.write_bytes(bytes(a.data))
    probe = json.loads(subprocess.run(
        ["ffprobe", "-v", "error", "-show_streams", "-of", "json", str(out)],
        capture_output=True, text=True).stdout)
    assert {s["codec_type"] for s in probe["streams"]} == {"video", "audio"}

    b_ = srv.broadcasts[stations[0].station_id]
    assert wait_for(lambda: b_.runner is None, 6), "renderer did not stop after viewers left"
    # coming back starts a fresh renderer
    c = Viewer(url)
    assert wait_for(lambda: c.first_byte is not None, 5)
    c.close()
    assert started == [stations[0].station_id] * 2


def test_quick_return_within_linger_reuses_renderer(env):
    srv, stations, started, _ = env
    url = srv.url_for(stations[1])
    a = Viewer(url)
    assert wait_for(lambda: a.first_byte is not None, 5)
    a.close()
    time.sleep(0.5)
    b = Viewer(url)
    assert wait_for(lambda: b.first_byte is not None, 5)
    b.close()
    assert started == [stations[1].station_id]


def test_unknown_station_and_status(env):
    srv, stations, _, _ = env
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(f"http://127.0.0.1:{srv.port}/radio/nope.ts", timeout=5)
    assert e.value.code == 404
    body = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{srv.port}/status", timeout=5).read())
    assert {s["name"] for s in body["stations"]} == {"Test FM", "Other FM"}


def test_removing_station_disconnects_viewer(env):
    srv, stations, _, _ = env
    a = Viewer(srv.url_for(stations[0]))
    assert wait_for(lambda: a.first_byte is not None, 5)
    srv.update([stations[1]])
    assert wait_for(lambda: a.closed_by_server, 8)
    assert stations[0].station_id not in srv.broadcasts
    a.close()


def test_startup_padding_fills_proxy_buffer_at_once(tmp_path):
    """Each viewer first gets ~1 MB of null TS packets (Dispatcharr waits for
    ~1 MB before releasing a stream), then the live stream with nothing lost."""
    from rs.server import NULL_TS_PACKET, STARTUP_PADDING_BYTES, TS_PACKET

    mp3 = make_tone_mp3(str(tmp_path / "tone.mp3"))
    icy = FakeIcyServer(mp3, ["Artist - Song"])
    settings = RenderSettings(width=640, height=360, data_dir=str(tmp_path / "data"),
                              video_kbps=800, image_seconds=5)
    seed_image_cache(settings.cache_dir, "Burlington, Vermont", count=2, size=(640, 360))
    st = Station("Pad FM", icy.url, "Burlington, VT")
    srv = RadioServer([st], settings, port=0, linger_seconds=1.0,
                      runner_factory=lambda s, rs: StationRunner(
                          s, rs, "-", geocoder=fake_geocoder, weather_fetch=fake_weather))
    srv.start()
    try:
        assert srv.startup_padding_bytes == STARTUP_PADDING_BYTES
        assert STARTUP_PADDING_BYTES % TS_PACKET == 0
        assert STARTUP_PADDING_BYTES >= 4 * 188 * 1361          # Dispatcharr: 4 x ~256 KB
        v = Viewer(srv.url_for(st))
        # the whole prelude arrives straight away, well before the renderer could fill it
        assert wait_for(lambda: len(v.data) >= STARTUP_PADDING_BYTES, 2), \
            f"only {len(v.data)} bytes after 2 s"
        # boost: for the first seconds the stream is topped up to ~512 KB/s
        time.sleep(3.0)
        boosted = len(v.data) - STARTUP_PADDING_BYTES
        assert boosted >= 0.7 * 512 * 1024 * 3.0, f"only {boosted} bytes in the first 3 s"
        assert wait_for(lambda: len(v.data) > STARTUP_PADDING_BYTES + 2_000_000, 10)
        v.close()
        data = bytes(v.data)
        pad, live = data[:STARTUP_PADDING_BYTES], data[STARTUP_PADDING_BYTES:]
        assert pad == NULL_TS_PACKET * (STARTUP_PADDING_BYTES // TS_PACKET)
        # boost padding may come first while the renderer starts; skip it
        real = [live[i:i + TS_PACKET] for i in range(0, len(live) - TS_PACKET + 1, TS_PACKET)
                if live[i:i + TS_PACKET] != NULL_TS_PACKET]
        assert real and real[0][:1] == b"\x47", "no real packets after the padding"
        # first viewer: the renderer starts after the prelude, so live data begins with its PAT
        pids = {((p[1] & 0x1F) << 8) | p[2] for p in real[:50]}
        assert 0 in pids, "PAT missing from the start of the live stream"
        # boost padding is interleaved, not one block replacing real data
        assert len(real) * TS_PACKET > 200_000
        # nothing was spliced mid-packet: every packet starts with the sync byte
        whole = len(data) - len(data) % TS_PACKET
        assert all(data[i] == 0x47 for i in range(0, whole, TS_PACKET))
        out = tmp_path / "padded.ts"
        out.write_bytes(data)
        probe = json.loads(subprocess.run(
            ["ffprobe", "-v", "error", "-show_streams", "-of", "json", str(out)],
            capture_output=True, text=True).stdout)
        assert {s["codec_type"] for s in probe["streams"]} == {"video", "audio"}
    finally:
        srv.stop()
        icy.close()


def test_startup_padding_can_be_disabled_and_stays_packet_aligned():
    srv = RadioServer([], RenderSettings(data_dir="/tmp/rs-unused"), port=0,
                      startup_padding_bytes=1000)
    assert srv.startup_padding_bytes == 940                       # 5 whole packets
    assert RadioServer([], RenderSettings(data_dir="/tmp/rs-unused"), port=0,
                       startup_padding_bytes=0).startup_padding_bytes == 0


def test_pump_pushes_whole_packets_only():
    """Odd-sized reads from ffmpeg are regrouped into whole TS packets, so a
    viewer can insert null packets between chunks without corrupting the stream."""
    import io
    from rs.server import Broadcast, TS_PACKET, _Client

    payload = bytes((i * 7) % 251 for i in range(TS_PACKET * 40 + 100))
    pieces = [payload[:1000], payload[1000:1001], payload[1001:5000], payload[5000:]]

    class Out(io.RawIOBase):
        def __init__(self):
            self.parts = list(pieces)
        def read(self, n=-1):
            return self.parts.pop(0) if self.parts else b""

    class Runner:
        stdout = Out()
        error = None
        stopping = True
        def stop(self):
            pass

    b = Broadcast(Station("X", "http://x", "Y"), RenderSettings(data_dir="/tmp/rs-unused"),
                  lambda s, r: Runner(), 1.0)
    cl = _Client("t")
    b.clients.append(cl)
    r = Runner()
    b.runner, b._generation = r, 1
    b._pump(r, 1)
    got = []
    while True:
        x = cl.q.get_nowait()
        if x is None:
            break
        got.append(x)
    assert got and all(len(x) % TS_PACKET == 0 for x in got)
    assert b"".join(got) == payload[:TS_PACKET * 40]       # the trailing partial packet is held back

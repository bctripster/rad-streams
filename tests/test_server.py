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
    srv = RadioServer(stations, settings, port=0, linger_seconds=1.5, runner_factory=factory)
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
            chunk = resp.read(4096)
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

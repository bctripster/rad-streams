"""End-to-end: fake ICY server -> StationRunner -> MPEG-TS file -> ffprobe."""
import json
import subprocess
import time

import pytest

from rs.config import RenderSettings, Station
from rs.station import StationRunner

from fakes import FakeIcyServer, fake_geocoder, fake_weather, make_tone_mp3, seed_image_cache


def probe(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", path],
                         capture_output=True, text=True, check=True).stdout
    return json.loads(out)


def run_station(tmp_path, server, seconds, **kw):
    settings = RenderSettings(width=1280, height=720, data_dir=str(tmp_path / "data"),
                              image_seconds=4, crossfade_seconds=1.0, **kw)
    seed_image_cache(settings.cache_dir, "Burlington, Vermont", count=3, size=(1280, 720))
    out = str(tmp_path / "out.ts")
    runner = StationRunner(Station("Test FM", server.url, "Burlington, VT"), settings, out,
                           geocoder=fake_geocoder, weather_fetch=fake_weather,
                           log_path=str(tmp_path / "ffmpeg.log"))
    titles = []
    runner.radio.on_title = titles.append
    runner.run_for(seconds)
    return runner, out, titles


def test_end_to_end(tmp_path):
    mp3 = make_tone_mp3(str(tmp_path / "tone.mp3"))
    server = FakeIcyServer(mp3, ["Artist One - First Song", "Unknown", "Artist Two - Second Song"],
                           title_every=3)
    try:
        runner, out, titles = run_station(tmp_path, server, 12)
    finally:
        server.close()
    assert runner.error is None, runner.error
    info = probe(out)
    kinds = {s["codec_type"]: s for s in info["streams"]}
    assert kinds["video"]["codec_name"] == "h264" and kinds["video"]["width"] == 1280
    assert kinds["audio"]["codec_name"] == "aac"
    assert 10 <= float(info["format"]["duration"]) <= 13.5
    # "Unknown" is generic, so the title falls back (None) between the real ones
    assert "Artist One - First Song" in titles and None in titles
    assert runner.frames_rendered > 30
    assert runner.place.name == "Burlington" and runner.weather is not None
    assert len(runner._photos) == 3 and not runner.comp.on_splash   # cached photos were used


def test_stream_drop_reconnects_and_keeps_video(tmp_path):
    mp3 = make_tone_mp3(str(tmp_path / "tone.mp3"))
    server = FakeIcyServer(mp3, ["A - B"], drop_after=3)
    try:
        runner, out, _ = run_station(tmp_path, server, 12)
    finally:
        server.close()
    assert runner.error is None
    assert server.connections >= 2
    assert 10 <= float(probe(out)["format"]["duration"]) <= 13.5


def test_dead_stream_still_renders(tmp_path):
    class Dead:
        url = "http://127.0.0.1:9/nothing"
        def close(self): pass
    runner, out, _ = run_station(tmp_path, Dead(), 8)
    assert runner.error is None
    assert runner.radio.last_error
    info = probe(out)
    assert {s["codec_type"] for s in info["streams"]} == {"video", "audio"}
    assert float(info["format"]["duration"]) >= 6.5

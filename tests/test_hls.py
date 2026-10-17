"""HLS (.m3u8) radio: playlist parsing, titles, and live playback via the fake server."""
import json
import subprocess
import time

from rs import audio, hls, validate
from rs.config import RenderSettings, Station
from rs.station import StationRunner

from fakes import FakeHlsServer, fake_geocoder, fake_weather, id3_tag, seed_image_cache


def test_extinf_titles():
    assert hls.extinf_title('10.0,title="Hey Jude",artist="The Beatles"') == "The Beatles - Hey Jude"
    assert hls.extinf_title('10.0,title="Station ID"') == "Station ID"
    assert hls.extinf_title("10.0,Band - Song") == "Band - Song"
    assert hls.extinf_title("10.0,") is None


def test_parse_master_and_media():
    master = hls.parse_playlist("#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=64000\na.m3u8\n"
                                "#EXT-X-STREAM-INF:BANDWIDTH=128000\nb/c.m3u8\n", "http://h/x/master.m3u8")
    assert master.variants == [(64000, "http://h/x/a.m3u8"), (128000, "http://h/x/b/c.m3u8")]
    media = hls.parse_playlist("#EXTM3U\n#EXT-X-TARGETDURATION:10\n#EXT-X-MEDIA-SEQUENCE:41\n"
                               '#EXTINF:10,title="T",artist="A"\ns41.aac\n#EXTINF:9.5,\ns42.aac\n',
                               "http://h/live/m.m3u8")
    assert [(s.seq, s.url, s.title) for s in media.segments] == \
        [(41, "http://h/live/s41.aac", "A - T"), (42, "http://h/live/s42.aac", None)]
    assert media.target == 10 and not media.ended
    assert hls.parse_playlist("#EXTM3U\n#EXT-X-KEY:METHOD=AES-128,URI=\"k\"\n", "http://h/").encrypted


def test_id3_split():
    frames, rest = hls.split_id3(id3_tag("Jóga", "Björk") + b"\xff\xf1rest")
    assert hls.id3_title(frames) == "Björk - Jóga" and rest == b"\xff\xf1rest"
    assert hls.split_id3(b"\xff\xf1abc") == ({}, b"\xff\xf1abc")


def test_detection():
    assert hls.is_hls("https://playerservices.streamtheworld.com/api/livestream-redirect/CJAYFM_ADP.m3u8")
    assert hls.is_hls("http://x/live", "application/vnd.apple.mpegurl")
    assert not hls.is_hls("http://x/stream.m3u", "audio/x-mpegurl")
    assert hls.looks_like_hls("#EXTM3U\n#EXT-X-TARGETDURATION:6\n")
    assert not hls.looks_like_hls("#EXTM3U\n#EXTINF:-1,Radio\nhttp://x/stream\n")


def _listen(url, seconds):
    src = audio.RadioSource(url, "Test FM")
    titles = []
    src.on_title = titles.append
    src.start()
    time.sleep(seconds)
    live, buffered, err = src.is_live, src.pcm.seconds, src.last_error
    src.stop()
    return src, titles, live, buffered, err


def test_hls_ts_with_extinf_titles(tmp_path):
    srv = FakeHlsServer(str(tmp_path), ["Artist One - First", "Artist Two - Second"], kind="ts")
    try:
        src, titles, live, buffered, err = _listen(srv.redirect_url, 8)
    finally:
        srv.close()
    assert err is None and live and src.is_hls
    assert buffered > 2.0                       # whole segments arrive at once
    assert "Artist One - First" in titles
    assert any("high/live.m3u8" in p for p in srv.requests)   # picked the best variant


def test_hls_packed_aac_with_id3_titles(tmp_path):
    srv = FakeHlsServer(str(tmp_path), ["Björk - Jóga"], kind="aac")
    try:
        src, titles, live, buffered, err = _listen(srv.url, 7)
    finally:
        srv.close()
    assert err is None and live and buffered > 2.0
    assert titles and titles[0] == "Björk - Jóga"


def test_validate_hls(tmp_path):
    srv = FakeHlsServer(str(tmp_path), ["Artist - Song"], kind="ts")
    try:
        out = validate.check_stream(srv.redirect_url, "Test FM", seconds=5)
    finally:
        srv.close()
    assert out["reachable"] and out["content_type"] == "HLS" and out["title"] == "Artist - Song"


def test_hls_station_renders_end_to_end(tmp_path):
    srv = FakeHlsServer(str(tmp_path), ["Artist - Song"], kind="ts")
    settings = RenderSettings(width=640, height=360, data_dir=str(tmp_path / "data"), video_kbps=800)
    seed_image_cache(settings.cache_dir, "Burlington, Vermont", count=2, size=(640, 360))
    out = str(tmp_path / "out.ts")
    runner = StationRunner(Station("CJAY", srv.url, "Vancouver, BC"), settings, out,
                           geocoder=fake_geocoder, weather_fetch=fake_weather)
    try:
        runner.run_for(10)
    finally:
        srv.close()
    assert runner.error is None and runner.radio.title == "Artist - Song"
    # the audio track holds the tone, not padded silence
    vol = subprocess.run(["ffmpeg", "-hide_banner", "-ss", "4", "-i", out, "-map", "0:a",
                          "-af", "volumedetect", "-f", "null", "-"], capture_output=True, text=True).stderr
    mean = float(vol.split("mean_volume:")[1].split("dB")[0])
    assert mean > -40, vol[-400:]

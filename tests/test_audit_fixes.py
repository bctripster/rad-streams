"""Tests for the fixes from the v0.5.1 audit (released as v0.5.2).

Each test is named after the audit finding it covers (H = high, M = medium,
L = low, O = optimisation).
"""
import json
import os
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from PIL import Image

import plugin as plugin_mod
from rs import audio, encoder, geo, hls, images, net
from rs.channels import GROUP_NAME, ChannelSync
from rs.config import MAX_CITY_LENGTH, MAX_NAME_LENGTH, RenderSettings, Station, parse_stations
from rs.render import Compositor, PanelInfo
from rs.server import RadioServer
from rs.station import StationRunner

from fake_orm import make_models
from fakes import make_scenic

URL = lambda st: f"http://127.0.0.1:5980/radio/{st.station_id}.ts"
TWO = "A FM | http://a/s | Austin, TX\nB FM | http://b/s | Boston, MA"


def stations(text):
    return parse_stations(text).stations


def other_peoples_channels(m):
    """A database holding two channels that have nothing to do with Rad Streams."""
    group = m.ChannelGroup.objects.create(name="Sports")
    for name in ("ESPN", "CNN"):
        s = m.Stream.objects.create(name=f"{name} HD", url=f"http://provider/{name}.ts")
        c = m.Channel.objects.create(name=name, channel_number=10 + s.id, channel_group=group)
        m.ChannelStream.objects.create(channel=c, stream=s, order=0)


# ---- H1: saved ids are only trusted when the row is still ours ---------------
def test_h1_sync_leaves_other_channels_alone_after_a_database_reset(tmp_path):
    path = str(tmp_path / "channels.json")
    ChannelSync(make_models(), path).sync(stations(TWO), URL)     # records ids 1 and 2

    fresh = make_models()                  # the database was reset; channels.json survived
    other_peoples_channels(fresh)          # ...and ids 1 and 2 are someone else's now
    res = ChannelSync(fresh, path).sync(stations(TWO), URL)

    names = sorted(c.name for c in fresh.Channel.objects.all())
    assert names == ["A FM", "B FM", "CNN", "ESPN"]            # ours were created, theirs kept
    assert sorted(res.created) == ["A FM", "B FM"]
    theirs = {c.name: c for c in fresh.Channel.objects.all() if c.name in ("ESPN", "CNN")}
    assert all(c.channel_group.name == "Sports" for c in theirs.values())
    assert sorted(s.url for s in fresh.Stream.objects.all() if "provider" in s.url) == [
        "http://provider/CNN.ts", "http://provider/ESPN.ts"]
    # The new mapping points at the new rows, so the next Sync changes nothing.
    assert sorted(ChannelSync(fresh, path).sync(stations(TWO), URL).unchanged) == ["A FM", "B FM"]


def test_h1_remove_all_and_removals_skip_rows_that_are_not_ours(tmp_path):
    path = tmp_path / "channels.json"
    stale = {"a-fm-687648": {"channel_id": 1, "stream_id": 1, "name": "A FM"},
             "gone-000000": {"channel_id": 2, "stream_id": 2, "name": "Gone FM"}}

    m = make_models()
    other_peoples_channels(m)
    path.write_text(json.dumps(stale))
    assert ChannelSync(m, str(path)).remove_all() == 0
    assert m.Channel.objects.all().count() == 2 and m.Stream.objects.all().count() == 2

    # A Sync that drops "Gone FM" must not delete channel 2 / stream 2 either.
    m = make_models()
    other_peoples_channels(m)
    path.write_text(json.dumps(stale))
    ChannelSync(m, str(path)).sync(stations("A FM | http://a/s | Austin, TX"), URL)
    assert sorted(c.name for c in m.Channel.objects.all()) == ["A FM", "CNN", "ESPN"]


def test_h1_our_own_rows_are_still_updated_and_removed(tmp_path):
    m = make_models()
    path = str(tmp_path / "channels.json")
    sync = ChannelSync(m, path)
    sync.sync(stations(TWO), URL)
    # A channel moved to another group by hand is still ours (it's linked to our stream).
    moved = m.Channel.objects.get(name="A FM")
    moved.channel_group = m.ChannelGroup.objects.create(name="Favourites")
    moved.save()
    res = ChannelSync(m, path).sync(stations("A FM | http://a/s | Austin, TX"), URL)
    assert res.removed == ["B FM"] and m.Channel.objects.all().count() == 1
    assert m.Channel.objects.get(name="A FM").channel_group.name == GROUP_NAME
    assert ChannelSync(m, path).remove_all() == 1 and m.Channel.objects.all().count() == 0


# ---- H2: nothing is downloaded without a size cap -----------------------------
@pytest.fixture
def endless_server():
    """An HTTP server that answers every GET with bytes that never stop."""
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "audio/mpeg")
            self.end_headers()
            try:
                while True:
                    self.wfile.write(b"\xff" * 65536)
                    if "slow" in self.path:
                        time.sleep(0.2)
            except OSError:
                pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()


def test_h2_endless_playlists_and_segments_are_cut_off(endless_server):
    t0 = time.monotonic()
    with pytest.raises(net.TooLarge):
        audio.resolve_stream(f"{endless_server}/live.m3u")
    with pytest.raises(net.TooLarge):
        audio.resolve_stream(f"{endless_server}/live.pls")
    with pytest.raises(net.TooLarge):
        hls._get_text(f"{endless_server}/live.m3u8")
    assert hls._get_segment(f"{endless_server}/seg.ts", threading.Event()) is None
    assert time.monotonic() - t0 < 20       # it used to hang for ever


def test_h2_fetch_limited_has_an_overall_deadline(endless_server):
    # Slow enough to stay under the size cap; only the deadline can stop it.
    with pytest.raises(TimeoutError):
        net.fetch_limited(f"{endless_server}/slow", 10 ** 9, deadline=1.0)


# ---- M1: a busy port is refused before anything is changed -------------------
@pytest.fixture
def plugin(tmp_path, monkeypatch):
    monkeypatch.setattr(plugin_mod, "_data_dir", lambda: tmp_path / "data")
    models = make_models()
    monkeypatch.setattr(plugin_mod.Plugin, "_models", staticmethod(lambda: models))
    p = plugin_mod.Plugin()
    yield p, models
    p._stop_service(keep_desired=False)


def test_m1_sync_refuses_a_port_something_else_holds(plugin):
    p, m = plugin
    busy = socket.socket()
    busy.bind(("127.0.0.1", 0))
    busy.listen(1)
    try:
        port = busy.getsockname()[1]
        res = p.run("sync", {}, {"settings": {"stations": TWO, "port": port}})
        assert res["status"] == "error" and str(port) in res["message"]
        assert m.Channel.objects.all().count() == 0           # nothing was changed
        assert not p.config_path.exists()
    finally:
        busy.close()


def test_m1_our_own_server_on_the_port_is_not_a_problem(plugin, monkeypatch):
    p, _ = plugin
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    try:
        port = listener.getsockname()[1]
        assert p._port_problem(port)                           # a stranger holds it
        net.write_json(str(p.status_path), {"running": True, "port": port})
        assert p._port_problem(port)                           # a stale status file changes nothing
        with monkeypatch.context() as mp:                      # undone before the fixture stops the service
            mp.setattr(p, "_service_pid", lambda: os.getpid())
            assert p._port_problem(port) is None               # it's our running server
    finally:
        listener.close()


# ---- M2: a GPU encoder that dies at start is replaced by software -------------
class _Proc:
    def __init__(self, code):
        self.code = code

    def wait(self, timeout=None):
        if self.code is None:
            raise encoder.subprocess.TimeoutExpired("ffmpeg", timeout)
        return self.code


def _gpu_encoder(monkeypatch, name="nvenc"):
    encoder.forget()
    monkeypatch.setattr(encoder, "probe", lambda *a, **k: None)   # the start-up test passes
    return encoder.Encoder(320, 180, render_fps=4, output_fps=25, video_kbps=1000, audio_kbps=96,
                           output="-", video_encoder=name)


def test_m2_gpu_failure_right_after_start_falls_back(monkeypatch):
    enc = _gpu_encoder(monkeypatch)
    assert enc.args["video_encoder"] == "nvenc"
    enc.proc, enc.started_at = _Proc(1), time.monotonic()
    reason = enc.gpu_failed_at_start()
    assert reason and "code 1" in reason
    encoder.mark_failed("nvenc", reason)
    assert encoder.resolve("nvenc") == ("software", reason)        # later renderers use software
    assert encoder.cached("nvenc") == ("software", reason)         # and Status says why
    encoder.forget()                                               # Sync asks for a new test
    assert encoder.resolve("nvenc") == ("nvenc", None)
    encoder.forget()


def test_m2_other_failures_are_not_blamed_on_the_gpu(monkeypatch):
    enc = _gpu_encoder(monkeypatch)
    enc.proc, enc.started_at = _Proc(1), time.monotonic() - encoder.EARLY_EXIT_SECONDS - 1
    assert enc.gpu_failed_at_start() is None            # it ran fine for a while
    enc.proc, enc.started_at = _Proc(None), time.monotonic()
    assert enc.gpu_failed_at_start() is None            # ffmpeg is still running
    encoder.forget()
    soft = encoder.Encoder(320, 180, render_fps=4, output_fps=25, video_kbps=1000, audio_kbps=96,
                           output="-")
    soft.proc, soft.started_at = _Proc(1), time.monotonic()
    assert soft.gpu_failed_at_start() is None           # software has nothing to fall back to


def test_m2_station_records_the_failure(tmp_path, monkeypatch):
    encoder.forget()
    monkeypatch.setattr(encoder, "probe", lambda *a, **k: None)
    st = RenderSettings(width=320, height=180, data_dir=str(tmp_path), video_encoder="nvenc")
    r = StationRunner(Station("X", "http://127.0.0.1:9/x", "Theme: Space"), st, "/dev/null")
    r.encoder.proc, r.encoder.started_at = _Proc(1), time.monotonic()
    r.encoder.abort = lambda: None
    r._fail()
    assert encoder.resolve("nvenc")[0] == "software"
    encoder.forget()


# ---- M3: an expired city lookup is used when a fresh one fails ----------------
def test_m3_expired_geocode_is_the_fallback(tmp_path, monkeypatch):
    cache = tmp_path / "geocode" / "seattle-wa.json"
    net.write_json(str(cache), {"name": "Seattle", "lat": 47.6, "lon": -122.3, "country": "United States",
                                "country_code": "US", "admin1": "Washington", "timezone": "", "population": 1,
                                "_t": time.time() - 90 * 86400, "_v": geo.CACHE_VERSION, "extra": "ignored"})

    def down(*a, **k):
        raise ConnectionError("geocoding is down")
    monkeypatch.setattr(geo, "_lookup", down)
    place = geo.geocode("Seattle, WA", str(tmp_path))
    assert place is not None and place.label == "Seattle, Washington"
    with pytest.raises(ConnectionError):                 # nothing saved: the error still shows
        geo.geocode("Portland, OR", str(tmp_path))

    # When the service answers, the fresh answer replaces the expired one.
    monkeypatch.setattr(geo, "_lookup", lambda name, q: {"name": "Seattle", "latitude": 1.0, "longitude": 2.0})
    assert geo.geocode("Seattle, WA", str(tmp_path)).lat == 1.0
    assert geo.geocode(" , ,", str(tmp_path)) is None


# ---- M4: the ffmpeg log of a running station is kept small --------------------
def test_m4_log_rotation_while_ffmpeg_holds_the_file(tmp_path):
    path = str(tmp_path / "ffmpeg-x.log")
    with open(path, "ab") as held:                       # as ffmpeg holds it: append mode
        held.write(b"w" * 5000)
        held.flush()
        assert not encoder.rotate_log(path, max_bytes=10_000, in_use=True)
        assert encoder.rotate_log(path, max_bytes=1000, in_use=True)
        assert os.path.getsize(path) == 0 and os.path.getsize(path + ".1") == 5000
        held.write(b"next line\n")                       # lands at the new end, not at offset 5000
        held.flush()
        assert os.path.getsize(path) == 10
    assert not encoder.rotate_log(None) and not encoder.rotate_log(str(tmp_path / "missing.log"))
    assert encoder.rotate_log(path + ".1", max_bytes=1000)       # not in use: a plain rename
    assert os.path.exists(path + ".1.1")


# ---- L2: huge originals are rejected before they are decoded ------------------
def test_l2_usable_size_has_a_pixel_limit(tmp_path, monkeypatch):
    path = str(tmp_path / "big.png")
    Image.new("L", (4000, 2500)).save(path)             # 10 megapixels, landscape
    assert images._usable_size(path)
    monkeypatch.setattr(images.nasa, "MAX_PIXELS", 5_000_000)
    assert not images._usable_size(path)


# ---- L3: a frame is written whole even when the pipe takes it in pieces -------
def test_l3_write_video_finishes_short_writes():
    class Pipe:
        def __init__(self):
            self.got = bytearray()

        def write(self, view):
            part = bytes(view[:7])                       # the pipe accepts 7 bytes at a time
            self.got += part
            return len(part)

    enc = encoder.Encoder(320, 180, render_fps=4, output_fps=25, video_kbps=1000, audio_kbps=96,
                          output="-")
    enc.proc = type("P", (), {"stdin": Pipe()})()
    frame = bytes(range(100))
    enc.write_video(frame)
    assert bytes(enc.proc.stdin.got) == frame
    enc.proc.stdin.write = lambda view: 0                # a closed pipe must not loop for ever
    with pytest.raises(BrokenPipeError):
        enc.write_video(frame)
    enc.proc = None


# ---- L4: a new round of photos doesn't open with the one on screen ------------
def test_l4_reshuffle_never_repeats_the_photo_on_screen(tmp_path, monkeypatch):
    from rs import station as station_mod
    files = [make_scenic(str(tmp_path / f"p{i}.jpg"), i, size=(320, 180)) for i in range(3)]
    st = RenderSettings(width=320, height=180, data_dir=str(tmp_path))
    r = StationRunner(Station("X", "http://127.0.0.1:9/x", "Theme: Space"), st, "/dev/null")
    r.started_at = 0.0
    r._photos = [images.Photo(f, os.path.basename(f), "A", "CC0", "") for f in files]
    # The worst shuffle: it puts the photo that is on screen first.
    monkeypatch.setattr(station_mod.random, "shuffle",
                        lambda seq: seq.sort(key=lambda p: p.file != r._shown_file))
    shown = []
    for i in range(12):
        r._advance_photo(100.0 + i)
        shown.append(r._shown_file)
    assert all(a != b for a, b in zip(shown, shown[1:]))


# ---- L5: HLS prefers audio, and refuses byte-range playlists ------------------
def test_l5_audio_only_variant_is_preferred(monkeypatch):
    master = ("#EXTM3U\n"
              '#EXT-X-STREAM-INF:BANDWIDTH=5000000,RESOLUTION=1920x1080,CODECS="avc1.640028,mp4a.40.2"\nvideo.m3u8\n'
              '#EXT-X-STREAM-INF:BANDWIDTH=96000,CODECS="mp4a.40.2"\naudio-lo.m3u8\n'
              '#EXT-X-STREAM-INF:BANDWIDTH=192000,CODECS="mp4a.40.2"\naudio-hi.m3u8\n')
    media = "#EXTM3U\n#EXT-X-TARGETDURATION:6\n#EXTINF:6,\ns1.ts\n"
    pl = hls.parse_playlist(master, "http://h/master.m3u8")
    assert len(pl.variants) == 3
    assert pl.audio_variants == [(96000, "http://h/audio-lo.m3u8"), (192000, "http://h/audio-hi.m3u8")]
    fetched = []

    def fake(url):
        fetched.append(url)
        return (master if url.endswith("master.m3u8") else media), url
    monkeypatch.setattr(hls, "_get_text", fake)
    assert hls.open_media_playlist("http://h/master.m3u8")[0] == "http://h/audio-hi.m3u8"
    assert "http://h/video.m3u8" not in fetched
    # With only video variants on offer, the best one is used as before.
    video_only = "#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1,RESOLUTION=640x360\nv.m3u8\n"
    assert hls.parse_playlist(video_only, "http://h/m.m3u8").audio_variants == []


def test_l5_byte_range_playlists_are_refused(monkeypatch):
    text = "#EXTM3U\n#EXT-X-TARGETDURATION:6\n#EXTINF:6,\n#EXT-X-BYTERANGE:1000@0\nall.ts\n"
    monkeypatch.setattr(hls, "_get_text", lambda url: (text, url))
    with pytest.raises(hls.HlsError, match="byte-range"):
        hls.open_media_playlist("http://h/a.m3u8")


# ---- L6: the old-Shoutcast fallback names the port in its Host header ---------
def test_l6_icy_fallback_sends_host_with_port():
    seen = {}
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]

    def serve():
        conn, _ = srv.accept()
        with conn:
            seen["request"] = conn.recv(4096).decode("latin-1")
            conn.sendall(b"ICY 200 OK\r\nicy-metaint: 0\r\n\r\n")

    t = threading.Thread(target=serve, daemon=True)
    t.start()
    try:
        with audio._IcyResponse(f"http://127.0.0.1:{port}/stream", {"Icy-MetaData": "1"}) as resp:
            assert resp.status_code == 200
        t.join(5)
    finally:
        srv.close()
    assert f"Host: 127.0.0.1:{port}\r\n" in seen["request"]


# ---- L7: a nonsense Retry-After doesn't break the retry -----------------------
def test_l7_retry_after_nan_is_ignored(monkeypatch):
    class Resp:
        def __init__(self, code):
            self.status_code, self.headers = code, {"Retry-After": "nan"}

        def raise_for_status(self):
            pass

        def json(self):
            return {"ok": True}

    replies = [Resp(429), Resp(200)]
    monkeypatch.setattr(net, "session", lambda: type("S", (), {"get": lambda self, *a, **k: replies.pop(0)})())
    waits = []
    monkeypatch.setattr(net.time, "sleep", waits.append)
    assert net.get_json("http://x/") == {"ok": True}
    assert waits == [2.0]                               # the normal backoff, not NaN


# ---- L8: names and cities that can't fit Dispatcharr's columns ----------------
def test_l8_over_long_names_and_cities_are_reported():
    ok = f"{'N' * MAX_NAME_LENGTH} | http://a/s | {'C' * MAX_CITY_LENGTH}"
    assert len(parse_stations(ok).stations) == 1
    parsed = parse_stations(f"{'N' * (MAX_NAME_LENGTH + 1)} | http://a/s | Austin\n"
                            f"B | http://b/s | {'C' * (MAX_CITY_LENGTH + 1)}")
    assert not parsed.stations and len(parsed.errors) == 2
    assert "station name is longer" in parsed.errors[0] and "city is longer" in parsed.errors[1]
    # The longest allowed pair still fits a 255-character stream name.
    from rs.channels import stream_name
    assert len(stream_name(parse_stations(ok).stations[0])) <= 255


# ---- L9: a new linger time reaches stations that are already there ------------
def test_l9_linger_change_reaches_existing_stations(tmp_path):
    sts = stations("A FM | http://a/s | Austin, TX")
    srv = RadioServer(sts, RenderSettings(data_dir=str(tmp_path)), linger_seconds=10.0)
    b = next(iter(srv.broadcasts.values()))
    srv.update(sts, linger_seconds=3.0)
    assert next(iter(srv.broadcasts.values())) is b and b.linger == 3.0 and srv.linger == 3.0


# ---- O1 / O2: the CPU Use setting and quality-based rate control --------------
def test_o1_cpu_use_setting(monkeypatch):
    p = plugin_mod.Plugin()
    normal, low = p._render_settings({}), p._render_settings({"cpu_mode": "low"})
    assert (normal.output_fps, normal.x264_preset) == (25, "veryfast")
    assert (low.output_fps, low.x264_preset) == (15, "superfast")
    assert p._render_settings({"cpu_mode": "; rm -rf /"}).x264_preset == "veryfast"
    cmd = encoder.build_command(1280, 720, 4, low.output_fps, 3000, 128, 5, "-",
                                x264_preset=low.x264_preset)
    assert cmd[cmd.index("-preset") + 1] == "superfast" and cmd[cmd.index("-g") + 1] == "30"
    assert "fps=15" in cmd[cmd.index("-vf") + 1]
    # An unknown preset never reaches ffmpeg's command line.
    cmd = encoder.build_command(1280, 720, 4, 25, 3000, 128, 5, "-", x264_preset="--evil")
    assert cmd[cmd.index("-preset") + 1] == "veryfast"


def test_o2_software_encoding_is_quality_based_with_a_cap():
    cmd = encoder.build_command(1280, 720, 4, 25, 3000, 128, 5, "-")
    assert cmd[cmd.index("-crf") + 1] == str(encoder.SOFTWARE_CRF)
    assert cmd[cmd.index("-maxrate") + 1] == "3000k" and cmd[cmd.index("-bufsize") + 1] == "6000k"
    assert "-b:v" not in cmd


# ---- O3: still frames are recognised, moving ones are not ---------------------
def test_o3_static_key_matches_only_identical_frames():
    comp = Compositor((320, 180), "X", crossfade=2.0, credit_seconds=5.0)
    info = PanelInfo("X", "Seattle", "Artist - Song")
    photo = Image.new("RGB", (320, 180), (10, 80, 160))
    comp.show(photo, "Photo: A / CC0 / Wikimedia Commons", 100.0, 60.0)
    assert comp.static_key(info, 100.5) is None                    # crossfading
    comp.frame(info, 103.0)                                        # the fade is over
    k1, k2 = comp.static_key(info, 103.0), comp.static_key(info, 104.0)
    assert k1 is not None and k1 == k2
    assert comp.frame(info, 103.0).tobytes() == comp.frame(info, 104.0).tobytes()
    assert comp.static_key(info, 105.5) is None                    # the credit is fading out
    k3 = comp.static_key(info, 110.0)
    assert k3 is not None and k3 != k1                             # the credit has gone
    assert comp.frame(info, 110.0).tobytes() != comp.frame(info, 103.0).tobytes()
    # A new title, or a new photo, is a new picture.
    assert comp.static_key(PanelInfo("X", "Seattle", "Other - Song"), 110.0) != k3
    comp.show(photo, None, 200.0, 60.0)
    comp.frame(info, 203.0)
    assert comp.static_key(info, 203.0) not in (k1, k3)


def test_o3_video_loop_reuses_still_frames(tmp_path, monkeypatch):
    # No crossfade, so the picture is still from the first frame on.
    st = RenderSettings(width=320, height=180, data_dir=str(tmp_path), render_fps=50.0,
                        crossfade_seconds=0.0)
    r = StationRunner(Station("X", "http://127.0.0.1:9/x", "Theme: Space"), st, "/dev/null")
    drawn, written = [], []
    real_frame = r.comp.frame
    monkeypatch.setattr(r.comp, "frame", lambda info, now: drawn.append(1) or real_frame(info, now))

    def write(data):
        written.append(data)
        if len(written) >= 6:
            r._stop.set()
    r.encoder.write_video = write
    r._photos = []
    r.started_at = time.monotonic()
    r._video_loop()
    assert len(written) >= 6 and len(set(written)) == 1            # the same still picture each time
    assert len(drawn) < len(written)                               # ...drawn fewer times than sent


# ---- O4: the photo index is parsed once per version of the file ---------------
def test_o4_index_is_read_again_only_when_it_changes(tmp_path, monkeypatch):
    lib = images.ImageLibrary(str(tmp_path), "seattle", "Seattle", None, None)
    assert lib._index() == {}                                      # no file yet
    reads = []
    real = net.read_json
    monkeypatch.setattr(images.net, "read_json", lambda p: reads.append(p) or real(p))
    net.write_json(lib.index_path, {"images": [], "city": "Seattle"})
    assert lib._index()["city"] == "Seattle" and lib._index()["city"] == "Seattle"
    assert len(reads) == 1
    net.write_json(lib.index_path, {"images": [], "city": "Tacoma"})   # a refresh rewrote it
    assert lib._index()["city"] == "Tacoma" and len(reads) == 2
    os.unlink(lib.index_path)
    assert lib._index() == {}

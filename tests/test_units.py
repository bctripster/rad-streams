import os

from PIL import Image

from rs import audio, geo, images, weather
from rs.config import Station, parse_stations
from rs.motion import PanelMotion
from rs.render import Compositor, PanelInfo, fit_photo

from fakes import load_fixture, make_scenic


# ---- station list ---------------------------------------------------------
def test_parse_stations():
    text = """
    # comment
    KEXP | https://kexp.streamguys1.com/kexp160.aac | Seattle, WA | 501
    Radio Paradise | http://stream.radioparadise.com/mp3-192 | Paradise, CA
    bad line
    No Url | ftp://x | Somewhere
    KEXP | https://dup.example/x | Seattle
    Neg | https://a.example/s | Town | -3
    """
    r = parse_stations(text)
    assert [s.name for s in r.stations] == ["KEXP", "Radio Paradise"]
    assert r.stations[0].channel_number == 501
    assert r.stations[1].channel_number is None
    assert len(r.errors) == 4
    assert any("duplicate" in e for e in r.errors)


def test_station_id_stable_and_url_safe():
    a = Station("Radio Ñ / Test!", "http://x", "City")
    b = Station("Radio Ñ / Test!", "http://other", "Elsewhere")
    assert a.station_id == b.station_id
    assert all(c.isalnum() or c == "-" for c in a.station_id)


# ---- ICY ------------------------------------------------------------------
def test_parse_stream_title():
    assert audio.parse_stream_title("StreamTitle='Daft Punk - One More Time';StreamUrl='';") == \
        "Daft Punk - One More Time"
    assert audio.parse_stream_title("StreamTitle='Rock 'n' Roll - It's Fine';") == \
        "Rock 'n' Roll - It's Fine"
    assert audio.parse_stream_title("StreamUrl='x';") is None


def test_decode_meta_latin1_fallback():
    assert audio.decode_meta("StreamTitle='Café';".encode("latin-1") + b"\x00\x00") == \
        "StreamTitle='Café';"


def test_clean_title_generic():
    for t in ["", "  ", "-", "Unknown", "ADVERTISEMENT", "KEXP", "https://kexp.org", "12345"]:
        assert audio.clean_title(t, "KEXP") is None, t
    assert audio.clean_title("  Björk - Jóga ", "KEXP") == "Björk - Jóga"


def test_split_artist_title():
    assert audio.split_artist_title("Björk - Jóga") == ("Björk", "Jóga")
    assert audio.split_artist_title("Just A Title") == ("", "Just A Title")


def test_pcm_buffer_primes_and_pads():
    buf = audio.PcmBuffer(prime_seconds=0.01, max_seconds=1)
    assert buf.read(8) == bytes(8)            # not primed yet -> silence
    buf.write(b"\x01" * 2000)
    assert buf.read(8) == b"\x01" * 8         # primed
    out = buf.read(4000)                      # underrun: data then zero padding
    assert out[:1992] == b"\x01" * 1992 and out[1992:] == bytes(2008)
    assert buf.underruns == 1


# ---- geocoding --------------------------------------------------------------
def test_pick_result_qualifiers():
    results = load_fixture("geocode_portland.json")["results"]
    assert geo.pick_result(results, [])["admin1"] == "Oregon"          # most populous
    assert geo.pick_result(results, ["ME"])["admin1"] == "Maine"       # state code
    assert geo.pick_result(results, ["Maine"])["admin1"] == "Maine"
    assert geo.pick_result(results, ["Australia"])["country_code"] == "AU"
    assert geo.pick_result(results, ["France"]) is None


def test_place_label():
    p = geo.Place("Lyon", 45.7, 4.8, country="France", country_code="FR", admin1="Auvergne")
    assert p.label == "Lyon, France"
    p = geo.Place("Burlington", 44.4, -73.2, country="United States", country_code="US", admin1="Vermont")
    assert p.label == "Burlington, Vermont"


# ---- weather ----------------------------------------------------------------
def test_weather_parse():
    w = weather.parse(load_fixture("forecast.json"), "F")
    assert round(w.temp) == 54 and w.condition == "Partly cloudy"
    assert w.summary() == "54°F  Partly cloudy  ·  H 61°  L 47°"
    assert w.utc_offset_seconds == -14400


# ---- images -----------------------------------------------------------------
def test_commons_filters():
    pages = load_fixture("commons_search.json")["query"]["pages"]
    kept = [images.acceptable(p) for p in pages]
    titles = [k["title"] for k in kept if k]
    assert titles == ["Burlington waterfront at sunset.jpg", "Lake Champlain view.jpg"]
    first = next(k for k in kept if k)
    assert first["artist"] == "Jane Doe" and first["license"] == "CC BY-SA 4.0"
    assert first["url"].endswith("1920px-a.jpg")


def test_search_tiers_and_fallback():
    calls = []

    def search(q, width):
        calls.append(q)
        if "incategory:\"Burlington\"" in q:
            return load_fixture("commons_search.json")["query"]["pages"]
        return []

    found, tiers = images.find_candidates("Burlington", 44.47, -73.21, 1920, want=2, search=search)
    assert "nearcoord:12km,44.4700,-73.2100" in calls[0] and "Quality_images" in calls[0]
    assert len(found) == 2 and tiers == ["category:2"]
    # without coordinates only the name-based tiers run
    calls.clear()
    images.find_candidates("Burlington", None, None, 1920, want=2, search=search)
    assert not any("nearcoord" in c for c in calls)


def test_library_refresh_downloads_and_prunes(tmp_path, monkeypatch):
    src = make_scenic(str(tmp_path / "src.jpg"), seed=1, size=(1600, 1000))
    pages = load_fixture("commons_search.json")["query"]["pages"]
    monkeypatch.setattr(images, "_search", lambda q, w, limit=50: pages)
    lib = images.ImageLibrary(str(tmp_path / "cache"), "burlington", "Burlington", 44.4, -73.2,
                              count=6)
    monkeypatch.setattr(lib, "_download", lambda url, path: open(path, "wb").write(open(src, "rb").read()))
    os.makedirs(lib.dir, exist_ok=True)
    open(os.path.join(lib.dir, "stale.jpg"), "wb").write(b"x")
    assert lib.refresh() == 2
    assert not os.path.exists(os.path.join(lib.dir, "stale.jpg"))
    photos = lib.photos()
    assert {p.title for p in photos} == {"Burlington waterfront at sunset.jpg", "Lake Champlain view.jpg"}
    assert photos[0].credit.endswith("/ Wikimedia Commons")
    assert not lib.is_stale()


# ---- motion -----------------------------------------------------------------
def test_motion_jumps_and_stays_in_safe_area():
    m = PanelMotion("cycle", seed=3)
    frame, panel = (1920, 1080), (760, 320)
    seen = set()
    for i in range(8):
        m.jump(i * 60.0, 60.0)
        seen.add(m.preset)
        for t in (0, 30, 60):
            x, y = m.position(i * 60.0 + t, frame, panel)
            assert 96 <= x <= 1920 - 96 - 760 and 54 <= y <= 1080 - 54 - 320
        # no drift between jumps: the panel holds still (drift looked glitchy after frame blending)
        assert m.position(i * 60.0, frame, panel) == m.position(i * 60.0 + 59, frame, panel)
    assert len(seen) == 8
    c = PanelMotion("corners", seed=1)
    corners = {c.jump(i, 60) for i in range(8)}
    assert len(corners) == 4
    r = PanelMotion("random", seed=1)
    prev = r.preset
    for i in range(20):
        assert r.jump(i, 60) != prev
        prev = r.preset


# ---- rendering --------------------------------------------------------------
def test_compositor_frames(tmp_path):
    size = (1280, 720)
    comp = Compositor(size, "KEXP", crossfade=2.0, seed=1)
    info = PanelInfo("KEXP", "Seattle, Washington", "Björk - Jóga",
                     "54°F  Partly cloudy  ·  H 61°  L 47°")
    f0 = comp.frame(info, 0.0)
    assert f0.size == size and f0.mode == "RGB"
    photo = fit_photo(make_scenic(str(tmp_path / "a.jpg"), seed=2, size=(1600, 1000)), size)
    comp.show(photo, "Photo: Jane Doe / CC BY-SA 4.0 / Wikimedia Commons", 10.0, 60.0)
    mid = comp.frame(info, 11.0)
    end = comp.frame(info, 13.0)
    assert comp.previous is None              # crossfade finished
    assert mid.tobytes() != end.tobytes()
    # very long titles still fit in the panel
    long = PanelInfo("A Station With An Extremely Long Name Indeed", "Llanfairpwllgwyngyll, Wales",
                     "Someone - " + "word " * 60, None, "Reconnecting to stream…")
    assert comp.frame(long, 20.0).size == size


def test_splash_cached_on_disk(tmp_path):
    from rs import splash
    size = (480, 270)
    img = splash.splash_background(size, str(tmp_path))
    assert img.size == size and img.mode == "RGB"
    files = list(tmp_path.glob("splash-v*-480x270.png"))
    assert len(files) == 1
    splash._splash_background.cache_clear()
    again = splash.splash_background(size, str(tmp_path))     # loaded, not redrawn
    assert again.tobytes() == img.tobytes()
    from rs.render import splash_size
    splash.splash_background(splash_size(size), str(tmp_path))     # what the service prewarms
    n = len(list(tmp_path.glob("splash-*.png")))
    comp = Compositor(size, "KEXP", splash_dir=str(tmp_path))
    assert len(list(tmp_path.glob("splash-*.png"))) == n             # the Compositor reused it
    assert comp.on_splash and comp.fallback.size == size
    assert comp.motion.preset in ("top-right", "bottom-right", "middle-right")   # clear of the logo
    first = comp.fallback.tobytes()
    comp.show(None, None, 60.0, 60.0)                        # still no photos: the splash shifts
    assert comp.on_splash and comp.fallback.tobytes() != first
    assert comp.motion.preset in ("top-right", "bottom-right", "middle-right")


def test_panel_labels_weather_place():
    from rs.render import Fonts, build_panel
    fonts = Fonts(1080)
    base = PanelInfo("KEXP", "Seattle, WA", None, "54°F  Partly cloudy")
    plain = build_panel(base, fonts, 1920, (255, 0, 0, 255))
    base.weather_place = "Seattle"
    labelled = build_panel(base, fonts, 1920, (255, 0, 0, 255))
    assert labelled.size[1] > plain.size[1]     # the "WEATHER IN SEATTLE" caption adds a row
    assert labelled.tobytes() != plain.tobytes()
    assert not hasattr(base, "clock")          # no clock: it would only show server time

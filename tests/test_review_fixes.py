"""Regression tests for issues found in the code review."""
import threading
import time

import pytest

from rs import audio, hls, images
from rs.config import RenderSettings, Station
from rs.server import Broadcast


def test_title_regex_stops_at_underscored_keys():
    meta = "StreamTitle='A - B';adw_ad='true';durationMilliseconds='3000';"
    assert audio.parse_stream_title(meta) == "A - B"


def test_photo_filenames_unique_for_non_latin_titles():
    names = {images.photo_filename(t) for t in
             ("File:東京タワー.jpg", "File:Москва Кремль.jpg", "File:القاهرة.jpg", "File:Paris.png")}
    assert len(names) == 4
    assert images.photo_filename("File:Paris.png").endswith(".png")


class _BadFactory:
    def __call__(self, station, settings):
        raise FileNotFoundError("ffmpeg")


def test_failed_start_leaves_no_phantom_viewer():
    b = Broadcast(Station("X", "http://x", "Y"), RenderSettings(), _BadFactory(), linger_seconds=0)
    c = b.attach("test")
    assert b.runner is None and b.last_error and "failed to start" in b.last_error
    assert c.q.get(timeout=1) is None          # told to disconnect
    b.detach(c)
    assert b.clients == []


def test_closed_broadcast_refuses_viewers():
    b = Broadcast(Station("X", "http://x", "Y"), RenderSettings(), _BadFactory(), linger_seconds=0)
    b.close()
    assert b.attach("test") is None


def test_failed_refresh_backs_off(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(images, "find_candidates", lambda *a, **k: calls.append(1) or ([], []))
    lib = images.ImageLibrary(str(tmp_path), "nowhere", "Nowhere", None, None)
    lib.refresh_async()
    for _ in range(50):
        if calls and lib.dir not in images._REFRESHING:
            break
        time.sleep(0.05)
    lib.refresh_async()                         # within the backoff: no new search
    time.sleep(0.2)
    assert len(calls) == 1
    images._RETRY_AT.pop(lib.dir, None)


def test_hls_skips_a_missing_segment_and_handles_sequence_reset(monkeypatch):
    lists = iter([
        "#EXTM3U\n#EXT-X-TARGETDURATION:1\n#EXT-X-MEDIA-SEQUENCE:100\n"
        "#EXTINF:1,\na100.ts\n#EXTINF:1,\na101.ts\n#EXTINF:1,\na102.ts\n",
        # encoder restarted: sequence numbers start again
        "#EXTM3U\n#EXT-X-TARGETDURATION:1\n#EXT-X-MEDIA-SEQUENCE:0\n"
        "#EXTINF:1,\nb0.ts\n#EXTINF:1,\nb1.ts\n",
    ])

    def get_text(url):
        return next(lists), "http://h/live.m3u8"

    def get_segment(url, stop, tries=3):
        return None if url.endswith("a101.ts") else url.rsplit("/", 1)[-1].encode()

    monkeypatch.setattr(hls, "_get_text", get_text)
    monkeypatch.setattr(hls, "_get_segment", get_segment)
    stop = threading.Event()
    got = []
    for body, _ in hls.segments("http://h/live.m3u8", stop):
        got.append(body)
        if len(got) == 4:
            break
    stop.set()
    assert got == [b"a100.ts", b"a102.ts", b"b0.ts", b"b1.ts"]


def test_icy_status_line_fallback():
    import socket
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]

    def serve():
        conn, _ = srv.accept()
        conn.recv(4096)
        conn.sendall(b"ICY 200 OK\r\nicy-name: Old Radio\r\nicy-metaint: 16\r\n"
                     b"content-type: audio/mpeg\r\n\r\n" + b"x" * 16 + b"\x00")
        conn.close()

    threading.Thread(target=serve, daemon=True).start()
    with audio._IcyResponse(f"http://127.0.0.1:{port}/", {"Icy-MetaData": "1"}) as r:
        r.raise_for_status()
        assert r.headers["icy-metaint"] == "16" and r.headers["Content-Type"] == "audio/mpeg"
        assert audio._read_exact(r.raw, 16) == b"x" * 16
    srv.close()


def test_no_desc_title_is_generic():
    assert audio.clean_title("no desc", "BBC Radio 1") is None
    assert audio.clean_title("No Description", "BBC Radio 1") is None


def _page(title, width=3000, thumb=True, cats=""):
    info = {"mime": "image/jpeg", "width": width, "height": int(width / 1.5),
            "extmetadata": {"LicenseShortName": {"value": "CC BY-SA 4.0"},
                            "Artist": {"value": "Jane"}, "Categories": {"value": cats}}}
    if thumb:
        info["thumburl"] = "https://upload.wikimedia.org/x/1920px-x.jpg"
    return {"title": f"File:{title}", "imageinfo": [info]}


def test_commons_thumbnails_only_and_no_scans():
    ok = images.acceptable(_page("Harbor at dusk.jpg", cats="Portland, Maine|Pages with maps|"
                                 "Uploaded with UploadWizard|Self-published work"))
    assert ok and ok["url"].endswith("1920px-x.jpg")
    assert images.acceptable(_page("Harbor.jpg", width=1900)) is None      # would be the original
    assert images.acceptable(_page("Harbor.jpg", thumb=False)) is None
    assert images.acceptable(_page("Portland card 1912.jpg")) is None
    assert images.acceptable(_page("Old view.jpg", cats="Portland, Maine|Scanned documents")) is None
    assert images.acceptable(_page("Old view.jpg", cats="Bird's-eye views of Portland, Maine")) is None
    assert images.thumb_width(1280) == 1280 and images.thumb_width(3840) == 1920


def test_region_fallback_only_when_city_is_short():
    calls = []

    def search(q, width):
        calls.append(q)
        if 'incategory:"Smallville' in q:
            return [_page(f"Smallville street {i}.jpg") for i in range(3)]
        if "100km" in q and "Quality_images" in q:
            return [_page(f"Kansas prairie sunset {i}.jpg") for i in range(10)]
        return []

    found, tiers = images.find_candidates("Smallville, Kansas", 39.0, -98.0, 1920, want=10,
                                          search=search, region="Kansas")
    assert any(t.startswith("region-quality") for t in tiers)
    assert len(found) == 13
    # plenty in the city itself: the region is never searched
    calls.clear()
    big = lambda q, w: [_page(f"Metropolis view {i} {len(q)}.jpg") for i in range(12)]
    found, tiers = images.find_candidates("Metropolis, Kansas", 39.0, -98.0, 1920, want=10,
                                          search=big, region="Kansas")
    assert not any(t.startswith("region") for t in tiers)


def test_scenic_photos_rank_first():
    plain = images.acceptable(_page("Main Street building.jpg"))
    lake = images.acceptable(_page("IMG 1234.jpg", cats="Lakes of Maine"))
    sky = images.acceptable(_page("Portland skyline at dusk.jpg"))
    assert lake["scenic"] and sky["scenic"] and not plain["scenic"]
    plain["tier"], lake["tier"], sky["tier"] = "quality-nearby", "nearby", "region-scenic"
    ranked = images.rank([plain, lake, sky], shuffle=False)
    assert [r["title"] for r in ranked] == ["IMG 1234.jpg", "Portland skyline at dusk.jpg",
                                            "Main Street building.jpg"]
    assert images.search_queries("Portland, Maine", 43.6, -70.2)[1][0] == "scenic-nearby"


def test_orbital_and_closeup_photos_rejected():
    assert images.acceptable(_page("ISS026-E-8585 - View of Arizona.jpg")) is None
    assert images.acceptable(_page("STS-114 Earth.jpg", cats="Photos by the International Space Station")) is None
    assert images.acceptable(_page("Desert bloom.jpg", cats="Flowers of Texas")) is None
    assert images.acceptable(_page("Gator.jpg", cats="Alligator farms in Texas")) is None
    assert images.acceptable(_page("Chisos Mountains view.jpg", cats="Big Bend National Park"))["scenic"]
    assert images.acceptable(_page("Grasshopper in Davis Mountain Preserve.JPG")) is None
    assert images.acceptable(_page("New England cottontail.jpg")) is None
    assert images.acceptable(_page("Deer Isle harbor view.jpg"))       # place names stay


def test_region_fallback_when_city_has_only_plain_photos():
    def search(q, width):
        if "100km" in q and "Quality_images" not in q:
            return [_page(f"Big Bend canyon river {i}.jpg") for i in range(15)]
        if "12km" in q and "Quality_images" not in q and " OR " not in q:
            return [_page(f"Terlingua building {i}.jpg") for i in range(12)]
        return []

    found, tiers = images.find_candidates("Terlingua, Texas", 29.3, -103.6, 1920, want=10,
                                          search=search, region="Texas")
    assert any(t.startswith("region-scenic") for t in tiers)
    top = images.rank(found)[:10]
    assert all(c["tier"] == "region-scenic" for c in top)       # scenery beats plain town shots
    q = dict(images.region_queries("Texas", 29.3, -103.6))["region-name"]
    assert 'incategory:"Landscapes_of_Texas"' in q and '"Texas"' not in q


@pytest.mark.parametrize("raw,want", [
    ('title="American Woman",artist="Guess Who"', "Guess Who - American Woman"),
    ('artist="Guess Who" title="American Woman"', "Guess Who - American Woman"),
    ('Guess Who - text="American Woman" song_spot="M" MediaBaseId="123"', "Guess Who - American Woman"),
    ("title=American Woman,artist=Guess Who", "Guess Who - American Woman"),
    ('title="American Woman"', "American Woman"),
    ("Guess Who - American Woman", "Guess Who - American Woman"),
    ("Band = Name - Song", "Band = Name - Song"),
])
def test_key_value_titles(raw, want):
    assert audio.clean_title(raw, "Station") == want
    meta = f"StreamTitle='{raw}';"
    assert audio.clean_title(audio.parse_stream_title(meta), "Station") == want


def test_credit_fades_out_after_credit_seconds():
    from rs.render import Compositor, PanelInfo
    size = (480, 270)
    comp = Compositor(size, "KEXP", crossfade=1.0, credit_seconds=10.0, seed=1)
    from PIL import Image
    photo = Image.new("RGB", size, (90, 120, 160))
    comp.show(photo, "Photo: Jane / CC BY-SA 4.0 / Wikimedia Commons", 0.0, 60.0)
    assert comp._credit_alpha(5.0, 5.0) == 1.0
    assert 0 < comp._credit_alpha(10.75, 10.75) < 1
    assert comp._credit_alpha(12.0, 12.0) == 0.0
    info = PanelInfo("KEXP", "Seattle, WA")
    assert comp.frame(info, 5.0).tobytes() != comp.frame(info, 20.0).tobytes()  # credit gone
    always = Compositor(size, "KEXP", crossfade=1.0, credit_seconds=0.0, seed=1)
    always.show(photo, "Photo: Jane", 0.0, 60.0)
    assert always._credit_alpha(50.0, 50.0) == 1.0


@pytest.mark.parametrize("raw,want", [
    (". - Junkhouse - Shine ~ ", "Junkhouse - Shine"),
    ("~~ Junkhouse - Shine ~~", "Junkhouse - Shine"),
    ("Junkhouse -  - Shine", "Junkhouse - Shine"),
    ("* Guess Who - No Time *", "Guess Who - No Time"),
    ("R.E.M. - Losing My Religion", "R.E.M. - Losing My Religion"),
    ("Panic! At The Disco - High Hopes", "Panic! At The Disco - High Hopes"),
    ("Wham! - Wake Me Up Before You Go-Go", "Wham! - Wake Me Up Before You Go-Go"),
    (" - ~ ", None),
])
def test_tidy_titles(raw, want):
    assert audio.clean_title(raw, "Station") == want
    if want:
        assert audio.split_artist_title(want)[0]


def test_html_codes_in_titles_become_characters():
    c = lambda t: audio.clean_title(t, "KEXP")
    assert c("Guns N&apos; Roses - Don&apos;t Cry") == "Guns N' Roses - Don't Cry"
    assert c("Bob Marley - &amp;apos;Jammin&amp;apos;") == "Bob Marley - 'Jammin'"   # encoded twice
    assert c("Sam&aposs Song") == "Sam's Song"                                     # no ";"
    assert c("&#39;Til Tuesday - Voices Carry") == "'Til Tuesday - Voices Carry"
    assert c("Simon &amp; Garfunkel - America") == "Simon & Garfunkel - America"
    assert c("R&B Hits") == "R&B Hits"                                             # a real "&" stays
    assert c("A &ampersand") == "A &ampersand"
    assert c('title="Don&apos;t Stop",artist="Fleetwood Mac"') == "Fleetwood Mac - Don't Stop"
    # quotes encoded too: decoded before the key="value" unpacking
    assert c("title=&quot;American Woman&quot;,artist=&quot;Guess Who&quot;") == "Guess Who - American Woman"
    assert c('Guess Who - text=&quot;American Woman&quot; song_spot=&quot;M&quot;') == "Guess Who - American Woman"

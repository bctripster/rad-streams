"""Photo rotation on refresh, the cache cleanup, and a 5 s start-up splash before the cached photos."""
import os
import time

from rs import cleanup, images
from rs.config import RenderSettings, Station
from rs.images import photo_filename, rotate
from rs.net import write_json
from rs.station import StationRunner

from fakes import make_scenic


def _cand(title, scenic=True):
    return {"title": title, "scenic": scenic, "tier": "quality-nearby"}


def test_refresh_swaps_in_new_photos():
    ranked = [_cand(f"File:Old {i}.jpg") for i in range(20)] + [_cand(f"File:New {i}.jpg") for i in range(20)]
    have = {photo_filename(f"File:Old {i}.jpg") for i in range(20)}
    picked = rotate(ranked, have, 20)[:20]
    new = [c for c in picked if c["title"].startswith("File:New")]
    assert len(new) == 10                     # half the set is new each refresh
    assert len(picked) == 20
    assert rotate(ranked, set(), 20) == ranked   # first fetch: plain ranking


def test_rotation_prefers_new_scenic_over_new_plain():
    ranked = [_cand("File:Old.jpg"), _cand("File:New plain.jpg", scenic=False), _cand("File:New view.jpg")]
    out = rotate(ranked, {photo_filename("File:Old.jpg")}, 2)
    assert [c["title"] for c in out[:2]] == ["File:New view.jpg", "File:Old.jpg"]


def test_refresh_downloads_only_new_and_prunes(tmp_path, monkeypatch):
    lib = images.ImageLibrary(str(tmp_path), "town", "Town", 1.0, 2.0, count=4)
    pool = [dict(_cand(f"File:P{i}.jpg"), url=f"u{i}", artist="a", license="CC0", page="p")
            for i in range(12)]
    monkeypatch.setattr(images, "find_candidates", lambda *a, **k: (list(pool), ["t"]))
    got = []
    monkeypatch.setattr(lib, "_download", lambda url, path: (got.append(url), open(path, "wb").close()))
    lib.refresh()
    first = {p.title for p in lib.photos()}
    assert len(first) == 4 and len(got) == 4
    got.clear()
    lib.refresh()
    second = {p.title for p in lib.photos()}
    assert len(second) == 4 and len(second - first) == 2 and len(got) == 2
    assert len(os.listdir(lib.dir)) == 5      # 4 photos + index.json; dropped ones deleted


def test_cleanup_removes_unused_cities_old_splashes_and_leftovers(tmp_path):
    root = tmp_path / "images"
    for city in ("used", "gone"):
        write_json(str(root / city / "index.json"), {"images": [{"file": "a.jpg"}]})
        (root / city / "a.jpg").write_bytes(b"x")
        (root / city / "stray.jpg").write_bytes(b"x")
    old = time.time() - 40 * 86400
    os.utime(root / "gone" / "index.json", (old, old))
    for name in ("used", "gone"):
        os.utime(root / name / "stray.jpg", (old, old))
    (tmp_path / "splash-v1-480x270.png").write_bytes(b"x")
    (tmp_path / "splash-v1-520x292.png").write_bytes(b"x")
    (tmp_path / "abc.tmp").write_bytes(b"x")
    os.utime(tmp_path / "abc.tmp", (old, old))

    cleanup.sweep(str(tmp_path), keep_splash=["splash-v1-520x292.png"])

    assert not (root / "gone").exists()
    assert sorted(os.listdir(root / "used")) == ["a.jpg", "index.json"]
    assert sorted(p.name for p in tmp_path.iterdir()) == ["images", "splash-v1-520x292.png"]


def test_opening_a_city_marks_it_used(tmp_path):
    write_json(str(tmp_path / "images" / "x" / "index.json"), {"images": []})
    idx = tmp_path / "images" / "x" / "index.json"
    old = time.time() - 40 * 86400
    os.utime(idx, (old, old))
    images.ImageLibrary(str(tmp_path), "x", "X", None, None).touch()
    cleanup.sweep(str(tmp_path))
    assert idx.exists()


def test_splash_shows_5_seconds_then_cached_photos(tmp_path):
    st = RenderSettings(width=320, height=180, data_dir=str(tmp_path))
    d = os.path.join(st.cache_dir, "images", "nowhere")
    os.makedirs(d)
    make_scenic(os.path.join(d, "a.jpg"), seed=1)
    write_json(os.path.join(d, "index.json"),
               {"fetched_at": time.time(), "city": "Nowhere", "want": st.images_per_city,
                "images": [{"file": "a.jpg", "title": "A"}]})
    r = StationRunner(Station("X", "http://127.0.0.1:9/x", "Nowhere"), st, "/dev/null")
    r.started_at = 100.0
    r._advance_photo(100.0)               # the first frame goes out before photos load
    assert r.comp.on_splash and r._next_switch > 105.0
    r._open_library()
    assert r._next_switch == 105.0        # cached photos take over after the 5 s splash
    r._advance_photo(102.0)               # even with photos, the splash holds for 5 s
    assert r.comp.on_splash and r._next_switch == 105.0
    r._advance_photo(105.0)
    assert not r.comp.on_splash


def test_running_station_keeps_marking_its_photos_used(tmp_path, monkeypatch):
    from rs import station as station_mod
    st = RenderSettings(width=320, height=180, data_dir=str(tmp_path))
    r = StationRunner(Station("X", "http://127.0.0.1:9/x", "Theme: Space"), st, "/dev/null")
    touches = []
    monkeypatch.setattr(station_mod, "TOUCH_SECONDS", 0.0)
    monkeypatch.setattr(images.ImageLibrary, "touch", lambda self: touches.append(1))
    monkeypatch.setattr(images.ImageLibrary, "refresh_async", lambda self: None)
    r._stop.wait = lambda t: r._stop.set() if len(touches) >= 2 else None
    r._data_loop()
    assert len(touches) >= 2                  # on open, then again while running

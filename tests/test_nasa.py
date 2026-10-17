"""The NASA Image Library source: parsing, filtering, credits, and size checks."""
import os

from PIL import Image

from rs import images, nasa


def _item(title, nasa_id="ID1", keywords=(), desc="", photographer="", links=None):
    return {"data": [{"title": title, "nasa_id": nasa_id, "media_type": "image", "keywords": list(keywords),
                      "description": desc, "photographer": photographer}],
            "links": links if links is not None else
            [{"href": f"https://images-assets.nasa.gov/image/{nasa_id}/{nasa_id}~thumb.jpg", "rel": "preview"}]}


def test_search_parses_filters_and_credits(monkeypatch):
    items = [
        _item("Carina Nebula", "C1", photographer="Hubble Heritage Team"),
        _item("Pillars of Creation", "C2", keywords=["nebula", "Webb"]),           # term in keywords
        _item("Webb telescope mirror", "C3", desc="the Orion nebula seen ..."),   # only in description
        _item("Artist's concept of a nebula", "C4"),
        _item("Nebula over the observatory", "C5", desc="Image © 2019 Some Photographer"),
        _item("Crab Nebula", "C6", links=[{"href": "https://x/C6~orig.tif"}, {"href": "https://x/C6~large.jpg"}]),
    ]
    calls = []
    monkeypatch.setattr(nasa.net, "get_json", lambda url, params: calls.append(params) or
                        {"collection": {"items": items}})
    out = nasa.search("nebula")
    assert calls[0]["q"] == "nebula" and calls[0]["media_type"] == "image"
    assert [c["title"] for c in out] == ["Carina Nebula", "Pillars of Creation", "Crab Nebula"]
    assert out[0]["url"].endswith("C1~large.jpg") and out[2]["url"] == "https://x/C6~large.jpg"
    assert out[0]["artist"] == "NASA / Hubble Heritage Team" and out[1]["artist"] == "NASA"
    assert out[0]["page"] == "https://images.nasa.gov/details/C1" and out[0]["check_size"]
    photo = images.Photo("/x.jpg", out[0]["title"], out[0]["artist"], out[0]["license"], out[0]["page"],
                         out[0]["source"])
    assert photo.credit == "Photo: NASA / Hubble Heritage Team / Public domain / NASA Image Library"


def test_refresh_keeps_only_big_enough_nasa_photos(tmp_path, monkeypatch):
    lib = images.ImageLibrary(str(tmp_path), "theme-space", "Space", None, None, count=4,
                              queries=[("nasa-title", "nebula")])
    pool = [{"title": f"Nebula {i}", "scenic": True, "url": f"u{i}", "artist": "NASA",
             "license": "Public domain", "page": "p", "source": "NASA Image Library", "check_size": True,
             "tier": "nasa-title"} for i in range(3)]
    monkeypatch.setattr(images, "find_candidates", lambda *a, **k: (list(pool), ["nasa-title:3"]))
    sizes = {"u0": (1920, 1080), "u1": (800, 600), "u2": (2000, 2000)}   # big, too small, square

    def fake_download(url, path):
        Image.new("RGB", sizes[url]).save(path, "JPEG")
    monkeypatch.setattr(lib, "_download", fake_download)
    lib.refresh()
    kept = lib.photos()
    assert [p.title for p in kept] == ["Nebula 0"] and kept[0].source == "NASA Image Library"
    assert sorted(os.listdir(lib.dir)) == sorted([os.path.basename(kept[0].file), "index.json"])


def _sized(nasa_id, renditions):
    return [{"href": f"https://images-assets.nasa.gov/image/{nasa_id}/{nasa_id}~{name}.jpg",
             "width": w, "height": h, "size": size, "rel": "canonical" if name == "orig" else "alternate"}
            for name, w, h, size in renditions]


def test_rendition_chosen_from_listed_sizes():
    pick = nasa._rendition
    # ~medium at 1280 wide is enough; no download check needed
    assert pick(_sized("A", [("orig", 6000, 4000, 9e6), ("medium", 1280, 853, 3e5)])) == \
        ("https://images-assets.nasa.gov/image/A/A~medium.jpg", False)
    # medium too small -> orig, if it isn't huge
    assert pick(_sized("B", [("orig", 3000, 2000, 4e6), ("medium", 640, 427, 1e5)]))[0].endswith("B~orig.jpg")
    assert pick(_sized("C", [("orig", 12000, 8000, 9e7)])) == (None, False)        # too big
    assert pick(_sized("D", [("orig", 1920, 1920, 1e6)])) == (None, False)         # square
    assert pick(_sized("E", [("orig", None, None, None)])) == \
        ("https://images-assets.nasa.gov/image/E/E~orig.jpg", True)                # unknown: check later
    assert pick([{"href": "https://x/F~orig.tif", "width": 4000, "height": 3000}]) == (None, False)


def test_nasa_filters_from_the_live_check(monkeypatch):
    items = [
        _item("KSC-2009-1100", "KSC-2009-1100", keywords=["night sky"]),
        _item("Kepler at night sky test", "K2", keywords=["night sky"], desc="in the clean room at Astrotech"),
        _item("Hubble Captures Vivid Auroras in Jupiter", "J1"),
        _item("Aurora over Canada from the ISS", "A1"),
        _item("Astronauts photograph aurora", "A2"),
    ]
    monkeypatch.setattr(nasa.net, "get_json", lambda url, params: {"collection": {"items": items}})
    assert [c["title"] for c in nasa.search("aurora")] == ["Aurora over Canada from the ISS"]
    assert nasa.search('"night sky"') == []
    earth = [_item("Earth observation taken by the Expedition 68 crew", "iss068e1"),
             _item("Galileo view of Moon and Earth", "G1", keywords=["earth observation"]),
             _item("iss068e012345", "iss068e012345", keywords=["earth observation"])]
    monkeypatch.setattr(nasa.net, "get_json", lambda url, params: {"collection": {"items": earth}})
    assert [c["title"] for c in nasa.search("earth observation")] == [
        "Earth observation taken by the Expedition 68 crew", "iss068e012345"]


def test_event_announcements_and_earth_institutes_are_skipped(monkeypatch):
    from rs import themes
    items = [_item("NASA Google+ Hangout: 'Earthrise' A New Visualization", "E1", keywords=["earth observation"]),
             _item("Earth observation over the Sahara", "E2")]
    monkeypatch.setattr(nasa.net, "get_json", lambda url, params: {"collection": {"items": items}})
    assert [c["title"] for c in nasa.search("earth observation")] == ["Earth observation over the Sahara"]
    earth = themes.parse("Theme: Earth")
    assert not earth.text_tier
    for title in ("Higashimatsuyama Earth Observation Center Entrance 1.jpg", "Spaceship Earth at Night.jpg",
                  "Qihao Weng spoke at the International Workshop on Earth Observation.jpg"):
        page = {"title": f"File:{title}", "imageinfo": [{"mime": "image/jpeg", "width": 3000, "height": 2000,
                "thumburl": "https://x/1920px-x.jpg", "extmetadata": {"LicenseShortName": {"value": "CC0"}}}]}
        assert images.acceptable(page, theme=True, **earth.filters()) is None, title

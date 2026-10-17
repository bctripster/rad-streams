"""Theme channels ("Theme: Space"): their own photo searches, no city lookup or weather."""
import os

from rs import images, themes
from rs.config import RenderSettings, Station, parse_stations
from rs.render import Fonts, PanelInfo, build_panel
from rs.station import StationRunner


def _page(title, cats=""):
    info = {"mime": "image/jpeg", "width": 3000, "height": 2000, "thumburl": "https://x/1920px-x.jpg",
            "extmetadata": {"LicenseShortName": {"value": "CC BY 4.0"},
                            "Artist": {"value": "ESA/Hubble"}, "Categories": {"value": cats}}}
    return {"title": f"File:{title}", "imageinfo": [info]}


def test_parse_theme_column():
    assert themes.parse("Seattle, WA") is None
    assert themes.parse("Theme:") is None
    space = themes.parse("Theme: Space")
    assert space.label == "Space" and space.key == "theme-space" and "nebula" in space.terms
    assert themes.parse("  theme :  northern   lights ").label == "Aurora"     # alias, any spacing
    custom = themes.parse("Theme: old lighthouses")
    assert custom.label == "Old lighthouses"
    assert custom.terms == ("old lighthouses", "old lighthouse")    # unquoted, singular too
    assert custom.key.startswith("theme-old-lighthouses-")
    assert themes.parse("Theme: Glass").terms == ("Glass",)
    # one query per term (no "( ... OR "phrase")" groups, which Commons mishandles); space has no text tier
    qs = space.queries()
    order = []
    for t, _ in qs:
        if not order or order[-1] != t:
            order.append(t)
    assert order == ["theme-featured-title", "nasa-title", "theme-quality-title", "theme-featured",
                     "theme-quality"]
    assert len(qs) == 4 * len(space.terms) + len(space.nasa)
    assert not any("(" in q or " OR " in q for t, q in qs if not t.startswith("nasa"))
    assert qs[0][1].endswith("intitle:nebula")       # title matches first: on-topic
    assert 'intitle:"old lighthouse"' in " ".join(q for _, q in custom.queries())
    aurora = themes.parse("Theme: Aurora")
    assert all("northern" in t or "aurora " in t for t in aurora.terms)    # no bare "aurora"
    assert len(aurora.queries()) == 6 * len(aurora.terms) + len(aurora.nasa)
    assert not themes.parse("Theme: Forest").nasa and not themes.parse("Theme: Cats").nasa


def test_custom_themes_never_share_a_folder():
    names = ["Night-Sky", "Cats", "Cats!", "CATS", "宇宙", "Кошки"]
    keys = [themes.parse(f"Theme: {n}").key for n in names]
    assert len(set(keys)) == len(keys)
    assert keys[0] != themes.parse("Theme: Night Sky").key          # the built-in theme
    assert themes.parse("Theme: Cats").key == themes.parse("Theme:  Cats ").key


def test_theme_line_parses_as_a_station():
    res = parse_stations("Soma | https://ice1.somafm.com/x | Theme: Nature | 7")
    assert not res.errors and res.stations[0].city == "Theme: Nature"


def test_theme_filters_allow_space_and_wildlife_but_not_maps():
    orbit = _page("ISS-42 E 1234 Earth at night.jpg", cats="Photos by the International Space Station")
    animal = _page("Red squirrel in snow.jpg", cats="Sciurus vulgaris|Mammals of Scotland")
    chart = _page("Star chart of Orion.jpg")
    assert images.acceptable(orbit) is None and images.acceptable(animal) is None   # city channels
    assert images.acceptable(orbit, theme=True)["scenic"]
    assert images.acceptable(animal, theme=True, animals=True)                        # Theme: Wildlife
    assert images.acceptable(chart, theme=True, animals=True) is None


def test_scenery_themes_keep_out_animals_named_after_habitats():
    forest = themes.parse("Theme: Forest").filters()
    assert forest == {"animals": False, "planetary": False}
    assert themes.parse("Theme: Wildlife").filters()["animals"]
    assert themes.parse("Theme: Cats").filters()["animals"]          # a theme the user names
    assert themes.parse("Theme: Space").filters()["planetary"]
    for title in ("White-winged forest sylph.jpg", "Mountain gorilla.jpg", "Boletus in Finnish forest.jpg",
                  "Earth Observation Antenna - Maspalomas.jpg", "Dead baby bird.jpg",
                  "Low-end EF3 damage to a home in Virginia Beach.jpg"):
        assert images.acceptable(_page(title), theme=True, **forest) is None, title
    assert images.acceptable(_page("Kibale forest canopy.jpg", cats="Featured pictures of animals"),
                             theme=True, **forest) is None
    mars = _page("PIA20736 Glowing Gullies in Kaiser Crater.jpg")
    assert images.acceptable(mars, theme=True, **forest) is None
    assert images.acceptable(mars, theme=True, **themes.parse("Theme: Space").filters())
    assert images.acceptable(_page("Beech forest in autumn.jpg"), theme=True, **forest)
    # city channels are unchanged: "Fox River" and "Griffith Observatory" are places
    assert images.acceptable(_page("Fox River at dusk.jpg")) and images.acceptable(_page("Griffith Observatory.jpg"))
    assert "tarn" not in themes.parse("Theme: Lakes").terms
    assert not any("bird" in t for t in themes.parse("Theme: Wildlife").terms)


def test_theme_search_uses_only_theme_queries():
    seen = []

    def search(query, width):
        seen.append(query)
        return [_page(f"Nebula {len(seen)}-{i}.jpg") for i in range(5)]

    nasa_seen = []

    def nasa_search(term):
        nasa_seen.append(term)
        return [{"title": f"Hubble {term} {i}", "scenic": True, "url": "u", "artist": "NASA",
                 "license": "Public domain", "page": "p", "source": "NASA Image Library", "check_size": True}
                for i in range(2)]

    space = themes.parse("Theme: Space")
    found, tiers = images.find_candidates("Space", None, None, 1920, want=100, search=search,
                                          region="Texas", queries=space.queries(), nasa_search=nasa_search)
    assert len(seen) + len(nasa_seen) == len(space.queries()) and all("nearcoord" not in q for q in seen)
    assert nasa_seen == list(space.nasa)
    assert "incategory:Featured_pictures_on_Wikimedia_Commons intitle:nebula" in seen[0]
    n = 5 * len(space.terms)
    assert tiers == [f"theme-featured-title:{n}", f"nasa-title:{2 * len(space.nasa)}", f"theme-quality-title:{n}",
                     f"theme-featured:{n}", f"theme-quality:{n}"]
    ranked = images.rank(found)
    assert ranked[0]["tier"] == "theme-featured-title" and ranked[-1]["tier"] == "theme-quality"
    few, _ = images.find_candidates("Space", None, None, 1920, want=2, search=search,
                                    queries=space.queries(), nasa_search=nasa_search)
    assert len(few) == 10                     # stops once it has three times what it needs


def test_theme_station_skips_city_and_weather(tmp_path):
    def no_geocode(*a):
        raise AssertionError("theme channels don't look up a city")

    st = RenderSettings(width=320, height=180, data_dir=str(tmp_path))
    r = StationRunner(Station("Soma", "http://127.0.0.1:9/x", "Theme: Space"), st, "/dev/null",
                      geocoder=no_geocode, weather_fetch=no_geocode)
    r._open_library()
    assert r.library.dir.endswith(os.path.join("images", "theme-space")) and r.library.queries
    info = r.panel_info()
    assert info.city == "Space" and info.themed and info.weather is None
    shown = []
    r.comp.show = lambda photo, credit, now, span: shown.append(credit)
    r._advance_photo(100.0)
    assert shown == [None]                    # no "Weather: Open-Meteo.com" credit either


def test_theme_panel_has_no_live_from_line_or_theme_name():
    fonts = Fonts(1080)
    city = build_panel(PanelInfo("Soma", "Space"), fonts, 1920, (255, 0, 0, 255))
    theme = build_panel(PanelInfo("Soma", "Space", themed=True), fonts, 1920, (255, 0, 0, 255))
    assert theme.size[1] < city.size[1]
    # with a track playing the layout matches, but the "· Space" after the station name is gone
    playing = build_panel(PanelInfo("Soma", "Space", "Air - La femme d'argent"), fonts, 1920, (255, 0, 0, 255))
    themed = build_panel(PanelInfo("Soma", "Space", "Air - La femme d'argent", themed=True),
                         fonts, 1920, (255, 0, 0, 255))
    same_name = build_panel(PanelInfo("Soma", "Elsewhere", "Air - La femme d'argent", themed=True),
                            fonts, 1920, (255, 0, 0, 255))
    assert themed.size == playing.size and themed.tobytes() != playing.tobytes()
    assert themed.tobytes() == same_name.tobytes()


def test_one_photographer_cannot_fill_the_set():
    letters = "abcdefghij"
    ordered = [{"title": f"D{x}", "artist": "Dietmar"} for x in letters] + \
              [{"title": f"O{x}", "artist": f"Other {x}"} for x in letters]
    out = images.vary_authors(ordered, 20)
    assert [c["title"] for c in out[:13]] == ["Da", "Db", "Dc"] + [f"O{x}" for x in letters]
    assert len(out) == 20                      # the extras still fill in at the end


def test_one_source_or_series_cannot_fill_the_set():
    nasa = [{"title": f"Earth observation {i}", "artist": f"NASA Johnson Space Center / crew {i}"}
            for i in range(5)]
    series = [{"title": f"Earth at Night 2012 (SVS{i})", "artist": f"Person {i}"} for i in range(5)]
    out = images.vary_authors(nasa + series, 20)
    assert sum(1 for c in out[:6] if c["artist"].startswith("NASA")) == 3
    assert sum(1 for c in out[:6] if c["title"].startswith("Earth at Night")) == 3
    for title in ("Wildlife Conservation Issue 8c 1971 U.S. stamps.jpg",
                  "Carved tree with reliefs of dinosaur and other animals.jpg"):
        assert images.acceptable(_page(title), theme=True, animals=True) is None

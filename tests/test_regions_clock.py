"""Province/state qualifiers and exact-name ranking."""
from rs import geo
from rs.config import RenderSettings, Station
from rs.net import write_json
from rs.station import StationRunner

VANCOUVER = [
    {"name": "Vancouver Island", "latitude": 49.6, "longitude": -125.6, "country_code": "CA",
     "country": "Canada", "admin1": "British Columbia", "feature_code": "ISL", "population": 870297},
    {"name": "Vancouver", "latitude": 49.25, "longitude": -123.12, "country_code": "CA",
     "country": "Canada", "admin1": "British Columbia", "feature_code": "PPL", "population": 600000},
    {"name": "Vancouver", "latitude": 45.64, "longitude": -122.66, "country_code": "US",
     "country": "United States", "admin1": "Washington", "feature_code": "PPL", "population": 190915},
]
PERTH = [
    {"name": "Perth", "latitude": -31.95, "longitude": 115.86, "country_code": "AU",
     "country": "Australia", "admin1": "Western Australia", "feature_code": "PPLA", "population": 2000000},
    {"name": "Perth", "latitude": 56.39, "longitude": -3.43, "country_code": "GB",
     "country": "United Kingdom", "admin1": "Scotland", "feature_code": "PPL", "population": 47180},
]


def test_canadian_provinces():
    assert geo.pick_result(VANCOUVER, ["BC"], "Vancouver")["name"] == "Vancouver"
    assert geo.pick_result(VANCOUVER, ["B.C."], "Vancouver")["admin1"] == "British Columbia"
    assert geo.pick_result(VANCOUVER, ["British Columbia"], "Vancouver")["name"] == "Vancouver"
    hit = geo.pick_result(VANCOUVER, ["Canada"], "Vancouver")
    assert hit["name"] == "Vancouver" and hit["country_code"] == "CA"   # not the island


def test_exact_name_beats_bigger_partial_match_without_feature_codes():
    plain = [{k: v for k, v in r.items() if k != "feature_code"} for r in VANCOUVER]
    assert geo.pick_result(plain, ["Canada"], "Vancouver")["name"] == "Vancouver"


def test_region_codes_are_per_country():
    assert geo.pick_result(VANCOUVER, ["WA"], "Vancouver")["country_code"] == "US"   # Washington
    assert geo.pick_result(PERTH, ["WA"], "Perth")["country_code"] == "AU"           # Western Australia
    assert geo.pick_result(PERTH, ["UK"], "Perth")["country_code"] == "GB"
    assert geo.pick_result(VANCOUVER, ["ON"], "Vancouver") is None


def test_label_uses_province():
    p = geo.Place("Vancouver", 49.25, -123.12, country="Canada", country_code="CA",
                  admin1="British Columbia")
    assert p.label == "Vancouver, British Columbia"


def test_old_cache_entries_are_ignored(tmp_path, monkeypatch):
    stale = {"name": "Vancouver Island", "lat": 49.6, "lon": -125.6, "country": "Canada",
             "country_code": "CA", "admin1": "British Columbia", "timezone": "", "population": 1,
             "_t": 9e12}
    write_json(str(tmp_path / "geocode" / "vancouver-canada.json"), stale)
    monkeypatch.setattr(geo.net, "get_json", lambda url, params: {"results": VANCOUVER})
    assert geo.geocode("Vancouver, Canada", str(tmp_path)).name == "Vancouver"
    # and the fresh answer is what gets cached now
    monkeypatch.setattr(geo.net, "get_json", lambda *a: (_ for _ in ()).throw(AssertionError))
    assert geo.geocode("Vancouver, Canada", str(tmp_path)).name == "Vancouver"


def test_photo_cache_refetches_when_city_resolves_differently(tmp_path):
    from rs.images import ImageLibrary
    write_json(str(tmp_path / "images" / "vancouver-canada" / "index.json"),
               {"fetched_at": 9e12, "city": "Vancouver Island, British Columbia",
                "images": [{"file": "a.jpg", "title": "a"}]})
    lib = ImageLibrary(str(tmp_path), "vancouver-canada", "Vancouver, British Columbia", 49.2, -123.1)
    assert lib.is_stale()
    same = ImageLibrary(str(tmp_path), "vancouver-canada", "Vancouver Island, British Columbia", 49.6, -125.6)
    assert not same.is_stale()

"""Fixes from the live Milestone 1 checks: retries, qualified photo searches, St./Saint."""
import requests

from rs import geo, images, net


class FakeResp:
    def __init__(self, status, body=None, headers=None):
        self.status_code, self._body, self.headers = status, body, headers or {}

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code} error")


class FakeSession:
    def __init__(self, responses):
        self.responses, self.calls = list(responses), 0

    def get(self, url, params=None, timeout=None):
        self.calls += 1
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def test_get_json_retries_429_timeouts_and_5xx(monkeypatch):
    sleeps = []
    monkeypatch.setattr(net.time, "sleep", sleeps.append)
    fake = FakeSession([FakeResp(429, headers={"Retry-After": "90"}), requests.Timeout("hang"),
                        FakeResp(503), FakeResp(200, {"ok": 1})])
    monkeypatch.setattr(net, "session", lambda: fake)
    assert net.get_json("http://x") == {"ok": 1}
    assert fake.calls == 4 and sleeps[0] == net.MAX_RETRY_WAIT


def test_get_json_gives_up(monkeypatch):
    monkeypatch.setattr(net.time, "sleep", lambda s: None)
    monkeypatch.setattr(net, "session", lambda: FakeSession([FakeResp(429)] * 4))
    try:
        net.get_json("http://x")
    except requests.HTTPError:
        pass
    else:
        raise AssertionError("expected HTTPError")


def test_pick_result_prefers_populated_places():
    results = [
        {"name": "St. Paul Downtown Airport", "feature_code": "AIRP", "admin1": "Minnesota", "population": 0},
        {"name": "St. Paul Park", "feature_code": "PPL", "admin1": "Minnesota", "population": 5000},
    ]
    assert geo.pick_result(results, ["MN"])["feature_code"] == "PPL"


def test_geocode_expands_st_to_saint(monkeypatch):
    by_name = {
        "St. Paul": [{"name": "St. Paul Downtown Airport", "feature_code": "AIRP", "latitude": 44.93,
                      "longitude": -93.06, "admin1": "Minnesota", "country_code": "US"}],
        "Saint Paul": [{"name": "Saint Paul", "feature_code": "PPLA", "latitude": 44.94,
                        "longitude": -93.09, "admin1": "Minnesota", "country_code": "US",
                        "population": 303176}],
    }
    monkeypatch.setattr(net, "get_json", lambda url, params: {"results": by_name.get(params["name"], [])})
    assert geo.geocode("St. Paul, MN").name == "Saint Paul"
    assert geo.geocode("Portland, ME") is None  # no hit and nothing to expand


def test_search_queries_use_full_city_name():
    tiers = dict(images.search_queries("Portland, Maine", 43.66, -70.26))
    assert list(tiers) == ["quality-nearby", "scenic-nearby", "category", "nearby", "text"]
    assert 'incategory:"Portland,_Maine"' in tiers["category"]
    assert tiers["text"].endswith('"Portland, Maine"')
    assert list(dict(images.search_queries("Portland", None, None))) == ["category", "text"]


def test_bad_titles_filtered():
    for t in ("Cathedral interior - Portland.jpg", "Milliken Mausoleum - Evergreen Cemetery.JPG",
              "Marine Marching Band (4945890813).jpg"):
        assert images._BAD_TITLE.search(t.replace("-", " ")), t
    assert not images._BAD_TITLE.search("Bandstand at Deering Oaks")

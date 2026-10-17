from rs import geo, images, validate
from rs.config import parse_stations

from fakes import FakeIcyServer, fake_geocoder, make_tone_mp3


def test_validate_reports_and_summary(tmp_path, monkeypatch):
    mp3 = make_tone_mp3(str(tmp_path / "t.mp3"))
    good = FakeIcyServer(mp3, ["Artist - Song"])
    generic = FakeIcyServer(mp3, ["Unknown"])
    noicy = FakeIcyServer(mp3, send_icy=False)
    monkeypatch.setattr(geo, "geocode", lambda city, cache=None: None if "Atlantis" in city else fake_geocoder(city))
    monkeypatch.setattr(images, "find_candidates", lambda *a, **k: ([{"title": "x"}] * 4, ["nearby:4"]))
    monkeypatch.setattr(validate, "STREAM_SECONDS", 3)
    parsed = parse_stations(
        f"Good | {good.url} | Burlington, VT\n"
        f"Generic | {generic.url} | Burlington, VT\n"
        f"NoIcy | {noicy.url} | Burlington, VT\n"
        f"Dead | http://127.0.0.1:9/x | Atlantis\n")
    try:
        reports = validate.validate_all(parsed.stations, str(tmp_path))
    finally:
        for s in (good, generic, noicy):
            s.close()
    by = {r.station.name: r for r in reports}
    assert by["Good"].ok and any("now playing: Artist - Song" in n for n in by["Good"].notes)
    assert not by["Generic"].ok and "generic" in by["Generic"].problems[0]
    assert not by["NoIcy"].ok and "no ICY metadata" in by["NoIcy"].problems[0]
    assert not by["Dead"].ok and any("not found" in p for p in by["Dead"].problems)
    text = validate.format_report(reports, ["line 9: bad"])
    assert "[OK] Good" in text and "[PROBLEM] Dead" in text
    msg = validate.summarize(reports, ["line 9: bad"], "/data/rad_streams/logs/validate.log")
    assert msg.startswith("1/4 stations OK, 1 bad line(s)") and len(msg) <= 280
    assert msg.endswith("Full report: /data/rad_streams/logs/validate.log")

"""v0.6.0: Dispatcharr 0.34 plugin features.

1. A stations table (Dispatcharr "table" setting) replaces the text box, with
   every cell checked before Sync changes anything, and the old text copied in.
2. Sync and Validate run as background tasks with a progress bar, and every
   run saves a report.
3. An "Advanced" section, collapsed, holds the encoder, bitrate, port and
   resolution settings.
4. Sync and Remove ask Dispatcharr to refresh its pages.
"""
import json
import os
import stat
import sys
import types
from pathlib import Path

import pytest

import plugin as plugin_mod
from rs import reports, validate
from rs.config import (COLUMN_IDS, MAX_CELL_LENGTH, MAX_STATIONS, TABLE_COLUMNS, Station,
                       parse_rows, parse_stations, rows_from_text)

from fake_orm import make_models

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = json.loads((ROOT / "plugin.json").read_text(encoding="utf-8"))

KEXP = {"name": "KEXP", "url": "https://kexp.streamguys1.com/kexp160.aac", "city": "Seattle, WA"}


def row(**cells):
    return dict(KEXP, **cells)


# ---- 1. the stations table ---------------------------------------------------
def test_table_rows_become_stations():
    r = parse_rows([row(number="501", id="Old KEXP", logo="https://x/l.png"),
                    {"name": "Space", "url": "http://s/x", "city": "Theme: Space"}])
    assert not r.errors
    a, b = r.stations
    assert (a.channel_number, a.key, a.logo_url, a.line_no) == (501, "Old KEXP", "https://x/l.png", 1)
    assert (b.channel_number, b.key, b.logo_url, b.line_no) == (None, None, None, 2)


@pytest.mark.parametrize("cells, column, why", [
    ({"name": ""}, "Name", "missing station name"),
    ({"name": "x" * 121}, "Name", "longer than 120"),
    ({"name": "A\x00B"}, "Name", "control character"),
    ({"url": "ftp://x"}, "Stream URL", "must start with http"),
    ({"url": "http://a b"}, "Stream URL", "no spaces"),
    ({"url": "javascript:alert(1)"}, "Stream URL", "must start with http"),
    ({"city": ""}, "City or Theme", "missing city"),
    ({"city": "Seattle\nWA"}, "City or Theme", "control character"),
    ({"number": "abc"}, "Channel #", "not a positive number"),
    ({"number": "-5"}, "Channel #", "not a positive number"),
    ({"number": "nan"}, "Channel #", "not a positive number"),
    ({"id": "x" * 101}, "Keep ID", "id must be 1 to 100"),
    ({"logo": "file:///etc/passwd"}, "Logo URL", "logo must be an http"),
    ({"name": True}, "Name", "must be text"),
    ({"city": ["Seattle"]}, "City or Theme", "must be text"),
    ({"logo": "https://x/" + "a" * MAX_CELL_LENGTH}, "Logo URL", "longer than"),
])
def test_every_problem_names_its_row_and_column(cells, column, why):
    r = parse_rows([row(), row(name="Second", **{k: v for k, v in cells.items() if k != "name"})
                    if "name" not in cells else row(**cells)])
    assert len(r.stations) == 1 and len(r.errors) == 1, r.errors
    assert r.errors[0].startswith("row 2"), r.errors
    assert f", {column}: " in r.errors[0] and why in r.errors[0], r.errors
    assert r.cell_errors[0]["row"] == 2 and r.cell_errors[0]["column"] in COLUMN_IDS


def test_numbers_from_the_api_are_accepted():
    r = parse_rows([row(number=501), row(name="B", number=7.5)])
    assert [s.channel_number for s in r.stations] == [501, 7.5] and not r.errors


def test_empty_and_skipped_rows_are_left_out():
    r = parse_rows([{}, {"name": "", "url": "", "city": "", "skip": False}, row(skip=True), row()])
    assert len(r.stations) == 1 and r.skipped == 1 and not r.errors


def test_only_a_real_true_ticks_skip():
    r = parse_rows([row(skip="true"), row(name="B", skip=1)])
    assert len(r.stations) == 2 and r.skipped == 0


def test_duplicates_and_bad_shapes_are_reported():
    r = parse_rows([row(), row(url="http://other/x"), "nonsense", row(name="B", id="kexp")])
    assert [s.name for s in r.stations] == ["KEXP"]
    assert "row 2 (KEXP): duplicate station name" in r.errors[0]
    assert "row 3: is not a table row" in r.errors[1]
    assert "duplicate id 'kexp'" in r.errors[2]
    assert parse_rows("text").errors == ["the Stations table is not a list of rows"]
    assert not parse_rows(None).errors and not parse_rows([]).stations


def test_station_count_is_capped():
    rows = [row(name=f"S{i}") for i in range(MAX_STATIONS + 3)]
    r = parse_rows(rows)
    assert len(r.stations) == MAX_STATIONS and "first 500" in r.errors[-1]


def test_table_columns_are_types_dispatcharr_accepts():
    assert {c["type"] for c in TABLE_COLUMNS} <= {"string", "number", "boolean", "select"}
    # Channel # is text on purpose: Dispatcharr's number cell sends "" when
    # cleared, which its own save check refuses.
    assert next(c for c in TABLE_COLUMNS if c["id"] == "number")["type"] == "string"


OLD_TEXT = """# My stations
KEXP | https://kexp.streamguys1.com/kexp160.aac | Seattle, WA | 501
WFMU | http://stream0.wfmu.org/freeform-128k | Jersey City, NJ | | id=Old WFMU | logo=https://x/w.png
# KCRW | https://kcrw.streamguys1.com/kcrw_192k_mp3_on_air | Santa Monica, CA
Space | http://s/x | Theme: Space | 12.5
just some words
Bad | ftp://nope | Austin
"""


def test_old_text_is_copied_into_rows_without_losing_anything():
    m = rows_from_text(OLD_TEXT)
    names = [r["name"] for r in m.rows]
    assert names == ["KEXP", "WFMU", "KCRW", "Space", "just some words", "Bad"]
    assert m.rows[1] == {"name": "WFMU", "url": "http://stream0.wfmu.org/freeform-128k",
                         "city": "Jersey City, NJ", "number": "", "id": "Old WFMU",
                         "logo": "https://x/w.png", "skip": False}
    assert m.rows[2]["skip"] is True                      # was commented out
    assert m.rows[3]["number"] == "12.5"
    assert len(m.notes) == 1 and "line 6" in m.notes[0]   # kept whole, with a note
    # Every value is the type Dispatcharr's table check wants on save.
    for r in m.rows:
        assert set(r) == set(COLUMN_IDS)
        assert all(isinstance(r[c], str) for c in COLUMN_IDS if c != "skip")
        assert isinstance(r["skip"], bool)


def test_migrated_rows_keep_every_station_id():
    """Same ids = same channels: the move to the table must not recreate any."""
    text = "\n".join(line for line in OLD_TEXT.splitlines()
                     if "just some" not in line and "Bad" not in line)
    old = parse_stations(text)
    new = parse_rows(rows_from_text(text).rows)
    assert not old.errors and not new.errors
    assert [s.station_id for s in new.stations] == [s.station_id for s in old.stations]
    assert [(s.channel_number, s.key, s.logo_url) for s in new.stations] == \
        [(s.channel_number, s.key, s.logo_url) for s in old.stations]


def test_migration_never_raises():
    for text in ("", "\n\n", "|||", "#", "# | | ", "a|" * 5000, "\x00|\x00|\x00"):
        rows_from_text(text)


def test_get_fields_fills_the_table_from_the_old_text():
    p = plugin_mod.Plugin()
    fields = p.get_fields({"settings": {"stations": OLD_TEXT}})
    table = next(f for f in fields if f["id"] == "station_table")
    assert [r["name"] for r in table["default"]][:2] == ["KEXP", "WFMU"]
    ids = [f["id"] for f in fields]
    assert ids[ids.index("station_table") + 1] == "migration_info"
    info = fields[ids.index("migration_info")]
    assert info["type"] == "info" and "line 6" in info["description"]
    # the class's own fields are untouched
    assert next(f for f in p.fields if f["id"] == "station_table")["default"] == []


@pytest.mark.parametrize("saved", [
    {}, {"stations": ""}, {"stations": "   \n"}, {"stations": 42},
    {"stations": OLD_TEXT, "station_table": []},          # a saved table wins, even empty
    {"stations": OLD_TEXT, "station_table": [row()]},
])
def test_get_fields_leaves_the_form_alone_otherwise(saved):
    fields = plugin_mod.Plugin().get_fields({"settings": saved})
    assert fields == plugin_mod.Plugin.fields
    assert plugin_mod.Plugin().get_fields(None) == plugin_mod.Plugin.fields


def test_sync_reads_the_table_before_the_old_text(plugin):
    p, _ = plugin
    table = p._stations({"stations": "A | http://a/x | Austin", "station_table": [row()]})
    assert [s.name for s in table.stations] == ["KEXP"]
    text = p._stations({"stations": "A | http://a/x | Austin"})
    assert [s.name for s in text.stations] == ["A"]


# ---- 2. background Sync / Validate, progress, locks, reports ----------------
@pytest.fixture
def plugin(tmp_path, monkeypatch):
    monkeypatch.setattr(plugin_mod, "_data_dir", lambda: tmp_path / "data")
    models = make_models()
    monkeypatch.setattr(plugin_mod.Plugin, "_models", staticmethod(lambda: models))
    p = plugin_mod.Plugin()
    yield p, models
    p._stop_service(keep_desired=False)


@pytest.fixture
def quiet_server(plugin, monkeypatch):
    """Sync without launching the real stream server."""
    p, models = plugin
    monkeypatch.setattr(plugin_mod.shutil, "which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(p, "_port_problem", lambda port: None)
    monkeypatch.setattr(p, "_ensure_service", lambda: True)
    monkeypatch.setattr(p, "_stop_service", lambda keep_desired: False)
    return p, models


def test_sync_and_validate_are_background_actions():
    for source in (MANIFEST["actions"], plugin_mod.Plugin.actions):
        flags = {a["id"]: a.get("async", False) for a in source}
        assert flags == {"sync": True, "validate": True, "status": False,
                         "stop_server": False, "remove_channels": False}
    assert "background_tasks" in MANIFEST["capabilities"]


def test_sync_reports_progress_and_saves_a_report(quiet_server):
    p, models = quiet_server
    seen = []
    r = p.run("sync", {}, {"settings": {"station_table": [row(), row(name="B", url="http://b/x")]},
                           "report_progress": lambda pct, msg: seen.append((pct, msg))})
    assert r["status"] == "ok", r
    assert r["channels"]["created"] == ["KEXP", "B"]
    pcts = [pct for pct, _ in seen]
    assert pcts == sorted(pcts) and all(isinstance(x, int) and 0 <= x <= 99 for x in pcts)
    assert any("Channels: 2/2" in msg for _, msg in seen)
    text = Path(r["report"]).read_text()
    assert "created: KEXP, B" in text and "server started" in text
    assert Path(r["report"]).parent.name == "reports"
    assert json.dumps(r)                                   # task history keeps it


def test_a_broken_progress_bar_never_breaks_sync(quiet_server):
    p, _ = quiet_server

    def boom(pct, msg):
        raise RuntimeError("websocket down")
    r = p.run("sync", {}, {"settings": {"station_table": [row()]}, "report_progress": boom})
    assert r["status"] == "ok", r
    r = p.run("sync", {}, {"settings": {"station_table": [row()]}, "report_progress": "not callable"})
    assert r["status"] == "ok", r


def test_sync_changes_nothing_while_a_cell_is_wrong(quiet_server, tmp_path):
    p, models = quiet_server
    assert p.run("sync", {}, {"settings": {"station_table": [row(), row(name="B", url="http://b/x")]}}
                 )["status"] == "ok"
    before = sorted(c.name for c in models.Channel.objects.all())
    r = p.run("sync", {}, {"settings": {"station_table": [row(), row(name="B", url="b")]}})
    assert r["status"] == "error" and "Nothing was changed" in r["message"]
    assert r["problems"] == [{"row": 2, "column": "url",
                              "problem": "stream URL must start with http:// or https:// and have no spaces"}]
    assert "Report: " in r["message"] and len(r["message"]) <= 280
    assert sorted(c.name for c in models.Channel.objects.all()) == before == ["B", "KEXP"]
    assert "row 2 (B), Stream URL" in Path(r["report"]).read_text()


def test_an_empty_table_never_deletes_channels(quiet_server):
    p, models = quiet_server
    p.run("sync", {}, {"settings": {"station_table": [row()]}})
    r = p.run("sync", {}, {"settings": {"station_table": []}})
    assert r["status"] == "error" and "Remove All Channels" in r["message"]
    assert models.Channel.objects.all().count() == 1
    # Skipping every row is a deliberate choice: the channel goes.
    r = p.run("sync", {}, {"settings": {"station_table": [row(skip=True)]}})
    assert r["status"] == "ok" and r["channels"]["removed"] == ["KEXP"], r
    assert models.Channel.objects.all().count() == 0


def test_an_empty_table_is_fine_with_no_channels(quiet_server):
    p, _ = quiet_server
    r = p.run("sync", {}, {"settings": {"station_table": []}})
    assert r["status"] == "ok" and "no stations" in r["message"]


def test_sync_warnings_are_a_partial_result(quiet_server):
    p, models = quiet_server
    models.Channel.objects.create(name="Someone else", channel_number=501)
    r = p.run("sync", {}, {"settings": {"station_table": [row(number="501")]}})
    assert r["status"] == "partial" and "taken by" in r["message"] and r["warnings"]


def test_validate_runs_in_the_background_with_progress(plugin, monkeypatch):
    p, _ = plugin

    def fake_station(st, cache_dir):
        rep = validate.StationReport(st)
        if st.name == "B":
            rep.problem("stream unreachable (test)")
        return rep
    monkeypatch.setattr(validate, "validate_station", fake_station)
    seen = []
    r = p.run("validate", {}, {"settings": {"station_table": [row(), row(name="B", url="http://b/x")]},
                               "report_progress": lambda pct, msg: seen.append((pct, msg))})
    assert r["status"] == "partial", r
    assert [s["ok"] for s in r["stations"]] == [True, False]
    assert r["stations"][1]["problems"] == ["stream unreachable (test)"]
    assert any("Checked 2/2" in msg for _, msg in seen)
    assert "[PROBLEM] B" in Path(r["report"]).read_text()
    # the latest report is still where earlier versions put it
    assert (Path(p.data_dir) / "logs" / "validate.log").read_text() == Path(r["report"]).read_text()


def test_validate_budget_grows_with_the_station_count():
    assert validate.budget_for(0) == validate.budget_for(1) == 45
    assert validate.budget_for(8) == 45 and validate.budget_for(9) == 90
    assert validate.budget_for(10_000) == validate.MAX_TOTAL_SECONDS


def test_validate_all_survives_a_failing_check_and_callback(monkeypatch):
    def broken(st, cache_dir):
        raise ValueError("bug")
    monkeypatch.setattr(validate, "validate_station", broken)

    def bad_callback(done, total, rep):
        raise RuntimeError("ui")
    out = validate.validate_all([Station("A", "http://a/x", "Austin")], None, on_done=bad_callback)
    assert not out[0].ok and "ValueError: bug" in out[0].problems[0]
    assert validate.validate_all([], None) == []


def test_a_second_sync_waits_for_the_first(quiet_server):
    p, _ = quiet_server
    with p._action_lock("channels") as free:
        assert free
        r = p.run("sync", {}, {"settings": {"station_table": [row()]}})
        assert r["status"] == "error" and "still running" in r["message"]
        r = p.run("remove_channels", {}, {"settings": {}})
        assert "still running" in r["message"]
        # Validate and Status don't touch channels, so they still run.
        assert "still running" not in p.run("status", {}, {"settings": {}})["message"]
    assert p.run("sync", {}, {"settings": {"station_table": [row()]}})["status"] == "ok"


def test_reports_are_private_timestamped_and_pruned(tmp_path):
    other = tmp_path / "reports" / "notes.txt"
    other.parent.mkdir(parents=True)
    other.write_text("mine")
    paths = [reports.save(str(tmp_path), "sync", f"run {i}", now=1_700_000_000) for i in range(25)]
    assert len(set(paths)) == 25                              # same second, no overwrite
    kept = reports.listing(tmp_path / "reports", "sync")
    assert len(kept) == reports.KEEP and kept[-1] == paths[-1]
    assert other.read_text() == "mine"                        # never touches other files
    assert stat.S_IMODE(os.stat(paths[-1]).st_mode) == 0o600
    assert (tmp_path / "logs" / "sync.log").read_text() == "run 24\n"
    assert reports.latest(str(tmp_path), "sync") == paths[-1]
    assert reports.latest(str(tmp_path), "validate") is None
    with pytest.raises(ValueError):
        reports.save(str(tmp_path), "../../etc/passwd", "x")


# ---- 3. the Advanced section --------------------------------------------------
def test_advanced_section_holds_the_technical_settings():
    fields = plugin_mod.Plugin.fields
    section = next(f for f in fields if f["type"] == "section")
    assert section == {"id": "advanced", "label": "Advanced", "type": "section", "collapsed": True}
    inside = {f["id"] for f in fields if f.get("section") == "advanced"}
    assert inside == {"resolution", "video_kbps", "video_encoder", "port"}
    sections = {f["id"] for f in fields if f["type"] == "section"}
    assert all(f["section"] in sections for f in fields if "section" in f)


def test_manifest_matches_the_class_and_needs_version_1_features():
    assert MANIFEST["fields"] == plugin_mod.Plugin.fields
    assert MANIFEST["actions"] == plugin_mod.Plugin.actions
    # table, section and "collapsed" need manifest_version >= 1 (we are 2)
    assert MANIFEST["manifest_version"] >= 1
    assert "stations" not in {f["id"] for f in MANIFEST["fields"]}   # the old text box is gone


# ---- 4. refreshing Dispatcharr's pages ------------------------------------------
@pytest.fixture
def sent(monkeypatch):
    events = []
    core = types.ModuleType("core")
    utils = types.ModuleType("core.utils")
    utils.send_websocket_update = lambda group, kind, data: events.append(data)
    core.utils = utils
    monkeypatch.setitem(sys.modules, "core", core)
    monkeypatch.setitem(sys.modules, "core.utils", utils)
    return events


def test_sync_refreshes_the_plugin_page_and_channel_list(quiet_server, sent):
    p, _ = quiet_server
    r = p.run("sync", {}, {"settings": {"station_table": [row(), row(name="B", url="http://b/x")]}})
    assert r["_dispatcharr"] == {"refresh": "ui"}
    assert {"type": "channels_created", "count": 2} in sent
    sent.clear()
    r = p.run("sync", {}, {"settings": {"station_table": [row()]}})      # B removed
    assert any(e["type"] == "epg_match" and "changed 1" in e["message"] for e in sent), sent
    sent.clear()
    p.run("sync", {}, {"settings": {"station_table": [row()]}})          # nothing changed
    assert [e["type"] for e in sent] == ["epg_sources_changed", "epg_data_created"]


def test_remove_refreshes_the_plugin_page_and_channel_list(quiet_server, sent):
    p, _ = quiet_server
    p.run("sync", {}, {"settings": {"station_table": [row()]}})
    sent.clear()
    r = p.run("remove_channels", {}, {"settings": {}})
    assert r["status"] == "ok" and r["_dispatcharr"] == {"refresh": "ui"}
    assert any(e["type"] == "epg_match" and "removed 1" in e["message"] for e in sent), sent


def test_failed_actions_ask_for_no_refresh(quiet_server):
    p, _ = quiet_server
    r = p.run("sync", {}, {"settings": {"station_table": [row(url="x")]}})
    assert r["status"] == "error" and "_dispatcharr" not in r


def test_clip_keeps_the_tail():
    msg = plugin_mod.Plugin._clip("x" * 400, tail=" Report: /data/r.txt")
    assert len(msg) == 280 and msg.endswith(" Report: /data/r.txt") and "…" in msg
    assert plugin_mod.Plugin._clip("short") == "short"


# ---- John's real station list (2026-11-24) ------------------------------------
def test_johns_station_list_moves_into_the_table_exactly():
    """A real list, typed by hand: a missing space before a pipe (K106.3), a tab
    before a URL (100.3 the Q), .pls and .m3u8 links, themes and numbers 2000-2018."""
    text = (ROOT / "tests" / "fixtures" / "johns_stations.txt").read_text(encoding="utf-8")
    assert "K106.3| http" in text and "| \thttps://stream.jpbgdigital" in text   # quirks kept
    m = rows_from_text(text)
    assert not m.notes and len(m.rows) == 19
    assert [r["number"] for r in m.rows] == [str(n) for n in range(2000, 2019)]
    by_name = {r["name"]: r for r in m.rows}
    assert by_name["K106.3"]["url"] == "http://live.leanstream.co/CHKSFM"
    assert by_name["100.3 the Q"]["url"] == "https://stream.jpbgdigital.com/CKKQ/HEAAC/48k/playlist.m3u8"
    assert by_name["Deep Space One"]["url"] == "https://somafm.com/deepspaceone130.pls"
    assert by_name["Groove Salad"]["city"] == "Theme: Mountains"
    assert not any(r["skip"] or r["id"] or r["logo"] for r in m.rows)
    # every cell is clean text, with nothing left over from the tab or the pipes
    for r in m.rows:
        for col in ("name", "url", "city", "number"):
            assert r[col] == r[col].strip() and "|" not in r[col] and "\t" not in r[col]

    # The table gives the same 19 stations, with the same ids (so the same
    # channels) and numbers, as the text box did in v0.5.3.
    old, new = parse_stations(text), parse_rows(m.rows)
    assert not old.errors and not new.errors, (old.errors, new.errors)
    assert len(new.stations) == 19
    assert [(s.station_id, s.name, s.stream_url, s.city, s.channel_number) for s in new.stations] == \
        [(s.station_id, s.name, s.stream_url, s.city, s.channel_number) for s in old.stations]

    # and Dispatcharr's settings form opens with them already in the table
    fields = plugin_mod.Plugin().get_fields({"settings": {"stations": text}})
    table = next(f for f in fields if f["id"] == "station_table")
    assert table["default"] == m.rows


def test_johns_station_list_syncs_to_19_channels(quiet_server):
    p, models = quiet_server
    text = (ROOT / "tests" / "fixtures" / "johns_stations.txt").read_text(encoding="utf-8")
    rows = plugin_mod.Plugin().get_fields({"settings": {"stations": text}})
    rows = next(f for f in rows if f["id"] == "station_table")["default"]
    r = p.run("sync", {}, {"settings": {"stations": text, "station_table": rows}})
    assert r["status"] == "ok", r
    numbers = sorted(c.channel_number for c in models.Channel.objects.all())
    assert numbers == list(range(2000, 2019))

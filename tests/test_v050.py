"""v0.5.0: guide entries, channel logos, station ids that survive a rename,
and the GPU encoder option."""
import json
import re
import shutil

import pytest

import plugin as plugin_mod
from rs import encoder
from rs.channels import (GUIDE_NAME, GUIDE_PROPERTIES, ChannelSync, guide_about,
                         stream_name)
from rs.config import RenderSettings, Station, parse_stations
from rs.service import load_config

from fake_orm import make_models

URL = lambda st: f"http://127.0.0.1:5980/radio/{st.station_id}.ts"
LOGO = "/data/logos/rad-streams.png"


def stations(text):
    r = parse_stations(text)
    assert not r.errors, r.errors
    return r.stations


def js_pattern(p):
    """Dispatcharr turns JavaScript-style (?<name>) groups into Python ones the same way."""
    return re.sub(r"\(\?<(?![=!])([^>]+)>", r"(?P<\1>", p)


# ---- station list: id= and logo= ---------------------------------------------
def test_id_column_keeps_the_station_id_through_a_rename():
    old = stations("KEXP | http://a/s | Seattle, WA | 501")[0]
    same = stations("KEXP | http://a/s | Seattle, WA | 501 | id=KEXP")[0]
    renamed = stations("KEXP Seattle | http://a/s | Seattle, WA | 501 | id=KEXP")[0]
    assert old.station_id == same.station_id == renamed.station_id
    assert renamed.name == "KEXP Seattle" and renamed.channel_number == 501


def test_options_in_any_order_and_without_a_channel_number():
    a, b, c = stations(
        "A | http://a/s | Austin | logo=https://x/a.png | id=Old A\n"
        "B | http://b/s | Boston | | id=b\n"
        "C | http://c/s | Chicago | 7 | LOGO = https://x/c.png?size=1&v=2")
    assert (a.key, a.logo_url, a.channel_number) == ("Old A", "https://x/a.png", None)
    assert (b.key, b.channel_number) == ("b", None)
    assert c.logo_url == "https://x/c.png?size=1&v=2" and c.channel_number == 7


@pytest.mark.parametrize("line, why", [
    ("A | http://a/s | Austin | 1 | colour=red", "should be id="),
    ("A | http://a/s | Austin | 1 | 2", "should be id="),
    ("A | http://a/s | Austin | id=x | id=y", "given twice"),
    ("A | http://a/s | Austin | id=", "id= must be"),
    ("A | http://a/s | Austin | logo=javascript:alert(1)", "logo= must be"),
    ("A | http://a/s | Austin | logo=file:///etc/passwd", "logo= must be"),
    ("A | http://a/s | Austin | logo=https://x/a b.png", "logo= must be"),
    ("A | http://a/s | Austin | inf", "not a positive number"),
    ("A | http://a/s | Austin | nan", "not a positive number"),
])
def test_bad_options_are_reported(line, why):
    r = parse_stations(line)
    assert not r.stations and why in r.errors[0]


def test_duplicate_ids_are_skipped():
    r = parse_stations("A | http://a/s | Austin | id=x\nB | http://b/s | Boston | id=X")
    assert [s.name for s in r.stations] == ["A"] and "duplicate id 'X'" in r.errors[0]
    # a name that equals another line's id is the same station too
    r = parse_stations("A | http://a/s | Austin | id=B\nB | http://b/s | Boston")
    assert len(r.stations) == 1


def test_service_config_round_trips_id_and_logo(tmp_path, monkeypatch):
    monkeypatch.setattr(plugin_mod, "_data_dir", lambda: tmp_path / "data")
    monkeypatch.setattr(plugin_mod.Plugin, "_schedule_autostart", lambda self: None)
    p = plugin_mod.Plugin()
    sts = stations("New | http://a/s | Austin | 3 | id=Old | logo=https://x/l.png")
    p._write_service_config(sts, RenderSettings(video_encoder="nvenc"), 5980, desired=False)
    loaded, settings, _ = load_config(str(p.config_path))
    assert loaded[0].station_id == sts[0].station_id and loaded[0].logo_url == "https://x/l.png"
    assert settings.video_encoder == "nvenc"
    # a config from an older version (no id/logo keys) still loads
    data = json.loads(p.config_path.read_text())
    for s in data["stations"]:
        s.pop("id"), s.pop("logo_url")
    data["render"].pop("video_encoder")
    p.config_path.write_text(json.dumps(data))
    loaded, settings, _ = load_config(str(p.config_path))
    assert loaded[0].key is None and settings.video_encoder == "software"


# ---- guide -------------------------------------------------------------------
def test_guide_pattern_reads_every_stream_name():
    pat = re.compile(js_pattern(GUIDE_PROPERTIES["title_pattern"]))
    for st, about in ((Station("KEXP", "http://a", "Seattle, WA"), "Live from Seattle, WA"),
                      (Station("Soma: {Groove}", "http://a", "Theme: Nature"), "Nature photos")):
        m = pat.search(stream_name(st))
        assert m and m.group("station") == st.name and m.group("about") == about == guide_about(st)


def test_sync_creates_one_guide_and_points_every_channel_at_it(tmp_path):
    m = make_models()
    sync = ChannelSync(m, str(tmp_path / "channels.json"))
    sync.sync(stations("A | http://a/s | Austin, TX\nB | http://b/s | Theme: Space"), URL)
    src = m.EPGSource.objects.get(name=GUIDE_NAME)
    assert src.source_type == "dummy" and src.custom_properties == GUIDE_PROPERTIES
    epg = m.EPGData.objects.get(epg_source=src)
    assert all(c.epg_data_id == epg.id for c in m.Channel.objects.all())
    assert {s.name for s in m.Stream.objects.all()} == {
        "Rad Streams: A | Live from Austin, TX", "Rad Streams: B | Space photos"}

    # a second Sync reuses it, and an existing (pre-0.5) channel without a guide gets it
    ch = m.Channel.objects.filter(name="A").first()
    ch.epg_data, ch.epg_data_id = None, None
    res = sync.sync(stations("A | http://a/s | Austin, TX\nB | http://b/s | Theme: Space"), URL)
    assert res.updated == ["A"] and res.unchanged == ["B"]
    assert m.EPGSource.objects.all().count() == 1 and m.EPGData.objects.all().count() == 1
    assert ch.epg_data_id == epg.id

    sync.remove_all()
    assert m.EPGSource.objects.all().count() == 0 and m.EPGData.objects.all().count() == 0


def test_hand_picked_guide_and_logo_are_kept(tmp_path):
    m = make_models()
    sync = ChannelSync(m, str(tmp_path / "channels.json"), default_logo=LOGO)
    text = "A | http://a/s | Austin, TX"
    sync.sync(stations(text), URL)
    ch = m.Channel.objects.first()
    other_epg = m.EPGData.objects.create(tvg_id="kexp.us", name="Real guide")
    other_logo = m.Logo.objects.create(url="https://x/mine.png", name="Mine")
    ch.epg_data, ch.logo = other_epg, other_logo
    ch.save()
    res = sync.sync(stations(text), URL)
    assert res.unchanged == ["A"]
    assert ch.epg_data_id == other_epg.id and ch.logo_id == other_logo.id


def test_someone_elses_epg_source_is_left_alone(tmp_path):
    m = make_models()
    theirs = m.EPGSource.objects.create(name=GUIDE_NAME, source_type="xmltv",
                                        custom_properties={})
    sync = ChannelSync(m, str(tmp_path / "channels.json"))
    res = sync.sync(stations("A | http://a/s | Austin, TX"), URL)
    assert res.created == ["A"] and any("guide entries were skipped" in w for w in res.warnings)
    assert getattr(m.Channel.objects.first(), "epg_data_id", None) is None
    assert theirs.custom_properties == {}
    sync.remove_all()
    assert m.EPGSource.objects.first() is theirs


def test_guide_and_logo_are_optional(tmp_path):
    m = make_models()
    m.EPGSource = m.EPGData = m.Logo = None      # an older Dispatcharr
    res = ChannelSync(m, str(tmp_path / "c.json"), default_logo=LOGO).sync(
        stations("A | http://a/s | Austin, TX"), URL)
    assert res.created == ["A"] and not res.warnings


# ---- logos -------------------------------------------------------------------
def test_logos_default_and_per_station(tmp_path):
    m = make_models()
    sync = ChannelSync(m, str(tmp_path / "channels.json"), default_logo=LOGO)
    sync.sync(stations("A | http://a/s | Austin\nB | http://b/s | Boston | logo=https://x/b.png\n"
                       "C | http://c/s | Chicago"), URL)
    ch = {c.name: c for c in m.Channel.objects.all()}
    default = m.Logo.objects.get(url=LOGO)
    assert ch["A"].logo_id == ch["C"].logo_id == default.id    # one shared Logo row
    assert m.Logo.objects.get(url="https://x/b.png").id == ch["B"].logo_id
    assert m.Logo.objects.all().count() == 2

    # B drops its logo= and goes back to the default, since Sync set the old one
    sync.sync(stations("A | http://a/s | Austin\nB | http://b/s | Boston\nC | http://c/s | Chicago"), URL)
    assert ch["B"].logo_id == default.id


def test_plugin_copies_the_bundled_logo_only_under_data(tmp_path, monkeypatch):
    monkeypatch.setattr(plugin_mod.Plugin, "_schedule_autostart", lambda self: None)
    monkeypatch.setattr(plugin_mod, "_data_dir", lambda: tmp_path / "data")
    assert plugin_mod.Plugin()._default_logo() is None   # Dispatcharr can't serve it from here


# ---- rename keeps the channel ------------------------------------------------
def test_rename_with_id_updates_the_same_channel(tmp_path):
    m = make_models()
    sync = ChannelSync(m, str(tmp_path / "channels.json"))
    sync.sync(stations("KEXP | http://a/s | Seattle, WA | 501"), URL)
    before = m.Channel.objects.first()
    res = sync.sync(stations("KEXP Seattle | http://a/s | Seattle, WA | 501 | id=KEXP"), URL)
    assert res.updated == ["KEXP Seattle"] and not res.created and not res.removed
    assert m.Channel.objects.all().count() == 1 and m.Channel.objects.first() is before
    assert before.name == "KEXP Seattle" and before.channel_number == 501


# ---- hardware encoding -------------------------------------------------------
def test_encoder_args_per_choice(monkeypatch):
    monkeypatch.setattr(encoder, "render_device", lambda: "/dev/dri/renderD128")
    cmd = encoder.build_command(1280, 720, 4, 25, 3000, 128, 5, "-", video_encoder="vaapi")
    assert cmd[cmd.index("-vaapi_device") + 1] == "/dev/dri/renderD128"
    assert cmd.index("-vaapi_device") < cmd.index("-i")          # device opens before inputs
    assert cmd[cmd.index("-c:v") + 1] == "h264_vaapi" and cmd[cmd.index("-vf") + 1].endswith(
        "format=nv12,hwupload")
    for name, codec in (("nvenc", "h264_nvenc"), ("qsv", "h264_qsv"), ("software", "libx264")):
        cmd = encoder.build_command(1280, 720, 4, 25, 3000, 128, 5, "-", video_encoder=name)
        assert cmd[cmd.index("-c:v") + 1] == codec
        assert cmd[cmd.index("-g") + 1] == "50" and cmd[cmd.index("-b:v") + 1] == "3000k"
    # VAAPI with no render node falls back to the software arguments
    monkeypatch.setattr(encoder, "render_device", lambda: None)
    cmd = encoder.build_command(1280, 720, 4, 25, 3000, 128, 5, "-", video_encoder="vaapi")
    assert cmd[cmd.index("-c:v") + 1] == "libx264" and "-vaapi_device" not in cmd


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="needs ffmpeg")
def test_probe_command_works_with_software():
    assert encoder.probe("software") is None


def test_resolve_falls_back_to_software_and_caches(monkeypatch):
    calls = []
    monkeypatch.setattr(encoder, "_resolved", {})
    monkeypatch.setattr(encoder, "probe", lambda enc, ff="ffmpeg": calls.append(enc) or "no GPU")
    assert encoder.cached("nvenc") is None
    assert encoder.resolve("nvenc") == ("software", "no GPU")
    assert encoder.resolve("nvenc") == ("software", "no GPU") and calls == ["nvenc"]
    assert encoder.cached("nvenc") == ("software", "no GPU")
    monkeypatch.setattr(encoder, "probe", lambda enc, ff="ffmpeg": None)
    assert encoder.resolve("qsv") == ("qsv", None)
    assert encoder.resolve("bogus") == ("software", None)


def test_probe_reports_a_missing_ffmpeg():
    assert encoder.probe("nvenc", ffmpeg="/nonexistent/ffmpeg") == "ffmpeg not found"


def test_plugin_validates_the_encoder_setting(tmp_path, monkeypatch):
    monkeypatch.setattr(plugin_mod, "_data_dir", lambda: tmp_path / "data")
    monkeypatch.setattr(plugin_mod.Plugin, "_schedule_autostart", lambda self: None)
    p = plugin_mod.Plugin()
    assert p._render_settings({"video_encoder": "nvenc"}).video_encoder == "nvenc"
    assert p._render_settings({"video_encoder": "rm -rf /"}).video_encoder == "software"
    assert p._render_settings({}).video_encoder == "software"


def test_plugin_json_matches_the_plugin():
    data = json.load(open(plugin_mod.Path(plugin_mod.__file__).with_name("plugin.json")))
    assert data["version"] == plugin_mod.Plugin.version
    assert data["fields"] == plugin_mod.Plugin.fields
    assert data["actions"] == plugin_mod.Plugin.actions


def test_probe_reason_is_the_first_error(monkeypatch):
    class Done:
        returncode = 1
        stderr = (b"[h264_nvenc @ 0x55dc] Cannot load libcuda.so.1\n"
                  b"[vost#0:0/h264_nvenc @ 0x55] Error while opening encoder\n")
    monkeypatch.setattr(encoder.subprocess, "run", lambda *a, **k: Done())
    assert encoder.probe("nvenc") == "Cannot load libcuda.so.1"

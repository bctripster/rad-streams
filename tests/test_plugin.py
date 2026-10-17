"""Plugin shell: channel sync against a fake ORM, and the service lifecycle."""
import json
import os
import time
import urllib.request

import pytest

import plugin as plugin_mod
from rs.channels import GROUP_NAME, ChannelSync
from rs.config import parse_stations

from fake_orm import make_models
from fakes import FakeIcyServer, make_tone_mp3

URL = lambda st: f"http://127.0.0.1:5980/radio/{st.station_id}.ts"


def stations(text):
    return parse_stations(text).stations


def test_sync_create_update_remove(tmp_path):
    m = make_models()
    sync = ChannelSync(m, str(tmp_path / "channels.json"))
    res = sync.sync(stations("A FM | http://a/s | Austin, TX | 501\nB FM | http://b/s | Boston, MA"), URL)
    assert sorted(res.created) == ["A FM", "B FM"]
    chans = {c.name: c for c in m.Channel.objects.all()}
    assert chans["A FM"].channel_number == 501 and chans["B FM"].channel_number == 1000
    group = m.ChannelGroup.objects.get(name=GROUP_NAME)
    assert all(c.channel_group is group for c in chans.values())
    proxy = m.StreamProfile.objects.get(name="Proxy")
    assert all(c.stream_profile_id == proxy.id for c in chans.values())
    assert m.Stream.objects.all().count() == 2 and m.ChannelStream.objects.all().count() == 2

    # same list again: nothing changes
    res = sync.sync(stations("A FM | http://a/s | Austin, TX | 501\nB FM | http://b/s | Boston, MA"), URL)
    assert sorted(res.unchanged) == ["A FM", "B FM"]

    # renumber A, drop B, add C
    res = sync.sync(stations("A FM | http://a/other | Austin, TX | 777\nC FM | http://c/s | Chicago"), URL)
    assert res.updated == ["A FM"] and res.created == ["C FM"] and res.removed == ["B FM"]
    names = {c.name: c.channel_number for c in m.Channel.objects.all()}
    assert names == {"A FM": 777, "C FM": 1000}
    assert m.Stream.objects.all().count() == 2

    assert sync.remove_all() == 2
    assert m.Channel.objects.all().count() == 0 and m.Stream.objects.all().count() == 0


def test_sync_number_clash_warns(tmp_path):
    m = make_models()
    m.Channel.objects.create(name="Someone else", channel_number=501)
    sync = ChannelSync(m, str(tmp_path / "channels.json"))
    res = sync.sync(stations("A FM | http://a/s | Austin | 501"), URL)
    assert res.warnings and "taken by 'Someone else'" in res.warnings[0]
    a = m.Channel.objects.get(name="A FM")
    assert a.channel_number == 1000


def test_sync_recovers_stream_when_mapping_lost(tmp_path):
    m = make_models()
    path = tmp_path / "channels.json"
    ChannelSync(m, str(path)).sync(stations("A FM | http://a/s | Austin"), URL)
    path.unlink()
    ChannelSync(m, str(path)).sync(stations("A FM | http://a/s | Austin"), URL)
    assert m.Stream.objects.all().count() == 1   # found again by its URL
    assert m.Channel.objects.all().count() == 1  # and its channel through the stream link


def test_sync_failure_part_way_keeps_mapping(tmp_path):
    m = make_models()
    path = tmp_path / "channels.json"
    two = stations("A FM | http://a/s | Austin\nB FM | http://b/s | Boston")
    real_create = m.Channel.objects.create
    calls = []

    def flaky(**kw):
        calls.append(1)
        if len(calls) == 2:
            raise RuntimeError("database hiccup")
        return real_create(**kw)

    m.Channel.objects.create = flaky
    with pytest.raises(RuntimeError):
        ChannelSync(m, str(path)).sync(two, URL)
    m.Channel.objects.create = real_create
    ChannelSync(m, str(path)).sync(two, URL)
    assert m.Channel.objects.all().count() == 2   # A wasn't duplicated


@pytest.fixture
def plugin(tmp_path, monkeypatch):
    monkeypatch.setattr(plugin_mod, "_data_dir", lambda: tmp_path / "data")
    monkeypatch.setattr(plugin_mod.Plugin, "_schedule_autostart", lambda self: None)
    models = make_models()
    monkeypatch.setattr(plugin_mod.Plugin, "_models", staticmethod(lambda: models))
    p = plugin_mod.Plugin()
    yield p, models
    p._stop_service(keep_desired=False)


def free_port():
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_plugin_sync_starts_service_and_serves(plugin, tmp_path):
    p, models = plugin
    icy = FakeIcyServer(make_tone_mp3(str(tmp_path / "t.mp3")), ["A - B"])
    port = free_port()
    settings = {"stations": f"Test FM | {icy.url} | Nowhere Special\nbad line", "port": port,
                "resolution": "854x480", "units": "C", "video_kbps": 800}
    try:
        r = p.run("sync", {}, {"settings": settings})
        assert r["status"] == "ok", r
        assert "1 created" in r["message"] and "server started" in r["message"]
        assert "warning" in r["message"] and len(r["message"]) <= 280
        cfg = json.loads((tmp_path / "data" / "service.json").read_text())
        assert cfg["render"]["units"] == "C" and cfg["port"] == port

        ch = models.Channel.objects.first()
        url = models.Stream.objects.first().url
        assert url.startswith(f"http://127.0.0.1:{port}/radio/") and ch.name == "Test FM"

        # the service comes up and the route streams MPEG-TS
        deadline = time.time() + 15
        data = b""
        while time.time() < deadline and not data:
            try:
                with urllib.request.urlopen(url, timeout=10) as resp:
                    data = resp.read(188 * 100)
            except OSError:
                time.sleep(0.5)
        assert data[:1] == b"\x47", "expected MPEG-TS sync byte"

        # a second sync reuses the running service
        r = p.run("sync", {}, {"settings": settings})
        assert "server running" in r["message"]
        time.sleep(6)   # let the status file refresh
        assert "Server running on port" in p.run("status", {}, {"settings": settings})["message"]

        # after the plugin's code is updated, Sync replaces the old server
        old_pid = p._service_pid()
        monkey_id = "updatedcode1"
        orig = plugin_mod.Plugin._code_id
        plugin_mod.Plugin._code_id = lambda self: monkey_id
        try:
            r = p.run("sync", {}, {"settings": settings})
            assert "server started" in r["message"], r
            assert p._service_pid() != old_pid and p._running_code_id() == monkey_id
        finally:
            plugin_mod.Plugin._code_id = orig

        r = p.run("stop_server", {}, {"settings": settings})
        assert r["message"] == "Server stopped."
        assert "not running" in p.run("status", {}, {"settings": settings})["message"]
    finally:
        icy.close()


def test_plugin_sync_rejects_empty(plugin):
    p, _ = plugin
    r = p.run("sync", {}, {"settings": {"stations": "nonsense"}})
    assert r["status"] == "error" and "No valid stations" in r["message"]

"""Dispatcharr 0.34 plugin API: capabilities, leader hooks, and its sandbox.

Dispatcharr 0.34 enforces a manifest_version 2 plugin's declared capabilities
inside the plugin's own modules: open() for writing outside the plugin's code
and data directories needs filesystem_write, requests/urllib/socket.connect
need outbound_network, socket bind/listen need network_listener and the
subprocess functions need subprocess. v0.5.2 declared only three of these, so
Stop Server failed with "requires the 'filesystem_write' capability" (it opens
/data/rad_streams/.service.lock for writing).

The last tests load the plugin under Dispatcharr's real sandbox code. They need
a Dispatcharr checkout and are skipped without one:

    DISPATCHARR_SRC=/path/to/Dispatcharr python3 -m pytest tests/test_dispatcharr_034.py
"""
import importlib
import importlib.machinery
import importlib.util
import json
import os
import sys
import threading
import types
from pathlib import Path

import pytest

import plugin as plugin_mod
from fake_orm import make_models

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = json.loads((ROOT / "plugin.json").read_text(encoding="utf-8"))
OLD_CAPS = ["subprocess", "persistent_service", "network_listener"]   # v0.5.2


# ---- manifest --------------------------------------------------------------
def test_manifest_declares_every_capability_the_plugin_uses():
    assert MANIFEST["manifest_version"] == 2
    assert set(MANIFEST["capabilities"]) == {
        "background_tasks",     # Sync and Validate are async actions (v0.6.0)
        "filesystem_write",     # /data/rad_streams state + /data/logos logo
        "network_listener",     # port check binds 127.0.0.1; the server listens
        "outbound_network",     # Validate fetches streams; downloads in-process
        "persistent_service",   # on_leader_acquired auto-start
        "subprocess",           # launches rs.service
    }
    assert len(MANIFEST["capabilities"]) == len(set(MANIFEST["capabilities"]))


def test_manifest_and_class_versions_match():
    assert MANIFEST["version"] == plugin_mod.Plugin.version


def test_every_action_is_declared_in_the_manifest():
    # Version 2 refuses to run an action id the manifest doesn't list.
    declared = {a["id"] for a in MANIFEST["actions"]}
    assert declared == {a["id"] for a in plugin_mod.Plugin.actions}


# ---- leader election instead of an __init__ thread -------------------------
@pytest.fixture
def bare_plugin(tmp_path, monkeypatch):
    monkeypatch.setattr(plugin_mod, "_data_dir", lambda: tmp_path / "data")
    return plugin_mod.Plugin()


def test_init_starts_no_threads(tmp_path, monkeypatch):
    monkeypatch.setattr(plugin_mod, "_data_dir", lambda: tmp_path / "data")
    before = {t.ident for t in threading.enumerate()}
    plugin_mod.Plugin()
    assert {t.ident for t in threading.enumerate()} == before


def test_leader_acquired_starts_one_named_service(bare_plugin):
    calls = []
    bare_plugin.on_leader_acquired({"start_service": lambda target, name=None: calls.append((target, name))})
    assert len(calls) == 1 and calls[0][1] == "autostart"
    assert calls[0][0] == bare_plugin._leader_service


def test_leader_acquired_does_nothing_inside_our_own_server(bare_plugin, monkeypatch):
    monkeypatch.setenv("RS_RUN_TOKEN", "x")
    calls = []
    bare_plugin.on_leader_acquired({"start_service": lambda *a, **k: calls.append(a)})
    assert calls == []


def test_leader_lost_is_a_no_op(bare_plugin):
    assert bare_plugin.on_leader_lost({}) is None


def test_leader_service_stopped_during_delay_skips_autostart(bare_plugin, monkeypatch):
    ran = []
    monkeypatch.setattr(bare_plugin, "_autostart", lambda: ran.append(1))
    stop = threading.Event()
    stop.set()
    bare_plugin._leader_service(stop)   # returns at once
    assert ran == []


def test_leader_service_autostarts_once_then_waits_for_stop(bare_plugin, monkeypatch):
    monkeypatch.setattr(plugin_mod, "_AUTOSTART_DELAY_SEC", 0)
    ran = []
    monkeypatch.setattr(bare_plugin, "_autostart", lambda: ran.append(1))
    stop = threading.Event()
    t = threading.Thread(target=bare_plugin._leader_service, args=(stop,), daemon=True)
    t.start()
    t.join(0.5)
    # Still alive: returning would count as an unexpected exit and cost the lease.
    assert t.is_alive() and ran == [1]
    stop.set()
    t.join(5)
    assert not t.is_alive() and ran == [1]


def test_autostart_only_when_server_was_running(bare_plugin, monkeypatch):
    started = []
    monkeypatch.setattr(bare_plugin, "_ensure_service", lambda: started.append(1) or True)
    bare_plugin._autostart()                       # no config yet
    bare_plugin.data_dir.mkdir(parents=True)
    bare_plugin.config_path.write_text(json.dumps({"desired_running": False, "stations": [{}]}))
    bare_plugin._autostart()
    assert started == []
    bare_plugin.config_path.write_text(json.dumps({"desired_running": True, "stations": [{}]}))
    bare_plugin._autostart()
    assert started == [1]


def test_autostart_logs_and_swallows_errors(bare_plugin, monkeypatch):
    bare_plugin.data_dir.mkdir(parents=True)
    bare_plugin.config_path.write_text(json.dumps({"desired_running": True, "stations": [{}]}))

    def boom():
        raise OSError("disk full")
    monkeypatch.setattr(bare_plugin, "_ensure_service", boom)
    bare_plugin._autostart()   # must not raise into Dispatcharr's service thread


# ---- under Dispatcharr's real sandbox --------------------------------------
DSP = os.environ.get("DISPATCHARR_SRC")
needs_dispatcharr = pytest.mark.skipif(
    not (DSP and (Path(DSP) / "apps" / "plugins" / "sandbox.py").is_file()),
    reason="set DISPATCHARR_SRC to a Dispatcharr checkout to run the sandbox tests")

KEY = "rad_streams"


@pytest.fixture
def sandboxed(tmp_path, monkeypatch):
    """Import plugin.py and rs/ the way Dispatcharr 0.34 does, with guarded builtins.

    Yields a function (capabilities) -> (Plugin instance, running_as_plugin).
    sys.modules and the import caches are restored afterwards so the other
    tests keep their unguarded copies.
    """
    pkg_dir = Path(DSP) / "apps" / "plugins"
    saved_modules = dict(sys.modules)
    saved_cache = dict(sys.path_importer_cache)

    host = types.ModuleType("_dsp_plugins")
    host.__path__ = [str(pkg_dir)]
    sys.modules["_dsp_plugins"] = host
    # context.plugin_has_capability imports .loader (Django) before it checks
    # the capability snapshot it was given; the snapshot is all it needs here.
    loader_stub = types.ModuleType("_dsp_plugins.loader")
    loader_stub.PluginManager = None
    sys.modules["_dsp_plugins.loader"] = loader_stub
    sandbox = importlib.import_module("_dsp_plugins.sandbox")
    ctx = importlib.import_module("_dsp_plugins.context")

    state = {}

    class GuardedLoader(importlib.machinery.SourceFileLoader):
        # Same as Dispatcharr's _PluginSourceLoader.exec_module.
        def exec_module(self, module):
            module.__builtins__ = sandbox.plugin_builtins(KEY, str(ROOT))
            with ctx.running_as_plugin(KEY, capabilities=state["caps"], manifest_schema_version=2):
                return super().exec_module(module)

    def load(caps):
        state["caps"] = list(caps)
        for name in [n for n in sys.modules if n in ("plugin", "rs") or n.startswith(("rs.", "rsbx"))]:
            del sys.modules[name]
        for directory in (ROOT, ROOT / "rs"):
            sys.path_importer_cache[str(directory)] = importlib.machinery.FileFinder(
                str(directory), (GuardedLoader, importlib.machinery.SOURCE_SUFFIXES))
        parent = types.ModuleType("rsbx")
        parent.__path__ = [str(ROOT)]
        sys.modules["rsbx"] = parent
        spec = importlib.util.spec_from_file_location(
            "rsbx.plugin", ROOT / "plugin.py", loader=GuardedLoader("rsbx.plugin", str(ROOT / "plugin.py")))
        mod = importlib.util.module_from_spec(spec)
        sys.modules["rsbx.plugin"] = mod
        spec.loader.exec_module(mod)
        # /data stands in as tmp_path: outside the plugin's code and data dirs.
        monkeypatch.setattr(mod, "_data_dir", lambda: tmp_path / "data")
        models = make_models()
        monkeypatch.setattr(mod.Plugin, "_models", staticmethod(lambda: models))
        with ctx.running_as_plugin(KEY, capabilities=state["caps"], manifest_schema_version=2):
            inst = mod.Plugin()

        def as_plugin():
            return ctx.running_as_plugin(KEY, capabilities=state["caps"], manifest_schema_version=2)
        state["inst"] = inst
        return inst, as_plugin

    try:
        yield load
    finally:
        inst = state.get("inst")
        if inst is not None:
            with ctx.running_as_plugin(KEY, capabilities=MANIFEST["capabilities"], manifest_schema_version=2):
                inst._stop_service(keep_desired=False)
        sys.modules.clear()
        sys.modules.update(saved_modules)
        sys.path_importer_cache.clear()
        sys.path_importer_cache.update(saved_cache)


@needs_dispatcharr
def test_v052_capabilities_reproduce_the_stop_server_error(sandboxed):
    p, as_plugin = sandboxed(OLD_CAPS)
    with as_plugin():
        r = p.run("stop_server", {}, {"settings": {}})
    assert r["status"] == "error"
    assert "requires the 'filesystem_write' capability" in r["message"]


@needs_dispatcharr
def test_downloads_in_the_plugin_thread_need_outbound_network(sandboxed):
    # Validate's worker threads (ThreadPoolExecutor) don't carry the plugin
    # context, so Dispatcharr 0.34 doesn't gate them yet; a download in the
    # action's own thread is gated, which is why outbound_network is declared.
    for caps, blocked in ((OLD_CAPS + ["filesystem_write"], True), (MANIFEST["capabilities"], False)):
        _, as_plugin = sandboxed(caps)
        validate = sys.modules["rsbx.rs.validate"]
        with as_plugin():
            out = validate.check_stream("http://127.0.0.1:9/x", "A", seconds=0.1)
        assert ("requires the 'outbound_network' capability" in (out["error"] or "")) is blocked, out


@needs_dispatcharr
@pytest.mark.parametrize("layout", ["table", "old text"])
def test_every_action_runs_with_the_declared_capabilities(sandboxed, tmp_path, layout):
    from fakes import FakeIcyServer, make_tone_mp3
    p, as_plugin = sandboxed(MANIFEST["capabilities"])
    icy = FakeIcyServer(make_tone_mp3(str(tmp_path / "t.mp3")), ["A - B"])
    import socket as real_socket
    s = real_socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    settings = {"port": port, "resolution": "854x480", "video_kbps": 800}
    if layout == "table":     # v0.6.0
        settings["station_table"] = [{"name": "Test FM", "url": icy.url, "city": "Theme: Space"}]
    else:                     # a setup saved before v0.6.0
        settings["stations"] = f"Test FM | {icy.url} | Theme: Space"
    progress = []
    results = {}
    with as_plugin():
        for action in ("validate", "sync", "status", "stop_server", "remove_channels"):
            results[action] = p.run(action, {}, {
                "settings": settings,
                "report_progress": lambda pct, msg: progress.append(pct)})
        p.stop({"reason": "disable"})
    for action, r in results.items():
        assert "requires the" not in r["message"], (action, r)
        assert r["status"] in ("ok", "partial"), (action, r)
    assert "server started" in results["sync"]["message"]
    log = (tmp_path / "data" / "logs" / "validate.log").read_text()
    assert "requires the" not in log
    # the saved reports and the progress bar work inside the sandbox too
    assert len(list((tmp_path / "data" / "reports").glob("*.txt"))) == 2
    assert progress and max(progress) <= 99


# ---- the settings form, checked by Dispatcharr's own code (v0.6.0) ----------
def _dispatcharr_form_checks():
    """Dispatcharr's field serializer and its table save check, or None.

    Needs DISPATCHARR_SRC plus Django and Django REST framework installed
    (they aren't in requirements-dev.txt, so CI skips this).
    """
    if not (DSP and (Path(DSP) / "apps" / "plugins" / "serializers.py").is_file()):
        return None
    try:
        import django
        from django.conf import settings as dj
        import rest_framework  # noqa: F401
    except ImportError:
        return None
    import ast
    if not dj.configured:
        dj.configure(INSTALLED_APPS=[], USE_I18N=False)
        django.setup()
    pkg = Path(DSP) / "apps" / "plugins"
    fv = types.ModuleType("_dsp_field_versions")
    exec(compile((pkg / "field_versions.py").read_text(), "field_versions.py", "exec"), fv.__dict__)
    src = (pkg / "serializers.py").read_text()
    src = src.replace("from .field_versions import", "from _dsp_field_versions import")
    src = src.replace("from .models import PluginRepo", "PluginRepo = None")
    src = src[: src.index("class PluginRepoSerializer")]   # needs a real model
    sys.modules["_dsp_field_versions"] = fv
    ser = types.ModuleType("_dsp_serializers")
    exec(compile(src, "serializers.py", "exec"), ser.__dict__)
    tree = ast.parse((pkg / "loader.py").read_text())
    fn = next(n for c in ast.walk(tree) if isinstance(c, ast.ClassDef) for n in c.body
              if isinstance(n, ast.FunctionDef) and n.name == "_validate_table_value")
    ns = {"Dict": dict, "Any": object}
    exec(compile(ast.Module(body=[fn], type_ignores=[]), "loader.py", "exec"), ns)
    return ser.PluginFieldSerializer, lambda field, value: ns["_validate_table_value"](None, field, value)


def test_dispatcharr_accepts_the_form_and_the_migrated_table():
    checks = _dispatcharr_form_checks()
    if checks is None:
        pytest.skip("needs DISPATCHARR_SRC, Django and djangorestframework")
    serializer, validate_table = checks
    old = "KEXP | https://k/x | Seattle | 501\n# KCRW | https://c/x | Santa Monica\nbroken line"
    for fields in (MANIFEST["fields"], plugin_mod.Plugin().get_fields({"settings": {"stations": old}})):
        s = serializer(data=fields, many=True)
        assert s.is_valid(), s.errors
        out = [dict(f) for f in s.validated_data]
        assert [f["id"] for f in out] == [f["id"] for f in fields]   # nothing dropped
        table = next(f for f in out if f["id"] == "station_table")
        validate_table(table, table.get("default"))   # Save with the table untouched
        validate_table(table, [{}, {"name": "A", "number": "", "skip": False}])   # new row, cleared #
        assert next(f for f in out if f["type"] == "section")["collapsed"] is True

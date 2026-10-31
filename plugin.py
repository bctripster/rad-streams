"""Rad Streams: internet radio stations as TV-style Dispatcharr channels.

The plugin itself is thin: it parses the station list, keeps one channel per
station in the "Rad Streams" group, and keeps a small background service
running (``rs/service.py``). That service holds the on-demand HTTP server;
each channel's stream URL points at its route there, and a station renders
only while someone is watching.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Optional

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None

try:  # relative when Dispatcharr imports us as a package, absolute in tests
    from .rs import encoder, net, validate
    from .rs.channels import ChannelSync, Models
    from .rs.config import RESOLUTIONS, VIDEO_ENCODERS, RenderSettings, parse_stations
    from .rs.server import DEFAULT_PORT
except ImportError:
    from rs import encoder, net, validate
    from rs.channels import ChannelSync, Models
    from rs.config import RESOLUTIONS, VIDEO_ENCODERS, RenderSettings, parse_stations
    from rs.server import DEFAULT_PORT

_log = logging.getLogger("plugins.rad_streams")

_AUTOSTART_DELAY_SEC = 20
_AUTOSTART_SCHEDULED = False
_MESSAGE_LIMIT = 280

_STATIONS_HELP = (
    "One station per line: Name | Stream URL | City | Channel # (optional). "
    "Example: KEXP | https://kexp.streamguys1.com/kexp160.aac | Seattle, WA | 501. "
    "Use 'City, ST' or 'City, Country' when a city name is ambiguous. "
    "For photos of a subject instead of a city (no weather), put Theme: and a name in the City column, "
    "e.g. Theme: Space. Built in: Space, Earth, Nature, Ocean, Mountains, Forest, Waterfalls, Desert, "
    "Aurora, Sunsets, Beaches, Lakes, Wildlife, Night Sky; any other word is searched on Wikimedia Commons. "
    "Icecast/Shoutcast, .pls/.m3u and HLS (.m3u8) stream URLs all work. "
    "Optional extra columns: id=<old name> keeps a station's channel when you rename it, "
    "and logo=<image URL> sets its channel logo (default: the Rad Streams logo). "
    "Example: KEXP Seattle | https://kexp.streamguys1.com/kexp160.aac | Seattle, WA | 501 | id=KEXP. "
    "Lines starting with # are ignored. Press Sync Stations after editing."
)


def _data_dir() -> Path:
    for base in ("/data", "/app/data"):
        if os.path.isdir(base) and os.access(base, os.W_OK):
            return Path(base) / "rad_streams"
    # Never write anywhere else; actions report the permission error if /data isn't writable.
    return Path("/data") / "rad_streams"


class Plugin:
    name = "Rad Streams"
    version = "0.5.1"
    description = ("Internet radio stations as TV channels: photos of each station's city, "
                   "the current track and local weather, with the station playing underneath.")

    fields = [
        {"id": "setup_info", "label": "Setup", "type": "info",
         "description": "Enter your stations below, then press Sync Stations. Each station "
                        "becomes a channel in the Rad Streams group. Channels render only "
                        "while someone is watching. Validate checks every stream and city "
                        "and writes a full report to /data/rad_streams/logs/validate.log."},
        {"id": "stations", "label": "Stations", "type": "text", "default": "",
         "help_text": _STATIONS_HELP},
        {"id": "units", "label": "Temperature Units", "type": "select", "default": "F",
         "options": [{"value": "F", "label": "Fahrenheit (°F, mph)"},
                     {"value": "C", "label": "Celsius (°C, km/h)"}]},
        {"id": "resolution", "label": "Resolution", "type": "select", "default": "1920x1080",
         "options": [{"value": k, "label": k} for k in ("3840x2160", "2560x1440", "1920x1080",
                                                        "1280x720", "854x480")]},
        {"id": "video_kbps", "label": "Video Bitrate (kbps)", "type": "number", "default": 3000,
         "min": 500, "max": 20000, "step": 100,
         "help_text": "Upper limit per station. Mostly-still pictures usually use less."},
        {"id": "text_pattern", "label": "Text Movement", "type": "select", "default": "cycle",
         "options": [{"value": "cycle", "label": "Cycle through 8 positions"},
                     {"value": "random", "label": "Random position each photo"},
                     {"value": "corners", "label": "Corners only"}],
         "help_text": "Burn-in protection: the text moves to a new spot on every photo change."},
        {"id": "credit_mode", "label": "Photo Credits", "type": "select", "default": "brief",
         "options": [{"value": "brief", "label": "Show for 5 seconds per photo"},
                     {"value": "always", "label": "Always show"}],
         "help_text": "Photo and weather licences require a credit; brief shows it when each photo appears, then fades it out."},
        {"id": "video_encoder", "label": "Video Encoding", "type": "select", "default": "software",
         "options": [{"value": "software", "label": "Software (CPU)"},
                     {"value": "vaapi", "label": "VAAPI (Intel or AMD GPU)"},
                     {"value": "qsv", "label": "Intel Quick Sync"},
                     {"value": "nvenc", "label": "NVIDIA NVENC"}],
         "help_text": "A GPU takes the video encoding off the CPU. The container needs the GPU "
                      "passed through (/dev/dri, or the NVIDIA runtime). Sync tests it and uses "
                      "software encoding if it doesn't work."},
        {"id": "port", "label": "Server Port", "type": "number", "default": DEFAULT_PORT,
         "min": 1024, "max": 65535, "step": 1,
         "help_text": "Local port for the channel streams (127.0.0.1 only). Change it if something else uses it."},
    ]

    actions = [
        {"id": "sync", "label": "Sync Stations", "button_label": "Sync", "button_color": "green",
         "description": "Create, update and remove channels to match the station list, and start the stream server."},
        {"id": "validate", "label": "Validate", "button_label": "Validate", "button_color": "blue",
         "description": "Check each stream, its track titles, the city and available photos."},
        {"id": "status", "label": "Status", "button_label": "Status", "button_color": "gray",
         "description": "Show whether the stream server is running and which stations are being watched."},
        {"id": "stop_server", "label": "Stop Server", "button_label": "Stop", "button_color": "yellow",
         "description": "Stop the stream server. Channels stay; Sync starts it again.",
         "confirm": {"required": True, "title": "Stop Rad Streams?",
                     "message": "Anyone watching a Rad Streams channel will be disconnected."}},
        {"id": "remove_channels", "label": "Remove All Channels", "button_label": "Remove",
         "button_color": "red",
         "description": "Stop the server and delete every channel this plugin created.",
         "confirm": {"required": True, "title": "Remove Rad Streams channels?",
                     "message": "This deletes every channel and stream in the Rad Streams group that this plugin created."}},
    ]

    def __init__(self) -> None:
        self.base_dir = Path(__file__).resolve().parent
        self.data_dir = _data_dir()
        self.config_path = self.data_dir / "service.json"
        self.status_path = self.data_dir / "status.json"
        self.mapping_path = self.data_dir / "channels.json"
        self.log_dir = self.data_dir / "logs"
        self._schedule_autostart()

    # ---- entry points ----------------------------------------------------
    def run(self, action: str, params: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
        settings = dict((context or {}).get("settings") or {})
        settings.update({k: v for k, v in (params or {}).items() if k in self._field_ids()})
        action = (action or "status").lower()
        try:
            handler = {
                "sync": self._sync, "validate": self._validate, "status": self._status,
                "stop_server": self._stop_server_action, "remove_channels": self._remove_channels,
            }.get(action)
            if handler is None:
                return {"status": "error", "message": f"Unknown action '{action}'"}
            result = handler(settings)
        except Exception as exc:
            _log.exception("Rad Streams action %s failed", action)
            result = {"status": "error", "message": f"{action} failed: {exc}"}
        result["message"] = self._clip(result.get("message", ""))
        return result

    def stop(self, context: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Called when the plugin is disabled, deleted or reloaded."""
        reason = (context or {}).get("reason")
        keep = reason == "reload"   # an update: the new instance relaunches it
        self._stop_service(keep_desired=keep)
        return {"status": "ok", "message": "Rad Streams server stopped"}

    # ---- actions ---------------------------------------------------------
    def _sync(self, settings: Dict[str, Any]) -> Dict[str, Any]:
        parsed = parse_stations(settings.get("stations") or "")
        if not parsed.stations and parsed.errors:
            return {"status": "error",
                    "message": f"No valid stations. {parsed.errors[0]}"}
        port = self._port(settings)
        render = self._render_settings(settings)
        has_ffmpeg = bool(shutil.which("ffmpeg"))
        # Without ffmpeg, don't mark the server wanted, so a restart doesn't launch a doomed one.
        self._write_service_config(parsed.stations, render, port,
                                   desired=bool(parsed.stations) and has_ffmpeg)

        sync = ChannelSync(self._models(), str(self.mapping_path), default_logo=self._default_logo())
        res = sync.sync(parsed.stations,
                        lambda st: f"http://127.0.0.1:{port}/radio/{st.station_id}.ts")

        if parsed.stations and not has_ffmpeg:
            return {"status": "error",
                    "message": f"Channels: {res.summary()}, but ffmpeg was not found on the PATH, "
                               "so the stream server can't run."}
        if parsed.stations:
            try:
                started = self._ensure_service()
            except RuntimeError as exc:
                return {"status": "error", "message": f"Channels: {res.summary()}, but {exc}"}
            server = "server started" if started else "server running"
        else:
            self._stop_service(keep_desired=False)
            server = "server stopped"
        self._notify_ui()
        msg = f"Channels: {res.summary()}; {server}."
        extra = parsed.errors + res.warnings
        if render.video_encoder != "software":
            # Test the GPU here too (same container), so Sync can say if it isn't used.
            reason = encoder.probe(render.video_encoder)
            if reason:
                extra.insert(0, f"{encoder.ENCODER_LABELS[render.video_encoder]} isn't working "
                                f"({reason}), so software encoding is used")
        if extra:
            msg += f" {len(extra)} warning(s): {extra[0]}"
            self._write_log("sync.log", "\n".join([msg, ""] + extra))
        return {"status": "ok", "message": msg}

    def _validate(self, settings: Dict[str, Any]) -> Dict[str, Any]:
        parsed = parse_stations(settings.get("stations") or "")
        if not parsed.stations:
            reason = parsed.errors[0] if parsed.errors else "the Stations box is empty"
            return {"status": "error", "message": f"Nothing to validate: {reason}"}
        reports = validate.validate_all(parsed.stations, str(self.data_dir / "cache"))
        path = self._write_log("validate.log", validate.format_report(reports, parsed.errors))
        ok = all(r.ok for r in reports) and not parsed.errors
        return {"status": "ok" if ok else "warning",
                "message": validate.summarize(reports, parsed.errors, str(path), _MESSAGE_LIMIT)}

    def _status(self, settings: Dict[str, Any]) -> Dict[str, Any]:
        pid = self._service_pid()
        if not pid:
            return {"status": "ok", "message": "Server not running. Press Sync Stations to start it."}
        st = net.read_json(str(self.status_path)) or {}
        stations = st.get("stations", [])
        live = [s for s in stations if s.get("running")]
        msg = f"Server running on port {st.get('port', '?')} with {len(stations)} station(s)"
        if live:
            msg += "; on air: " + ", ".join(
                f"{s['name']} ({s.get('viewers', 0)} viewer{'s' if s.get('viewers') != 1 else ''})"
                for s in live)
        else:
            msg += "; nobody watching"
        enc = st.get("video_encoder") or {}
        if enc.get("requested") not in (None, "software"):
            if enc.get("using") == enc.get("requested"):
                msg += f"; encoding with {encoder.ENCODER_LABELS.get(enc['using'], enc['using'])}"
            elif enc.get("using"):
                msg += "; GPU encoding unavailable, using software"
        return {"status": "ok", "message": msg + "."}

    def _stop_server_action(self, settings: Dict[str, Any]) -> Dict[str, Any]:
        stopped = self._stop_service(keep_desired=False)
        return {"status": "ok",
                "message": "Server stopped." if stopped else "Server was not running."}

    def _remove_channels(self, settings: Dict[str, Any]) -> Dict[str, Any]:
        self._stop_service(keep_desired=False)
        n = ChannelSync(self._models(), str(self.mapping_path)).remove_all()
        self._notify_ui()
        return {"status": "ok", "message": f"Removed {n} channel(s) and stopped the server."}

    # ---- settings helpers -----------------------------------------------
    def _field_ids(self) -> set:
        return {f["id"] for f in self.fields}

    @staticmethod
    def _int(value: Any, default: int, lo: int, hi: int) -> int:
        try:
            v = int(float(value))
        except (TypeError, ValueError, OverflowError):
            return default
        return min(max(v, lo), hi)

    def _port(self, settings: Dict[str, Any]) -> int:
        return self._int(settings.get("port"), DEFAULT_PORT, 1024, 65535)

    def _render_settings(self, settings: Dict[str, Any]) -> RenderSettings:
        res = settings.get("resolution") if settings.get("resolution") in RESOLUTIONS else "1920x1080"
        pattern = settings.get("text_pattern")
        return RenderSettings.from_resolution(
            res,
            units="C" if str(settings.get("units", "F")).upper().startswith("C") else "F",
            video_kbps=self._int(settings.get("video_kbps"), 3000, 500, 20000),
            text_pattern=pattern if pattern in ("cycle", "random", "corners") else "cycle",
            credit_seconds=0.0 if settings.get("credit_mode") == "always" else 5.0,
            video_encoder=(settings.get("video_encoder")
                           if settings.get("video_encoder") in VIDEO_ENCODERS else "software"),
            data_dir=str(self.data_dir),
        )

    def _write_service_config(self, stations, render: RenderSettings, port: int, desired: bool) -> None:
        data = {
            "desired_running": desired,
            "host": "127.0.0.1",
            "port": port,
            "linger_seconds": 10,
            "render": render.__dict__,
            "stations": [{"name": s.name, "stream_url": s.stream_url, "city": s.city,
                          "channel_number": s.channel_number, "id": s.key,
                          "logo_url": s.logo_url} for s in stations],
        }
        net.write_json(str(self.config_path), data)

    def _write_log(self, name: str, text: str) -> Path:
        self.log_dir.mkdir(parents=True, exist_ok=True)
        path = self.log_dir / name
        path.write_text(text + "\n", encoding="utf-8")
        return path

    @staticmethod
    def _clip(msg: str) -> str:
        return msg if len(msg) <= _MESSAGE_LIMIT else msg[: _MESSAGE_LIMIT - 1].rstrip() + "…"

    @staticmethod
    def _models():
        from apps.channels.models import Channel, ChannelGroup, ChannelStream, Stream
        from core.models import StreamProfile
        try:   # guide and logo support are optional: skip them on a Dispatcharr without these
            from apps.epg.models import EPGData, EPGSource
        except ImportError:
            EPGData = EPGSource = None
        try:
            from apps.channels.models import Logo
        except ImportError:
            Logo = None
        return Models(Channel, ChannelGroup, ChannelStream, Stream, StreamProfile,
                      EPGSource=EPGSource, EPGData=EPGData, Logo=Logo)

    @staticmethod
    def _notify_ui() -> None:
        """Tell open Dispatcharr pages to reload their EPG lists.

        The web page loads its EPG sources once, when it opens, so a source
        that Sync creates is missing from the EPG manager and the channel
        dropdown until a reload, and the TV guide can't match its programmes
        to the channels. ``epg_sources_changed`` is the event Dispatcharr's
        page listens for from plugins; ``epg_data_created`` reloads the EPG
        entries. Best effort: an older Dispatcharr just needs a page reload.
        """
        try:
            from core.utils import send_websocket_update
        except ImportError:
            return
        for event in ("epg_sources_changed", "epg_data_created"):
            try:
                send_websocket_update("updates", "update", {"type": event})
            except Exception as exc:   # never fail a Sync over a page refresh
                _log.debug("could not send %s: %s", event, exc)

    def _default_logo(self) -> Optional[str]:
        """Copy the bundled logo to /data/logos, where Dispatcharr serves channel logos from.

        Returns its path, or None if it can't be placed there (Dispatcharr only
        serves local logo files from /data/logos).
        """
        src = self.base_dir / "logo.png"
        dest = self.data_dir.parent / "logos" / "rad-streams.png"
        if not str(dest).startswith("/data/logos/") or not src.is_file():
            return None
        try:
            if not dest.is_file() or dest.read_bytes() != src.read_bytes():
                dest.parent.mkdir(parents=True, exist_ok=True)
                tmp = dest.with_suffix(".png.tmp")
                shutil.copyfile(src, tmp)
                os.replace(tmp, dest)      # atomic, so a half-written logo is never served
        except OSError as exc:
            _log.warning("could not copy the channel logo: %s", exc)
            return None
        return str(dest)

    # ---- the background service ------------------------------------------
    def _python(self) -> str:
        """A real interpreter even when Dispatcharr runs under uWSGI."""
        exe = Path(sys.executable or "")
        candidates = []
        if exe.name.lower().startswith("uwsgi"):
            candidates += [exe.with_name("python"), exe.with_name("python3")]
        venv = os.environ.get("VIRTUAL_ENV")
        if venv:
            candidates += [Path(venv) / "bin" / "python", Path(venv) / "bin" / "python3"]
        candidates += [exe, Path("python3"), Path("python")]
        for c in candidates:
            if str(c) and c.is_absolute() and c.exists() and not c.name.lower().startswith("uwsgi"):
                return str(c)
            found = shutil.which(str(c)) if str(c) else None
            if found and not Path(found).name.lower().startswith("uwsgi"):
                return found
        raise RuntimeError("no Python interpreter found to run the Rad Streams server")

    def _pid_file(self) -> Path:
        return self.data_dir / "service.pid"

    def _service_pid(self) -> Optional[int]:
        """PID of our running service, checked against /proc so a recycled PID never counts."""
        try:
            pid_s, token = self._pid_file().read_text().split()[:2]
            pid = int(pid_s)
        except (OSError, ValueError):
            return None
        try:
            with open(f"/proc/{pid}/environ", "rb") as f:
                env = f.read().split(b"\0")
            if f"RS_RUN_TOKEN={token}".encode() not in env:
                return None
        except OSError:
            return None   # gone, or not ours to inspect: never signal a process we can't confirm
        try:
            os.kill(pid, 0)
        except OSError:
            return None
        try:  # reap if it's our zombie child
            done, _ = os.waitpid(pid, os.WNOHANG)
            if done == pid:
                return None
        except ChildProcessError:
            pass
        return pid

    def _lock(self):
        self.data_dir.mkdir(parents=True, exist_ok=True)
        fh = open(self.data_dir / ".service.lock", "w")
        if fcntl:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        return fh

    def _code_id(self) -> str:
        """Fingerprint of the server's code, so an updated plugin replaces an old server."""
        h = hashlib.sha1(self.version.encode())
        for path in sorted((self.base_dir / "rs").glob("*.py")):
            h.update(path.name.encode())
            h.update(path.read_bytes())
        return h.hexdigest()[:12]

    def _running_code_id(self) -> Optional[str]:
        try:
            parts = self._pid_file().read_text().split()
        except OSError:
            return None
        return parts[2] if len(parts) > 2 else None

    def _ensure_service(self) -> bool:
        """Start the service if it isn't running. Returns True if it was started."""
        if self._service_pid() and self._running_code_id() != self._code_id():
            # The plugin files were replaced but the old server is still running
            # the old code; restart it so the update takes effect.
            _log.info("Rad Streams code changed; restarting the server")
            self._stop_service(keep_desired=True)
        lock = self._lock()
        try:
            if self._service_pid():
                return False   # it picks up the new config file on its own
            self.log_dir.mkdir(parents=True, exist_ok=True)
            token = uuid.uuid4().hex
            env = os.environ.copy()
            env["PYTHONPATH"] = os.pathsep.join(p for p in (str(self.base_dir), env.get("PYTHONPATH")) if p)
            env["PYTHONDONTWRITEBYTECODE"] = "1"
            env["RS_RUN_TOKEN"] = token
            out = open(self.log_dir / "service-console.log", "ab", buffering=0)
            try:
                proc = subprocess.Popen(
                    [self._python(), "-m", "rs.service", "--config", str(self.config_path)],
                    cwd=str(self.base_dir), env=env, stdout=out, stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL, start_new_session=True)
            finally:
                out.close()
            self._pid_file().write_text(f"{proc.pid} {token} {self._code_id()}\n")
            # A bad port or a crash at import shows up within a moment; report it
            # instead of claiming the server started.
            for _ in range(8):
                time.sleep(0.25)
                if proc.poll() is not None:
                    raise RuntimeError(f"the stream server exited at start ({self._console_tail()})")
            _log.info("Rad Streams server started (pid %s)", proc.pid)
            return True
        finally:
            lock.close()

    def _console_tail(self) -> str:
        try:
            lines = (self.log_dir / "service-console.log").read_text(errors="replace").strip().splitlines()
        except OSError:
            return "see logs/service-console.log"
        return (lines[-1][-160:] if lines else "no output") + "; see logs/service-console.log"

    def _stop_service(self, keep_desired: bool) -> bool:
        if not keep_desired:
            data = net.read_json(str(self.config_path))
            if isinstance(data, dict) and data.get("desired_running"):
                data["desired_running"] = False
                net.write_json(str(self.config_path), data)
        lock = self._lock()
        try:
            pid = self._service_pid()
            if not pid:
                return False
            try:
                os.killpg(pid, signal.SIGTERM)
            except OSError:
                os.kill(pid, signal.SIGTERM)
            deadline = time.time() + 10
            while time.time() < deadline and self._service_pid():
                time.sleep(0.25)
            if self._service_pid():
                try:
                    os.killpg(pid, signal.SIGKILL)
                except OSError:
                    pass
            try:
                self._pid_file().unlink()
            except OSError:
                pass
            _log.info("Rad Streams server stopped (pid %s)", pid)
            return True
        finally:
            lock.close()

    # ---- auto-start after Dispatcharr restarts ----------------------------
    def _schedule_autostart(self) -> None:
        global _AUTOSTART_SCHEDULED
        if _AUTOSTART_SCHEDULED or os.environ.get("RS_RUN_TOKEN"):
            return
        _AUTOSTART_SCHEDULED = True
        threading.Thread(target=self._autostart, name="rad-streams-autostart", daemon=True).start()

    def _autostart(self) -> None:
        time.sleep(_AUTOSTART_DELAY_SEC)
        try:
            data = net.read_json(str(self.config_path)) or {}
            if not data.get("desired_running") or not data.get("stations"):
                return
            from apps.plugins.models import PluginConfig
            cfg = PluginConfig.objects.filter(key=self.base_dir.name).first()
            if cfg is not None and not cfg.enabled:
                return
            if self._ensure_service():
                _log.info("Rad Streams auto-start: server relaunched")
        except Exception:
            _log.exception("Rad Streams auto-start failed")
        finally:
            try:
                from django.db import close_old_connections
                close_old_connections()
            except Exception:
                pass

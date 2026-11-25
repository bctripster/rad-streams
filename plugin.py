"""Rad Streams: internet radio stations as TV-style Dispatcharr channels.

The plugin itself is thin: it parses the station list, keeps one channel per
station in the "Rad Streams" group, and keeps a small background service
running (``rs/service.py``). That service holds the on-demand HTTP server;
each channel's stream URL points at its route there, and a station renders
only while someone is watching.
"""
from __future__ import annotations

import contextlib
import copy
import hashlib
import logging
import os
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, Optional

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None

try:  # relative when Dispatcharr imports us as a package, absolute in tests
    from .rs import encoder, net, reports, validate
    from .rs.channels import ChannelSync, Models
    from .rs.config import (CPU_MODES, RESOLUTIONS, TABLE_COLUMNS, VIDEO_ENCODERS, ParseResult,
                            RenderSettings, parse_rows, parse_stations, rows_from_text)
    from .rs.server import DEFAULT_PORT
except ImportError:
    from rs import encoder, net, reports, validate
    from rs.channels import ChannelSync, Models
    from rs.config import (CPU_MODES, RESOLUTIONS, TABLE_COLUMNS, VIDEO_ENCODERS, ParseResult,
                           RenderSettings, parse_rows, parse_stations, rows_from_text)
    from rs.server import DEFAULT_PORT

_log = logging.getLogger("plugins.rad_streams")

_AUTOSTART_DELAY_SEC = 20
_MESSAGE_LIMIT = 280

# Shown above the stations table: Dispatcharr's table setting has no help text
# of its own, so this info field explains the columns.
_STATIONS_HELP = (
    "One row per station. Name, Stream URL and City or Theme are required. "
    "City: use 'City, ST' or 'City, Country' when a name is ambiguous, e.g. Seattle, WA. "
    "Theme: put Theme: and a subject in the City column for photos of that subject instead "
    "(no weather), e.g. Theme: Space. Built in: Space, Earth, Nature, Ocean, Mountains, Forest, "
    "Waterfalls, Desert, Aurora, Sunsets, Beaches, Lakes, Wildlife, Night Sky; any other word is "
    "searched on Wikimedia Commons. Channel # is optional (blank picks one from 1000 up). "
    "Keep ID: when you rename a station, put its old name here so it keeps its channel. "
    "Logo URL: an http(s) image for the channel logo (blank = the Rad Streams logo). "
    "Skip: tick to take a station off without deleting its row. Icecast/Shoutcast, .pls/.m3u and "
    "HLS (.m3u8) stream URLs all work. Save, then press Sync Stations: Sync checks every cell "
    "and changes nothing until every row is right."
)

# Shown under the table when the old text-box list was just copied into it.
_MIGRATED_HELP = (
    "Your station list from the old text box has been copied into the table above. "
    "Check it and press Save Settings to keep it. Until you save, Sync and Validate "
    "use this copy. The old text is kept unchanged in the saved settings."
)

# Field and action ids this version understands from params; "stations" is
# the pre-0.6 text setting, still read until a table is saved.
_TABLE_ID = "station_table"
_LEGACY_TEXT_ID = "stations"

# Actions that change channels, and actions that may take minutes. A lock per
# group stops two of them overlapping (e.g. Sync pressed twice).
_LOCK_GROUPS = {"sync": "channels", "remove_channels": "channels", "validate": "validate"}

# Returned with results that change channels so Dispatcharr refreshes the
# plugin page (its settings and buttons) once the action is done.
_REFRESH_UI = {"refresh": "ui"}


def _data_dir() -> Path:
    for base in ("/data", "/app/data"):
        if os.path.isdir(base) and os.access(base, os.W_OK):
            return Path(base) / "rad_streams"
    # Never write anywhere else; actions report the permission error if /data isn't writable.
    return Path("/data") / "rad_streams"


class _Report:
    """A plain-text Sync report built section by section."""

    def __init__(self, title: str, started_at: float) -> None:
        self.started_at = started_at
        self.lines = [f"Rad Streams {title}, "
                      f"{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(started_at))}", ""]

    def section(self, heading: str, items: list) -> None:
        self.lines.append(f"{heading}:")
        self.lines.extend(f"    {item}" for item in items)
        self.lines.append("")

    def text(self) -> str:
        return "\n".join(self.lines + [f"Took {time.time() - self.started_at:.0f}s."])


class Plugin:
    name = "Rad Streams"
    version = "0.6.1"
    description = ("Internet radio stations as TV channels: photos of each station's city, "
                   "the current track and local weather, with the station playing underneath.")

    # Order matters: Dispatcharr draws the ungrouped fields first, then each
    # section (here only "Advanced", collapsed until opened).
    fields = [
        {"id": "setup_info", "label": "Setup", "type": "info",
         "description": "Add your stations to the table below, press Save Settings, then press "
                        "Sync Stations. Each station becomes a channel in the Rad Streams group. "
                        "Channels render only while someone is watching. Sync and Validate run in "
                        "the background with a progress bar; each saves a full report in "
                        "/data/rad_streams/reports (the latest is also in /data/rad_streams/logs)."},
        {"id": "stations_help", "label": "Stations", "type": "info", "description": _STATIONS_HELP},
        {"id": _TABLE_ID, "label": "Stations", "type": "table", "default": [],
         "columns": [dict(c) for c in TABLE_COLUMNS]},
        {"id": "units", "label": "Temperature Units", "type": "select", "default": "F",
         "options": [{"value": "F", "label": "Fahrenheit (°F, mph)"},
                     {"value": "C", "label": "Celsius (°C, km/h)"}]},
        {"id": "text_pattern", "label": "Text Movement", "type": "select", "default": "cycle",
         "options": [{"value": "cycle", "label": "Cycle through 8 positions"},
                     {"value": "random", "label": "Random position each photo"},
                     {"value": "corners", "label": "Corners only"}],
         "help_text": "Burn-in protection: the text moves to a new spot on every photo change."},
        {"id": "credit_mode", "label": "Photo Credits", "type": "select", "default": "brief",
         "options": [{"value": "brief", "label": "Show for 5 seconds per photo"},
                     {"value": "always", "label": "Always show"}],
         "help_text": "Photo and weather licences require a credit; brief shows it when each photo appears, then fades it out."},
        {"id": "cpu_mode", "label": "CPU Use", "type": "select", "default": "normal",
         "options": [{"value": "normal", "label": "Normal (25 fps)"},
                     {"value": "low", "label": "Low (15 fps, faster encoding)"}],
         "help_text": "Low uses about a third less CPU per station with software encoding. "
                      "Crossfades between photos look a little less smooth."},
        {"id": "advanced", "label": "Advanced", "type": "section", "collapsed": True},
        {"id": "resolution", "label": "Resolution", "type": "select", "default": "1920x1080",
         "section": "advanced",
         "options": [{"value": k, "label": k} for k in ("3840x2160", "2560x1440", "1920x1080",
                                                        "1280x720", "854x480")]},
        {"id": "video_kbps", "label": "Video Bitrate (kbps)", "type": "number", "default": 3000,
         "min": 500, "max": 20000, "step": 100, "section": "advanced",
         "help_text": "Upper limit per station. Still pictures use less with software encoding."},
        {"id": "video_encoder", "label": "Video Encoding", "type": "select", "default": "software",
         "section": "advanced",
         "options": [{"value": "software", "label": "Software (CPU)"},
                     {"value": "vaapi", "label": "VAAPI (Intel or AMD GPU)"},
                     {"value": "qsv", "label": "Intel Quick Sync"},
                     {"value": "nvenc", "label": "NVIDIA NVENC"}],
         "help_text": "A GPU takes the video encoding off the CPU. The container needs the GPU "
                      "passed through (/dev/dri, or the NVIDIA runtime). Sync tests it and uses "
                      "software encoding if it doesn't work."},
        {"id": "port", "label": "Server Port", "type": "number", "default": DEFAULT_PORT,
         "min": 1024, "max": 65535, "step": 1, "section": "advanced",
         "help_text": "Local port for the channel streams (127.0.0.1 only). Change it if something else uses it."},
    ]

    actions = [
        # Sync and Validate run as Dispatcharr background tasks ("async"), with a
        # progress bar, so a long station list no longer times out the button.
        {"id": "sync", "label": "Sync Stations", "button_label": "Sync", "button_color": "green",
         "async": True,
         "description": "Check the stations table, then create, update and remove channels to match "
                        "it and start the stream server. Runs in the background."},
        {"id": "validate", "label": "Validate", "button_label": "Validate", "button_color": "blue",
         "async": True,
         "description": "Check each stream, its track titles, the city and available photos. "
                        "Runs in the background and saves a full report."},
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

    # ---- entry points ----------------------------------------------------
    def get_fields(self, context: Optional[Dict[str, Any]] = None) -> list:
        """The settings form, built when Dispatcharr discovers the plugin.

        Before v0.6.0 stations were typed into one text box ("stations"). When
        a saved setup still has that text and no stations table yet, the text
        is converted here and becomes the table's default, so the table opens
        already filled in and Sync uses it until the person saves. Nothing is
        written: the old text stays in the saved settings as a backup.
        Never raises, since an error here would hide the whole form.
        """
        fields = copy.deepcopy(self.fields)
        try:
            saved = dict((context or {}).get("settings") or {})
            text = saved.get(_LEGACY_TEXT_ID)
            if _TABLE_ID in saved or not isinstance(text, str) or not text.strip():
                return fields
            migration = rows_from_text(text)
            at = next(i for i, f in enumerate(fields) if f["id"] == _TABLE_ID)
            fields[at]["default"] = migration.rows
            notice = _MIGRATED_HELP
            if migration.notes:
                notice += " Check these: " + "; ".join(migration.notes[:5]) + "."
            fields.insert(at + 1, {"id": "migration_info", "label": "Copied from the old list",
                                   "type": "info", "description": notice})
        except Exception:   # pragma: no cover - defensive: show the plain form instead
            _log.exception("Rad Streams: could not convert the old station list")
            return copy.deepcopy(self.fields)
        return fields

    def run(self, action: str, params: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
        context = context or {}
        settings = dict(context.get("settings") or {})
        settings.update({k: v for k, v in (params or {}).items() if k in self._field_ids()})
        action = (action or "status").lower()
        progress = self._progress_reporter(context.get("report_progress"))
        try:
            handler = {
                "sync": self._sync, "validate": self._validate, "status": self._status,
                "stop_server": self._stop_server_action, "remove_channels": self._remove_channels,
            }.get(action)
            if handler is None:
                return {"status": "error", "message": f"Unknown action '{action}'"}
            with self._action_lock(_LOCK_GROUPS.get(action)) as free:
                if not free:
                    return {"status": "error",
                            "message": "Another Sync, Validate or Remove is still running. "
                                       "Try again when it has finished."}
                result = handler(settings, progress)
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

    # ---- progress and locking ---------------------------------------------
    @staticmethod
    def _progress_reporter(report: Any) -> Callable[[float, str], None]:
        """Wrap Dispatcharr's ``report_progress`` so it can't break an action.

        Dispatcharr makes it a no-op outside background tasks; it may be missing
        altogether on older versions or in tests. Percent is clamped to 0-99:
        Dispatcharr marks the task finished itself when the action returns.
        """
        def progress(percent: float, message: str) -> None:
            if not callable(report):
                return
            try:
                report(int(min(max(percent, 0), 99)), message[:200])
            except Exception as exc:
                _log.debug("progress report failed: %s", exc)
        return progress

    @contextlib.contextmanager
    def _action_lock(self, group: Optional[str]):
        """Hold a non-blocking lock for ``group``; yields False if it's taken.

        Locks live in the data folder, so they work across Dispatcharr's worker
        processes, and the OS drops them if a worker dies mid-action.
        """
        if group is None or fcntl is None:
            yield True
            return
        self.data_dir.mkdir(parents=True, exist_ok=True)
        fh = open(self.data_dir / f".{group}.lock", "w")
        try:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                yield False
                return
            yield True
        finally:
            fh.close()       # closing the file releases the lock

    # ---- the station list -------------------------------------------------
    def _stations(self, settings: Dict[str, Any]) -> ParseResult:
        """The station list: the table when the settings have one, else the old text."""
        table = settings.get(_TABLE_ID)
        if table is not None:
            return parse_rows(table)      # a non-list is reported as a problem there
        return parse_stations(settings.get(_LEGACY_TEXT_ID) or "")

    def _channel_count(self) -> int:
        mapping = net.read_json(str(self.mapping_path))
        return len(mapping) if isinstance(mapping, dict) else 0

    # ---- actions ---------------------------------------------------------
    def _sync(self, settings: Dict[str, Any], progress: Callable[[float, str], None]) -> Dict[str, Any]:
        started_at = time.time()
        progress(2, "Checking the stations table")
        parsed = self._stations(settings)
        port = self._port(settings)
        render = self._render_settings(settings)
        report = _Report("Sync", started_at)
        report.section("Settings", [
            f"resolution {render.width}x{render.height}, bitrate {render.video_kbps} kbps, "
            f"encoder {render.video_encoder}, {render.output_fps} fps, port {port}"])

        if parsed.errors:
            # Per-cell check: one bad row would otherwise drop that station and
            # delete its channel, so nothing changes until every row is right.
            report.section("Stations table problems (nothing was changed)", parsed.errors)
            path = self._save_report("sync", report)
            first = parsed.errors[0]
            more = f" (+{len(parsed.errors) - 1} more)" if len(parsed.errors) > 1 else ""
            return {"status": "error",
                    "message": self._clip(f"Nothing was changed. Fix the stations table: {first}",
                                          tail=f"{more}. Report: {path}"),
                    "problems": parsed.cell_errors or parsed.errors, "report": str(path)}
        if not parsed.stations and not parsed.skipped and self._channel_count():
            # An empty table is far more often an accident (a cleared form, a
            # bad import) than a wish to delete every channel; that has its own button.
            return {"status": "error",
                    "message": f"The stations table is empty, so nothing was changed. To delete "
                               f"the {self._channel_count()} Rad Streams channel(s), press "
                               "Remove All Channels."}

        has_ffmpeg = bool(shutil.which("ffmpeg"))
        if parsed.stations and has_ffmpeg:
            progress(8, f"Checking port {port}")
            # Check the port before anything is written: the channels are about to
            # be pointed at it, and a port the server can't open would leave every
            # channel dead while Sync reported success.
            busy = self._port_problem(port)
            if busy:
                return {"status": "error",
                        "message": f"Port {port} can't be used ({busy}). Nothing was changed. "
                                   "Pick another Server Port and Sync again."}
        # Without ffmpeg, don't mark the server wanted, so a restart doesn't launch a doomed one.
        self._write_service_config(parsed.stations, render, port,
                                   desired=bool(parsed.stations) and has_ffmpeg)

        progress(12, "Updating channels")

        def channel_progress(done: int, total: int, name: str) -> None:
            progress(12 + 60 * done / max(total, 1), f"Channels: {done}/{total} ({name})")

        sync = ChannelSync(self._models(), str(self.mapping_path), default_logo=self._default_logo())
        res = sync.sync(parsed.stations,
                        lambda st: f"http://127.0.0.1:{port}/radio/{st.station_id}.ts",
                        progress=channel_progress)
        report.section("Channels", [f"{label}: {', '.join(items)}" for label, items in (
            ("created", res.created), ("updated", res.updated),
            ("unchanged", res.unchanged), ("removed", res.removed)) if items] or ["no stations"])
        if parsed.skipped:
            report.section("Skipped rows", [f"{parsed.skipped} row(s) have Skip ticked"])
        details = {"created": res.created, "updated": res.updated, "unchanged": res.unchanged,
                   "removed": res.removed, "skipped": parsed.skipped}

        if parsed.stations and not has_ffmpeg:
            report.section("Server", ["ffmpeg was not found on the PATH"])
            path = self._save_report("sync", report)
            self._notify_ui(res)
            return {"status": "error", "report": str(path), "channels": details,
                    "message": f"Channels: {res.summary()}, but ffmpeg was not found on the PATH, "
                               "so the stream server can't run."}
        progress(78, "Starting the stream server" if parsed.stations else "Stopping the stream server")
        if parsed.stations:
            try:
                started = self._ensure_service()
            except RuntimeError as exc:
                report.section("Server", [str(exc)])
                path = self._save_report("sync", report)
                self._notify_ui(res)
                return {"status": "error", "report": str(path), "channels": details,
                        "message": f"Channels: {res.summary()}, but {exc}"}
            server = "server started" if started else "server running"
        else:
            self._stop_service(keep_desired=False)
            server = "server stopped"
        report.section("Server", [server])
        self._notify_ui(res)
        msg = f"Channels: {res.summary()}; {server}."
        extra = list(res.warnings)
        if render.video_encoder != "software":
            progress(88, f"Testing {encoder.ENCODER_LABELS[render.video_encoder]}")
            # Test the GPU here too (same container), so Sync can say if it isn't used.
            reason = encoder.probe(render.video_encoder)
            if reason:
                extra.insert(0, f"{encoder.ENCODER_LABELS[render.video_encoder]} isn't working "
                                f"({reason}), so software encoding is used")
        if extra:
            msg += f" {len(extra)} warning(s): {extra[0]}"
            report.section("Warnings", extra)
        progress(96, "Saving the report")
        path = self._save_report("sync", report)
        return {"status": "partial" if extra else "ok", "message": msg, "report": str(path),
                "channels": details, "warnings": extra, "_dispatcharr": dict(_REFRESH_UI)}

    def _validate(self, settings: Dict[str, Any], progress: Callable[[float, str], None]) -> Dict[str, Any]:
        started_at = time.time()
        progress(2, "Checking the stations table")
        parsed = self._stations(settings)
        if not parsed.stations:
            reason = parsed.errors[0] if parsed.errors else "the stations table is empty"
            return {"status": "error", "message": f"Nothing to validate: {reason}"}
        total = len(parsed.stations)
        progress(5, f"Checking {total} station(s)")

        def station_done(done: int, count: int, rep: Any) -> None:
            progress(5 + 90 * done / max(count, 1),
                     f"Checked {done}/{count}: {rep.station.name} {'OK' if rep.ok else 'has problems'}")

        results = validate.validate_all(parsed.stations, str(self.data_dir / "cache"),
                                        on_done=station_done)
        text = validate.format_report(results, parsed.errors)
        text += f"\nTook {time.time() - started_at:.0f}s.\n"
        path = self._save_report_text("validate", text)
        ok = all(r.ok for r in results) and not parsed.errors
        return {"status": "ok" if ok else "partial",
                "message": validate.summarize(results, parsed.errors, str(path), _MESSAGE_LIMIT),
                "report": str(path), "stations": validate.as_dicts(results),
                "table_problems": parsed.errors}

    def _status(self, settings: Dict[str, Any], progress: Any = None) -> Dict[str, Any]:
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

    def _stop_server_action(self, settings: Dict[str, Any], progress: Any = None) -> Dict[str, Any]:
        stopped = self._stop_service(keep_desired=False)
        return {"status": "ok",
                "message": "Server stopped." if stopped else "Server was not running."}

    def _remove_channels(self, settings: Dict[str, Any], progress: Any = None) -> Dict[str, Any]:
        self._stop_service(keep_desired=False)
        n = ChannelSync(self._models(), str(self.mapping_path)).remove_all()
        self._notify_ui(None, removed=n)
        return {"status": "ok", "message": f"Removed {n} channel(s) and stopped the server.",
                "_dispatcharr": dict(_REFRESH_UI)}

    # ---- settings helpers -----------------------------------------------
    def _field_ids(self) -> set:
        """Setting ids an action's params may override (plus the old text box)."""
        return {f["id"] for f in self.fields if f.get("type") not in ("info", "section")} | {_LEGACY_TEXT_ID}

    @staticmethod
    def _int(value: Any, default: int, lo: int, hi: int) -> int:
        try:
            v = int(float(value))
        except (TypeError, ValueError, OverflowError):
            return default
        return min(max(v, lo), hi)

    def _port(self, settings: Dict[str, Any]) -> int:
        return self._int(settings.get("port"), DEFAULT_PORT, 1024, 65535)

    def _port_problem(self, port: int) -> Optional[str]:
        """Why the stream server couldn't listen on ``port``, or None if it can.

        Our own running server already holding the port is fine. Otherwise the
        port is opened and closed again, the same way the server opens it.
        """
        if self._service_pid():
            status = net.read_json(str(self.status_path)) or {}
            if status.get("running") and status.get("port") == port:
                return None
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            # As http.server does, so a port in TIME_WAIT isn't reported as busy.
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            probe.bind(("127.0.0.1", port))
        except OSError as exc:
            return exc.strerror or str(exc)
        finally:
            probe.close()
        return None

    def _render_settings(self, settings: Dict[str, Any]) -> RenderSettings:
        res = settings.get("resolution") if settings.get("resolution") in RESOLUTIONS else "1920x1080"
        pattern = settings.get("text_pattern")
        output_fps, preset = CPU_MODES.get(settings.get("cpu_mode"), CPU_MODES["normal"])
        return RenderSettings.from_resolution(
            res,
            units="C" if str(settings.get("units", "F")).upper().startswith("C") else "F",
            video_kbps=self._int(settings.get("video_kbps"), 3000, 500, 20000),
            text_pattern=pattern if pattern in ("cycle", "random", "corners") else "cycle",
            credit_seconds=0.0 if settings.get("credit_mode") == "always" else 5.0,
            video_encoder=(settings.get("video_encoder")
                           if settings.get("video_encoder") in VIDEO_ENCODERS else "software"),
            output_fps=output_fps,
            x264_preset=preset,
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

    def _save_report(self, kind: str, report: "_Report") -> Path:
        return self._save_report_text(kind, report.text())

    def _save_report_text(self, kind: str, text: str) -> Path:
        """Save a full report (see rs/reports.py); returns its timestamped path."""
        return reports.save(str(self.data_dir), kind, text)

    @staticmethod
    def _clip(msg: str, tail: str = "") -> str:
        """Fit ``msg + tail`` in Dispatcharr's pop-up, cutting ``msg`` so ``tail`` survives."""
        room = _MESSAGE_LIMIT - len(tail)
        if len(msg) > room:
            msg = msg[: max(room - 1, 0)].rstrip() + "…"
        return (msg + tail)[:_MESSAGE_LIMIT]

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
    def _notify_ui(res: Any = None, removed: int = 0) -> None:
        """Tell open Dispatcharr pages to reload what Sync or Remove changed.

        The web page loads its lists once, when it opens, so channels and the
        guide source that Sync creates are missing until a reload. These are
        events Dispatcharr's page already handles:

        - ``epg_sources_changed`` / ``epg_data_created`` reload the EPG lists;
        - ``channels_created`` (when Sync created channels) reloads the
          channel and stream tables and says how many were created;
        - otherwise ``epg_match`` with our own message reloads the channel
          table (it is the one event that does so with a custom message).

        Best effort: an older Dispatcharr just needs a page reload.
        """
        try:
            from core.utils import send_websocket_update
        except ImportError:
            return
        events: list = [{"type": "epg_sources_changed"}, {"type": "epg_data_created"}]
        created = len(getattr(res, "created", None) or [])
        changed = len(getattr(res, "updated", None) or []) + len(getattr(res, "removed", None) or [])
        if created:
            events.append({"type": "channels_created", "count": created})
        elif changed or removed:
            what = f"removed {removed}" if res is None else f"changed {changed}"
            events.append({"type": "epg_match",
                           "message": f"Rad Streams {what} channel(s); channel list refreshed."})
        for event in events:
            try:
                send_websocket_update("updates", "update", event)
            except Exception as exc:   # never fail a Sync over a page refresh
                _log.debug("could not send %s: %s", event.get("type"), exc)

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
                try:
                    os.kill(pid, signal.SIGTERM)
                except OSError:
                    pass       # it exited between the check and the signal
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
    # Dispatcharr 0.34+ builds a Plugin in every worker process, so nothing may
    # start in __init__. Instead it elects one process cluster-wide and calls
    # on_leader_acquired there (needs the persistent_service capability). The
    # stream server itself is a separate OS process shared by every worker, so
    # the leader's only job is to relaunch it after a restart or an update; the
    # lock file and pid token still stop two leaders (e.g. no Redis) from
    # starting two servers.
    def on_leader_acquired(self, context: Dict[str, Any]) -> None:
        """Run the one-shot auto-start in a Dispatcharr-owned service thread."""
        if os.environ.get("RS_RUN_TOKEN"):
            return   # never inside our own server process
        context["start_service"](self._leader_service, name="autostart")

    def on_leader_lost(self, context: Dict[str, Any]) -> None:
        """Nothing to tear down: the stream server outlives a lease handover.

        Disable, delete and reload go through stop(), which stops it when needed.
        """

    def _leader_service(self, stop_event: threading.Event) -> None:
        """Auto-start once, then idle until Dispatcharr stops the service.

        Returning early would count as an unexpected exit and cost the lease,
        so the thread waits on stop_event (a cheap, interruptible wait).
        """
        if stop_event.wait(_AUTOSTART_DELAY_SEC):
            return   # stopped before Dispatcharr finished starting up
        self._autostart()
        stop_event.wait()

    def _autostart(self) -> None:
        """Relaunch the server if it was running before (desired_running)."""
        try:
            data = net.read_json(str(self.config_path)) or {}
            if not data.get("desired_running") or not data.get("stations"):
                return
            # Leader hooks only run for enabled plugins, so no DB check is needed.
            if self._ensure_service():
                _log.info("Rad Streams auto-start: server relaunched")
        except Exception:
            _log.exception("Rad Streams auto-start failed")

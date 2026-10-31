"""The long-running Rad Streams process the plugin launches.

``python -m rs.service --config /data/rad_streams/service.json``

Runs the on-demand HTTP server outside Dispatcharr's web workers (which run
under gevent and can be recycled at any time). The plugin's Sync action
rewrites the config file; this process notices the change within a couple
of seconds and applies it without dropping unaffected stations. It writes a
small status file the plugin reads for its Status/Validate output.
"""
from __future__ import annotations

import argparse
import logging
import logging.handlers
import os
import signal
import sys
import threading
import time
from typing import Optional

from . import cleanup, encoder, net
from .config import RenderSettings, Station
from .server import DEFAULT_PORT, RadioServer
from .render import splash_size
from .splash import SPLASH_VERSION, splash_background

log = logging.getLogger("rad_streams.service")

RELOAD_POLL_SECONDS = 2.0
STATUS_EVERY_SECONDS = 5.0
CLEANUP_EVERY_SECONDS = 24 * 3600


def load_config(path: str) -> tuple[list[Station], RenderSettings, dict]:
    """Read service.json -> (stations, render settings, server options)."""
    data = net.read_json(path)
    if not isinstance(data, dict):
        raise ValueError(f"unreadable config: {path}")
    stations = [Station(s["name"], s["stream_url"], s["city"], s.get("channel_number"),
                        key=s.get("id") or None, logo_url=s.get("logo_url") or None)
                for s in data.get("stations", [])]
    known = set(RenderSettings.__dataclass_fields__)
    settings = RenderSettings(**{k: v for k, v in (data.get("render") or {}).items() if k in known})
    server = {"host": data.get("host", "127.0.0.1"), "port": int(data.get("port", DEFAULT_PORT)),
              "linger_seconds": float(data.get("linger_seconds", 10.0))}
    return stations, settings, server


def setup_logging(log_dir: str) -> None:
    os.makedirs(log_dir, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(
        os.path.join(log_dir, "rad_streams.log"), maxBytes=5 * 1024 * 1024, backupCount=2,
        encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(logging.INFO)


class Service:
    def __init__(self, config_path: str):
        self.config_path = config_path
        self.stop_event = threading.Event()
        self.server: Optional[RadioServer] = None
        self._mtime = 0.0

    def _config_mtime(self) -> float:
        try:
            return os.stat(self.config_path).st_mtime
        except OSError:
            return 0.0

    def run(self) -> int:
        stations, settings, opts = load_config(self.config_path)
        self._mtime = self._config_mtime()
        self.server = RadioServer(stations, settings, **opts)
        self.server.start()
        self._prewarm_splash(settings)
        self._prewarm_encoder(settings)
        self._write_status()
        last_status = time.monotonic()
        last_cleanup = None
        while not self.stop_event.wait(RELOAD_POLL_SECONDS):
            if last_cleanup is None or time.monotonic() - last_cleanup >= CLEANUP_EVERY_SECONDS:
                last_cleanup = time.monotonic()
                self._cleanup(self.server.settings)
            mtime = self._config_mtime()
            if mtime == 0.0:
                log.info("config file removed; shutting down")
                break
            if mtime != self._mtime:
                self._mtime = mtime
                self._reload()
            if time.monotonic() - last_status >= STATUS_EVERY_SECONDS:
                self._write_status()
                last_status = time.monotonic()
        self.server.stop()
        self._write_status(running=False)
        return 0

    def _reload(self) -> None:
        try:
            stations, settings, opts = load_config(self.config_path)
        except Exception as exc:
            log.warning("ignoring bad config update: %s", exc)
            return
        if (opts["host"], opts["port"]) != (self.server.host, self.server.port):
            # A new port needs a new listener; restart the whole server.
            log.info("port changed to %s:%s; restarting server", opts["host"], opts["port"])
            new = RadioServer(stations, settings, **opts)
            try:
                new.start()      # bind the new port before letting go of the old one
            except OSError as exc:
                log.error("can't listen on %s:%s (%s); staying on port %s",
                          opts["host"], opts["port"], exc, self.server.port)
                return
            self.server.stop()
            self.server = new
        else:
            self.server.linger = opts["linger_seconds"]
            self.server.update(stations, settings)
        self._prewarm_splash(settings)
        self._prewarm_encoder(settings)
        log.info("config reloaded: %d stations", len(stations))

    @staticmethod
    def _cleanup(settings) -> None:
        """Delete photos and splash files nothing uses any more (in the background)."""
        w, h = splash_size((settings.width, settings.height))
        keep = [f"splash-v{SPLASH_VERSION}-{w}x{h}.png"]

        def work():
            try:
                n = cleanup.sweep(settings.cache_dir, keep_splash=keep)
                if n:
                    log.info("cache cleanup removed %d old item(s)", n)
            except Exception:
                log.exception("cache cleanup failed")
        threading.Thread(target=work, name="cleanup", daemon=True).start()

    @staticmethod
    def _prewarm_splash(settings) -> None:
        """Draw (or load) the splash now, so a first viewer doesn't wait for it."""
        def work():
            try:
                splash_background(splash_size((settings.width, settings.height)), settings.cache_dir)
            except Exception:
                log.exception("splash drawing failed")
        threading.Thread(target=work, name="splash", daemon=True).start()

    @staticmethod
    def _prewarm_encoder(settings) -> None:
        """Test a GPU encoder now, so a first viewer doesn't wait for the test."""
        if settings.video_encoder == "software":
            return

        def work():
            try:
                encoder.resolve(settings.video_encoder)
            except Exception:
                log.exception("encoder test failed")
        threading.Thread(target=work, name="encoder-probe", daemon=True).start()

    def _write_status(self, running: bool = True) -> None:
        status = self.server.status() if self.server else {}
        if self.server is not None:
            wanted = self.server.settings.video_encoder
            known = encoder.cached(wanted)
            status["video_encoder"] = {
                "requested": wanted,
                "using": known[0] if known else None,     # None while the test runs
                "fallback_reason": known[1] if known else None,
            }
        status.update(pid=os.getpid(), running=running, updated_at=time.time(),
                      run_token=os.environ.get("RS_RUN_TOKEN", ""))
        try:
            net.write_json(os.path.join(os.path.dirname(self.config_path), "status.json"), status)
        except OSError as exc:
            log.warning("could not write status: %s", exc)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="rs.service")
    ap.add_argument("--config", required=True)
    a = ap.parse_args(argv)
    try:
        _, settings, _ = load_config(a.config)
        setup_logging(settings.log_dir)
    except Exception as exc:
        print(f"rad_streams: {exc}", file=sys.stderr)
        return 2
    svc = Service(a.config)

    def on_signal(signum, _frame):
        log.info("signal %s: stopping", signum)
        svc.stop_event.set()

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)
    log.info("Rad Streams service starting (pid %d)", os.getpid())
    try:
        return svc.run()
    except OSError as exc:
        log.error("service failed: %s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

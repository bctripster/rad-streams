"""On-demand HTTP server: one port, one route per station.

``GET /radio/<station-id>.ts`` starts that station's renderer on the first
connection and fans its MPEG-TS out to every connected client. When the last
client leaves, the renderer keeps running for ``linger_seconds`` (so a quick
channel flip back doesn't restart it) and then stops.

Response headers go out immediately and the renderer's first frame is a
placeholder drawn before any network call, so Dispatcharr's proxy sees data
well inside its connect/read timeouts.
"""
from __future__ import annotations

import json
import logging
import queue
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Optional

from .config import RenderSettings, Station
from .station import StationRunner

log = logging.getLogger("rad_streams.server")

TS_PACKET = 188
READ_SIZE = TS_PACKET * 64          # ~12 KB per read from ffmpeg
CLIENT_QUEUE_CHUNKS = 400           # ~5 MB, ~13 s at 3 Mbps; slower clients are dropped
DEFAULT_PORT = 5980
RESTART_BACKOFF = 3.0

RunnerFactory = Callable[[Station, RenderSettings], StationRunner]


FFMPEG_LOG_MAX = 2 * 1024 * 1024


def default_runner_factory(station: Station, settings: RenderSettings) -> StationRunner:
    import os
    os.makedirs(settings.log_dir, exist_ok=True)
    log_path = os.path.join(settings.log_dir, f"ffmpeg-{station.station_id}.log")
    try:  # keep one previous log per station
        if os.path.getsize(log_path) > FFMPEG_LOG_MAX:
            os.replace(log_path, log_path + ".1")
    except OSError:
        pass
    return StationRunner(station, settings, "-", log_path=log_path)


class _Client:
    def __init__(self, addr: str):
        self.addr = addr
        self.q: "queue.Queue[Optional[bytes]]" = queue.Queue(maxsize=CLIENT_QUEUE_CHUNKS)
        self.dropped = False
        self.since = time.time()

    def push(self, chunk: Optional[bytes]) -> None:
        try:
            self.q.put_nowait(chunk)
        except queue.Full:
            # Too far behind to catch up: end this client so it reconnects at live.
            self.dropped = True
            try:
                while True:
                    self.q.get_nowait()
            except queue.Empty:
                pass
            self.q.put_nowait(None)


class Broadcast:
    """Owns one station's renderer and the clients watching it."""

    def __init__(self, station: Station, settings: RenderSettings, factory: RunnerFactory,
                 linger_seconds: float):
        self.station = station
        self.settings = settings
        self.factory = factory
        self.linger = linger_seconds
        self.clients: list[_Client] = []
        self.runner: Optional[StationRunner] = None
        self._lock = threading.RLock()
        self._idle_since: Optional[float] = None
        self._generation = 0
        self._last_failure = -RESTART_BACKOFF
        self.starts = 0
        self.bytes_out = 0
        self.last_error: Optional[str] = None
        self.closed = False
        self._starting = False

    # ---- clients -------------------------------------------------------
    def attach(self, addr: str) -> Optional[_Client]:
        """Add a viewer, starting the renderer if needed. None if this station was removed."""
        c = _Client(addr)
        with self._lock:
            if self.closed:
                return None
            self.clients.append(c)
            self._idle_since = None
            start = self.runner is None and not self._starting
            if start:
                self._starting = True
            n = len(self.clients)
        log.info("%s: viewer connected (%s), %d watching", self.station.name, addr, n)
        if start:
            self._start_runner()
        return c

    def detach(self, c: _Client) -> None:
        with self._lock:
            if c in self.clients:
                self.clients.remove(c)
            if not self.clients:
                self._idle_since = time.monotonic()
            n = len(self.clients)
        log.info("%s: viewer left (%s), %d watching", self.station.name, c.addr, n)

    # ---- renderer ------------------------------------------------------
    def _start_runner(self) -> None:
        """Start a renderer. Runs outside the lock: it can take a few seconds."""
        runner = None
        try:
            # After an unplanned exit, wait a moment so a crashing renderer
            # isn't relaunched in a tight loop by reconnecting clients.
            wait = RESTART_BACKOFF - (time.monotonic() - self._last_failure)
            if wait > 0:
                time.sleep(wait)
            runner = self.factory(self.station, self.settings)
            runner.start()
        except Exception as exc:
            log.exception("%s: renderer failed to start", self.station.name)
            if runner is not None:
                threading.Thread(target=runner.stop, daemon=True).start()
            with self._lock:
                self._starting = False
                self._last_failure = time.monotonic()
                self.last_error = f"renderer failed to start: {exc}"
                targets = list(self.clients)
            for c in targets:      # end them; the proxy retries after the backoff
                c.push(None)
            return
        with self._lock:
            self._starting = False
            if self.closed:        # the station was removed while starting
                orphan = True
            else:
                orphan = False
                self._generation += 1
                gen = self._generation
                self.runner = runner
                self.starts += 1
        if orphan:
            runner.stop()
            return
        log.info("%s: renderer started", self.station.name)
        threading.Thread(target=self._pump, args=(runner, gen), daemon=True,
                         name=f"rs-pump-{self.station.station_id}").start()

    def _pump(self, runner: StationRunner, gen: int) -> None:
        out = runner.stdout
        while True:
            try:
                chunk = out.read(READ_SIZE)
            except (OSError, ValueError):
                chunk = b""
            if not chunk:
                break
            self.bytes_out += len(chunk)
            with self._lock:
                targets = list(self.clients)
            for c in targets:
                c.push(chunk)
        # Renderer output ended. If it wasn't a planned stop, end the clients
        # so the proxy reconnects (and a fresh renderer starts).
        with self._lock:
            if gen != self._generation or self.runner is not runner:
                return
            self.runner = None
            if runner.error:
                self.last_error = runner.error
            self._last_failure = time.monotonic()
            targets = list(self.clients)
        if runner.error or not runner.stopping:
            log.warning("%s: renderer output ended%s", self.station.name,
                        f" ({runner.error})" if runner.error else "")
        for c in targets:
            c.push(None)
        threading.Thread(target=runner.stop, daemon=True).start()

    def stop_runner(self, only_if_idle: bool = False) -> None:
        with self._lock:
            if only_if_idle:
                # Decide and detach the runner in one step, so a viewer
                # arriving now gets a fresh renderer instead of a dying one.
                idle = (self.runner is not None and not self.clients and self._idle_since is not None
                        and time.monotonic() - self._idle_since >= self.linger)
                if not idle:
                    return
            runner, self.runner = self.runner, None
            self._generation += 1
            targets = list(self.clients)
        if runner is not None:
            log.info("%s: renderer stopped", self.station.name)
            runner.stop()
        for c in targets:
            c.push(None)

    def close(self) -> None:
        """The station was removed or replaced: stop and refuse new viewers."""
        with self._lock:
            self.closed = True
        self.stop_runner()

    def reap(self) -> None:
        """Stop the renderer once it has been idle for ``linger`` seconds."""
        self.stop_runner(only_if_idle=True)

    def status(self) -> dict:
        r = self.runner
        return {
            "id": self.station.station_id, "name": self.station.name, "city": self.station.city,
            "running": r is not None, "viewers": len(self.clients), "starts": self.starts,
            "bytes_out": self.bytes_out, "last_error": self.last_error,
            "title": r.radio.title if r else None,
            "stream_live": r.radio.is_live if r else None,
            "stream_error": r.radio.last_error if r else None,
        }


class RadioServer:
    """Threaded HTTP server holding a Broadcast per configured station."""

    def __init__(self, stations: list[Station], settings: RenderSettings, *,
                 host: str = "127.0.0.1", port: int = DEFAULT_PORT, linger_seconds: float = 10.0,
                 runner_factory: RunnerFactory = default_runner_factory):
        self.settings = settings
        self.factory = runner_factory
        self.linger = linger_seconds
        self.host, self.port = host, port
        self._lock = threading.Lock()
        self.broadcasts: dict[str, Broadcast] = {}
        self.update(stations, settings)
        self.httpd: Optional[ThreadingHTTPServer] = None
        self._stop = threading.Event()

    # ---- config --------------------------------------------------------
    def update(self, stations: list[Station], settings: Optional[RenderSettings] = None) -> None:
        """Apply a new station list. Changed or removed stations stop."""
        settings = settings or self.settings
        settings_changed = settings != self.settings
        self.settings = settings
        new = {s.station_id: s for s in stations}
        stale: list[Broadcast] = []
        with self._lock:
            for sid, b in list(self.broadcasts.items()):
                s = new.get(sid)
                if s is None:
                    stale.append(self.broadcasts.pop(sid))
                elif settings_changed or (s.stream_url, s.city, s.name) != (
                        b.station.stream_url, b.station.city, b.station.name):
                    stale.append(b)
                    self.broadcasts[sid] = Broadcast(s, settings, self.factory, self.linger)
            for sid, s in new.items():
                if sid not in self.broadcasts:
                    self.broadcasts[sid] = Broadcast(s, settings, self.factory, self.linger)
        for b in stale:
            b.close()

    def url_for(self, station: Station, host: Optional[str] = None) -> str:
        return f"http://{host or self.host}:{self.port}/radio/{station.station_id}.ts"

    # ---- lifecycle -----------------------------------------------------
    def start(self) -> None:
        server = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"   # stream until close; no chunked encoding
            server_version = "RadStreams/0.1"
            # A viewer that stops reading without disconnecting must not pin the renderer.
            timeout = 30

            def log_message(self, fmt, *args):
                log.debug("%s %s", self.address_string(), fmt % args)

            def do_HEAD(self):
                self._route(head=True)

            def do_GET(self):
                self._route(head=False)

            def _route(self, head: bool):
                path = self.path.split("?", 1)[0]
                if path in ("/", "/status", "/status.json"):
                    body = json.dumps(server.status(), indent=1).encode()
                    self._send(200, "application/json", body, head)
                    return
                if path.startswith("/radio/") and path.endswith(".ts"):
                    sid = path[len("/radio/"):-len(".ts")]
                    b = server.broadcasts.get(sid)
                    if b is None:
                        self._send(404, "text/plain", b"unknown station\n", head)
                        return
                    if head:
                        self._send(200, "video/mp2t", None, True)
                        return
                    self._stream(b)
                    return
                self._send(404, "text/plain", b"not found\n", head)

            def _send(self, code, ctype, body, head):
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                if body is not None:
                    self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                if body and not head:
                    self.wfile.write(body)

            def _stream(self, b: Broadcast):
                self.send_response(200)
                self.send_header("Content-Type", "video/mp2t")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.flush()
                client = b.attach(self.address_string())
                if client is None:      # removed by a config reload just now
                    return
                try:
                    while not server._stop.is_set():
                        try:
                            chunk = client.q.get(timeout=1.0)
                        except queue.Empty:
                            continue
                        if chunk is None:
                            break
                        self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError, TimeoutError, OSError):
                    pass
                finally:
                    b.detach(client)

        self.httpd = ThreadingHTTPServer((self.host, self.port), Handler)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]   # resolves port 0 in tests
        threading.Thread(target=self.httpd.serve_forever, name="rs-http", daemon=True).start()
        threading.Thread(target=self._reaper, name="rs-reaper", daemon=True).start()
        log.info("Rad Streams server listening on %s:%d (%d stations)", self.host, self.port,
                 len(self.broadcasts))

    def _reaper(self) -> None:
        while not self._stop.wait(1.0):
            for b in list(self.broadcasts.values()):
                try:
                    b.reap()
                except Exception:
                    log.exception("reaper error")

    def stop(self) -> None:
        self._stop.set()
        if self.httpd:
            self.httpd.shutdown()
            self.httpd.server_close()
        for b in list(self.broadcasts.values()):
            b.stop_runner()

    def status(self) -> dict:
        return {"port": self.port, "stations": [b.status() for b in self.broadcasts.values()]}


def main(argv=None) -> int:
    """``python -m rs.server --stations stations.txt`` for testing outside Dispatcharr."""
    import argparse
    import os
    import sys

    from .config import RESOLUTIONS, parse_stations

    ap = argparse.ArgumentParser(prog="rs.server")
    ap.add_argument("--stations", required=True, help="file with 'Name | URL | City | #' lines")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--resolution", default="1920x1080", choices=sorted(RESOLUTIONS))
    ap.add_argument("--units", default="F", choices=["F", "C"])
    ap.add_argument("--data-dir", default=os.path.abspath("rs-data"))
    ap.add_argument("--linger", type=float, default=10.0)
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    parsed = parse_stations(open(a.stations, encoding="utf-8").read())
    for e in parsed.errors:
        log.warning("%s", e)
    settings = RenderSettings.from_resolution(a.resolution, units=a.units, data_dir=a.data_dir)
    srv = RadioServer(parsed.stations, settings, host=a.host, port=a.port, linger_seconds=a.linger)
    srv.start()
    for s in parsed.stations:
        print(f"{s.name}: {srv.url_for(s)}", flush=True)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        srv.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

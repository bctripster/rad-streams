"""One station's renderer: data sources + compositor + encoder, paced in real time."""
from __future__ import annotations

import logging
import os
import random
import threading
import time
from typing import IO, Callable, Optional, Union

from . import geo, themes, weather as weather_mod
from .audio import BYTES_PER_SECOND, RadioSource
from .config import RenderSettings, Station
from .encoder import Encoder
from .images import ImageLibrary, Photo
from .render import CREDIT_SEP, Compositor, PanelInfo, fit_photo


log = logging.getLogger("rad_streams.station")

AUDIO_CHUNK_SECONDS = 0.05
WEATHER_STALE_SECONDS = 3 * 3600
GEOCODE_RETRY_SECONDS = 300
TOUCH_SECONDS = 6 * 3600   # how often a running station marks its photos as in use
SPLASH_SECONDS = 5.0     # the Rad Streams splash always shows this long when a station starts


class StationRunner:
    """Renders one station until ``stop()`` (or ``seconds`` elapse).

    ``output`` is a file path, ``"-"`` (read MPEG-TS from ``self.stdout``),
    or an open binary file object.
    """

    def __init__(self, station: Station, settings: RenderSettings,
                 output: Union[str, IO[bytes]], *, ffmpeg: str = "ffmpeg",
                 geocoder: Callable = geo.geocode, weather_fetch: Callable = weather_mod.fetch,
                 smooth: bool = True, log_path: Optional[str] = None):
        self.station = station
        self.s = settings
        self.geocoder = geocoder
        self.weather_fetch = weather_fetch
        self.place: Optional[geo.Place] = None
        self.theme = themes.parse(station.city)   # "Theme: Space": no city, no weather
        self.weather: Optional[weather_mod.Weather] = None
        self.weather_error: Optional[str] = None
        self.library: Optional[ImageLibrary] = None
        self.radio = RadioSource(station.stream_url, station.name, ffmpeg=ffmpeg)
        self.comp = Compositor((settings.width, settings.height), station.name,
                               pattern=settings.text_pattern, crossfade=settings.crossfade_seconds,
                               credit_seconds=settings.credit_seconds,
                               splash_dir=settings.cache_dir)
        self.encoder = Encoder(settings.width, settings.height, render_fps=settings.render_fps,
                               output_fps=settings.output_fps, video_kbps=settings.video_kbps,
                               audio_kbps=settings.audio_kbps, output=output, ffmpeg=ffmpeg,
                               smooth=smooth, log_path=log_path)
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._photos: list[Photo] = []
        self._photo_i = -1
        self._next_switch = 0.0
        self.started_at = 0.0
        self.frames_rendered = 0
        self.frames_repeated = 0
        self.error: Optional[str] = None

    @property
    def stdout(self):
        return self.encoder.stdout

    @property
    def stopping(self) -> bool:
        return self._stop.is_set()

    # ---- lifecycle -----------------------------------------------------
    def start(self) -> None:
        self.encoder.start()
        self.started_at = time.monotonic()
        # Video first: the placeholder frame goes out before any network call.
        self._spawn(self._video_loop, "video")
        self._spawn(self._audio_loop, "audio")
        self.radio.start()
        self._spawn(self._data_loop, "data")

    def stop(self) -> None:
        self._stop.set()
        self.radio.stop()
        for t in self._threads:
            t.join(timeout=3)
        self.encoder.stop()

    def run_for(self, seconds: float) -> None:
        self.start()
        try:
            self._stop.wait(seconds)
        finally:
            self.stop()

    def _spawn(self, fn, name: str) -> None:
        def wrapped():
            try:
                fn()
            except (BrokenPipeError, OSError) as exc:
                if not self._stop.is_set():
                    self.error = f"{name}: {exc}"
                    log.warning("%s: %s loop stopped: %s", self.station.name, name, exc)
                    self._fail()
            except Exception as exc:  # keep the failure visible in the log
                self.error = f"{name}: {exc}"
                log.exception("%s: %s loop crashed", self.station.name, name)
                self._fail()

        t = threading.Thread(target=wrapped, name=f"rs-{name}-{self.station.station_id}", daemon=True)
        self._threads.append(t)
        t.start()

    def _geocode(self, quiet: bool = False) -> bool:
        try:
            self.place = self.geocoder(self.station.city, self.s.cache_dir)
        except Exception as exc:
            log.warning("%s: geocoding %r failed: %s", self.station.name, self.station.city, exc)
        if self.place is None and not quiet:
            log.warning("%s: city %r not resolved; weather and nearby photos disabled for now",
                        self.station.name, self.station.city)
        return self.place is not None

    def _open_library(self) -> None:
        if self.theme:
            self.library = ImageLibrary(
                self.s.cache_dir, self.theme.key, self.theme.label, None, None,
                width=self.s.width, count=self.s.images_per_city, refresh_hours=self.s.image_refresh_hours,
                queries=self.theme.queries(), theme_filters=self.theme.filters())
        else:
            # "Name, Region" lets the photo search use qualified category/text queries,
            # and keys the cache, so stations naming one city the same way share it.
            city_name = self.place.label if self.place else self.station.city.strip()
            self.library = ImageLibrary(
                self.s.cache_dir, geo.slug(city_name), city_name,
                self.place.lat if self.place else None, self.place.lon if self.place else None,
                width=self.s.width, count=self.s.images_per_city, refresh_hours=self.s.image_refresh_hours,
                region=(self.place.admin1 or self.place.country) if self.place else None)
        try:
            self.library.touch()
            self._photos = self.library.photos()
            if self._photos and self.comp.on_splash:
                self._photos_ready()  # cached photos: replace the splash once the intro is over
            if self.library.is_stale():
                self.library.refresh_async()
        except Exception as exc:
            log.warning("%s: photo cache unavailable: %s", self.station.name, exc)

    def _fail(self) -> None:
        # Stop everything and end the encoder's output, so the server sees EOF
        # and tells viewers to reconnect, rather than leaving a frozen stream.
        self._stop.set()
        self.encoder.abort()

    # ---- data ----------------------------------------------------------
    def _data_loop(self) -> None:
        if not self.theme:
            self._geocode()
        self._open_library()
        next_weather = 0.0
        next_geocode = time.monotonic() + GEOCODE_RETRY_SECONDS
        next_weather = 0.0
        next_photo_check = 0.0
        next_touch = time.monotonic() + TOUCH_SECONDS
        while not self._stop.is_set():
            now = time.monotonic()
            if self.place is None and not self.theme and now >= next_geocode:
                # A failed lookup (network blip at start) shouldn't disable weather for the whole run.
                next_geocode = now + GEOCODE_RETRY_SECONDS
                if self._geocode(quiet=True):
                    self._open_library()
                    next_photo_check = 0.0
            if self.place and now >= next_weather:
                try:
                    self.weather = self.weather_fetch(self.place.lat, self.place.lon, self.s.units)
                    self.weather_error = None
                    next_weather = now + self.s.weather_refresh_minutes * 60
                except Exception as exc:
                    self.weather_error = str(exc)
                    log.warning("%s: weather fetch failed: %s", self.station.name, exc)
                    next_weather = now + 120
            if now >= next_photo_check:
                next_photo_check = now + 10
                try:
                    if now >= next_touch:
                        # A channel left running for weeks still counts as using its photos,
                        # even if every refresh fails, so the daily cleanup keeps them.
                        next_touch = now + TOUCH_SECONDS
                        self.library.touch()
                    fresh = self.library.photos()
                    # Compare as sets: _advance_photo reshuffles self._photos in place.
                    if {p.file for p in fresh} != {p.file for p in self._photos}:
                        self._photos = fresh
                        if self._photo_i < 0 or self.comp.on_splash:
                            self._photos_ready()  # show the first real photo right away
                    if self.library.is_stale():
                        self.library.refresh_async()
                except Exception as exc:   # photo trouble must never take the channel down
                    log.warning("%s: photo cache check failed: %s", self.station.name, exc)
                    next_photo_check = now + 60
            self._stop.wait(1.0)

    def panel_info(self) -> PanelInfo:
        city = self.theme.label if self.theme else self.place.label if self.place else self.station.city
        w = self.weather
        weather_line = None
        if w and time.time() - w.fetched_at < WEATHER_STALE_SECONDS:
            weather_line = w.summary()
        status = None
        elapsed = time.monotonic() - self.started_at
        if not self.radio.is_live and elapsed > 4:
            status = "Reconnecting to stream…" if self.radio.last_error else "Tuning in…"
        return PanelInfo(station=self.station.name, city=city, title=self.radio.title,
                         weather=weather_line, status=status,
                         weather_place=self.place.name if self.place else None,
                         themed=self.theme is not None)

    # ---- photos ----------------------------------------------------------
    def _photos_ready(self) -> None:
        """Photos just became available: switch to them after the start-up splash."""
        self._next_switch = self.started_at + SPLASH_SECONDS

    def _advance_photo(self, now: float) -> None:
        span = float(self.s.image_seconds)
        photo_img, credit = None, None
        intro = now < self.started_at + SPLASH_SECONDS
        photos = [] if intro else list(self._photos)
        if photos:
            for _ in range(len(photos)):
                self._photo_i = (self._photo_i + 1) % len(photos)
                if self._photo_i == 0 and len(photos) > 2:
                    random.shuffle(photos)
                    self._photos = photos
                p = photos[self._photo_i]
                try:
                    photo_img = fit_photo(p.file, self.comp.size)
                    credit = p.credit
                    break
                except Exception as exc:
                    log.warning("%s: could not open %s: %s", self.station.name, p.file, exc)
        if self.place:  # Open-Meteo's data license asks for attribution
            credit = f"{credit}{CREDIT_SEP}Weather: Open-Meteo.com" if credit else "Weather: Open-Meteo.com"
        self.comp.show(photo_img, credit, now, span)
        self._next_switch = self.started_at + SPLASH_SECONDS if intro and self._photos else now + span

    # ---- pacing ----------------------------------------------------------
    def _video_loop(self) -> None:
        fps = self.s.render_fps
        interval = 1.0 / fps
        k = 0
        last: Optional[bytes] = None
        while not self._stop.is_set():
            due = self.started_at + k * interval
            now = time.monotonic()
            if now < due:
                self._stop.wait(due - now)
                continue
            behind = now - due > interval  # more than a frame late: repeat, don't render
            if last is None or not behind:
                if now >= self._next_switch:
                    self._advance_photo(now)
                last = self.comp.frame(self.panel_info(), now).tobytes()
                self.frames_rendered += 1
            else:
                self.frames_repeated += 1
            self.encoder.write_video(last)
            k += 1

    def _audio_loop(self) -> None:
        chunk = int(BYTES_PER_SECOND * AUDIO_CHUNK_SECONDS) & ~3
        j = 0
        while not self._stop.is_set():
            due = self.started_at + j * AUDIO_CHUNK_SECONDS
            now = time.monotonic()
            if now < due:
                self._stop.wait(due - now)
                continue
            self.encoder.write_audio(self.radio.pcm.read(chunk))
            j += 1

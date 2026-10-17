"""Station list parsing and shared render settings."""
from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass, field
from typing import Optional

DEFAULT_DATA_DIR = "/data/rad_streams"

RESOLUTIONS = {
    "3840x2160": (3840, 2160),
    "2560x1440": (2560, 1440),
    "1920x1080": (1920, 1080),
    "1280x720": (1280, 720),
    "854x480": (854, 480),
}

TEXT_PATTERNS = ("cycle", "random", "corners")


@dataclass
class Station:
    name: str
    stream_url: str
    city: str
    channel_number: Optional[float] = None
    line_no: int = 0

    @property
    def station_id(self) -> str:
        """Stable, URL-safe id: a slug of the name plus a short hash.

        Derived from the name only, so editing the stream URL or city keeps
        the same route and Dispatcharr channel.
        """
        slug = re.sub(r"[^a-z0-9]+", "-", self.name.lower()).strip("-")[:32] or "station"
        digest = hashlib.sha1(self.name.strip().lower().encode("utf-8")).hexdigest()[:6]
        return f"{slug}-{digest}"


@dataclass
class ParseResult:
    stations: list[Station] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def parse_stations(text: str) -> ParseResult:
    """Parse ``Name | Stream URL | City | Channel # (optional)`` lines.

    Blank lines and lines starting with ``#`` are ignored. Duplicate names
    are reported and the later line is skipped, since the name is the id.
    """
    result = ParseResult()
    seen: set[str] = set()
    for line_no, raw in enumerate((text or "").splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) < 3 or len(parts) > 4:
            result.errors.append(
                f"line {line_no}: expected 'Name | Stream URL | City | Channel #', got {len(parts)} part(s)")
            continue
        name, url, city = parts[0], parts[1], parts[2]
        number_raw = parts[3] if len(parts) == 4 else ""
        if not name:
            result.errors.append(f"line {line_no}: missing station name")
            continue
        if not re.match(r"^https?://", url, re.I):
            result.errors.append(f"line {line_no}: stream URL must start with http:// or https://")
            continue
        if not city:
            result.errors.append(f"line {line_no}: missing city")
            continue
        number: Optional[float] = None
        if number_raw:
            try:
                number = float(number_raw)
                if number <= 0:
                    raise ValueError
            except ValueError:
                result.errors.append(f"line {line_no}: channel number '{number_raw}' is not a positive number")
                continue
        key = name.lower()
        if key in seen:
            result.errors.append(f"line {line_no}: duplicate station name '{name}' skipped")
            continue
        seen.add(key)
        result.stations.append(Station(name, url, city, number, line_no))
    return result


@dataclass
class RenderSettings:
    width: int = 1920
    height: int = 1080
    units: str = "F"                 # "F" or "C"
    video_kbps: int = 3000
    audio_kbps: int = 128
    render_fps: float = 4.0          # frames Pillow draws per second
    output_fps: int = 25             # frames ffmpeg emits per second
    image_seconds: int = 60          # time per photo
    crossfade_seconds: float = 2.0
    credit_seconds: float = 5.0      # photo credit shows this long after each change; 0 = always
    text_pattern: str = "cycle"
    images_per_city: int = 20
    image_refresh_hours: float = 12.0  # each refresh swaps in up to half new photos
    weather_refresh_minutes: float = 15.0
    data_dir: str = DEFAULT_DATA_DIR

    @classmethod
    def from_resolution(cls, resolution: str, **kw) -> "RenderSettings":
        w, h = RESOLUTIONS.get(resolution, RESOLUTIONS["1920x1080"])
        return cls(width=w, height=h, **kw)

    @property
    def cache_dir(self) -> str:
        return os.path.join(self.data_dir, "cache")

    @property
    def log_dir(self) -> str:
        return os.path.join(self.data_dir, "logs")

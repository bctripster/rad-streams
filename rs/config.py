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

# How each station's video is encoded. "software" is libx264; the others hand
# the H.264 encode to a GPU and fall back to software when it isn't usable.
VIDEO_ENCODERS = ("software", "vaapi", "qsv", "nvenc")

# Optional "key=value" columns after the Channel # column.
OPTION_KEYS = ("id", "logo")
MAX_ID_LENGTH = 100
MAX_LOGO_URL_LENGTH = 1000


@dataclass
class Station:
    name: str
    stream_url: str
    city: str
    channel_number: Optional[float] = None
    line_no: int = 0
    key: Optional[str] = None        # the optional "id=" column; the name when unset
    logo_url: Optional[str] = None   # the optional "logo=" column

    @property
    def id_source(self) -> str:
        """The text the station's id comes from: ``id=`` when given, else the name."""
        return (self.key or self.name).strip()

    @property
    def station_id(self) -> str:
        """Stable, URL-safe id: a slug of the name (or ``id=``) plus a short hash.

        Editing the stream URL or city keeps the same route and Dispatcharr
        channel. Renaming does too when the line has ``id=<old name>``: the id
        is derived from that text exactly as it was from the name, so adding
        it to an existing station changes nothing.
        """
        source = self.id_source
        slug = re.sub(r"[^a-z0-9]+", "-", source.lower()).strip("-")[:32] or "station"
        digest = hashlib.sha1(source.lower().encode("utf-8")).hexdigest()[:6]
        return f"{slug}-{digest}"


@dataclass
class ParseResult:
    stations: list[Station] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def _parse_option(item: str) -> Optional[tuple[str, str]]:
    """``"id=KEXP"`` -> ``("id", "KEXP")``; None when ``item`` isn't a known option."""
    m = re.match(r"^\s*([A-Za-z]+)\s*=(.*)$", item)
    if not m or m.group(1).lower() not in OPTION_KEYS:
        return None
    return m.group(1).lower(), m.group(2).strip()


def parse_stations(text: str) -> ParseResult:
    """Parse ``Name | Stream URL | City | Channel # (optional) | id=… | logo=…`` lines.

    Blank lines and lines starting with ``#`` are ignored. ``id=`` and
    ``logo=`` are optional, in any order after the City column; the Channel #
    column can be left out or empty before them. Duplicate ids (the name when
    there's no ``id=``) are reported and the later line is skipped.
    """
    result = ParseResult()
    seen: set[str] = set()
    for line_no, raw in enumerate((text or "").splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) < 3:
            result.errors.append(
                f"line {line_no}: expected 'Name | Stream URL | City | Channel #', got {len(parts)} part(s)")
            continue
        name, url, city = parts[0], parts[1], parts[2]
        number_raw = ""
        options: dict[str, str] = {}
        bad = None
        for pos, item in enumerate(parts[3:], start=4):
            opt = _parse_option(item)
            if opt is not None:
                if opt[0] in options:
                    bad = f"'{opt[0]}=' is given twice"
                    break
                options[opt[0]] = opt[1]
            elif pos == 4:
                number_raw = item          # the Channel # column (may be empty)
            elif item:
                bad = f"column {pos} '{item[:40]}' should be id=… or logo=…"
                break
        if bad:
            result.errors.append(f"line {line_no}: {bad}")
            continue
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
                # float() accepts "inf"/"nan"; neither is a channel number.
                if not number > 0 or number == float("inf"):
                    raise ValueError
            except ValueError:
                result.errors.append(f"line {line_no}: channel number '{number_raw}' is not a positive number")
                continue
        key = options.get("id")
        if key is not None and (not key or len(key) > MAX_ID_LENGTH):
            result.errors.append(f"line {line_no}: id= must be 1 to {MAX_ID_LENGTH} characters")
            continue
        logo = options.get("logo") or None
        if logo is not None and (not re.match(r"^https?://\S+$", logo, re.I)
                                 or len(logo) > MAX_LOGO_URL_LENGTH):
            result.errors.append(f"line {line_no}: logo= must be an http:// or https:// image URL")
            continue
        station = Station(name, url, city, number, line_no, key=key, logo_url=logo)
        dedupe = station.id_source.lower()
        if dedupe in seen:
            what = "id" if key else "station name"
            result.errors.append(f"line {line_no}: duplicate {what} '{station.id_source}' skipped")
            continue
        seen.add(dedupe)
        result.stations.append(station)
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
    video_encoder: str = "software"  # one of VIDEO_ENCODERS
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

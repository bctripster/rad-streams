"""Station list parsing and shared render settings."""
from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass, field
from typing import Any, Optional

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
# Dispatcharr's name columns hold 255 characters, and a stream is named
# "Rad Streams: <name> | Live from <city>", so both parts are limited here.
MAX_NAME_LENGTH = 120
MAX_CITY_LENGTH = 100

# "CPU Use" setting -> (frames per second ffmpeg emits, libx264 preset).
# "low" trades a little crossfade smoothness and sharpness for about a third less CPU.
CPU_MODES = {"normal": (25, "veryfast"), "low": (15, "superfast")}
X264_PRESETS = ("ultrafast", "superfast", "veryfast", "faster", "fast", "medium")
MAX_LOGO_URL_LENGTH = 1000
MAX_URL_LENGTH = 2000
# More stations than this is almost certainly a mistake (each one is a channel).
MAX_STATIONS = 500


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
    # Table only: one {"row": 1-based, "column": id, "problem": text} per bad
    # row, for the saved report, and how many rows had Skip ticked.
    cell_errors: list[dict] = field(default_factory=list)
    skipped: int = 0


def _parse_option(item: str) -> Optional[tuple[str, str]]:
    """``"id=KEXP"`` -> ``("id", "KEXP")``; None when ``item`` isn't a known option."""
    m = re.match(r"^\s*([A-Za-z]+)\s*=(.*)$", item)
    if not m or m.group(1).lower() not in OPTION_KEYS:
        return None
    return m.group(1).lower(), m.group(2).strip()


def _has_control_chars(text: str) -> bool:
    """True if ``text`` holds a control character (newline, tab, NUL, DEL...)."""
    return any(ord(ch) < 32 or ord(ch) == 127 for ch in text)


def check_station(name: str, url: str, city: str, number_raw: str = "",
                  key: Optional[str] = None, logo: Optional[str] = None,
                  option_suffix: str = "=") -> tuple[Optional[Station], Optional[tuple[str, str]]]:
    """Check one station's values; the one rule both station formats share.

    Returns ``(station, None)`` when every value is usable, else
    ``(None, (column, problem))`` naming the first bad column (one of
    ``COLUMN_IDS``) and why. ``option_suffix`` is how the id/logo options are
    written in the problem text: ``"id="`` for the old text box, ``"id"`` for
    the table. ``key`` / ``logo`` are None when not given at all; an empty
    ``key`` is a problem (the text box's ``id=`` with nothing after it).
    """
    if not name:
        return None, ("name", "missing station name")
    if len(name) > MAX_NAME_LENGTH:
        return None, ("name", f"station name is longer than {MAX_NAME_LENGTH} characters")
    if _has_control_chars(name):
        return None, ("name", "station name contains a control character")
    if (not re.match(r"^https?://\S+$", url, re.I) or len(url) > MAX_URL_LENGTH
            or _has_control_chars(url)):
        return None, ("url", "stream URL must start with http:// or https:// and have no spaces")
    if not city:
        return None, ("city", "missing city")
    if len(city) > MAX_CITY_LENGTH:
        return None, ("city", f"city is longer than {MAX_CITY_LENGTH} characters")
    if _has_control_chars(city):
        return None, ("city", "city contains a control character")
    number: Optional[float] = None
    if number_raw:
        try:
            number = float(number_raw)
            # float() accepts "inf"/"nan"; neither is a channel number.
            if not number > 0 or number == float("inf"):
                raise ValueError
        except ValueError:
            return None, ("number", f"channel number '{number_raw[:20]}' is not a positive number")
    if key is not None and (not key or len(key) > MAX_ID_LENGTH or _has_control_chars(key)):
        return None, ("id", f"id{option_suffix} must be 1 to {MAX_ID_LENGTH} characters")
    if logo is not None and (not re.match(r"^https?://\S+$", logo, re.I)
                             or len(logo) > MAX_LOGO_URL_LENGTH or _has_control_chars(logo)):
        return None, ("logo", f"logo{option_suffix} must be an http:// or https:// image URL")
    return Station(name, url, city, number, key=key, logo_url=logo or None), None


def _add_unique(result: ParseResult, seen: set, station: Station, where: str) -> None:
    """Append ``station`` unless its id was already used (then report ``where``)."""
    dedupe = station.id_source.lower()
    if dedupe in seen:
        what = "id" if station.key else "station name"
        result.errors.append(f"{where}: duplicate {what} '{station.id_source}' skipped")
        return
    seen.add(dedupe)
    result.stations.append(station)


def _split_line(line: str) -> tuple[Optional[dict], Optional[str]]:
    """Split one text-box line into its columns.

    Returns ``({"name", "url", "city", "number", "id"?, "logo"?}, None)``, or
    ``(None, why)`` when the line's shape is wrong. ``id``/``logo`` are present
    only when the line has them.
    """
    parts = [p.strip() for p in line.split("|")]
    if len(parts) < 3:
        return None, f"expected 'Name | Stream URL | City | Channel #', got {len(parts)} part(s)"
    cols: dict = {"name": parts[0], "url": parts[1], "city": parts[2], "number": ""}
    for pos, item in enumerate(parts[3:], start=4):
        opt = _parse_option(item)
        if opt is not None:
            if opt[0] in cols:
                return None, f"'{opt[0]}=' is given twice"
            cols[opt[0]] = opt[1]
        elif pos == 4:
            cols["number"] = item          # the Channel # column (may be empty)
        elif item:
            return None, f"column {pos} '{item[:40]}' should be id=… or logo=…"
    return cols, None


def parse_stations(text: str) -> ParseResult:
    """Parse ``Name | Stream URL | City | Channel # (optional) | id=… | logo=…`` lines.

    This is the pre-0.6 text-box format. It is still read when a saved setup
    has no stations table yet (see ``rows_from_text``). Blank lines and lines
    starting with ``#`` are ignored. ``id=`` and ``logo=`` are optional, in
    any order after the City column; the Channel # column can be left out or
    empty before them. Duplicate ids (the name when there's no ``id=``) are
    reported and the later line is skipped.
    """
    result = ParseResult()
    seen: set[str] = set()
    for line_no, raw in enumerate((text or "").splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        cols, why = _split_line(line)
        if cols is None:
            result.errors.append(f"line {line_no}: {why}")
            continue
        station, bad = check_station(cols["name"], cols["url"], cols["city"], cols["number"],
                                     key=cols.get("id"), logo=cols.get("logo"))
        if station is None:
            result.errors.append(f"line {line_no}: {bad[1]}")
            continue
        station.line_no = line_no
        _add_unique(result, seen, station, f"line {line_no}")
    return result


# ---- the stations table (Dispatcharr 0.34 "table" setting) ------------------
# Column ids are what Dispatcharr stores in each row; labels are what the table
# header and every problem message show. Every column is a "string" column
# except Skip: Dispatcharr's number cell sends "" when it is cleared, which its
# own type check refuses on save, so the optional Channel # is checked here.
TABLE_COLUMNS = (
    {"id": "name", "label": "Name", "type": "string"},
    {"id": "url", "label": "Stream URL", "type": "string"},
    {"id": "city", "label": "City or Theme", "type": "string"},
    {"id": "number", "label": "Channel #", "type": "string"},
    {"id": "id", "label": "Keep ID", "type": "string"},
    {"id": "logo", "label": "Logo URL", "type": "string"},
    {"id": "skip", "label": "Skip", "type": "boolean"},
)
COLUMN_IDS = tuple(c["id"] for c in TABLE_COLUMNS)
COLUMN_LABELS = {c["id"]: c["label"] for c in TABLE_COLUMNS}
TEXT_COLUMNS = ("name", "url", "city", "number", "id", "logo")
# A cell longer than this is refused before any other check, so one huge
# value can't bloat the report or the service config.
MAX_CELL_LENGTH = 2000


def _cell(row: dict, col: str) -> tuple[str, Optional[str]]:
    """A row's cell as stripped text, or ``("", why)`` when it isn't text.

    Dispatcharr stores what the browser sent: strings normally, a number if
    the row came from the API, nothing at all for a cell never typed in.
    """
    value = row.get(col)
    if value is None:
        return "", None
    if isinstance(value, bool):          # bool is an int: check it first
        return "", "must be text, not true/false"
    if isinstance(value, int):
        return str(value), None
    if isinstance(value, float):
        return (f"{value:g}" if value == value else "nan"), None
    if not isinstance(value, str):
        return "", "must be text"
    if len(value) > MAX_CELL_LENGTH:
        return "", f"is longer than {MAX_CELL_LENGTH} characters"
    return value.strip(), None


def _row_label(index: int, name: str) -> str:
    """``row 3 (KEXP)``: 1-based, as people count table rows."""
    return f"row {index + 1}" + (f" ({name[:40]})" if name else "")


def parse_rows(rows: Any) -> ParseResult:
    """Check the stations table cell by cell and build the station list.

    Every problem names its row and column, e.g. ``row 3 (KEXP), Stream URL:
    stream URL must start with http:// or https://``. Rows with Skip ticked
    and rows with every cell empty (a fresh "Add row") are left out without
    a problem. Duplicate ids are reported and the later row is skipped, as
    in the text format.
    """
    result = ParseResult()
    if rows is None:
        return result
    if not isinstance(rows, list):
        result.errors.append("the Stations table is not a list of rows")
        return result
    seen: set[str] = set()
    for index, row in enumerate(rows):
        if len(result.stations) >= MAX_STATIONS:
            result.errors.append(f"only the first {MAX_STATIONS} stations are used")
            break
        if not isinstance(row, dict):
            result.errors.append(f"{_row_label(index, '')}: is not a table row")
            continue
        if row.get("skip") is True:
            result.skipped += 1
            continue
        cells: dict[str, str] = {}
        bad: Optional[tuple[str, str]] = None
        for col in TEXT_COLUMNS:
            cells[col], why = _cell(row, col)
            if why and bad is None:
                bad = (col, f"{COLUMN_LABELS[col]} {why}")
        if not any(cells.values()) and bad is None:
            continue                     # an empty row: nothing typed in yet
        where = _row_label(index, cells["name"])
        if bad is None:
            station, bad = check_station(cells["name"], cells["url"], cells["city"], cells["number"],
                                         key=cells["id"] or None, logo=cells["logo"] or None,
                                         option_suffix="")
        else:
            station = None
        if station is None:
            result.errors.append(f"{where}, {COLUMN_LABELS[bad[0]]}: {bad[1]}")
            result.cell_errors.append({"row": index + 1, "column": bad[0], "problem": bad[1]})
            continue
        station.line_no = index + 1
        _add_unique(result, seen, station, where)
    return result


@dataclass
class Migration:
    """What ``rows_from_text`` made of an old text-box station list."""
    rows: list[dict] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def _blank_row() -> dict:
    return {"name": "", "url": "", "city": "", "number": "", "id": "", "logo": "", "skip": False}


def rows_from_text(text: str) -> Migration:
    """Turn the old ``Name | URL | City | # | id= | logo=`` text into table rows.

    Nothing typed is thrown away silently: a station line becomes a row even
    when a value in it is wrong (Sync then names the bad cell), a line that
    was commented out with ``#`` becomes a row with Skip ticked, and a line
    whose shape can't be read is kept whole in the Name cell with a note.
    Plain comments (``# my stations``) are dropped. Never raises: it runs while
    Dispatcharr builds the settings form.
    """
    out = Migration()
    for line_no, raw in enumerate((text or "").splitlines(), start=1):
        line = raw.strip()
        if not line:
            continue
        skip = line.startswith("#")
        if skip:
            line = line.lstrip("#").strip()
            parts = [p.strip() for p in line.split("|")]
            if len(parts) < 3 or not re.match(r"^https?://", parts[1], re.I):
                continue                 # a comment, not a commented-out station
        cols, why = _split_line(line)
        row = _blank_row()
        if cols is None:
            row["name"] = line[:MAX_CELL_LENGTH]
            out.notes.append(f"line {line_no} could not be split into columns ({why}); "
                             "it is in the Name cell of its row")
        else:
            for col in TEXT_COLUMNS:
                row[col] = (cols.get(col) or "")[:MAX_CELL_LENGTH]
        row["skip"] = skip
        out.rows.append(row)
    return out


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
    x264_preset: str = "veryfast"    # software encoder speed, one of X264_PRESETS
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

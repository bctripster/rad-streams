"""Scenic city photos from Wikimedia Commons, cached on disk.

Search order, leaning towards scenery (skylines, parks, water, mountains):
  1. Commons "Quality images" within a radius of the city
  2. Scenic photos within that radius
  3. Photos in the city's category ("Portland,_Maine")
  4. Any freely licensed photo within that radius, then a text search for
     the full city name
  5. Only if the city is still short: scenic and quality photos from the
     wider area, then from the state/province (or country) by name
Candidates are then ranked: scenic subjects first, then by tier.
A theme channel ("Theme: Space", see themes.py) uses its own searches instead.

Only CC0 / public domain / CC BY / CC BY-SA photos are kept (no NC or ND,
because the renderer crops and scales them), landscape and at least
``MIN_WIDTH`` wide. Each kept photo stores its author and license for the
on-screen credit.
"""
from __future__ import annotations

import hashlib
import html
import logging
import os
import random
import re
import threading
import time
from dataclasses import dataclass
from typing import Callable, Iterable, Optional

from PIL import Image

from . import nasa, net

COMMONS_API = "https://commons.wikimedia.org/w/api.php"
MIN_WIDTH = 1280
MIN_ASPECT = 1.25
MAX_ASPECT = 2.4
RADIUS_KM = 12
REGION_RADIUS_KM = 100

# Words (in titles, descriptions and categories) that suggest a scenic or
# nature shot. Used both in searches and to rank what comes back.
SCENIC_WORDS = ("skyline", "panorama", "cityscape", "view", "sunset", "sunrise", "dusk", "landscape",
                "park", "lake", "river", "beach", "coast", "mountain", "harbor", "harbour", "bay",
                "waterfront", "falls", "waterfall", "forest", "nature", "valley", "island", "aerial",
                "autumn", "snow", "night")
_SCENIC = re.compile(r"\b(" + "|".join(SCENIC_WORDS) + r"|mountains|lakes|parks|beaches|landscapes|"
                     r"rivers|coastline|scenery|vista|lookout|trail|garden|gardens|bridge|"
                     r"fall foliage|wilderness|national park|provincial park|state park)\b", re.I)
_SCENIC_QUERY = " OR ".join(("skyline", "panorama", "landscape", "sunset", "park", "lake", "river",
                             "beach", "mountain", "harbor", "waterfront", "nature"))

_BAD_TITLE = re.compile(
    r"\b(map|maps|logo|flag|coat of arms|seal|diagram|chart|plan|plaque|sign|signs|"
    r"document|letter|scan|poster|stamp|screenshot|graph|locator|svg|icon|"
    r"portrait|headshot|selfie|interior|inside|cemetery|mausoleum|grave|gravestone|tomb|"
    r"restaurant|menu|food|meal|convention|conference|ceremony|meeting|rally|protest|"
    r"band|concert|speech|award|museum exhibit|"
    r"card|cards|postcard|catalog|catalogue|scanned|archive|archives|record|newspaper|manuscript|"
    r"model|painting|drawing|engraving|lithograph|illustration|sketch|blueprint|birds eye|bird s eye|"
    r"annotated|smartphone|phone|damage|damaged|dead|stamps|carved|carving|relief|reliefs|sculpture)\b",
    re.I)
# On a scenery theme ("Theme: Forest"), titles that name a habitat but show something
# else: animals called after it ("forest sylph", "mountain gorilla"), plants, mushrooms,
# space hardware, and Mars. Too common in place names ("Fox River") to use for cities.
_THEME_OFF_TOPIC = re.compile(
    r"\b(gorilla|giraffe|wildebeest|chameleon|camel|coati|onagers?|pika|fox|deer|elk|moose|bear|wolf|"
    r"lion|zebra|antelope|monkey|lemur|heron|egret|warbler|finch|weaver|sylph|eagle|owl|bird|birds|"
    r"wattle eye|flower|flowers|mushroom|boletus|fungus|fungi|orchid|rudbeckia|shepherd|seller|"
    r"antenna|committee|observatory|satellites?|spoke|workshop|entrance|presentation|expo|spaceship)\b", re.I)
_PLANETARY_ID = re.compile(r"^PIA\d+", re.I)        # NASA planetary photos (Mars, moons)
# Animal close-ups whose categories don't say so (words rarely found in place names).
# Fine on a theme channel ("Theme: Wildlife"), so only city searches use it.
_ANIMAL_TITLE = re.compile(r"\b(grasshopper|insect|insects|butterfly|moth|beetle|dragonfly|spider|"
                           r"caterpillar|cottontail|squirrel|chipmunk|lizard|snake|frog|toad)\b", re.I)
# Commons categories that mark scans, documents and artwork rather than photos of the place.
_BAD_CATEGORY = re.compile(
    r"\b(maps?|scans?|scanned|documents?|archives?|archival|postcards?|cards|catalogs?|"
    r"manuscripts?|newspapers?|letters|drawings?|paintings?|engravings?|lithographs?|"
    r"illustrations?|bird's-eye views?|scale models?|architectural models?|blueprints?|"
    r"plans of|texts?)\b", re.I)
# Astronaut/shuttle photos of Earth are geotagged to whatever is below, so they
# turn up in "nearby" searches for small towns.
_ORBITAL_TITLE = re.compile(r"^(ISS|STS)[-_ ]?\d+[A-Z]?[-_ ]E[-_ ]?\d+|\bfrom (the )?(ISS|space)\b|"
                            r"\b(astronaut|expedition \d+)\b", re.I)
_ORBITAL_CATEGORY = re.compile(r"international space station|\bISS\b|space shuttle|earth observation|"
                               r"astronaut photograph|photos? (taken )?from space|earth from space|"
                               r"\bSTS-\d+", re.I)
# Close-ups of single plants and animals, zoos and farms: nature, but not scenery.
_CLOSEUP_CATEGORY = re.compile(r"\b(flora|fauna|taxa|plants|flowers|insects|birds|animals|mammals|"
                               r"reptiles|fungi|cacti|macro photography|close-ups?|zoos?|"
                               r"alligator farms?|botanical specimens?)\b", re.I)

# Commons serves these widths as cached thumbnails; others (and originals) get rate limited.
THUMB_WIDTHS = (1280, 1920)
_FREE_LICENSE = re.compile(r"^(cc0|cc[- ]by(?:[- ]sa)?\b|public domain|pd\b|pdm)", re.I)
_NONFREE = re.compile(r"\b(nc|nd|non[- ]?commercial|no[- ]?deriv)", re.I)

log = logging.getLogger("rad_streams.images")


@dataclass
class Photo:
    file: str          # absolute path on disk
    title: str         # Commons file title, without "File:"
    artist: str
    license: str
    page: str          # description page (Commons or NASA)
    source: str = "Wikimedia Commons"

    @property
    def credit(self) -> str:
        who = self.artist or "Unknown author"
        return f"Photo: {who} / {self.license} / {self.source}"


def clean_text(raw: str, limit: int = 60) -> str:
    text = html.unescape(re.sub(r"<[^>]+>", " ", raw or ""))
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


_TRACKING_CATEGORY = re.compile(r"^(pages|files|media|images|photos) (with|from|by|uploaded)|"
                                r"uploaded|location|self-published|^cc[- ]|license", re.I)


def _content_categories(meta: dict) -> list[str]:
    """The file's topical categories, leaving out tracking ones like "Pages with maps"."""
    raw = (meta.get("Categories") or {}).get("value", "")
    cats = [c.replace("_", " ").strip() for c in raw.split("|")]
    return [c for c in cats if c and not _TRACKING_CATEGORY.search(c)]


def thumb_width(frame_width: int) -> int:
    """The standard thumbnail width to ask for: 1280 up to 720p, 1920 above (4K upscales)."""
    return THUMB_WIDTHS[0] if frame_width <= THUMB_WIDTHS[0] else THUMB_WIDTHS[1]


def title_ok(title: str, theme: bool = False, animals: bool = False, planetary: bool = False) -> bool:
    """The title checks shared by every photo source."""
    plain_title = re.sub(r"[_\-.]", " ", title)
    if _BAD_TITLE.search(plain_title) or (not animals and _ANIMAL_TITLE.search(plain_title)):
        return False
    if theme and not animals and _THEME_OFF_TOPIC.search(plain_title):
        return False
    return not (theme and not planetary and _PLANETARY_ID.match(title))


def acceptable(page: dict, thumb: int = THUMB_WIDTHS[1], theme: bool = False,
               animals: bool = False, planetary: bool = False) -> Optional[dict]:
    """Return the useful fields of a Commons API page if it passes the filters.

    ``theme`` relaxes the filters that only make sense for a city, so photos from
    orbit suit "Theme: Earth". Animals and close-ups stay out unless ``animals``
    ("Theme: Wildlife", or a theme the user typed). ``planetary`` lets NASA's
    PIA planetary photos into "Theme: Space".
    """
    info = (page.get("imageinfo") or [None])[0]
    if not info:
        return None
    title = page.get("title", "").removeprefix("File:")
    if info.get("mime") not in ("image/jpeg", "image/png"):
        return None
    w, h = int(info.get("width") or 0), int(info.get("height") or 0)
    if w < MIN_WIDTH or h <= 0 or not (MIN_ASPECT <= w / h <= MAX_ASPECT):
        return None
    # Only files wider than the thumbnail: otherwise Commons hands back the
    # original, and original downloads are rate limited (HTTP 429).
    if w <= thumb or not info.get("thumburl"):
        return None
    if not title_ok(title, theme, animals, planetary):
        return None
    meta = info.get("extmetadata") or {}
    cats = _content_categories(meta)
    raw_cats = (meta.get("Categories") or {}).get("value", "").replace("_", " ")
    if not theme and (_ORBITAL_TITLE.search(title) or _ORBITAL_CATEGORY.search(raw_cats)):  # "Photos by the ISS"
        return None
    if any(_BAD_CATEGORY.search(c) or (not animals and _CLOSEUP_CATEGORY.search(c)) for c in cats):
        return None
    lic = clean_text((meta.get("LicenseShortName") or {}).get("value", ""), 40)
    if not _FREE_LICENSE.match(lic) or _NONFREE.search(lic):
        return None
    about = " ".join([re.sub(r"[_\-.]", " ", title), " ".join(cats),
                      clean_text((meta.get("ImageDescription") or {}).get("value", ""), 400)])
    return {
        "scenic": theme or bool(_SCENIC.search(about)),   # a theme's photos rank by tier alone
        "title": title,
        "width": w,
        "url": info["thumburl"],
        "artist": clean_text((meta.get("Artist") or {}).get("value", "")),
        "license": lic,
        "page": info.get("descriptionurl", ""),
    }


def _search(query: str, width: int, limit: int = 50) -> list[dict]:
    params = {
        "action": "query", "format": "json", "formatversion": 2,
        "generator": "search", "gsrnamespace": 6, "gsrlimit": limit,
        "gsrsearch": query,
        "prop": "imageinfo", "iiprop": "url|size|mime|extmetadata",
        "iiurlwidth": width,
        "iiextmetadatafilter": "LicenseShortName|Artist|Categories|ImageDescription",
    }
    data = net.get_json(COMMONS_API, params)
    return (data.get("query") or {}).get("pages") or []


def search_queries(city_name: str, lat: Optional[float], lon: Optional[float]) -> list[tuple[str, str]]:
    """City tiers: (tier label, CirrusSearch query) pairs, best first.

    ``city_name`` may carry its region ("Portland, Maine"); Commons names city
    categories that way, and a bare name in a text search finds other cities.
    """
    name, _, region = (p.strip() for p in city_name.partition(","))
    full = f"{name}, {region}" if region else name
    queries = []
    near = f"nearcoord:{RADIUS_KM}km,{lat:.4f},{lon:.4f}" if lat is not None and lon is not None else ""
    if near:
        queries.append(("quality-nearby", f"filetype:bitmap {near} incategory:Quality_images"))
        queries.append(("scenic-nearby", f"filetype:bitmap {near} ({_SCENIC_QUERY})"))
    queries.append(("category", f'filetype:bitmap incategory:"{full.replace(" ", "_")}"'))
    if near:
        queries.append(("nearby", f"filetype:bitmap {near}"))
    queries.append(("text", f'filetype:bitmap "{full}"'))
    return queries


def region_queries(region: Optional[str], lat: Optional[float], lon: Optional[float]
                   ) -> list[tuple[str, str]]:
    """Fallback tiers for a city with too few photos: the wider area, then the state/province."""
    queries = []
    if lat is not None and lon is not None:
        wide = f"nearcoord:{REGION_RADIUS_KM}km,{lat:.4f},{lon:.4f}"
        queries.append(("region-quality", f"filetype:bitmap {wide} incategory:Quality_images"))
        queries.append(("region-scenic", f"filetype:bitmap {wide} ({_SCENIC_QUERY})"))
    if region:
        # By category, not text: a text match on "Texas" also finds "Texas Park" in Spain.
        r = region.replace(" ", "_")
        cats = "|".join(f'"{c}_of_{r}"' for c in ("Landscapes", "Nature", "Mountains", "Lakes", "Rivers"))
        queries.append(("region-name", f"filetype:bitmap incategory:{cats}|\"Parks_in_{r}\""))
    return queries


TIER_ORDER = {"quality-nearby": 0, "scenic-nearby": 1, "category": 2, "nearby": 3, "text": 4,
              "region-quality": 5, "region-scenic": 6, "region-name": 7,
              "theme-featured-title": 0, "theme-quality-title": 1, "theme-text-title": 2,
              "theme-featured": 3, "theme-quality": 4, "theme-text": 5,
              "nasa-title": 1}


def rank(candidates: list[dict], shuffle: bool = True) -> list[dict]:
    """Scenic shots first, then the better tiers; shuffled within that, for variety."""
    out = list(candidates)
    if shuffle:
        random.shuffle(out)
    out.sort(key=lambda c: (not c.get("scenic"), TIER_ORDER.get(c.get("tier"), 9)))
    return out


def rotate(ranked: list[dict], have: set[str], count: int) -> list[dict]:
    """Order ranked candidates so a refresh swaps in new photos, not the same set again.

    Up to half of ``count`` comes from photos not already cached (scenic ones
    when there are any), then the cached ones, then any other new ones.
    """
    if not have:
        return ranked
    fresh = [c for c in ranked if photo_filename(c["title"]) not in have]
    old = [c for c in ranked if photo_filename(c["title"]) in have]
    scenic = [c for c in fresh if c.get("scenic")]
    first = (scenic if scenic else fresh)[:max(1, count // 2)]
    taken = {c["title"] for c in first}
    return first + old + [c for c in fresh if c["title"] not in taken]


def _author_key(artist: str) -> str:
    who = re.sub(r"\s+", " ", (artist or "").strip().lower())
    # "NASA Johnson Space Center / Expedition 68 crew" and "NASA/JSC" are one source.
    return "nasa" if who.startswith("nasa") else who


def _series_key(title: str) -> str:
    """The first words of a title, without numbers: "Earth at Night 2012 (SVS...)" -> "earth at night"."""
    words = re.findall(r"[^\W\d_]+", title.lower())
    return " ".join(words[:3])


def vary_authors(ordered: list[dict], count: int) -> list[dict]:
    """Keep the order, but move a photographer's (or a series') photos beyond the first few to the end.

    One prolific uploader can otherwise fill a whole set, often with a series
    of near-identical shots; their extras still fill in if the set is short.
    """
    cap = max(3, count // 6)
    seen: dict[tuple[str, str], int] = {}
    first, extra = [], []
    for c in ordered:
        keys = [("a", _author_key(c.get("artist", ""))), ("s", _series_key(c.get("title", "")))]
        keys = [k for k in keys if k[1]]
        over = any(seen.get(k, 0) >= cap for k in keys)
        for k in keys:
            seen[k] = seen.get(k, 0) + 1
        (extra if over else first).append(c)
    return first + extra


def find_candidates(city_name: str, lat: Optional[float], lon: Optional[float], width: int,
                    want: int, search: Optional[Callable[[str, int], list[dict]]] = None,
                    region: Optional[str] = None,
                    queries: Optional[list[tuple[str, str]]] = None,
                    theme_filters: Optional[dict] = None,
                    nasa_search: Optional[Callable[[str], list[dict]]] = None) -> tuple[list[dict], list[str]]:
    """Collect acceptable photos across tiers. Returns (candidates, tiers used).

    ``region`` (state, province or country) is searched only when the city
    itself doesn't have ``want`` usable photos. ``queries`` replaces the city
    searches entirely, for a theme channel (see themes.py); ``theme_filters``
    are its options for ``acceptable`` (animals, planetary). Tiers named
    "nasa..." search the NASA Image Library instead of Commons.
    """
    search = search or _search
    nasa_search = nasa_search or nasa.search
    opts = theme_filters or {}
    thumb = thumb_width(width)
    found: dict[str, dict] = {}
    tiers: list[tuple[str, int]] = []
    theme = queries is not None
    tiers_to_try = queries if theme else search_queries(city_name, lat, lon) + region_queries(region, lat, lon)
    for tier, query in tiers_to_try:
        # The wider area fills in when the city has too few *scenic* shots;
        # plenty of so-so nearby photos shouldn't keep it out.
        if tier.startswith("region") and sum(1 for c in found.values() if c["scenic"]) >= want:
            break
        from_nasa = tier.startswith("nasa")
        try:
            pages = nasa_search(query) if from_nasa else search(query, thumb)
        except Exception as exc:  # one failing tier shouldn't stop the others
            log.warning("%s search failed (%s): %s", "NASA" if from_nasa else "Commons", tier, exc)
            continue
        added = 0
        for page in pages:
            if from_nasa:
                item = page if title_ok(page["title"], theme, **opts) else None
            else:
                item = acceptable(page, thumb, theme=theme, **opts)
            if item and item["title"] not in found:
                item["tier"] = tier
                found[item["title"]] = item
                added += 1
        if added:
            tiers.append((tier, added))
        # Two to three times the wanted count leaves room for variety.
        if len(found) >= want * 3:
            break
    # One "tier:count" per tier, even when a tier ran several queries (theme channels).
    merged: dict[str, int] = {}
    for tier, added in tiers:
        merged[tier] = merged.get(tier, 0) + added
    return list(found.values()), [f"{t}:{n}" for t, n in merged.items()]


# Shared by every ImageLibrary in the process: two stations in one city use the
# same directory, so their refreshes must not run at once.
_DIR_LOCKS: dict[str, threading.Lock] = {}
_REFRESHING: set[str] = set()
_RETRY_AT: dict[str, tuple[float, float]] = {}    # dir -> (next attempt, current backoff)
_REGISTRY_LOCK = threading.Lock()
RETRY_MIN, RETRY_MAX = 300.0, 3600.0               # after a failed refresh
MAX_DOWNLOAD_BYTES = 40 * 1024 * 1024


NASA_MIN_ASPECT = 1.1       # many space photos are near-square; the crop to 16:9 still works


def _usable_size(path: str) -> bool:
    """A downloaded file big and wide enough to fill the frame (for sources without sizes)."""
    try:
        with Image.open(path) as im:
            w, h = im.size
    except (OSError, ValueError):
        return False
    return w >= MIN_WIDTH and h > 0 and NASA_MIN_ASPECT <= w / h <= MAX_ASPECT


def _dir_lock(path: str) -> threading.Lock:
    with _REGISTRY_LOCK:
        return _DIR_LOCKS.setdefault(path, threading.Lock())


def photo_filename(title: str) -> str:
    """A stable, unique file name for a Commons title (non-Latin titles included)."""
    ext = os.path.splitext(title)[1].lower()
    if ext not in (".jpg", ".jpeg", ".png"):
        ext = ".jpg"
    return hashlib.sha1(title.encode("utf-8")).hexdigest()[:16] + ext


class ImageLibrary:
    """Keeps ~N photos for one city on disk and swaps some for new ones every few hours."""

    def __init__(self, cache_dir: str, city_key: str, city_name: str,
                 lat: Optional[float], lon: Optional[float], *, width: int = 1920,
                 count: int = 10, refresh_hours: float = 4.0, region: Optional[str] = None,
                 queries: Optional[list[tuple[str, str]]] = None, theme_filters: Optional[dict] = None):
        self.dir = os.path.join(cache_dir, "images", city_key)
        self.index_path = os.path.join(self.dir, "index.json")
        self.city_name, self.lat, self.lon = city_name, lat, lon
        self.region = region
        self.queries = queries       # a theme channel's searches, instead of the city's
        self.theme_filters = theme_filters
        self.width, self.count = width, count
        self.refresh_seconds = refresh_hours * 3600
        self._lock = _dir_lock(self.dir)
        self.last_error: Optional[str] = None

    def touch(self) -> None:
        """Record that a station is using this city (the cleanup removes unused ones)."""
        try:
            os.utime(self.index_path)
        except OSError:
            pass

    # ---- reading -------------------------------------------------------
    def _index(self) -> dict:
        return net.read_json(self.index_path) or {}

    def photos(self) -> list[Photo]:
        out = []
        for e in self._index().get("images", []):
            path = os.path.join(self.dir, e["file"])
            if os.path.exists(path):
                out.append(Photo(path, e["title"], e.get("artist", ""), e.get("license", ""),
                                 e.get("page", ""), e.get("source") or "Wikimedia Commons"))
        return out

    def is_stale(self) -> bool:
        idx = self._index()
        if idx.get("city") and idx["city"] != self.city_name:
            return True  # the city resolved differently (e.g. a geocoding fix); refetch now
        if idx.get("images") and idx.get("want", 0) < self.count and \
                time.time() - idx.get("fetched_at", 0) > 600:
            return True  # cached before the photo count went up; top it up (at most every 10 min)
        return not idx.get("images") or time.time() - idx.get("fetched_at", 0) > self.refresh_seconds

    # ---- refreshing ----------------------------------------------------
    def refresh(self, force: bool = True) -> int:
        """Fetch a new set. Keeps the old set if nothing usable comes back.

        Returns the number of photos now cached. With ``force=False`` it does
        nothing if another station refreshed this city meanwhile.
        """
        with self._lock:
            if not force and not self.is_stale():
                return len(self.photos())
            candidates, tiers = find_candidates(self.city_name, self.lat, self.lon,
                                                self.width, self.count, region=self.region,
                                                queries=self.queries, theme_filters=self.theme_filters)
            if not candidates:
                self.last_error = "no usable photos found on Wikimedia Commons"
                log.warning("%s: %s", self.city_name, self.last_error)
                self._failed()
                return len(self.photos())
            have = {e["file"] for e in self._index().get("images", [])
                    if os.path.exists(os.path.join(self.dir, e["file"]))}
            candidates = vary_authors(rotate(rank(candidates), have, self.count), self.count)
            os.makedirs(self.dir, exist_ok=True)
            kept: list[dict] = []
            for c in candidates:
                if len(kept) >= self.count:
                    break
                fname = photo_filename(c["title"])
                path = os.path.join(self.dir, fname)
                try:
                    if not os.path.exists(path):
                        self._download(c["url"], path)
                        if c.get("check_size") and not _usable_size(path):
                            os.unlink(path)   # its source didn't say how big it is
                            log.info("%s: skipped %s (too small or not landscape)", self.city_name, c["title"])
                            continue
                except Exception as exc:
                    log.warning("download failed for %s: %s", c["title"], exc)
                    continue
                kept.append({"file": fname, "title": c["title"], "artist": c["artist"],
                             "license": c["license"], "page": c["page"], "tier": c["tier"],
                             "source": c.get("source", "Wikimedia Commons")})
            if not kept:
                self.last_error = "photo downloads failed"
                self._failed()
                return len(self.photos())
            net.write_json(self.index_path, {"fetched_at": time.time(), "city": self.city_name,
                                             "want": self.count, "tiers": tiers, "images": kept})
            self._prune({k["file"] for k in kept} | {"index.json"})
            with _REGISTRY_LOCK:
                _RETRY_AT.pop(self.dir, None)
            self.last_error = None
            log.info("%s: cached %d photos (%s)", self.city_name, len(kept), ", ".join(tiers))
            return len(kept)

    def _failed(self) -> None:
        """Back off before searching again (5 min, doubling to an hour)."""
        with _REGISTRY_LOCK:
            _, prev = _RETRY_AT.get(self.dir, (0.0, 0.0))
            wait = min(RETRY_MAX, max(RETRY_MIN, prev * 2))
            _RETRY_AT[self.dir] = (time.time() + wait, wait)

    def refresh_async(self) -> None:
        with _REGISTRY_LOCK:
            if self.dir in _REFRESHING or time.time() < _RETRY_AT.get(self.dir, (0.0, 0.0))[0]:
                return
            _REFRESHING.add(self.dir)

        def run():
            try:
                self.refresh(force=False)
            except Exception as exc:
                self.last_error = str(exc)
                log.warning("photo refresh failed for %s: %s", self.city_name, exc)
                self._failed()
            finally:
                with _REGISTRY_LOCK:
                    _REFRESHING.discard(self.dir)

        threading.Thread(target=run, name=f"rs-images-{self.city_name}", daemon=True).start()

    def _download(self, url: str, path: str) -> None:
        r = net.session().get(url, timeout=net.TIMEOUT, stream=True)
        r.raise_for_status()
        tmp = f"{path}.{os.getpid()}-{threading.get_ident()}.part"
        size = 0
        try:
            with open(tmp, "wb") as f:
                for chunk in r.iter_content(65536):
                    size += len(chunk)
                    if size > MAX_DOWNLOAD_BYTES:
                        raise ValueError("image too large")
                    f.write(chunk)
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def _prune(self, keep: Iterable[str]) -> None:
        keep = set(keep)
        for name in os.listdir(self.dir):
            if name not in keep:
                try:
                    os.unlink(os.path.join(self.dir, name))
                except OSError:
                    pass

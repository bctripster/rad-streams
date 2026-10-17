"""Photos from the NASA Image and Video Library (images.nasa.gov), for space themes.

No API key is needed. NASA's own photos are public domain in the US; NASA
asks for a credit, which the on-screen credit gives. Items NASA marks as
someone else's copyright (ESA, a university, a photographer) are skipped.

Each result lists its renditions (~orig, ~medium, ~small, ~thumb) with
their sizes, so the width and shape are checked before downloading. When a
size is missing the candidate carries ``check_size`` and the library checks
the downloaded file instead.
"""
from __future__ import annotations

import logging
import re
from typing import Optional

from . import net

API = "https://images-api.nasa.gov/search"
log = logging.getLogger("rad_streams.nasa")

# Artwork and diagrams rather than photos; press events and hardware on the ground.
_NOT_A_PHOTO = re.compile(
    r"\b(artist'?s?|concept|illustration|rendering|render|animation|infographic|diagram|chart|"
    r"graphic|logo|poster|briefing|conference|ceremony|award|meeting|portrait|visit|visits|"
    r"launch pad|clean ?room|test stand|mockup|model|astronauts?|missile|undocking|worker|"
    r"spacecraft|probes?|processing|mercury 7|binoculars|composite|hangout|webcast|event|join)\b", re.I)
_GROUND_WORK = re.compile(r"clean ?room|astrotech|payload processing|processing facility", re.I)
# Titles that are only a photo ID ("KSC-2009-1100", "S114E5360"): usually hardware on the ground.
_ID_ONLY = re.compile(r"^[A-Z]{1,6}[-_]?\d[\w-]*$|^S\d+E\d+$", re.I)
# Per-term extras: Earth photos must say they show Earth; aurora means Earth's aurora.
_EARTH_VIEW = re.compile(r"earth observation|earth limb|blue marble|earth at night|earth from|"
                         r"^iss\d+-?e-?\d+", re.I)
_OTHER_PLANETS = re.compile(r"\b(jupiter|saturn|uranus|neptune|io|mars|venus|ganymede)\b", re.I)
_REQUIRE = {"earth observation": _EARTH_VIEW, "earth from space": _EARTH_VIEW, "blue marble": _EARTH_VIEW}
_EXCLUDE = {"aurora": _OTHER_PLANETS}

MIN_WIDTH, MIN_ASPECT, MAX_ASPECT = 1280, 1.1, 2.4   # as images.py, but space photos may be squarer
MAX_BYTES = 25 * 1024 * 1024
MAX_PIXELS = 40_000_000
_COPYRIGHT = re.compile(r"©|\bcopyright\b|\(c\)\s*\d{4}|all rights reserved", re.I)


def _plain(term: str) -> str:
    return term.strip('"')


def _rendition(links: list[dict]) -> tuple[Optional[str], bool]:
    """(URL to download, whether its size still needs checking), or (None, False) if none fits.

    Prefers ~large, then ~medium, then ~orig, using the sizes the API lists.
    """
    sized = [l for l in links if l.get("href")]
    by_name: dict[str, dict] = {}
    for link in sized:
        m = re.search(r"~(large|medium|orig)\.(jpe?g|png)$", link["href"], re.I)
        if m:
            by_name[m.group(1).lower()] = link
    for name in ("large", "medium", "orig"):
        link = by_name.get(name)
        if not link:
            continue
        w, h, size = link.get("width"), link.get("height"), link.get("size")
        if size and size > MAX_BYTES:
            continue
        if w and h:
            if w < MIN_WIDTH or not (MIN_ASPECT <= w / h <= MAX_ASPECT) or w * h > MAX_PIXELS:
                continue
            return link["href"], False
        return link["href"], True                 # size not listed: check after download
    if not any("width" in l for l in sized):      # an older API reply without sizes
        for link in sized:
            if link.get("rel") == "preview" and "~thumb." in link["href"]:
                return link["href"].replace("~thumb.", "~large."), True
    return None, False


def search(term: str, limit: int = 100) -> list[dict]:
    """Candidates for one search term, in the shape images.find_candidates uses."""
    q = _plain(term)
    data = net.get_json(API, {"q": q, "media_type": "image", "page_size": limit})
    items = ((data or {}).get("collection") or {}).get("items") or []
    words = [w for w in re.findall(r"\w+", q.lower()) if len(w) > 2]
    out = []
    for item in items:
        meta = (item.get("data") or [{}])[0]
        title = re.sub(r"\s+", " ", meta.get("title") or "").strip()
        nasa_id = meta.get("nasa_id") or ""
        url, check = _rendition(item.get("links") or [])
        if not title or not nasa_id or not url or meta.get("media_type", "image") != "image":
            continue
        is_iss_photo = re.match(r"^iss\d+-?e-?\d+$", title, re.I)   # ISS Earth photos keep their IDs
        if not is_iss_photo and (_ID_ONLY.match(title) or title.lower() == nasa_id.lower()):
            continue
        if q.lower() in _REQUIRE and not (_REQUIRE[q.lower()].search(title) or
                                          _REQUIRE[q.lower()].search(nasa_id)):
            continue
        if q.lower() in _EXCLUDE and _EXCLUDE[q.lower()].search(title):
            continue
        about = " ".join([title, " ".join(meta.get("keywords") or [])]).lower()
        if words and not all(w in about for w in words):
            continue            # the term must be in the title or keywords, not just the description
        if _NOT_A_PHOTO.search(title) or _GROUND_WORK.search(meta.get("description") or ""):
            continue
        credit_bits = " ".join(str(meta.get(k) or "") for k in ("photographer", "secondary_creator"))
        if _COPYRIGHT.search(" ".join([meta.get("description") or "", credit_bits])):
            continue
        who = (meta.get("photographer") or meta.get("secondary_creator") or "").strip()
        artist = f"NASA / {who}" if who and "nasa" not in who.lower() else (who or "NASA")
        out.append({
            "scenic": True,
            "title": title[:120],
            "url": url,
            "artist": re.sub(r"\s+", " ", artist)[:60],
            "license": "Public domain",
            "page": f"https://images.nasa.gov/details/{nasa_id}",
            "source": "NASA Image Library",
            "check_size": check,
        })
    return out

"""Theme channels: photos of a subject ("Theme: Space") instead of a city.

Put ``Theme: <name>`` in a station's City column. A built-in theme uses
search terms picked for good photos; any other name is searched on Commons.
Theme channels skip the city lookup and weather.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Optional

# name -> (label shown on screen, search terms, whether to fall back to a plain text search).
# Commons search mishandles a quoted phrase inside "( ... OR ... )" (no results, or the
# group ignored), so each term is searched on its own and the results merged.
PRESETS: dict[str, tuple[str, tuple[str, ...], bool]] = {
    # no plain text tier for space: "galaxy" alone finds phones
    "space": ("Space", ("nebula", "galaxy", '"milky way"', '"star cluster"'), False),
    # no plain text tier for earth: it finds institutes and talks named "Earth Observation"
    "earth": ("Earth", ('"earth from space"', '"earth at night"', '"blue marble"', '"earth observation"'), False),
    "nature": ("Nature", ("landscape", "forest", "waterfall", "mountain", "lake", "meadow", "canyon"), True),
    "ocean": ("Ocean", ("ocean", "seascape", '"sea waves"', "coastline"), True),
    "mountains": ("Mountains", ("mountain", "mountains", '"mountain range"', "alps"), True),
    "forest": ("Forest", ("forest", "woodland", "rainforest"), True),
    "waterfalls": ("Waterfalls", ("waterfall", "waterfalls"), True),
    "desert": ("Desert", ("desert", "dunes", "badlands"), True),
    # not bare "aurora": that finds a cruiser and a dragonfly
    "aurora": ("Aurora", ('"northern lights"', '"aurora borealis"', '"aurora australis"'), True),
    "sunsets": ("Sunsets", ("sunset", "sunrise"), True),
    "beaches": ("Beaches", ("beach", "beaches", "shoreline"), True),
    "lakes": ("Lakes", ("lake", "lakes"), True),             # not "tarn": the French river
    # not "bird(s)": matches "Bird-cherry ermine moth"
    "wildlife": ("Wildlife", ("wildlife", "mammals", "animals"), True),
    "night sky": ("Night Sky", ('"night sky"', '"milky way"', '"star trails"', "astrophotography"), True),
}
# Themes that also search the NASA Image Library (public domain, see nasa.py).
NASA_TERMS: dict[str, tuple[str, ...]] = {
    "space": ("nebula", "galaxy", "star cluster", "hubble"),
    "earth": ("earth observation", "earth from space", "blue marble"),
    "night sky": ("milky way", "night sky"),
    "aurora": ("aurora",),
}

ALIASES = {"sunset": "sunsets", "beach": "beaches", "lake": "lakes", "waterfall": "waterfalls",
           "mountain": "mountains", "forests": "forest", "deserts": "desert", "oceans": "ocean",
           "sea": "ocean", "animals": "wildlife", "northern lights": "aurora", "stars": "night sky",
           "astronomy": "space", "landscapes": "nature", "landscape": "nature"}

_PREFIX = re.compile(r"^\s*theme\s*:\s*", re.I)


@dataclass(frozen=True)
class Theme:
    key: str                  # cache folder name
    label: str                # for logs and Validate (the panel doesn't show it)
    terms: tuple[str, ...]    # searched one at a time
    text_tier: bool = True    # also search all photos, not just featured and quality ones
    animals: bool = False     # allow animals and close-ups (Wildlife, and themes the user names)
    nasa: tuple[str, ...] = ()   # NASA Image Library searches, right after Commons' featured photos

    def filters(self) -> dict:
        """Options for images.acceptable: scenery themes keep animals and Mars out."""
        return {"animals": self.animals, "planetary": self.key == "theme-space"}

    def queries(self) -> list[tuple[str, str]]:
        """(tier, query) pairs, best first.

        A bare term matches anywhere in a long description (a featured photo of a
        bird "seen in the forest"), so photos with the term in their title come
        first; the full-text searches only top up a theme that's still short.
        """
        cats = [("featured", "incategory:Featured_pictures_on_Wikimedia_Commons "),
                ("quality", "incategory:Quality_images ")]
        if self.text_tier:
            cats.append(("text", ""))
        def in_title(term: str) -> str:   # intitle: takes one word or one quoted phrase
            return f'intitle:"{term}"' if " " in term and not term.startswith('"') else f"intitle:{term}"
        titled = [(f"theme-{name}-title", f"filetype:bitmap {cat}{in_title(term)}")
                  for name, cat in cats for term in self.terms]
        anywhere = [(f"theme-{name}", f"filetype:bitmap {cat}{term}") for name, cat in cats for term in self.terms]
        nasa = [("nasa-title", term) for term in self.nasa]
        n = len(self.terms)                     # after the featured title matches
        return titled[:n] + nasa + titled[n:] + anywhere


def parse(city_field: str) -> Optional[Theme]:
    """The theme named in a City column ("Theme: Space"), or None for a real city."""
    m = _PREFIX.match(city_field or "")
    if not m:
        return None
    name = re.sub(r"\s+", " ", city_field[m.end():]).strip()
    if not name:
        return None
    low = ALIASES.get(name.lower(), name.lower())
    slug = re.sub(r"[^a-z0-9]+", "-", low).strip("-") or "theme"
    if low in PRESETS:
        label, terms, text_tier = PRESETS[low]
        return Theme(f"theme-{slug}", label, terms, text_tier, animals=low == "wildlife",
                     nasa=NASA_TERMS.get(low, ()))
    words = re.sub(r"[\"()]", " ", name).split()
    if not words:
        return None
    # Unquoted, so "old lighthouses" matches either word order; a plural last word is
    # also tried in the singular, since most file names use it ("Lighthouse at ...").
    terms = [" ".join(words)]
    last = words[-1]
    if len(last) > 3 and last.lower().endswith("s") and not last.lower().endswith("ss"):
        terms.append(" ".join(words[:-1] + [last[:-1]]))
    label = " ".join(words)
    label = label[:1].upper() + label[1:]
    # The slug alone can collide ("Cats!" and "cats", or any non-Latin name), and two
    # themes sharing a folder would keep refetching over each other's photos.
    # Hash the exact text: the label comes from it, and one folder must mean one label.
    digest = hashlib.sha1(name.encode("utf-8")).hexdigest()[:8]
    # A theme the user names could be anything ("Theme: Cats"), so animals are allowed.
    return Theme(f"theme-{slug[:40]}-{digest}", label, tuple(terms), animals=True)

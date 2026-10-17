"""City name -> coordinates via Open-Meteo geocoding (no API key), cached on disk."""
from __future__ import annotations

import os
import re
import time
from dataclasses import asdict, dataclass
from typing import Optional

from . import net

GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
CACHE_DAYS = 30

US_STATES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California",
    "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware", "FL": "Florida", "GA": "Georgia",
    "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois", "IN": "Indiana", "IA": "Iowa",
    "KS": "Kansas", "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland",
    "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota", "MS": "Mississippi",
    "MO": "Missouri", "MT": "Montana", "NE": "Nebraska", "NV": "Nevada", "NH": "New Hampshire",
    "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York", "NC": "North Carolina",
    "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma", "OR": "Oregon", "PA": "Pennsylvania",
    "RI": "Rhode Island", "SC": "South Carolina", "SD": "South Dakota", "TN": "Tennessee",
    "TX": "Texas", "UT": "Utah", "VT": "Vermont", "VA": "Virginia", "WA": "Washington",
    "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming", "DC": "District of Columbia",
    "PR": "Puerto Rico",
}
CA_PROVINCES = {
    "AB": "Alberta", "BC": "British Columbia", "MB": "Manitoba", "NB": "New Brunswick",
    "NL": "Newfoundland and Labrador", "NS": "Nova Scotia", "NT": "Northwest Territories",
    "NU": "Nunavut", "ON": "Ontario", "PE": "Prince Edward Island", "QC": "Quebec",
    "SK": "Saskatchewan", "YT": "Yukon",
}
AU_STATES = {
    "ACT": "Australian Capital Territory", "NSW": "New South Wales", "NT": "Northern Territory",
    "QLD": "Queensland", "SA": "South Australia", "TAS": "Tasmania", "VIC": "Victoria",
    "WA": "Western Australia",
}
# Region abbreviations by country code. Looked up against each result's own
# country, so "WA" means Washington for a US hit and Western Australia for an AU one.
REGION_CODES = {"US": US_STATES, "CA": CA_PROVINCES, "AU": AU_STATES}
# Countries whose on-screen label uses the region (state/province) instead of the country.
REGION_LABEL_COUNTRIES = {"US", "CA", "AU"}
# Accent/spelling variants Open-Meteo uses for some regions.
REGION_ALIASES = {"québec": "quebec", "yukon territory": "yukon"}
COUNTRY_ALIASES = {
    "usa": "US", "u.s.": "US", "u.s.a.": "US", "united states of america": "US", "america": "US",
    "uk": "GB", "u.k.": "GB", "england": "GB", "scotland": "GB", "wales": "GB",
    "great britain": "GB", "britain": "GB", "northern ireland": "GB",
}
# Bump when lookup rules change so cached answers from older rules are redone.
CACHE_VERSION = 2


@dataclass
class Place:
    name: str
    lat: float
    lon: float
    country: str = ""
    country_code: str = ""
    admin1: str = ""
    timezone: str = ""
    population: int = 0

    @property
    def label(self) -> str:
        """Short on-screen label, e.g. 'Burlington, Vermont' or 'Lyon, France'."""
        region = self.admin1 if self.country_code in REGION_LABEL_COUNTRIES else self.country
        return f"{self.name}, {region}" if region else self.name


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "place"


def _norm(text) -> str:
    t = str(text or "").strip().lower()
    return REGION_ALIASES.get(t, t)


def _matches(result: dict, qualifier: str) -> bool:
    """Does one qualifier ('BC', 'Ontario', 'Canada', 'UK', ...) fit this hit?"""
    q = _norm(qualifier)
    if not q:
        return True
    cc = str(result.get("country_code", "")).upper()
    fields = {_norm(result.get(k)) for k in ("admin1", "admin2", "country", "country_code")}
    if q in fields:
        return True
    code = q.upper().replace(".", "")
    # A hit without a country code is checked against every region table.
    tables = [REGION_CODES[cc]] if cc in REGION_CODES else ([] if cc else REGION_CODES.values())
    if any(_norm(t.get(code)) in fields for t in tables if t.get(code)):
        return True
    alias = COUNTRY_ALIASES.get(q)
    return bool(alias and alias == cc)


def pick_result(results: list[dict], qualifiers: list[str], name: str = "") -> Optional[dict]:
    """Choose the best geocoding hit for 'City, Qualifier, ...'.

    Every qualifier (state, province, country, or their abbreviations) must
    match. Among matches, towns and cities beat islands, airports and the
    like, an exact name match beats a partial one ("Vancouver" over
    "Vancouver Island"), and then the most populous wins. With no matches, fall back to the most
    populous result only when no qualifier was given.
    """
    if not results:
        return None
    # Populated places (feature codes PPL*) before airports, parishes and the like.
    ranked = sorted(results, key=lambda r: (not str(r.get("feature_code", "PPL")).startswith("PPL"),
                                            _norm(r.get("name")) != _norm(name) if name else False,
                                            -(r.get("population") or 0)))
    for r in ranked:
        if all(_matches(r, q) for q in qualifiers):
            return r
    return None if qualifiers else ranked[0]


def _lookup(name: str, qualifiers: list[str]) -> Optional[dict]:
    data = net.get_json(GEOCODE_URL, {"name": name, "count": 20, "language": "en", "format": "json"})
    return pick_result(data.get("results") or [], qualifiers, name)


def geocode(city: str, cache_dir: Optional[str] = None) -> Optional[Place]:
    """Resolve 'City', 'City, ST' or 'City, Country' to a Place, or None."""
    city = (city or "").strip()
    if not city:
        return None
    cache_path = os.path.join(cache_dir, "geocode", slug(city) + ".json") if cache_dir else None
    if cache_path:
        cached = net.read_json(cache_path)
        if (cached and cached.get("_v") == CACHE_VERSION
                and time.time() - cached.get("_t", 0) < CACHE_DAYS * 86400):
            cached.pop("_t", None)
            cached.pop("_v", None)
            return Place(**cached)

    parts = [p.strip() for p in city.split(",") if p.strip()]
    name, qualifiers = parts[0], parts[1:]
    hit = _lookup(name, qualifiers)
    saint = re.sub(r"^(St|Ste)\.?\s+", lambda m: "Saint " if m.group(1) == "St" else "Sainte ", name)
    if saint != name and (not hit or not str(hit.get("feature_code", "PPL")).startswith("PPL")):
        hit = _lookup(saint, qualifiers) or hit  # "St. Paul, MN" is listed as "Saint Paul"
    if not hit:
        return None
    place = Place(
        name=hit.get("name", name), lat=float(hit["latitude"]), lon=float(hit["longitude"]),
        country=hit.get("country", ""), country_code=hit.get("country_code", ""),
        admin1=hit.get("admin1", ""), timezone=hit.get("timezone", ""),
        population=int(hit.get("population") or 0),
    )
    if cache_path:
        net.write_json(cache_path, {**asdict(place), "_t": time.time(), "_v": CACHE_VERSION})
    return place

"""Create, update and remove Dispatcharr channels to match the station list.

The ORM models are passed in rather than imported, so this module loads
outside Dispatcharr (the tests drive it with small in-memory stand-ins).
Which station owns which channel/stream is remembered in a JSON file next to
the service config; the stream URL is the fallback when that file is lost.

Guide data: Sync keeps one "Custom Dummy EPG" source called "Rad Streams".
Dispatcharr generates its programmes on the fly (nothing to refresh or
expire) by matching a pattern against each channel's stream name, which is
``Rad Streams: <station> | <about>``. Station names and cities can't contain
``|`` (it separates the columns), so the split is unambiguous.

Logos and guide data are only set on a channel that has none, or that still
has the one Sync set last time, so a logo or EPG picked by hand is kept.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Optional

from . import net, themes
from .config import Station

GROUP_NAME = "Rad Streams"
STREAM_PREFIX = "Rad Streams: "
AUTO_NUMBER_FROM = 1000

GUIDE_NAME = "Rad Streams"
GUIDE_TVG_ID = "dummy_rad_streams"
MANAGED_BY = "rad_streams"
# Programme length in the guide, in minutes. Radio has no schedule, so the
# blocks only give guide grids something to draw.
GUIDE_BLOCK_MINUTES = 240
GUIDE_PROPERTIES = {
    "managed_by": MANAGED_BY,
    "name_source": "stream",
    "stream_index": 1,
    "title_pattern": r"^Rad Streams: (?<station>.+) \| (?<about>[^|]+)$",
    "title_template": "{station}",
    "subtitle_template": "{about}",
    "description_template": "{about}. Internet radio with the track that's playing on screen.",
    "program_duration": GUIDE_BLOCK_MINUTES,
    "category": "Music",
    "include_date": False,
    "timezone": "UTC",
}

log = logging.getLogger("rad_streams.channels")


@dataclass
class Models:
    Channel: Any
    ChannelGroup: Any
    ChannelStream: Any
    Stream: Any
    StreamProfile: Any
    # Optional: left as None where Dispatcharr lacks them, and Sync skips that part.
    EPGSource: Any = None
    EPGData: Any = None
    Logo: Any = None


def guide_about(st: Station) -> str:
    """The guide's subtitle for a station: where it's from, or what it shows."""
    theme = themes.parse(st.city)
    if theme is not None:
        return f"{theme.label} photos"
    return f"Live from {st.city}"


def stream_name(st: Station) -> str:
    """``Rad Streams: KEXP | Live from Seattle, WA`` (the guide pattern reads this)."""
    return f"{STREAM_PREFIX}{st.name} | {guide_about(st)}"


@dataclass
class SyncResult:
    created: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def summary(self) -> str:
        parts = []
        for label, items in (("created", self.created), ("updated", self.updated),
                             ("unchanged", self.unchanged), ("removed", self.removed)):
            if items:
                parts.append(f"{len(items)} {label}")
        return ", ".join(parts) or "no stations"


class ChannelSync:
    def __init__(self, models: Models, mapping_path: str, default_logo: Optional[str] = None):
        """``default_logo`` is a path under /data (or a URL) for channels without ``logo=``."""
        self.m = models
        self.mapping_path = mapping_path
        self.default_logo = default_logo
        self._profile_id: Optional[int] = None
        self._logos: dict = {}

    # ---- helpers -------------------------------------------------------
    def _mapping(self) -> dict:
        return net.read_json(self.mapping_path) or {}

    def _profile(self) -> Optional[int]:
        """The built-in Proxy stream profile: a real-time source can look like
        it's buffering to an ffmpeg profile and trigger stream switching."""
        if self._profile_id is None:
            P = self.m.StreamProfile.objects
            prof = (P.filter(name__iexact="proxy").first() or P.filter(name__icontains="proxy").first()
                    or P.first())
            self._profile_id = prof.id if prof else None
        return self._profile_id

    def _get(self, model, obj_id) -> Optional[Any]:
        if not obj_id:
            return None
        try:
            return model.objects.get(id=obj_id)
        except Exception:
            return None

    def _number_taken(self, number: float, exclude_id: Optional[int]) -> Optional[Any]:
        q = self.m.Channel.objects.filter(channel_number=number)
        if exclude_id:
            q = q.exclude(id=exclude_id)
        return q.first()

    def _guide(self, res: SyncResult) -> Optional[Any]:
        """The shared EPG entry for every Rad Streams channel, created on first use."""
        if self.m.EPGSource is None or self.m.EPGData is None:
            return None
        try:
            src = self.m.EPGSource.objects.filter(name=GUIDE_NAME).first()
            if src is None:
                src = self.m.EPGSource.objects.create(
                    name=GUIDE_NAME, source_type="dummy", is_active=True,
                    custom_properties=dict(GUIDE_PROPERTIES))
            elif (src.source_type != "dummy"
                  or (src.custom_properties or {}).get("managed_by") != MANAGED_BY):
                # Someone else's source with our name: never touch it.
                res.warnings.append(f"an EPG source named '{GUIDE_NAME}' already exists and isn't "
                                    "this plugin's, so guide entries were skipped")
                return None
            elif src.custom_properties != GUIDE_PROPERTIES:
                src.custom_properties = dict(GUIDE_PROPERTIES)
                src.save(update_fields=["custom_properties"])
            # Dispatcharr creates the EPG entry for a new dummy source itself;
            # make it here too in case that signal didn't run.
            epg = self.m.EPGData.objects.filter(epg_source=src).first()
            if epg is None:
                epg = self.m.EPGData.objects.create(tvg_id=GUIDE_TVG_ID, name=GUIDE_NAME,
                                                    epg_source=src)
            return epg
        except Exception as exc:
            log.exception("guide setup failed")
            res.warnings.append(f"guide entries skipped: {exc}")
            return None

    def _logo(self, st: Station, res: SyncResult) -> Optional[Any]:
        """The station's ``logo=`` image, else the Rad Streams logo."""
        url = st.logo_url or self.default_logo
        if self.m.Logo is None or not url:
            return None
        if url in self._logos:
            return self._logos[url]
        name = st.name if st.logo_url else GUIDE_NAME
        try:
            logo, _ = self.m.Logo.objects.get_or_create(url=url, defaults={"name": name[:255]})
        except Exception as exc:
            log.exception("logo setup failed for %s", st.name)
            res.warnings.append(f"{st.name}: logo skipped: {exc}")
            logo = None
        self._logos[url] = logo
        return logo

    @staticmethod
    def _managed(current_id, last_set_id) -> bool:
        """True if Sync may change this field: it's empty, or still what Sync set."""
        return current_id is None or (last_set_id is not None and current_id == last_set_id)

    # ---- main ------------------------------------------------------------
    def sync(self, stations: list[Station], url_for) -> SyncResult:
        """``url_for(station)`` gives the station's route on the local server."""
        res = SyncResult()
        self._logos = {}          # Logo rows looked up during this Sync, by URL
        mapping = self._mapping()
        group, _ = self.m.ChannelGroup.objects.get_or_create(name=GROUP_NAME)
        wanted = {s.station_id for s in stations}
        new_mapping: dict = {}

        try:
            self._sync(stations, url_for, mapping, group, wanted, new_mapping, res)
        finally:
            # Even after a failure part way, record what exists, so the next Sync
            # updates those channels instead of creating duplicates.
            for sid, entry in mapping.items():
                if sid not in new_mapping and (sid in wanted or sid in self._undeleted):
                    new_mapping[sid] = entry
            net.write_json(self.mapping_path, new_mapping)
        return res

    def _sync(self, stations, url_for, mapping, group, wanted, new_mapping, res) -> None:
        self._undeleted: set = set()
        guide = self._guide(res)
        # Removals first, so their channel numbers are free for new stations.
        for sid, entry in mapping.items():
            if sid in wanted:
                continue
            failed = False
            for model, key in ((self.m.Channel, "channel_id"), (self.m.Stream, "stream_id")):
                obj = self._get(model, entry.get(key))
                if obj is not None:
                    try:
                        obj.delete()
                    except Exception as exc:
                        failed = True
                        res.warnings.append(f"could not delete {entry.get('name', sid)}: {exc}")
            if failed:
                self._undeleted.add(sid)   # keep it mapped and try again next Sync
            else:
                res.removed.append(entry.get("name", sid))

        for st in stations:
            url = url_for(st)
            entry = mapping.get(st.station_id, {})
            stream = self._get(self.m.Stream, entry.get("stream_id")) or \
                self.m.Stream.objects.filter(url=url).first()
            channel = self._get(self.m.Channel, entry.get("channel_id"))
            if channel is None and stream is not None:
                channel = self._channel_for(stream)   # mapping lost: find the channel through its stream
            changed = False
            is_new = channel is None

            want_name = stream_name(st)
            if stream is None:
                stream = self.m.Stream.objects.create(
                    name=want_name, url=url, is_custom=True,
                    channel_group=group, stream_profile_id=self._profile())
                changed = True
            else:
                fields = []
                if stream.name != want_name:
                    stream.name = want_name
                    fields.append("name")
                if stream.url != url:
                    stream.url = url
                    fields.append("url")
                if fields:
                    stream.save(update_fields=fields)
                    changed = True

            number = st.channel_number
            if number is not None:
                clash = self._number_taken(number, channel.id if channel else None)
                if clash is not None:
                    res.warnings.append(f"{st.name}: channel {number:g} is taken by "
                                        f"'{clash.name}', using another number")
                    number = None

            logo = self._logo(st, res)
            if channel is None:
                if number is None:
                    number = self.m.Channel.get_next_available_channel_number(
                        starting_from=AUTO_NUMBER_FROM)
                extra = {}
                if logo is not None:
                    extra["logo"] = logo
                if guide is not None:
                    extra["epg_data"] = guide
                channel = self.m.Channel.objects.create(
                    name=st.name, channel_number=number, channel_group=group,
                    stream_profile_id=self._profile(), **extra)
                changed = True
            else:
                fields = []
                if channel.name != st.name:
                    channel.name = st.name
                    fields.append("name")
                if number is not None and channel.channel_number != number:
                    channel.channel_number = number
                    fields.append("channel_number")
                if getattr(channel, "channel_group_id", None) != group.id:
                    channel.channel_group = group
                    fields.append("channel_group")
                have_logo = getattr(channel, "logo_id", None)
                if (logo is not None and have_logo != logo.id
                        and self._managed(have_logo, entry.get("logo_id"))):
                    channel.logo = logo
                    fields.append("logo")
                have_epg = getattr(channel, "epg_data_id", None)
                if (guide is not None and have_epg != guide.id
                        and self._managed(have_epg, entry.get("epg_data_id"))):
                    channel.epg_data = guide
                    fields.append("epg_data")
                if fields:
                    channel.save(update_fields=fields)
                    changed = True

            _, linked = self.m.ChannelStream.objects.get_or_create(
                channel=channel, stream=stream, defaults={"order": 0})
            changed = changed or linked

            new_entry = {"channel_id": channel.id, "stream_id": stream.id, "name": st.name}
            # Remember what Sync set, so a later Sync can tell it from a hand-picked one.
            for key, obj, attr in (("logo_id", logo, "logo_id"), ("epg_data_id", guide, "epg_data_id")):
                if obj is not None and getattr(channel, attr, None) == obj.id:
                    new_entry[key] = obj.id
                elif entry.get(key) is not None and getattr(channel, attr, None) == entry.get(key):
                    new_entry[key] = entry[key]
            new_mapping[st.station_id] = new_entry
            (res.created if is_new else res.updated if changed else res.unchanged).append(st.name)

    def _channel_for(self, stream) -> Optional[Any]:
        try:
            link = self.m.ChannelStream.objects.filter(stream=stream).first()
            return link.channel if link is not None else None
        except Exception:
            return None

    def remove_all(self) -> int:
        """Delete every channel/stream this plugin created."""
        mapping = self._mapping()
        for entry in mapping.values():
            for model, key in ((self.m.Channel, "channel_id"), (self.m.Stream, "stream_id")):
                obj = self._get(model, entry.get(key))
                if obj is not None:
                    obj.delete()
        net.write_json(self.mapping_path, {})
        self._remove_guide()
        return len(mapping)

    def _remove_guide(self) -> None:
        """Delete the plugin's EPG source (its entry goes with it). Logos are left,
        since other channels may use them."""
        if self.m.EPGSource is None:
            return
        try:
            src = self.m.EPGSource.objects.filter(name=GUIDE_NAME).first()
            if src is not None and src.source_type == "dummy" and \
                    (src.custom_properties or {}).get("managed_by") == MANAGED_BY:
                if self.m.EPGData is not None:
                    for epg in self.m.EPGData.objects.filter(epg_source=src):
                        epg.delete()
                src.delete()
        except Exception:
            log.exception("could not remove the Rad Streams EPG source")

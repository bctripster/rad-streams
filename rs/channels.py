"""Create, update and remove Dispatcharr channels to match the station list.

The ORM models are passed in rather than imported, so this module loads
outside Dispatcharr (the tests drive it with small in-memory stand-ins).
Which station owns which channel/stream is remembered in a JSON file next to
the service config; the stream URL is the fallback when that file is lost.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Optional

from . import net
from .config import Station

GROUP_NAME = "Rad Streams"
STREAM_PREFIX = "Rad Streams: "
AUTO_NUMBER_FROM = 1000

log = logging.getLogger("rad_streams.channels")


@dataclass
class Models:
    Channel: Any
    ChannelGroup: Any
    ChannelStream: Any
    Stream: Any
    StreamProfile: Any


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
    def __init__(self, models: Models, mapping_path: str):
        self.m = models
        self.mapping_path = mapping_path
        self._profile_id: Optional[int] = None

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

    # ---- main ------------------------------------------------------------
    def sync(self, stations: list[Station], url_for) -> SyncResult:
        """``url_for(station)`` gives the station's route on the local server."""
        res = SyncResult()
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

            if stream is None:
                stream = self.m.Stream.objects.create(
                    name=STREAM_PREFIX + st.name, url=url, is_custom=True,
                    channel_group=group, stream_profile_id=self._profile())
                changed = True
            else:
                fields = []
                if stream.name != STREAM_PREFIX + st.name:
                    stream.name = STREAM_PREFIX + st.name
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

            if channel is None:
                if number is None:
                    number = self.m.Channel.get_next_available_channel_number(
                        starting_from=AUTO_NUMBER_FROM)
                channel = self.m.Channel.objects.create(
                    name=st.name, channel_number=number, channel_group=group,
                    stream_profile_id=self._profile())
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
                if fields:
                    channel.save(update_fields=fields)
                    changed = True

            _, linked = self.m.ChannelStream.objects.get_or_create(
                channel=channel, stream=stream, defaults={"order": 0})
            changed = changed or linked

            new_mapping[st.station_id] = {"channel_id": channel.id, "stream_id": stream.id,
                                          "name": st.name}
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
        return len(mapping)

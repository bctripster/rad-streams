"""Just enough of Django's ORM surface for rs/channels.py."""
from __future__ import annotations

import itertools

from rs.channels import Models


class _Query:
    def __init__(self, rows):
        self.rows = list(rows)

    def _match(self, row, key, value):
        if "__" in key:
            field, op = key.split("__", 1)
            have = str(getattr(row, field, "") or "")
            if op == "iexact":
                return have.lower() == str(value).lower()
            if op == "icontains":
                return str(value).lower() in have.lower()
            raise NotImplementedError(op)
        return getattr(row, key, None) == value

    def filter(self, **kw):
        return _Query(r for r in self.rows if all(self._match(r, k, v) for k, v in kw.items()))

    def exclude(self, **kw):
        return _Query(r for r in self.rows if not all(self._match(r, k, v) for k, v in kw.items()))

    def first(self):
        return self.rows[0] if self.rows else None

    def count(self):
        return len(self.rows)

    def __iter__(self):
        return iter(self.rows)


class _Manager:
    def __init__(self, model):
        self.model = model

    def all(self):
        return _Query(self.model._rows.values())

    def filter(self, **kw):
        return self.all().filter(**kw)

    def first(self):
        return self.all().first()

    def get(self, **kw):
        q = self.filter(**kw)
        if not q.rows:
            raise self.model.DoesNotExist()
        return q.rows[0]

    def create(self, **kw):
        obj = self.model(**kw)
        obj.save()
        return obj

    def get_or_create(self, defaults=None, **kw):
        found = self.filter(**kw).first()
        if found:
            return found, False
        return self.create(**kw, **(defaults or {})), True


class _Meta(type):
    def __new__(mcs, name, bases, ns):
        cls = super().__new__(mcs, name, bases, ns)
        cls._rows = {}
        cls._ids = itertools.count(1)
        cls.objects = _Manager(cls)
        cls.DoesNotExist = type("DoesNotExist", (Exception,), {})
        return cls


class FakeModel(metaclass=_Meta):
    def __init__(self, **kw):
        self.id = None
        for k, v in kw.items():
            setattr(self, k, v)
            if isinstance(v, FakeModel):
                setattr(self, f"{k}_id", v.id)

    def save(self, update_fields=None):
        if self.id is None:
            self.id = next(type(self)._ids)
        for k, v in list(vars(self).items()):
            if isinstance(v, FakeModel):
                setattr(self, f"{k}_id", v.id)
        type(self)._rows[self.id] = self

    def delete(self):
        type(self)._rows.pop(self.id, None)


def make_models():
    class Channel(FakeModel):
        @classmethod
        def get_next_available_channel_number(cls, starting_from=1):
            used = {c.channel_number for c in cls._rows.values()}
            n = starting_from
            while n in used:
                n += 1
            return n

    class ChannelGroup(FakeModel):
        pass

    class ChannelStream(FakeModel):
        pass

    class Stream(FakeModel):
        pass

    class StreamProfile(FakeModel):
        pass

    class EPGSource(FakeModel):
        pass

    class EPGData(FakeModel):
        pass

    class Logo(FakeModel):
        pass

    StreamProfile.objects.create(name="ffmpeg")
    StreamProfile.objects.create(name="Proxy")
    return Models(Channel, ChannelGroup, ChannelStream, Stream, StreamProfile,
                  EPGSource=EPGSource, EPGData=EPGData, Logo=Logo)

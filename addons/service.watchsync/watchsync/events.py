"""Event and snapshot format (version 1) with strict parsing."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

FORMAT_VERSION = 1
TYPES = ("progress", "watched", "unwatched", "reset")
DEVICE_RE = re.compile(r"^[a-z0-9-]+$")
FILE_RE = re.compile(r"^([a-z0-9-]+)_(\d{6,})\.json$")
_TS_RE = re.compile(r"^(\d{4})-(\d\d)-(\d\d)T(\d\d):(\d\d):(\d\d)(?:\.(\d{1,6}))?Z$")


class EventError(ValueError):
    """A file is not a valid event or snapshot."""


class UnsupportedVersion(EventError):
    """The file has a newer format version than this add-on knows."""


def parse_ts(text: Any) -> datetime:
    match = _TS_RE.match(text) if isinstance(text, str) else None
    if not match:
        raise EventError(f"bad timestamp: {text!r}")
    year, month, day, hour, minute, second = (int(g) for g in match.groups()[:6])
    micro = int((match.group(7) or "0").ljust(6, "0"))
    try:
        return datetime(year, month, day, hour, minute, second, micro, tzinfo=timezone.utc)
    except ValueError as exc:
        raise EventError(f"bad timestamp: {text!r}") from exc


def format_ts(moment: datetime) -> str:
    moment = moment.astimezone(timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"


def event_filename(device: str, seq: int) -> str:
    return f"{device}_{seq:06d}.json"


def parse_filename(name: str) -> tuple[str, int] | None:
    match = FILE_RE.match(name)
    return (match.group(1), int(match.group(2))) if match else None


@dataclass(frozen=True)
class Event:
    device: str
    seq: int
    ts: str
    type: str
    media_id: str
    position_s: float = 0.0
    playcount: int = 0
    duration_s: float | None = None
    fp: str | None = None
    unmapped: bool = False

    def media_dict(self) -> dict[str, Any]:
        media: dict[str, Any] = {"id": self.media_id}
        if self.fp:
            media["fp"] = self.fp
        if self.unmapped:
            media["unmapped"] = True
        return media

    def body(self) -> dict[str, Any]:
        """The fields shared by an event and a snapshot item."""
        body: dict[str, Any] = {
            "ts": self.ts,
            "type": self.type,
            "media": self.media_dict(),
            "position_s": self.position_s,
        }
        if self.duration_s is not None:
            body["duration_s"] = self.duration_s
        body["playcount"] = self.playcount
        return body

    def to_dict(self) -> dict[str, Any]:
        head = {"v": FORMAT_VERSION, "device": self.device, "seq": self.seq}
        return {**head, **self.body()}


def _check_version(data: dict[str, Any]) -> None:
    version = data.get("v")
    if isinstance(version, int) and not isinstance(version, bool) and version > FORMAT_VERSION:
        raise UnsupportedVersion(f"format version {version}")
    if version != FORMAT_VERSION or isinstance(version, bool):
        raise EventError(f"bad version: {version!r}")


def _number(data: dict[str, Any], key: str, default: float | None) -> float | None:
    value = data.get(key, default)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        raise EventError(f"bad {key}: {value!r}")
    return float(value)


def _integer(data: dict[str, Any], key: str) -> int:
    value = data.get(key, 0)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise EventError(f"bad {key}: {value!r}")
    return value


def event_from_dict(data: Any) -> Event:
    if not isinstance(data, dict):
        raise EventError("not an object")
    _check_version(data)
    device, kind, media = data.get("device"), data.get("type"), data.get("media")
    if not isinstance(device, str) or not DEVICE_RE.match(device):
        raise EventError(f"bad device: {device!r}")
    if kind not in TYPES:
        raise EventError(f"bad type: {kind!r}")
    if not isinstance(media, dict) or not isinstance(media.get("id"), str) or not media["id"]:
        raise EventError("bad media")
    if "seq" not in data:
        raise EventError("missing seq")
    ts = data.get("ts")
    parse_ts(ts)
    fp = media.get("fp")
    return Event(
        device=device,
        seq=_integer(data, "seq"),
        ts=str(ts),
        type=kind,
        media_id=media["id"],
        position_s=_number(data, "position_s", 0.0) or 0.0,
        playcount=_integer(data, "playcount"),
        duration_s=_number(data, "duration_s", None),
        fp=fp if isinstance(fp, str) and fp else None,
        unmapped=media.get("unmapped") is True,
    )


def _load_json(text: str) -> Any:
    try:
        return json.loads(text)
    except ValueError as exc:
        raise EventError(f"not valid JSON: {exc}") from exc


def encode_event(event: Event) -> str:
    return json.dumps(event.to_dict(), ensure_ascii=False, indent=2)


def decode_event(text: str) -> Event:
    return event_from_dict(_load_json(text))


@dataclass(frozen=True)
class Snapshot:
    device: str
    upto_seq: int
    ts: str
    items: tuple[Event, ...]


def encode_snapshot(snapshot: Snapshot) -> str:
    data = {
        "v": FORMAT_VERSION,
        "device": snapshot.device,
        "upto_seq": snapshot.upto_seq,
        "ts": snapshot.ts,
        "items": [event.body() for event in snapshot.items],
    }
    return json.dumps(data, ensure_ascii=False, indent=2)


def decode_snapshot(text: str) -> Snapshot:
    """Items become events of the snapshot's device with seq = upto_seq."""
    data = _load_json(text)
    if not isinstance(data, dict):
        raise EventError("not an object")
    _check_version(data)
    items, upto, device = data.get("items"), data.get("upto_seq"), data.get("device")
    if not isinstance(items, list) or isinstance(upto, bool) or not isinstance(upto, int):
        raise EventError("bad snapshot")
    if not isinstance(device, str) or not DEVICE_RE.match(device):
        raise EventError(f"bad device: {device!r}")
    parse_ts(data.get("ts"))
    head = {"v": FORMAT_VERSION, "device": device, "seq": upto}
    events = tuple(event_from_dict({**head, **item}) for item in items if isinstance(item, dict))
    if len(events) != len(items):
        raise EventError("bad snapshot item")
    return Snapshot(device, upto, data["ts"], events)

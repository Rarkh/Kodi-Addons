"""Merge rules: per medium the event with the newest UTC timestamp wins."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
from typing import Iterable

from .events import Event, parse_ts

MAX_FUTURE = timedelta(hours=24)


def sort_key(event: Event) -> tuple[datetime, str]:
    """Newest timestamp first; with equal timestamps the alphabetically larger device wins."""
    return (parse_ts(event.ts), event.device)


def filter_future(events: Iterable[Event], now: datetime) -> tuple[list[Event], list[Event]]:
    """Split into (usable, rejected): events more than 24 h in the future are rejected."""
    listed = list(events)
    future = [parse_ts(event.ts) - now > MAX_FUTURE for event in listed]
    kept = [event for event, late in zip(listed, future) if not late]
    rejected = [event for event, late in zip(listed, future) if late]
    return kept, rejected


def merge(events: Iterable[Event]) -> dict[str, Event]:
    """Map every media id to its effective event.

    One medium = same id, or same fingerprint under different ids (moved or renamed file).
    The winner of the medium is stored for each of its ids (re-keyed to that id).
    """
    parent: dict[str, str] = {}

    def find(item: str) -> str:
        while parent[item] != item:
            parent[item] = parent[parent[item]]
            item = parent[item]
        return item

    fp_owner: dict[str, str] = {}
    all_events = list(events)
    for event in all_events:
        parent.setdefault(event.media_id, event.media_id)
        if event.fp:
            owner = fp_owner.setdefault(event.fp, event.media_id)
            parent[find(event.media_id)] = find(owner)

    groups: dict[str, list[Event]] = {}
    for event in all_events:
        groups.setdefault(find(event.media_id), []).append(event)

    result: dict[str, Event] = {}
    for members in groups.values():
        winner = max(members, key=sort_key)
        for media_id in {m.media_id for m in members}:
            unmapped = any(m.unmapped for m in members if m.media_id == media_id)
            result[media_id] = replace(winner, media_id=media_id, unmapped=unmapped)
    return result

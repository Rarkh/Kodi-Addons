"""Sync engine: own events out (queue, tmp+rename), foreign events in (merge), snapshots."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Callable

from .applier import applied_entry
from .backend import Backend, BackendError, optional_text
from .events import (
    Event,
    EventError,
    Snapshot,
    UnsupportedVersion,
    decode_event,
    decode_snapshot,
    encode_event,
    encode_snapshot,
    event_filename,
    event_from_dict,
    format_ts,
    parse_filename,
)
from .logger import DEBUG, INFO, WARNING
from .merge import filter_future, merge
from .store import LocalState, StateStore

PROGRESS_INTERVAL_S = 60.0
MAX_ATTEMPTS = 5
COMPACT_THRESHOLD = 100
CLOCK_TOLERANCE_S = 120.0

Log = Callable[..., None]


class SyncEngine:
    def __init__(
        self,
        backend: Backend,
        store: StateStore,
        device: str,
        clock: Callable[[], datetime],
        mono: Callable[[], float],
        log: Log,
    ) -> None:
        self.backend = backend
        self.store = store
        self.device = device
        self.clock = clock
        self.mono = mono
        self.log = log
        self.local: LocalState = store.load()
        self.state: dict[str, Event] = {}
        for data in self.local.state.values():
            try:
                event = event_from_dict(data)
            except EventError:
                continue
            self.state[event.media_id] = event
        self.online = False
        self.clock_offset: float | None = None
        self._seq_checked = False
        self._last_upload: dict[str, float] = {}
        self._snapshot_seen: dict[str, float] = {}
        self._own_files: list[tuple[int, str]] = []

    # ---- recording own events -------------------------------------------------------------

    def record(
        self,
        media_id: str,
        kind: str,
        position_s: float,
        duration_s: float | None,
        playcount: int,
        fp: str | None = None,
        unmapped: bool = False,
        force: bool = True,
    ) -> Event | None:
        """Create an own event and send it. A progress event is only kept locally while
        the last one for this medium went out less than 60 s ago (unless force)."""
        media: dict[str, Any] = {"id": media_id}
        if fp:
            media["fp"] = fp
        if unmapped:
            media["unmapped"] = True
        fields: dict[str, Any] = {
            "ts": format_ts(self.clock()),
            "type": kind,
            "media": media,
            "position_s": position_s,
            "playcount": playcount,
        }
        if duration_s is not None:
            fields["duration_s"] = duration_s
        if kind == "progress" and not force and self._sent_recently(media_id):
            self.local.pending[media_id] = fields
            self.save()
            return None
        self.local.pending.pop(media_id, None)
        event = self._enqueue(fields)
        self._last_upload[media_id] = self.mono()
        self._apply([event])
        self.save()
        if self.online:
            self._flush()
        return event

    def _sent_recently(self, media_id: str) -> bool:
        last = self._last_upload.get(media_id)
        return last is not None and self.mono() - last < PROGRESS_INTERVAL_S

    def _enqueue(self, fields: dict[str, Any]) -> Event:
        head = {"v": 1, "device": self.device, "seq": self.local.next_seq}
        event = event_from_dict({**head, **fields})
        self.local.next_seq += 1
        self.local.queue.append(event.to_dict())
        # Kodi's own database already shows what this device just did
        self.local.applied[event.media_id] = applied_entry(event)
        if self.local.processed.get(self.device, 0) == event.seq - 1:
            self.local.processed[self.device] = event.seq
        return event

    def _flush_pending(self) -> None:
        """Progress saved locally only (e.g. before a crash) becomes real events."""
        for fields in list(self.local.pending.values()):
            event = self._enqueue(fields)
            self._apply([event])
        self.local.pending.clear()

    def _flush(self) -> bool:
        """Upload the queue in order; stops at the first failure. True if nothing is left."""
        sent = 0
        try:
            while self.local.queue:
                event = event_from_dict(self.local.queue[0])
                name = event_filename(event.device, event.seq)
                self.backend.write_text("events", name, encode_event(event))
                self.local.queue.pop(0)
                sent += 1
            self.online = True
        except BackendError as exc:
            self.online = False
            self.log(f"Upload failed, {len(self.local.queue)} event(s) queued: {exc}", WARNING)
        if sent:
            self._write_device()
            self.save()
        return not self.local.queue

    def _write_device(self) -> None:
        info = {
            "v": 1,
            "device": self.device,
            "name": self.device,
            "last_contact": format_ts(self.clock()),
            "last_seq": self.local.next_seq - 1,
        }
        try:
            text = json.dumps(info, indent=2)
            self.backend.write_text("devices", f"{self.device}.json", text, overwrite=True)
        except BackendError as exc:
            self.log(f"Device file not written: {exc}", WARNING)

    # ---- reading foreign events -----------------------------------------------------------

    def _read_event(self, device: str, seq: int, name: str) -> Event:
        event = decode_event(self.backend.read_text("events", name))
        if event.device != device or event.seq != seq:
            raise EventError(f"{name}: content does not match the file name")
        return event

    def _pull_device(self, device: str, files: list[tuple[int, str]]) -> list[Event]:
        found: list[Event] = []
        for seq, name in sorted(files):
            if seq <= self.local.processed.get(device, 0):
                continue
            try:
                found.append(self._read_event(device, seq, name))
            except UnsupportedVersion as exc:
                self.log(f"{name} skipped: {exc}", WARNING)
            except (EventError, BackendError) as exc:
                count = self.local.attempts.get(name, 0) + 1
                self.local.attempts[name] = count
                if count < MAX_ATTEMPTS:
                    self.log(f"{name} unreadable (attempt {count}): {exc}", DEBUG)
                    break
                self.local.defective.append(name)
                self.log(f"{name} marked as defective: {exc}", WARNING)
            self.local.processed[device] = seq
            self.local.attempts.pop(name, None)
        return found

    def _pull_snapshots(self) -> list[Event]:
        found: list[Event] = []
        for name in self.backend.list_files("snapshots"):
            if not name.endswith(".json"):
                continue
            device = name[: -len(".json")]
            try:
                stamp = self.backend.mtime("snapshots", name)
                if self._snapshot_seen.get(device) == stamp:
                    continue
                snapshot = decode_snapshot(self.backend.read_text("snapshots", name))
            except (EventError, BackendError) as exc:
                self.log(f"Snapshot {name} unreadable: {exc}", DEBUG)
                continue
            self._snapshot_seen[device] = stamp
            if snapshot.upto_seq > self.local.processed.get(device, 0):
                found.extend(snapshot.items)
                self.local.processed[device] = snapshot.upto_seq
        return found

    def _pull(self) -> list[Event]:
        found = self._pull_snapshots()
        by_device: dict[str, list[tuple[int, str]]] = {}
        for name in self.backend.list_files("events"):
            parsed = parse_filename(name)
            if parsed:
                by_device.setdefault(parsed[0], []).append((parsed[1], name))
        self._own_files = sorted(by_device.get(self.device, []))
        for device, files in by_device.items():
            found.extend(self._pull_device(device, files))
        usable, rejected = filter_future(found, self.clock())
        for event in rejected:
            self.log(f"Ignored event from the future: {event.device} {event.ts}", WARNING)
        return self._apply(usable)

    def _apply(self, events: list[Event]) -> list[Event]:
        """Merge events into the state; return the entries that changed."""
        merged = merge([*self.state.values(), *events])
        changed = [event for key, event in merged.items() if self.state.get(key) != event]
        self.state = merged
        self.local.state = {key: event.to_dict() for key, event in merged.items()}
        return changed

    # ---- compaction -----------------------------------------------------------------------

    def _compact(self) -> None:
        """Condense own old events into the own snapshot, then delete them."""
        if len(self._own_files) < COMPACT_THRESHOLD or self.local.queue:
            return
        upto = self._own_files[-1][0]
        try:
            old = optional_text(self.backend, "snapshots", f"{self.device}.json")
            items = list(decode_snapshot(old).items) if old else []
            items.extend(self._read_event(self.device, seq, name) for seq, name in self._own_files)
            stamp = format_ts(self.clock())
            snapshot = Snapshot(self.device, upto, stamp, tuple(merge(items).values()))
            text = encode_snapshot(snapshot)
            self.backend.write_text("snapshots", f"{self.device}.json", text, overwrite=True)
        except (EventError, BackendError) as exc:
            self.log(f"Compaction skipped: {exc}", WARNING)
            return
        for _seq, name in self._own_files:
            try:
                self.backend.delete("events", name)
            except BackendError as exc:
                self.log(f"Could not delete {name}: {exc}", WARNING)
        self.log(f"Compacted {len(self._own_files)} events into the snapshot", INFO)
        self._own_files = []

    # ---- recovery and checks --------------------------------------------------------------

    def _recover_seq(self) -> None:
        """Never reuse a seq: continue after everything of this device found on the NAS."""
        highest = 0
        info = optional_text(self.backend, "devices", f"{self.device}.json")
        try:
            highest = int(json.loads(info)["last_seq"]) if info else 0
        except (ValueError, KeyError, TypeError):
            highest = 0
        for name in self.backend.list_files("events"):
            parsed = parse_filename(name)
            if parsed and parsed[0] == self.device:
                highest = max(highest, parsed[1])
        snap = optional_text(self.backend, "snapshots", f"{self.device}.json")
        if snap:
            try:
                highest = max(highest, decode_snapshot(snap).upto_seq)
            except EventError:
                pass
        self.local.next_seq = max(self.local.next_seq, highest + 1)

    def check_clock(self) -> float | None:
        """Seconds the NAS file time is ahead of this clock (logs a warning if large)."""
        self._write_device()
        try:
            stamp = self.backend.mtime("devices", f"{self.device}.json")
        except BackendError:
            return None
        offset = stamp - self.clock().timestamp()
        self.clock_offset = offset
        if abs(offset) > CLOCK_TOLERANCE_S:
            self.log(f"Clock differs from the NAS by {offset:.0f} s: check the time", WARNING)
        return offset

    # ---- public steps ---------------------------------------------------------------------

    def sync(self) -> list[Event]:
        """One round: send pending events, read foreign ones. Returns the changed entries."""
        changed: list[Event] = []
        try:
            self.backend.ensure_layout()
            if not self._seq_checked:
                self._recover_seq()
                self._seq_checked = True
                self.check_clock()
            self._flush_pending()
            self._flush()
            changed = self._pull()
            self._compact()
            self.online = True
            self.local.last_sync = format_ts(self.clock())
        except BackendError as exc:
            self.online = False
            self.log(f"NAS not reachable: {exc}", WARNING)
        self.save()
        return changed

    def flush(self) -> None:
        """Send everything that is still local (used when Kodi shuts down)."""
        self._flush_pending()
        self._flush()
        self.save()

    def rebuild(self) -> list[Event]:
        """Forget what was read; read all snapshots and events again (queue is kept)."""
        self.local.processed.clear()
        self.local.attempts.clear()
        self.local.defective.clear()
        self.local.state.clear()
        self.local.applied.clear()
        self.state = {}
        self._snapshot_seen = {}
        return self.sync()

    def lookup(self, media_id: str, fp: str | None) -> Event | None:
        """Effective state of a medium: by id, otherwise by fingerprint."""
        event = self.state.get(media_id)
        if event or not fp:
            return event
        return next((e for e in self.state.values() if e.fp == fp), None)

    def status(self) -> dict[str, Any]:
        return {
            "device": self.device,
            "online": self.online,
            "last_sync": self.local.last_sync,
            "queued": len(self.local.queue),
            "media": len(self.state),
            "unmapped": sum(1 for e in self.state.values() if e.unmapped),
            "defective": list(self.local.defective),
            "clock_offset_s": self.clock_offset,
        }

    def save(self) -> None:
        try:
            self.store.save(self.local)
        except BackendError as exc:
            self.log(f"Local cache not written: {exc}", WARNING)

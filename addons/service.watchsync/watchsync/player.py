"""Playback tracking: turns Kodi player callbacks into sync events."""

from __future__ import annotations

import functools
from dataclasses import dataclass
from typing import Any, Callable

import xbmc

from .applier import is_applied
from .engine import SyncEngine
from .events import Event
from .logger import DEBUG, INFO, WARNING
from .mediaid import Fingerprinter, Mapping, is_trackable, to_media_id

# Below this position no progress is written: a playback that starts at 0 (a quick look at a
# file) must not overwrite the saved state of the medium with "0". Only a stop right after a
# deliberate start from the beginning (a saved position existed) clears it: a "reset" event.
QUIET_START_S = 5.0
RESUME_TOLERANCE_S = 5.0
RESUME_AUTO, RESUME_ASK = 0, 1


@dataclass
class Current:
    media_id: str
    unmapped: bool
    fp: str | None
    duration: float
    position: float
    playcount: int
    watched: bool = False
    # a saved position existed but the playback began at the start (deliberately: stopping
    # right away then clears the saved position)
    restarted: bool = False


class Tracker:
    def __init__(
        self,
        engine: SyncEngine,
        mappings: list[Mapping],
        fingerprinter: Fingerprinter,
        threshold: float,
        resume_mode: int,
        seek: Callable[[float], None],
        ask_resume: Callable[[float], bool],
        log: Callable[..., None],
    ) -> None:
        self.engine = engine
        self.mappings = mappings
        self.fingerprinter = fingerprinter
        self.threshold = threshold
        self.resume_mode = resume_mode
        self.seek = seek
        self.ask_resume = ask_resume
        self.log = log
        self.current: Current | None = None

    def started(self, path: str, position: float, duration: float) -> None:
        self.current = None
        if not is_trackable(path):
            return
        ref = to_media_id(path, self.mappings)
        fp = self.fingerprinter.get(path)
        previous = self.engine.lookup(ref.id, fp)
        cur = self.current = Current(
            ref.id, ref.unmapped, fp, duration, position, previous.playcount if previous else 0
        )
        if ref.unmapped:
            self.engine.remember_path(path)
            self.log(f"No path mapping for {ref.id}: matched by file name and folders", DEBUG)
        if previous:
            self._maybe_resume(cur, previous)
            cur.restarted = (
                previous.type == "progress"
                and previous.position_s > RESUME_TOLERANCE_S
                and cur.position <= RESUME_TOLERANCE_S
            )

    def _maybe_resume(self, cur: Current, previous: Event) -> None:
        """Fallback: jump to the position another device saved, if it is the newest state.

        Normally Kodi's own resume handling already has it (applied to its database before the
        playback). Only when that was not possible (no library entry, query failed) we seek.
        """
        if previous.device == self.engine.device or previous.type != "progress":
            return
        applied = self.engine.local.applied
        entry = applied.get(previous.media_id)
        if is_applied(previous, applied) and entry and entry["ok"] and entry.get("via") != "file":
            return  # Kodi's library has it; a file path entry may not make Kodi resume: seek
        if abs(previous.position_s - cur.position) <= RESUME_TOLERANCE_S:
            return
        if self.resume_mode == RESUME_AUTO or self.ask_resume(previous.position_s):
            self.log(f"Resuming at {previous.position_s:.0f} s (saved on {previous.device})", INFO)
            self.seek(previous.position_s)
            cur.position = previous.position_s

    def _ratio(self, cur: Current) -> float:
        return cur.position / cur.duration if cur.duration > 0 else 0.0

    def _record(self, cur: Current, kind: str, force: bool) -> None:
        if kind == "progress" and cur.position < QUIET_START_S:
            return
        position = 0.0 if kind in ("watched", "reset") else cur.position
        self.engine.record(
            cur.media_id,
            kind,
            position,
            cur.duration if cur.duration > 0 else None,
            cur.playcount,
            fp=cur.fp,
            unmapped=cur.unmapped,
            force=force,
        )

    def _mark_watched(self, cur: Current) -> None:
        cur.playcount += 1
        cur.watched = True
        self._record(cur, "watched", True)

    def tick(self, position: float, duration: float) -> None:
        """Called regularly while playing."""
        cur = self.current
        if not cur or cur.watched:
            return
        cur.position = position
        cur.duration = duration
        if self._ratio(cur) >= self.threshold:
            self._mark_watched(cur)
        else:
            self._record(cur, "progress", False)

    def paused(self, position: float) -> None:
        cur = self.current
        if cur and not cur.watched:
            cur.position = position
            self._record(cur, "progress", True)

    def stopped(self) -> None:
        cur, self.current = self.current, None
        if not cur or cur.watched:
            return
        if self._ratio(cur) >= self.threshold:
            self._mark_watched(cur)
        elif cur.position < QUIET_START_S:
            if cur.restarted:
                self._record(cur, "reset", True)
        else:
            self._record(cur, "progress", True)

    def ended(self) -> None:
        cur, self.current = self.current, None
        if cur and not cur.watched:
            self._mark_watched(cur)


def _guard(method: Callable[..., None]) -> Callable[..., None]:
    """A failure in a Kodi callback must never stop the service."""

    @functools.wraps(method)
    def wrapper(self: PlayerMonitor, *args: Any) -> None:
        if self.tracker is None:
            return
        try:
            method(self, *args)
        except Exception as exc:  # noqa: BLE001
            self.tracker.log(f"{method.__name__} failed: {exc!r}", WARNING)

    return wrapper


class PlayerMonitor(xbmc.Player):
    """Thin adapter: reads position and file from Kodi and hands them to the Tracker."""

    def __init__(self) -> None:
        super().__init__()
        self.tracker: Tracker | None = None

    @_guard
    def onAVStarted(self) -> None:
        assert self.tracker
        self.tracker.started(self.getPlayingFile(), self.getTime(), self.getTotalTime())

    @_guard
    def onPlayBackPaused(self) -> None:
        assert self.tracker
        self.tracker.paused(self.getTime())

    @_guard
    def onPlayBackStopped(self) -> None:
        assert self.tracker
        self.tracker.stopped()

    @_guard
    def onPlayBackEnded(self) -> None:
        assert self.tracker
        self.tracker.ended()

    @_guard
    def poll(self) -> None:
        assert self.tracker
        if self.isPlayingVideo():
            self.tracker.tick(self.getTime(), self.getTotalTime())

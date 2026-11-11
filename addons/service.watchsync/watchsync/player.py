"""Playback tracking: turns Kodi player callbacks into sync events."""

from __future__ import annotations

import functools
from dataclasses import dataclass
from typing import Any, Callable

import xbmc

from .engine import SyncEngine
from .logger import DEBUG, WARNING
from .mediaid import Fingerprinter, Mapping, is_trackable, to_media_id

# Below this position no progress is written: a playback that starts at 0 (a quick look at a
# file) must not overwrite the saved state of the medium with "0". Only a stop right at the
# start of a medium that had a saved position clears it: a "reset" event.
QUIET_START_S = 1.0
# Stopping within this many seconds of the start clears a saved position (also after Kodi's
# own resume put the playback at that position)
QUICK_STOP_S = 1.0
RESUME_TOLERANCE_S = 5.0


@dataclass
class Current:
    media_id: str
    unmapped: bool
    fp: str | None
    duration: float
    position: float
    playcount: int
    watched: bool = False
    # a position was saved for this medium: stopping right away clears it
    had_saved: bool = False
    started_at: float = 0.0


class Tracker:
    def __init__(
        self,
        engine: SyncEngine,
        mappings: list[Mapping],
        fingerprinter: Fingerprinter,
        threshold: float,
        log: Callable[..., None],
    ) -> None:
        self.engine = engine
        self.mappings = mappings
        self.fingerprinter = fingerprinter
        self.threshold = threshold
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
        cur.started_at = self.engine.mono()
        if ref.unmapped:
            self.engine.remember_path(path)
            self.log(f"No path mapping for {ref.id}: matched by file name and folders", DEBUG)
        # Resuming is Kodi's own business: the saved position is in its database (applied by the
        # service), so Kodi asks where to continue. This add-on never jumps and never asks.
        cur.had_saved = bool(
            previous and previous.type == "progress" and previous.position_s > RESUME_TOLERANCE_S
        )

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
        elif cur.had_saved and self._quick_stop(cur):
            self._record(cur, "reset", True)
        elif cur.position < QUIET_START_S:
            pass  # a quick look: the saved state stays
        else:
            self._record(cur, "progress", True)

    def _quick_stop(self, cur: Current) -> bool:
        """Stopped right at the start (position, or time since the start even after a jump)."""
        return cur.position < QUIET_START_S or self.engine.mono() - cur.started_at < QUICK_STOP_S

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

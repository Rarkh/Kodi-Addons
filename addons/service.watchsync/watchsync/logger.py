"""Logging to the Kodi log plus a short ring buffer file the menu can show."""

from __future__ import annotations

from collections import deque
from datetime import datetime
from typing import Callable

from .backend import Backend, BackendError

# Same values as xbmc.LOGDEBUG, xbmc.LOGINFO, xbmc.LOGWARNING
DEBUG, INFO, WARNING = 0, 1, 2
LOG_FILE = "log.txt"
LEVEL_NAMES = {DEBUG: "DEBUG", INFO: "INFO", WARNING: "WARN"}


class Logger:
    def __init__(
        self,
        sink: Callable[[str, int], None],
        debug: bool,
        backend: Backend,
        now: Callable[[], datetime],
        max_lines: int = 300,
    ) -> None:
        self.sink = sink
        self.debug = debug
        self.backend = backend
        self.now = now
        self.lines: deque[str] = deque(maxlen=max_lines)
        self.dirty = False

    def __call__(self, message: str, level: int = INFO) -> None:
        if level == DEBUG and not self.debug:
            return
        # Debug lines go to the Kodi log as INFO, otherwise Kodi hides them by default
        self.sink(f"[WatchSync] {message}", max(level, INFO))
        stamp = self.now().strftime("%Y-%m-%d %H:%M:%S")
        self.lines.append(f"{stamp} {LEVEL_NAMES.get(level, 'INFO')} {message}")
        self.dirty = True

    def flush(self) -> None:
        if not self.dirty:
            return
        self.dirty = False
        try:
            self.backend.write_text("", LOG_FILE, "\n".join(self.lines) + "\n", overwrite=True)
        except BackendError:
            pass  # the log file is a convenience; the Kodi log has the same lines

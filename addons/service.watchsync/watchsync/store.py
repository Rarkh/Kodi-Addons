"""Local cache in the Kodi profile: sequence counter, processed events, upload queue."""

from __future__ import annotations

import json
from dataclasses import dataclass, field, fields
from typing import Any

from .backend import Backend, optional_text

STATE_FILE = "state.json"
CACHE_FILE = "cache.json"
# The big part of the cache: what was read and applied; written less often than the rest
CACHE_FIELDS = ("processed", "attempts", "defective", "state", "applied")


@dataclass
class LocalState:
    next_seq: int = 1
    # device -> highest processed seq
    processed: dict[str, int] = field(default_factory=dict)
    # event file name -> failed read attempts
    attempts: dict[str, int] = field(default_factory=dict)
    defective: list[str] = field(default_factory=list)
    # own events (dicts) not yet on the NAS, in seq order
    queue: list[dict[str, Any]] = field(default_factory=list)
    # media id -> merged event (dict)
    state: dict[str, dict[str, Any]] = field(default_factory=dict)
    # media id -> progress (event dict without seq) saved locally only
    pending: dict[str, dict[str, Any]] = field(default_factory=dict)
    # media id -> what this add-on last wrote into Kodi (playcount, position_s, ts, ok)
    applied: dict[str, dict[str, Any]] = field(default_factory=dict)
    # paths of the files this device played itself (to translate other devices' paths)
    own_paths: list[str] = field(default_factory=list)
    last_sync: str = ""


class StateStore:
    """Persists LocalState; with the cache switched off nothing is read or written."""

    def __init__(self, backend: Backend, enabled: bool) -> None:
        self.backend = backend
        self.enabled = enabled

    def load(self) -> LocalState:
        known = LocalState.__dataclass_fields__
        data: dict[str, Any] = {}
        # the big part second, so it wins (older versions kept everything in state.json)
        for name in (STATE_FILE, CACHE_FILE):
            text = optional_text(self.backend, "", name) if self.enabled else None
            try:
                loaded = json.loads(text) if text else {}
            except ValueError:
                loaded = {}
            if isinstance(loaded, dict):
                data.update(loaded)
        return LocalState(**{k: v for k, v in data.items() if k in known})

    def save(self, local: LocalState, big: bool = True) -> None:
        """Always the small part; the big part only if big."""
        if not self.enabled:
            return
        # no asdict(): it would copy the whole (possibly big) state
        small = {
            f.name: getattr(local, f.name) for f in fields(local) if f.name not in CACHE_FIELDS
        }
        self.backend.write_text("", STATE_FILE, json.dumps(small), overwrite=True)
        if big:
            cache = {name: getattr(local, name) for name in CACHE_FIELDS}
            self.backend.write_text("", CACHE_FILE, json.dumps(cache), overwrite=True)

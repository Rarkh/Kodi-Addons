"""Local cache in the Kodi profile: sequence counter, processed events, upload queue."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any

from .backend import Backend, optional_text

STATE_FILE = "state.json"


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
        text = optional_text(self.backend, "", STATE_FILE) if self.enabled else None
        try:
            data = json.loads(text) if text else {}
            known = LocalState.__dataclass_fields__
            return LocalState(**{k: v for k, v in data.items() if k in known})
        except (ValueError, TypeError, AttributeError):
            return LocalState()

    def save(self, local: LocalState) -> None:
        if self.enabled:
            self.backend.write_text("", STATE_FILE, json.dumps(asdict(local)), overwrite=True)

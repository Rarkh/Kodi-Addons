"""Media identity: a NAS-relative id built with path mappings, plus a content fingerprint."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import unquote

CHUNK = 64 * 1024
MAPPING_SEPARATOR = "|"
ARROW = "=>"
MATCH_DEPTH = 3
MIN_MATCH_PARTS = 2

# Paths that are not files on a share: nothing to identify, nothing to sync
UNTRACKED_SCHEMES = (
    "plugin://",
    "pvr://",
    "http://",
    "https://",
    "rtsp://",
    "rtmp://",
    "stack://",
    "videodb://",
    "musicdb://",
    "upnp://",
    "special://",
)
_LOCATION = re.compile(r"^(?:[a-z][a-z0-9+.-]*://[^/]*/|[a-z]:/)")
_CREDENTIALS = re.compile(r"^([a-zA-Z][a-zA-Z0-9+.-]*://)[^/@]*@")


@dataclass(frozen=True)
class MediaRef:
    id: str
    unmapped: bool


@dataclass(frozen=True)
class Mapping:
    """prefix: normalised prefix with trailing "/"; base: id part in front of the rest;
    raw: the prefix as typed (used to turn an id back into a local path)."""

    prefix: str
    base: str
    raw: str


def is_trackable(path: str) -> bool:
    return bool(path) and not path.lower().startswith(UNTRACKED_SCHEMES)


def normalize_path(path: str) -> str:
    """Forward slashes, no credentials, URL escapes decoded."""
    path = _CREDENTIALS.sub(r"\1", path)
    if "://" in path:
        path = unquote(path)
    return path.replace("\\", "/")


def _with_slash(text: str) -> str:
    return text if text.endswith("/") else text + "/"


def parse_mappings(text: str) -> list[Mapping]:
    """Parse "prefix" or "prefix=>base" entries separated by "|" or line breaks."""
    mappings = []
    for entry in re.split(r"[|\n]", text):
        prefix, _, base = entry.partition(ARROW)
        prefix, base = prefix.strip(), base.strip()
        if not prefix:
            continue
        raw = prefix if prefix[-1] in "/\\" else prefix + ("\\" if "\\" in prefix else "/")
        base = base.replace("\\", "/").strip("/")
        mappings.append(
            Mapping(_with_slash(normalize_path(prefix)), _with_slash(base) if base else "", raw)
        )
    return mappings


def format_mappings(mappings: list[Mapping]) -> str:
    return MAPPING_SEPARATOR.join(
        m.raw + (ARROW + m.base.rstrip("/") if m.base else "") for m in mappings
    )


def to_media_id(path: str, mappings: list[Mapping]) -> MediaRef:
    """The longest matching prefix wins; without a match the whole path is the id."""
    normal = normalize_path(path)
    lowered = normal.lower()
    for mapping in sorted(mappings, key=lambda m: len(m.prefix), reverse=True):
        if lowered.startswith(mapping.prefix.lower()):
            return MediaRef(mapping.base + normal[len(mapping.prefix) :], False)
    return MediaRef(normal, True)


def from_media_id(media_id: str, mappings: list[Mapping]) -> str | None:
    """A path this device can open for a mapped id (None if no mapping fits)."""
    for mapping in mappings:
        if media_id.startswith(mapping.base):
            rest = media_id[len(mapping.base) :]
            return mapping.raw + (rest.replace("/", "\\") if "\\" in mapping.raw else rest)
    return None


def _parts(media_id: str) -> list[str]:
    """Folders and file name, lower case, without protocol, host or drive letter."""
    return [part for part in _LOCATION.sub("", normalize_path(media_id).lower()).split("/") if part]


def file_name_key(media_id: str) -> str:
    """Lower-case file name of an id or path: where candidates for same_medium are looked up."""
    parts = _parts(media_id)
    return parts[-1] if parts else ""


def same_medium(first: str, second: str) -> bool:
    """Do two ids or paths name the same file, whatever protocol, host or share they use?

    The last folders and the file name decide (up to MATCH_DEPTH parts, case-insensitive), at
    least the file name and its folder. The file name alone never matches.
    """
    one, two = _parts(first), _parts(second)
    depth = min(len(one), len(two), MATCH_DEPTH)
    return depth >= MIN_MATCH_PARTS and one[-depth:] == two[-depth:]


def fingerprint(file: Any) -> str | None:
    """The fingerprint "s=<bytes>;h=<sha1 of first and last 64 KB>;" (None for an empty file)."""
    size = int(file.size())
    if size <= 0:
        return None
    digest = hashlib.sha1()  # noqa: S324  (a content fingerprint, not security)
    file.seek(0, 0)
    digest.update(bytes(file.readBytes(min(size, CHUNK))))
    file.seek(max(0, size - CHUNK), 0)
    digest.update(bytes(file.readBytes(min(size, CHUNK))))
    return f"s={size};h={digest.hexdigest()};"


class Fingerprinter:
    """Computes fingerprints once per path (only when a playback starts) and caches them."""

    def __init__(self, opener: Callable[[str], Any], max_entries: int = 200) -> None:
        self.opener = opener
        self.max_entries = max_entries
        self.cache: dict[str, str | None] = {}

    def get(self, path: str) -> str | None:
        if path not in self.cache:
            if len(self.cache) >= self.max_entries:
                self.cache.clear()
            self.cache[path] = self._compute(path)
        return self.cache[path]

    def _compute(self, path: str) -> str | None:
        try:
            file = self.opener(path)
        except Exception:  # noqa: BLE001  (unreadable source: no fingerprint)
            return None
        try:
            return fingerprint(file)
        except Exception:  # noqa: BLE001
            return None
        finally:
            file.close()

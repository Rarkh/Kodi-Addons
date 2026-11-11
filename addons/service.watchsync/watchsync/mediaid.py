"""Media identity: a NAS-relative id built with path mappings, plus a content fingerprint."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any, Callable, Iterable
from urllib.parse import unquote

CHUNK = 64 * 1024
MAPPING_SEPARATOR = "|"
ARROW = "=>"
MATCH_DEPTH = 3
MIN_MATCH_PARTS = 2

# Paths that are not files on a share: nothing to identify, nothing to sync
UNTRACKED_SCHEMES = tuple(
    f"{name}://"
    for name in (
        "plugin",
        "pvr",
        "http",
        "https",
        "rtsp",
        "rtmp",
        "stack",
        "videodb",
        "musicdb",
        "upnp",
        "special",
    )
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


# Folders of a file view where files can be listed (not library nodes, add-ons, sources ...)
FILE_SCHEMES = tuple(f"{name}://" for name in ("nfs", "smb", "sftp", "ftp", "ftps", "dav", "davs"))
_LOCAL_PATH = re.compile(r"^(?:/|[A-Za-z]:[\\/])")


def is_file_folder(path: str) -> bool:
    """A folder of a share or of local storage, as shown in the file view."""
    return path.lower().startswith(FILE_SCHEMES) or bool(_LOCAL_PATH.match(path))


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
        separator = "\\" if "\\" in prefix else "/"
        raw = prefix if prefix[-1] in "/\\" else prefix + separator
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


_ANY_LOCATION = re.compile(r"^(?:[A-Za-z][A-Za-z0-9+.-]*://[^/]*/|[A-Za-z]:/)")


def location_of(path: str) -> str:
    """Where a path lives: "nfs://host/" or a drive "r:/" (lower case); "" for anything else."""
    match = _ANY_LOCATION.match(normalize_path(path))
    return match.group(0).lower() if match else ""


class PathResolver:
    """Finds the path this device can open for an id another device wrote.

    Learned from the files this device played itself (own_paths): if another device has an id for
    the same file, the difference in front of the common tail (file name and folders) is the
    translation between the two locations, e.g. smb://nas/ and nfs://192.168.0.5/. The same
    location needs no translation. Nothing is guessed: unknown locations give None.
    """

    def __init__(self, own_paths: Iterable[str], known_ids: Iterable[str]) -> None:
        self.own = [path for path in own_paths if path]
        self.locations = {location_of(path): path for path in self.own if location_of(path)}
        by_name: dict[str, list[str]] = {}
        for media_id in known_ids:
            by_name.setdefault(file_name_key(media_id), []).append(media_id)
        pairs: dict[str, tuple[str, bool]] = {}
        for own in self.own:
            for media_id in by_name.get(file_name_key(own), ()):
                pair = self._pair(own, media_id)
                if pair:
                    pairs.setdefault(pair[1].lower(), (pair[0], "\\" in own))
        # longest foreign prefix first: the most specific translation wins
        self.pairs = sorted(pairs.items(), key=lambda item: len(item[0]), reverse=True)

    @staticmethod
    def _pair(own_path: str, foreign_id: str) -> tuple[str, str] | None:
        """(own prefix, foreign prefix) if both name the same file at different locations."""
        own, foreign = normalize_path(own_path).split("/"), normalize_path(foreign_id).split("/")
        common = 0
        while (
            common < min(len(own), len(foreign))
            and own[-1 - common].lower() == foreign[-1 - common].lower()
        ):
            common += 1
        if common < MIN_MATCH_PARTS or common >= len(own) or common >= len(foreign):
            return None
        own_prefix, foreign_prefix = (
            "/".join(own[:-common]) + "/",
            "/".join(foreign[:-common]) + "/",
        )
        return (
            None if own_prefix.lower() == foreign_prefix.lower() else (own_prefix, foreign_prefix)
        )

    def resolve(self, media_id: str) -> str | None:
        normal = normalize_path(media_id)
        same = self.locations.get(location_of(normal))
        if same:
            return normal.replace("/", "\\") if "\\" in same else normal
        for foreign_prefix, (own_prefix, backslashes) in self.pairs:
            if normal.lower().startswith(foreign_prefix):
                path = own_prefix + normal[len(foreign_prefix) :]
                return path.replace("/", "\\") if backslashes else path
        return None


def fingerprint(file: Any) -> str | None:
    """The fingerprint "s=<bytes>;h=<sha1 of first and last 64 KB>;" (None for an empty file)."""
    size = int(file.size())
    if size <= 0:
        return None
    # a content fingerprint of a file, not a security feature
    digest = hashlib.sha1()  # NOSONAR
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

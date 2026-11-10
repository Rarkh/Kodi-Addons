"""Comparison logic: which cached textures no longer belong to any library?

Plain Python without Kodi imports, so it can be tested.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Tuple
from urllib.parse import unquote, urlsplit

VIDEO = "video"
MUSIC = "music"
FANART_TV = "fanart.tv"  # serves both video and music art, decided by the URL path

# A normalised URL: (type of the image:// wrapper or "", key)
NormUrl = Tuple[str, str]  # noqa: UP006  (Kodi 19 may run on Python 3.8)

_CREDENTIALS = re.compile(r"^([a-z][a-z0-9+.-]*://)[^/@]*@", re.IGNORECASE)
_WRAPPER_TYPE = re.compile(r"^([a-z]+)@(.*)$", re.DOTALL)

# These textures never belong to the library: skin, add-ons, system resources
_PROTECTED_PREFIXES = ("special://", "resource://", "plugin://", "androidapp://", "addons://")
_PROTECTED_PARTS = ("/addons/", "/skin.", "/addon_data/")

# Image hosts used by scrapers
_VIDEO_HOSTS = (
    "image.tmdb.org",
    "thetvdb.com",
    "artworks.thetvdb.com",
    "m.media-amazon.com",
    "media-imdb.com",
    FANART_TV,
    "trakt.tv",
)
_MUSIC_HOSTS = (
    "theaudiodb.com",
    "coverartarchive.org",
    "archive.org",
    "lastfm.freetls.fastly.net",
    "last.fm",
    FANART_TV,
    "discogs.com",
    "musicbrainz.org",
)


def normalize(url: str) -> NormUrl:
    """Bring a URL into a comparable form.

    Resolves image:// wrappers (also nested) and percent-encoding; ignores options after
    '?', credentials in the URL, letter case and slash direction.
    """
    kind = ""
    text = (url or "").strip()
    for _ in range(5):
        if not text.lower().startswith("image://"):
            break
        text = text[len("image://") :]
        text = text.split("?", 1)[0].rstrip("/")
        match = _WRAPPER_TYPE.match(text)
        if match:
            kind, text = match.group(1).lower(), match.group(2)
        text = unquote(text)
    # Decode once more uniformly so that "%20" and " " match
    text = unquote(text).replace("\\", "/")
    text = _CREDENTIALS.sub(r"\1", text, count=1)
    return kind, text.rstrip("/").casefold()


def source_prefixes(source_files: Iterable[str]) -> list[str]:
    """Normalised paths of the library sources (multipath:// is resolved)."""
    result: list[str] = []
    for src in source_files:
        if src.lower().startswith("multipath://"):
            parts = [unquote(p) for p in src[len("multipath://") :].split("/") if p]
        else:
            parts = [src]
        for part in parts:
            key = normalize(part)[1]
            if key:
                result.append(key)
    return result


def _under(key: str, prefixes: Iterable[str]) -> bool:
    return any(key == p or key.startswith(p + "/") for p in prefixes)


def _host_matches(host: str, suffixes: Iterable[str]) -> bool:
    return any(host == s or host.endswith("." + s) for s in suffixes)


def _classify_web(key: str) -> str | None:
    """Category of an http(s) image URL, decided by its host (None = unknown host)."""
    parts = urlsplit(key)
    host = parts.hostname or ""
    if _host_matches(host, (FANART_TV,)):
        return MUSIC if "/music/" in parts.path or "musiclabel" in parts.path else VIDEO
    if _host_matches(host, _VIDEO_HOSTS):
        return VIDEO
    if _host_matches(host, _MUSIC_HOSTS):
        return MUSIC
    return None


def _classify_local(
    key: str, video_sources: Iterable[str], music_sources: Iterable[str]
) -> str | None:
    """Category of a local path, decided by the library sources it lies under."""
    if _under(key, video_sources):
        return VIDEO
    if _under(key, music_sources):
        return MUSIC
    return None


def classify(
    norm: NormUrl, video_sources: Iterable[str] = (), music_sources: Iterable[str] = ()
) -> str | None:
    """Return 'video', 'music' or None (None = do not touch)."""
    kind, key = norm
    if not key:
        return None
    if kind:  # image:// wrapper: video@ / music@ are known, others (picturefolder, ...) are not
        return kind if kind in (VIDEO, MUSIC) else None
    if key.startswith(_PROTECTED_PREFIXES) or any(part in key for part in _PROTECTED_PARTS):
        return None
    if key.startswith(("http://", "https://")):
        return _classify_web(key)
    return _classify_local(key, video_sources, music_sources)


@dataclass(frozen=True)
class Texture:
    texture_id: int
    url: str
    size: int
    category: str


def default_size(texture: dict) -> int:
    """File size of a texture; callers pass a real lookup (the cache file) as size_of."""
    return int(texture.get("filesize", 0) or 0)


def find_orphans(
    textures: Iterable[dict],
    referenced: Iterable[str],
    *,
    reference_categories: dict[str, int],
    video_sources: Iterable[str] = (),
    music_sources: Iterable[str] = (),
    include_music: bool = True,
    size_of: Callable[[dict], int] = default_size,
) -> list[Texture]:
    """Bestimmt verwaiste Texturen.

    reference_categories says how many library entries were found per category.
    A category without entries is never cleaned (protection against total loss).
    """
    refs = {normalize(u) for u in referenced if u}
    video_src = list(video_sources)
    music_src = list(music_sources)
    active = {VIDEO} if not include_music else {VIDEO, MUSIC}
    active = {c for c in active if reference_categories.get(c, 0) > 0}

    orphans: list[Texture] = []
    for tex in textures:
        url = tex.get("url", "")
        norm = normalize(url)
        category = classify(norm, video_src, music_src)
        if category is None or category not in active or norm in refs:
            continue
        orphans.append(Texture(int(tex["textureid"]), url, size_of(tex), category))
    return orphans

"""Vergleichslogik: welche gecachten Texturen gehören zu keiner Bibliothek mehr?

Reiner Python-Code ohne Kodi-Importe, damit er sich testen lässt.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Tuple
from urllib.parse import unquote, urlsplit

VIDEO = "video"
MUSIC = "music"

# Eine normalisierte URL: (Art des image://-Wrappers oder "", Schlüssel)
NormUrl = Tuple[str, str]  # noqa: UP006  (Kodi 19 läuft teils auf Python 3.8)

_CREDENTIALS = re.compile(r"^([a-z][a-z0-9+.-]*://)[^/@]*@", re.IGNORECASE)
_WRAPPER_TYPE = re.compile(r"^([a-z]+)@(.*)$", re.DOTALL)

# Diese Texturen gehören nie zur Bibliothek: Skin, Addons, Systemressourcen
_PROTECTED_PREFIXES = ("special://", "resource://", "plugin://", "androidapp://", "addons://")
_PROTECTED_PARTS = ("/addons/", "/skin.", "/addon_data/")

# Bildserver der Scraper (Host-Endung -> Kategorie)
_VIDEO_HOSTS = (
    "image.tmdb.org",
    "thetvdb.com",
    "artworks.thetvdb.com",
    "m.media-amazon.com",
    "media-imdb.com",
    "fanart.tv",
    "trakt.tv",
)
_MUSIC_HOSTS = (
    "theaudiodb.com",
    "coverartarchive.org",
    "archive.org",
    "lastfm.freetls.fastly.net",
    "last.fm",
    "fanart.tv",
    "discogs.com",
    "musicbrainz.org",
)


def normalize(url: str) -> NormUrl:
    """Bringt eine URL in eine vergleichbare Form.

    Löst image://-Wrapper (auch geschachtelt) und Prozent-Kodierung auf, ignoriert Optionen
    hinter '?', Zugangsdaten in der URL, Groß-/Kleinschreibung und Schrägstrich-Richtung.
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
    # Einheitlich ein weiteres Mal dekodieren, damit "%20" und " " zusammenpassen
    text = unquote(text).replace("\\", "/")
    text = _CREDENTIALS.sub(r"\1", text, count=1)
    return kind, text.rstrip("/").casefold()


def source_prefixes(source_files: Iterable[str]) -> list[str]:
    """Normalisierte Pfade der Bibliotheksquellen (multipath:// wird aufgelöst)."""
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


def classify(
    norm: NormUrl, video_sources: Iterable[str] = (), music_sources: Iterable[str] = ()
) -> str | None:
    """Gibt 'video', 'music' oder None zurück (None = nicht anfassen)."""
    kind, key = norm
    if not key:
        return None
    if kind == "video":
        return VIDEO
    if kind == "music":
        return MUSIC
    if kind:  # picturefolder, pictures, ...
        return None
    if key.startswith(_PROTECTED_PREFIXES) or any(part in key for part in _PROTECTED_PARTS):
        return None
    if key.startswith(("http://", "https://")):
        parts = urlsplit(key)
        host = parts.hostname or ""
        if _host_matches(host, ("fanart.tv",)):
            return MUSIC if "/music/" in parts.path or "musiclabel" in parts.path else VIDEO
        if _host_matches(host, _VIDEO_HOSTS):
            return VIDEO
        if _host_matches(host, _MUSIC_HOSTS):
            return MUSIC
        return None
    if _under(key, video_sources):
        return VIDEO
    if _under(key, music_sources):
        return MUSIC
    return None


@dataclass(frozen=True)
class Texture:
    texture_id: int
    url: str
    size: int
    category: str


def texture_size(texture: dict) -> int:
    return sum(int(s.get("size", 0) or 0) for s in texture.get("sizes", []) or [])


def find_orphans(
    textures: Iterable[dict],
    referenced: Iterable[str],
    *,
    reference_categories: dict[str, int],
    video_sources: Iterable[str] = (),
    music_sources: Iterable[str] = (),
    include_music: bool = True,
) -> list[Texture]:
    """Bestimmt verwaiste Texturen.

    reference_categories sagt, wie viele Bibliothekseinträge je Kategorie gefunden wurden.
    Eine Kategorie ohne Einträge wird nie bereinigt (Schutz gegen Totalverlust).
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
        orphans.append(Texture(int(tex["textureid"]), url, texture_size(tex), category))
    return orphans

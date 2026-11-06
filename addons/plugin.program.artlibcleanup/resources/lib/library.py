"""Fetch data from Kodi (JSON-RPC). The actual call function is passed in so the logic
can be tested without Kodi: call(method, params) -> result dict.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any, Callable, Dict

from resources.lib.orphans import MUSIC, VIDEO, source_prefixes

PAGE_SIZE = 200

Call = Callable[[str, Dict[str, Any]], Dict[str, Any]]
# progress(label, fraction 0..1); may raise an exception to cancel
Progress = Callable[[str, float], None]


class RpcError(Exception):
    """A JSON-RPC query failed."""


def make_call(execute: Callable[[str], str]) -> Call:
    """Wrap xbmc.executeJSONRPC as call(method, params)."""
    counter = {"id": 0}

    def call(method: str, params: dict[str, Any]) -> dict[str, Any]:
        counter["id"] += 1
        request = {"jsonrpc": "2.0", "id": counter["id"], "method": method, "params": params}
        try:
            response = json.loads(execute(json.dumps(request)))
        except ValueError as exc:
            raise RpcError(f"{method}: invalid response") from exc
        if "error" in response:
            message = response["error"].get("message", response["error"])
            raise RpcError(f"{method}: {message}")
        return response.get("result") or {}

    return call


def fetch_pages(
    call: Call,
    method: str,
    result_key: str,
    params: dict[str, Any],
    id_key: str,
    on_page: Callable[[int, int], None] | None = None,
) -> list[dict[str, Any]]:
    """Fetch all pages of a list query. on_page(fetched, total) reports progress."""
    items: list[dict[str, Any]] = []
    seen_ids: set[Any] = set()
    start = 0
    while True:
        page_params = dict(params, limits={"start": start, "end": start + PAGE_SIZE})
        result = call(method, page_params)
        page = result.get(result_key) or []
        total = int((result.get("limits") or {}).get("total", 0))
        fresh = 0
        for item in page:
            ident = item.get(id_key)
            if ident is not None:
                if ident in seen_ids:
                    continue
                seen_ids.add(ident)
            items.append(item)
            fresh += 1
        start += len(page)
        if on_page:
            on_page(len(items), max(total, len(items)))
        # Done: empty/short page, total reached, or the server ignores limits
        if not page or fresh == 0 or len(page) < PAGE_SIZE or (total and start >= total):
            return items


def _art_values(item: dict[str, Any]) -> Iterable[str]:
    for value in (item.get("art") or {}).values():
        if isinstance(value, str):
            yield value
    for key in ("thumbnail", "fanart"):
        if isinstance(item.get(key), str):
            yield item[key]
    for member in item.get("cast") or []:
        if isinstance(member.get("thumbnail"), str):
            yield member["thumbnail"]


@dataclass
class Collected:
    urls: set[str] = field(default_factory=set)
    # Number of library entries per category
    counts: dict[str, int] = field(default_factory=lambda: {VIDEO: 0, MUSIC: 0})
    video_sources: list[str] = field(default_factory=list)
    music_sources: list[str] = field(default_factory=list)


_VIDEO_QUERIES = (
    ("VideoLibrary.GetMovies", "movies", "movieid", ["art", "thumbnail", "fanart", "cast"]),
    ("VideoLibrary.GetMovieSets", "sets", "setid", ["art", "thumbnail", "fanart"]),
    ("VideoLibrary.GetTVShows", "tvshows", "tvshowid", ["art", "thumbnail", "fanart", "cast"]),
    ("VideoLibrary.GetSeasons", "seasons", "seasonid", ["art", "thumbnail", "fanart"]),
    ("VideoLibrary.GetEpisodes", "episodes", "episodeid", ["art", "thumbnail", "fanart", "cast"]),
    ("VideoLibrary.GetMusicVideos", "musicvideos", "musicvideoid", ["art", "thumbnail", "fanart"]),
)
_MUSIC_QUERIES = (
    ("AudioLibrary.GetArtists", "artists", "artistid", ["art", "thumbnail", "fanart"]),
    ("AudioLibrary.GetAlbums", "albums", "albumid", ["art", "thumbnail", "fanart"]),
    ("AudioLibrary.GetSongs", "songs", "songid", ["art", "thumbnail", "fanart"]),
)


def _sources(call: Call, media: str) -> list[str]:
    result = call("Files.GetSources", {"media": media})
    return source_prefixes(s.get("file", "") for s in result.get("sources") or [])


def collect_library(call: Call, include_music: bool, progress: Progress) -> Collected:
    """Collect all image URLs used by the libraries, and the source paths.

    Raises RpcError if any query fails: in that case nothing may be deleted.
    """
    queries = [(VIDEO, *q) for q in _VIDEO_QUERIES]
    if include_music:
        queries += [(MUSIC, *q) for q in _MUSIC_QUERIES]
    steps = len(queries) + 1  # +1 for the sources
    collected = Collected()

    progress("Files.GetSources", 0.0)
    collected.video_sources = _sources(call, "video")
    if include_music:
        collected.music_sources = _sources(call, "music")

    for index, (category, method, key, id_key, props) in enumerate(queries, start=1):

        def on_page(done: int, total: int, index: int = index, method: str = method) -> None:
            fraction = done / total if total else 1.0
            progress(f"{method} ({done}/{total})", (index + fraction) / steps)

        items = fetch_pages(call, method, key, {"properties": props}, id_key, on_page)
        collected.counts[category] += len(items)
        for item in items:
            collected.urls.update(_art_values(item))
    return collected


def fetch_textures(call: Call, progress: Progress) -> list[dict[str, Any]]:
    def on_page(done: int, total: int) -> None:
        progress(f"Textures.GetTextures ({done}/{total})", done / total if total else 0.0)

    return fetch_pages(
        call,
        "Textures.GetTextures",
        "textures",
        {"properties": ["url", "sizes"]},
        "textureid",
        on_page,
    )

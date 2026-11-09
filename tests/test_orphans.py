"""Tests of the orphan logic with mocked JSON-RPC responses."""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

import pytest
from resources.lib import library
from resources.lib.library import RpcError, collect_library, fetch_textures
from resources.lib.orphans import (
    MUSIC,
    VIDEO,
    classify,
    find_orphans,
    normalize,
    source_prefixes,
)


def wrap(url: str, kind: str = "") -> str:
    prefix = f"{kind}@" if kind else ""
    return f"image://{prefix}{quote(url, safe='')}/"


TMDB_POSTER = "https://image.tmdb.org/t/p/original/poster.jpg"
TMDB_OLD = "https://image.tmdb.org/t/p/original/deleted-movie.jpg"
ACTOR = "smb://nas/Filme/.actors/Jane Doe.jpg"
LOCAL_OLD = "smb://nas/Filme/Gone (2001)/fanart.jpg"
ALBUM = "https://www.theaudiodb.com/images/media/album/thumb/abc.jpg"
ALBUM_OLD = "https://www.theaudiodb.com/images/media/album/thumb/old.jpg"


class FakeKodi:
    """Answers JSON-RPC calls from fixed data, with paging like Kodi."""

    def __init__(self, library_data: dict[str, list[dict]], textures: list[dict]) -> None:
        self.data = dict(library_data)
        self.data["Textures.GetTextures"] = textures
        self.calls: list[str] = []
        self.removed: list[int] = []
        self.fail_on: str | None = None
        self.reject_sizes = False

    KEYS = {
        "VideoLibrary.GetMovies": "movies",
        "VideoLibrary.GetMovieSets": "sets",
        "VideoLibrary.GetTVShows": "tvshows",
        "VideoLibrary.GetSeasons": "seasons",
        "VideoLibrary.GetEpisodes": "episodes",
        "VideoLibrary.GetMusicVideos": "musicvideos",
        "AudioLibrary.GetArtists": "artists",
        "AudioLibrary.GetAlbums": "albums",
        "AudioLibrary.GetSongs": "songs",
        "Textures.GetTextures": "textures",
    }

    def __call__(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(method)
        if method == self.fail_on:
            raise RpcError(f"{method}: boom")
        if method == "Files.GetSources":
            files = {
                "video": [{"file": "smb://nas/Filme/"}, {"file": "multipath://%2fx%2fa/b/"}],
                "music": [{"file": "smb://nas/Musik/"}],
            }
            return {"sources": files[params["media"]]}
        if method == "Textures.RemoveTexture":
            self.removed.append(params["textureid"])
            return {}
        items = self.data.get(method, [])
        if method == "Textures.GetTextures":
            # Like real Kodi: no `limits` support, everything in one answer
            if "limits" in params:
                raise RpcError(f"{method}: Invalid params.")
            if self.reject_sizes and "sizes" in params["properties"]:
                raise RpcError(f"{method}: Invalid params.")
            return {"textures": items}
        lim = params["limits"]
        return {
            self.KEYS[method]: items[lim["start"] : lim["end"]],
            "limits": {"start": lim["start"], "end": lim["end"], "total": len(items)},
        }


def make_fake(**overrides: Any) -> FakeKodi:
    library_data = {
        "VideoLibrary.GetMovies": [
            {
                "movieid": 1,
                "art": {
                    "poster": wrap(TMDB_POSTER),
                    "fanart": wrap(LOCAL_OLD.replace("Gone", "Here")),
                },
                "cast": [{"name": "Jane", "thumbnail": wrap(ACTOR)}],
            }
        ],
        "AudioLibrary.GetAlbums": [{"albumid": 5, "art": {"thumb": wrap(ALBUM)}}],
    }
    textures = [
        {"textureid": 1, "url": TMDB_POSTER, "sizes": [{"size": 1000}, {"size": 500}]},
        {"textureid": 2, "url": TMDB_OLD, "sizes": [{"size": 2000}]},
        {"textureid": 3, "url": ACTOR, "sizes": [{"size": 300}]},
        {"textureid": 4, "url": "smb://nas/Filme/Here (2001)/fanart.jpg", "sizes": [{"size": 10}]},
        {"textureid": 5, "url": LOCAL_OLD, "sizes": [{"size": 4000}]},
        {"textureid": 6, "url": "special://skin/media/icon.png", "sizes": [{"size": 99}]},
        {"textureid": 7, "url": "C:/Users/x/addons/plugin.video.foo/icon.png", "sizes": []},
        {"textureid": 8, "url": ALBUM, "sizes": [{"size": 70}]},
        {"textureid": 9, "url": ALBUM_OLD, "sizes": [{"size": 800}]},
        {"textureid": 10, "url": "https://example.org/foreign-addon.jpg", "sizes": [{"size": 5}]},
        {
            "textureid": 11,
            "url": wrap("smb://nas/Filme/Gone.mkv", "video"),
            "sizes": [{"size": 60}],
        },
    ]
    library_data.update(overrides)
    return FakeKodi(library_data, textures)


def run_scan(fake: FakeKodi, include_music: bool = True):
    collected = collect_library(fake, include_music, lambda label, fraction: None)
    textures = fetch_textures(fake, lambda label, fraction: None)
    orphans = find_orphans(
        textures,
        collected.urls,
        reference_categories=collected.counts,
        video_sources=collected.video_sources,
        music_sources=collected.music_sources,
        include_music=include_music,
    )
    return collected, {o.texture_id: o for o in orphans}


def test_normalize_unwraps_and_decodes():
    assert normalize(wrap(TMDB_POSTER)) == normalize(TMDB_POSTER)
    assert normalize(wrap(wrap(TMDB_POSTER))) == normalize(TMDB_POSTER)
    assert normalize(wrap(TMDB_POSTER) + "?size=big") == normalize(TMDB_POSTER)
    assert normalize("smb://user:pw@nas/a%20b.jpg") == normalize("smb://nas/a b.jpg")
    assert normalize("C:\\Medien\\Poster.JPG") == normalize("c:/medien/poster.jpg")
    assert normalize(wrap("smb://nas/x.mkv", "video"))[0] == "video"
    assert normalize("") == ("", "")


def test_source_prefixes_resolves_multipath():
    assert source_prefixes(["multipath://%2fx%2fa/b/", "smb://nas/Filme/"]) == [
        "/x/a",
        "b",
        "smb://nas/filme",
    ]


def test_classify_protects_skin_and_addon_art():
    sources = ["smb://nas/filme"]
    assert classify(normalize("special://skin/x.png"), sources) is None
    assert classify(normalize("C:/Kodi/addons/skin.estuary/x.png"), sources) is None
    assert classify(normalize("https://example.org/a.jpg"), sources) is None
    assert classify(normalize("plugin://plugin.video.foo/x"), sources) is None
    assert classify(normalize(TMDB_POSTER), sources) == VIDEO
    assert classify(normalize(ALBUM), sources, []) == MUSIC
    assert classify(normalize("https://assets.fanart.tv/fanart/music/x/a.jpg")) == MUSIC
    assert classify(normalize("https://assets.fanart.tv/fanart/movies/x/a.jpg")) == VIDEO
    assert classify(normalize("smb://nas/filme/x.jpg"), sources) == VIDEO
    assert classify(normalize("smb://nas/filmeX/x.jpg"), sources) is None
    assert classify(normalize(wrap("a.jpg", "picturefolder"))) is None


def test_finds_only_unreferenced_library_art():
    _, orphans = run_scan(make_fake())
    assert set(orphans) == {2, 5, 9, 11}
    assert orphans[2].size == 2000
    assert orphans[2].category == VIDEO
    assert orphans[9].category == MUSIC
    assert orphans[11].category == VIDEO  # video@ thumbnail of a removed file


def test_music_can_be_excluded():
    fake = make_fake()
    collected, orphans = run_scan(fake, include_music=False)
    assert set(orphans) == {2, 5, 11}
    assert "AudioLibrary.GetAlbums" not in fake.calls
    assert collected.counts[MUSIC] == 0


def test_empty_video_library_deletes_nothing():
    _, orphans = run_scan(make_fake(**{"VideoLibrary.GetMovies": []}))
    assert all(o.category == MUSIC for o in orphans.values())
    _, orphans = run_scan(make_fake(**{"VideoLibrary.GetMovies": [], "AudioLibrary.GetAlbums": []}))
    assert orphans == {}


def test_failed_query_raises_so_nothing_is_deleted():
    fake = make_fake()
    fake.fail_on = "VideoLibrary.GetEpisodes"
    with pytest.raises(RpcError):
        collect_library(fake, True, lambda label, fraction: None)
    assert "Textures.RemoveTexture" not in fake.calls


def test_paging_collects_every_page(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(library, "PAGE_SIZE", 2)
    movies = [{"movieid": i, "art": {"poster": f"http://x/{i}.jpg"}} for i in range(1, 6)]
    fake = make_fake(**{"VideoLibrary.GetMovies": movies})
    collected, _ = run_scan(fake)
    assert collected.counts[VIDEO] == 5
    assert {f"http://x/{i}.jpg" for i in range(1, 6)} <= collected.urls
    assert fake.calls.count("VideoLibrary.GetMovies") == 3


def test_paging_survives_server_that_ignores_limits(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(library, "PAGE_SIZE", 2)
    items = [{"textureid": i, "url": f"u{i}"} for i in range(1, 4)]

    def call(method: str, params: dict[str, Any]) -> dict[str, Any]:
        return {"textures": items}

    assert len(library.fetch_pages(call, "Textures.GetTextures", "textures", {}, "textureid")) == 3


def test_make_call_reports_errors_and_bad_json():
    ok = library.make_call(lambda req: '{"id":1,"result":{"a":1}}')
    assert ok("X", {}) == {"a": 1}
    failing = library.make_call(lambda req: '{"error":{"message":"nope"}}')
    with pytest.raises(RpcError, match="nope"):
        failing("X", {})
    garbage = library.make_call(lambda req: "<html>")
    with pytest.raises(RpcError):
        garbage("X", {})


def test_textures_are_fetched_without_limits():
    fake = make_fake()
    assert len(fetch_textures(fake, lambda label, fraction: None)) == 11


def test_textures_fall_back_when_sizes_is_rejected():
    fake = make_fake()
    fake.reject_sizes = True
    assert len(fetch_textures(fake, lambda label, fraction: None)) == 11
    assert fake.calls.count("Textures.GetTextures") == 2


def test_normalize_stops_after_five_wrapper_levels():
    deep = TMDB_POSTER
    for _ in range(6):
        deep = wrap(deep)
    # More nesting than the loop resolves: still returns a key without raising
    assert normalize(deep)[1]


def test_source_prefixes_skips_empty_parts():
    assert source_prefixes(["", "multipath://%2fx%2f/", "smb://nas/a/"]) == ["/x", "smb://nas/a"]


def test_classify_handles_empty_and_typed_keys():
    assert classify(("", "")) is None
    assert classify(("video", "smb://nas/x.jpg")) == VIDEO
    assert classify(("music", "smb://nas/x.jpg")) == MUSIC


def test_classify_matches_local_music_sources():
    assert classify(normalize("smb://nas/musik/a.jpg"), [], ["smb://nas/musik"]) == MUSIC
    assert classify(normalize("smb://nas/other/a.jpg"), [], ["smb://nas/musik"]) is None


def test_paging_keeps_items_without_id():
    items = [{"name": "a"}, {"name": "b"}]
    fake = make_fake(**{"VideoLibrary.GetMovies": items})
    fetched = library.fetch_pages(fake, "VideoLibrary.GetMovies", "movies", {}, "movieid")
    assert [i["name"] for i in fetched] == ["a", "b"]


def test_art_values_ignore_non_string_values():
    item = {"art": {"poster": None, "fanart": wrap(TMDB_POSTER)}, "thumbnail": None}
    item["cast"] = [{"thumbnail": None}, {"thumbnail": ACTOR}]
    assert set(library._art_values(item)) == {wrap(TMDB_POSTER), ACTOR}


def test_collect_reads_top_level_thumbnails():
    sets = [{"setid": 1, "thumbnail": wrap(TMDB_POSTER), "fanart": ACTOR}]
    collected, _ = run_scan(make_fake(**{"VideoLibrary.GetMovieSets": sets}))
    assert {wrap(TMDB_POSTER), ACTOR} <= collected.urls

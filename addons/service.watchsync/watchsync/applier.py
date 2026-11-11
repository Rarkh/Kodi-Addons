"""Apply the NAS state to Kodi's own database, and notice manual changes made in Kodi.

The NAS is the truth; Kodi's database is only the display. An entry is written to Kodi only
if its timestamp is newer than what this add-on last applied for that medium.
"""

from __future__ import annotations

import json
from typing import Any, Callable, Dict, Iterable, NamedTuple

from .events import Event, parse_ts
from .logger import DEBUG, INFO, WARNING
from .mediaid import (
    Mapping,
    PathResolver,
    file_name_key,
    from_media_id,
    is_trackable,
    same_medium,
    to_media_id,
)

Rpc = Callable[[str, Dict[str, Any]], Dict[str, Any]]
Applied = Dict[str, Dict[str, Any]]

ECHO_WINDOW_S = 5.0


class Kind(NamedTuple):
    item_type: str  # "type" in VideoLibrary.OnUpdate
    list_method: str
    list_key: str
    id_key: str
    set_method: str
    details_method: str
    details_key: str


LIBRARY_KINDS = (
    Kind(
        "movie",
        "VideoLibrary.GetMovies",
        "movies",
        "movieid",
        "VideoLibrary.SetMovieDetails",
        "VideoLibrary.GetMovieDetails",
        "moviedetails",
    ),
    Kind(
        "episode",
        "VideoLibrary.GetEpisodes",
        "episodes",
        "episodeid",
        "VideoLibrary.SetEpisodeDetails",
        "VideoLibrary.GetEpisodeDetails",
        "episodedetails",
    ),
    Kind(
        "musicvideo",
        "VideoLibrary.GetMusicVideos",
        "musicvideos",
        "musicvideoid",
        "VideoLibrary.SetMusicVideoDetails",
        "VideoLibrary.GetMusicVideoDetails",
        "musicvideodetails",
    ),
)
KINDS_BY_TYPE = {kind.item_type: kind for kind in LIBRARY_KINDS}


class RpcError(Exception):
    """A JSON-RPC call failed."""


def make_rpc(execute: Callable[[str], str]) -> Rpc:
    """Wrap xbmc.executeJSONRPC as rpc(method, params) -> result."""
    counter = {"id": 0}

    def rpc(method: str, params: dict[str, Any]) -> dict[str, Any]:
        counter["id"] += 1
        request = {"jsonrpc": "2.0", "id": counter["id"], "method": method, "params": params}
        try:
            response = json.loads(execute(json.dumps(request)))
        except ValueError as exc:
            raise RpcError(f"{method}: invalid response") from exc
        if "error" in response:
            raise RpcError(f"{method}: {response['error'].get('message', response['error'])}")
        return response.get("result") or {}

    return rpc


def details(event: Event) -> dict[str, Any]:
    """The Kodi fields (playcount, resume) an event stands for."""
    total = event.duration_s or 0
    if event.type == "progress":
        resume = {"position": event.position_s, "total": total}
        return {"playcount": event.playcount, "resume": resume}
    if event.type == "reset":
        return {"resume": {"position": 0, "total": 0}}
    count = event.playcount if event.type == "watched" else 0
    return {"playcount": count, "resume": {"position": 0, "total": 0}}


def is_applied(event: Event, applied: Applied) -> bool:
    """True if Kodi already got this event or a newer one."""
    entry = applied.get(event.media_id)
    return bool(entry) and parse_ts(entry["ts"]) >= parse_ts(event.ts)


def applied_entry(event: Event, ok: bool = True, via: str = "library") -> dict[str, Any]:
    """What was written to Kodi; via says whether to a library item or to a file path."""
    return {
        "playcount": event.playcount,
        "position_s": event.position_s,
        "ts": event.ts,
        "ok": ok,
        "via": via,
    }


class KodiApplier:
    def __init__(
        self,
        rpc: Rpc,
        mappings: list[Mapping],
        mono: Callable[[], float],
        log: Callable[..., None],
    ) -> None:
        self.rpc = rpc
        self.mappings = mappings
        self.mono = mono
        self.log = log
        self.file_details_failed = False
        # (item type, database id) -> (playcount we set, time) for echo suppression
        self.echo: dict[tuple[str, int], tuple[int, float]] = {}

    def is_echo(self, item_type: str, dbid: int, playcount: int) -> bool:
        """True if Kodi only reports the playcount this add-on set a moment ago."""
        entry = self.echo.get((item_type, dbid))
        return bool(entry) and entry[0] == playcount and self.mono() - entry[1] <= ECHO_WINDOW_S

    def _library_index(self) -> tuple[dict[str, tuple[Kind, int]], bool]:
        """media id -> (kind, database id); False if a query failed (index incomplete)."""
        index: dict[str, tuple[Kind, int]] = {}
        complete = True
        for kind in LIBRARY_KINDS:
            try:
                items = self.rpc(kind.list_method, {"properties": ["file"]}).get(kind.list_key, [])
            except RpcError as exc:
                self.log(f"Library query failed: {exc}", WARNING)
                complete = False
                continue
            for item in items:
                path = item.get("file", "")
                if is_trackable(path):
                    index[to_media_id(path, self.mappings).id] = (kind, item[kind.id_key])
        return index, complete

    @staticmethod
    def _find_target(
        index: dict[str, tuple[Kind, int]], by_name: dict[str, list[str]], media_id: str
    ) -> tuple[Kind, int] | None:
        """The library item of a medium: same id, otherwise the same file under another path.

        Other devices reach the same file by another protocol, host or drive, so without a path
        mapping the ids differ; the last folders and the file name identify it."""
        if media_id in index:
            return index[media_id]
        for candidate in by_name.get(file_name_key(media_id), ()):
            if same_medium(candidate, media_id):
                return index[candidate]
        return None

    def _apply_library(self, kind: Kind, dbid: int, event: Event) -> None:
        values = details(event)
        if "playcount" in values:
            self.echo[(kind.item_type, dbid)] = (values["playcount"], self.mono())
        self.rpc(kind.set_method, {kind.id_key: dbid, **values})

    def _apply_file(self, event: Event, resolver: PathResolver) -> bool:
        """Files outside the library: Files.SetFileDetails for a path this device can open.

        A mapped id gives that path; for a full path another device used, the resolver translates
        it with what this device learned from its own playbacks (same location, or the
        difference between two locations). Without a known path nothing is written: details for a
        foreign path would only look like success. The fallback seek at the start of the playback
        covers those files."""
        path = resolver.resolve(event.media_id) if event.unmapped else None
        path = path or (None if event.unmapped else from_media_id(event.media_id, self.mappings))
        if not path:
            self.log(f"No local path known for {event.media_id}: not written to Kodi", INFO)
            return False
        try:
            self.rpc("Files.SetFileDetails", {"file": path, "media": "video", **details(event)})
        except RpcError as exc:
            if not self.file_details_failed:
                self.file_details_failed = True
                self.log(f"Files.SetFileDetails failed for {path}: {exc}", WARNING)
            return False
        self.log(f"Files.SetFileDetails wrote {event.type} {event.position_s:.0f} s: {path}", INFO)
        return True

    def apply(
        self,
        events: Iterable[Event],
        applied: Applied,
        playing: str | None = None,
        own_paths: Iterable[str] = (),
    ) -> int:
        """Write entries newer than the applied state to Kodi; returns how many worked.

        The medium that is playing right now is skipped: its foreign events wait until playback
        stopped and are then compared with the own stop event by the merge rules.
        """
        known = list(events)
        pending = [e for e in known if e.media_id != playing and not is_applied(e, applied)]
        if not pending:
            return 0
        resolver = PathResolver(own_paths, [e.media_id for e in known])
        index, complete = self._library_index()
        by_name: dict[str, list[str]] = {}
        for media_id in index:
            by_name.setdefault(file_name_key(media_id), []).append(media_id)
        done = 0
        for event in pending:
            target = self._find_target(index, by_name, event.media_id)
            if not target and not complete:
                continue  # try again at the next sync
            try:
                if target:
                    self._apply_library(target[0], target[1], event)
                    ok = True
                else:
                    ok = self._apply_file(event, resolver)
            except RpcError as exc:
                self.log(f"Could not apply {event.media_id}: {exc}", WARNING)
                continue
            applied[event.media_id] = applied_entry(event, ok, "library" if target else "file")
            done += ok
        self.log(f"Applied {done} of {len(pending)} changes to Kodi", INFO)
        return done


class ManualChanges:
    """Turns "marked as watched/unwatched in Kodi" (VideoLibrary.OnUpdate) into events."""

    def __init__(
        self,
        rpc: Rpc,
        mappings: list[Mapping],
        engine: Any,
        applier: KodiApplier,
        log: Callable[..., None],
    ) -> None:
        self.rpc = rpc
        self.mappings = mappings
        self.engine = engine
        self.applier = applier
        self.log = log

    def _path_of(self, kind: Kind, dbid: int) -> str:
        params = {kind.id_key: dbid, "properties": ["file"]}
        try:
            return str(
                self.rpc(kind.details_method, params).get(kind.details_key, {}).get("file", "")
            )
        except RpcError as exc:
            self.log(f"Could not resolve the file of {kind.item_type} {dbid}: {exc}", WARNING)
            return ""

    def handle(self, data: str) -> bool:
        """Process one notification payload; True if an event was created."""
        try:
            payload = json.loads(data)
        except ValueError:
            return False
        item = payload.get("item") if isinstance(payload, dict) else None
        playcount = payload.get("playcount") if isinstance(payload, dict) else None
        if not isinstance(item, dict) or isinstance(playcount, bool):
            return False
        kind, dbid = KINDS_BY_TYPE.get(item.get("type", "")), item.get("id")
        if not kind or not isinstance(dbid, int) or not isinstance(playcount, int):
            return False
        if self.applier.is_echo(kind.item_type, dbid, playcount):
            self.log(f"Ignored echo of our own change ({kind.item_type} {dbid})", DEBUG)
            return False
        path = self._path_of(kind, dbid)
        if not is_trackable(path):
            return False
        ref = to_media_id(path, self.mappings)
        known = self.engine.lookup(ref.id, None)
        if known and known.playcount == playcount:
            return False  # Kodi writes the same value at the end of a playback
        self.engine.record(
            ref.id,
            "watched" if playcount > 0 else "unwatched",
            0.0,
            known.duration_s if known else None,
            playcount,
            fp=known.fp if known else None,
            unmapped=ref.unmapped,
        )
        self.log(f"Manual change in Kodi: {ref.id} playcount {playcount}", INFO)
        return True

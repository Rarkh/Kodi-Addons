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
    is_file_folder,
    is_trackable,
    same_medium,
    to_media_id,
)

Rpc = Callable[[str, Dict[str, Any]], Dict[str, Any]]
Applied = Dict[str, Dict[str, Any]]

# The library list is reused this long (a miss or a failed write refreshes it at once)
LIBRARY_INDEX_TTL_S = 300.0
MAX_KNOWN_COUNTS = 5000
# Service steps (seconds) a folder must stay shown before it is looked at
SETTLE_STEPS = 3
# A marked folder: how deep and how many folders are listed (each is a listing of the share)
MAX_TREE_DEPTH = 3
MAX_TREE_LISTINGS = 40
MAX_KNOWN_FILES = 300
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
        # the library list (media id -> item, file name -> ids, complete) and when it was read
        self._library: tuple[dict[str, tuple[Kind, int]], dict[str, list[str]], bool] | None = None
        self._library_at = 0.0
        # files outside the library this add-on wrote to: path -> media id, path -> playcount
        self.file_media: dict[str, str] = {}
        self.file_playcounts: dict[str, int] = {}
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
        self._remember_file(path, event)
        return True

    def _remember_file(self, path: str, event: Event) -> None:
        """What was written is the baseline for noticing a later manual change of the file."""
        self.file_media.pop(path, None)
        self.file_media[path] = event.media_id
        while len(self.file_media) > MAX_KNOWN_FILES:
            self.file_media.pop(next(iter(self.file_media)))
        values = details(event)
        if "playcount" in values:
            self.file_playcounts[path] = values["playcount"]

    def _library_lists(
        self, refresh: bool
    ) -> tuple[dict[str, tuple[Kind, int]], dict[str, list[str]], bool, bool]:
        """The library as media id -> item and file name -> ids, plus whether it is complete and
        whether it was just read. A complete list is reused for LIBRARY_INDEX_TTL_S seconds
        (reading a big library over JSON-RPC takes a while)."""
        cached = self._library
        if cached and not refresh and self.mono() - self._library_at < LIBRARY_INDEX_TTL_S:
            return (*cached, False)
        index, complete = self._library_index()
        by_name: dict[str, list[str]] = {}
        for media_id in index:
            by_name.setdefault(file_name_key(media_id), []).append(media_id)
        self._library = (index, by_name, complete) if complete else None
        self._library_at = self.mono()
        return index, by_name, complete, True

    def apply(
        self,
        events: Iterable[Event],
        applied: Applied,
        playing: str | None = None,
        own_paths: Iterable[str] = (),
        known_ids: Iterable[str] | None = None,
    ) -> int:
        """Write entries newer than the applied state to Kodi; returns how many worked.

        The medium that is playing right now is skipped: its foreign events wait until playback
        stopped and are then compared with the own stop event by the merge rules. known_ids are
        all media ids this device has a state for (the events may be only the changed ones).
        """
        known = list(events)
        pending = [e for e in known if e.media_id != playing and not is_applied(e, applied)]
        if not pending:
            return 0
        resolver = PathResolver(
            own_paths, known_ids if known_ids is not None else [e.media_id for e in known]
        )
        index, by_name, complete, fresh = self._library_lists(refresh=False)
        done = 0
        for event in pending:
            target = self._find_target(index, by_name, event.media_id)
            if not target and not fresh:
                # not in the reused list: new in the library? read it again once
                index, by_name, complete, fresh = self._library_lists(refresh=True)
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
                self._library = None  # the list may be out of date (item removed)
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
        if (
            not isinstance(item, dict)
            or isinstance(playcount, bool)
            or not isinstance(playcount, int)
        ):
            self.log(f"Ignored notification: {data[:300]}", DEBUG)
            return False
        kind, dbid = KINDS_BY_TYPE.get(item.get("type", "")), item.get("id")
        if kind and isinstance(dbid, int):
            if self.applier.is_echo(kind.item_type, dbid, playcount):
                self.log(f"Ignored echo of our own change ({kind.item_type} {dbid})", DEBUG)
                return False
            path = self._path_of(kind, dbid)
        else:
            # not a library item (a file marked in a file list): the notification may name the file
            path = str(item.get("file") or "")
        if not is_trackable(path):
            self.log(f"Ignored notification without a usable file: {data[:300]}", DEBUG)
            return False
        return self.record_path(path, playcount)

    def record_path(self, path: str, playcount: int, skip_unknown_unwatched: bool = False) -> bool:
        """Create the watched/unwatched event for a file whose playcount was changed by hand.

        skip_unknown_unwatched: a file nobody knows and that is not watched is nothing to send."""
        ref = to_media_id(path, self.mappings)
        known = self.engine.lookup(ref.id, None)
        if known and known.playcount == playcount:
            return False  # Kodi writes the same value at the end of a playback
        if not known and playcount == 0 and skip_unknown_unwatched:
            return False
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


class FilePoller:
    """Notices "marked as watched/unwatched" on files outside the library.

    Kodi does not have to announce such a change. So the folder shown in the file view is asked
    for its files and their playcounts (one request, also for files this add-on never saw); a
    change since the last look is a manual change. What this add-on wrote itself is the baseline,
    so its own writes are no change.

    Asking costs Kodi a listing of the share, and many requests next to Kodi listing a share once
    froze Kodi (navigating up a folder). So the service only calls touch() every second with the
    shown folder: after SETTLE_STEPS in a row one look is the baseline. check() compares with it,
    when a context menu closed (a mark was just set) and now and then; nothing is asked while the
    shown folder changes."""

    def __init__(
        self,
        rpc: Rpc,
        mappings: list[Mapping],
        manual: ManualChanges,
        applier: KodiApplier,
    ) -> None:
        self.rpc = rpc
        self.mappings = mappings
        self.manual = manual
        self.applier = applier
        self.last_folder = ""
        self.steady = 0
        self.baselined = ""

    def _items(self, folder: str) -> tuple[dict[str, int], list[str]]:
        """(path -> playcount of the files, paths of the sub folders) of a folder; nothing if Kodi
        cannot list it."""
        params = {"directory": folder, "media": "video", "properties": ["playcount"]}
        try:
            items = self.rpc("Files.GetDirectory", params).get("files", [])
        except RpcError:
            return {}, []
        files: dict[str, int] = {}
        folders: list[str] = []
        for item in items:
            path, count = item.get("file"), item.get("playcount")
            if not isinstance(path, str):
                continue
            if item.get("filetype") == "directory":
                folders.append(path)
            elif isinstance(count, int) and not isinstance(count, bool):
                files[path] = count
        return files, folders

    def _compare(self, folder: str) -> int:
        changes = 0
        counts = self.applier.file_playcounts
        for path, count in self._items(folder)[0].items():
            last = counts.pop(path, None)
            counts[path] = count
            if last is not None and last != count and self.manual.record_path(path, count):
                changes += 1
        while len(counts) > MAX_KNOWN_COUNTS:
            counts.pop(next(iter(counts)))
        return changes

    def touch(self, folder: str) -> None:
        """Called every second with the shown folder: once it stayed for SETTLE_STEPS calls, the
        first look at it is the baseline."""
        if folder == self.last_folder:
            self.steady += 1
        else:
            self.last_folder, self.steady, self.baselined = folder, 0, ""
        if is_file_folder(folder) and self.steady >= SETTLE_STEPS and self.baselined != folder:
            self._compare(folder)
            self.baselined = folder

    def check(self, folder: str) -> int:
        """Compare the shown folder with the baseline; returns how many manual changes became
        events. Nothing happens for a folder without a baseline."""
        if not self.baselined or folder != self.baselined or folder != self.last_folder:
            return 0
        return self._compare(folder)

    def sync_tree(self, folder: str) -> int:
        """A folder was marked (all files in it, also in sub folders, got the same playcount):
        send the state of its files that differs from what this device knows. Unlike a single
        file this has no baseline: the NAS state is the comparison. Returns the events made."""
        if not is_file_folder(folder):
            return 0
        changes = 0
        counts = self.applier.file_playcounts
        todo = [(folder if folder.endswith("/") else folder + "/", 0)]
        listings = 0
        while todo and listings < MAX_TREE_LISTINGS:
            directory, depth = todo.pop(0)
            listings += 1
            files, folders = self._items(directory)
            for path, count in files.items():
                counts.pop(path, None)
                counts[path] = count
                if self.manual.record_path(path, count, skip_unknown_unwatched=True):
                    changes += 1
            if depth < MAX_TREE_DEPTH:
                todo.extend((sub, depth + 1) for sub in folders)
        return changes

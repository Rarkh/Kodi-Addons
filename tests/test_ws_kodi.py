"""WatchSync: applying the NAS state to Kodi, echo suppression, manual changes, tracking."""

from __future__ import annotations

import json
from typing import Any

import pytest
from test_ws_engine import Rig, record
from watchsync.applier import (
    ECHO_WINDOW_S,
    MAX_KNOWN_FILES,
    FilePoller,
    KodiApplier,
    ManualChanges,
    RpcError,
    applied_entry,
    details,
    is_applied,
    make_rpc,
)
from watchsync.events import Event
from watchsync.mediaid import Fingerprinter, parse_mappings
from watchsync.player import Current, PlayerMonitor, Tracker

MAPS = parse_mappings("nfs://nas/Media/|R:\\")


def ev(media="Filme/A.mkv", ts="2026-11-10T12:00:00.000Z", kind="progress", **kw: Any) -> Event:
    kw.setdefault("device", "other")
    kw.setdefault("seq", 1)
    return Event(ts=ts, type=kind, media_id=media, **kw)


# ---- JSON-RPC wrapper --------------------------------------------------------------------


def test_make_rpc():
    seen: list[dict] = []

    def execute(text: str) -> str:
        request = json.loads(text)
        seen.append(request)
        method = request["method"]
        if method == "bad":
            return "no json"
        if method == "error":
            return json.dumps({"error": {"message": "nope"}})
        if method == "empty":
            return json.dumps({"result": None})
        return json.dumps({"result": {"ok": 1}})

    rpc = make_rpc(execute)
    assert rpc("a", {"x": 1}) == {"ok": 1}
    assert rpc("empty", {}) == {}
    assert seen[1]["id"] == 2
    with pytest.raises(RpcError, match="invalid response"):
        rpc("bad", {})
    with pytest.raises(RpcError, match="nope"):
        rpc("error", {})


def test_details_per_event_type():
    assert details(ev(position_s=5.0, duration_s=9.0, playcount=1)) == {
        "playcount": 1,
        "resume": {"position": 5.0, "total": 9.0},
    }
    assert details(ev(position_s=5.0))["resume"]["total"] == 0
    assert details(ev(kind="reset")) == {"resume": {"position": 0, "total": 0}}
    assert details(ev(kind="watched", playcount=2))["playcount"] == 2
    assert details(ev(kind="unwatched"))["playcount"] == 0


def test_is_applied_compares_timestamps():
    entry = {"Filme/A.mkv": applied_entry(ev(ts="2026-11-10T12:00:00.000Z"))}
    assert is_applied(ev(ts="2026-11-10T12:00:00.000Z"), entry)
    assert is_applied(ev(ts="2026-11-10T11:00:00.000Z"), entry)
    assert not is_applied(ev(ts="2026-11-10T12:00:00.001Z"), entry)
    assert not is_applied(ev(media="other"), entry)


# ---- applier -----------------------------------------------------------------------------


class FakeKodi:
    """JSON-RPC endpoint with a small library."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.movies = [{"movieid": 1, "file": "nfs://nas/Media/Filme/A.mkv"}]
        self.episodes = [{"episodeid": 7, "file": "R:\\Serien\\S01E01.mkv"}]
        self.musicvideos: list[dict] = []
        self.fail: set[str] = set()
        self.details = {"VideoLibrary.GetMovieDetails": {"moviedetails": {"file": MOVIE_PATH}}}
        self.file_playcounts: dict[str, Any] = {}  # path -> playcount shown by Files.GetFileDetails

    def __call__(self, method: str, params: dict) -> dict:
        self.calls.append((method, params))
        if method in self.fail:
            raise RpcError(f"{method} failed")
        if method == "VideoLibrary.GetMovies":
            return {"movies": self.movies}
        if method == "VideoLibrary.GetEpisodes":
            return {"episodes": self.episodes}
        if method == "VideoLibrary.GetMusicVideos":
            return {"musicvideos": self.musicvideos}
        if method == "Files.GetFileDetails":
            if params["file"] not in self.file_playcounts:
                raise RpcError("unknown file")
            return {"filedetails": {"playcount": self.file_playcounts[params["file"]]}}
        return self.details.get(method, {})

    def sets(self) -> list[tuple[str, dict]]:
        return [c for c in self.calls if ".Set" in c[0]]


MOVIE_PATH = "nfs://nas/Media/Filme/A.mkv"


@pytest.fixture
def kodi() -> FakeKodi:
    return FakeKodi()


@pytest.fixture
def clock() -> list[float]:
    return [1000.0]


def make_applier(kodi: FakeKodi, clock: list[float]) -> tuple[KodiApplier, list[str]]:
    logs: list[str] = []
    applier = KodiApplier(kodi, MAPS, lambda: clock[0], lambda msg, level=1: logs.append(msg))
    return applier, logs


def test_library_item_gets_playcount_and_resume(kodi, clock):
    applier, _ = make_applier(kodi, clock)
    applied: dict = {}
    event = ev(position_s=600.0, duration_s=7000.0, playcount=0)
    assert applier.apply([event], applied) == 1
    assert kodi.sets() == [
        (
            "VideoLibrary.SetMovieDetails",
            {"movieid": 1, "playcount": 0, "resume": {"position": 600.0, "total": 7000.0}},
        )
    ]
    assert applied["Filme/A.mkv"]["ts"] == event.ts and applied["Filme/A.mkv"]["ok"]


def test_item_is_found_without_mappings_when_another_device_uses_another_path(kodi, clock):
    """The tablet plays over SMB with other letter case; this device has the library over NFS."""
    kodi.movies = [
        {"movieid": 5, "file": "nfs://192.168.0.5/RAID5/SERIES/Show/Season 01/Pilot.S01E01.mkv"}
    ]
    kodi.movies.append(
        {"movieid": 6, "file": "nfs://192.168.0.5/RAID5/SERIES/Show/Season 01/Other.mkv"}
    )
    applier = KodiApplier(kodi, [], lambda: clock[0], lambda msg, level=1: None)
    foreign = "smb://nas/RAID5/series/show/season 01/pilot.s01e01.mkv"
    applied: dict = {}
    assert applier.apply([ev(media=foreign, position_s=60.0)], applied) == 1
    assert kodi.sets()[0][1]["movieid"] == 5
    stranger = ev(media="smb://nas/RAID5/SERIES/Other Show/Season 01/Pilot.S01E01.mkv")
    kodi.calls.clear()
    assert applier.apply([stranger], applied) == 0 and kodi.sets() == []


def test_only_entries_newer_than_the_applied_state_are_written(kodi, clock):
    applier, _ = make_applier(kodi, clock)
    applied = {"Filme/A.mkv": applied_entry(ev(ts="2026-11-10T12:00:00.000Z"))}
    old = ev(ts="2026-11-10T11:59:59.000Z", position_s=1.0)
    same = ev(ts="2026-11-10T12:00:00.000Z", position_s=2.0)
    assert applier.apply([old, same], applied) == 0
    assert kodi.calls == []  # not even a library query
    newer = ev(ts="2026-11-10T12:00:01.000Z", position_s=3.0)
    assert applier.apply([old, same, newer], applied) == 1
    assert len(kodi.sets()) == 1
    assert applier.apply([newer], applied) == 0  # now it is applied


def test_episodes_and_reset_events(kodi, clock):
    applier, _ = make_applier(kodi, clock)
    applied: dict = {}
    event = ev(media="Serien/S01E01.mkv", kind="reset")
    assert applier.apply([event], applied) == 1
    assert kodi.sets() == [
        ("VideoLibrary.SetEpisodeDetails", {"episodeid": 7, "resume": {"position": 0, "total": 0}})
    ]
    assert not applier.echo  # a reset sets no playcount, so there is nothing to echo


def test_echo_suppression_window(kodi, clock):
    applier, _ = make_applier(kodi, clock)
    applier.apply([ev(kind="watched", playcount=1)], {})
    assert applier.is_echo("movie", 1, 1)
    assert not applier.is_echo("movie", 1, 0)  # a different value is a real change
    assert not applier.is_echo("movie", 2, 1)
    assert not applier.is_echo("episode", 1, 1)
    clock[0] += ECHO_WINDOW_S + 1
    assert not applier.is_echo("movie", 1, 1)


def test_files_outside_the_library_use_set_file_details_for_mapped_paths(kodi, clock):
    applier, _ = make_applier(kodi, clock)
    applied: dict = {}
    mapped = ev(media="Other/B.mkv", position_s=10.0)
    assert applier.apply([mapped], applied) == 1
    assert [c[1]["file"] for c in kodi.sets()] == ["nfs://nas/Media/Other/B.mkv"]
    assert applied["Other/B.mkv"]["ok"]


def test_foreign_path_is_translated_with_what_this_device_played_itself(kodi, clock):
    """Another device played the file over SMB; here the same share is mounted over NFS."""
    applier = KodiApplier(kodi, [], lambda: clock[0], lambda msg, level=1: None)
    played_here = "nfs://10.0.0.5/RAID5/Show/S01/E01.mkv"
    first = ev(media="smb://nas/RAID5/Show/S01/E01.mkv", unmapped=True, position_s=30.0)
    second = ev(
        media="smb://nas/RAID5/Show/S01/E02.mkv", unmapped=True, kind="watched", playcount=1
    )
    applied: dict = {}
    assert applier.apply([first, second], applied, own_paths=[played_here]) == 2
    files = [c[1]["file"] for c in kodi.sets()]
    assert files == [played_here, "nfs://10.0.0.5/RAID5/Show/S01/E02.mkv"]
    assert applied[first.media_id]["via"] == "file"


def test_foreign_path_on_the_same_location_is_used_as_it_is(kodi, clock):
    applier = KodiApplier(kodi, [], lambda: clock[0], lambda msg, level=1: None)
    foreign = ev(media="nfs://10.0.0.5/RAID5/Other/S01/E05.mkv", unmapped=True)
    own = ["nfs://10.0.0.5/RAID5/Show/S01/E01.mkv"]
    assert applier.apply([foreign], {}, own_paths=own) == 1
    assert kodi.sets()[0][1]["file"] == foreign.media_id


def test_foreign_path_is_not_written_to_kodi_and_leaves_the_seek_to_the_player(kodi, clock):
    """A path another device used says nothing about the path here: no fake success, so the
    fallback seek at the start of the playback still runs."""
    applier, _ = make_applier(kodi, clock)
    applied: dict = {}
    foreign = ev(media="smb://x/y/C.mkv", unmapped=True, kind="watched", playcount=1)
    assert applier.apply([foreign], applied) == 0
    assert kodi.sets() == []
    assert not applied["smb://x/y/C.mkv"]["ok"]


def test_set_file_details_failure_is_logged_once_and_not_retried(kodi, clock):
    applier, logs = make_applier(kodi, clock)
    kodi.fail.add("Files.SetFileDetails")
    applied: dict = {}
    events = [ev(media="Other/B.mkv"), ev(media="Other/C.mkv")]
    assert applier.apply(events, applied) == 0
    assert sum("SetFileDetails" in m for m in logs) == 1
    assert not applied["Other/B.mkv"]["ok"]
    kodi.calls.clear()
    assert applier.apply(events, applied) == 0
    assert kodi.calls == []  # tried once, not every sync


def test_mapped_file_without_local_mapping_cannot_be_applied(clock):
    kodi = FakeKodi()
    applier = KodiApplier(kodi, [], lambda: clock[0], lambda *a, **k: None)
    applied: dict = {}
    assert applier.apply([ev(media="Other/B.mkv")], applied) == 0
    assert not applied["Other/B.mkv"]["ok"]
    assert kodi.sets() == []


def test_failed_library_query_is_retried_later(kodi, clock):
    applier, logs = make_applier(kodi, clock)
    kodi.fail.add("VideoLibrary.GetEpisodes")
    applied: dict = {}
    movie, episode = ev(), ev(media="Serien/S01E01.mkv")
    assert applier.apply([movie, episode], applied) == 1
    assert "Filme/A.mkv" in applied and "Serien/S01E01.mkv" not in applied
    assert any("Library query failed" in m for m in logs)


def test_failed_write_is_retried_later(kodi, clock):
    applier, logs = make_applier(kodi, clock)
    kodi.fail.add("VideoLibrary.SetMovieDetails")
    applied: dict = {}
    assert applier.apply([ev()], applied) == 0
    assert applied == {} and any("Could not apply" in m for m in logs)


def test_playing_medium_is_left_alone(kodi, clock):
    applier, _ = make_applier(kodi, clock)
    applied: dict = {}
    assert applier.apply([ev()], applied, playing="Filme/A.mkv") == 0
    assert kodi.calls == [] and applied == {}
    assert applier.apply([ev()], applied, playing=None) == 1


def test_untrackable_library_paths_are_skipped(kodi, clock):
    applier, _ = make_applier(kodi, clock)
    kodi.movies = [{"movieid": 5, "file": "stack://a , b"}, {"movieid": 1, "file": MOVIE_PATH}]
    assert applier.apply([ev()], {}) == 1


# ---- manual changes ----------------------------------------------------------------------


class FakeEngine:
    def __init__(self, known: Event | None = None) -> None:
        self.known = known
        self.recorded: list[tuple] = []

    def lookup(self, media_id: str, fp: str | None) -> Event | None:
        return self.known

    def record(self, *args: Any, **kwargs: Any) -> None:
        self.recorded.append((args, kwargs))


def make_manual(kodi, clock, known=None):
    applier, logs = make_applier(kodi, clock)
    engine = FakeEngine(known)
    return (
        ManualChanges(kodi, MAPS, engine, applier, lambda msg, level=1: logs.append(msg)),
        engine,
        applier,
    )


def update(**payload: Any) -> str:
    return json.dumps({"item": {"id": 1, "type": "movie"}, **payload})


def test_manual_watched_mark_becomes_an_event(kodi, clock):
    manual, engine, _ = make_manual(kodi, clock)
    assert manual.handle(update(playcount=1))
    args, kwargs = engine.recorded[0]
    assert args == ("Filme/A.mkv", "watched", 0.0, None, 1)
    assert kwargs == {"fp": None, "unmapped": False}


def test_manual_unwatched_mark_keeps_known_fingerprint_and_duration(kodi, clock):
    known = ev(kind="watched", playcount=1, duration_s=7000.0, fp="s=1;h=x;")
    manual, engine, _ = make_manual(kodi, clock, known)
    assert manual.handle(update(playcount=0))
    args, kwargs = engine.recorded[0]
    assert args == ("Filme/A.mkv", "unwatched", 0.0, 7000.0, 0)
    assert kwargs["fp"] == "s=1;h=x;"


def test_manual_change_of_an_unmapped_file(kodi, clock):
    kodi.details["VideoLibrary.GetMovieDetails"] = {"moviedetails": {"file": "smb://x/y.mkv"}}
    manual, engine, _ = make_manual(kodi, clock)
    assert manual.handle(update(playcount=1))
    assert engine.recorded[0][1]["unmapped"] is True


def manual_with_logs(kodi, clock):
    applier, _ = make_applier(kodi, clock)
    logs: list[str] = []
    engine = FakeEngine()
    manual = ManualChanges(kodi, MAPS, engine, applier, lambda msg, level=1: logs.append(msg))
    return manual, engine, logs


def test_manual_mark_of_a_file_outside_the_library_uses_the_file_of_the_notification(kodi, clock):
    manual, engine, logs = manual_with_logs(kodi, clock)
    file = "nfs://nas/Media/Show/S01/E01.mkv"
    assert manual.handle(json.dumps({"item": {"type": "unknown", "file": file}, "playcount": 1}))
    assert manual.handle(json.dumps({"item": {"file": file}, "playcount": 0}))
    kinds = [(args[0], args[1]) for args, _ in engine.recorded]
    assert kinds == [("Show/S01/E01.mkv", "watched"), ("Show/S01/E01.mkv", "unwatched")]
    assert any("Manual change" in m for m in logs)


def test_ignored_notifications_are_logged_for_debugging(kodi, clock):
    manual, _, logs = manual_with_logs(kodi, clock)
    assert not manual.handle(json.dumps({"item": {"type": "unknown"}, "playcount": 1}))
    assert not manual.handle(json.dumps({"item": "x", "playcount": 1}))
    assert sum("Ignored notification" in m for m in logs) == 2


# ---- files outside the library: noticing manual marks by looking at the playcount ----------

FILE = "nfs://nas/Media/Show/S01/E01.mkv"
FOLDER = "nfs://nas/Media/Show/S01/"


def poller_with(kodi, clock, own_paths=(FILE,), known=None, folder=FOLDER):
    """A poller whose shown folder has already settled (the first look at a folder is skipped)."""
    applier, _ = make_applier(kodi, clock)
    engine = FakeEngine(known)
    manual = ManualChanges(kodi, MAPS, engine, applier, lambda *a, **k: None)
    poller = FilePoller(kodi, MAPS, manual, applier)
    poller.poll([], folder)
    return poller, engine, applier, list(own_paths)


def test_a_change_of_the_playcount_since_the_last_look_becomes_an_event(kodi, clock):
    poller, engine, _, own = poller_with(kodi, clock)
    kodi.file_playcounts[FILE] = 0
    assert poller.poll(own, FOLDER) == 0 and engine.recorded == []  # only a baseline
    assert poller.poll(own, FOLDER) == 0
    kodi.file_playcounts[FILE] = 1  # marked as watched in the file list
    assert poller.poll(own, FOLDER) == 1
    kodi.file_playcounts[FILE] = 0  # and unmarked again
    assert poller.poll(own, FOLDER) == 1
    assert [(a[0], a[1], a[4]) for a, _ in engine.recorded] == [
        ("Show/S01/E01.mkv", "watched", 1),
        ("Show/S01/E01.mkv", "unwatched", 0),
    ]


def looks(kodi) -> list[str]:
    return [c[1]["file"] for c in kodi.calls if c[0] == "Files.GetFileDetails"]


def test_only_files_of_the_folder_that_is_shown_are_looked_at(kodi, clock):
    poller, _, _, own = poller_with(kodi, clock, folder="")
    kodi.file_playcounts[FILE] = 0
    assert poller.poll(own, "") == 0  # no folder shown (or not a file view): no look at all
    poller.poll(own, "nfs://nas/Media/Other/")
    assert poller.poll(own, "nfs://nas/Media/Other/") == 0  # settled, but another folder
    assert looks(kodi) == []
    poller.poll(own, FOLDER)  # the folder changed: skipped, the user is moving around
    assert looks(kodi) == []
    assert poller.poll(own, FOLDER.upper().rstrip("/")) == 0  # case and trailing slash: same
    assert looks(kodi) == [FILE]


def test_the_look_has_a_time_budget(kodi, clock):
    poller, _, applier, _ = poller_with(kodi, clock)
    files = [f"{FOLDER}{n}.mkv" for n in range(5)]
    for path in files:
        kodi.file_playcounts[path] = 0
    seen: list[str] = []
    real = kodi.__call__

    def slow(method, params):
        seen.append(params["file"])
        clock[0] += 1.5  # every look takes 1.5 s
        return real(method, params)

    poller.rpc = slow
    poller.poll(files, FOLDER)
    assert len(seen) == 2  # budget of POLL_BUDGET_S: the rest waits for the next sync


def test_what_the_add_on_wrote_itself_is_no_manual_change(kodi, clock):
    other = "nfs://nas/Media/Other/"
    poller, engine, applier, _ = poller_with(kodi, clock, own_paths=(), folder=other)
    applier.apply([ev(media="Other/B.mkv", kind="watched", playcount=1)], {})
    written = "nfs://nas/Media/Other/B.mkv"
    assert applier.file_playcounts[written] == 1
    kodi.file_playcounts[written] = 1  # Kodi shows what we wrote
    assert poller.poll([], other) == 0 and engine.recorded == []


def test_a_change_that_matches_the_nas_state_creates_no_event(kodi, clock):
    known = ev(media="Show/S01/E01.mkv", kind="watched", playcount=1)
    poller, engine, _, own = poller_with(kodi, clock, known=known)
    kodi.file_playcounts[FILE] = 0
    poller.poll(own, FOLDER)
    kodi.file_playcounts[FILE] = 1
    assert poller.poll(own, FOLDER) == 0 and engine.recorded == []


def test_unknown_or_odd_answers_are_skipped(kodi, clock):
    poller, engine, _, own = poller_with(kodi, clock)
    assert poller.poll(own, FOLDER) == 0  # Files.GetFileDetails fails for the file
    kodi.file_playcounts[FILE] = True
    assert poller.poll(own, FOLDER) == 0
    kodi.file_playcounts[FILE] = None
    assert poller.poll(own, FOLDER) == 0 and engine.recorded == []


def test_a_reset_gives_no_playcount_baseline(kodi, clock):
    applier, _ = make_applier(kodi, clock)
    applier._remember_file("nfs://x/a.mkv", ev(media="x/a.mkv", kind="reset"))
    assert "nfs://x/a.mkv" in applier.file_media and applier.file_playcounts == {}


def test_the_applier_remembers_only_the_newest_files(kodi, clock):
    applier, _ = make_applier(kodi, clock)
    for number in range(MAX_KNOWN_FILES + 5):
        applier._remember_file(f"nfs://x/{number}.mkv", ev(media=f"x/{number}.mkv"))
    applier._remember_file("nfs://x/304.mkv", ev(media="x/304.mkv"))  # same file again
    assert len(applier.file_media) == MAX_KNOWN_FILES
    assert next(reversed(applier.file_media)) == "nfs://x/304.mkv"
    assert "nfs://x/0.mkv" not in applier.file_media


def test_same_playcount_as_the_state_creates_no_event(kodi, clock):
    manual, engine, _ = make_manual(kodi, clock, ev(kind="watched", playcount=1))
    assert not manual.handle(update(playcount=1))
    assert engine.recorded == []


def test_echo_of_our_own_write_is_ignored(kodi, clock):
    manual, engine, applier = make_manual(kodi, clock)
    applier.apply([ev(kind="watched", playcount=1)], {})
    kodi.calls.clear()
    assert not manual.handle(update(playcount=1))
    assert engine.recorded == [] and kodi.calls == []  # not even the details are fetched
    # the user un-marks it right after: that is a real change
    assert manual.handle(update(playcount=0))
    clock[0] += ECHO_WINDOW_S + 1
    applier.apply([ev(kind="watched", playcount=1, ts="2026-11-10T13:00:00.000Z")], {})
    clock[0] += ECHO_WINDOW_S + 1
    assert manual.handle(update(playcount=1))  # window over: counts as a manual change


@pytest.mark.parametrize(
    "data",
    [
        "no json",
        "[]",
        json.dumps({"playcount": 1}),
        json.dumps({"item": "x", "playcount": 1}),
        update(),
        update(playcount=True),
        update(playcount="1"),
        json.dumps({"item": {"id": 1, "type": "tvshow"}, "playcount": 1}),
        json.dumps({"item": {"id": "1", "type": "movie"}, "playcount": 1}),
        json.dumps({"item": {"type": "movie"}, "playcount": 1}),
    ],
)
def test_irrelevant_notifications_are_ignored(kodi, clock, data):
    manual, engine, _ = make_manual(kodi, clock)
    assert not manual.handle(data)
    assert engine.recorded == []


def test_unresolvable_file_is_ignored(kodi, clock):
    manual, engine, _ = make_manual(kodi, clock)
    kodi.fail.add("VideoLibrary.GetMovieDetails")
    assert not manual.handle(update(playcount=1))
    kodi.fail.clear()
    kodi.details["VideoLibrary.GetMovieDetails"] = {"moviedetails": {"file": "stack://a , b"}}
    assert not manual.handle(update(playcount=1))
    assert engine.recorded == []


# ---- tracker (with the real engine on the fake NAS) --------------------------------------


class Env:
    def __init__(self, rig: Rig, name: str = "b") -> None:
        self.rig = rig
        self.engine = rig.device(name)
        self.engine.sync()
        self.logs: list[str] = []
        self.fingerprints = Fingerprinter(lambda path: (_ for _ in ()).throw(OSError("no")))
        self.tracker = Tracker(
            self.engine,
            MAPS,
            self.fingerprints,
            0.9,
            lambda msg, level=1: self.logs.append(msg),
        )

    def events(self) -> list[Event]:
        from watchsync.events import decode_event

        return [
            decode_event(self.rig.vfs.get(f"nfs://nas/WatchSync/events/{name}"))
            for name in sorted(self.rig.vfs.listdir("nfs://nas/WatchSync/events/")[1])
            if name.startswith(self.engine.device)
        ]


@pytest.fixture
def env() -> Env:
    return Env(Rig())


PATH = "nfs://nas/Media/Filme/A.mkv"


def test_playback_of_an_unmapped_file_remembers_its_path(env):
    env.tracker.started("smb://other/Share/Show/S01/E01.mkv", 0.0, 100.0)
    env.tracker.started("smb://other/Share/Show/S01/E01.mkv", 0.0, 100.0)
    assert env.engine.local.own_paths == ["smb://other/Share/Show/S01/E01.mkv"]
    env.tracker.started(PATH, 0.0, 100.0)  # mapped: nothing to learn from
    assert env.engine.local.own_paths == ["smb://other/Share/Show/S01/E01.mkv"]


def test_untrackable_playback_is_ignored(env):
    env.tracker.started("plugin://x/", 0.0, 100.0)
    assert env.tracker.current is None
    env.tracker.tick(5.0, 100.0)
    env.tracker.paused(5.0)
    env.tracker.stopped()
    env.tracker.ended()
    assert env.events() == []


def test_progress_pause_and_stop_events(env):
    env.tracker.started(PATH, 0.0, 7000.0)
    env.tracker.tick(100.0, 7000.0)
    env.rig.clock.advance(5)
    env.tracker.tick(150.0, 7000.0)  # within a minute: only saved locally
    env.rig.clock.advance(5)
    env.tracker.paused(160.0)
    env.rig.clock.advance(5)
    env.tracker.stopped()
    assert [(e.type, e.position_s) for e in env.events()] == [
        ("progress", 100.0),
        ("progress", 160.0),
        ("progress", 160.0),
    ]
    assert env.events()[0].duration_s == 7000.0
    assert env.tracker.current is None


def test_a_quick_look_at_a_file_without_a_saved_position_writes_nothing(env):
    env.tracker.started(PATH, 0.0, 7000.0)
    env.tracker.tick(0.5, 7000.0)
    env.tracker.paused(0.5)
    env.tracker.stopped()
    assert env.events() == []


def test_stopping_right_at_the_start_clears_the_saved_position(env):
    """Saved position, Kodi resumes there, and a stop within a second of the start."""
    put_foreign_progress(env)
    env.tracker.started(PATH, 600.0, 7000.0)
    env.tracker.tick(600.5, 7000.0)
    env.rig.clock.advance(0.5)
    env.tracker.stopped()
    last = env.events()[-1]
    assert (last.type, last.position_s) == ("reset", 0.0)
    state = env.engine.lookup("Filme/A.mkv", None)
    assert state is not None and state.type == "reset"


def test_stopping_before_the_stream_is_up_still_clears_the_saved_position(env):
    """onPlayBackStarted comes first; the user stops before onAVStarted ever arrives."""
    put_foreign_progress(env)
    env.tracker.started(PATH, 0.0, 0.0)
    env.tracker.stopped()
    assert env.events()[-1].type == "reset"


def test_the_late_stream_start_only_adds_position_and_duration(env):
    put_foreign_progress(env)
    env.tracker.started(PATH, 0.0, 0.0)
    first = env.tracker.current
    assert first is not None and first.had_saved
    env.rig.clock.advance(3)
    env.tracker.started(PATH, 600.0, 7000.0)  # onAVStarted: Kodi resumed at the saved position
    assert env.tracker.current is first
    assert (first.position, first.duration) == (600.0, 7000.0)
    assert first.started_at < env.engine.mono()  # the stop-quickly clock keeps its first start
    env.tracker.stopped()  # 3 s after the first start: not quick any more, so progress, no reset
    assert env.events()[-1].type == "progress"


def test_even_a_short_saved_position_is_cleared_by_a_quick_stop(env):
    put_foreign_progress(env, position_s=1.5)
    env.tracker.started(PATH, 0.0, 7000.0)
    env.tracker.stopped()
    assert env.events()[-1].type == "reset"


def test_stopping_at_the_start_of_a_playback_from_the_beginning_clears_it_too(env):
    put_foreign_progress(env)  # the user answered Kodi's resume question with "from beginning"
    env.tracker.started(PATH, 0.0, 7000.0)
    env.rig.clock.advance(5)
    env.tracker.tick(0.5, 7000.0)
    env.tracker.stopped()
    assert env.events()[-1].type == "reset"


def test_stopping_later_keeps_the_progress(env):
    put_foreign_progress(env)
    env.tracker.started(PATH, 0.0, 7000.0)
    env.tracker.tick(20.0, 7000.0)
    env.rig.clock.advance(20)
    env.tracker.stopped()
    assert {(e.type, e.position_s) for e in env.events()} == {("progress", 20.0)}


def test_progress_is_recorded_from_five_seconds_on(env):
    env.tracker.started(PATH, 0.0, 7000.0)
    env.tracker.paused(5.0)
    assert [e.position_s for e in env.events()] == [5.0]


def test_threshold_marks_watched_once_and_counts_up(env):
    env.engine.record("Filme/A.mkv", "watched", 0.0, 7000.0, 2)
    env.tracker.started(PATH, 0.0, 7000.0)
    env.tracker.tick(6400.0, 7000.0)  # 91 %
    env.tracker.tick(6500.0, 7000.0)  # nothing more
    env.tracker.paused(6500.0)
    env.tracker.stopped()
    last = env.events()[-1]
    assert (last.type, last.playcount, last.position_s) == ("watched", 3, 0.0)
    assert len(env.events()) == 2


def test_stop_after_threshold_without_tick_is_watched(env):
    env.tracker.started(PATH, 0.0, 100.0)
    env.tracker.tick(50.0, 100.0)
    env.tracker.current.position = 95.0
    env.tracker.stopped()
    assert env.events()[-1].type == "watched"


def test_end_of_playback_is_watched(env):
    env.tracker.started(PATH, 0.0, 100.0)
    env.tracker.ended()
    assert env.events()[-1].type == "watched" and env.events()[-1].playcount == 1
    env.tracker.ended()
    assert len(env.events()) == 1


def test_unknown_duration_gives_no_ratio_and_no_duration_in_the_event(env):
    env.tracker.started(PATH, 0.0, 0.0)
    env.tracker.tick(500.0, 0.0)
    assert env.events()[0].duration_s is None and env.events()[0].type == "progress"


def test_unmapped_path_is_reported(env):
    env.tracker.started("smb://other/x.mkv", 0.0, 100.0)
    assert any("No path mapping" in m for m in env.logs)
    assert env.tracker.current.unmapped


def put_foreign_progress(env: Env, device: str = "a", **kw: Any) -> None:
    other = env.rig.device(device)
    other.sync()
    record(other, **{"position_s": 600.0, **kw})
    env.engine.sync()


def test_state_of_another_device_is_found_by_file_name_and_folders(env):
    """No fingerprint match and another path: the last folders and the file name still match."""
    other = env.rig.device("a")
    other.sync()
    record(other, media="smb://nas/RAID5/filme/A.mkv", position_s=500.0)
    env.engine.sync()
    mine = "nfs://elsewhere/RAID5/Filme/A.mkv"
    found = env.engine.lookup(mine, None)
    assert found is not None and found.position_s == 500.0
    assert env.engine.lookup(mine, "s=1;h=unknown;") == found
    assert env.engine.lookup("nfs://elsewhere/RAID5/Other/A.mkv", None) is None


def test_the_add_on_never_jumps_or_asks_kodi_resumes_by_itself(env):
    """The saved position is in Kodi's database; where to continue is Kodi's question."""
    put_foreign_progress(env)
    before = list(env.engine.local.queue)
    env.tracker.started(PATH, 0.0, 7000.0)
    assert env.tracker.current is not None and env.tracker.current.position == 0.0
    assert all("Resuming" not in m for m in env.logs)
    assert env.engine.local.queue == before


# ---- PlayerMonitor adapter ---------------------------------------------------------------


class RecordingTracker:
    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.logs: list[str] = []
        self.fail = False

    def log(self, msg: str, level: int = 1) -> None:
        self.logs.append(msg)

    def _call(self, *call: Any) -> None:
        if self.fail:
            raise RuntimeError("boom")
        self.calls.append(call)

    def started(self, *a: Any) -> None:
        self._call("started", *a)

    def paused(self, *a: Any) -> None:
        self._call("paused", *a)

    def stopped(self) -> None:
        self._call("stopped")

    def ended(self) -> None:
        self._call("ended")

    def tick(self, *a: Any) -> None:
        self._call("tick", *a)


@pytest.fixture
def player() -> PlayerMonitor:
    monitor = PlayerMonitor()
    monitor.getPlayingFile = lambda: PATH  # type: ignore[method-assign]
    monitor.getTime = lambda: 12.5  # type: ignore[method-assign]
    monitor.getTotalTime = lambda: 100.0  # type: ignore[method-assign]
    monitor.isPlayingVideo = lambda: True  # type: ignore[method-assign]
    return monitor


def test_player_callbacks_reach_the_tracker(player):
    tracker = RecordingTracker()
    player.tracker = tracker  # type: ignore[assignment]
    player.onAVStarted()
    player.onPlayBackPaused()
    player.onPlayBackStopped()
    player.onPlayBackEnded()
    player.poll()
    player.isPlayingVideo = lambda: False  # type: ignore[method-assign]
    player.poll()
    assert tracker.calls == [
        ("started", PATH, 12.5, 100.0),
        ("paused", 12.5),
        ("stopped",),
        ("ended",),
        ("tick", 12.5, 100.0),
    ]


def test_player_does_nothing_without_a_tracker(player):
    player.onAVStarted()
    player.poll()  # no exception


def test_player_callback_errors_are_logged_not_raised(player):
    tracker = RecordingTracker()
    tracker.fail = True
    player.tracker = tracker  # type: ignore[assignment]
    player.onPlayBackStopped()
    assert tracker.logs and "onPlayBackStopped failed" in tracker.logs[0]


def test_current_dataclass_defaults():
    assert not Current("m", False, None, 1.0, 0.0, 0).watched

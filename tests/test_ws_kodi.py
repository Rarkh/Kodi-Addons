"""WatchSync: applying the NAS state to Kodi, echo suppression, manual changes, tracking."""

from __future__ import annotations

import json
from typing import Any

import pytest
from test_ws_engine import Rig, record
from watchsync.applier import (
    ECHO_WINDOW_S,
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
        return self.details.get(method, {})

    def sets(self) -> list[tuple[str, dict]]:
        return [c for c in self.calls if ".Set" in c[0] or c[0].startswith("Files.")]


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


def test_foreign_unmapped_path_is_not_written_to_kodi_and_leaves_the_seek_to_the_player(
    kodi, clock
):
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
    def __init__(self, rig: Rig, name: str = "b", mode: int = 0, answer: bool = True) -> None:
        self.rig = rig
        self.engine = rig.device(name)
        self.engine.sync()
        self.seeks: list[float] = []
        self.asked: list[float] = []
        self.logs: list[str] = []
        self.answer = answer
        self.fingerprints = Fingerprinter(lambda path: (_ for _ in ()).throw(OSError("no")))
        self.tracker = Tracker(
            self.engine,
            MAPS,
            self.fingerprints,
            0.9,
            mode,
            self.seeks.append,
            lambda pos: self.asked.append(pos) or self.answer,
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


def test_stop_near_the_beginning_resets_the_resume_point(env):
    env.tracker.started(PATH, 0.0, 7000.0)
    env.tracker.tick(2.0, 7000.0)
    env.tracker.stopped()
    assert env.events()[-1].type == "reset"


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


def test_newer_position_of_another_device_is_a_fallback_seek(env):
    put_foreign_progress(env)
    env.tracker.started(PATH, 0.0, 7000.0)
    assert env.seeks == [600.0] and env.tracker.current.position == 600.0


def test_ask_mode_respects_the_answer():
    no = Env(Rig(), mode=1, answer=False)
    put_foreign_progress(no)
    no.tracker.started(PATH, 0.0, 7000.0)
    assert no.asked == [600.0] and no.seeks == []
    yes = Env(Rig(), mode=1, answer=True)
    put_foreign_progress(yes)
    yes.tracker.started(PATH, 0.0, 7000.0)
    assert yes.seeks == [600.0]


def test_no_seek_when_kodi_already_has_the_position(env):
    put_foreign_progress(env)
    state = env.engine.state["Filme/A.mkv"]
    env.engine.local.applied["Filme/A.mkv"] = applied_entry(state, ok=True)
    env.tracker.started(PATH, 0.0, 7000.0)
    assert env.seeks == []
    # applied but not successfully (file outside the library): still seek
    env.engine.local.applied["Filme/A.mkv"] = applied_entry(state, ok=False)
    env.tracker.started(PATH, 0.0, 7000.0)
    assert env.seeks == [600.0]


def test_no_seek_for_close_positions_own_events_or_other_types(env):
    put_foreign_progress(env, position_s=602.0)
    env.tracker.started(PATH, 600.0, 7000.0)
    assert env.seeks == []
    own = Env(Rig(), name="b")
    own.engine.record("Filme/A.mkv", "progress", 500.0, 7000.0, 0)
    own.tracker.started(PATH, 0.0, 7000.0)
    assert own.seeks == []
    watched = Env(Rig())
    put_foreign_progress(watched, kind="watched", position_s=0.0, playcount=1)
    watched.tracker.started(PATH, 0.0, 7000.0)
    assert watched.seeks == []


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

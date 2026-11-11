"""WatchSync: the sync engine with several simulated devices on one fake NAS."""

from __future__ import annotations

import json
from typing import Any

import pytest
from watchsync import engine as engine_mod
from watchsync.backend import Backend, BackendError
from watchsync.engine import SyncEngine
from watchsync.events import Event, encode_event
from watchsync.store import StateStore
from ws_fakes import Clock, FakeVfs, make_backend, make_local

NAS = "nfs://nas/WatchSync/"


class Rig:
    """One shared NAS and clock; devices are created on demand."""

    def __init__(self) -> None:
        self.vfs = FakeVfs()
        self.clock = Clock()
        self.vfs.default_mtime = self.clock.now_value.timestamp()
        self.logs: list[tuple[int, str]] = []

    def device(self, name: str, cache: bool = True) -> SyncEngine:
        local = make_local(self.vfs, f"special://profile/{name}/")
        return SyncEngine(
            make_backend(self.vfs),
            StateStore(local, cache),
            name,
            self.clock,
            self.clock.mono,
            lambda msg, level=1: self.logs.append((level, msg)),
        )

    def warnings(self) -> list[str]:
        return [m for level, m in self.logs if level == 2]


@pytest.fixture
def rig() -> Rig:
    return Rig()


def record(engine: SyncEngine, media: str = "Filme/A.mkv", kind: str = "progress", **kw: Any):
    args = {"position_s": 100.0, "duration_s": 7000.0, "playcount": 0}
    args.update(kw)
    return engine.record(
        media, kind, args.pop("position_s"), args.pop("duration_s"), args.pop("playcount"), **args
    )


def test_record_is_uploaded_as_numbered_event_file(rig):
    a = rig.device("a")
    a.sync()
    event = record(a)
    assert event is not None and event.seq == 1
    assert rig.vfs.get(NAS + "events/a_000001.json") == encode_event(event)
    assert a.local.queue == []
    info = json.loads(rig.vfs.get(NAS + "devices/a.json"))
    assert info["last_seq"] == 1
    second = record(a, kind="watched", position_s=0.0, playcount=1, force=True)
    assert second is not None and second.seq == 2


def test_other_device_receives_the_state(rig):
    a, b = rig.device("a"), rig.device("b")
    a.sync()
    record(a, fp="s=1;h=x;")
    changed = b.sync()
    assert [(e.media_id, e.device, e.position_s) for e in changed] == [("Filme/A.mkv", "a", 100.0)]
    assert b.lookup("Filme/A.mkv", None).device == "a"
    assert b.lookup("other/name.mkv", "s=1;h=x;").media_id == "Filme/A.mkv"
    assert b.lookup("other/name.mkv", None) is None
    assert b.sync() == []  # nothing new the second time


def test_newest_event_wins_across_devices_including_rewind(rig):
    a, b = rig.device("a"), rig.device("b")
    a.sync()
    b.sync()
    record(a, position_s=3000.0)
    rig.clock.advance(120)
    b.sync()
    record(b, position_s=50.0)  # deliberate rewind, newer
    a.sync()
    assert a.lookup("Filme/A.mkv", None).position_s == 50.0
    assert b.lookup("Filme/A.mkv", None).position_s == 50.0


def test_parallel_playback_resolved_by_timestamp(rig):
    a, b = rig.device("a"), rig.device("b")
    a.sync()
    b.sync()
    record(a, position_s=10.0)
    rig.clock.advance(1)
    record(b, position_s=20.0)
    a.sync()
    b.sync()
    assert a.lookup("Filme/A.mkv", None).position_s == 20.0
    assert b.lookup("Filme/A.mkv", None).device == "b"


def test_offline_events_are_queued_and_sent_in_order(rig):
    a, b = rig.device("a"), rig.device("b")
    a.sync()
    rig.vfs.down = True
    a.sync()
    assert not a.online
    record(a, position_s=1.0)
    record(a, media="Filme/B.mkv", position_s=2.0)
    assert len(a.local.queue) == 2
    rig.vfs.down = False
    a.sync()
    assert a.local.queue == [] and a.online
    b.sync()
    assert b.lookup("Filme/B.mkv", None).position_s == 2.0


def test_upload_failure_keeps_event_in_queue(rig):
    a = rig.device("a")
    a.sync()
    rig.vfs.fail_rename = True
    record(a)
    assert len(a.local.queue) == 1 and not a.online
    assert any("Upload failed" in w for w in rig.warnings())
    rig.vfs.fail_rename = False
    a.sync()
    assert a.local.queue == []


def test_queue_survives_a_restart(rig):
    a = rig.device("a")
    a.sync()
    rig.vfs.down = True
    record(a, position_s=7.0)
    again = rig.device("a")
    assert len(again.local.queue) == 1
    assert again.state["Filme/A.mkv"].position_s == 7.0
    rig.vfs.down = False
    again.sync()
    assert again.local.queue == []
    assert NAS + "events/a_000001.json" in rig.vfs.files


def test_processed_marker_only_advances_without_gaps(rig):
    a = rig.device("a")
    a.sync()
    a.local.next_seq = 5  # e.g. continued after a lost cache
    record(a)
    assert a.local.processed.get("a", 0) == 0


def test_corrupt_cached_state_entries_are_dropped(rig):
    a = rig.device("a")
    a.sync()
    record(a)
    a.local.state["junk"] = {"v": 1}
    a.save()
    assert set(rig.device("a").state) == {"Filme/A.mkv"}


def test_progress_is_throttled_to_one_write_per_minute_per_medium(rig):
    a = rig.device("a")
    a.sync()
    assert record(a, force=False) is not None  # first one goes out
    rig.clock.advance(10)
    assert record(a, force=False, position_s=110.0) is None
    assert a.local.pending["Filme/A.mkv"]["position_s"] == 110.0
    assert len(a.local.queue) == 0
    assert record(a, media="Filme/Other.mkv", force=False) is not None  # other medium
    rig.clock.advance(60)
    assert record(a, force=False, position_s=130.0) is not None
    assert "Filme/A.mkv" not in a.local.pending
    rig.clock.advance(1)
    assert record(a, position_s=140.0, force=True) is not None  # pause/stop always writes


def test_local_only_progress_is_sent_after_a_restart(rig):
    a = rig.device("a")
    a.sync()
    record(a, force=False)
    rig.clock.advance(5)
    record(a, force=False, position_s=222.0)
    assert len(rig.vfs.listdir(NAS + "events/")[1]) == 1
    restarted = rig.device("a")
    restarted.sync()
    assert restarted.local.pending == {}
    event = event_from_nas(rig, "a_000002.json")
    assert event.position_s == 222.0 and event.ts.endswith(":05.000Z")


def event_from_nas(rig: Rig, name: str) -> Event:
    from watchsync.events import decode_event

    return decode_event(rig.vfs.get(NAS + "events/" + name))


# ---- reading problems --------------------------------------------------------------------


def test_tmp_files_and_foreign_files_are_ignored(rig):
    a, b = rig.device("a"), rig.device("b")
    a.sync()
    rig.vfs.put(NAS + "events/a_000009.tmp", "{half")
    rig.vfs.put(NAS + "events/readme.txt", "hello")
    record(a)
    b.sync()
    assert list(b.state) == ["Filme/A.mkv"]
    assert b.local.defective == []


def test_unreadable_file_is_retried_five_times_then_marked_defective(rig):
    b = rig.device("b")
    b.sync()
    rig.vfs.put(NAS + "events/x_000001.json", '{"v": 1, "dev')
    good = Event("x", 2, "2026-11-10T12:00:00.000Z", "progress", "m", position_s=5.0)
    rig.vfs.put(NAS + "events/x_000002.json", encode_event(good))
    for attempt in range(4):
        b.sync()
        assert "m" not in b.state  # later events wait behind the broken one
        assert b.local.attempts["x_000001.json"] == attempt + 1
    b.sync()
    assert b.local.defective == ["x_000001.json"]
    assert b.state["m"].position_s == 5.0
    assert any("defective" in w for w in rig.warnings())
    b.sync()
    assert b.local.defective == ["x_000001.json"]


def test_file_whose_content_does_not_match_its_name_counts_as_broken(rig):
    b = rig.device("b")
    b.sync()
    wrong = Event("other", 1, "2026-11-10T12:00:00.000Z", "progress", "m")
    rig.vfs.put(NAS + "events/x_000001.json", encode_event(wrong))
    b.sync()
    assert b.local.attempts == {"x_000001.json": 1}


def test_event_with_newer_format_version_is_skipped(rig):
    b = rig.device("b")
    b.sync()
    data = Event("x", 1, "2026-11-10T12:00:00.000Z", "progress", "m").to_dict()
    data["v"] = 2
    rig.vfs.put(NAS + "events/x_000001.json", json.dumps(data))
    b.sync()
    assert b.state == {} and b.local.processed["x"] == 1
    assert any("skipped" in w for w in rig.warnings())


def test_event_from_the_future_is_ignored(rig):
    b = rig.device("b")
    b.sync()
    far = Event("x", 1, "2026-11-20T12:00:00.000Z", "progress", "m")
    rig.vfs.put(NAS + "events/x_000001.json", encode_event(far))
    b.sync()
    assert b.state == {}
    assert any("future" in w for w in rig.warnings())


def test_unreachable_nas_at_start_is_survived(rig):
    rig.vfs.down = True
    a = rig.device("a")
    assert a.sync() == []
    assert any("not reachable" in w for w in rig.warnings())


# ---- snapshots and compaction ------------------------------------------------------------


def test_compaction_writes_snapshot_deletes_own_old_events_and_others_catch_up(rig, monkeypatch):
    monkeypatch.setattr(engine_mod, "COMPACT_THRESHOLD", 3)
    a, early, late = rig.device("a"), rig.device("early"), rig.device("late")
    a.sync()
    for index in range(4):
        rig.clock.advance(1)
        record(a, media=f"m{index % 2}", position_s=float(index))
    early.sync()  # reads the events before they are compacted
    a.sync()  # compaction
    assert rig.vfs.listdir(NAS + "events/")[1] == []
    assert NAS + "snapshots/a.json" in rig.vfs.files
    early.sync()  # snapshot adds nothing new
    late.sync()  # only has the snapshot
    for device in (early, late):
        assert device.state["m0"].position_s == 2.0
        assert device.state["m1"].position_s == 3.0
    # more events after the snapshot are merged on top
    rig.clock.advance(1)
    record(a, media="m0", position_s=99.0)
    late.sync()
    assert late.state["m0"].position_s == 99.0
    assert a.local.next_seq == 6


def test_second_compaction_merges_with_the_old_snapshot(rig, monkeypatch):
    monkeypatch.setattr(engine_mod, "COMPACT_THRESHOLD", 2)
    a, b = rig.device("a"), rig.device("b")
    a.sync()
    record(a, media="one")
    rig.clock.advance(1)
    record(a, media="two")
    a.sync()
    rig.clock.advance(1)
    record(a, media="three")
    rig.clock.advance(1)
    record(a, media="one", position_s=500.0)
    a.sync()
    b.sync()
    assert set(b.state) == {"one", "two", "three"}
    assert b.state["one"].position_s == 500.0


def test_compaction_waits_for_the_queue_and_survives_errors(rig, monkeypatch):
    monkeypatch.setattr(engine_mod, "COMPACT_THRESHOLD", 1)
    a = rig.device("a")
    a.sync()
    record(a)
    a._own_files = [(1, "a_000001.json")]
    a.local.queue.append(Event("a", 9, "2026-11-10T12:00:00.000Z", "reset", "x").to_dict())
    a._compact()  # queue not empty: nothing happens
    assert NAS + "snapshots/a.json" not in rig.vfs.files
    a.local.queue.clear()
    rig.vfs.put(NAS + "events/a_000001.json", "garbage")
    a._compact()
    assert any("Compaction skipped" in w for w in rig.warnings())
    assert NAS + "events/a_000001.json" in rig.vfs.files
    rig.vfs.put(
        NAS + "events/a_000001.json",
        encode_event(Event("a", 1, "2026-11-10T12:00:00.000Z", "reset", "x")),
    )
    rig.vfs.fail_delete = True
    a._compact()
    assert any("Could not delete" in w for w in rig.warnings())


def test_unreadable_snapshot_is_skipped_and_other_files_ignored(rig):
    b = rig.device("b")
    b.sync()
    rig.vfs.put(NAS + "snapshots/x.json", "not json")
    rig.vfs.put(NAS + "snapshots/notes.txt", "ignore me")
    b.sync()
    assert b.state == {}


# ---- recovery ----------------------------------------------------------------------------


def test_sequence_numbers_are_never_reused_after_losing_local_state(rig, monkeypatch):
    monkeypatch.setattr(engine_mod, "COMPACT_THRESHOLD", 1000)
    a = rig.device("a")
    a.sync()
    for _ in range(3):
        rig.clock.advance(1)
        record(a)
    fresh = rig.device("a", cache=False)
    fresh.sync()
    assert fresh.local.next_seq == 4


def test_sequence_recovery_uses_device_file_and_snapshot(rig):
    rig.vfs.dirs.update({NAS, NAS + "events/", NAS + "devices/", NAS + "snapshots/"})
    rig.vfs.put(NAS + "devices/a.json", json.dumps({"last_seq": 40}))
    a = rig.device("a", cache=False)
    a.sync()
    assert a.local.next_seq == 41
    rig.vfs.put(NAS + "devices/a.json", "broken")
    snap = {"v": 1, "device": "a", "upto_seq": 77, "ts": "2026-11-10T12:00:00.000Z", "items": []}
    rig.vfs.put(NAS + "snapshots/a.json", json.dumps(snap))
    b = rig.device("a", cache=False)
    b.sync()
    assert b.local.next_seq == 78
    rig.vfs.put(NAS + "snapshots/a.json", "broken")
    rig.vfs.put(NAS + "devices/a.json", json.dumps({"nothing": 1}))
    c = rig.device("a", cache=False)
    c.sync()
    assert c.local.next_seq == 1


def test_clock_check_warns_when_nas_time_is_far_off(rig):
    rig.vfs.default_mtime = rig.clock.now_value.timestamp() + 3600
    a = rig.device("a")
    a.sync()
    assert a.clock_offset is not None and a.clock_offset > 3000
    assert any("Clock differs" in w for w in rig.warnings())


def test_clock_check_ignores_a_nonsense_file_time(rig):
    """Android NFS has reported a file time of 3.6e18: not a clock problem."""
    rig.vfs.default_mtime = 3.6e18
    a = rig.device("a")
    a.sync()
    assert a.clock_offset is None
    assert not any("Clock differs" in w for w in rig.warnings())


def test_clock_check_without_stat_support(rig):
    a = rig.device("a")
    a.backend.ensure_layout()
    rig.vfs.fail_write = True  # device file cannot be written, so there is nothing to stat
    assert a.check_clock() is None
    assert any("Device file not written" in w for w in rig.warnings())


def test_rebuild_reads_everything_again_but_keeps_the_queue(rig):
    a, b = rig.device("a"), rig.device("b")
    a.sync()
    record(a, position_s=5.0)
    b.sync()
    rig.vfs.down = True
    record(b, media="Filme/B.mkv", position_s=9.0)
    rig.vfs.down = False
    b.local.applied["x"] = {"ts": "2026-01-01T00:00:00.000Z"}
    changed = b.rebuild()
    assert b.local.applied.get("x") is None
    assert {e.media_id for e in changed} >= {"Filme/A.mkv"}
    assert b.local.queue == [] and "Filme/B.mkv" in b.state


def test_flush_sends_everything_before_shutdown(rig):
    a = rig.device("a")
    a.sync()
    record(a, force=False)
    rig.clock.advance(1)
    record(a, force=False, position_s=3.0)
    rig.vfs.down = True
    a.flush()
    assert len(a.local.queue) == 1
    rig.vfs.down = False
    a.flush()
    assert a.local.queue == []


def test_status_summary(rig):
    a = rig.device("a")
    a.sync()
    record(a, media="/full/path.mkv", unmapped=True)
    status = a.status()
    assert status["device"] == "a" and status["media"] == 1 and status["unmapped"] == 1
    assert status["online"] and status["queued"] == 0 and status["defective"] == []
    assert status["last_sync"].endswith("Z")


class BrokenLocal(Backend):
    def read_text(self, folder: str, name: str) -> str:
        raise BackendError("no file")

    def write_text(self, folder: str, name: str, text: str, overwrite: bool = False) -> None:
        raise BackendError("disk full")


def test_failing_local_cache_is_only_logged(rig):
    logs: list[str] = []
    engine = SyncEngine(
        make_backend(rig.vfs),
        StateStore(BrokenLocal(), True),
        "a",
        rig.clock,
        rig.clock.mono,
        lambda msg, level=1: logs.append(msg),
    )
    engine.sync()
    assert any("Local cache not written" in m for m in logs)


def test_own_paths_are_remembered_once_and_capped(rig):
    a = rig.device("a")
    a.remember_path("nfs://x/a.mkv")
    a.remember_path("nfs://x/a.mkv")
    assert a.local.own_paths == ["nfs://x/a.mkv"]
    for number in range(305):
        a.remember_path(f"nfs://x/{number}.mkv")
    assert len(a.local.own_paths) == 300 and a.local.own_paths[-1] == "nfs://x/304.mkv"

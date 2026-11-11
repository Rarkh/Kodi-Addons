"""WatchSync: storage backend, local state, logger, config."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest
from watchsync import logger as logmod
from watchsync.backend import Backend, BackendError, VfsBackend, optional_text
from watchsync.config import device_id, load_config, sanitize_device
from watchsync.store import LocalState, StateStore
from ws_fakes import FakeVfs, make_backend, make_local


@pytest.fixture
def vfs() -> FakeVfs:
    return FakeVfs()


def test_ensure_layout_creates_folders(vfs):
    backend = make_backend(vfs)
    backend.ensure_layout()
    for folder in ("", "events/", "devices/", "snapshots/"):
        assert f"nfs://nas/WatchSync/{folder}" in vfs.dirs
    backend.ensure_layout()  # nothing to create the second time


def test_ensure_layout_fails_when_nas_is_down(vfs):
    vfs.down = True
    with pytest.raises(BackendError):
        make_backend(vfs).ensure_layout()


def test_root_without_trailing_slash_is_normalised(vfs):
    backend = VfsBackend("nfs://nas/W", vfs=vfs)
    backend.ensure_layout()
    assert "nfs://nas/W/events/" in vfs.dirs


def test_list_files_needs_a_reachable_folder(vfs):
    backend = make_backend(vfs)
    with pytest.raises(BackendError):
        backend.list_files("events")
    backend.ensure_layout()
    backend.write_text("events", "a_000001.json", "{}")
    assert backend.list_files("events") == ["a_000001.json"]


def test_write_goes_through_tmp_and_leaves_no_tmp_behind(vfs):
    backend = make_backend(vfs)
    backend.ensure_layout()
    vfs.put("nfs://nas/WatchSync/events/a_000001.tmp", "stale")
    backend.write_text("events", "a_000001.json", "hällo")
    assert backend.read_text("events", "a_000001.json") == "hällo"
    assert backend.list_files("events") == ["a_000001.json"]


def test_events_are_never_overwritten(vfs):
    backend = make_backend(vfs)
    backend.ensure_layout()
    backend.write_text("events", "a_000001.json", "one")
    backend.write_text("events", "a_000001.json", "one")  # identical retry is fine
    with pytest.raises(BackendError):
        backend.write_text("events", "a_000001.json", "two")
    assert backend.read_text("events", "a_000001.json") == "one"
    assert backend.list_files("events") == ["a_000001.json"]


def test_overwrite_replaces_the_file(vfs):
    backend = make_backend(vfs)
    backend.ensure_layout()
    backend.write_text("devices", "a.json", "one", overwrite=True)
    backend.write_text("devices", "a.json", "two", overwrite=True)
    assert backend.read_text("devices", "a.json") == "two"


def test_write_failures(vfs, monkeypatch):
    backend = make_backend(vfs)
    backend.ensure_layout()
    vfs.fail_write = True
    with pytest.raises(BackendError):
        backend.write_text("events", "a_000001.json", "x")
    vfs.fail_write = False
    vfs.fail_rename = True
    with pytest.raises(BackendError):
        backend.write_text("events", "a_000001.json", "x")
    vfs.fail_rename = False
    monkeypatch.setattr(backend, "_same_content", lambda path, data: False)
    with pytest.raises(BackendError):
        backend.write_text("events", "a_000001.json", "x")
    assert backend.list_files("events") == []


def test_read_errors(vfs):
    backend = make_backend(vfs)
    backend.ensure_layout()
    with pytest.raises(BackendError):
        backend.read_text("events", "missing.json")
    vfs.files["nfs://nas/WatchSync/events/bad.json"] = b"\xff\xfe"
    with pytest.raises(BackendError):
        backend.read_text("events", "bad.json")
    assert optional_text(backend, "events", "missing.json") is None


def test_delete_and_mtime(vfs):
    backend = make_backend(vfs)
    backend.ensure_layout()
    backend.write_text("events", "a_000001.json", "x")
    vfs.mtimes["nfs://nas/WatchSync/events/a_000001.json"] = 123.0
    assert backend.mtime("events", "a_000001.json") == 123.0
    backend.delete("events", "a_000001.json")
    backend.delete("events", "a_000001.json")  # already gone: fine
    with pytest.raises(BackendError):
        backend.mtime("events", "a_000001.json")
    backend.write_text("events", "b_000001.json", "x")
    vfs.fail_delete = True
    with pytest.raises(BackendError):
        backend.delete("events", "b_000001.json")


def test_probe(vfs, monkeypatch):
    backend = make_backend(vfs)
    backend.ensure_layout()
    backend.probe("dev")
    assert backend.list_files("") == []
    monkeypatch.setattr(backend, "read_text", lambda folder, name: "other")
    with pytest.raises(BackendError):
        backend.probe("dev")


def test_abstract_backend_has_no_implementation():
    base = Backend()
    for call in (
        base.ensure_layout,
        lambda: base.list_files("x"),
        lambda: base.read_text("x", "y"),
        lambda: base.write_text("x", "y", "z"),
        lambda: base.delete("x", "y"),
        lambda: base.mtime("x", "y"),
    ):
        with pytest.raises(NotImplementedError):
            call()


# ---- local state -------------------------------------------------------------------------


def test_state_store_round_trip_and_disabled_cache(vfs):
    local = make_local(vfs)
    store = StateStore(local, True)
    assert store.load() == LocalState()
    state = LocalState(next_seq=7, processed={"a": 3}, queue=[{"x": 1}])
    store.save(state)
    assert store.load() == state
    off = StateStore(local, False)
    off.save(LocalState(next_seq=99))
    assert off.load() == LocalState()
    assert store.load().next_seq == 7


@pytest.mark.parametrize("content", ["not json", "[1, 2]", '{"next_seq": "x", "queue": 5}'])
def test_corrupt_state_file_gives_a_fresh_state(vfs, content):
    local = make_local(vfs)
    vfs.put("special://profile/a/state.json", content)
    loaded = StateStore(local, True).load()
    assert loaded.processed == {}


def test_unknown_keys_in_state_file_are_ignored(vfs):
    local = make_local(vfs)
    vfs.put("special://profile/a/state.json", json.dumps({"next_seq": 4, "future": 1}))
    assert StateStore(local, True).load().next_seq == 4


# ---- logger ------------------------------------------------------------------------------


def test_logger_levels_ring_buffer_and_flush(vfs):
    sink: list[tuple[str, int]] = []
    local = make_local(vfs)
    now = lambda: datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)  # noqa: E731
    log = logmod.Logger(lambda m, level: sink.append((m, level)), False, local, now, max_lines=2)
    log("hidden", logmod.DEBUG)
    assert sink == []
    log.flush()  # nothing written yet
    assert optional_text(local, "", logmod.LOG_FILE) is None
    log("one")
    log.debug = True
    log("two", logmod.DEBUG)
    log("three", logmod.WARNING)
    assert sink == [("[WatchSync] one", 1), ("[WatchSync] two", 1), ("[WatchSync] three", 2)]
    log.flush()
    text = optional_text(local, "", logmod.LOG_FILE)
    assert text is not None
    assert text.splitlines() == [
        "2026-01-01 12:00:00 DEBUG two",
        "2026-01-01 12:00:00 WARN three",
    ]
    log.flush()  # not dirty: no second write


def test_logger_survives_a_failing_log_file(vfs):
    local = make_local(vfs)
    log = logmod.Logger(lambda m, level: None, False, local, lambda: datetime.now(timezone.utc))
    log("x")
    vfs.fail_write = True
    log.flush()  # must not raise


# ---- config ------------------------------------------------------------------------------


class FakeAddon:
    def __init__(self, **values):
        self.values = {
            "enabled": True,
            "storage_path": " nfs://nas/W ",
            "device_id": "",
            "path_mappings": "R:\\",
            "sync_interval": 1,
            "save_interval": 20,
            "watched_threshold": 150,
            "resume_mode": 1,
            "cache_enabled": True,
            "log_level": 1,
        }
        self.values.update(values)

    def getSettingBool(self, key):  # noqa: N802
        return self.values[key]

    getSettingInt = getSettingString = getSettingBool  # noqa: N815

    def setSettingString(self, key, value):  # noqa: N802
        self.values[key] = value


def test_sanitize_device():
    assert sanitize_device("Kodi (Wohn_Zimmer)!") == "kodi-wohn-zimmer"
    assert sanitize_device("***") == ""


def test_device_id_is_derived_once_and_stored():
    addon = FakeAddon()
    assert device_id(addon, "LibreELEC Büro") == "libreelec-b-ro"
    assert addon.values["device_id"] == "libreelec-b-ro"
    assert device_id(addon, "something else") == "libreelec-b-ro"
    assert device_id(FakeAddon(), "***") == "kodi"


def test_load_config_clamps_values():
    config = load_config(FakeAddon(), "Kodi")
    assert config.storage_path == "nfs://nas/W"
    assert config.sync_interval == 5 and config.save_interval == 20
    assert config.threshold == 1.0
    assert config.debug and config.resume_mode == 1
    assert config.mappings[0].raw == "R:\\"
    low = load_config(FakeAddon(watched_threshold=0, log_level=0), "Kodi")
    assert low.threshold == 0.01 and not low.debug

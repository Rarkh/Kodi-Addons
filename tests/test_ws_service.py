"""WatchSync: the service loop, the menu and the entry points."""

from __future__ import annotations

import json
import runpy
from pathlib import Path
from typing import Any

import pytest
import xbmc
from test_ws_backend import FakeAddon
from test_ws_engine import NAS, Rig, record
from test_ws_kodi import MOVIE_PATH, FakeKodi
from watchsync import menu, runtime
from watchsync.backend import LAYOUT, VfsBackend
from watchsync.config import Config
from watchsync.engine import SyncEngine
from watchsync.player import Current
from watchsync.runtime import Env, Service, ServiceMonitor
from ws_fakes import make_local

ADDONS = Path(__file__).resolve().parent.parent / "addons"


class ScriptedMonitor(ServiceMonitor):
    """Runs the service loop for a fixed number of steps."""

    def __init__(self, steps: int = 0) -> None:
        super().__init__()
        self.steps = steps
        self.aborted = False

    def abortRequested(self) -> bool:  # noqa: N802
        return self.aborted

    def waitForAbort(self, timeout: float = 0) -> bool:  # noqa: N802
        self.steps -= 1
        return self.steps < 0


class Setup:
    def __init__(self, name: str = "b", **settings: Any) -> None:
        self.rig = Rig()
        self.kodi = FakeKodi()
        self.folder = ""  # the folder shown in the file view
        self.menu_open = False  # a context menu is open
        self.selected: tuple[str, bool] = ("", False)  # the focused item and whether a folder
        self.sink: list[tuple[str, int]] = []
        values: dict[str, Any] = {
            "storage_path": NAS,
            "device_id": name,
            "path_mappings": "nfs://nas/Media/",
            "sync_interval": 30,
            "save_interval": 10,
            "watched_threshold": 90,
            **settings,
        }
        self.addon = FakeAddon(**values)
        self.local = make_local(self.rig.vfs, "special://profile/svc/")
        env = Env(
            addon=self.addon,
            local=self.local,
            sink=lambda msg, level: self.sink.append((msg, level)),
            now=self.rig.clock,
            mono=self.rig.clock.mono,
            rpc=self.kodi,
            friendly_name=lambda: "Kodi Büro",
            folder=lambda: self.folder,
            menu_open=lambda: self.menu_open,
            selected=lambda: self.selected,
            opener=lambda path: (_ for _ in ()).throw(OSError("no")),
            make_backend=lambda path: VfsBackend(path, LAYOUT, self.rig.vfs),
        )
        self.monitor = ScriptedMonitor()
        self.service = Service(env, self.monitor, runtime.PlayerMonitor())


def engine_of(service: Service) -> SyncEngine:
    assert service.engine
    return service.engine


def config_of(service: Service) -> Config:
    assert service.config
    return service.config


def foreign_event(setup: Setup, **kw: Any) -> None:
    other = setup.rig.device("a")
    other.sync()
    record(other, **{"position_s": 600.0, **kw})


def test_disabled_service_does_nothing():
    setup = Setup(enabled=False)
    setup.service.build()
    assert setup.service.engine is None
    assert setup.service.player.tracker is None
    setup.service.sync()  # no engine: no error
    setup.monitor.updates.append("{}")
    setup.service.step()
    assert setup.monitor.updates == []


def test_missing_storage_location_is_reported():
    setup = Setup(storage_path="")
    setup.service.build()
    assert setup.service.engine is None
    assert any("No storage location" in msg for msg, _ in setup.sink)


def test_status_file_failure_does_not_stop_the_sync():
    setup = Setup()
    setup.service.build()
    setup.rig.vfs.fail_write = True
    setup.service.sync()  # neither NAS nor local writes work: no exception


def test_sync_applies_nas_state_to_kodi_and_writes_status():
    setup = Setup()
    foreign_event(setup, duration_s=7000.0)
    setup.service.build()
    setup.service.sync()
    assert [c[0] for c in setup.kodi.sets()] == ["VideoLibrary.SetMovieDetails"]
    status = json.loads(setup.rig.vfs.get("special://profile/svc/status.json"))
    assert status["device"] == "b"
    assert status["media"] == 1
    assert status["storage_path"] == NAS
    assert "Service started" not in setup.rig.vfs.get("special://profile/svc/log.txt")
    setup.kodi.calls.clear()
    setup.service.sync()
    assert setup.kodi.sets() == []  # already applied, nothing newer


def test_medium_in_playback_is_deferred_until_it_stopped():
    setup = Setup()
    setup.service.build()
    setup.service.sync()
    tracker = setup.service.player.tracker
    assert tracker is not None
    tracker.current = Current("Filme/A.mkv", False, None, 7000.0, 10.0, 0)
    foreign_event(setup)
    setup.service.sync()
    assert setup.kodi.sets() == []
    tracker.current = None
    setup.service.sync()
    assert len(setup.kodi.sets()) == 1


MARKED = "nfs://nas/Media/Show/S01/"


def listing(setup: Setup, **playcounts: int) -> None:
    setup.kodi.directories[MARKED] = [
        {"file": f"{MARKED}{name}.mkv", "filetype": "file", "playcount": count}
        for name, count in playcounts.items()
    ]


def settled_in_the_folder(setup: Setup) -> None:
    setup.folder = MARKED
    listing(setup, E01=0)
    setup.service.build()
    setup.service.sync()
    for _ in range(4):  # the folder stays shown: the baseline is taken
        setup.service.step()


def test_a_closing_context_menu_makes_the_service_look_at_the_shown_folder_at_once():
    setup = Setup()
    settled_in_the_folder(setup)
    listing(setup, E01=1)  # the user marks E01 as watched in the context menu ...
    setup.menu_open = True
    setup.service.step()  # ... the menu is open: nothing yet
    assert "Show/S01/E01.mkv" not in engine_of(setup.service).state
    setup.menu_open = False
    setup.service.step()  # the menu closed: looked at once
    assert engine_of(setup.service).state["Show/S01/E01.mkv"].type == "watched"


def test_marking_a_folder_in_the_context_menu_sends_the_files_below_it():
    setup = Setup()
    settled_in_the_folder(setup)
    below = "nfs://nas/Media/Show/S01/Season 02/"
    setup.kodi.directories[below] = [
        {"file": below + "E01.mkv", "filetype": "file", "playcount": 1},
        {"file": below + "E02.mkv", "filetype": "file", "playcount": 1},
    ]
    setup.selected = (below, True)  # the folder is focused, then the menu opens ...
    setup.service.step()
    setup.menu_open = True
    setup.selected = ("the menu's own item", False)  # ... and ListItem is the menu's now
    setup.service.step()
    setup.menu_open = False
    setup.service.step()  # closed: the folder that was focused is walked
    state = engine_of(setup.service).state
    assert state["Show/S01/Season 02/E01.mkv"].type == "watched"
    assert state["Show/S01/Season 02/E02.mkv"].type == "watched"
    setup.service.step()  # the second look, a second later (the database may lag)
    setup.kodi.calls.clear()
    for _ in range(3):  # the looks are over; nothing keeps walking the folder
        setup.service.step()
    assert not [c for c in setup.kodi.calls if c[1].get("directory") == below]


def test_a_notification_from_kodi_makes_the_service_look_at_the_shown_folder_at_once():
    setup = Setup()
    settled_in_the_folder(setup)
    listing(setup, E01=1)
    setup.monitor.updates.append("{}")  # Kodi announced a change (the content does not matter)
    setup.service.step()
    assert engine_of(setup.service).state["Show/S01/E01.mkv"].type == "watched"


def test_the_service_does_not_look_at_folders_while_a_video_plays():
    setup = Setup()
    settled_in_the_folder(setup)
    tracker = setup.service.player.tracker
    assert tracker is not None
    tracker.started("nfs://nas/Media/Filme/A.mkv", 0.0, 100.0)
    asked = len(setup.kodi.calls)
    listing(setup, E01=1)
    setup.menu_open = True
    setup.service.step()
    setup.menu_open = False
    setup.service.step()
    assert not [c for c in setup.kodi.calls[asked:] if c[0] == "Files.GetDirectory"]


def test_step_handles_requests_and_intervals():
    setup = Setup()
    service = setup.service
    service.build()
    service.sync()
    polled: list[int] = []
    service.player.poll = lambda: polled.append(1)  # type: ignore[method-assign]
    clock = setup.rig.clock
    service.step()  # first step: next_save reached
    assert polled == [1]
    clock.advance(5)
    service.step()
    assert polled == [1]  # not yet due
    clock.advance(6)
    foreign_event(setup)
    service.step()  # interval of 30 s not over, but 11 s passed: only the poll
    assert polled == [1, 1]
    assert engine_of(service).state == {}
    clock.advance(30)
    service.step()  # interval over: sync
    assert "Filme/A.mkv" in engine_of(service).state
    foreign_event(setup, media="Filme/B.mkv")
    setup.monitor.sync_now = True
    service.step()
    assert "Filme/B.mkv" in engine_of(service).state
    assert not setup.monitor.sync_now
    setup.monitor.rebuild = True
    setup.kodi.calls.clear()
    service.step()
    assert not setup.monitor.rebuild
    assert len(setup.kodi.sets()) == 2  # a rebuild applies everything again


def test_step_creates_events_for_manual_changes():
    setup = Setup()
    service = setup.service
    service.build()
    service.sync()
    setup.monitor.updates.append(json.dumps({"item": {"id": 1, "type": "movie"}, "playcount": 1}))
    service.step()
    assert engine_of(service).state["Filme/A.mkv"].type == "watched"
    assert MOVIE_PATH  # fake library resolves the movie path


def test_settings_change_rebuilds_the_wiring():
    setup = Setup()
    service = setup.service
    service.build()
    first = service.engine
    setup.addon.values["sync_interval"] = 60
    setup.monitor.settings_changed = True
    service.step()
    assert service.engine is not first
    assert config_of(service).sync_interval == 60
    assert not setup.monitor.settings_changed


def test_run_loop_and_shutdown_flush():
    setup = Setup()
    setup.monitor.steps = 2
    setup.service.run()
    assert [m for m, _ in setup.sink][0].endswith("Service started")
    assert [m for m, _ in setup.sink][-1].endswith("Service stopped")
    # abort requested directly: the loop body never runs
    again = Setup()
    again.monitor.aborted = True
    again.service.run()
    # disabled: no engine to flush
    off = Setup(enabled=False)
    off.service.run()


def test_monitor_notifications():
    monitor = ServiceMonitor()
    monitor.onSettingsChanged()
    monitor.onNotification("xbmc", "VideoLibrary.OnUpdate", "{}")
    monitor.onNotification("xbmc", runtime.METHOD_SYNC, "")  # not from us: ignored
    monitor.onNotification(runtime.ADDON_ID, runtime.METHOD_SYNC, "")
    monitor.onNotification(runtime.ADDON_ID, runtime.METHOD_REBUILD, "")
    monitor.onNotification(runtime.ADDON_ID, "Other.Else", "")
    assert monitor.settings_changed
    assert monitor.sync_now
    assert monitor.rebuild
    assert monitor.updates == ["{}"]


def test_run_service_wires_real_objects(monkeypatch):
    captured: dict[str, Any] = {}

    class Capture:
        def __init__(self, env, monitor, player):
            captured["env"] = env

        def run(self):
            captured["ran"] = True

    monkeypatch.setattr(runtime, "Service", Capture)
    runtime.run_service()
    env = captured["env"]
    assert captured["ran"]
    assert env.now().tzinfo is not None
    assert env.mono() >= 0
    assert env.friendly_name() == xbmc.getInfoLabel("System.FriendlyName")
    assert env.make_backend("nfs://x/").root == "nfs://x/"


# ---- menu --------------------------------------------------------------------------------


class MenuDialog:
    calls: list[tuple[str, Any]] = []
    selects: list[int] = []
    inputs: list[str] = []
    browses: list[str] = []
    yesno_answer = True

    def select(self, heading, items):
        self.calls.append(("select", list(items)))
        return self.selects.pop(0)

    def input(self, heading, default=""):
        self.calls.append(("input", default))
        return self.inputs.pop(0)

    def browseSingle(self, kind, heading, shares):  # noqa: N802
        return self.browses.pop(0)

    def yesno(self, heading, text):
        return MenuDialog.yesno_answer

    def ok(self, heading, text):
        self.calls.append(("ok", text))

    def textviewer(self, heading, text):
        self.calls.append(("text", text))

    def notification(self, heading, text):
        self.calls.append(("note", text))


class MenuAddon(FakeAddon):
    FORMATS = {
        30054: "S30054:{0}",
        30076: "S30076:{0}:{1}:{2}",
        30077: "S30077:{0}",
        30081: "S30081:{0}",
    }

    def getAddonInfo(self, key):  # noqa: N802
        return {"version": "9.9.9", "id": "service.watchsync"}[key]

    def getLocalizedString(self, string_id):  # noqa: N802
        return self.FORMATS.get(string_id, f"S{string_id}")


@pytest.fixture
def ui(monkeypatch):
    rig = Rig()
    for name, value in (
        ("calls", []),
        ("selects", []),
        ("inputs", []),
        ("browses", []),
        ("yesno_answer", True),
    ):
        monkeypatch.setattr(MenuDialog, name, value)
    built: list[str] = []
    addon = MenuAddon(storage_path=NAS, device_id="b", path_mappings="")
    monkeypatch.setattr(menu, "ADDON", addon)
    monkeypatch.setattr(menu.xbmcgui, "Dialog", MenuDialog)
    monkeypatch.setattr(menu.xbmc, "executebuiltin", lambda cmd, wait=False: built.append(cmd))
    monkeypatch.setattr(
        menu, "VfsBackend", lambda root, folders=LAYOUT: VfsBackend(root, folders, rig.vfs)
    )
    make_local(rig.vfs, runtime.PROFILE)
    return SimpleUi(rig, addon, built, monkeypatch)


class SimpleUi:
    def __init__(self, rig, addon, built, monkeypatch):
        self.rig, self.addon, self.built = rig, addon, built
        self.monkeypatch = monkeypatch

    def script(self, **values):
        """What the dialogs answer next (undone with the test)."""
        for name, value in values.items():
            self.monkeypatch.setattr(MenuDialog, name, value)

    @property
    def calls(self):
        return MenuDialog.calls


def test_menu_lists_actions_and_runs_the_chosen_one(ui):
    ui.script(selects=[0])
    menu.run(["default.py", "1", ""])
    assert ui.calls[0][1][0] == "S30041"
    assert ui.built == [runtime.NOTIFY_SYNC]
    MenuDialog.calls.clear()
    ui.script(selects=[-1])
    menu.run(["default.py"])
    assert ui.built == [runtime.NOTIFY_SYNC]


def test_script_action_runs_directly(ui):
    menu.run(["default.py", "sync"])
    assert ui.built == [runtime.NOTIFY_SYNC]
    assert ui.calls[-1][0] == "note"


@pytest.mark.parametrize(
    ("offer", "expected"),
    [
        (("10.0.0", None), "S30076:9.9.9:10.0.0:S30079"),
        (("9.9.9", None), "S30076:9.9.9:9.9.9:S30078"),
        ((None, "no network"), "S30076:9.9.9:-:S30081:no network"),
        ((None, None), "S30076:9.9.9:-:S30081:-"),
    ],
)
def test_check_updates_compares_versions_and_refreshes_the_repositories(
    ui, monkeypatch, offer, expected
):
    asked: list[str] = []
    monkeypatch.setattr(menu, "newest_offer", lambda addon_id: asked.append(addon_id) or offer)
    menu.run(["x", "update"])
    assert asked == ["service.watchsync"]
    assert ui.built == [menu.REFRESH_REPOS]
    assert ui.calls[-1] == ("ok", expected)


def test_diagnose_reports_every_write_case(ui):
    ui.rig.vfs.dirs.add(NAS)
    ui.rig.vfs.dirs.add(NAS + "devices/")
    menu.run(["x", "diagnose"])
    kind, text = ui.calls[-1]
    assert kind == "text"
    assert text.startswith("S30077:" + NAS)
    assert "diag.txt (bytes): write=True exists=True size=9" in text
    assert "devices/: exists=True" in text
    assert "rename=True" in text
    assert ui.rig.vfs.files.keys().isdisjoint({NAS + "diag.txt", NAS + "diag-rename.txt"})


def test_diagnose_shows_refused_writes_and_errors(ui):
    ui.rig.vfs.fail_write = True
    menu.run(["x", "diagnose"])
    assert "write=False" in ui.calls[-1][1]
    ui.rig.vfs.fail_write = False

    def boom(path, mode=None):
        raise OSError("denied")

    ui.rig.vfs.File = boom
    menu.run(["x", "diagnose"])
    assert "error: denied" in ui.calls[-1][1]


def test_diagnose_notices_a_file_that_never_shows_up(ui):
    ui.rig.vfs.exists = lambda path: False
    menu.run(["x", "diagnose"])
    assert "exists=False size=-1" in ui.calls[-1][1]


def test_diagnose_needs_a_storage_location(ui):
    ui.addon.values["storage_path"] = ""
    menu.run(["x", "diagnose"])
    assert ui.calls[-1] == ("ok", "S30055")


def test_rebuild_cache_asks_first(ui):
    ui.script(yesno_answer=False)
    menu.run(["x", "rebuild"])
    assert ui.built == []
    ui.script(yesno_answer=True)
    menu.run(["x", "rebuild"])
    assert ui.built == [runtime.NOTIFY_REBUILD]


def test_status_and_log_views(ui):
    menu.run(["x", "status"])
    menu.run(["x", "log"])
    assert [c[0] for c in ui.calls] == ["ok", "ok"]
    local = VfsBackend(runtime.PROFILE, (), ui.rig.vfs)
    local.write_text(
        "",
        "status.json",
        json.dumps(
            {"device": "b", "online": True, "media": 3, "clock_offset_s": 2.4, "defective": ["x"]}
        ),
        True,
    )
    local.write_text("", "log.txt", "line", True)
    MenuDialog.calls.clear()
    menu.run(["x", "status"])
    menu.run(["x", "log"])
    assert "S30072" in ui.calls[0][1]
    assert ui.calls[1] == ("text", "line")
    local.write_text("", "status.json", json.dumps({"online": False}), True)
    MenuDialog.calls.clear()
    menu.run(["x", "status"])
    assert ui.calls[0][0] == "text"


def test_connection_test(ui):
    ui.addon.values["storage_path"] = ""
    menu.run(["x", "test"])
    assert ui.calls[-1] == ("ok", "S30055")
    ui.addon.values["storage_path"] = NAS
    menu.run(["x", "test"])
    assert ui.calls[-1] == ("ok", "S30053")
    ui.rig.vfs.down = True
    menu.run(["x", "test"])
    assert ui.calls[-1][1].startswith("S30054")


def stored(ui) -> str:
    return ui.addon.values["path_mappings"]


def test_mapping_editor_add_edit_delete(ui):
    # add by browsing, with a base
    ui.script(selects=[0, 0, -1])
    ui.script(browses=["nfs://nas/Media/"])
    ui.script(inputs=["Base"])
    menu.run(["x", "mappings"])
    assert stored(ui) == "nfs://nas/Media/=>Base"
    # add by typing, no base
    ui.script(selects=[1, 1, -1])
    ui.script(inputs=["R:\\", ""])
    menu.run(["x", "mappings"])
    assert stored(ui) == "nfs://nas/Media/=>Base|R:\\"
    # edit the first (type again, keep nothing new)
    ui.script(selects=[0, 0, 1, -1])
    ui.script(inputs=["/media/nas", ""])
    menu.run(["x", "mappings"])
    assert stored(ui) == "/media/nas/|R:\\"
    # delete the second
    ui.script(selects=[1, 1, -1])
    menu.run(["x", "mappings"])
    assert stored(ui) == "/media/nas/"


def test_mapping_editor_cancel_paths(ui):
    ui.addon.values["path_mappings"] = "R:\\"
    # cancel the add dialog; empty prefix; blank prefix; cancel the item menu; edit cancelled
    ui.script(selects=[1, -1, -1])
    menu.run(["x", "mappings"])
    ui.script(selects=[1, 1, -1])
    ui.script(inputs=[""])
    menu.run(["x", "mappings"])
    ui.script(selects=[1, 1, -1])
    ui.script(inputs=["   ", ""])
    menu.run(["x", "mappings"])
    ui.script(selects=[0, -1, -1])
    menu.run(["x", "mappings"])
    ui.script(selects=[0, 0, -1, -1])
    menu.run(["x", "mappings"])
    assert stored(ui) == "R:\\"


# ---- entry points ------------------------------------------------------------------------


def test_service_entry_point_runs_the_service(monkeypatch):
    calls: list[int] = []
    monkeypatch.setattr(runtime, "run_service", lambda: calls.append(1))
    runpy.run_path(str(ADDONS / "service.watchsync" / "service.py"), run_name="__main__")
    runpy.run_path(str(ADDONS / "service.watchsync" / "service.py"), run_name="service")
    assert calls == [1]


def test_default_entry_point_opens_the_menu(monkeypatch):
    calls: list[list[str]] = []
    monkeypatch.setattr(menu, "run", lambda argv: calls.append(argv))
    runpy.run_path(str(ADDONS / "service.watchsync" / "default.py"), run_name="__main__")
    runpy.run_path(str(ADDONS / "service.watchsync" / "default.py"), run_name="default")
    assert len(calls) == 1

"""The background service: wires settings, engine, player and Kodi together."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

import xbmc
import xbmcaddon
import xbmcvfs

from .applier import FilePoller, KodiApplier, ManualChanges, Rpc, make_rpc
from .backend import Backend, BackendError, VfsBackend
from .config import Config, load_config
from .engine import SyncEngine
from .logger import WARNING, Logger
from .mediaid import Fingerprinter
from .player import PlayerMonitor, Tracker
from .store import StateStore

ADDON_ID = "service.watchsync"
PROFILE = f"special://profile/addon_data/{ADDON_ID}/"
STATUS_FILE = "status.json"
STEP_S = 1.0
METHOD_SYNC = "Other.SyncNow"
METHOD_REBUILD = "Other.Rebuild"
METHOD_UPDATE = "VideoLibrary.OnUpdate"
LOOKS_AFTER_MARK = 2  # looks at the shown folder after a mark (the database may lag a second)
NOTIFY_SYNC = f"NotifyAll({ADDON_ID},SyncNow)"
NOTIFY_REBUILD = f"NotifyAll({ADDON_ID},Rebuild)"


class ServiceMonitor(xbmc.Monitor):
    """Collects requests from Kodi; the service loop acts on them."""

    def __init__(self) -> None:
        super().__init__()
        self.settings_changed = False
        self.sync_now = False
        self.rebuild = False
        self.updates: list[str] = []

    def onSettingsChanged(self) -> None:
        self.settings_changed = True

    def onNotification(self, sender: str, method: str, data: str) -> None:
        if method == METHOD_UPDATE:
            self.updates.append(data)
        elif sender != ADDON_ID:
            return
        elif method == METHOD_SYNC:
            self.sync_now = True
        elif method == METHOD_REBUILD:
            self.rebuild = True


@dataclass
class Env:
    """Everything the service takes from outside (replaced by fakes in tests)."""

    addon: Any
    local: Backend
    sink: Callable[[str, int], None]
    now: Callable[[], datetime]
    mono: Callable[[], float]
    rpc: Rpc
    friendly_name: Callable[[], str]
    folder: Callable[[], str]
    menu_open: Callable[[], bool]
    selected: Callable[[], tuple[str, bool]]
    opener: Callable[[str], Any]
    make_backend: Callable[[str], Backend]


class Service:
    def __init__(self, env: Env, monitor: ServiceMonitor, player: PlayerMonitor) -> None:
        self.env = env
        self.monitor = monitor
        self.player = player
        self.log = Logger(env.sink, False, env.local, env.now)
        self.config: Config | None = None
        self.engine: SyncEngine | None = None
        self.applier: KodiApplier | None = None
        self.manual: ManualChanges | None = None
        self.poller: FilePoller | None = None
        self._menu_was_open = False
        self._selected: tuple[str, bool] = ("", False)  # the item before a menu opened
        self._marked_folder = ""  # a folder a context menu may just have marked
        self._looks = 0  # looks at the shown folder still to do after a context menu closed
        self.next_sync = 0.0
        self.next_save = 0.0

    def build(self) -> None:
        env = self.env
        config = self.config = load_config(env.addon, env.friendly_name())
        self.log.debug = config.debug
        self.engine = self.applier = self.manual = self.poller = self.player.tracker = None
        if not config.enabled:
            self.log("Synchronisation is switched off in the settings")
            return
        if not config.storage_path:
            self.log("No storage location set in the settings", WARNING)
            return
        engine = SyncEngine(
            env.make_backend(config.storage_path),
            StateStore(env.local, config.cache_enabled),
            config.device,
            env.now,
            env.mono,
            self.log,
        )
        self.engine = engine
        self.applier = KodiApplier(env.rpc, config.mappings, env.mono, self.log)
        self.manual = ManualChanges(env.rpc, config.mappings, engine, self.applier, self.log)
        self.poller = FilePoller(env.rpc, config.mappings, self.manual, self.applier)
        self.player.tracker = Tracker(
            engine,
            config.mappings,
            Fingerprinter(env.opener),
            config.threshold,
            self.log,
        )
        self.next_sync = self.next_save = env.mono()

    def sync(self, rebuild: bool = False) -> None:
        engine, config = self.engine, self.config
        if not engine or not config or not self.applier:
            return
        if rebuild:
            engine.rebuild()
        else:
            engine.sync()
        # The NAS is the truth: bring Kodi's database up to date, also before a playback starts
        # so Kodi's own "resume" works. The medium playing right now waits until it stopped.
        tracker = self.player.tracker
        playing = tracker.current.media_id if tracker and tracker.current else None
        events = engine.unapplied_events()
        self.applier.apply(
            events, engine.local.applied, playing, engine.local.own_paths, list(engine.state)
        )
        engine.applied_done(events)
        if self.poller and not playing:
            self.poller.check(self.env.folder())
        engine.save()
        self._write_status(engine, config)
        self.log.flush()
        self.next_sync = self.env.mono() + config.sync_interval

    def _write_status(self, engine: SyncEngine, config: Config) -> None:
        status = {**engine.status(), "storage_path": config.storage_path}
        try:
            self.env.local.write_text("", STATUS_FILE, json.dumps(status), overwrite=True)
        except BackendError:
            pass  # only the menu reads the status file

    def step(self) -> None:
        monitor = self.monitor
        if monitor.settings_changed:
            monitor.settings_changed = False
            self.build()
            self.sync()
            return
        if not self.engine or not self.config or not self.manual:
            monitor.updates.clear()
            return
        while monitor.updates:
            self.manual.handle(monitor.updates.pop(0))
            self._looks = LOOKS_AFTER_MARK  # Kodi announced a change: look at the shown folder
        now = self.env.mono()
        if monitor.rebuild:
            monitor.rebuild = False
            self.sync(rebuild=True)
        elif monitor.sync_now or now >= self.next_sync:
            monitor.sync_now = False
            self.sync()
        if now >= self.next_save:
            self.player.poll()
            self.next_save = now + self.config.save_interval
        self._watch_marks()

    def _watch_marks(self) -> None:
        """Marks set by hand in a file list. The trigger is the marking itself: Kodi announces a
        change (VideoLibrary.OnUpdate) or a context menu just closed (it may have set a mark).
        The shown folder is then compared with the baseline at once and once more a second later."""
        tracker = self.player.tracker
        if not self.poller or (tracker and tracker.current):
            return
        folder = self.env.folder()
        self.poller.touch(folder)
        open_now = self.env.menu_open()
        if self._menu_was_open and not open_now:
            self._looks = LOOKS_AFTER_MARK
            path, is_folder = self._selected  # what was focused before the menu opened
            self._marked_folder = path if is_folder else ""
        if not open_now:
            self._selected = self.env.selected()  # while the menu is open, ListItem is the menu's
        self._menu_was_open = open_now
        if self._looks:
            self._looks -= 1
            self.poller.check(folder)
            if self._marked_folder:
                self.poller.sync_tree(self._marked_folder)
            if not self._looks:
                self._marked_folder = ""

    def run(self) -> None:
        self.log("Service started")
        self.build()
        self.sync()
        while not self.monitor.abortRequested():
            if self.monitor.waitForAbort(STEP_S):
                break
            self.step()
        if self.engine:
            self.engine.flush()
        self.log("Service stopped")
        self.log.flush()


def run_service() -> None:
    addon = xbmcaddon.Addon()

    env = Env(
        addon=addon,
        local=VfsBackend(PROFILE, folders=()),
        sink=xbmc.log,
        now=lambda: datetime.now(timezone.utc),
        mono=time.monotonic,
        rpc=make_rpc(xbmc.executeJSONRPC),
        friendly_name=lambda: xbmc.getInfoLabel("System.FriendlyName"),
        folder=lambda: xbmc.getInfoLabel("Container.FolderPath"),
        menu_open=lambda: xbmc.getCondVisibility("Window.IsActive(contextmenu)"),
        selected=lambda: (
            xbmc.getInfoLabel("ListItem.FileNameAndPath"),
            xbmc.getCondVisibility("ListItem.IsFolder"),
        ),
        opener=xbmcvfs.File,
        make_backend=VfsBackend,
    )
    Service(env, ServiceMonitor(), PlayerMonitor()).run()

"""The background service: wires settings, engine, player and Kodi together."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

import xbmc
import xbmcaddon
import xbmcgui
import xbmcvfs

from .applier import KodiApplier, ManualChanges, Rpc, make_rpc
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
NOTIFY_SYNC = f"NotifyAll({ADDON_ID},SyncNow)"
NOTIFY_REBUILD = f"NotifyAll({ADDON_ID},Rebuild)"
STRING_RESUME = 30070


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
    opener: Callable[[str], Any]
    ask_resume: Callable[[float], bool]
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
        self.next_sync = 0.0
        self.next_save = 0.0

    def build(self) -> None:
        env = self.env
        config = self.config = load_config(env.addon, env.friendly_name())
        self.log.debug = config.debug
        self.engine = self.applier = self.manual = self.player.tracker = None
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
        self.player.tracker = Tracker(
            engine,
            config.mappings,
            Fingerprinter(env.opener),
            config.threshold,
            config.resume_mode,
            self.player.seekTime,
            env.ask_resume,
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
        self.applier.apply(
            engine.state.values(), engine.local.applied, playing, engine.local.own_paths
        )
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

    def ask_resume(position: float) -> bool:
        text = addon.getLocalizedString(STRING_RESUME).format(
            time.strftime("%H:%M:%S", time.gmtime(position))
        )
        return xbmcgui.Dialog().yesno(addon.getAddonInfo("name"), text)

    env = Env(
        addon=addon,
        local=VfsBackend(PROFILE, folders=()),
        sink=xbmc.log,
        now=lambda: datetime.now(timezone.utc),
        mono=time.monotonic,
        rpc=make_rpc(xbmc.executeJSONRPC),
        friendly_name=lambda: xbmc.getInfoLabel("System.FriendlyName"),
        opener=xbmcvfs.File,
        ask_resume=ask_resume,
        make_backend=VfsBackend,
    )
    Service(env, ServiceMonitor(), PlayerMonitor()).run()

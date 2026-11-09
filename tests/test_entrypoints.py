"""Tests of the Kodi entry points: the watchsync service and the artlibcleanup default.py.

They run the files as __main__, with xbmc.Monitor and the add-on logic replaced by fakes.
"""

from __future__ import annotations

import runpy
from collections.abc import Iterator
from pathlib import Path

import pytest
import xbmc
from resources.lib import main

ADDONS = Path(__file__).resolve().parent.parent / "addons"
SERVICE = ADDONS / "service.watchsync" / "service.py"
DEFAULT = ADDONS / "plugin.program.artlibcleanup" / "default.py"


def _monitor(abort_states: list[bool], wait_results: list[bool]) -> type:
    """A xbmc.Monitor whose answers come from fixed lists (last value repeats)."""
    abort_iter: Iterator[bool] = iter(abort_states)
    wait_iter: Iterator[bool] = iter(wait_results)

    class FakeMonitor:
        def abortRequested(self) -> bool:
            return next(abort_iter, True)

        def waitForAbort(self, timeout: float) -> bool:
            return next(wait_iter, True)

    return FakeMonitor


@pytest.fixture
def logs(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    captured: list[str] = []
    monkeypatch.setattr(xbmc, "log", lambda msg, level=0: captured.append(msg))
    return captured


def test_service_stops_when_kodi_asks_to_wait_abort(monkeypatch, logs):
    # The first wait returns True: Kodi asks the service to stop during the wait
    monkeypatch.setattr(xbmc, "Monitor", _monitor([False, False], [True]))
    ns = runpy.run_path(str(SERVICE), run_name="__main__")
    assert logs[0].endswith("Service started")
    assert logs[-1].endswith("Service stopped")
    assert len(logs) == 2
    assert "PlayerMonitor" in ns


def test_service_stops_when_abort_is_requested_in_loop(monkeypatch, logs):
    # Abort is requested only after one wait: the loop ends by its own condition
    monkeypatch.setattr(xbmc, "Monitor", _monitor([False, True], [False]))
    runpy.run_path(str(SERVICE), run_name="__main__")
    assert logs[-1].endswith("Service stopped")


def test_service_logs_playback_events(monkeypatch, logs):
    monkeypatch.setattr(xbmc, "Monitor", _monitor([True], []))
    ns = runpy.run_path(str(SERVICE), run_name="__main__")
    player = ns["PlayerMonitor"]()
    player.onPlayBackStarted()
    player.onPlayBackStopped()
    player.onPlayBackEnded()
    assert [line.split("] ", 1)[1] for line in logs[-3:]] == [
        "Playback started",
        "Playback stopped",
        "Playback ended",
    ]


def test_service_log_helper_uses_addon_name(logs):
    ns = runpy.run_path(str(SERVICE), run_name="service_module")
    ns["log"]("hello", xbmc.LOGDEBUG)
    assert logs == [f"[{ns['ADDON_NAME']}] hello"]


def test_default_entry_point_starts_the_addon(monkeypatch):
    calls: list[int] = []
    monkeypatch.setattr(main, "run", lambda: calls.append(1))
    runpy.run_path(str(DEFAULT), run_name="__main__")
    assert calls == [1]


def test_default_module_does_not_run_on_import(monkeypatch):
    calls: list[int] = []
    monkeypatch.setattr(main, "run", lambda: calls.append(1))
    runpy.run_path(str(DEFAULT), run_name="default")
    assert calls == []

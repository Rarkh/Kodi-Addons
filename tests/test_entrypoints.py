"""Tests of the artlibcleanup entry point (default.py), run as __main__ with a fake main.run.

The watchsync entry points are tested in test_ws_service.py.
"""

from __future__ import annotations

import runpy
from pathlib import Path

from resources.lib import main

ADDONS = Path(__file__).resolve().parent.parent / "addons"
DEFAULT = ADDONS / "plugin.program.artlibcleanup" / "default.py"


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

"""Tests of the UI layer (main.py) with fake Kodi dialogs and a fake JSON-RPC endpoint."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest
import xbmc
from resources.lib import main
from resources.lib.library import RpcError, make_call
from resources.lib.main import (
    Cancelled,
    Log,
    ProgressDialog,
    delete,
    human_size,
    label_for,
    run,
    scan,
    show_details,
    tr,
)
from resources.lib.orphans import MUSIC, VIDEO, Texture
from test_orphans import FakeKodi, make_fake

DEFAULT_SETTINGS = {
    "dry_run": False,
    "include_music": True,
    "show_details": False,
    "log_to_kodi": True,
}


class FakeAddon:
    def __init__(self, settings: dict[str, bool]) -> None:
        self.settings = dict(settings)
        self.strings: dict[int, str] = {}

    def getAddonInfo(self, key: str) -> str:
        return {"name": "Artlib Cleanup", "id": "plugin.program.artlibcleanup"}[key]

    def getSettingBool(self, key: str) -> bool:
        return self.settings[key]

    def getLocalizedString(self, string_id: int) -> str:
        return self.strings.get(string_id, f"S{string_id}")


class FakeDialog:
    def __init__(self) -> None:
        self.answers: list[bool] = []
        self.oks: list[str] = []
        self.selects: list[list[str]] = []
        self.notes: list[str] = []

    def ok(self, heading: str, message: str) -> None:
        self.oks.append(message)

    def yesno(
        self, heading: str, message: str, nolabel: str = "", yeslabel: str = "", **_: Any
    ) -> bool:
        return self.answers.pop(0)

    def select(self, heading: str, items: list[str]) -> int:
        self.selects.append(items)
        return -1

    def notification(self, heading: str, message: str) -> None:
        self.notes.append(message)


class FakeProgress:
    def __init__(self, cancel_after: int | None) -> None:
        self.cancel_after = cancel_after
        self.checks = 0
        self.updates: list[tuple[int, str]] = []
        self.closed = False

    def create(self, heading: str, title: str) -> None:
        pass

    def iscanceled(self) -> bool:
        self.checks += 1
        return self.cancel_after is not None and self.checks >= self.cancel_after

    def update(self, percent: int, text: str = "") -> None:
        self.updates.append((percent, text))

    def close(self) -> None:
        self.closed = True


def rpc_endpoint(fake: FakeKodi, request: str) -> str:
    """Behave like xbmc.executeJSONRPC: JSON in, JSON out, errors as an error object."""
    req = json.loads(request)
    try:
        result = fake(req["method"], req["params"])
    except RpcError as exc:
        return json.dumps({"id": req["id"], "error": {"message": str(exc)}})
    return json.dumps({"id": req["id"], "result": result})


@pytest.fixture
def ui(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    state = SimpleNamespace(
        addon=FakeAddon(DEFAULT_SETTINGS),
        dialog=FakeDialog(),
        progress=[],
        cancel_after=None,
        logs=[],
        kodi=make_fake(),
    )

    def new_progress() -> FakeProgress:
        progress = FakeProgress(state.cancel_after)
        state.progress.append(progress)
        return progress

    monkeypatch.setattr(main, "ADDON", state.addon)
    monkeypatch.setattr(main.xbmcgui, "Dialog", lambda: state.dialog)
    monkeypatch.setattr(main.xbmcgui, "DialogProgress", new_progress)
    monkeypatch.setattr(xbmc, "log", lambda msg, level=0: state.logs.append((msg, level)))
    monkeypatch.setattr(xbmc, "executeJSONRPC", lambda req: rpc_endpoint(state.kodi, req))
    return state


def test_tr_formats_only_with_arguments(ui: SimpleNamespace):
    ui.addon.strings[30001] = "{} files, {}"
    assert tr(30001) == "{} files, {}"
    assert tr(30001, 3, "1 KB") == "3 files, 1 KB"


def test_human_size_uses_kilobytes_then_megabytes():
    assert human_size(512) == "0.5 KB"
    assert human_size(2 * 1024 * 1024) == "2.0 MB"


def test_log_writes_only_when_enabled(ui: SimpleNamespace):
    Log(False)("hidden")
    assert ui.logs == []
    Log(True)("shown", xbmc.LOGWARNING)
    assert ui.logs == [("[plugin.program.artlibcleanup] shown", xbmc.LOGWARNING)]


def test_progress_dialog_clamps_and_cancels(ui: SimpleNamespace):
    dialog = ProgressDialog("Title")
    dialog("low", -0.5)
    dialog("high", 1.5)
    assert ui.progress[0].updates == [(0, "low"), (100, "high")]
    ui.progress[0].cancel_after = 1
    with pytest.raises(Cancelled):
        dialog("again", 0.5)
    dialog.close()
    assert ui.progress[0].closed


def test_label_for_uses_file_name_and_size(ui: SimpleNamespace):
    texture = Texture(1, "smb://nas/Filme/Gone (2001)/Fanart.jpg", 2048, VIDEO)
    assert label_for(texture) == "[S30032] fanart.jpg (2.0 KB)"
    assert label_for(Texture(2, "", 0, MUSIC)) == "[S30033]  (0.0 KB)"


def test_show_details_lists_largest_first(ui: SimpleNamespace):
    small = Texture(1, "smb://a/small.jpg", 10, VIDEO)
    big = Texture(2, "smb://a/big.jpg", 5000, MUSIC)
    show_details([small, big])
    assert ui.dialog.selects[0][0].endswith("(4.9 KB)")
    assert "big.jpg" in ui.dialog.selects[0][0]


def test_scan_collects_orphans_and_skipped_categories(ui: SimpleNamespace):
    orphans, skipped = scan(make_call(lambda req: rpc_endpoint(ui.kodi, req)), True, Log(False))
    assert {o.texture_id for o in orphans} == {2, 5, 9, 11}
    assert skipped == []


def test_scan_reports_empty_categories(ui: SimpleNamespace):
    ui.kodi = make_fake(**{"AudioLibrary.GetAlbums": []})
    orphans, skipped = scan(make_call(lambda req: rpc_endpoint(ui.kodi, req)), True, Log(False))
    assert skipped == ["S30033"]
    assert {o.texture_id for o in orphans} == {2, 5, 11}


def test_scan_without_music_skips_music_category(ui: SimpleNamespace):
    orphans, skipped = scan(make_call(lambda req: rpc_endpoint(ui.kodi, req)), False, Log(False))
    assert skipped == []
    assert {o.texture_id for o in orphans} == {2, 5, 11}


def test_scan_cancel_closes_progress(ui: SimpleNamespace):
    ui.cancel_after = 1
    with pytest.raises(Cancelled):
        scan(make_call(lambda req: rpc_endpoint(ui.kodi, req)), True, Log(False))
    assert ui.progress[0].closed


def test_delete_removes_every_orphan(ui: SimpleNamespace):
    orphans = [Texture(2, "a", 100, VIDEO), Texture(5, "b", 2048, MUSIC)]
    delete(make_call(lambda req: rpc_endpoint(ui.kodi, req)), orphans, Log(True))
    assert ui.kodi.removed == [2, 5]
    assert ui.dialog.oks == ["S30027"]
    assert ui.progress[0].closed


def test_delete_reports_failures(ui: SimpleNamespace):
    ui.kodi.fail_on = "Textures.RemoveTexture"
    orphans = [Texture(2, "a", 100, VIDEO)]
    delete(make_call(lambda req: rpc_endpoint(ui.kodi, req)), orphans, Log(True))
    assert ui.dialog.oks == ["S30027\nS30034"]
    assert any(level == xbmc.LOGWARNING for _, level in ui.logs)


def test_delete_cancel_keeps_what_was_removed(ui: SimpleNamespace):
    ui.cancel_after = 2
    orphans = [Texture(2, "a", 100, VIDEO), Texture(5, "b", 100, VIDEO)]
    delete(make_call(lambda req: rpc_endpoint(ui.kodi, req)), orphans, Log(False))
    assert ui.kodi.removed == [2]
    assert ui.dialog.oks == ["S30028\nS30027"]


def test_run_deletes_after_confirmation(ui: SimpleNamespace):
    ui.dialog.answers = [True]
    run()
    assert ui.kodi.removed == [2, 5, 9, 11]
    assert ui.dialog.oks[-1] == "S30027"


def test_run_declined_deletes_nothing(ui: SimpleNamespace):
    ui.dialog.answers = [False]
    run()
    assert ui.kodi.removed == []


def test_run_dry_run_deletes_nothing(ui: SimpleNamespace):
    ui.addon.settings["dry_run"] = True
    run()
    assert ui.kodi.removed == []
    assert ui.dialog.oks[-1].endswith("\nS30024")


def test_run_dry_run_with_details_shows_only_result(ui: SimpleNamespace):
    ui.addon.settings.update(dry_run=True, show_details=True)
    ui.dialog.answers = [False]
    run()
    assert ui.kodi.removed == []
    assert ui.dialog.oks == ["S30024"]


def test_run_details_loop_shows_list_until_declined(ui: SimpleNamespace):
    ui.addon.settings["show_details"] = True
    ui.dialog.answers = [True, False, True]
    run()
    assert len(ui.dialog.selects) == 1
    assert ui.kodi.removed == [2, 5, 9, 11]


def test_run_cancel_during_scan_only_notifies(ui: SimpleNamespace):
    ui.cancel_after = 1
    run()
    assert ui.dialog.notes == ["S30028"]
    assert ui.dialog.oks == []


def test_run_rpc_error_is_shown_and_logged(ui: SimpleNamespace):
    ui.kodi.fail_on = "VideoLibrary.GetEpisodes"
    run()
    assert ui.dialog.oks[-1].startswith("S30029")
    assert any("Query failed" in msg for msg, _ in ui.logs)
    assert ui.kodi.removed == []


def test_run_empty_library_deletes_nothing(ui: SimpleNamespace):
    ui.kodi = make_fake(**{"VideoLibrary.GetMovies": [], "AudioLibrary.GetAlbums": []})
    run()
    assert ui.dialog.oks == ["S30030"]
    assert ui.kodi.removed == []


def test_run_empty_video_library_without_music_is_aborted(ui: SimpleNamespace):
    ui.addon.settings["include_music"] = False
    ui.kodi = make_fake(**{"VideoLibrary.GetMovies": []})
    run()
    assert ui.dialog.oks == ["S30030"]


def test_run_without_orphans_reports_nothing_to_do(ui: SimpleNamespace):
    ui.kodi.data["Textures.GetTextures"] = []
    run()
    assert ui.dialog.oks == ["S30020"]


def test_run_without_orphans_mentions_skipped_categories(ui: SimpleNamespace):
    ui.kodi = make_fake(**{"AudioLibrary.GetAlbums": []})
    ui.kodi.data["Textures.GetTextures"] = []
    run()
    assert ui.dialog.oks == ["S30020\nS30031"]


def test_run_summary_mentions_skipped_categories(ui: SimpleNamespace):
    ui.kodi = make_fake(**{"AudioLibrary.GetAlbums": []})
    ui.dialog.answers = [True]
    run()
    assert ui.kodi.removed == [2, 5, 11]
    assert ui.dialog.oks[-1] == "S30027"

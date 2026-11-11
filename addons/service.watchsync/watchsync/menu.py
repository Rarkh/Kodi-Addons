"""Program add-on menu and RunScript actions (called from the settings dialog)."""

from __future__ import annotations

import json
from typing import Any

import xbmc
import xbmcaddon
import xbmcgui

from .backend import BackendError, VfsBackend, optional_text
from .config import SETTING_MAPPINGS, load_config
from .logger import LOG_FILE
from .mediaid import ARROW, Mapping, format_mappings, parse_mappings
from .runtime import NOTIFY_REBUILD, NOTIFY_SYNC, PROFILE, STATUS_FILE
from .update import newest_offer, version_key

ADDON = xbmcaddon.Addon()
HEADING = ADDON.getAddonInfo("name")
FOLDER_DIALOG = 0
REFRESH_REPOS = "UpdateAddonRepos"


def tr(string_id: int, *args: Any) -> str:
    text = ADDON.getLocalizedString(string_id)
    return text.format(*args) if args else text


def _local() -> VfsBackend:
    return VfsBackend(PROFILE, folders=())


def sync_now() -> None:
    xbmc.executebuiltin(NOTIFY_SYNC)
    xbmcgui.Dialog().notification(HEADING, tr(30050))


def rebuild_cache() -> None:
    if xbmcgui.Dialog().yesno(HEADING, tr(30052)):
        xbmc.executebuiltin(NOTIFY_REBUILD)
        xbmcgui.Dialog().notification(HEADING, tr(30051))


def check_updates() -> None:
    """Show installed and offered version and make Kodi refresh its repositories.

    Installing stays with Kodi (automatic updates, a Kodi restart, or Update in the add-on
    information); the dialog says whether a newer version is there at all."""
    xbmc.executebuiltin(REFRESH_REPOS)
    installed = ADDON.getAddonInfo("version")
    offered, error = newest_offer(ADDON.getAddonInfo("id"))
    if offered is None:
        state = tr(30081, error or "-")
    elif version_key(offered) > version_key(installed):
        state = tr(30079)
    else:
        state = tr(30078)
    xbmcgui.Dialog().ok(HEADING, tr(30076, installed, offered or "-", state))


def show_status() -> None:
    text = optional_text(_local(), "", STATUS_FILE)
    if not text:
        xbmcgui.Dialog().ok(HEADING, tr(30056))
        return
    status = json.loads(text)
    offset = status.get("clock_offset_s")
    xbmcgui.Dialog().textviewer(
        HEADING,
        tr(
            30072,
            status.get("device", ""),
            status.get("storage_path", ""),
            tr(30074) if status.get("online") else tr(30075),
            status.get("last_sync", "-") or "-",
            status.get("queued", 0),
            status.get("media", 0),
            status.get("unmapped", 0),
            ", ".join(status.get("defective", [])) or "-",
            "-" if offset is None else f"{offset:.0f} s",
        ),
    )


def show_log() -> None:
    text = optional_text(_local(), "", LOG_FILE)
    if text:
        xbmcgui.Dialog().textviewer(HEADING, text)
    else:
        xbmcgui.Dialog().ok(HEADING, tr(30073))


def test_connection() -> None:
    config = load_config(ADDON, xbmc.getInfoLabel("System.FriendlyName"))
    if not config.storage_path:
        xbmcgui.Dialog().ok(HEADING, tr(30055))
        return
    backend = VfsBackend(config.storage_path)
    try:
        backend.ensure_layout()
        backend.probe(config.device)
    except BackendError as exc:
        xbmcgui.Dialog().ok(HEADING, tr(30054, exc))
        return
    xbmcgui.Dialog().ok(HEADING, tr(30053))


# (file name, written as text): which kind of file the NAS accepts from this device
DIAGNOSE_CASES = (
    ("diag.txt", False),
    ("diag.tmp", False),
    (".diag.tmp", False),
    ("diag-text.txt", True),
)
DIAGNOSE_FOLDERS = ("", "devices/")


def _try_write(vfs: Any, path: str, as_text: bool) -> str:
    """Write a small file the way the add-on does and say what Kodi answered."""
    payload: Any = "watchsync" if as_text else bytearray(b"watchsync")
    try:
        handle = vfs.File(path, "w")
        try:
            written = handle.write(payload)
        finally:
            handle.close()
        found = vfs.exists(path)
        size = -1
        if found:
            reader = vfs.File(path)
            size = reader.size()
            reader.close()
            vfs.delete(path)
    except Exception as exc:  # noqa: BLE001  (any error is the answer here)
        return f"error: {exc}"
    return f"write={written} exists={found} size={size}"


def _try_rename(vfs: Any, folder_path: str) -> str:
    source, target = folder_path + "diag-rename.tmp", folder_path + "diag-rename.txt"
    try:
        handle = vfs.File(source, "w")
        handle.write(bytearray(b"watchsync"))
        handle.close()
        moved = vfs.rename(source, target)
        vfs.delete(target)
        vfs.delete(source)
    except Exception as exc:  # noqa: BLE001
        return f"error: {exc}"
    return f"rename={moved}"


def diagnose() -> None:
    """Try several ways of writing to the storage location and show the answers."""
    config = load_config(ADDON, xbmc.getInfoLabel("System.FriendlyName"))
    if not config.storage_path:
        xbmcgui.Dialog().ok(HEADING, tr(30055))
        return
    backend = VfsBackend(config.storage_path)
    lines = [tr(30077, config.storage_path), ""]
    for folder in DIAGNOSE_FOLDERS:
        base = backend.root + folder
        lines.append(f"{folder or '/'}: exists={backend.vfs.exists(base)}")
        for name, as_text in DIAGNOSE_CASES:
            kind = "text" if as_text else "bytes"
            lines.append(f"  {name} ({kind}): {_try_write(backend.vfs, base + name, as_text)}")
        lines.append(f"  rename: {_try_rename(backend.vfs, base)}")
    text = "\n".join(lines)
    xbmc.log(f"[WatchSync] diagnosis\n{text}", xbmc.LOGINFO)
    xbmcgui.Dialog().textviewer(HEADING, text)


def ask_mapping(current: Mapping | None) -> Mapping | None:
    """Ask for a path on this device and an optional NAS-relative base."""
    dialog = xbmcgui.Dialog()
    how = dialog.select(tr(30065), [tr(30063), tr(30064)])
    if how < 0:
        return None
    if how == 0:
        prefix = dialog.browseSingle(FOLDER_DIALOG, tr(30065), "video")
    else:
        prefix = dialog.input(tr(30065), current.raw if current else "")
    if not prefix:
        return None
    base = dialog.input(tr(30066), current.base.rstrip("/") if current else "")
    parsed = parse_mappings(prefix + (ARROW + base if base else ""))
    return parsed[0] if parsed else None


def _label(mapping: Mapping) -> str:
    return mapping.raw + (f"  ->  {mapping.base}" if mapping.base else "")


def _change_mapping(mappings: list[Mapping], choice: int) -> None:
    action = xbmcgui.Dialog().select(HEADING, [tr(30061), tr(30062)])
    if action == 0:
        edited = ask_mapping(mappings[choice])
        if edited:
            mappings[choice] = edited
    elif action == 1:
        del mappings[choice]


def edit_mappings() -> None:
    while True:
        mappings = parse_mappings(ADDON.getSettingString(SETTING_MAPPINGS))
        choice = xbmcgui.Dialog().select(HEADING, [*map(_label, mappings), tr(30060)])
        if choice < 0:
            return
        if choice == len(mappings):
            added = ask_mapping(None)
            if added:
                mappings.append(added)
        else:
            _change_mapping(mappings, choice)
        ADDON.setSettingString(SETTING_MAPPINGS, format_mappings(mappings))


ACTIONS = {
    "sync": (30041, sync_now),
    "status": (30042, show_status),
    "mappings": (30043, edit_mappings),
    "log": (30044, show_log),
    "rebuild": (30045, rebuild_cache),
    "test": (30046, test_connection),
    "update": (30047, check_updates),
    "diagnose": (30048, diagnose),
}


def run(argv: list[str]) -> None:
    """RunScript(service.watchsync,<action>) runs one action; otherwise show the menu."""
    name = argv[1] if len(argv) > 1 else ""
    if name not in ACTIONS:
        keys = list(ACTIONS)
        choice = xbmcgui.Dialog().select(HEADING, [tr(ACTIONS[k][0]) for k in keys])
        if choice < 0:
            return
        name = keys[choice]
    ACTIONS[name][1]()

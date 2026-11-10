"""User interface: progress, result dialog, deletion."""

from __future__ import annotations

import posixpath
from typing import Any

import xbmc
import xbmcaddon
import xbmcgui
import xbmcvfs

from resources.lib.library import (
    Call,
    RpcError,
    collect_library,
    fetch_textures,
    make_call,
)
from resources.lib.orphans import MUSIC, VIDEO, Texture, find_orphans, normalize

ADDON = xbmcaddon.Addon()
HEADING = ADDON.getAddonInfo("name")


class Cancelled(Exception):
    """The user cancelled the progress dialog."""


def tr(string_id: int, *args: Any) -> str:
    text = ADDON.getLocalizedString(string_id)
    return text.format(*args) if args else text


def human_size(size: int) -> str:
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size / 1024 / 1024:.1f} MB"


class Log:
    def __init__(self, enabled: bool) -> None:
        self.enabled = enabled

    def __call__(self, message: str, level: int = xbmc.LOGINFO) -> None:
        if self.enabled:
            xbmc.log(f"[{ADDON.getAddonInfo('id')}] {message}", level)


class ProgressDialog:
    """Progress dialog that raises Cancelled when the user aborts."""

    def __init__(self, title: str) -> None:
        self.dialog = xbmcgui.DialogProgress()
        self.dialog.create(HEADING, title)

    def __call__(self, label: str, fraction: float) -> None:
        if self.dialog.iscanceled():
            raise Cancelled
        self.dialog.update(max(0, min(100, int(fraction * 100))), label)

    def close(self) -> None:
        self.dialog.close()


def cache_file_size(texture: dict) -> int:
    """Size in bytes of the cached file of a texture (0 if it is missing)."""
    cached = texture.get("cachedurl")
    if not cached:
        return 0
    try:
        return int(xbmcvfs.Stat(f"special://thumbnails/{cached}").st_size())
    except Exception:  # noqa: BLE001  (missing file or unsupported path: size unknown)
        return 0


def scan(call: Call, include_music: bool, log: Log) -> tuple[list[Texture], list[str]]:
    """Read library and cache; return (orphans, skipped categories)."""
    progress = ProgressDialog(tr(30011))
    try:
        collected = collect_library(call, include_music, progress)
        progress.dialog.update(0, tr(30012))
        textures = fetch_textures(call, progress)
    finally:
        progress.close()

    log(
        f"Library: {collected.counts}, {len(collected.urls)} image URLs, "
        f"{len(textures)} textures in cache"
    )
    skipped = []
    categories = [VIDEO] + ([MUSIC] if include_music else [])
    for category in categories:
        if collected.counts.get(category, 0) == 0:
            skipped.append(tr(30032 if category == VIDEO else 30033))
    orphans = find_orphans(
        textures,
        collected.urls,
        reference_categories=collected.counts,
        video_sources=collected.video_sources,
        music_sources=collected.music_sources,
        include_music=include_music,
        size_of=cache_file_size,
    )
    return orphans, skipped


def label_for(texture: Texture) -> str:
    name = posixpath.basename(normalize(texture.url)[1]) or texture.url
    category = tr(30032 if texture.category == VIDEO else 30033)
    return f"[{category}] {name} ({human_size(texture.size)})"


def show_details(orphans: list[Texture]) -> None:
    ordered = sorted(orphans, key=lambda t: t.size, reverse=True)
    xbmcgui.Dialog().select(HEADING, [label_for(t) for t in ordered])


def delete(call: Call, orphans: list[Texture], log: Log) -> None:
    progress = ProgressDialog(tr(30026))
    removed = failed = freed = 0
    try:
        for index, tex in enumerate(orphans, start=1):
            progress(f"{index}/{len(orphans)}", index / len(orphans))
            try:
                call("Textures.RemoveTexture", {"textureid": tex.texture_id})
            except RpcError as exc:
                failed += 1
                log(f"Delete failed: {tex.url}: {exc}", xbmc.LOGWARNING)
                continue
            removed += 1
            freed += tex.size
            log(f"Deleted: {tex.url} ({human_size(tex.size)})")
    except Cancelled:
        xbmcgui.Dialog().ok(HEADING, tr(30028) + "\n" + tr(30027, removed, human_size(freed)))
        return
    finally:
        progress.close()
    message = tr(30027, removed, human_size(freed))
    if failed:
        message += "\n" + tr(30034, failed)
    log(message)
    xbmcgui.Dialog().ok(HEADING, message)


def run() -> None:
    settings = ADDON
    dry_run = settings.getSettingBool("dry_run")
    include_music = settings.getSettingBool("include_music")
    details = settings.getSettingBool("show_details")
    log = Log(settings.getSettingBool("log_to_kodi"))
    dialog = xbmcgui.Dialog()
    call = make_call(xbmc.executeJSONRPC)

    try:
        orphans, skipped = scan(call, include_music, log)
    except Cancelled:
        dialog.notification(HEADING, tr(30028))
        return
    except RpcError as exc:
        log(f"Query failed: {exc}", xbmc.LOGERROR)
        dialog.ok(HEADING, tr(30029, exc))
        return

    if len(skipped) == (2 if include_music else 1):
        # No category has library entries: nothing to compare against, delete nothing
        log("Library empty, aborting", xbmc.LOGWARNING)
        dialog.ok(HEADING, tr(30030))
        return

    note = ("\n" + tr(30031, ", ".join(skipped))) if skipped else ""
    if not orphans:
        dialog.ok(HEADING, tr(30020) + note)
        return

    total = sum(t.size for t in orphans)
    summary = tr(30021, len(orphans), human_size(total)) + note
    log(summary)
    for tex in orphans:
        log(f"Orphaned: {tex.url} ({tex.category}, {human_size(tex.size)})", xbmc.LOGDEBUG)

    while details:
        if not dialog.yesno(HEADING, summary, nolabel=tr(30023), yeslabel=tr(30022)):
            break
        show_details(orphans)

    if dry_run:
        dialog.ok(HEADING, tr(30024) if details else summary + chr(10) + tr(30024))
        return
    if dialog.yesno(HEADING, tr(30025, len(orphans), human_size(total))):
        delete(call, orphans, log)

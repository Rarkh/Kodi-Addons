"""Settings of the add-on as one value object."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .mediaid import Mapping, parse_mappings

SETTING_MAPPINGS = "path_mappings"


@dataclass(frozen=True)
class Config:
    enabled: bool
    storage_path: str
    device: str
    mappings: list[Mapping]
    sync_interval: float
    save_interval: float
    threshold: float
    cache_enabled: bool
    debug: bool


def sanitize_device(name: str) -> str:
    """Device ids use a-z, 0-9 and "-" only."""
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def device_id(addon: Any, friendly_name: str) -> str:
    """The configured device id; if empty it is derived from Kodi's name and stored."""
    configured = sanitize_device(addon.getSettingString("device_id"))
    if configured:
        return configured
    derived = sanitize_device(friendly_name) or "kodi"
    addon.setSettingString("device_id", derived)
    return derived


def load_config(addon: Any, friendly_name: str) -> Config:
    return Config(
        enabled=addon.getSettingBool("enabled"),
        storage_path=addon.getSettingString("storage_path").strip(),
        device=device_id(addon, friendly_name),
        mappings=parse_mappings(addon.getSettingString(SETTING_MAPPINGS)),
        sync_interval=max(5, addon.getSettingInt("sync_interval")),
        save_interval=max(5, addon.getSettingInt("save_interval")),
        threshold=min(100, max(1, addon.getSettingInt("watched_threshold"))) / 100,
        cache_enabled=addon.getSettingBool("cache_enabled"),
        debug=addon.getSettingInt("log_level") == 1,
    )

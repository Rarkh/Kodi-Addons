"""Ask the repositories which version of this add-on they offer (standard library only)."""

from __future__ import annotations

import re
import time
import urllib.request
import xml.etree.ElementTree as ET
from typing import Callable

REPO_INDEXES = (
    "https://raw.githubusercontent.com/Rarkh/Kodi-Addons/dev/repo-test/addons.xml",
    "https://raw.githubusercontent.com/Rarkh/Kodi-Addons/main/repo/addons.xml",
)
TIMEOUT_S = 10


def fetch_text(url: str) -> str:
    """Download a text file; a changing query keeps a cache on the way from answering."""
    request = urllib.request.Request(
        f"{url}?t={int(time.time())}", headers={"User-Agent": "WatchSync"}
    )
    with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:  # noqa: S310  (https only)
        return response.read().decode("utf-8")


def version_key(version: str) -> tuple[int, ...]:
    """1.2.3 -> (1, 2, 3); 1.2.3-rc4 -> (1, 2, 3, 4). Like Kodi, a test build -rcN ranks above
    the release of the same number and below the next release."""
    return tuple(int(number) for number in re.findall(r"\d+", version))


def offered_version(index_xml: str, addon_id: str) -> str | None:
    """The highest version of an add-on listed in an addons.xml."""
    root = ET.fromstring(index_xml)
    versions = [
        str(item.get("version"))
        for item in root.iter("addon")
        if item.get("id") == addon_id and item.get("version")
    ]
    return max(versions, key=version_key, default=None)


def newest_offer(
    addon_id: str, fetch: Callable[[str], str] = fetch_text
) -> tuple[str | None, str | None]:
    """(highest version any repository offers, error text if none could be read)."""
    best: str | None = None
    errors: list[str] = []
    for url in REPO_INDEXES:
        try:
            version = offered_version(fetch(url), addon_id)
        except (OSError, ValueError, ET.ParseError) as exc:
            errors.append(str(exc))
            continue
        if version and (best is None or version_key(version) > version_key(best)):
            best = version
    return best, ("; ".join(errors) if best is None and errors else None)

"""WatchSync: looking up the version the repositories offer."""

from __future__ import annotations

import io
import urllib.request
from typing import Callable

import pytest
from watchsync import update

INDEX = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<addons>
<addon id="other" version="9.0.0"/>
<addon id="service.watchsync" version="0.2.10"/>
<addon id="service.watchsync" version="0.2.9"/>
</addons>"""


def pages(mapping: dict[str, str]) -> Callable[[str], str]:
    return lambda url: mapping[url]


def test_version_key_compares_numbers_not_text():
    assert update.version_key("0.2.10") > update.version_key("0.2.9")
    assert update.version_key("1.0.0-test") == (1, 0, 0)


def test_offered_version_takes_the_highest_listing():
    assert update.offered_version(INDEX, "service.watchsync") == "0.2.10"
    assert update.offered_version(INDEX, "missing") is None


def test_newest_offer_uses_the_best_of_all_repositories():
    both = pages({update.REPO_INDEXES[0]: INDEX, update.REPO_INDEXES[1]: "<addons/>"})
    assert update.newest_offer("service.watchsync", both) == ("0.2.10", None)


@pytest.mark.parametrize("first_is_higher", [True, False])
def test_newest_offer_prefers_the_higher_of_two_repositories(first_is_higher: bool):
    low = '<addons><addon id="a" version="1.0.0"/></addons>'
    high = '<addons><addon id="a" version="1.1.0"/></addons>'
    first, second = (high, low) if first_is_higher else (low, high)
    both = pages({update.REPO_INDEXES[0]: first, update.REPO_INDEXES[1]: second})
    assert update.newest_offer("a", both) == ("1.1.0", None)


def test_newest_offer_reports_errors_only_when_nothing_was_found():
    def broken(url: str) -> str:
        raise OSError("offline")

    assert update.newest_offer("a", broken) == (None, "offline; offline")
    mixed = pages({update.REPO_INDEXES[0]: "<addons", update.REPO_INDEXES[1]: INDEX})
    assert update.newest_offer("service.watchsync", mixed) == ("0.2.10", None)


def test_fetch_text_downloads_with_a_cache_buster(monkeypatch: pytest.MonkeyPatch):
    seen = {}

    def fake_urlopen(request, timeout):
        seen["url"], seen["timeout"] = request.full_url, timeout
        return io.BytesIO("grüß".encode())

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    assert update.fetch_text("https://example.org/addons.xml") == "grüß"
    assert seen["url"].startswith("https://example.org/addons.xml?t=")
    assert seen["timeout"] == update.TIMEOUT_S

"""WatchSync: media ids, event format, merge rules."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any

import pytest
from watchsync import mediaid
from watchsync.events import (
    Event,
    EventError,
    Snapshot,
    UnsupportedVersion,
    decode_event,
    decode_snapshot,
    encode_event,
    encode_snapshot,
    event_filename,
    event_from_dict,
    format_ts,
    parse_filename,
    parse_ts,
)
from watchsync.merge import filter_future, merge

# ---- media ids ---------------------------------------------------------------------------

MAPS = mediaid.parse_mappings(
    "nfs://192.168.178.57/RAID5/|R:\\|/media/nas/|nfs://192.168.178.57/RAID5/Serien/=>Series"
)


@pytest.mark.parametrize(
    "path",
    [
        "nfs://192.168.178.57/RAID5/Filme/Alien.mkv",
        "R:\\Filme\\Alien.mkv",
        "r:/Filme/Alien.mkv",
        "/media/nas/Filme/Alien.mkv",
        "smb://user:secret@192.168.178.57/RAID5/Filme/Alien.mkv".replace("smb", "nfs"),
    ],
)
def test_same_movie_gets_the_same_id_on_every_device(path):
    ref = mediaid.to_media_id(path, MAPS)
    assert ref == mediaid.MediaRef("Filme/Alien.mkv", False)


def test_longest_prefix_wins_and_base_is_prepended():
    ref = mediaid.to_media_id("nfs://192.168.178.57/RAID5/Serien/A/S01E01.mkv", MAPS)
    assert ref.id == "Series/A/S01E01.mkv"


def test_unmapped_path_keeps_full_path_without_credentials_and_is_decoded():
    ref = mediaid.to_media_id("smb://user:pw@other/Share/My%20Film.mkv", MAPS)
    assert ref == mediaid.MediaRef("smb://other/Share/My Film.mkv", True)


def test_file_name_alone_is_never_the_id():
    assert mediaid.to_media_id("/somewhere/Alien.mkv", []).id != "Alien.mkv"


def test_trackable_paths():
    assert mediaid.is_trackable("nfs://nas/a.mkv")
    assert not mediaid.is_trackable("plugin://plugin.video.x/?a=1")
    assert not mediaid.is_trackable("")


def test_parse_mappings_ignores_empty_entries_and_round_trips():
    parsed = mediaid.parse_mappings("R:\\ | \n/media/nas=>Base\\Sub|")
    assert [m.raw for m in parsed] == ["R:\\", "/media/nas/"]
    assert parsed[1].base == "Base/Sub/"
    assert mediaid.format_mappings(parsed) == "R:\\|/media/nas/=>Base/Sub"
    assert mediaid.parse_mappings("") == []


def test_from_media_id_builds_local_paths():
    maps = mediaid.parse_mappings("R:\\|/media/nas/=>Base")
    assert mediaid.from_media_id("Filme/A.mkv", maps) == "R:\\Filme\\A.mkv"
    assert mediaid.from_media_id("Base/Filme/A.mkv", maps[1:]) == "/media/nas/Filme/A.mkv"
    assert mediaid.from_media_id("Base/x", [maps[0]]) == "R:\\Base\\x"
    assert mediaid.from_media_id("x", maps[1:]) is None
    assert mediaid.from_media_id("x", []) is None


class FakeSource:
    def __init__(self, data: bytes) -> None:
        self.data = data
        self.pos = 0
        self.closed = False

    def size(self) -> int:
        return len(self.data)

    def seek(self, offset: int, whence: int = 0) -> int:
        self.pos = offset
        return offset

    def readBytes(self, count: int) -> bytearray:  # noqa: N802
        return bytearray(self.data[self.pos : self.pos + count])

    def close(self) -> None:
        self.closed = True


def test_fingerprint_uses_size_and_first_and_last_64kb():
    data = bytes(range(256)) * 1024  # 256 KB
    expected = hashlib.sha1(data[:65536] + data[-65536:]).hexdigest()  # noqa: S324
    assert mediaid.fingerprint(FakeSource(data)) == f"s={len(data)};h={expected};"


def test_fingerprint_of_small_and_empty_files():
    small = b"abc"
    expected = hashlib.sha1(small + small).hexdigest()  # noqa: S324
    assert mediaid.fingerprint(FakeSource(small)) == f"s=3;h={expected};"
    assert mediaid.fingerprint(FakeSource(b"")) is None


def test_fingerprinter_caches_and_survives_errors():
    opened: list[str] = []

    class Unsized:
        def close(self) -> None:
            pass

    sources: dict[str, Any] = {"a": FakeSource(b"abc"), "bad": Unsized()}

    def opener(path: str):
        opened.append(path)
        if path == "missing":
            raise OSError("gone")
        return sources[path]

    printer = mediaid.Fingerprinter(opener, max_entries=2)
    first = printer.get("a")
    assert printer.get("a") == first
    assert opened == ["a"]
    assert sources["a"].closed
    assert printer.get("missing") is None
    assert printer.get("bad") is None  # size() missing: no fingerprint
    assert len(printer.cache) <= 2


# ---- event format ------------------------------------------------------------------------

SPEC_EVENT = {
    "v": 1,
    "device": "android-wohnzimmer",
    "seq": 42,
    "ts": "2026-11-10T11:47:03.512Z",
    "type": "progress",
    "media": {"id": "Filme/Alien.mkv", "fp": "s=4812345678;h=9f2c;"},
    "position_s": 3120.5,
    "duration_s": 7020.0,
    "playcount": 0,
}


def test_event_round_trip_matches_the_specified_format():
    event = event_from_dict(SPEC_EVENT)
    assert event.to_dict() == SPEC_EVENT
    assert decode_event(encode_event(event)) == event
    assert event_filename(event.device, event.seq) == "android-wohnzimmer_000042.json"


def test_optional_fields_and_unmapped_flag():
    minimal = {
        "v": 1,
        "device": "a",
        "seq": 1,
        "ts": "2026-01-01T00:00:00Z",
        "type": "reset",
        "media": {"id": "/x/y.mkv", "unmapped": True},
    }
    event = event_from_dict(minimal)
    assert (event.position_s, event.playcount, event.duration_s, event.fp) == (0.0, 0, None, None)
    assert event.unmapped
    assert event.to_dict()["media"] == {"id": "/x/y.mkv", "unmapped": True}
    assert "duration_s" not in event.to_dict()


def _bad(**changes: Any) -> dict[str, Any]:
    data = {**SPEC_EVENT, **changes}
    return {k: v for k, v in data.items() if v is not None}


@pytest.mark.parametrize(
    "data",
    [
        "text",
        _bad(v=0),
        _bad(v=True),
        _bad(v=None),
        _bad(device="Bad_Device"),
        _bad(device=5),
        _bad(type="bogus"),
        _bad(media="x"),
        _bad(media={"id": ""}),
        _bad(media={"id": 5}),
        _bad(seq=None),
        _bad(seq=-1),
        _bad(seq=True),
        _bad(seq="1"),
        _bad(ts="yesterday"),
        _bad(ts=5),
        _bad(ts="2026-13-40T00:00:00Z"),
        _bad(playcount=-1),
        _bad(playcount=1.5),
        _bad(position_s=-1),
        _bad(position_s="1"),
        _bad(position_s=True),
        _bad(duration_s=-5),
    ],
)
def test_invalid_events_are_rejected(data):
    with pytest.raises(EventError):
        event_from_dict(data)


def test_newer_format_version_is_reported_separately():
    data = _bad(v=2)
    with pytest.raises(UnsupportedVersion):
        event_from_dict(data)


def test_decode_rejects_broken_json():
    with pytest.raises(EventError):
        decode_event('{"v": 1, "dev')


def test_timestamps():
    assert format_ts(datetime(2026, 11, 10, 11, 47, 3, 512000, tzinfo=timezone.utc)) == (
        "2026-11-10T11:47:03.512Z"
    )
    assert parse_ts("2026-11-10T11:47:03Z").second == 3
    assert parse_ts("2026-11-10T11:47:03.5Z").microsecond == 500000


def test_filenames():
    assert parse_filename("a-b_000042.json") == ("a-b", 42)
    assert parse_filename("a-b_1234567.json") == ("a-b", 1234567)
    assert parse_filename("a-b_000042.tmp") is None
    assert parse_filename("a-b_42.json") is None


def _event(device="a", seq=1, ts="2026-01-01T10:00:00.000Z", kind="progress", media="m", **kw):
    return Event(device=device, seq=seq, ts=ts, type=kind, media_id=media, **kw)


def test_snapshot_round_trip():
    items = (
        _event(position_s=5.0, duration_s=9.0, fp="s=1;h=x;"),
        _event(media="n", unmapped=True),
    )
    snapshot = Snapshot("a", 7, "2026-01-02T00:00:00.000Z", tuple(replace_seq(i, 7) for i in items))
    decoded = decode_snapshot(encode_snapshot(snapshot))
    assert decoded == snapshot
    assert decoded.items[0].seq == 7


def replace_seq(event: Event, seq: int) -> Event:
    from dataclasses import replace

    return replace(event, seq=seq)


@pytest.mark.parametrize(
    "data",
    [
        [],
        {"v": 1, "device": "a", "upto_seq": "x", "ts": "2026-01-01T00:00:00Z", "items": []},
        {"v": 1, "device": "a", "upto_seq": 1, "ts": "2026-01-01T00:00:00Z", "items": {}},
        {"v": 1, "device": "A!", "upto_seq": 1, "ts": "2026-01-01T00:00:00Z", "items": []},
        {"v": 1, "device": "a", "upto_seq": 1, "ts": "never", "items": []},
        {"v": 1, "device": "a", "upto_seq": 1, "ts": "2026-01-01T00:00:00Z", "items": [5]},
        {"v": 1, "device": "a", "upto_seq": 1, "ts": "2026-01-01T00:00:00Z", "items": [{}]},
    ],
)
def test_invalid_snapshots(data):
    text = json.dumps(data)
    with pytest.raises(EventError):
        decode_snapshot(text)


def test_snapshot_with_newer_version():
    text = json.dumps({"v": 9, "device": "a", "upto_seq": 1, "ts": "x", "items": []})
    with pytest.raises(UnsupportedVersion):
        decode_snapshot(text)


# ---- merge -------------------------------------------------------------------------------


def test_newest_timestamp_wins_not_the_largest_position():
    old = _event(device="a", ts="2026-01-01T10:00:00.000Z", position_s=3000.0)
    rewound = _event(device="b", ts="2026-01-01T11:00:00.000Z", position_s=100.0)
    assert merge([old, rewound])["m"].position_s == 100.0
    assert merge([rewound, old])["m"].position_s == 100.0


def test_equal_timestamps_use_the_larger_device_id():
    a = _event(device="alpha", position_s=1.0)
    b = _event(device="beta", position_s=2.0)
    assert merge([b, a])["m"].device == "beta"
    assert merge([a, b])["m"].device == "beta"


def test_unwatched_after_watched_wins_by_time():
    watched = _event(kind="watched", playcount=1, ts="2026-01-01T10:00:00.000Z")
    unwatched = _event(device="b", kind="unwatched", ts="2026-01-01T12:00:00.000Z")
    assert merge([watched, unwatched])["m"].type == "unwatched"


def test_same_fingerprint_links_renamed_files():
    old = _event(media="Old/A.mkv", fp="s=1;h=x;", ts="2026-01-01T10:00:00.000Z", playcount=1)
    new = _event(device="b", media="New/A.mkv", fp="s=1;h=x;", ts="2026-01-02T10:00:00.000Z")
    result = merge([old, new])
    assert result["Old/A.mkv"].ts == result["New/A.mkv"].ts == new.ts
    assert result["Old/A.mkv"].media_id == "Old/A.mkv"
    assert result["New/A.mkv"].device == "b"


def test_different_media_stay_separate_and_unmapped_flag_is_per_id():
    one = _event(media="one", fp="s=1;h=x;")
    two = _event(media="two", fp="s=2;h=y;", unmapped=True)
    third = _event(media="three", fp="s=1;h=x;")
    result = merge([one, two, third])
    assert result["two"].unmapped
    assert not result["one"].unmapped
    assert set(result) == {"one", "two", "three"}
    assert merge([]) == {}


def test_events_far_in_the_future_are_ignored():
    now = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)
    fine = _event(ts="2026-01-02T11:00:00.000Z")
    broken = _event(device="b", ts="2026-01-05T00:00:00.000Z")
    assert filter_future([fine, broken], now) == ([fine], [broken])


@pytest.mark.parametrize(
    ("first", "second", "same"),
    [
        (
            "nfs://192.168.0.5/RAID5/SERIES/Show/S01/E01.mkv",
            "smb://nas/RAID5/series/show/s01/e01.MKV",
            True,
        ),
        (r"R:\SERIES\Show\S01\E01.mkv", "nfs://nas/RAID5/SERIES/Show/S01/E01.mkv", True),
        ("Filme/Alien.mkv", "nfs://nas/RAID5/Filme/Alien.mkv", True),
        ("nfs://nas/A/Show/S01/E01.mkv", "nfs://nas/A/Other/S01/E01.mkv", False),
        ("smb://nas/RAID5/film.mkv", "nfs://192.168.0.5/RAID5/Film.mkv", True),
        ("R:\film.mkv", "nfs://nas/RAID5/film.mkv", False),
        ("nfs://nas/Alien.mkv", "nfs://nas/Filme/Alien.mkv", False),
        ("Alien.mkv", "nfs://nas/Filme/Alien.mkv", False),
        ("", "Filme/Alien.mkv", False),
    ],
)
def test_same_medium_ignores_protocol_host_share_and_case(first, second, same):
    assert mediaid.same_medium(first, second) is same
    assert mediaid.same_medium(second, first) is same


def test_file_name_key():
    assert mediaid.file_name_key("smb://nas/Show/S01/E01.MKV") == "e01.mkv"
    assert mediaid.file_name_key("") == ""


@pytest.mark.parametrize(
    ("path", "location"),
    [
        ("nfs://192.168.0.5/RAID5/a.mkv", "nfs://192.168.0.5/"),
        ("SMB://NAS/Share/a.mkv", "smb://nas/"),
        ("R:\\Filme\a.mkv", "r:/"),
        ("/media/nas/a.mkv", "/media/nas/"),
        ("/a.mkv", ""),
    ],
)
def test_location_of(path, location):
    assert mediaid.location_of(path) == location


NFS_PLAYED = "nfs://10.0.0.5/RAID5/SERIES/Show/S01/E01.mkv"


def test_resolver_translates_between_locations_it_has_seen_the_same_file_in():
    resolver = mediaid.PathResolver([NFS_PLAYED], ["smb://nas/RAID5/SERIES/Show/S01/e01.MKV"])
    assert resolver.resolve("smb://nas/RAID5/SERIES/Other/S01/E02.mkv") == (
        "nfs://10.0.0.5/RAID5/SERIES/Other/S01/E02.mkv"
    )
    assert resolver.resolve("smb://elsewhere/RAID5/x/y.mkv") is None


def test_resolver_uses_the_same_location_unchanged():
    resolver = mediaid.PathResolver([NFS_PLAYED], [])
    assert resolver.resolve("nfs://10.0.0.5/Other/x.mkv") == "nfs://10.0.0.5/Other/x.mkv"
    assert resolver.resolve("nfs://10.9.9.9/Other/x.mkv") is None


def test_resolver_keeps_the_windows_style_of_a_drive():
    resolver = mediaid.PathResolver(
        [r"R:\SERIES\Show\S01\E01.mkv"], ["smb://nas/SERIES/Show/S01/E01.mkv"]
    )
    assert resolver.resolve("smb://nas/SERIES/Other/S01/E02.mkv") == r"R:\SERIES\Other\S01\E02.mkv"
    assert resolver.resolve("r:/Anything/b.mkv") == "r:\\Anything\\b.mkv"


@pytest.mark.parametrize(
    "known",
    [
        "smb://nas/other/name.mkv",  # not the same file
        "nfs://10.0.0.5/RAID5/SERIES/Show/S01/E01.mkv",  # the same location: nothing to learn
        "E01.mkv",  # nothing in front of the common tail
    ],
)
def test_resolver_learns_nothing_from_unusable_pairs(known):
    assert mediaid.PathResolver([NFS_PLAYED], [known]).pairs == []


@pytest.mark.parametrize(
    ("folder", "usable"),
    [
        ("nfs://192.168.0.5/RAID5/Show/", True),
        ("SMB://nas/Share/", True),
        ("/storage/emulated/0/Serien/", True),
        ("D:\\Serien\\", True),
        ("D:/Serien/", True),
        ("", False),
        ("videodb://tvshows/titles/", False),
        ("library://video/", False),
        ("plugin://plugin.video.x/", False),
        ("sources://video/", False),
    ],
)
def test_is_file_folder(folder, usable):
    assert mediaid.is_file_folder(folder) is usable


def test_a_local_path_of_this_device_resolves_to_itself():
    from watchsync.mediaid import PathResolver, location_of

    assert location_of("/storage/1234-5678/KODI/a.mkv") == "/storage/1234-5678/"
    assert location_of("/a.mkv") == ""
    resolver = PathResolver(["/storage/1234-5678/KODI/x.mkv"], [])
    assert resolver.resolve("/storage/1234-5678/KODI/y.mkv") == "/storage/1234-5678/KODI/y.mkv"
    assert resolver.resolve("/storage/emulated/0/y.mkv") is None

"""Fakes for the WatchSync tests: an in-memory xbmcvfs and a controllable clock."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from watchsync.backend import LAYOUT, VfsBackend


class FakeFile:
    def __init__(self, vfs: FakeVfs, path: str, mode: str | None = None) -> None:
        self.vfs = vfs
        self.path = path
        self.mode = mode
        self.buffer = bytearray()
        self.position = 0
        self.closed = False
        if mode != "w":
            self.buffer = bytearray(vfs.files.get(path, b""))

    def size(self) -> int:
        return len(self.buffer)

    def seek(self, offset: int, whence: int = 0) -> int:
        self.position = offset
        return offset

    def readBytes(self, count: int = 0) -> bytearray:  # noqa: N802
        data = self.buffer[self.position : self.position + count]
        self.position += len(data)
        return bytearray(data)

    def write(self, data: Any) -> bool:
        if self.vfs.fail_write or self.path.startswith(tuple(self.vfs.readonly)):
            return False
        self.buffer += bytes(data)
        return True

    def close(self) -> None:
        self.closed = True
        if self.mode == "w":
            self.vfs.files[self.path] = bytes(self.buffer)


class FakeStat:
    def __init__(self, vfs: FakeVfs, path: str) -> None:
        if path not in vfs.files:
            raise OSError("no such file")
        self.mtime = vfs.mtimes.get(path, vfs.default_mtime)

    def st_mtime(self) -> float:
        return self.mtime


class FakeVfs:
    """Just enough of xbmcvfs. down=True behaves like an unreachable NAS."""

    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}
        self.dirs: set[str] = set()
        self.mtimes: dict[str, float] = {}
        self.default_mtime = 0.0
        self.down = False
        self.fail_write = False
        self.readonly: set[str] = set()  # folders this device may not write to
        self.fail_rename = False
        self.fail_delete = False

    def File(self, path: str, mode: str | None = None) -> FakeFile:  # noqa: N802
        return FakeFile(self, path, mode)

    def Stat(self, path: str) -> FakeStat:  # noqa: N802
        return FakeStat(self, path)

    def _unreachable(self, path: str) -> bool:
        """Only the NAS (nfs://) goes away; the local profile stays available."""
        return self.down and path.startswith("nfs://")

    def exists(self, path: str) -> bool:
        return not self._unreachable(path) and (path in self.files or path in self.dirs)

    def mkdirs(self, path: str) -> bool:
        if self._unreachable(path):
            return False
        self.dirs.add(path)
        return True

    def listdir(self, path: str) -> tuple[list[str], list[str]]:
        if self._unreachable(path):
            return [], []
        names = [
            p[len(path) :] for p in self.files if p.startswith(path) and "/" not in p[len(path) :]
        ]
        return [], sorted(names)

    def delete(self, path: str) -> bool:
        if self.fail_delete:
            return False
        self.files.pop(path, None)
        return True

    def rename(self, source: str, target: str) -> bool:
        if self.fail_rename or source not in self.files:
            return False
        self.files[target] = self.files.pop(source)
        return True

    # helpers for tests
    def put(self, path: str, text: str) -> None:
        self.files[path] = text.encode("utf-8")

    def get(self, path: str) -> str:
        return self.files[path].decode("utf-8")


class Clock:
    """A settable UTC clock; also usable as the monotonic clock via .mono()."""

    def __init__(self, start: str = "2026-11-10T12:00:00") -> None:
        self.now_value = datetime.fromisoformat(start).replace(tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.now_value

    def advance(self, seconds: float) -> None:
        self.now_value += timedelta(seconds=seconds)

    def mono(self) -> float:
        return self.now_value.timestamp()


def make_backend(vfs: FakeVfs, root: str = "nfs://nas/WatchSync/") -> VfsBackend:
    return VfsBackend(root, LAYOUT, vfs)


def make_local(vfs: FakeVfs, root: str = "special://profile/a/") -> VfsBackend:
    vfs.dirs.add(root)
    return VfsBackend(root, (), vfs)

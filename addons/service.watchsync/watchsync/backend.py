"""Storage backend: the interface and the xbmcvfs implementation (NFS/SMB/local).

All access goes through xbmcvfs; there is no file locking. Files are written as <name>.tmp,
read back, then renamed to their final name.
"""

from __future__ import annotations

import posixpath
from typing import Any, Iterable

import xbmcvfs

LAYOUT = ("events", "devices", "snapshots")


class BackendError(Exception):
    """A storage operation failed (network down, file missing, rename refused ...)."""


class Backend:
    """What the sync engine needs from a storage. Replace for e.g. an HTTP backend."""

    def ensure_layout(self) -> None:
        raise NotImplementedError

    def list_files(self, folder: str) -> list[str]:
        raise NotImplementedError

    def read_text(self, folder: str, name: str) -> str:
        raise NotImplementedError

    def write_text(self, folder: str, name: str, text: str, overwrite: bool = False) -> None:
        raise NotImplementedError

    def delete(self, folder: str, name: str) -> None:
        raise NotImplementedError

    def mtime(self, folder: str, name: str) -> float:
        raise NotImplementedError

    def probe(self, label: str) -> None:
        """Write, read back and delete a test file; raise BackendError if that fails."""
        name = f".probe-{label}.txt"
        self.write_text("", name, "watchsync", overwrite=True)
        if self.read_text("", name) != "watchsync":
            raise BackendError("probe file differs")
        self.delete("", name)


class VfsBackend(Backend):
    def __init__(self, root: str, folders: Iterable[str] = LAYOUT, vfs: Any = xbmcvfs) -> None:
        self.root = root if root.endswith(("/", "\\")) else root + "/"
        self.folders = tuple(folders)
        self.vfs = vfs

    def _dir(self, folder: str) -> str:
        return self.root + (folder + "/" if folder else "")

    def _path(self, folder: str, name: str) -> str:
        return self._dir(folder) + name

    def ensure_layout(self) -> None:
        for folder in ("", *self.folders):
            path = self._dir(folder)
            if not self.vfs.exists(path) and not self.vfs.mkdirs(path):
                raise BackendError(f"cannot create {path}")

    def list_files(self, folder: str) -> list[str]:
        path = self._dir(folder)
        if not self.vfs.exists(path):
            raise BackendError(f"{path} not reachable")
        return list(self.vfs.listdir(path)[1])

    def _read_bytes(self, path: str) -> bytes:
        if not self.vfs.exists(path):
            raise BackendError(f"{path} does not exist")
        file = self.vfs.File(path)
        try:
            return bytes(file.readBytes(file.size()))
        finally:
            file.close()

    def read_text(self, folder: str, name: str) -> str:
        try:
            return self._read_bytes(self._path(folder, name)).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise BackendError(f"{name}: not UTF-8") from exc

    def _same_content(self, path: str, data: bytes) -> bool:
        try:
            return self._read_bytes(path) == data
        except BackendError:
            return False

    def write_text(self, folder: str, name: str, text: str, overwrite: bool = False) -> None:
        data = text.encode("utf-8")
        target = self._path(folder, name)
        tmp = self._path(folder, posixpath.splitext(name)[0] + ".tmp")
        if self.vfs.exists(tmp):
            self.vfs.delete(tmp)
        file = self.vfs.File(tmp, "w")
        try:
            written = file.write(bytearray(data))
        finally:
            file.close()
        if not written or not self._same_content(tmp, data):
            self.vfs.delete(tmp)
            raise BackendError(f"cannot write {tmp}")
        if self.vfs.exists(target):
            if not overwrite:
                same = self._same_content(target, data)
                self.vfs.delete(tmp)
                if same:
                    return
                raise BackendError(f"{target} already exists")
            self.vfs.delete(target)
        if not self.vfs.rename(tmp, target):
            raise BackendError(f"cannot rename {tmp}")

    def delete(self, folder: str, name: str) -> None:
        path = self._path(folder, name)
        if self.vfs.exists(path) and not self.vfs.delete(path):
            raise BackendError(f"cannot delete {path}")

    def mtime(self, folder: str, name: str) -> float:
        try:
            return float(self.vfs.Stat(self._path(folder, name)).st_mtime())
        except Exception as exc:  # noqa: BLE001  (Stat raises various errors)
            raise BackendError(f"cannot stat {name}") from exc


def optional_text(backend: Backend, folder: str, name: str) -> str | None:
    """Text of a file, or None if it is not there."""
    try:
        return backend.read_text(folder, name)
    except BackendError:
        return None

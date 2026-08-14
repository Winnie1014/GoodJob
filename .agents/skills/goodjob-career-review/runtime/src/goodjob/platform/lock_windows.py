"""Windows non-blocking file lock backend."""

from __future__ import annotations

import importlib
import os
from pathlib import Path

from goodjob.errors import WriterBusyError


class WindowsFileLock:
    """Own one shared or exclusive locked byte until deterministic release."""

    def __init__(self, path: Path, *, shared: bool) -> None:
        self._path = path
        self._shared = shared
        self._fd: int | None = None

    def acquire(self) -> None:
        self._path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(self._path, os.O_CREAT | os.O_RDWR, 0o600)
        msvcrt = importlib.import_module("msvcrt")
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            mode = msvcrt.LK_NBRLCK if self._shared else msvcrt.LK_NBLCK
            msvcrt.locking(fd, mode, 1)
        except OSError as exc:
            os.close(fd)
            raise WriterBusyError("another GoodJob writer currently owns the lock") from exc
        self._fd = fd

    def release(self) -> None:
        if self._fd is None:
            return
        fd = self._fd
        self._fd = None
        msvcrt = importlib.import_module("msvcrt")
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        finally:
            os.close(fd)


class WindowsExclusiveFileLock(WindowsFileLock):
    def __init__(self, path: Path) -> None:
        super().__init__(path, shared=False)


class WindowsSharedFileLock(WindowsFileLock):
    def __init__(self, path: Path) -> None:
        super().__init__(path, shared=True)

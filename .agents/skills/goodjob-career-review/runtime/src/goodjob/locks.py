"""Operating-system-backed non-blocking writer locking."""

from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path
from types import TracebackType
from typing import Any, Self

from goodjob.errors import WriterBusyError


class ExclusiveWriterLock:
    """A lock whose authority is the held file descriptor, never file contents or age."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._fd: int | None = None
        self._windows_lock: Any | None = None

    def __enter__(self) -> Self:
        if sys.platform == "win32":
            from goodjob.platform.lock_windows import WindowsExclusiveFileLock

            windows_lock = WindowsExclusiveFileLock(self._path)
            windows_lock.acquire()
            self._windows_lock = windows_lock
            return self
        self._path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(self._path, os.O_CREAT | os.O_RDWR, 0o600)
        fcntl = importlib.import_module("fcntl")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            os.close(fd)
            raise WriterBusyError("another GoodJob writer currently owns the lock") from exc
        self._fd = fd
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self._windows_lock is not None:
            self._windows_lock.release()
            self._windows_lock = None
            return
        if self._fd is not None:
            fcntl = importlib.import_module("fcntl")
            fcntl.flock(self._fd, fcntl.LOCK_UN)
            os.close(self._fd)
            self._fd = None

"""Explicit ownership for native Windows handles."""

from __future__ import annotations

import ctypes
import sys
from collections.abc import Callable
from types import TracebackType
from typing import Any, Self


def require_windows() -> None:
    """Reject native API use on non-Windows hosts while keeping imports portable."""
    if sys.platform != "win32":
        raise OSError("the native Windows backend is available only on Windows")


def load_windows_dll(name: str) -> Any:
    """Load one system DLL only after the caller has selected the Windows backend."""
    require_windows()
    loader = getattr(ctypes, "WinDLL", None)
    if loader is None:
        raise OSError("ctypes WinDLL support is unavailable")
    return loader(name, use_last_error=True)


def last_error() -> int:
    getter = getattr(ctypes, "get_last_error", None)
    return int(getter()) if getter is not None else 0


def close_win32_handle(value: int) -> None:
    if value == 0:
        return
    kernel32 = load_windows_dll("kernel32.dll")
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = ctypes.c_int
    if not kernel32.CloseHandle(ctypes.c_void_p(value)):
        raise OSError(last_error(), "CloseHandle")


class OwnedHandle:
    """A move-only-by-convention native handle with deterministic close semantics."""

    __slots__ = ("_closer", "_value")

    def __init__(self, value: int, *, closer: Callable[[int], None] = close_win32_handle) -> None:
        if value == 0:
            raise ValueError("owned handle must be non-zero")
        self._value = value
        self._closer = closer

    @property
    def value(self) -> int:
        if self._value == 0:
            raise OSError("native handle ownership has already moved or closed")
        return self._value

    @property
    def closed(self) -> bool:
        return self._value == 0

    def detach(self) -> int:
        value = self.value
        self._value = 0
        return value

    def close(self) -> None:
        if self._value == 0:
            return
        value = self._value
        self._value = 0
        self._closer(value)

    def __enter__(self) -> Self:
        _ = self.value
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

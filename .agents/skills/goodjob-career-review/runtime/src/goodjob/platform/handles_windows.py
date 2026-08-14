"""Explicit ownership for native Windows handles."""

from __future__ import annotations

import ctypes
import importlib
import os
import sys
import threading
from collections.abc import Callable, Iterable
from types import TracebackType
from typing import Any, Protocol, Self


class RetryableOwner(Protocol):
    @property
    def closed(self) -> bool: ...

    def close(self) -> None: ...

    def retry_close(self) -> None: ...


_RETAINED_OWNERS: list[RetryableOwner] = []
_RETAINED_OWNERS_LOCK = threading.RLock()


def _retain_owner(owner: RetryableOwner) -> None:
    with _RETAINED_OWNERS_LOCK:
        if owner.closed:
            return
        if not any(retained is owner for retained in _RETAINED_OWNERS):
            _RETAINED_OWNERS.append(owner)


def _release_owner(owner: RetryableOwner) -> None:
    with _RETAINED_OWNERS_LOCK:
        _RETAINED_OWNERS[:] = [retained for retained in _RETAINED_OWNERS if retained is not owner]


def close_owned_resources(
    owners: Iterable[RetryableOwner | None], *, cause: BaseException | None = None
) -> None:
    """Close every owner and retain failures without abandoning later resources."""
    first_error: OSError | None = None
    for owner in owners:
        if owner is None or owner.closed:
            continue
        try:
            owner.close()
        except OSError as exc:
            _retain_owner(owner)
            if first_error is None:
                first_error = exc
    if first_error is not None:
        if cause is not None:
            raise first_error from cause
        raise first_error


def retry_retained_owners() -> None:
    """Retry every failed close before another protected Windows operation."""
    with _RETAINED_OWNERS_LOCK:
        retained = tuple(_RETAINED_OWNERS)
        first_error: OSError | None = None
        for owner in retained:
            if owner.closed:
                _release_owner(owner)
                continue
            try:
                owner.retry_close()
            except OSError as exc:
                if first_error is None:
                    first_error = exc
        incomplete = bool(_RETAINED_OWNERS)
        if incomplete:
            raise OSError("previous Windows owner cleanup remains incomplete") from first_error


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


def write_all_handle(value: int, content: bytes, *, chunk_size: int = 64 * 1024) -> None:
    """Write all bytes to one borrowed Win32 handle without taking ownership."""
    if value == 0 or chunk_size <= 0:
        raise ValueError("Win32 write requires a valid handle and positive chunk size")
    kernel32 = load_windows_dll("kernel32.dll")
    kernel32.WriteFile.argtypes = [
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.POINTER(ctypes.c_uint32),
        ctypes.c_void_p,
    ]
    kernel32.WriteFile.restype = ctypes.c_int
    remaining = memoryview(content)
    while remaining:
        chunk = bytes(remaining[:chunk_size])
        buffer = ctypes.create_string_buffer(chunk)
        written = ctypes.c_uint32()
        if not kernel32.WriteFile(
            ctypes.c_void_p(value), buffer, len(chunk), ctypes.byref(written), None
        ):
            raise OSError(last_error(), "WriteFile")
        if written.value == 0:
            raise OSError("Win32 handle write made no progress")
        remaining = remaining[written.value :]


def transfer_handle_to_crt_descriptor(
    handle: OwnedHandle, flags: int = os.O_RDONLY
) -> OwnedCrtDescriptor:
    """Move a HANDLE to the CRT only after open_osfhandle confirms ownership transfer."""
    msvcrt = importlib.import_module("msvcrt")
    try:
        descriptor = int(msvcrt.open_osfhandle(handle.value, flags))
        if descriptor < 0:
            raise OSError("open_osfhandle returned an invalid descriptor")
    except BaseException as primary_error:
        close_owned_resources((handle,), cause=primary_error)
        raise
    owner = OwnedCrtDescriptor(descriptor)
    handle.detach()
    return owner


_OWNER_OPEN = 0
_OWNER_CLOSING = 1
_OWNER_CLOSE_FAILED = 2
_OWNER_CLOSED = 3


class _OwnedValueState:
    """Serialize one resource value and distinguish close from explicit retry."""

    __slots__ = (
        "_close_error",
        "_close_started_message",
        "_closer",
        "_invalid_value",
        "_lock",
        "_moved_message",
        "_status",
        "_value",
    )

    def __init__(
        self,
        value: int,
        *,
        invalid_value: int,
        closer: Callable[[int], None],
        moved_message: str,
        close_started_message: str,
    ) -> None:
        self._value = value
        self._invalid_value = invalid_value
        self._closer = closer
        self._moved_message = moved_message
        self._close_started_message = close_started_message
        self._lock = threading.Lock()
        self._status = _OWNER_OPEN
        self._close_error: BaseException | None = None

    @property
    def value(self) -> int:
        with self._lock:
            if self._status == _OWNER_CLOSED:
                raise OSError(self._moved_message)
            return self._value

    @property
    def closed(self) -> bool:
        with self._lock:
            return self._status == _OWNER_CLOSED

    def detach(self) -> int:
        with self._lock:
            if self._status == _OWNER_CLOSED:
                raise OSError(self._moved_message)
            if self._status != _OWNER_OPEN:
                raise OSError(self._close_started_message)
            value = self._value
            self._value = self._invalid_value
            self._status = _OWNER_CLOSED
            return value

    def close(self, *, retry: bool) -> None:
        with self._lock:
            if self._status == _OWNER_CLOSED:
                return
            if self._status == _OWNER_CLOSE_FAILED and not retry:
                assert self._close_error is not None
                raise self._close_error
            self._status = _OWNER_CLOSING
            try:
                self._closer(self._value)
            except BaseException as exc:
                self._close_error = exc
                self._status = _OWNER_CLOSE_FAILED
                raise
            self._value = self._invalid_value
            self._close_error = None
            self._status = _OWNER_CLOSED


class _OwnedResource:
    __slots__ = ("_state",)

    def __init__(self, state: _OwnedValueState) -> None:
        self._state = state

    @property
    def value(self) -> int:
        return self._state.value

    @property
    def closed(self) -> bool:
        return self._state.closed

    def detach(self) -> int:
        value = self._state.detach()
        _release_owner(self)
        return value

    def close(self) -> None:
        self._state.close(retry=False)
        _release_owner(self)

    def retry_close(self) -> None:
        self._state.close(retry=True)
        _release_owner(self)

    def __enter__(self) -> Self:
        _ = self.value
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        close_owned_resources((self,))


class OwnedCrtDescriptor(_OwnedResource):
    """A CRT descriptor owner that remains retryable when close itself fails."""

    def __init__(self, value: int, *, closer: Callable[[int], None] | None = None) -> None:
        if value < 0:
            raise ValueError("owned CRT descriptor must be non-negative")
        super().__init__(
            _OwnedValueState(
                value,
                invalid_value=-1,
                closer=os.close if closer is None else closer,
                moved_message="CRT descriptor ownership has already moved or closed",
                close_started_message="CRT descriptor close has already started",
            )
        )


class OwnedHandle(_OwnedResource):
    """A move-only-by-convention native handle with deterministic close semantics."""

    def __init__(self, value: int, *, closer: Callable[[int], None] = close_win32_handle) -> None:
        if value == 0:
            raise ValueError("owned handle must be non-zero")
        super().__init__(
            _OwnedValueState(
                value,
                invalid_value=0,
                closer=closer,
                moved_message="native handle ownership has already moved or closed",
                close_started_message="native handle close has already started",
            )
        )

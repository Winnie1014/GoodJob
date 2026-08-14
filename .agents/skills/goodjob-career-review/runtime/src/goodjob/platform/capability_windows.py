"""Allowlisted Windows HANDLE transfer for private child inputs."""

from __future__ import annotations

import ctypes
import importlib
import os
from dataclasses import dataclass

from goodjob.errors import CapabilityError
from goodjob.platform.handles_windows import OwnedHandle, last_error, load_windows_dll

HANDLE_FLAG_INHERIT = 0x00000001
MAX_TRANSFER_BYTES = 2 * 1024 * 1024


class SECURITY_ATTRIBUTES(ctypes.Structure):
    _fields_ = [
        ("nLength", ctypes.c_uint32),
        ("lpSecurityDescriptor", ctypes.c_void_p),
        ("bInheritHandle", ctypes.c_int),
    ]


@dataclass
class WindowsTransferPipe:
    """Parent writes; the allowlisted child inherits and owns the read side."""

    child_read: OwnedHandle
    parent_write: OwnedHandle

    @classmethod
    def create(cls) -> WindowsTransferPipe:
        kernel32 = load_windows_dll("kernel32.dll")
        kernel32.CreatePipe.argtypes = [
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(SECURITY_ATTRIBUTES),
            ctypes.c_uint32,
        ]
        kernel32.CreatePipe.restype = ctypes.c_int
        kernel32.SetHandleInformation.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32]
        kernel32.SetHandleInformation.restype = ctypes.c_int
        read_handle = ctypes.c_void_p()
        write_handle = ctypes.c_void_p()
        attributes = SECURITY_ATTRIBUTES(ctypes.sizeof(SECURITY_ATTRIBUTES), None, True)
        if not kernel32.CreatePipe(
            ctypes.byref(read_handle), ctypes.byref(write_handle), ctypes.byref(attributes), 0
        ):
            raise OSError(last_error(), "CreatePipe")
        read = OwnedHandle(int(read_handle.value or 0))
        write = OwnedHandle(int(write_handle.value or 0))
        try:
            if not kernel32.SetHandleInformation(
                ctypes.c_void_p(write.value), HANDLE_FLAG_INHERIT, 0
            ):
                raise OSError(last_error(), "SetHandleInformation")
            return cls(read, write)
        except BaseException:
            write.close()
            read.close()
            raise

    def close(self) -> None:
        first_error: OSError | None = None
        for handle in (self.parent_write, self.child_read):
            try:
                handle.close()
            except OSError as exc:
                if first_error is None:
                    first_error = exc
        if first_error is not None:
            raise first_error


def write_handle(handle: int, content: bytes) -> None:
    if len(content) > MAX_TRANSFER_BYTES:
        raise OSError("protected input exceeded the Windows transfer limit")
    kernel32 = load_windows_dll("kernel32.dll")
    kernel32.WriteFile.argtypes = [
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.POINTER(ctypes.c_uint32),
        ctypes.c_void_p,
    ]
    kernel32.WriteFile.restype = ctypes.c_int
    view = memoryview(content)
    while view:
        chunk = bytes(view[:4096])
        buffer = ctypes.create_string_buffer(chunk)
        written = ctypes.c_uint32()
        if not kernel32.WriteFile(
            ctypes.c_void_p(handle), buffer, len(chunk), ctypes.byref(written), None
        ):
            raise OSError(last_error(), "WriteFile")
        if written.value == 0:
            raise OSError("protected Windows pipe write made no progress")
        view = view[written.value :]


def read_bytes_from_handle(handle: int, *, maximum_bytes: int) -> bytes:
    """Take ownership of a Win32 HANDLE by converting it to one CRT descriptor."""
    if handle <= 0:
        raise CapabilityError("protected input handle must be positive")
    msvcrt = importlib.import_module("msvcrt")
    try:
        descriptor = int(msvcrt.open_osfhandle(handle, os.O_RDONLY))
    except OSError as exc:
        raise CapabilityError("unable to take ownership of protected input handle") from exc
    chunks: list[bytes] = []
    total = 0
    try:
        while True:
            chunk = os.read(descriptor, min(4096, maximum_bytes + 1 - total))
            if not chunk:
                break
            total += len(chunk)
            if total > maximum_bytes:
                raise CapabilityError("protected input exceeded its bounded size")
            chunks.append(chunk)
    except OSError as exc:
        raise CapabilityError("unable to read protected input handle") from exc
    finally:
        os.close(descriptor)
    return b"".join(chunks)

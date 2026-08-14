"""Allowlisted Windows HANDLE transfer for private child inputs."""

from __future__ import annotations

import ctypes
import os
from dataclasses import dataclass

from goodjob.errors import CapabilityError
from goodjob.platform.handles_windows import (
    OwnedCrtDescriptor,
    OwnedHandle,
    close_owned_resources,
    last_error,
    load_windows_dll,
    retry_retained_owners,
    transfer_handle_to_crt_descriptor,
    write_all_handle,
)

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
        retry_retained_owners()
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
        read_value = int(read_handle.value or 0)
        write_value = int(write_handle.value or 0)
        if read_value == 0 or write_value == 0:
            owners = tuple(OwnedHandle(value) for value in (write_value, read_value) if value)
            primary_error = OSError("CreatePipe returned an invalid handle pair")
            close_owned_resources(owners, cause=primary_error)
            raise primary_error
        read = OwnedHandle(read_value)
        write = OwnedHandle(write_value)
        try:
            if not kernel32.SetHandleInformation(
                ctypes.c_void_p(write.value), HANDLE_FLAG_INHERIT, 0
            ):
                raise OSError(last_error(), "SetHandleInformation")
            return cls(read, write)
        except BaseException as primary_error:
            close_owned_resources((write, read), cause=primary_error)
            raise

    def close(self) -> None:
        close_owned_resources((self.parent_write, self.child_read))


def write_handle(handle: int, content: bytes) -> None:
    if len(content) > MAX_TRANSFER_BYTES:
        raise OSError("protected input exceeded the Windows transfer limit")
    write_all_handle(handle, content, chunk_size=4096)


def _close_descriptor(
    descriptor: OwnedCrtDescriptor, *, cause: BaseException | None = None
) -> None:
    try:
        close_owned_resources((descriptor,), cause=cause)
    except OSError as cleanup_error:
        raise CapabilityError("unable to close protected input handle") from cleanup_error


def read_bytes_from_handle(handle: int, *, maximum_bytes: int) -> bytes:
    """Take ownership of a Win32 HANDLE by converting it to one CRT descriptor."""
    retry_retained_owners()
    if handle <= 0:
        raise CapabilityError("protected input handle must be positive")
    owner = OwnedHandle(handle)
    try:
        descriptor = transfer_handle_to_crt_descriptor(owner, os.O_RDONLY)
    except OSError as exc:
        raise CapabilityError("unable to take ownership of protected input handle") from exc
    chunks: list[bytes] = []
    total = 0
    try:
        while True:
            chunk = os.read(descriptor.value, min(4096, maximum_bytes + 1 - total))
            if not chunk:
                break
            total += len(chunk)
            if total > maximum_bytes:
                raise CapabilityError("protected input exceeded its bounded size")
            chunks.append(chunk)
    except CapabilityError as primary_error:
        _close_descriptor(descriptor, cause=primary_error)
        raise
    except OSError as primary_error:
        public_error = CapabilityError("unable to read protected input handle")
        _close_descriptor(descriptor, cause=primary_error)
        raise public_error from primary_error
    except BaseException as primary_error:
        close_owned_resources((descriptor,), cause=primary_error)
        raise
    _close_descriptor(descriptor)
    return b"".join(chunks)

"""Windows process identity based on kernel creation time."""

from __future__ import annotations

import ctypes

from goodjob.platform.handles_windows import OwnedHandle, last_error, load_windows_dll

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
SYNCHRONIZE = 0x00100000
ERROR_ACCESS_DENIED = 5
ERROR_INVALID_PARAMETER = 87


class FILETIME(ctypes.Structure):
    _fields_ = [("dwLowDateTime", ctypes.c_uint32), ("dwHighDateTime", ctypes.c_uint32)]

    def as_uint64(self) -> int:
        return (int(self.dwHighDateTime) << 32) | int(self.dwLowDateTime)


def _open_process(pid: int, access: int) -> OwnedHandle | None:
    if pid <= 0:
        return None
    kernel32 = load_windows_dll("kernel32.dll")
    kernel32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    kernel32.OpenProcess.restype = ctypes.c_void_p
    raw = kernel32.OpenProcess(access, False, pid)
    if raw:
        return OwnedHandle(int(raw))
    error = last_error()
    if error == ERROR_ACCESS_DENIED:
        raise PermissionError(error, "OpenProcess")
    if error == ERROR_INVALID_PARAMETER:
        return None
    raise OSError(error, "OpenProcess")


def process_start_marker(pid: int) -> str | None:
    """Return the immutable FILETIME creation marker for one live process."""
    try:
        process = _open_process(pid, PROCESS_QUERY_LIMITED_INFORMATION)
    except PermissionError:
        return None
    if process is None:
        return None
    with process:
        kernel32 = load_windows_dll("kernel32.dll")
        kernel32.GetProcessTimes.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(FILETIME),
            ctypes.POINTER(FILETIME),
            ctypes.POINTER(FILETIME),
            ctypes.POINTER(FILETIME),
        ]
        kernel32.GetProcessTimes.restype = ctypes.c_int
        created = FILETIME()
        exited = FILETIME()
        kernel = FILETIME()
        user = FILETIME()
        if not kernel32.GetProcessTimes(
            ctypes.c_void_p(process.value),
            ctypes.byref(created),
            ctypes.byref(exited),
            ctypes.byref(kernel),
            ctypes.byref(user),
        ):
            error = last_error()
            if error == ERROR_ACCESS_DENIED:
                return None
            raise OSError(error, "GetProcessTimes")
        return str(created.as_uint64())


def process_exists(pid: int) -> bool:
    """Return false only when OpenProcess proves that the PID is absent."""
    try:
        process = _open_process(pid, SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION)
    except PermissionError:
        return True
    if process is None:
        return False
    process.close()
    return True

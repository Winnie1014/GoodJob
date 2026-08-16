from __future__ import annotations

import ctypes
from typing import Any

from goodjob.platform.process_windows import FILETIME


class FakeProcessCall:
    argtypes: object
    restype: object

    def __init__(self, callback: Any) -> None:
        self._callback = callback

    def __call__(self, *args: Any) -> Any:
        return self._callback(*args)


class FakeProcessApi:
    def __init__(
        self,
        wait_result: int,
        *,
        creation_marker: int = 100,
        open_result: int = 701,
    ) -> None:
        self.closed: list[int] = []
        self.creation_marker = creation_marker
        self.open_calls: list[tuple[int, int]] = []
        self.open_result = open_result
        self.wait_result = wait_result
        self.wait_calls: list[tuple[int, int]] = []
        self.GetProcessTimes = FakeProcessCall(self._get_process_times)
        self.OpenProcess = FakeProcessCall(self._open_process)
        self.WaitForSingleObject = FakeProcessCall(self._wait_for_single_object)

    def _open_process(self, access: int, _inherit: int, pid: int) -> int:
        self.open_calls.append((access, pid))
        return self.open_result

    def _get_process_times(
        self,
        _handle: Any,
        created: Any,
        _exited: Any,
        _kernel: Any,
        _user: Any,
    ) -> int:
        value = self.creation_marker
        output = ctypes.cast(created, ctypes.POINTER(FILETIME))
        output.contents.dwLowDateTime = value & 0xFFFFFFFF
        output.contents.dwHighDateTime = value >> 32
        return 1

    def _wait_for_single_object(self, handle: Any, timeout: int) -> int:
        self.wait_calls.append((int(handle.value), timeout))
        return self.wait_result

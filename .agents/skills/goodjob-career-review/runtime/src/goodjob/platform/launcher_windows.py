"""Direct CreateProcessW launcher with Job containment and bounded output."""

from __future__ import annotations

import ctypes
import queue
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from typing import Any, Protocol

from goodjob.platform.capability_windows import (
    SECURITY_ATTRIBUTES,
    WindowsTransferPipe,
    write_handle,
)
from goodjob.platform.handles_windows import OwnedHandle, last_error, load_windows_dll

CREATE_SUSPENDED = 0x00000004
CREATE_UNICODE_ENVIRONMENT = 0x00000400
EXTENDED_STARTUPINFO_PRESENT = 0x00080000
CREATE_NO_WINDOW = 0x08000000
STARTF_USESTDHANDLES = 0x00000100
HANDLE_FLAG_INHERIT = 0x00000001
PROC_THREAD_ATTRIBUTE_HANDLE_LIST = 0x00020002
JOB_OBJECT_LIMIT_ACTIVE_PROCESS = 0x00000008
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS = 9
WAIT_OBJECT_0 = 0
WAIT_TIMEOUT = 258
STILL_ACTIVE = 259
INFINITE = 0xFFFFFFFF
INVALID_RESUME_RESULT = 0xFFFFFFFF
ERROR_BROKEN_PIPE = 109


class STARTUPINFOW(ctypes.Structure):
    _fields_ = [
        ("cb", ctypes.c_uint32),
        ("lpReserved", ctypes.c_wchar_p),
        ("lpDesktop", ctypes.c_wchar_p),
        ("lpTitle", ctypes.c_wchar_p),
        ("dwX", ctypes.c_uint32),
        ("dwY", ctypes.c_uint32),
        ("dwXSize", ctypes.c_uint32),
        ("dwYSize", ctypes.c_uint32),
        ("dwXCountChars", ctypes.c_uint32),
        ("dwYCountChars", ctypes.c_uint32),
        ("dwFillAttribute", ctypes.c_uint32),
        ("dwFlags", ctypes.c_uint32),
        ("wShowWindow", ctypes.c_uint16),
        ("cbReserved2", ctypes.c_uint16),
        ("lpReserved2", ctypes.POINTER(ctypes.c_ubyte)),
        ("hStdInput", ctypes.c_void_p),
        ("hStdOutput", ctypes.c_void_p),
        ("hStdError", ctypes.c_void_p),
    ]


class STARTUPINFOEXW(ctypes.Structure):
    _fields_ = [("StartupInfo", STARTUPINFOW), ("lpAttributeList", ctypes.c_void_p)]


class PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("hProcess", ctypes.c_void_p),
        ("hThread", ctypes.c_void_p),
        ("dwProcessId", ctypes.c_uint32),
        ("dwThreadId", ctypes.c_uint32),
    ]


class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", ctypes.c_uint32),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", ctypes.c_uint32),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", ctypes.c_uint32),
        ("SchedulingClass", ctypes.c_uint32),
    ]


class IO_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_uint64),
        ("WriteOperationCount", ctypes.c_uint64),
        ("OtherOperationCount", ctypes.c_uint64),
        ("ReadTransferCount", ctypes.c_uint64),
        ("WriteTransferCount", ctypes.c_uint64),
        ("OtherTransferCount", ctypes.c_uint64),
    ]


class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ("IoInfo", IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class NetworkGuard(Protocol):
    verified: bool

    def close(self) -> None: ...


@dataclass
class ProtectedInput:
    option: str
    content: bytes
    pipe: WindowsTransferPipe | None = None


@dataclass(frozen=True)
class WindowsLaunchResult:
    args: tuple[str, ...]
    returncode: int
    stdout: bytes
    stderr: bytes


@dataclass(frozen=True)
class WindowsLaunchRequest:
    application: str
    arguments: tuple[str, ...]
    cwd: str
    environment: Mapping[str, str]
    maximum_output_bytes: int
    timeout_seconds: float
    active_process_limit: int | None = None


class _OutputBudget:
    def __init__(self, maximum: int) -> None:
        if maximum <= 0:
            raise ValueError("Windows output budget must be positive")
        self._maximum = maximum
        self._used = 0
        self._lock = threading.Lock()
        self.exceeded = threading.Event()

    def reserve(self, requested: int) -> int:
        with self._lock:
            remaining = self._maximum - self._used
            allowed = min(requested, max(0, remaining))
            self._used += allowed
            if allowed != requested:
                self.exceeded.set()
            return allowed


class _AttributeList:
    def __init__(self, handles: Sequence[int]) -> None:
        if not handles:
            raise ValueError("Windows inherited handle allowlist must not be empty")
        kernel32 = load_windows_dll("kernel32.dll")
        kernel32.InitializeProcThreadAttributeList.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.POINTER(ctypes.c_size_t),
        ]
        kernel32.InitializeProcThreadAttributeList.restype = ctypes.c_int
        kernel32.UpdateProcThreadAttribute.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_size_t,
            ctypes.c_void_p,
            ctypes.c_size_t,
            ctypes.c_void_p,
            ctypes.c_void_p,
        ]
        kernel32.UpdateProcThreadAttribute.restype = ctypes.c_int
        size = ctypes.c_size_t()
        kernel32.InitializeProcThreadAttributeList(None, 1, 0, ctypes.byref(size))
        if size.value == 0:
            raise OSError(last_error(), "InitializeProcThreadAttributeList(size)")
        self._storage = ctypes.create_string_buffer(size.value)
        self.pointer = ctypes.cast(self._storage, ctypes.c_void_p)
        if not kernel32.InitializeProcThreadAttributeList(self.pointer, 1, 0, ctypes.byref(size)):
            raise OSError(last_error(), "InitializeProcThreadAttributeList")
        self._handles = (ctypes.c_void_p * len(handles))(*handles)
        if not kernel32.UpdateProcThreadAttribute(
            self.pointer,
            0,
            PROC_THREAD_ATTRIBUTE_HANDLE_LIST,
            ctypes.cast(self._handles, ctypes.c_void_p),
            ctypes.sizeof(self._handles),
            None,
            None,
        ):
            self.close()
            raise OSError(last_error(), "UpdateProcThreadAttribute")

    def close(self) -> None:
        if not self.pointer:
            return
        kernel32 = load_windows_dll("kernel32.dll")
        kernel32.DeleteProcThreadAttributeList.argtypes = [ctypes.c_void_p]
        kernel32.DeleteProcThreadAttributeList.restype = None
        kernel32.DeleteProcThreadAttributeList(self.pointer)
        self.pointer = ctypes.c_void_p()


def _kernel32() -> Any:
    api = load_windows_dll("kernel32.dll")
    api.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
    api.CreateJobObjectW.restype = ctypes.c_void_p
    api.SetInformationJobObject.argtypes = [
        ctypes.c_void_p,
        ctypes.c_int,
        ctypes.c_void_p,
        ctypes.c_uint32,
    ]
    api.SetInformationJobObject.restype = ctypes.c_int
    api.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    api.AssignProcessToJobObject.restype = ctypes.c_int
    api.TerminateJobObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    api.TerminateJobObject.restype = ctypes.c_int
    api.TerminateProcess.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    api.TerminateProcess.restype = ctypes.c_int
    api.CreateProcessW.argtypes = [
        ctypes.c_wchar_p,
        ctypes.c_wchar_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_int,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_wchar_p,
        ctypes.POINTER(STARTUPINFOEXW),
        ctypes.POINTER(PROCESS_INFORMATION),
    ]
    api.CreateProcessW.restype = ctypes.c_int
    api.ResumeThread.argtypes = [ctypes.c_void_p]
    api.ResumeThread.restype = ctypes.c_uint32
    api.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    api.WaitForSingleObject.restype = ctypes.c_uint32
    api.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
    api.GetExitCodeProcess.restype = ctypes.c_int
    api.CreatePipe.argtypes = [
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(SECURITY_ATTRIBUTES),
        ctypes.c_uint32,
    ]
    api.CreatePipe.restype = ctypes.c_int
    api.SetHandleInformation.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32]
    api.SetHandleInformation.restype = ctypes.c_int
    api.ReadFile.argtypes = [
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.POINTER(ctypes.c_uint32),
        ctypes.c_void_p,
    ]
    api.ReadFile.restype = ctypes.c_int
    return api


def _create_output_pipe(api: Any) -> tuple[OwnedHandle, OwnedHandle]:
    read_raw = ctypes.c_void_p()
    write_raw = ctypes.c_void_p()
    attributes = SECURITY_ATTRIBUTES(ctypes.sizeof(SECURITY_ATTRIBUTES), None, True)
    if not api.CreatePipe(
        ctypes.byref(read_raw), ctypes.byref(write_raw), ctypes.byref(attributes), 0
    ):
        raise OSError(last_error(), "CreatePipe")
    read_handle = OwnedHandle(int(read_raw.value or 0))
    write_handle = OwnedHandle(int(write_raw.value or 0))
    try:
        if not api.SetHandleInformation(ctypes.c_void_p(read_handle.value), HANDLE_FLAG_INHERIT, 0):
            raise OSError(last_error(), "SetHandleInformation")
        return read_handle, write_handle
    except BaseException:
        write_handle.close()
        read_handle.close()
        raise


def _create_job(api: Any, active_process_limit: int | None) -> OwnedHandle:
    raw = api.CreateJobObjectW(None, None)
    if not raw:
        raise OSError(last_error(), "CreateJobObjectW")
    job = OwnedHandle(int(raw))
    limits = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
    limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if active_process_limit is not None:
        if active_process_limit <= 0:
            job.close()
            raise ValueError("active process limit must be positive")
        limits.BasicLimitInformation.LimitFlags |= JOB_OBJECT_LIMIT_ACTIVE_PROCESS
        limits.BasicLimitInformation.ActiveProcessLimit = active_process_limit
    if not api.SetInformationJobObject(
        ctypes.c_void_p(job.value),
        JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS,
        ctypes.byref(limits),
        ctypes.sizeof(limits),
    ):
        error = last_error()
        job.close()
        raise OSError(error, "SetInformationJobObject")
    return job


def _environment_block(environment: Mapping[str, str]) -> ctypes.Array[ctypes.c_wchar]:
    entries: list[str] = []
    for key, value in sorted(environment.items(), key=lambda item: item[0].upper()):
        if not key or "=" in key or "\0" in key or "\0" in value:
            raise ValueError("Windows child environment contains an invalid entry")
        entries.append(f"{key}={value}")
    return ctypes.create_unicode_buffer("\0".join(entries) + "\0\0")


def _reader(
    api: Any,
    handle: int,
    stream_name: str,
    output: queue.Queue[tuple[str, bytes] | tuple[str, BaseException] | tuple[str, None]],
    budget: _OutputBudget,
    stopping: threading.Event,
) -> None:
    try:
        buffer = ctypes.create_string_buffer(64 * 1024)
        while True:
            count = ctypes.c_uint32()
            ok = api.ReadFile(
                ctypes.c_void_p(handle), buffer, len(buffer), ctypes.byref(count), None
            )
            if count.value:
                chunk = bytes(buffer.raw[: count.value])
                allowed = budget.reserve(len(chunk))
                if allowed:
                    _put_output(output, (stream_name, chunk[:allowed]), stopping)
            if not ok:
                error = last_error()
                if error != ERROR_BROKEN_PIPE:
                    raise OSError(error, "ReadFile")
                break
    except BaseException as exc:
        _put_output(output, ("error", exc), stopping)
    finally:
        _put_output(output, (stream_name, None), stopping)


def _put_output(
    output: queue.Queue[tuple[str, bytes] | tuple[str, BaseException] | tuple[str, None]],
    item: tuple[str, bytes] | tuple[str, BaseException] | tuple[str, None],
    stopping: threading.Event,
) -> None:
    while not stopping.is_set():
        try:
            output.put(item, timeout=0.05)
            return
        except queue.Full:
            continue


def _emit(observer: Callable[[str], None] | None, event: str) -> None:
    if observer is not None:
        with suppress(Exception):
            observer(event)


def run_windows_process(
    request: WindowsLaunchRequest,
    *,
    inputs: Sequence[ProtectedInput] = (),
    network_guard: NetworkGuard | None = None,
    observer: Callable[[str], None] | None = None,
) -> WindowsLaunchResult:
    """Launch one child with no execution window before Job containment."""
    if network_guard is not None and not network_guard.verified:
        network_guard.close()
        raise OSError("Windows network filters were not fully read back")
    _emit(observer, "boundary_verified")
    try:
        api = _kernel32()
    except BaseException:
        if network_guard is not None:
            with suppress(OSError):
                network_guard.close()
            _emit(observer, "network_guard_closed")
        raise
    job: OwnedHandle | None = None
    process: OwnedHandle | None = None
    thread: OwnedHandle | None = None
    stdout_read: OwnedHandle | None = None
    stdout_write: OwnedHandle | None = None
    stderr_read: OwnedHandle | None = None
    stderr_write: OwnedHandle | None = None
    stdin_pipe: WindowsTransferPipe | None = None
    attributes: _AttributeList | None = None
    readers: list[threading.Thread] = []
    writers: list[threading.Thread] = []
    writer_errors: list[BaseException] = []
    writer_errors_lock = threading.Lock()
    stopping = threading.Event()
    created = False
    terminated = False
    process_stopped = False
    first_cleanup_error: OSError | None = None
    try:
        job = _create_job(api, request.active_process_limit)
        stdout_read, stdout_write = _create_output_pipe(api)
        stderr_read, stderr_write = _create_output_pipe(api)
        stdin_pipe = WindowsTransferPipe.create()
        prepared_inputs: list[ProtectedInput] = []
        for transfer in inputs:
            transfer.pipe = WindowsTransferPipe.create()
            prepared_inputs.append(transfer)
        inherited = [
            stdin_pipe.child_read.value,
            stdout_write.value,
            stderr_write.value,
            *(transfer.pipe.child_read.value for transfer in prepared_inputs if transfer.pipe),
        ]
        attributes = _AttributeList(inherited)
        startup = STARTUPINFOEXW()
        startup.StartupInfo.cb = ctypes.sizeof(startup)
        startup.StartupInfo.dwFlags = STARTF_USESTDHANDLES
        startup.StartupInfo.hStdInput = ctypes.c_void_p(stdin_pipe.child_read.value)
        startup.StartupInfo.hStdOutput = ctypes.c_void_p(stdout_write.value)
        startup.StartupInfo.hStdError = ctypes.c_void_p(stderr_write.value)
        startup.lpAttributeList = attributes.pointer
        command_arguments = [request.application, *request.arguments]
        for transfer in prepared_inputs:
            assert transfer.pipe is not None
            command_arguments.extend([transfer.option, str(transfer.pipe.child_read.value)])
        command_line = ctypes.create_unicode_buffer(subprocess.list2cmdline(command_arguments))
        environment = _environment_block(request.environment)
        process_info = PROCESS_INFORMATION()
        flags = (
            EXTENDED_STARTUPINFO_PRESENT
            | CREATE_SUSPENDED
            | CREATE_UNICODE_ENVIRONMENT
            | CREATE_NO_WINDOW
        )
        if not api.CreateProcessW(
            request.application,
            command_line,
            None,
            None,
            True,
            flags,
            ctypes.cast(environment, ctypes.c_void_p),
            request.cwd,
            ctypes.byref(startup),
            ctypes.byref(process_info),
        ):
            raise OSError(last_error(), "CreateProcessW")
        created = True
        process = OwnedHandle(int(process_info.hProcess))
        thread = OwnedHandle(int(process_info.hThread))
        _emit(observer, "created_suspended")
        if not api.AssignProcessToJobObject(
            ctypes.c_void_p(job.value), ctypes.c_void_p(process.value)
        ):
            raise OSError(last_error(), "AssignProcessToJobObject")
        _emit(observer, "assigned_job")
        for handle in (stdin_pipe.child_read, stdout_write, stderr_write):
            handle.close()
        for transfer in prepared_inputs:
            assert transfer.pipe is not None
            transfer.pipe.child_read.close()
        _emit(observer, "child_sides_closed")
        resumed = int(api.ResumeThread(ctypes.c_void_p(thread.value)))
        if resumed == INVALID_RESUME_RESULT:
            raise OSError(last_error(), "ResumeThread")
        _emit(observer, "resumed")
        thread.close()
        thread = None
        _emit(observer, "primary_thread_closed")
        stdin_pipe.parent_write.close()
        output_queue: queue.Queue[
            tuple[str, bytes] | tuple[str, BaseException] | tuple[str, None]
        ] = queue.Queue(maxsize=16)
        budget = _OutputBudget(request.maximum_output_bytes)
        for name, read_handle in (("stdout", stdout_read), ("stderr", stderr_read)):
            reader = threading.Thread(
                target=_reader,
                args=(api, read_handle.value, name, output_queue, budget, stopping),
                daemon=True,
            )
            reader.start()
            readers.append(reader)
        for transfer in prepared_inputs:
            assert transfer.pipe is not None

            def write_input(item: ProtectedInput = transfer) -> None:
                assert item.pipe is not None
                try:
                    write_handle(item.pipe.parent_write.value, item.content)
                except BaseException as exc:
                    with writer_errors_lock:
                        writer_errors.append(exc)
                    _put_output(output_queue, ("error", exc), stopping)
                finally:
                    try:
                        item.pipe.parent_write.close()
                    except BaseException as exc:
                        with writer_errors_lock:
                            writer_errors.append(exc)
                        _put_output(output_queue, ("error", exc), stopping)

            writer = threading.Thread(target=write_input, daemon=True)
            writer.start()
            writers.append(writer)
        outputs = {"stdout": bytearray(), "stderr": bytearray()}
        completed_streams: set[str] = set()
        deadline = time.monotonic() + request.timeout_seconds
        while True:
            while True:
                try:
                    stream, payload = output_queue.get_nowait()
                except queue.Empty:
                    break
                if stream == "error":
                    assert isinstance(payload, BaseException)
                    raise payload
                if payload is None:
                    completed_streams.add(stream)
                else:
                    assert isinstance(payload, bytes)
                    outputs[stream].extend(payload)
            if budget.exceeded.is_set():
                raise OSError("child process exceeded the shared bounded-output budget")
            wait = int(api.WaitForSingleObject(ctypes.c_void_p(process.value), 20))
            if wait == WAIT_OBJECT_0 and len(completed_streams) == 2:
                process_stopped = True
                break
            if wait not in {WAIT_OBJECT_0, WAIT_TIMEOUT}:
                raise OSError(last_error(), "WaitForSingleObject")
            if time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired(command_arguments, request.timeout_seconds)
        for writer in writers:
            writer.join(timeout=1.0)
            if writer.is_alive():
                raise OSError("protected input writer did not stop")
        with writer_errors_lock:
            if writer_errors:
                raise writer_errors[0]
        for reader in readers:
            reader.join(timeout=1.0)
            if reader.is_alive():
                raise OSError("bounded-output reader did not stop")
        exit_code = ctypes.c_uint32(STILL_ACTIVE)
        if not api.GetExitCodeProcess(ctypes.c_void_p(process.value), ctypes.byref(exit_code)):
            raise OSError(last_error(), "GetExitCodeProcess")
        if exit_code.value == STILL_ACTIVE:
            raise OSError("Windows child remained active after its wait completed")
        return WindowsLaunchResult(
            tuple(command_arguments),
            int(exit_code.value),
            bytes(outputs["stdout"]),
            bytes(outputs["stderr"]),
        )
    except BaseException:
        stopping.set()
        if created and job is not None and not job.closed:
            api.TerminateJobObject(ctypes.c_void_p(job.value), 1)
            terminated = True
            _emit(observer, "job_terminated")
            if process is not None and not process.closed:
                process_stopped = (
                    int(api.WaitForSingleObject(ctypes.c_void_p(process.value), 5000))
                    == WAIT_OBJECT_0
                )
        for cleanup_read_handle in (stdout_read, stderr_read):
            if cleanup_read_handle is not None:
                with suppress(OSError):
                    cleanup_read_handle.close()
        for reader in readers:
            reader.join(timeout=1.0)
        for transfer in inputs:
            if transfer.pipe is not None:
                with suppress(OSError):
                    transfer.pipe.parent_write.close()
        for writer in writers:
            writer.join(timeout=1.0)
        raise
    finally:
        if created and not terminated and process is not None and not process.closed:
            wait = int(api.WaitForSingleObject(ctypes.c_void_p(process.value), 0))
            process_stopped = wait == WAIT_OBJECT_0
            if not process_stopped and job is not None and not job.closed:
                api.TerminateJobObject(ctypes.c_void_p(job.value), 1)
                process_stopped = (
                    int(api.WaitForSingleObject(ctypes.c_void_p(process.value), 5000))
                    == WAIT_OBJECT_0
                )
        if created and not process_stopped and process is not None and not process.closed:
            api.TerminateProcess(ctypes.c_void_p(process.value), 1)
            process_stopped = (
                int(api.WaitForSingleObject(ctypes.c_void_p(process.value), 5000)) == WAIT_OBJECT_0
            )
        for resource_handle in (
            stdout_write,
            stderr_write,
            stdout_read,
            stderr_read,
            thread,
        ):
            if resource_handle is None:
                continue
            try:
                resource_handle.close()
            except OSError as exc:
                if first_cleanup_error is None:
                    first_cleanup_error = exc
        for transfer in inputs:
            if transfer.pipe is not None:
                try:
                    transfer.pipe.close()
                except OSError as exc:
                    if first_cleanup_error is None:
                        first_cleanup_error = exc
        if stdin_pipe is not None:
            try:
                stdin_pipe.close()
            except OSError as exc:
                if first_cleanup_error is None:
                    first_cleanup_error = exc
        if attributes is not None:
            try:
                attributes.close()
            except OSError as exc:
                if first_cleanup_error is None:
                    first_cleanup_error = exc
        if process_stopped and process is not None:
            try:
                process.close()
            except OSError as exc:
                if first_cleanup_error is None:
                    first_cleanup_error = exc
        if job is not None:
            try:
                job.close()
            except OSError as exc:
                if first_cleanup_error is None:
                    first_cleanup_error = exc
        _emit(observer, "job_closed")
        if not process_stopped and process is not None and not process.closed:
            process_stopped = (
                int(api.WaitForSingleObject(ctypes.c_void_p(process.value), 5000)) == WAIT_OBJECT_0
            )
            try:
                process.close()
            except OSError as exc:
                if first_cleanup_error is None:
                    first_cleanup_error = exc
        if created and not process_stopped and first_cleanup_error is None:
            first_cleanup_error = OSError(
                "Windows child exit could not be confirmed; retaining the network guard"
            )
        if network_guard is not None and (not created or process_stopped):
            try:
                network_guard.close()
            except OSError as exc:
                if first_cleanup_error is None:
                    first_cleanup_error = exc
            _emit(observer, "network_guard_closed")
        if first_cleanup_error is not None and sys.exception() is None:
            raise first_cleanup_error

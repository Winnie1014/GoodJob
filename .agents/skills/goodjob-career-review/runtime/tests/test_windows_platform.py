from __future__ import annotations

import ctypes
import inspect
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from goodjob.errors import InvalidInputError
from goodjob.git_metadata import GitMetadataReader, InternalGitBinding
from goodjob.platform import fs_windows, handles_windows, launcher_windows, sandbox_windows
from goodjob.platform.capability_windows import WindowsTransferPipe
from goodjob.platform.fs_windows import relative_components, validate_component
from goodjob.platform.handles_windows import OwnedHandle
from goodjob.platform.lock_windows import WindowsSharedFileLock
from goodjob.platform.process_windows import FILETIME


def test_owned_windows_handle_closes_exactly_once_and_invalidates_before_close() -> None:
    closed: list[int] = []
    handle = OwnedHandle(41, closer=closed.append)

    handle.close()
    handle.close()

    assert closed == [41]
    assert handle.closed
    with pytest.raises(OSError, match="already moved or closed"):
        _ = handle.value


def test_owned_windows_handle_detach_moves_close_authority() -> None:
    closed: list[int] = []
    handle = OwnedHandle(73, closer=closed.append)

    assert handle.detach() == 73
    handle.close()

    assert closed == []


def test_owned_windows_handle_retains_ownership_when_close_fails() -> None:
    attempts: list[int] = []

    def close(value: int) -> None:
        attempts.append(value)
        if len(attempts) == 1:
            raise OSError("injected close failure")

    handle = OwnedHandle(91, closer=close)

    with pytest.raises(OSError, match="injected close failure"):
        handle.close()

    assert not handle.closed
    assert handle.value == 91
    handle.close()
    assert handle.closed
    assert attempts == [91, 91]


def test_windows_write_all_handle_retries_partial_writes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    written_chunks: list[bytes] = []

    class WriteFileCall:
        argtypes: object
        restype: object

        def __call__(
            self, _handle: Any, buffer: Any, length: int, written: Any, _overlapped: Any
        ) -> int:
            count = min(int(length), 2)
            written_chunks.append(ctypes.string_at(buffer, count))
            ctypes.cast(written, ctypes.POINTER(ctypes.c_uint32))[0] = count
            return 1

    class FakeWriteApi:
        def __init__(self) -> None:
            self.WriteFile = WriteFileCall()

    monkeypatch.setattr(handles_windows, "load_windows_dll", lambda _name: FakeWriteApi())

    handles_windows.write_all_handle(42, b"abcdef", chunk_size=4)

    assert b"".join(written_chunks) == b"abcdef"


def test_windows_shared_file_lock_uses_nonblocking_read_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    operations: list[int] = []

    class FakeMsvcrt:
        LK_NBRLCK = 11
        LK_NBLCK = 12
        LK_UNLCK = 13

        @staticmethod
        def locking(_fd: int, mode: int, _length: int) -> None:
            operations.append(mode)

    monkeypatch.setitem(sys.modules, "msvcrt", FakeMsvcrt)
    lock = WindowsSharedFileLock(tmp_path / "shared.lock")

    lock.acquire()
    lock.release()

    assert operations == [FakeMsvcrt.LK_NBRLCK, FakeMsvcrt.LK_UNLCK]


@pytest.mark.parametrize(
    "component",
    [
        "",
        ".",
        "..",
        "C:",
        "C:escape",
        "child/name",
        "child\\name",
        "stream:secret",
        "\\\\server",
        "\\?\\device",
        "nul\0suffix",
        "a" * 256,
    ],
)
def test_windows_component_guard_rejects_every_forbidden_shape(component: str) -> None:
    with pytest.raises(InvalidInputError):
        validate_component(component)


def test_windows_component_guard_accepts_the_explicit_ntfs_boundary() -> None:
    assert validate_component("a" * 255) == "a" * 255
    assert validate_component("source.py") == "source.py"


@pytest.mark.parametrize("relative", ["/rooted", "\\rooted", "a//b", "a/../b", "a/./b"])
def test_windows_relative_path_rejects_rooted_empty_and_dot_components(relative: str) -> None:
    with pytest.raises(InvalidInputError):
        relative_components(relative)


def test_windows_output_budget_is_one_atomic_budget_across_both_streams() -> None:
    budget = launcher_windows._OutputBudget(100)
    allowed: list[int] = []

    def reserve() -> None:
        allowed.append(budget.reserve(60))

    threads = [threading.Thread(target=reserve) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(allowed) == [40, 60]
    assert budget.exceeded.is_set()


def test_windows_launcher_and_fs_do_not_contain_forbidden_fallbacks() -> None:
    launcher_source = inspect.getsource(launcher_windows)
    filesystem_source = inspect.getsource(fs_windows)

    assert "subprocess.Popen" not in launcher_source
    assert ".communicate(" not in launcher_source
    assert "DeleteFileW" not in filesystem_source
    assert "RemoveDirectoryW" not in filesystem_source
    assert "GetFileAttributesW" not in filesystem_source
    assert "CREATE_NO_WINDOW" in launcher_source
    assert "CREATE_SUSPENDED" in launcher_source


def test_windows_git_resolver_accepts_only_real_mingw_binary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_git = tmp_path / "Git" / "mingw64" / "bin" / "git.exe"
    shim = tmp_path / "Git" / "cmd" / "git.exe"
    real_git.parent.mkdir(parents=True)
    shim.parent.mkdir(parents=True)
    real_git.write_bytes(b"real")
    shim.write_bytes(b"shim")
    monkeypatch.setattr(sandbox_windows, "windows_git_candidates", lambda: (shim, real_git))

    assert sandbox_windows.resolve_windows_git_executable() == str(real_git.resolve())


def test_windows_git_resolver_rejects_cmd_shim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shim = tmp_path / "Git" / "cmd" / "git.exe"
    shim.parent.mkdir(parents=True)
    shim.write_bytes(b"shim")
    monkeypatch.setattr(sandbox_windows, "windows_git_candidates", lambda: (shim,))

    with pytest.raises(OSError, match=r"cmd\\git.exe"):
        sandbox_windows.resolve_windows_git_executable()


def test_windows_filetime_marker_preserves_all_64_bits() -> None:
    marker = FILETIME(0x89ABCDEF, 0x01234567)
    assert marker.as_uint64() == 0x0123456789ABCDEF


class _FakeWfpApi:
    def __init__(self, *, corrupt_readback: bool = False) -> None:
        self.blob_data = (ctypes.c_ubyte * 4)(1, 2, 3, 4)
        self.blob = sandbox_windows.FWP_BYTE_BLOB(4, self.blob_data)
        self.retrieved = sandbox_windows.FWPM_FILTER0()
        self.retrieved_condition = sandbox_windows.FWPM_FILTER_CONDITION0()
        self.layers: dict[int, sandbox_windows.GUID] = {}
        self.added_layers: list[str] = []
        self.readbacks: list[int] = []
        self.free_calls = 0
        self.close_calls = 0
        self.corrupt_readback = corrupt_readback
        self.close_statuses: list[int] = []

    def FwpmEngineOpen0(self, *_args: Any) -> int:
        output = ctypes.cast(_args[-1], ctypes.POINTER(ctypes.c_void_p))
        output[0] = ctypes.c_void_p(901)
        return 0

    def FwpmSubLayerAdd0(self, *_args: Any) -> int:
        return 0

    def FwpmGetAppIdFromFileName0(self, _path: str, output: Any) -> int:
        pointer = ctypes.cast(output, ctypes.POINTER(ctypes.POINTER(sandbox_windows.FWP_BYTE_BLOB)))
        pointer[0] = ctypes.pointer(self.blob)
        return 0

    def FwpmFilterAdd0(self, _engine: Any, filter_pointer: Any, _security: Any, output: Any) -> int:
        filter_object = ctypes.cast(
            filter_pointer, ctypes.POINTER(sandbox_windows.FWPM_FILTER0)
        ).contents
        self.added_layers.append(str(filter_object.displayData.name))
        identifier = len(self.added_layers)
        self.layers[identifier] = filter_object.layerKey
        ctypes.cast(output, ctypes.POINTER(ctypes.c_uint64))[0] = identifier
        return 0

    def FwpmFilterGetById0(self, _engine: Any, identifier: int, output: Any) -> int:
        self.readbacks.append(int(identifier))
        self.retrieved.filterId = identifier
        self.retrieved.layerKey = self.layers[int(identifier)]
        self.retrieved.action.type = sandbox_windows.FWP_ACTION_BLOCK
        if self.corrupt_readback and int(identifier) == len(sandbox_windows.LAYER_KEYS):
            self.retrieved.action.type = 0
        self.retrieved.numFilterConditions = 1
        self.retrieved_condition.fieldKey = sandbox_windows.APP_ID_KEY
        self.retrieved_condition.matchType = sandbox_windows.FWP_MATCH_EQUAL
        self.retrieved_condition.conditionValue.type = sandbox_windows.FWP_BYTE_BLOB_TYPE
        self.retrieved_condition.conditionValue.value.byteBlob = ctypes.pointer(self.blob)
        self.retrieved.filterCondition = ctypes.pointer(self.retrieved_condition)
        pointer = ctypes.cast(output, ctypes.POINTER(ctypes.POINTER(sandbox_windows.FWPM_FILTER0)))
        pointer[0] = ctypes.pointer(self.retrieved)
        return 0

    def FwpmFreeMemory0(self, _pointer: Any) -> None:
        self.free_calls += 1

    def FwpmEngineClose0(self, _engine: Any) -> int:
        self.close_calls += 1
        return self.close_statuses.pop(0) if self.close_statuses else 0


def test_wfp_filter_structure_keeps_the_native_64_bit_union_offsets() -> None:
    assert ctypes.sizeof(sandbox_windows.FWPM_FILTER_CONTEXT0) == 16
    if ctypes.sizeof(ctypes.c_void_p) == 8:
        assert sandbox_windows.FWPM_FILTER0.context.offset == 152
        assert sandbox_windows.FWPM_FILTER0.reserved.offset == 168
        assert sandbox_windows.FWPM_FILTER0.filterId.offset == 176
        assert ctypes.sizeof(sandbox_windows.FWPM_FILTER0) == 200


def test_wfp_session_requires_four_filter_additions_and_readbacks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = _FakeWfpApi()
    monkeypatch.setattr(sandbox_windows, "_wfp_api", lambda: api)

    session = sandbox_windows.WfpSession.create(r"C:\Program Files\Git\mingw64\bin\git.exe")

    assert session.verified
    assert session.filter_ids == (1, 2, 3, 4)
    assert len(api.added_layers) == 4
    assert api.readbacks == [1, 2, 3, 4]
    session.close()
    session.close()
    assert api.close_calls == 1
    assert api.free_calls == 5


def test_wfp_session_fails_closed_when_filter_readback_differs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = _FakeWfpApi(corrupt_readback=True)
    monkeypatch.setattr(sandbox_windows, "_wfp_api", lambda: api)

    with pytest.raises(OSError, match="readback did not match"):
        sandbox_windows.WfpSession.create(r"C:\Program Files\Git\mingw64\bin\git.exe")

    assert api.close_calls == 1
    assert api.free_calls == 5


def test_wfp_session_retains_engine_ownership_when_close_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = _FakeWfpApi()
    api.close_statuses = [5, 0]
    monkeypatch.setattr(sandbox_windows, "_wfp_api", lambda: api)
    session = sandbox_windows.WfpSession.create(r"C:\Program Files\Git\mingw64\bin\git.exe")

    with pytest.raises(OSError, match="FwpmEngineClose0"):
        session.close()

    assert session._engine == 901
    session.close()
    assert session._engine == 0
    assert api.close_calls == 2


def test_wfp_construction_retains_engine_when_cleanup_close_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = _FakeWfpApi(corrupt_readback=True)
    api.close_statuses = [5, 0]
    monkeypatch.setattr(sandbox_windows, "_wfp_api", lambda: api)
    sandbox_windows._RETAINED_WFP_ENGINES.clear()

    with pytest.raises(OSError, match="construction cleanup") as failure:
        sandbox_windows.WfpSession.create(r"C:\Program Files\Git\mingw64\bin\git.exe")

    assert failure.value.__cause__ is not None
    assert "readback did not match" in str(failure.value.__cause__)
    assert [(api, 901)] == sandbox_windows._RETAINED_WFP_ENGINES

    sandbox_windows._retry_retained_wfp_engines()
    assert sandbox_windows._RETAINED_WFP_ENGINES == []
    assert api.close_calls == 2


def test_windows_directory_retains_each_handle_when_close_fails() -> None:
    directory_attempts: list[int] = []
    root_attempts: list[int] = []

    def fail_directory_once(value: int) -> None:
        directory_attempts.append(value)
        if len(directory_attempts) == 1:
            raise OSError("injected directory close failure")

    def fail_root_once(value: int) -> None:
        root_attempts.append(value)
        if len(root_attempts) == 1:
            raise OSError("injected root close failure")

    root_handle = OwnedHandle(811, closer=fail_root_once)
    root = fs_windows.WindowsRoot(
        Path("/authorized-workspace"),
        root_handle,
        r"\\?\C:\authorized-workspace",
        fs_windows.WindowsFileIdentity(17, b"root".ljust(16, b"\0")),
    )
    directory_handle = OwnedHandle(812, closer=fail_directory_once)
    directory = fs_windows.WindowsDirectory(root, directory_handle, owns_root=True)

    with pytest.raises(OSError, match="directory close failure"):
        directory.close()

    assert not directory_handle.closed
    assert not root_handle.closed
    assert directory._directory is directory_handle
    assert directory._owns_root

    directory.close()
    assert directory_handle.closed
    assert root_handle.closed
    assert directory_attempts == [812, 812]
    assert root_attempts == [811, 811]


def test_windows_scan_scope_reuses_one_absolute_root_handle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root_path = Path("/authorized-workspace")
    opened: list[Path] = []
    closed: list[int] = []
    child_closed: list[int] = []
    relative_opens: list[tuple[str, ...]] = []

    def open_root(_cls: type[fs_windows.WindowsRoot], path: Path) -> fs_windows.WindowsRoot:
        opened.append(path)
        return fs_windows.WindowsRoot(
            path,
            OwnedHandle(801, closer=closed.append),
            r"\\?\C:\authorized-workspace",
            fs_windows.WindowsFileIdentity(17, b"root".ljust(16, b"\0")),
        )

    monkeypatch.setattr(fs_windows.WindowsRoot, "open", classmethod(open_root))

    with fs_windows.bind_authorized_root(root_path) as boundary:

        def open_directory(parts: tuple[str, ...]) -> OwnedHandle:
            relative_opens.append(parts)
            return OwnedHandle(802, closer=child_closed.append)

        monkeypatch.setattr(boundary, "open_directory", open_directory)
        first = fs_windows.open_directory(root_path)
        second = fs_windows.open_directory(root_path)
        nested = fs_windows.open_directory(root_path / "nested-project", "src")
        first.close()
        second.close()
        nested.close()
        assert not boundary.handle.closed

    assert opened == [root_path]
    assert closed == [801]
    assert child_closed == [802]
    assert relative_opens == [("nested-project", "src")]


class _FakeKernel32:
    def __init__(self, *, assign_ok: bool = True) -> None:
        self.assign_ok = assign_ok
        self.resumed = False
        self.terminated = False
        self.read_counts: dict[int, int] = {}

    def CreateProcessW(self, *_args: Any) -> int:
        process_info = ctypes.cast(
            _args[-1], ctypes.POINTER(launcher_windows.PROCESS_INFORMATION)
        ).contents
        process_info.hProcess = 501
        process_info.hThread = 502
        process_info.dwProcessId = 600
        process_info.dwThreadId = 601
        return 1

    def AssignProcessToJobObject(self, _job: Any, _process: Any) -> int:
        return int(self.assign_ok)

    def ResumeThread(self, _thread: Any) -> int:
        self.resumed = True
        return 1

    def WaitForSingleObject(self, _process: Any, _timeout: int) -> int:
        return launcher_windows.WAIT_OBJECT_0

    def GetExitCodeProcess(self, _process: Any, output: Any) -> int:
        ctypes.cast(output, ctypes.POINTER(ctypes.c_uint32))[0] = 0
        return 1

    def TerminateJobObject(self, _job: Any, _code: int) -> int:
        self.terminated = True
        return 1

    def TerminateProcess(self, _process: Any, _code: int) -> int:
        self.terminated = True
        return 1

    def ReadFile(self, handle: Any, buffer: Any, _length: int, count: Any, _overlapped: Any) -> int:
        value = int(handle.value)
        reads = self.read_counts.get(value, 0)
        self.read_counts[value] = reads + 1
        if reads == 0:
            content = b"stdout" if value == 101 else b"stderr"
            ctypes.memmove(buffer, content, len(content))
            ctypes.cast(count, ctypes.POINTER(ctypes.c_uint32))[0] = len(content)
            return 1
        ctypes.cast(count, ctypes.POINTER(ctypes.c_uint32))[0] = 0
        return 0


class _NeverStopsKernel32(_FakeKernel32):
    def WaitForSingleObject(self, _process: Any, _timeout: int) -> int:
        return launcher_windows.WAIT_TIMEOUT


class _FakeAttributeList:
    def __init__(self, handles: list[int]) -> None:
        self.handles = handles
        self.pointer = ctypes.c_void_p(701)

    def close(self) -> None:
        self.pointer = ctypes.c_void_p()


class _FakeGuard:
    verified = True

    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.closed = False

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            self.events.append("guard_close_called")


def _configure_fake_launcher(
    monkeypatch: pytest.MonkeyPatch, api: _FakeKernel32, closed: list[int]
) -> None:
    output_pairs = iter(((101, 102), (103, 104)))

    def handle(value: int) -> OwnedHandle:
        return OwnedHandle(value, closer=closed.append)

    monkeypatch.setattr(launcher_windows, "_kernel32", lambda: api)
    monkeypatch.setattr(launcher_windows, "OwnedHandle", lambda value: handle(value))
    monkeypatch.setattr(launcher_windows, "_create_job", lambda _api, _limit: handle(200))
    monkeypatch.setattr(
        launcher_windows,
        "_create_output_pipe",
        lambda _api: tuple(handle(value) for value in next(output_pairs)),
    )
    monkeypatch.setattr(launcher_windows, "_AttributeList", _FakeAttributeList)
    monkeypatch.setattr(
        WindowsTransferPipe,
        "create",
        classmethod(lambda _cls: WindowsTransferPipe(handle(301), handle(302))),
    )
    monkeypatch.setattr(launcher_windows, "last_error", lambda: launcher_windows.ERROR_BROKEN_PIPE)


def test_direct_launcher_assigns_before_resume_and_closes_wfp_last(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = _FakeKernel32()
    closed: list[int] = []
    _configure_fake_launcher(monkeypatch, api, closed)
    events: list[str] = []
    guard = _FakeGuard(events)

    result = launcher_windows.run_windows_process(
        launcher_windows.WindowsLaunchRequest(
            application=r"C:\Python312\python.exe",
            arguments=("-c", "pass"),
            cwd=r"C:\runtime",
            environment={"PATH": ""},
            maximum_output_bytes=1024,
            timeout_seconds=1.0,
            active_process_limit=1,
        ),
        network_guard=guard,
        observer=events.append,
    )

    assert result.stdout == b"stdout"
    assert result.stderr == b"stderr"
    assert api.resumed
    assert events[:6] == [
        "boundary_verified",
        "created_suspended",
        "assigned_job",
        "child_sides_closed",
        "resumed",
        "primary_thread_closed",
    ]
    assert events[-3:] == ["job_closed", "guard_close_called", "network_guard_closed"]
    assert closed.count(200) == 1
    assert closed.count(501) == 1
    assert closed.count(502) == 1


def test_direct_launcher_assign_failure_terminates_without_resume(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = _FakeKernel32(assign_ok=False)
    closed: list[int] = []
    _configure_fake_launcher(monkeypatch, api, closed)
    events: list[str] = []
    guard = _FakeGuard(events)

    with pytest.raises(OSError, match="AssignProcessToJobObject"):
        launcher_windows.run_windows_process(
            launcher_windows.WindowsLaunchRequest(
                application=r"C:\Python312\python.exe",
                arguments=("-c", "pass"),
                cwd=r"C:\runtime",
                environment={},
                maximum_output_bytes=1024,
                timeout_seconds=1.0,
            ),
            network_guard=guard,
            observer=events.append,
        )

    assert api.terminated
    assert not api.resumed
    assert "resumed" not in events
    assert events[-3:] == ["job_closed", "guard_close_called", "network_guard_closed"]


def test_direct_launcher_closes_network_guard_when_api_loading_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    guard = _FakeGuard(events)

    def fail_api_load() -> None:
        raise OSError("kernel32 unavailable")

    monkeypatch.setattr(launcher_windows, "_kernel32", fail_api_load)

    with pytest.raises(OSError, match="kernel32 unavailable"):
        launcher_windows.run_windows_process(
            launcher_windows.WindowsLaunchRequest(
                application=r"C:\Python312\python.exe",
                arguments=("-c", "pass"),
                cwd=r"C:\runtime",
                environment={},
                maximum_output_bytes=1024,
                timeout_seconds=1.0,
            ),
            network_guard=guard,
            observer=events.append,
        )

    assert events == [
        "boundary_verified",
        "guard_close_called",
        "network_guard_closed",
    ]


def test_direct_launcher_reports_late_protected_input_writer_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = _FakeKernel32()
    closed: list[int] = []
    _configure_fake_launcher(monkeypatch, api, closed)

    def fail_write(_handle: int, _content: bytes) -> None:
        time.sleep(0.05)
        raise OSError("protected input write failed")

    monkeypatch.setattr(launcher_windows, "write_handle", fail_write)

    with pytest.raises(OSError, match="protected input write failed"):
        launcher_windows.run_windows_process(
            launcher_windows.WindowsLaunchRequest(
                application=r"C:\Python312\python.exe",
                arguments=("-c", "pass"),
                cwd=r"C:\runtime",
                environment={},
                maximum_output_bytes=1024,
                timeout_seconds=1.0,
            ),
            inputs=(launcher_windows.ProtectedInput("--capability-handle", b"secret"),),
        )


def test_direct_launcher_retains_wfp_when_child_exit_cannot_be_confirmed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = _NeverStopsKernel32()
    closed: list[int] = []
    _configure_fake_launcher(monkeypatch, api, closed)
    events: list[str] = []
    guard = _FakeGuard(events)

    with pytest.raises(subprocess.TimeoutExpired):
        launcher_windows.run_windows_process(
            launcher_windows.WindowsLaunchRequest(
                application=r"C:\Python312\python.exe",
                arguments=("-c", "pass"),
                cwd=r"C:\runtime",
                environment={},
                maximum_output_bytes=1024,
                timeout_seconds=0.001,
            ),
            network_guard=guard,
            observer=events.append,
        )

    assert not guard.closed
    assert "network_guard_closed" not in events


def test_direct_launcher_continues_cleanup_after_attribute_close_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = _FakeKernel32()
    closed: list[int] = []
    _configure_fake_launcher(monkeypatch, api, closed)
    events: list[str] = []
    guard = _FakeGuard(events)

    class FailingAttributeList(_FakeAttributeList):
        def close(self) -> None:
            super().close()
            raise OSError("attribute close failed")

    monkeypatch.setattr(launcher_windows, "_AttributeList", FailingAttributeList)

    with pytest.raises(OSError, match="attribute close failed"):
        launcher_windows.run_windows_process(
            launcher_windows.WindowsLaunchRequest(
                application=r"C:\Python312\python.exe",
                arguments=("-c", "pass"),
                cwd=r"C:\runtime",
                environment={},
                maximum_output_bytes=1024,
                timeout_seconds=1.0,
            ),
            network_guard=guard,
            observer=events.append,
        )

    assert guard.closed
    assert events[-3:] == ["job_closed", "guard_close_called", "network_guard_closed"]


windows_only = pytest.mark.skipif(
    __import__("sys").platform != "win32", reason="native Windows API smoke test"
)


@windows_only
def test_nt_handle_relative_data_tree_smoke(tmp_path: Path) -> None:
    from goodjob.platform.fs_windows import WindowsDataTree

    tree_root = tmp_path / "data"
    publication_parent = tree_root / "artifacts"
    publication_parent.mkdir(parents=True)
    with WindowsDataTree(tree_root, "test") as tree:
        tree.write_new("artifacts/source.tmp", b"source")
        assert tree.read_regular("artifacts/source.tmp") == b"source"
        tree.replace_file("artifacts/source.tmp", "artifacts/source.txt")
        assert tree.list_directory("artifacts") == {"source.txt"}
        tree.publish_directory(
            "artifacts/candidate",
            "artifacts/final",
            {"report.txt": b"report"},
            verify=lambda _relative: None,
            before_rename=None,
        )
        assert tree.read_regular("artifacts/final/report.txt") == b"report"
        tree.remove("artifacts/final")
        assert tree.list_directory("artifacts") == {"source.txt"}


def test_windows_git_runner_scopes_wfp_and_job_to_the_exact_application(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import goodjob.git_metadata as git_metadata

    git = tmp_path / "Git" / "mingw64" / "bin" / "git.exe"
    git.parent.mkdir(parents=True)
    git.write_bytes(b"git")
    workspace = tmp_path / "workspace"
    git_dir = workspace / ".git"
    git_dir.mkdir(parents=True)
    binding = InternalGitBinding(
        workspace_root=workspace,
        worktree_root=workspace,
        git_dir=git_dir,
        common_dir=git_dir,
        worktree_identity=(1, 2),
        git_dir_identity=(1, 3),
        common_dir_identity=(1, 3),
    )
    reader = GitMetadataReader(
        git_executable=str(git),
        issue_factory=lambda *_args: pytest.fail("issue factory should not run"),
        safe_history_path=lambda _path: True,
        git_command_timeout_seconds=lambda: 7.0,
        workspace_git_command=lambda _binding, arguments: [str(git), *arguments],
    )
    monkeypatch.setattr(reader, "_verify_git_binding", lambda _binding: None)
    sandbox = sandbox_windows.WfpGitSandbox(str(git))
    guard_events: list[str] = []
    guard = _FakeGuard(guard_events)
    monkeypatch.setattr(sandbox, "open_network_guard", lambda: guard)
    monkeypatch.setattr(git_metadata, "select_git_sandbox", lambda _executable: sandbox)
    captured: list[launcher_windows.WindowsLaunchRequest] = []

    def run(
        request: launcher_windows.WindowsLaunchRequest,
        **_kwargs: object,
    ) -> launcher_windows.WindowsLaunchResult:
        captured.append(request)
        launch_guard = _kwargs["network_guard"]
        assert isinstance(launch_guard, _FakeGuard)
        launch_guard.close()
        return launcher_windows.WindowsLaunchResult(request.arguments, 0, b"head\n", b"")

    monkeypatch.setattr(launcher_windows, "run_windows_process", run)

    returncode, stdout, stderr = reader._git_bounded_bytes_windows(
        binding, "rev-parse", "HEAD", maximum_output_bytes=4096
    )

    assert (returncode, stdout, stderr) == (0, b"head\n", b"")
    assert len(captured) == 1
    request = captured[0]
    assert request.application == str(git)
    assert request.arguments == ("rev-parse", "HEAD")
    assert request.active_process_limit == 1
    assert request.maximum_output_bytes == 4096
    assert request.environment["GIT_CONFIG_GLOBAL"] == "NUL"
    assert request.environment["GIT_ALLOW_PROTOCOL"] == "file"
    assert guard.closed


def test_cli_uses_numeric_handle_arguments_on_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    import goodjob.cli as cli

    monkeypatch.setattr(sys, "platform", "win32")
    parser = cli.build_parser()
    args = parser.parse_args(
        [
            "authorize",
            "--receipt-kind",
            "source_analysis",
            "--scope-json",
            "{}",
            "--notice-version",
            "v1",
            "--capability-handle",
            "91",
            "--confirmed",
        ]
    )

    assert args.capability_handle == 91
    assert not hasattr(args, "capability_fd")


def test_cli_fails_closed_while_native_windows_release_gate_is_disabled(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import goodjob.cli as cli
    import goodjob.platform.detect as platform_detect

    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(platform_detect, "NATIVE_WINDOWS_RELEASE_ENABLED", False)

    assert cli.run(["data-status"]) == 2
    response = capsys.readouterr()
    assert "unsupported_platform" in response.err
    assert "use WSL2" in response.err

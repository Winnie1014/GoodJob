from __future__ import annotations

import os
import threading
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from goodjob import git_metadata
from goodjob.platform import fs_windows, handles_windows, launcher_windows, sandbox_windows
from goodjob.platform.handles_windows import OwnedCrtDescriptor, OwnedHandle, RetryableOwner


@pytest.fixture(autouse=True)
def _isolate_retained_owners() -> Iterator[None]:
    handles_windows._RETAINED_OWNERS.clear()
    launcher_windows._RETAINED_CLEANUP_GROUPS.clear()
    sandbox_windows._RETAINED_WFP_ENGINES.clear()
    yield
    handles_windows._RETAINED_OWNERS.clear()
    launcher_windows._RETAINED_CLEANUP_GROUPS.clear()
    sandbox_windows._RETAINED_WFP_ENGINES.clear()


def test_retained_owner_retry_serializes_concurrent_callers() -> None:
    attempts = 0
    attempts_lock = threading.Lock()
    retry_started = threading.Event()
    release_retry = threading.Event()
    fail_close = True

    def close(_value: int) -> None:
        nonlocal attempts
        with attempts_lock:
            attempts += 1
            attempt = attempts
        if attempt == 2:
            retry_started.set()
            assert release_retry.wait(timeout=2.0)
        if fail_close:
            raise OSError("injected retained owner close failure")

    owner = OwnedHandle(101, closer=close)
    with pytest.raises(OSError, match="retained owner close failure"):
        handles_windows.close_owned_resources((owner,))

    outcomes: list[str] = []
    second_entered = threading.Event()
    second_done = threading.Event()

    def retry(
        *, entered: threading.Event | None = None, done: threading.Event | None = None
    ) -> None:
        if entered is not None:
            entered.set()
        try:
            handles_windows.retry_retained_owners()
        except OSError:
            outcomes.append("failed")
        else:
            outcomes.append("passed")
        finally:
            if done is not None:
                done.set()

    first = threading.Thread(target=retry)
    first.start()
    assert retry_started.wait(timeout=2.0)
    second = threading.Thread(target=retry, kwargs={"entered": second_entered, "done": second_done})
    second.start()

    assert second_entered.wait(timeout=2.0)
    assert not second_done.wait(timeout=0.2)
    release_retry.set()
    first.join(timeout=2.0)
    second.join(timeout=2.0)

    assert not first.is_alive()
    assert not second.is_alive()
    assert outcomes == ["failed", "failed"]

    fail_close = False
    handles_windows.retry_retained_owners()
    assert owner.closed


@pytest.mark.parametrize("owner_kind", ["handle", "descriptor"])
def test_owner_close_calls_the_underlying_closer_once_across_threads(owner_kind: str) -> None:
    close_started = threading.Event()
    release_close = threading.Event()
    second_entered = threading.Event()
    second_done = threading.Event()
    calls: list[int] = []

    def close(value: int) -> None:
        calls.append(value)
        close_started.set()
        assert release_close.wait(timeout=2.0)

    owner: RetryableOwner
    if owner_kind == "handle":
        owner = OwnedHandle(102, closer=close)
    else:
        owner = OwnedCrtDescriptor(102, closer=close)

    def close_again() -> None:
        second_entered.set()
        owner.close()
        second_done.set()

    first = threading.Thread(target=owner.close)
    second = threading.Thread(target=close_again)
    first.start()
    assert close_started.wait(timeout=2.0)
    second.start()
    assert second_entered.wait(timeout=2.0)
    assert not second_done.wait(timeout=0.2)
    release_close.set()
    first.join(timeout=2.0)
    second.join(timeout=2.0)

    assert not first.is_alive()
    assert not second.is_alive()
    assert calls == [102]
    assert owner.closed


@pytest.mark.parametrize(
    "factory",
    [
        lambda closer: OwnedHandle(103, closer=closer),
        lambda closer: OwnedCrtDescriptor(103, closer=closer),
    ],
    ids=("handle", "descriptor"),
)
def test_owner_close_blocks_concurrent_detach(
    factory: Callable[[Callable[[int], None]], OwnedHandle | OwnedCrtDescriptor],
) -> None:
    close_started = threading.Event()
    release_close = threading.Event()
    detach_entered = threading.Event()
    detach_done = threading.Event()
    detached: list[int | str] = []

    def close(_value: int) -> None:
        close_started.set()
        assert release_close.wait(timeout=2.0)

    owner = factory(close)

    def detach() -> None:
        detach_entered.set()
        try:
            detached.append(owner.detach())
        except OSError:
            detached.append("consumed")
        finally:
            detach_done.set()

    close_thread = threading.Thread(target=owner.close)
    detach_thread = threading.Thread(target=detach)
    close_thread.start()
    assert close_started.wait(timeout=2.0)
    detach_thread.start()

    assert detach_entered.wait(timeout=2.0)
    assert not detach_done.wait(timeout=0.2)
    release_close.set()
    close_thread.join(timeout=2.0)
    detach_thread.join(timeout=2.0)

    assert detached == ["consumed"]
    assert owner.closed


@pytest.mark.parametrize(
    "factory",
    [
        lambda closer: OwnedHandle(109, closer=closer),
        lambda closer: OwnedCrtDescriptor(109, closer=closer),
    ],
    ids=("handle", "descriptor"),
)
def test_failed_concurrent_close_calls_closer_once_until_registry_retry(
    factory: Callable[[Callable[[int], None]], OwnedHandle | OwnedCrtDescriptor],
) -> None:
    close_started = threading.Event()
    release_close = threading.Event()
    second_entered = threading.Event()
    fail_close = True
    calls: list[int] = []
    outcomes: list[str] = []

    def close(value: int) -> None:
        calls.append(value)
        if len(calls) == 1:
            close_started.set()
            assert release_close.wait(timeout=2.0)
        if fail_close:
            raise OSError("injected concurrent close failure")

    owner = factory(close)

    def close_owner(*, entered: threading.Event | None = None) -> None:
        if entered is not None:
            entered.set()
        try:
            handles_windows.close_owned_resources((owner,))
        except OSError:
            outcomes.append("failed")
        else:
            outcomes.append("passed")

    first = threading.Thread(target=close_owner)
    second = threading.Thread(target=close_owner, kwargs={"entered": second_entered})
    first.start()
    assert close_started.wait(timeout=2.0)
    second.start()
    assert second_entered.wait(timeout=2.0)
    release_close.set()
    first.join(timeout=2.0)
    second.join(timeout=2.0)

    assert not first.is_alive()
    assert not second.is_alive()
    assert calls == [109]
    assert outcomes == ["failed", "failed"]
    assert [owner] == handles_windows._RETAINED_OWNERS

    fail_close = False
    handles_windows.retry_retained_owners()
    assert calls == [109, 109]
    assert owner.closed


@pytest.mark.parametrize(
    "factory",
    [
        lambda closer: OwnedHandle(108, closer=closer),
        lambda closer: OwnedCrtDescriptor(108, closer=closer),
    ],
    ids=("handle", "descriptor"),
)
def test_failed_owner_close_blocks_concurrent_detach_and_remains_retryable(
    factory: Callable[[Callable[[int], None]], OwnedHandle | OwnedCrtDescriptor],
) -> None:
    close_started = threading.Event()
    release_close = threading.Event()
    fail_close = True
    detached: list[int | str] = []

    def close(_value: int) -> None:
        close_started.set()
        assert release_close.wait(timeout=2.0)
        if fail_close:
            raise OSError("injected concurrent close failure")

    owner = factory(close)

    def close_owner() -> None:
        with pytest.raises(OSError, match="concurrent close failure"):
            handles_windows.close_owned_resources((owner,))

    def detach_owner() -> None:
        try:
            detached.append(owner.detach())
        except OSError:
            detached.append("consumed")

    close_thread = threading.Thread(target=close_owner)
    detach_thread = threading.Thread(target=detach_owner)
    close_thread.start()
    assert close_started.wait(timeout=2.0)
    detach_thread.start()
    release_close.set()
    close_thread.join(timeout=2.0)
    detach_thread.join(timeout=2.0)

    assert detached == ["consumed"]
    assert [owner] == handles_windows._RETAINED_OWNERS

    fail_close = False
    handles_windows.retry_retained_owners()
    assert owner.closed


def test_launcher_closes_fresh_network_guard_when_historical_owner_retry_fails() -> None:
    fail_close = True

    def close_historical(_value: int) -> None:
        if fail_close:
            raise OSError("injected historical owner close failure")

    historical_owner = OwnedHandle(104, closer=close_historical)
    with pytest.raises(OSError, match="historical owner close failure"):
        handles_windows.close_owned_resources((historical_owner,))

    class FreshGuard:
        verified = True

        def __init__(self) -> None:
            self.close_calls = 0

        def close(self) -> None:
            self.close_calls += 1

    guard = FreshGuard()
    request = launcher_windows.WindowsLaunchRequest(
        application=r"C:\Program Files\Git\mingw64\bin\git.exe",
        arguments=("status",),
        cwd=r"C:\workspace",
        environment={},
        maximum_output_bytes=1024,
        timeout_seconds=1.0,
        active_process_limit=1,
    )

    with pytest.raises(OSError, match="previous Windows owner cleanup remains incomplete"):
        launcher_windows.run_windows_process(request, network_guard=guard)

    assert guard.close_calls == 1
    assert launcher_windows._RETAINED_CLEANUP_GROUPS == []

    fail_close = False
    handles_windows.retry_retained_owners()


def test_launcher_retains_fresh_network_guard_when_preflight_and_guard_close_fail() -> None:
    fail_historical_close = True

    def close_historical(_value: int) -> None:
        if fail_historical_close:
            raise OSError("injected historical owner close failure")

    historical_owner = OwnedHandle(105, closer=close_historical)
    with pytest.raises(OSError, match="historical owner close failure"):
        handles_windows.close_owned_resources((historical_owner,))

    class FreshGuard:
        verified = True

        def __init__(self) -> None:
            self.close_calls = 0
            self.fail_close = True

        def close(self) -> None:
            self.close_calls += 1
            if self.fail_close:
                raise OSError("injected fresh guard close failure")

    guard = FreshGuard()
    request = launcher_windows.WindowsLaunchRequest(
        application=r"C:\Program Files\Git\mingw64\bin\git.exe",
        arguments=("status",),
        cwd=r"C:\workspace",
        environment={},
        maximum_output_bytes=1024,
        timeout_seconds=1.0,
        active_process_limit=1,
    )

    with pytest.raises(OSError, match="fresh guard close failure") as raised:
        launcher_windows.run_windows_process(request, network_guard=guard)

    assert raised.value.__cause__ is not None
    assert "previous Windows owner cleanup remains incomplete" in str(raised.value.__cause__)
    assert guard.close_calls == 1
    assert len(launcher_windows._RETAINED_CLEANUP_GROUPS) == 1

    fail_historical_close = False
    guard.fail_close = False
    launcher_windows._retry_retained_cleanup_groups()

    assert historical_owner.closed
    assert guard.close_calls == 2
    assert launcher_windows._RETAINED_CLEANUP_GROUPS == []


def test_fs_read_retains_delivered_descriptor_when_final_close_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    attempts = 0

    def fail_close_once(_value: int) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError("injected delivered descriptor close failure")

    descriptor = OwnedCrtDescriptor(106, closer=fail_close_once)

    class FakeDirectory:
        def __enter__(self) -> FakeDirectory:
            return self

        def __exit__(self, *_exc: object) -> None:
            return None

        @staticmethod
        def open_regular(_relative: str) -> OwnedHandle:
            return OwnedHandle(206, closer=lambda _value: None)

    monkeypatch.setattr(fs_windows.WindowsDirectory, "open", lambda *_args: FakeDirectory())
    monkeypatch.setattr(fs_windows, "transfer_handle_to_crt_descriptor", lambda *_args: descriptor)
    monkeypatch.setattr(os, "fstat", lambda _descriptor: os.stat_result((0,) * 10))
    monkeypatch.setattr(os, "read", lambda _descriptor, _count: b"")
    monkeypatch.setattr(os, "close", fail_close_once)

    with pytest.raises(OSError, match="delivered descriptor close failure"):
        fs_windows.read_regular(tmp_path, "source.py")

    assert [descriptor] == handles_windows._RETAINED_OWNERS
    handles_windows.retry_retained_owners()
    assert descriptor.closed
    assert attempts == 2


def test_git_read_retains_delivered_descriptor_when_final_close_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts = 0

    def fail_close_once(_value: int) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError("injected Git descriptor close failure")

    descriptor = OwnedCrtDescriptor(107, closer=fail_close_once)
    monkeypatch.setattr(
        git_metadata,
        "_open_regular_file",
        lambda *_args: (descriptor, os.stat_result((0,) * 10)),
    )
    monkeypatch.setattr(git_metadata, "_read_open_file", lambda *_args, **_kwargs: b"gitdir: .git")

    assert git_metadata.GitMetadataReader._git_pointer_target_at(Path("workspace"), ".git") is None
    assert [descriptor] == handles_windows._RETAINED_OWNERS

    handles_windows.retry_retained_owners()
    assert descriptor.closed
    assert attempts == 2

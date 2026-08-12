from __future__ import annotations

import shutil
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from goodjob.platform import detect_platform, select_git_sandbox
from goodjob.platform.detect import Platform, git_executable_candidates, resolve_git_executable


def test_detect_platform_returns_current_platform() -> None:
    platform = detect_platform()
    if sys.platform == "darwin":
        assert platform == Platform.MACOS
    elif sys.platform.startswith("linux"):
        assert platform == Platform.LINUX
    else:
        pytest.skip(f"test does not cover platform: {sys.platform}")


def test_platform_from_sys_platform_handles_known_values() -> None:
    assert Platform.from_sys_platform("darwin") == Platform.MACOS
    assert Platform.from_sys_platform("linux") == Platform.LINUX
    assert Platform.from_sys_platform("linux2") == Platform.LINUX
    assert Platform.from_sys_platform("win32") == Platform.WINDOWS


def test_platform_from_sys_platform_rejects_unknown() -> None:
    with pytest.raises(OSError, match="unsupported host platform"):
        Platform.from_sys_platform("unsupported")


def test_select_git_sandbox_returns_matching_backend() -> None:
    platform = detect_platform()
    sandbox = select_git_sandbox("/usr/bin/git")
    if platform == Platform.MACOS:
        from goodjob.platform.sandbox_macos import SeatbeltSandbox

        assert isinstance(sandbox, SeatbeltSandbox)
    elif platform == Platform.LINUX:
        from goodjob.platform.sandbox_linux import BwrapSandbox

        assert isinstance(sandbox, BwrapSandbox)
    else:
        pytest.skip(f"test does not cover platform: {platform}")


def test_git_executable_candidates_includes_platform_paths() -> None:
    candidates = git_executable_candidates()
    platform = detect_platform()
    if platform == Platform.MACOS:
        assert any("Xcode" in str(c) for c in candidates)
    elif platform == Platform.LINUX:
        assert Path("/usr/bin/git") in candidates
    assert len(candidates) > 0


def test_resolve_git_executable_returns_existing_path() -> None:
    git = resolve_git_executable()
    assert isinstance(git, str)
    assert len(git) > 0


# --- macOS SeatbeltSandbox tests ---


macos_only = pytest.mark.skipif(
    sys.platform != "darwin", reason="macOS Seatbelt sandbox tests run only on macOS"
)


@macos_only
def test_seatbelt_sandbox_is_available_on_macos() -> None:
    from goodjob.platform.sandbox_macos import SeatbeltSandbox

    sandbox = SeatbeltSandbox()
    assert sandbox.is_available()


@macos_only
def test_seatbelt_sandbox_builds_correct_command_structure() -> None:
    from goodjob.platform.sandbox_macos import GIT_SANDBOX_PROFILE, SeatbeltSandbox

    binding = MagicMock()
    binding.workspace_root = Path("/test/workspace")
    binding.git_dir = Path("/test/workspace/.git")
    binding.worktree_root = Path("/test/workspace")

    sandbox = SeatbeltSandbox()
    command = sandbox.build_command("/usr/bin/git", binding, ["rev-parse", "HEAD"])

    assert command[0] == "/usr/bin/sandbox-exec"
    assert "-p" in command
    profile_idx = command.index("-p") + 1
    assert command[profile_idx] == GIT_SANDBOX_PROFILE
    assert f"AUTHORIZED_ROOT={binding.workspace_root}" in command
    assert "GIT_EXECUTABLE=/usr/bin/git" in command
    assert command[-2:] == ["rev-parse", "HEAD"]


@macos_only
def test_seatbelt_sandbox_raises_when_executable_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from goodjob.platform.sandbox_macos import SeatbeltSandbox

    sandbox = SeatbeltSandbox()
    monkeypatch.setattr(
        "goodjob.platform.sandbox_macos.SANDBOX_EXECUTABLE", tmp_path / "nonexistent"
    )
    assert not sandbox.is_available()
    binding = MagicMock()
    with pytest.raises(OSError, match="sandbox-exec"):
        sandbox.build_command("/usr/bin/git", binding, [])


# --- Linux BwrapSandbox tests ---


linux_only = pytest.mark.skipif(
    not sys.platform.startswith("linux"), reason="bwrap tests run only on Linux"
)


linux_with_bwrap = pytest.mark.skipif(
    not sys.platform.startswith("linux") or shutil.which("bwrap") is None,
    reason="bwrap tests require Linux with bwrap installed",
)


@linux_only
def test_bwrap_sandbox_availability_matches_which() -> None:
    from goodjob.platform.sandbox_linux import BwrapSandbox

    sandbox = BwrapSandbox()
    assert sandbox.is_available() == (shutil.which("bwrap") is not None)


@linux_only
def test_bwrap_sandbox_raises_when_executable_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    from goodjob.platform import sandbox_linux

    monkeypatch.setattr(sandbox_linux, "_find_bwrap", lambda: None)
    sandbox = sandbox_linux.BwrapSandbox()
    assert not sandbox.is_available()
    binding = MagicMock()
    with pytest.raises(OSError, match="bubblewrap"):
        sandbox.build_command("/usr/bin/git", binding, [])


@linux_only
def test_bwrap_sandbox_builds_correct_command_structure() -> None:
    from goodjob.platform.sandbox_linux import BwrapSandbox

    binding = MagicMock()
    binding.workspace_root = Path("/test/workspace")
    binding.git_dir = Path("/test/workspace/.git")
    binding.worktree_root = Path("/test/workspace")

    bwrap_path = shutil.which("bwrap")
    if bwrap_path is None:
        pytest.skip("bwrap not installed")

    sandbox = BwrapSandbox()
    command = sandbox.build_command("/usr/bin/git", binding, ["rev-parse", "HEAD"])

    assert command[0] == bwrap_path
    assert "--die-with-parent" in command
    assert "--unshare-net" in command
    assert "--unshare-pid" in command

    unshare_pid_idx = command.index("--unshare-pid")
    proc_idx = command.index("--proc")
    assert proc_idx > unshare_pid_idx, "--proc must come after --unshare-pid"

    assert "--ro-bind" in command
    ro_bind_idx = command.index("--ro-bind")
    assert command[ro_bind_idx + 1] == "/test/workspace"
    assert command[ro_bind_idx + 2] == "/test/workspace"

    assert "--dev" in command
    assert "--tmpfs" in command
    assert command[-2:] == ["rev-parse", "HEAD"]


@linux_only
def test_bwrap_sandbox_command_includes_required_system_paths() -> None:
    from goodjob.platform.sandbox_linux import BwrapSandbox

    binding = MagicMock()
    binding.workspace_root = Path("/test/workspace")
    binding.git_dir = Path("/test/workspace/.git")
    binding.worktree_root = Path("/test/workspace")

    bwrap_path = shutil.which("bwrap")
    if bwrap_path is None:
        pytest.skip("bwrap not installed")

    sandbox = BwrapSandbox()
    command = sandbox.build_command("/usr/bin/git", binding, ["status"])

    command_str = " ".join(command)
    assert "--ro-bind /usr /usr" in command_str
    assert "--ro-bind /lib /lib" in command_str
    assert "--ro-bind-try /lib64 /lib64" in command_str
    assert "--ro-bind-try /etc /etc" in command_str


# --- process_identity platform tests ---


def test_process_identity_returns_consistent_marker_for_current_process() -> None:
    from goodjob.process_identity import process_identity, process_start_marker

    identity = process_identity()
    assert identity.startswith("pid:")
    assert ";started:" in identity

    pid_str = identity.split(";")[0].removeprefix("pid:")
    pid = int(pid_str)
    marker = process_start_marker(pid)
    assert marker is not None
    assert marker == identity.split(";started:")[1]


def test_process_start_marker_returns_none_for_nonexistent_pid() -> None:
    from goodjob.process_identity import process_start_marker

    result = process_start_marker(99999999)
    assert result is None


@macos_only
def test_macos_start_marker_uses_ps() -> None:
    import os

    from goodjob.process_identity import _macos_start_marker

    marker = _macos_start_marker(os.getpid())
    assert marker is not None
    assert any(c.isalpha() for c in marker)


@linux_only
def test_linux_start_marker_uses_proc_stat() -> None:
    import os

    from goodjob.process_identity import _linux_start_marker

    marker = _linux_start_marker(os.getpid())
    assert marker is not None
    assert marker.isdigit()


@linux_only
def test_linux_start_marker_reads_field_22_of_proc_stat() -> None:
    import os

    from goodjob.process_identity import _linux_start_marker

    raw_path = f"/proc/{os.getpid()}/stat"
    with open(raw_path, "rb") as f:
        content = f.read().decode("utf-8", errors="replace")
    right_paren = content.rfind(")")
    fields = content[right_paren + 2 :].split()
    expected_starttime = fields[19]

    marker = _linux_start_marker(os.getpid())
    assert marker == expected_starttime

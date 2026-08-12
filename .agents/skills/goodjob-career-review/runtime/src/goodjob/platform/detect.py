"""Detect the host platform and select the matching Git sandbox backend."""

from __future__ import annotations

import enum
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from goodjob.git_metadata import InternalGitBinding


class GitSandbox(Protocol):
    """Build the sandboxed Git command for the current platform."""

    def build_command(
        self,
        git_executable: str,
        binding: InternalGitBinding,
        git_args: list[str],
    ) -> list[str]: ...


@dataclass(frozen=True)
class PlatformBackend:
    """The resolved platform identity and its Git sandbox backend (if any)."""

    platform: Platform
    sandbox: GitSandbox | None


class Platform(enum.Enum):
    """Enumeration of supported host platforms."""

    MACOS = "macos"
    LINUX = "linux"
    WINDOWS = "windows"

    @classmethod
    def from_sys_platform(cls, value: str) -> Platform:
        if value == "darwin":
            return cls.MACOS
        if value.startswith("linux"):
            return cls.LINUX
        if value == "win32":
            return cls.WINDOWS
        raise OSError(f"unsupported host platform: {value}")


def detect_platform() -> Platform:
    """Return the current host platform."""
    return Platform.from_sys_platform(sys.platform)


def _macos_sandbox() -> GitSandbox:
    from goodjob.platform.sandbox_macos import SeatbeltSandbox

    return SeatbeltSandbox()


def _linux_sandbox() -> GitSandbox:
    from goodjob.platform.sandbox_linux import BwrapSandbox

    return BwrapSandbox()


def select_git_sandbox(git_executable: str) -> GitSandbox:
    """Return the Git sandbox backend for the current platform.

    Raises OSError when no supported sandbox backend is available on the host.
    Fail-closed: never returns a no-op backend.
    """
    platform = detect_platform()
    if platform == Platform.MACOS:
        return _macos_sandbox()
    if platform == Platform.LINUX:
        return _linux_sandbox()
    raise OSError("a supported local Git filesystem sandbox is unavailable on this platform")


def git_executable_candidates() -> tuple[Path, ...]:
    """Return platform-aware Git executable candidate paths."""
    platform = detect_platform()
    if platform == Platform.MACOS:
        return (
            Path("/Applications/Xcode.app/Contents/Developer/usr/bin/git"),
            Path("/Library/Developer/CommandLineTools/usr/bin/git"),
            Path("/usr/bin/git"),
            Path("/bin/git"),
        )
    if platform == Platform.LINUX:
        return (
            Path("/usr/bin/git"),
            Path("/usr/local/bin/git"),
            Path("/bin/git"),
        )
    return (Path("git"),)


def resolve_git_executable() -> str:
    """Resolve the Git executable path, falling back to ``shutil.which``."""
    for candidate in git_executable_candidates():
        if candidate.is_file():
            return str(candidate)
    found = shutil.which("git")
    if found:
        return found
    return "/usr/bin/git"

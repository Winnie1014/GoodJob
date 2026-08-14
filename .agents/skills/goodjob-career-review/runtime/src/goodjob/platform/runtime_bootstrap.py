"""Select an installed Python 3.12+ without downloading or trusting a workspace."""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Protocol

_VERSION_PATTERN = re.compile(r"(?:Python\s+)?(3)\.(\d+)\.(\d+)")


class CommandRunner(Protocol):
    def __call__(
        self,
        command: Sequence[str],
        *,
        capture_output: bool,
        text: bool,
        check: bool,
        timeout: float,
    ) -> subprocess.CompletedProcess[str]: ...


@dataclass(frozen=True)
class PythonRuntime:
    command: tuple[str, ...]
    kind: str
    version: tuple[int, int, int]


def _run_command(
    command: Sequence[str],
    *,
    capture_output: bool,
    text: bool,
    check: bool,
    timeout: float,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        capture_output=capture_output,
        text=text,
        check=check,
        timeout=timeout,
    )


def _version_from_output(output: str) -> tuple[int, int, int] | None:
    match = _VERSION_PATTERN.search(output.strip())
    if match is None:
        return None
    version = tuple(int(part) for part in match.groups())
    assert len(version) == 3
    return version


def _probe_version(command: tuple[str, ...], runner: CommandRunner) -> tuple[int, int, int] | None:
    try:
        result = runner(
            [*command, "--version"],
            capture_output=True,
            text=True,
            check=False,
            timeout=5.0,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return _version_from_output(f"{result.stdout}\n{result.stderr}")


def _uv_runtime(uv: str, runner: CommandRunner) -> PythonRuntime | None:
    find_command = (
        uv,
        "python",
        "find",
        "--no-project",
        "--no-config",
        "--offline",
        "--no-python-downloads",
        "--show-version",
        "3.12",
    )
    try:
        result = runner(
            find_command,
            capture_output=True,
            text=True,
            check=False,
            timeout=5.0,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    version = _version_from_output(f"{result.stdout}\n{result.stderr}")
    if version is None or version < (3, 12, 0):
        return None
    return PythonRuntime(
        command=(
            uv,
            "run",
            "--isolated",
            "--no-project",
            "--no-config",
            "--offline",
            "--no-python-downloads",
            "--python",
            "3.12",
            "python",
        ),
        kind="uv",
        version=version,
    )


def discover_python312(
    *,
    platform_name: str = sys.platform,
    which: Callable[[str], str | None] = shutil.which,
    runner: CommandRunner = _run_command,
) -> PythonRuntime | None:
    """Find a usable Python runtime through uv or common platform launchers."""
    uv = which("uv")
    if uv is not None:
        runtime = _uv_runtime(uv, runner)
        if runtime is not None:
            return runtime

    candidates: list[tuple[tuple[str, ...], str]] = []
    direct_312 = which("python3.12")
    if direct_312 is not None:
        candidates.append(((direct_312,), "direct_python"))
    if platform_name == "win32":
        py_launcher = which("py")
        if py_launcher is not None:
            candidates.extend(
                (
                    ((py_launcher, "-3.12"), "windows_py_launcher"),
                    ((py_launcher, "-3"), "windows_py_launcher"),
                )
            )
        direct_names: tuple[str, ...] = ("python", "python3")
    else:
        direct_names = ("python3",)
    for name in direct_names:
        candidate = which(name)
        if candidate is not None:
            candidates.append(((candidate,), "direct_python"))

    seen: set[tuple[str, ...]] = set()
    for command, kind in candidates:
        if command in seen:
            continue
        seen.add(command)
        version = _probe_version(command, runner)
        if version is not None and version >= (3, 12, 0):
            return PythonRuntime(command=command, kind=kind, version=version)
    return None

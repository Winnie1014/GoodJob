from __future__ import annotations

import ctypes
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from goodjob.paths import DataPaths
from goodjob.platform.handles_windows import OwnedHandle, last_error

_GENERIC_READ = 0x80000000
_GENERIC_WRITE = 0x40000000


@pytest.fixture
def data_paths(tmp_path: Path) -> DataPaths:
    return DataPaths.from_argument(str(tmp_path / "goodjob-data"))


@pytest.fixture
def exclusive_outside_sentinel(tmp_path: Path) -> Iterator[Path]:
    """Keep the sentinel inaccessible to runtime opens during native Windows tests."""
    sentinel = tmp_path / "outside-sentinel.txt"
    sentinel.write_bytes(b"outside")
    owner: OwnedHandle | None = None
    if sys.platform == "win32":
        from goodjob.platform import fs_windows

        raw = fs_windows._kernel32().CreateFileW(
            str(sentinel),
            _GENERIC_READ | _GENERIC_WRITE | fs_windows.DELETE,
            0,
            None,
            fs_windows.OPEN_EXISTING,
            fs_windows.FILE_ATTRIBUTE_NORMAL,
            None,
        )
        invalid = ctypes.c_void_p(-1).value
        if not raw or int(raw) == invalid:
            raise OSError(last_error(), "CreateFileW(outside sentinel)")
        owner = OwnedHandle(int(raw))
        with pytest.raises(OSError):
            sentinel.read_bytes()
        with pytest.raises(OSError):
            sentinel.write_bytes(b"changed")
        with pytest.raises(OSError):
            sentinel.unlink()
    try:
        yield sentinel
    finally:
        if owner is not None:
            owner.close()
    assert sentinel.read_bytes() == b"outside"


def _git_sandbox_available() -> bool:
    if sys.platform == "darwin":
        from goodjob.platform.sandbox_macos import SANDBOX_EXECUTABLE

        return SANDBOX_EXECUTABLE.is_file()
    if sys.platform.startswith("linux"):
        from goodjob.platform.sandbox_linux import BwrapSandbox

        return BwrapSandbox().is_available()
    return False


@pytest.fixture
def git_sandbox_available() -> None:
    if not _git_sandbox_available():
        pytest.skip("no supported Git sandbox backend available on this platform")

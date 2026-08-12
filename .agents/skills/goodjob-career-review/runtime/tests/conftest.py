from __future__ import annotations

import sys
from pathlib import Path

import pytest

from goodjob.paths import DataPaths


@pytest.fixture
def data_paths(tmp_path: Path) -> DataPaths:
    return DataPaths.from_argument(str(tmp_path / "goodjob-data"))


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

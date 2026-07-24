from __future__ import annotations

from pathlib import Path

import pytest

from goodjob.paths import DataPaths


@pytest.fixture
def data_paths(tmp_path: Path) -> DataPaths:
    return DataPaths.from_argument(str(tmp_path / "goodjob-data"))

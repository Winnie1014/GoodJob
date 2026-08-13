from __future__ import annotations

import sys
from pathlib import Path

import pytest

from goodjob.paths import DataPaths


def _set_home(monkeypatch: pytest.MonkeyPatch, home: Path) -> None:
    monkeypatch.setattr(Path, "home", lambda: home)


def test_default_data_dir_preserves_an_existing_legacy_location(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_home(monkeypatch, tmp_path)
    legacy = tmp_path / ".codex" / "goodjob-career-review"
    legacy.mkdir(parents=True)
    monkeypatch.setattr(sys, "platform", "linux")

    assert DataPaths.from_argument(None).root == legacy


def test_default_data_dir_uses_linux_user_data_location(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_home(monkeypatch, tmp_path)
    monkeypatch.setattr(sys, "platform", "linux")

    assert DataPaths.from_argument(None).root == (
        tmp_path / ".local" / "share" / "goodjob-career-review"
    )


def test_default_data_dir_uses_windows_local_app_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_home(monkeypatch, tmp_path)
    local_app_data = tmp_path / "AppData" / "Local"
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", str(local_app_data))

    assert DataPaths.from_argument(None).root == local_app_data / "goodjob-career-review"


def test_default_data_dir_keeps_macos_location(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_home(monkeypatch, tmp_path)
    monkeypatch.setattr(sys, "platform", "darwin")

    assert DataPaths.from_argument(None).root == tmp_path / ".codex" / "goodjob-career-review"

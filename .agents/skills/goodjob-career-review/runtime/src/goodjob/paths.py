"""Personal-data directory management for the installed Skill."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

CONFIG_TEMPLATE = "[goodjob]\nconfig_version = 1\n"


def _default_data_dir() -> Path:
    legacy_data_dir = Path.home() / ".codex" / "goodjob-career-review"
    if legacy_data_dir.exists():
        return legacy_data_dir
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA")
        return (
            Path(base) / "goodjob-career-review" if base else Path.home() / "goodjob-career-review"
        )
    if sys.platform.startswith("linux"):
        return Path.home() / ".local" / "share" / "goodjob-career-review"
    return legacy_data_dir


@dataclass(frozen=True)
class DataPaths:
    """The immutable layout root for all owner-specific GoodJob state."""

    root: Path

    def __post_init__(self) -> None:
        object.__setattr__(self, "root", self.root.expanduser().resolve(strict=False))

    @classmethod
    def from_argument(cls, raw_path: str | None) -> DataPaths:
        path = Path(raw_path).expanduser() if raw_path else _default_data_dir()
        return cls(path)

    @property
    def config_file(self) -> Path:
        return self.root / "config.toml"

    @property
    def database_file(self) -> Path:
        return self.root / "goodjob.sqlite3"

    @property
    def artifacts_dir(self) -> Path:
        return self.root / "artifacts"

    @property
    def artifact_tmp_dir(self) -> Path:
        return self.artifacts_dir / ".tmp"

    @property
    def latest_artifact_file(self) -> Path:
        return self.artifacts_dir / "latest.json"

    @property
    def exports_dir(self) -> Path:
        return self.root / "exports"

    @property
    def export_tmp_dir(self) -> Path:
        return self.exports_dir / ".tmp"

    @property
    def drafts_dir(self) -> Path:
        return self.root / "drafts"

    @property
    def locks_dir(self) -> Path:
        return self.root / "locks"

    @property
    def writer_lock_file(self) -> Path:
        return self.locks_dir / "writer.lock"

    def ensure_layout(self) -> None:
        """Create the fixed layout without placing any personal data in the Skill tree."""
        for directory in (
            self.root,
            self.artifacts_dir,
            self.artifact_tmp_dir,
            self.exports_dir,
            self.export_tmp_dir,
            self.drafts_dir,
            self.locks_dir,
        ):
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        if not self.config_file.exists():
            self.config_file.write_text(CONFIG_TEMPLATE, encoding="utf-8")

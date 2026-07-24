from __future__ import annotations

import sqlite3

import pytest

from goodjob.db import Database
from goodjob.errors import WriterBusyError
from goodjob.locks import ExclusiveWriterLock
from goodjob.paths import DataPaths


def test_migration_creates_stable_owner_layout(data_paths: DataPaths) -> None:
    version = Database(data_paths).migrate()

    assert version == 1
    assert data_paths.config_file.read_text(encoding="utf-8") == "[goodjob]\nconfig_version = 1\n"
    assert data_paths.artifacts_dir.is_dir()
    assert data_paths.export_tmp_dir.is_dir()
    assert data_paths.drafts_dir.is_dir()
    connection = sqlite3.connect(data_paths.database_file)
    tables = {
        row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    connection.close()
    assert {"schema_migrations", "authorization_receipts"} <= tables
    assert Database(data_paths).migrate() == 1


def test_writer_lock_never_steals_an_active_lock(data_paths: DataPaths) -> None:
    data_paths.ensure_layout()
    with (
        ExclusiveWriterLock(data_paths.writer_lock_file),
        pytest.raises(WriterBusyError),
        ExclusiveWriterLock(data_paths.writer_lock_file),
    ):
        pass

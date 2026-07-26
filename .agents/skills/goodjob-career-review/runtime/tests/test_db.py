from __future__ import annotations

import sqlite3

import pytest

from goodjob.db import MIGRATIONS, Database
from goodjob.errors import WriterBusyError
from goodjob.locks import ExclusiveWriterLock
from goodjob.paths import DataPaths


def test_migration_creates_stable_owner_layout(data_paths: DataPaths) -> None:
    version = Database(data_paths).migrate()

    assert version == 5
    assert data_paths.config_file.read_text(encoding="utf-8") == "[goodjob]\nconfig_version = 1\n"
    assert data_paths.artifacts_dir.is_dir()
    assert data_paths.export_tmp_dir.is_dir()
    assert data_paths.drafts_dir.is_dir()
    connection = sqlite3.connect(data_paths.database_file)
    tables = {
        row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    observation_columns = {
        row[1] for row in connection.execute("PRAGMA table_info(worktree_observations)")
    }
    source_revision_columns = {
        row[1] for row in connection.execute("PRAGMA table_info(source_revisions)")
    }
    connection.close()
    assert {
        "schema_migrations",
        "authorization_receipts",
        "scan_runs",
        "evidence",
        "job_inputs",
        "role_lenses",
        "preparation_runs",
        "preparation_run_projects",
        "preparation_source_checks",
        "preparation_source_mismatches",
        "scan_run_overviews",
    } <= tables
    assert {
        "history_basis",
        "external_git_dir",
        "external_common_dir",
        "external_metadata_receipt_id",
    } <= observation_columns
    assert {
        "adapter_id",
        "adapter_version",
        "config_revision",
        "analysis_diagnostics",
    } <= source_revision_columns
    assert Database(data_paths).migrate() == 5


def test_writer_lock_never_steals_an_active_lock(data_paths: DataPaths) -> None:
    data_paths.ensure_layout()
    with (
        ExclusiveWriterLock(data_paths.writer_lock_file),
        pytest.raises(WriterBusyError),
        ExclusiveWriterLock(data_paths.writer_lock_file),
    ):
        pass


def test_writer_busy_performs_no_personal_data_initialization(data_paths: DataPaths) -> None:
    with (
        ExclusiveWriterLock(data_paths.writer_lock_file),
        pytest.raises(WriterBusyError),
    ):
        Database(data_paths).migrate()

    assert not data_paths.config_file.exists()
    assert not data_paths.artifacts_dir.exists()
    assert not data_paths.database_file.exists()


def test_migration_upgrades_v4_without_rewriting_existing_scan_schema(
    data_paths: DataPaths,
) -> None:
    data_paths.ensure_layout()
    connection = sqlite3.connect(data_paths.database_file)
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute(
        """
        CREATE TABLE schema_migrations (
            version INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            applied_at TEXT NOT NULL
        )
        """
    )
    for migration in MIGRATIONS[:4]:
        for statement in migration.statements:
            connection.execute(statement)
        connection.execute(
            "INSERT INTO schema_migrations(version, name, applied_at) VALUES (?, ?, ?)",
            (migration.version, migration.name, "2026-07-26T00:00:00Z"),
        )
    connection.execute(
        """
        INSERT INTO authorization_receipts(
            authorization_receipt_id, receipt_kind, session_binding_digest, issuer_kind,
            scope_descriptor, notice_version, confirmed_at
        ) VALUES ('receipt-v4', 'source_analysis', X'00', 'codex_task_runtime',
                  '{}', 'notice-v1', '2026-07-26T00:00:00Z')
        """
    )
    connection.commit()
    connection.close()

    assert Database(data_paths).migrate() == 5
    upgraded = sqlite3.connect(data_paths.database_file)
    receipt = upgraded.execute(
        "SELECT authorization_receipt_id FROM authorization_receipts"
    ).fetchone()
    preparation_table = upgraded.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'preparation_runs'"
    ).fetchone()
    upgraded.close()
    assert receipt == ("receipt-v4",)
    assert preparation_table == ("preparation_runs",)

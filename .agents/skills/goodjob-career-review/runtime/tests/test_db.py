from __future__ import annotations

import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

import pytest

from goodjob.db import MIGRATIONS, Database
from goodjob.errors import WriterBusyError
from goodjob.locks import ExclusiveWriterLock
from goodjob.paths import DataPaths


def test_data_paths_canonicalize_a_directly_constructed_root(tmp_path: Path) -> None:
    target = tmp_path / "canonical-data"
    target.mkdir()
    alias = tmp_path / "data-alias"
    alias.symlink_to(target, target_is_directory=True)

    paths = DataPaths(alias / "nested")

    assert paths.root == target / "nested"


def test_migration_creates_stable_owner_layout(data_paths: DataPaths) -> None:
    version = Database(data_paths).migrate()

    assert version == 10
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
    preparation_columns = {
        row[1] for row in connection.execute("PRAGMA table_info(preparation_runs)")
    }
    interview_review_columns = {
        row[1] for row in connection.execute("PRAGMA table_info(interview_reviews)")
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
        "context_interviews",
        "context_answers",
        "project_context_facts",
        "analysis_commits",
        "claims",
        "claim_revisions",
        "claim_evidence",
        "preparation_claims",
        "knowledge_gaps",
        "project_assessments",
        "render_attempts",
        "artifact_snapshots",
        "review_targets",
        "review_target_bindings",
        "interview_reviews",
        "export_attempts",
        "derived_exports",
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
    assert {"review_lineage_sequence", "review_cutoff_sequence"} <= preparation_columns
    assert "review_sequence" in interview_review_columns
    assert Database(data_paths).migrate() == 10


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


def test_each_writer_context_holds_one_lock_across_migration_recovery_and_business_write(
    data_paths: DataPaths,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    acquisitions: list[Path] = []

    @contextmanager
    def counting_lock(path: Path) -> Generator[ExclusiveWriterLock, None, None]:
        acquisitions.append(path)
        with ExclusiveWriterLock(path) as lock:
            yield lock

    monkeypatch.setattr("goodjob.db.ExclusiveWriterLock", counting_lock)
    database = Database(data_paths)

    with database.write_transaction() as connection:
        connection.execute("SELECT 1")
    assert acquisitions == [data_paths.writer_lock_file]

    acquisitions.clear()
    with database.exclusive_writer_connection() as connection:
        connection.execute("SELECT 1")
    assert acquisitions == [data_paths.writer_lock_file]


def test_v9_migration_conservatively_backfills_review_order_and_cutoffs(
    data_paths: DataPaths,
) -> None:
    data_paths.ensure_layout()
    connection = sqlite3.connect(data_paths.database_file)
    connection.execute(
        """
        CREATE TABLE schema_migrations (
            version INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            applied_at TEXT NOT NULL
        )
        """
    )
    connection.execute(
        """
        INSERT INTO schema_migrations(version, name, applied_at)
        VALUES (8, 'review_state_lineage', '2026-07-27T00:00:00.000000Z')
        """
    )
    connection.execute(
        """
        CREATE TABLE preparation_runs (
            preparation_run_id TEXT PRIMARY KEY,
            started_at TEXT NOT NULL
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE interview_reviews (
            review_id TEXT PRIMARY KEY,
            created_at TEXT NOT NULL
        )
        """
    )
    connection.execute(
        """
        INSERT INTO interview_reviews(review_id, created_at)
        VALUES ('review-1', '2026-07-27T12:00:00.123001Z')
        """
    )
    connection.execute(
        """
        INSERT INTO preparation_runs(preparation_run_id, started_at)
        VALUES ('run-1', '2026-07-27T12:00:00.123100Z')
        """
    )
    connection.execute(
        """
        INSERT INTO interview_reviews(review_id, created_at)
        VALUES ('review-2', '2026-07-27T12:00:00.123200Z')
        """
    )
    connection.execute(
        """
        INSERT INTO preparation_runs(preparation_run_id, started_at)
        VALUES ('run-2', '2026-07-27T12:00:00.123400Z')
        """
    )
    connection.execute(
        """
        INSERT INTO interview_reviews(review_id, created_at)
        VALUES ('review-3', '2026-07-27T12:00:00.123400Z')
        """
    )
    connection.execute(
        """
        INSERT INTO preparation_runs(preparation_run_id, started_at)
        VALUES ('run-3', '2026-07-27T12:00:00.123400Z')
        """
    )
    connection.commit()
    connection.close()

    assert Database(data_paths).migrate() == 10
    upgraded = sqlite3.connect(data_paths.database_file)
    runs = upgraded.execute(
        """
        SELECT preparation_run_id, review_lineage_sequence, review_cutoff_sequence
        FROM preparation_runs ORDER BY review_lineage_sequence
        """
    ).fetchall()
    reviews = upgraded.execute(
        """
        SELECT review_id, review_sequence
        FROM interview_reviews ORDER BY review_sequence
        """
    ).fetchall()
    with pytest.raises(sqlite3.IntegrityError, match="review_lineage_sequence"):
        upgraded.execute(
            """
            INSERT INTO preparation_runs(preparation_run_id, started_at)
            VALUES ('missing-sequence', '2026-07-27T13:00:00.000000Z')
            """
        )
    with pytest.raises(sqlite3.IntegrityError, match="review_sequence"):
        upgraded.execute(
            """
            INSERT INTO interview_reviews(review_id, created_at)
            VALUES ('missing-sequence', '2026-07-27T13:00:00.000000Z')
            """
        )
    upgraded.close()

    assert runs == [
        ("run-1", 1, 1),
        ("run-2", 2, 2),
        ("run-3", 3, 2),
    ]
    assert reviews == [("review-1", 1), ("review-2", 2), ("review-3", 3)]


def test_v7_migration_marks_populated_v6_claim_attribution_unknown(
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
    for migration in MIGRATIONS[:6]:
        for statement in migration.statements:
            connection.execute(statement)
        connection.execute(
            "INSERT INTO schema_migrations(version, name, applied_at) VALUES (?, ?, ?)",
            (migration.version, migration.name, "2026-07-26T00:00:00Z"),
        )
    connection.execute(
        """
        INSERT INTO projects(
            project_id, identity_kind, identity_key, display_name, first_seen_at
        ) VALUES ('legacy-project', 'non_git_root', '/legacy', 'legacy',
                  '2026-07-26T00:00:00Z')
        """
    )
    connection.execute(
        """
        INSERT INTO claims(
            claim_id, identity_sha256, claim_key, category, scope_kind,
            project_id, worktree_id, module_id, created_at
        ) VALUES ('legacy-claim', ?, 'legacy-contribution', 'contribution', 'project',
                  'legacy-project', NULL, NULL, '2026-07-26T00:00:00Z')
        """,
        ("a" * 64,),
    )
    connection.execute(
        """
        INSERT INTO claim_revisions(
            claim_revision_id, claim_id, revision_no, revision_sha256,
            statement, statement_tokens, facets, support_level,
            review_semantic_projection, review_semantic_sha256,
            supersedes_id, created_at
        ) VALUES ('legacy-revision', 'legacy-claim', 1, ?, '我实现了旧功能',
                  '[{"kind":"text","value":"我实现了旧功能"}]',
                  '["implemented"]', 'single_source', '{}', ?, NULL,
                  '2026-07-26T00:00:00Z')
        """,
        ("b" * 64, "c" * 64),
    )
    connection.commit()
    connection.close()

    assert Database(data_paths).migrate() == 10
    upgraded = sqlite3.connect(data_paths.database_file)
    attribution = upgraded.execute(
        "SELECT personal_attribution FROM claim_revisions WHERE claim_revision_id = ?",
        ("legacy-revision",),
    ).fetchone()
    upgraded.close()

    assert attribution == ("legacy_unknown",)


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

    assert Database(data_paths).migrate() == 10
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


def test_migration_upgrades_populated_v5_without_losing_preparation_evidence(
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
    for migration in MIGRATIONS[:5]:
        for statement in migration.statements:
            connection.execute(statement)
        connection.execute(
            "INSERT INTO schema_migrations(version, name, applied_at) VALUES (?, ?, ?)",
            (migration.version, migration.name, "2026-07-26T00:00:00Z"),
        )
    timestamp = "2026-07-26T01:00:00Z"
    connection.execute(
        """
        INSERT INTO authorization_receipts(
            authorization_receipt_id, receipt_kind, session_binding_digest, issuer_kind,
            scope_descriptor, notice_version, confirmed_at
        ) VALUES ('receipt-v5', 'source_analysis', X'01', 'codex_task_runtime',
                  '{"workspace_path":"/workspace-v5"}', 'notice-v1', ?)
        """,
        (timestamp,),
    )
    connection.execute(
        """
        INSERT INTO workspaces(
            workspace_id, canonical_root, display_name, registered_at, config_revision
        ) VALUES ('workspace-v5', '/workspace-v5', 'workspace-v5', ?, 'config-v1')
        """,
        (timestamp,),
    )
    connection.execute(
        """
        INSERT INTO projects(
            project_id, identity_kind, identity_key, display_name, first_seen_at
        ) VALUES ('project-v5', 'non_git_root', '/workspace-v5', 'project-v5', ?)
        """,
        (timestamp,),
    )
    connection.execute(
        """
        INSERT INTO workspace_projects(
            workspace_id, project_id, relative_location, first_seen_run_id
        ) VALUES ('workspace-v5', 'project-v5', '.', 'scan-v5')
        """
    )
    connection.execute(
        """
        INSERT INTO worktrees(worktree_id, project_id, canonical_root, git_dir)
        VALUES ('worktree-v5', 'project-v5', '/workspace-v5', NULL)
        """
    )
    connection.execute(
        """
        INSERT INTO scan_runs(
            scan_run_id, workspace_id, authorization_receipt_id, owner_process_identity,
            mode, change_detection_mode, config_revision, started_at, finished_at, status
        ) VALUES ('scan-v5', 'workspace-v5', 'receipt-v5', 'migration-test',
                  'full', NULL, 'config-v1', ?, ?, 'completed')
        """,
        (timestamp, timestamp),
    )
    connection.execute(
        """
        INSERT INTO source_artifacts(
            artifact_id, project_id, worktree_id, relative_path, artifact_kind,
            supersedes_artifact_id
        ) VALUES ('artifact-v5', 'project-v5', 'worktree-v5', 'app.py', 'source', NULL)
        """
    )
    connection.execute(
        """
        INSERT INTO source_revisions(
            source_revision_id, artifact_id, content_sha256, byte_size,
            analysis_fingerprint, observed_at, adapter_id, adapter_version,
            config_revision, analysis_diagnostics
        ) VALUES ('revision-v5', 'artifact-v5', ?, 10, 'fingerprint-v5', ?,
                  'python', '1', 'config-v1', '[]')
        """,
        ("a" * 64, timestamp),
    )
    connection.execute(
        """
        INSERT INTO project_snapshots(
            project_snapshot_id, project_id, scan_run_id, created_at, coverage_status
        ) VALUES ('snapshot-v5', 'project-v5', 'scan-v5', ?, 'complete')
        """,
        (timestamp,),
    )
    connection.execute(
        """
        INSERT INTO scan_run_projects(
            scan_run_id, project_id, snapshot_disposition, project_snapshot_id
        ) VALUES ('scan-v5', 'project-v5', 'fresh', 'snapshot-v5')
        """
    )
    connection.execute(
        """
        INSERT INTO project_snapshot_source_revisions(project_snapshot_id, source_revision_id)
        VALUES ('snapshot-v5', 'revision-v5')
        """
    )
    connection.execute(
        """
        INSERT INTO evidence(
            evidence_id, project_id, acquisition_scope, project_snapshot_id, module_id,
            source_revision_id, content_equivalence_key, origin_kind, evidence_kind,
            locator, summary, commit_state, created_at
        ) VALUES ('evidence-v5', 'project-v5', 'scan', 'snapshot-v5', NULL,
                  'revision-v5', 'equivalence-v5', 'source_revision', 'implementation',
                  '{}', 'Existing v5 implementation Evidence.', 'committed', ?)
        """,
        (timestamp,),
    )
    connection.execute(
        """
        INSERT INTO project_snapshot_evidence(project_snapshot_id, evidence_id)
        VALUES ('snapshot-v5', 'evidence-v5')
        """
    )
    connection.execute(
        """
        INSERT INTO job_inputs(
            job_input_id, role_name, jd_input_kind, jd_text, jd_source_path,
            jd_content_sha256, inferred_level, level_override, primary_language, created_at
        ) VALUES ('job-v5', '应用软件工程师', 'none', NULL, NULL, NULL,
                  NULL, NULL, 'zh-CN', ?)
        """,
        (timestamp,),
    )
    connection.execute(
        """
        INSERT INTO role_lenses(
            role_lens_id, job_input_id, contract_version, dimensions,
            evidence_requirements, ranking_rules, output_sections, question_strategy,
            gap_rules, assumptions, generator_id, prompt_contract_version,
            lens_sha256, created_at
        ) VALUES ('lens-v5', 'job-v5', 'role-lens-v1', '[]', '[]', '[]', '[]', '{}',
                  '[]', '[]', 'migration-test', 'prompt-v1', ?, ?)
        """,
        ("b" * 64, timestamp),
    )
    connection.execute(
        """
        INSERT INTO preparation_runs(
            preparation_run_id, request_id, request_sha256, workspace_id, scan_run_id,
            role_lens_id, authorization_receipt_id, config_revision, requested_exports,
            evidence_limit_per_project, status, status_reason, started_at,
            last_transition_at, finished_at
        ) VALUES ('preparation-v5', 'request-v5', ?, 'workspace-v5', 'scan-v5',
                  'lens-v5', 'receipt-v5', 'config-v1', '[]', 100, 'analyzing', NULL,
                  ?, ?, NULL)
        """,
        ("c" * 64, timestamp, timestamp),
    )
    connection.execute(
        """
        INSERT INTO preparation_run_projects(
            preparation_run_id, project_id, project_snapshot_id, snapshot_disposition
        ) VALUES ('preparation-v5', 'project-v5', 'snapshot-v5', 'fresh')
        """
    )
    connection.execute(
        """
        INSERT INTO preparation_source_checks(
            source_check_id, preparation_run_id, source_revision_id, phase,
            expected_sha256, observed_at, status
        ) VALUES ('check-v5', 'preparation-v5', 'revision-v5', 'preflight', ?, ?, 'passed')
        """,
        ("a" * 64, timestamp),
    )
    connection.commit()
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    connection.close()

    assert Database(data_paths).migrate() == 10
    upgraded = sqlite3.connect(data_paths.database_file)
    preparation = upgraded.execute(
        "SELECT preparation_run_id, status FROM preparation_runs"
    ).fetchone()
    evidence = upgraded.execute(
        """
        SELECT evidence_id, source_revision_id, preparation_run_id, query_reason
        FROM evidence
        """
    ).fetchone()
    source_check = upgraded.execute(
        "SELECT source_check_id, status FROM preparation_source_checks"
    ).fetchone()
    foreign_key_failures = upgraded.execute("PRAGMA foreign_key_check").fetchall()
    upgraded.close()

    assert preparation == ("preparation-v5", "analyzing")
    assert evidence == ("evidence-v5", "revision-v5", None, None)
    assert source_check == ("check-v5", "passed")
    assert foreign_key_failures == []
    assert Database(data_paths).migrate() == 10
    final = sqlite3.connect(data_paths.database_file)
    assert final.execute("SELECT COUNT(*) FROM schema_migrations").fetchone() == (10,)
    assert final.execute("SELECT COUNT(*) FROM preparation_runs").fetchone() == (1,)
    assert final.execute("SELECT COUNT(*) FROM evidence").fetchone() == (1,)
    assert final.execute(
        """
        SELECT review_lineage_sequence, review_cutoff_sequence
        FROM preparation_runs
        """
    ).fetchone() == (1, 0)
    final.close()

"""SQLite migrations and transaction boundaries for owner-local state."""

from __future__ import annotations

import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime

from goodjob.errors import UnsupportedSchemaError
from goodjob.locks import ExclusiveWriterLock
from goodjob.paths import DataPaths


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    statements: tuple[str, ...]


MIGRATIONS: tuple[Migration, ...] = (
    Migration(
        version=1,
        name="authorization_receipts",
        statements=(
            """
            CREATE TABLE authorization_receipts (
                authorization_receipt_id TEXT PRIMARY KEY,
                receipt_kind TEXT NOT NULL CHECK (
                    receipt_kind IN (
                        'source_analysis',
                        'external_git_relation_probe',
                        'external_git_metadata'
                    )
                ),
                session_binding_digest BLOB NOT NULL,
                issuer_kind TEXT NOT NULL CHECK (issuer_kind = 'codex_task_runtime'),
                scope_descriptor TEXT NOT NULL,
                notice_version TEXT NOT NULL,
                confirmed_at TEXT NOT NULL
            )
            """,
            """
            CREATE INDEX authorization_receipts_scope_idx
            ON authorization_receipts(receipt_kind, scope_descriptor, notice_version)
            """,
        ),
    ),
    Migration(
        version=2,
        name="scan_evidence_graph",
        statements=(
            """
            CREATE TABLE workspaces (
                workspace_id TEXT PRIMARY KEY,
                canonical_root TEXT NOT NULL UNIQUE,
                display_name TEXT NOT NULL,
                registered_at TEXT NOT NULL,
                config_revision TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE projects (
                project_id TEXT PRIMARY KEY,
                identity_kind TEXT NOT NULL CHECK (
                    identity_kind IN ('git_common_dir', 'non_git_root')
                ),
                identity_key TEXT NOT NULL UNIQUE,
                display_name TEXT NOT NULL,
                first_seen_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE workspace_projects (
                workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id),
                project_id TEXT NOT NULL REFERENCES projects(project_id),
                relative_location TEXT NOT NULL,
                first_seen_run_id TEXT NOT NULL,
                PRIMARY KEY (workspace_id, project_id)
            )
            """,
            """
            CREATE TABLE worktrees (
                worktree_id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL REFERENCES projects(project_id),
                canonical_root TEXT NOT NULL,
                git_dir TEXT,
                UNIQUE(project_id, canonical_root)
            )
            """,
            """
            CREATE TABLE scan_runs (
                scan_run_id TEXT PRIMARY KEY,
                workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id),
                authorization_receipt_id TEXT NOT NULL REFERENCES
                    authorization_receipts(authorization_receipt_id),
                owner_process_identity TEXT NOT NULL,
                mode TEXT NOT NULL CHECK (mode IN ('full', 'refresh')),
                change_detection_mode TEXT CHECK (
                    change_detection_mode IN ('fast', 'verify_content')
                    OR change_detection_mode IS NULL
                ),
                config_revision TEXT NOT NULL,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                status TEXT NOT NULL CHECK (
                    status IN ('running', 'completed', 'partial', 'failed', 'interrupted')
                )
            )
            """,
            """
            CREATE TABLE worktree_observations (
                worktree_id TEXT NOT NULL REFERENCES worktrees(worktree_id),
                scan_run_id TEXT NOT NULL REFERENCES scan_runs(scan_run_id),
                branch TEXT,
                head_commit TEXT,
                dirty_state TEXT NOT NULL CHECK (
                    dirty_state IN ('clean', 'modified', 'untracked', 'mixed', 'not_applicable')
                ),
                history_basis TEXT NOT NULL,
                external_git_dir TEXT,
                external_common_dir TEXT,
                external_metadata_receipt_id TEXT REFERENCES
                    authorization_receipts(authorization_receipt_id),
                external_metadata_confirmed_at TEXT,
                external_metadata_read_fields TEXT,
                observed_at TEXT NOT NULL,
                PRIMARY KEY (worktree_id, scan_run_id)
            )
            """,
            """
            CREATE TABLE modules (
                module_id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL REFERENCES projects(project_id),
                module_key TEXT NOT NULL,
                name TEXT NOT NULL,
                kind TEXT NOT NULL,
                UNIQUE(project_id, module_key)
            )
            """,
            """
            CREATE TABLE source_artifacts (
                artifact_id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL REFERENCES projects(project_id),
                worktree_id TEXT NOT NULL REFERENCES worktrees(worktree_id),
                relative_path TEXT NOT NULL,
                artifact_kind TEXT NOT NULL,
                supersedes_artifact_id TEXT REFERENCES source_artifacts(artifact_id),
                UNIQUE(project_id, worktree_id, relative_path)
            )
            """,
            """
            CREATE TABLE source_revisions (
                source_revision_id TEXT PRIMARY KEY,
                artifact_id TEXT NOT NULL REFERENCES source_artifacts(artifact_id),
                content_sha256 TEXT NOT NULL,
                byte_size INTEGER NOT NULL CHECK (byte_size >= 0),
                analysis_fingerprint TEXT NOT NULL,
                observed_at TEXT NOT NULL,
                UNIQUE(artifact_id, analysis_fingerprint)
            )
            """,
            """
            CREATE TABLE project_snapshots (
                project_snapshot_id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL REFERENCES projects(project_id),
                scan_run_id TEXT NOT NULL REFERENCES scan_runs(scan_run_id),
                created_at TEXT NOT NULL,
                coverage_status TEXT NOT NULL CHECK (coverage_status IN ('complete', 'partial')),
                UNIQUE(project_id, scan_run_id)
            )
            """,
            """
            CREATE TABLE scan_run_projects (
                scan_run_id TEXT NOT NULL REFERENCES scan_runs(scan_run_id),
                project_id TEXT NOT NULL REFERENCES projects(project_id),
                snapshot_disposition TEXT NOT NULL CHECK (
                    snapshot_disposition IN (
                        'fresh', 'carried_forward', 'failed_no_baseline', 'excluded'
                    )
                ),
                project_snapshot_id TEXT REFERENCES project_snapshots(project_snapshot_id),
                PRIMARY KEY (scan_run_id, project_id)
            )
            """,
            """
            CREATE TABLE module_observations (
                module_id TEXT NOT NULL REFERENCES modules(module_id),
                project_snapshot_id TEXT NOT NULL REFERENCES project_snapshots(project_snapshot_id),
                relative_root TEXT NOT NULL,
                manifest_evidence_id TEXT,
                adapter_id TEXT NOT NULL,
                PRIMARY KEY (module_id, project_snapshot_id)
            )
            """,
            """
            CREATE TABLE project_snapshot_source_revisions (
                project_snapshot_id TEXT NOT NULL REFERENCES project_snapshots(project_snapshot_id),
                source_revision_id TEXT NOT NULL REFERENCES source_revisions(source_revision_id),
                PRIMARY KEY (project_snapshot_id, source_revision_id)
            )
            """,
            """
            CREATE TABLE artifact_observations (
                project_snapshot_id TEXT NOT NULL REFERENCES project_snapshots(project_snapshot_id),
                artifact_id TEXT NOT NULL REFERENCES source_artifacts(artifact_id),
                source_revision_id TEXT NOT NULL REFERENCES source_revisions(source_revision_id),
                byte_size INTEGER NOT NULL CHECK (byte_size >= 0),
                mtime_ns INTEGER NOT NULL,
                PRIMARY KEY (project_snapshot_id, artifact_id)
            )
            """,
            """
            CREATE TABLE evidence (
                evidence_id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL REFERENCES projects(project_id),
                acquisition_scope TEXT NOT NULL CHECK (
                    acquisition_scope IN ('scan', 'preparation', 'context')
                ),
                project_snapshot_id TEXT REFERENCES project_snapshots(project_snapshot_id),
                module_id TEXT REFERENCES modules(module_id),
                source_revision_id TEXT REFERENCES source_revisions(source_revision_id),
                content_equivalence_key TEXT,
                origin_kind TEXT NOT NULL CHECK (
                    origin_kind IN ('source_revision', 'git_commit', 'context_fact')
                ),
                evidence_kind TEXT NOT NULL,
                locator TEXT NOT NULL,
                summary TEXT NOT NULL CHECK (length(summary) <= 500),
                commit_state TEXT NOT NULL CHECK (
                    commit_state IN (
                        'committed', 'modified', 'untracked', 'historical', 'not_applicable'
                    )
                ),
                created_at TEXT NOT NULL,
                UNIQUE(source_revision_id, evidence_kind, locator, commit_state)
            )
            """,
            """
            CREATE TABLE project_snapshot_evidence (
                project_snapshot_id TEXT NOT NULL REFERENCES project_snapshots(project_snapshot_id),
                evidence_id TEXT NOT NULL REFERENCES evidence(evidence_id),
                PRIMARY KEY (project_snapshot_id, evidence_id)
            )
            """,
            """
            CREATE TABLE evidence_validities (
                scan_run_id TEXT NOT NULL REFERENCES scan_runs(scan_run_id),
                evidence_id TEXT NOT NULL REFERENCES evidence(evidence_id),
                validity TEXT NOT NULL CHECK (validity IN ('current', 'stale', 'missing')),
                replacement_evidence_id TEXT REFERENCES evidence(evidence_id),
                resolved_at TEXT NOT NULL,
                PRIMARY KEY (scan_run_id, evidence_id)
            )
            """,
            """
            CREATE TABLE scan_issues (
                issue_id TEXT PRIMARY KEY,
                scan_run_id TEXT NOT NULL REFERENCES scan_runs(scan_run_id),
                project_id TEXT REFERENCES projects(project_id),
                artifact_id TEXT REFERENCES source_artifacts(artifact_id),
                kind TEXT NOT NULL,
                severity TEXT NOT NULL CHECK (severity IN ('info', 'warning', 'error')),
                relative_path TEXT,
                message TEXT NOT NULL CHECK (length(message) <= 500),
                remediation TEXT NOT NULL CHECK (length(remediation) <= 500)
            )
            """,
            (
                "CREATE INDEX scan_runs_workspace_status_idx "
                "ON scan_runs(workspace_id, status, started_at)"
            ),
            (
                "CREATE INDEX source_revisions_artifact_idx "
                "ON source_revisions(artifact_id, observed_at)"
            ),
            (
                "CREATE INDEX evidence_project_snapshot_idx "
                "ON evidence(project_id, project_snapshot_id)"
            ),
            "CREATE INDEX scan_issues_run_idx ON scan_issues(scan_run_id)",
        ),
    ),
    Migration(
        version=3,
        name="versioned_source_analysis",
        statements=(
            ("ALTER TABLE source_revisions ADD COLUMN adapter_id TEXT NOT NULL DEFAULT 'legacy'"),
            (
                "ALTER TABLE source_revisions ADD COLUMN adapter_version TEXT NOT NULL "
                "DEFAULT 'legacy'"
            ),
            (
                "ALTER TABLE source_revisions ADD COLUMN config_revision TEXT NOT NULL "
                "DEFAULT 'legacy'"
            ),
            (
                "CREATE INDEX source_revisions_analysis_version_idx "
                "ON source_revisions(adapter_id, adapter_version, config_revision)"
            ),
        ),
    ),
    Migration(
        version=4,
        name="source_analysis_diagnostics",
        statements=(
            (
                "ALTER TABLE source_revisions ADD COLUMN analysis_diagnostics TEXT "
                "NOT NULL DEFAULT '[]'"
            ),
        ),
    ),
    Migration(
        version=5,
        name="role_oriented_preparation",
        statements=(
            """
            CREATE TABLE job_inputs (
                job_input_id TEXT PRIMARY KEY,
                role_name TEXT NOT NULL,
                jd_input_kind TEXT NOT NULL CHECK (
                    jd_input_kind IN ('none', 'text', 'file', 'continue_without_jd')
                ),
                jd_text TEXT,
                jd_source_path TEXT,
                jd_content_sha256 TEXT,
                inferred_level TEXT,
                level_override TEXT,
                primary_language TEXT NOT NULL CHECK (primary_language = 'zh-CN'),
                created_at TEXT NOT NULL,
                CHECK (
                    (jd_input_kind IN ('none', 'continue_without_jd')
                        AND jd_text IS NULL
                        AND jd_source_path IS NULL
                        AND jd_content_sha256 IS NULL)
                    OR (jd_input_kind = 'text'
                        AND jd_text IS NOT NULL
                        AND jd_source_path IS NULL
                        AND jd_content_sha256 IS NOT NULL)
                    OR (jd_input_kind = 'file'
                        AND jd_text IS NOT NULL
                        AND jd_source_path IS NOT NULL
                        AND jd_content_sha256 IS NOT NULL)
                )
            )
            """,
            """
            CREATE TABLE role_lenses (
                role_lens_id TEXT PRIMARY KEY,
                job_input_id TEXT NOT NULL REFERENCES job_inputs(job_input_id),
                contract_version TEXT NOT NULL,
                dimensions TEXT NOT NULL,
                evidence_requirements TEXT NOT NULL,
                ranking_rules TEXT NOT NULL,
                output_sections TEXT NOT NULL,
                question_strategy TEXT NOT NULL,
                gap_rules TEXT NOT NULL,
                assumptions TEXT NOT NULL,
                generator_id TEXT NOT NULL,
                prompt_contract_version TEXT NOT NULL,
                lens_sha256 TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE preparation_runs (
                preparation_run_id TEXT PRIMARY KEY,
                request_id TEXT NOT NULL UNIQUE,
                request_sha256 TEXT NOT NULL,
                workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id),
                scan_run_id TEXT NOT NULL REFERENCES scan_runs(scan_run_id),
                role_lens_id TEXT NOT NULL REFERENCES role_lenses(role_lens_id),
                authorization_receipt_id TEXT NOT NULL REFERENCES
                    authorization_receipts(authorization_receipt_id),
                config_revision TEXT NOT NULL,
                requested_exports TEXT NOT NULL,
                evidence_limit_per_project INTEGER NOT NULL CHECK (
                    evidence_limit_per_project BETWEEN 1 AND 200
                ),
                status TEXT NOT NULL CHECK (
                    status IN (
                        'collecting', 'awaiting_context', 'analyzing', 'ready',
                        'rendering', 'completed', 'partial', 'render_failed',
                        'refresh_required', 'failed', 'interrupted', 'cancelled'
                    )
                ),
                status_reason TEXT,
                started_at TEXT NOT NULL,
                last_transition_at TEXT NOT NULL,
                finished_at TEXT
            )
            """,
            """
            CREATE TABLE preparation_run_projects (
                preparation_run_id TEXT NOT NULL REFERENCES
                    preparation_runs(preparation_run_id),
                project_id TEXT NOT NULL REFERENCES projects(project_id),
                project_snapshot_id TEXT REFERENCES project_snapshots(project_snapshot_id),
                snapshot_disposition TEXT NOT NULL CHECK (
                    snapshot_disposition IN (
                        'fresh', 'carried_forward', 'failed_no_baseline', 'excluded'
                    )
                ),
                PRIMARY KEY (preparation_run_id, project_id),
                CHECK (
                    (snapshot_disposition IN ('fresh', 'carried_forward')
                        AND project_snapshot_id IS NOT NULL)
                    OR (snapshot_disposition IN ('failed_no_baseline', 'excluded')
                        AND project_snapshot_id IS NULL)
                )
            )
            """,
            """
            CREATE TABLE preparation_source_checks (
                source_check_id TEXT PRIMARY KEY,
                preparation_run_id TEXT NOT NULL REFERENCES
                    preparation_runs(preparation_run_id),
                source_revision_id TEXT NOT NULL REFERENCES
                    source_revisions(source_revision_id),
                phase TEXT NOT NULL CHECK (phase IN ('preflight', 'before_read', 'commit')),
                expected_sha256 TEXT NOT NULL,
                observed_at TEXT NOT NULL,
                status TEXT NOT NULL CHECK (status IN ('passed', 'mismatch'))
            )
            """,
            """
            CREATE TABLE preparation_source_mismatches (
                source_mismatch_id TEXT PRIMARY KEY,
                source_check_id TEXT NOT NULL UNIQUE REFERENCES
                    preparation_source_checks(source_check_id),
                mismatch_kind TEXT NOT NULL CHECK (
                    mismatch_kind IN ('missing', 'unreadable', 'sha256_mismatch')
                ),
                observed_sha256 TEXT,
                detected_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE scan_run_overviews (
                scan_run_id TEXT PRIMARY KEY REFERENCES scan_runs(scan_run_id),
                coverage_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """,
            """
            CREATE INDEX preparation_runs_workspace_status_idx
            ON preparation_runs(workspace_id, status, started_at)
            """,
            """
            CREATE INDEX preparation_run_projects_snapshot_idx
            ON preparation_run_projects(preparation_run_id, snapshot_disposition)
            """,
            """
            CREATE INDEX preparation_source_checks_run_phase_idx
            ON preparation_source_checks(preparation_run_id, phase, source_revision_id)
            """,
        ),
    ),
)


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


class Database:
    """Versioned SQLite access with one non-blocking writer at a time."""

    def __init__(self, paths: DataPaths) -> None:
        self.paths = paths

    def migrate(self) -> int:
        """Apply all known migrations, rejecting a database from a newer runtime."""
        with ExclusiveWriterLock(self.paths.writer_lock_file):
            self.paths.ensure_layout()
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS schema_migrations (
                        version INTEGER PRIMARY KEY,
                        name TEXT NOT NULL,
                        applied_at TEXT NOT NULL
                    )
                    """
                )
                row = connection.execute(
                    "SELECT COALESCE(MAX(version), 0) AS version FROM schema_migrations"
                ).fetchone()
                current_version = int(row["version"])
                latest_version = MIGRATIONS[-1].version
                if current_version > latest_version:
                    raise UnsupportedSchemaError(
                        "database schema is newer than this installed GoodJob version"
                    )
                for migration in MIGRATIONS:
                    if migration.version <= current_version:
                        continue
                    for statement in migration.statements:
                        connection.execute(statement)
                    connection.execute(
                        "INSERT INTO schema_migrations(version, name, applied_at) VALUES (?, ?, ?)",
                        (migration.version, migration.name, _utc_now()),
                    )
                    current_version = migration.version
                connection.commit()
                return current_version
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.close()

    @contextmanager
    def write_transaction(self) -> Generator[sqlite3.Connection, None, None]:
        """Open a short atomic write transaction after applying known schema changes."""
        self.migrate()
        with ExclusiveWriterLock(self.paths.writer_lock_file):
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                yield connection
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.close()

    @contextmanager
    def read_connection(self) -> Generator[sqlite3.Connection, None, None]:
        """Open a parallel read connection without acquiring the writer lock."""
        if not self.paths.database_file.exists():
            raise UnsupportedSchemaError("database is not initialized; run bootstrap first")
        connection = self._connect()
        try:
            try:
                row = connection.execute(
                    "SELECT COALESCE(MAX(version), 0) AS version FROM schema_migrations"
                ).fetchone()
            except sqlite3.OperationalError as exc:
                raise UnsupportedSchemaError(
                    "database is not initialized; run bootstrap first"
                ) from exc
            if int(row["version"]) != MIGRATIONS[-1].version:
                raise UnsupportedSchemaError("database must be migrated before this read operation")
            yield connection
        finally:
            connection.close()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.paths.database_file)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 0")
        return connection

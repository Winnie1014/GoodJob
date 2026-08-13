"""SQLite migrations and transaction boundaries for owner-local state."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Generator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from goodjob.errors import UnsupportedSchemaError
from goodjob.locks import ExclusiveWriterLock
from goodjob.paths import DataPaths
from goodjob.recovery import recover_interrupted_exports


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    statements: tuple[str, ...] = ()
    handler: Callable[[sqlite3.Connection, Path], None] | None = None


def _restore_database_from_backup(connection: sqlite3.Connection, backup_path: Path) -> None:
    backup_connection = sqlite3.connect(backup_path)
    try:
        backup_connection.backup(connection)
    finally:
        backup_connection.close()


def _migrate_v11_remove_issuer_kind_check(
    connection: sqlite3.Connection, database_file: Path
) -> None:
    """Remove the CHECK constraint on authorization_receipts.issuer_kind.

    Uses the 11-step FK-safe table rebuild because authorization_receipts is
    referenced by foreign keys from scan_runs, worktree_observations, and
    preparation_runs.  PRAGMA foreign_keys cannot be toggled inside a
    transaction, so this handler manages its own transaction lifecycle.
    """
    backup_path = database_file.parent / f"{database_file.name}.v10-backup"
    with suppress(FileNotFoundError):
        backup_path.unlink()
    quoted = str(backup_path).replace("'", "''")
    connection.execute(f"VACUUM INTO '{quoted}'")
    connection.execute("PRAGMA foreign_keys = OFF")
    transaction_active = False
    success = False
    try:
        connection.execute("BEGIN")
        transaction_active = True
        connection.execute(
            """
            CREATE TABLE authorization_receipts_new (
                authorization_receipt_id TEXT PRIMARY KEY,
                receipt_kind TEXT NOT NULL CHECK (
                    receipt_kind IN (
                        'source_analysis',
                        'external_git_relation_probe',
                        'external_git_metadata'
                    )
                ),
                session_binding_digest BLOB NOT NULL,
                issuer_kind TEXT NOT NULL,
                scope_descriptor TEXT NOT NULL,
                notice_version TEXT NOT NULL,
                confirmed_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            "INSERT INTO authorization_receipts_new SELECT * FROM authorization_receipts"
        )
        connection.execute("DROP TABLE authorization_receipts")
        connection.execute(
            "ALTER TABLE authorization_receipts_new RENAME TO authorization_receipts"
        )
        connection.execute(
            """
            CREATE INDEX authorization_receipts_scope_idx
            ON authorization_receipts(receipt_kind, scope_descriptor, notice_version)
            """
        )
        violations = connection.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            connection.execute("ROLLBACK")
            transaction_active = False
            raise UnsupportedSchemaError(
                "v11 migration failed foreign_key_check after table rebuild"
            )
        connection.execute(
            "INSERT INTO schema_migrations(version, name, applied_at) VALUES (?, ?, ?)",
            (11, "remove_issuer_kind_check", _utc_now()),
        )
        connection.execute("COMMIT")
        transaction_active = False
        success = True
    except Exception:
        if transaction_active:
            with suppress(sqlite3.OperationalError):
                connection.execute("ROLLBACK")
        try:
            _restore_database_from_backup(connection, backup_path)
        except sqlite3.Error as restore_error:
            raise UnsupportedSchemaError(
                "v11 migration failed and the backup could not be restored"
            ) from restore_error
        raise
    finally:
        connection.execute("PRAGMA foreign_keys = ON")
        if success:
            with suppress(FileNotFoundError):
                backup_path.unlink()


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
    Migration(
        version=6,
        name="context_and_atomic_analysis",
        statements=(
            (
                "ALTER TABLE evidence ADD COLUMN preparation_run_id TEXT "
                "REFERENCES preparation_runs(preparation_run_id)"
            ),
            "ALTER TABLE evidence ADD COLUMN query_reason TEXT",
            """
            CREATE TABLE context_interviews (
                context_interview_id TEXT PRIMARY KEY,
                request_id TEXT NOT NULL UNIQUE,
                request_sha256 TEXT NOT NULL,
                preparation_run_id TEXT NOT NULL UNIQUE REFERENCES
                    preparation_runs(preparation_run_id),
                question_set_version TEXT NOT NULL,
                status TEXT NOT NULL CHECK (status IN ('open', 'completed', 'cancelled')),
                created_at TEXT NOT NULL,
                completed_at TEXT
            )
            """,
            """
            CREATE TABLE context_question_cards (
                context_interview_id TEXT NOT NULL REFERENCES
                    context_interviews(context_interview_id),
                project_id TEXT NOT NULL REFERENCES projects(project_id),
                questions_json TEXT NOT NULL,
                PRIMARY KEY (context_interview_id, project_id)
            )
            """,
            """
            CREATE TABLE context_answer_batches (
                context_answer_batch_id TEXT PRIMARY KEY,
                request_id TEXT NOT NULL UNIQUE,
                request_sha256 TEXT NOT NULL,
                context_interview_id TEXT NOT NULL UNIQUE REFERENCES
                    context_interviews(context_interview_id),
                preparation_run_id TEXT NOT NULL UNIQUE REFERENCES
                    preparation_runs(preparation_run_id),
                committed_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE context_answers (
                answer_id TEXT PRIMARY KEY,
                context_answer_batch_id TEXT NOT NULL REFERENCES
                    context_answer_batches(context_answer_batch_id),
                project_id TEXT NOT NULL REFERENCES projects(project_id),
                question_set_version TEXT NOT NULL,
                answer_status TEXT NOT NULL CHECK (
                    answer_status IN ('answered', 'partial', 'skipped')
                ),
                structured_answer TEXT NOT NULL,
                answered_at TEXT NOT NULL,
                UNIQUE (context_answer_batch_id, project_id)
            )
            """,
            """
            CREATE TABLE project_context_facts (
                context_fact_id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL REFERENCES projects(project_id),
                fact_key TEXT NOT NULL,
                fact_kind TEXT NOT NULL CHECK (
                    fact_kind IN (
                        'business_goal', 'target_user', 'role', 'ownership',
                        'metric', 'outcome', 'tradeoff', 'learning'
                    )
                ),
                statement TEXT NOT NULL CHECK (length(statement) <= 2000),
                source_kind TEXT NOT NULL CHECK (source_kind IN ('config', 'context_answer')),
                source_answer_id TEXT REFERENCES context_answers(answer_id),
                config_revision TEXT,
                status TEXT NOT NULL CHECK (status IN ('current', 'superseded', 'withdrawn')),
                created_at TEXT NOT NULL,
                CHECK (
                    (source_kind = 'context_answer' AND source_answer_id IS NOT NULL
                        AND config_revision IS NULL)
                    OR (source_kind = 'config' AND source_answer_id IS NULL
                        AND config_revision IS NOT NULL)
                )
            )
            """,
            """
            CREATE UNIQUE INDEX project_context_facts_current_idx
            ON project_context_facts(project_id, fact_key)
            WHERE status = 'current'
            """,
            """
            CREATE TABLE evidence_contexts (
                evidence_id TEXT PRIMARY KEY REFERENCES evidence(evidence_id),
                context_fact_id TEXT NOT NULL UNIQUE REFERENCES
                    project_context_facts(context_fact_id)
            )
            """,
            """
            CREATE TABLE preparation_context_facts (
                preparation_run_id TEXT NOT NULL REFERENCES
                    preparation_runs(preparation_run_id),
                context_fact_id TEXT NOT NULL REFERENCES project_context_facts(context_fact_id),
                project_id TEXT NOT NULL REFERENCES projects(project_id),
                fact_key TEXT NOT NULL,
                bound_status TEXT NOT NULL CHECK (bound_status = 'current'),
                bound_at TEXT NOT NULL,
                PRIMARY KEY (preparation_run_id, context_fact_id),
                UNIQUE (preparation_run_id, project_id, fact_key)
            )
            """,
            """
            CREATE TABLE analysis_commits (
                analysis_commit_id TEXT PRIMARY KEY,
                request_id TEXT NOT NULL UNIQUE,
                request_sha256 TEXT NOT NULL,
                preparation_run_id TEXT NOT NULL UNIQUE REFERENCES
                    preparation_runs(preparation_run_id),
                role_lens_id TEXT NOT NULL REFERENCES role_lenses(role_lens_id),
                contract_version TEXT NOT NULL,
                evidence_count INTEGER NOT NULL CHECK (evidence_count >= 0),
                claim_count INTEGER NOT NULL CHECK (claim_count >= 0),
                assessment_count INTEGER NOT NULL CHECK (assessment_count >= 0),
                gap_count INTEGER NOT NULL CHECK (gap_count >= 0),
                committed_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE claims (
                claim_id TEXT PRIMARY KEY,
                identity_sha256 TEXT NOT NULL UNIQUE,
                claim_key TEXT NOT NULL,
                category TEXT NOT NULL CHECK (
                    category IN (
                        'technology', 'business', 'architecture', 'implementation_method',
                        'challenge', 'tradeoff', 'contribution', 'outcome', 'learning',
                        'knowledge_gap'
                    )
                ),
                scope_kind TEXT NOT NULL CHECK (scope_kind IN ('project', 'worktree', 'module')),
                project_id TEXT NOT NULL REFERENCES projects(project_id),
                worktree_id TEXT REFERENCES worktrees(worktree_id),
                module_id TEXT REFERENCES modules(module_id),
                created_at TEXT NOT NULL,
                CHECK (
                    (scope_kind = 'project' AND worktree_id IS NULL AND module_id IS NULL)
                    OR (scope_kind = 'worktree' AND worktree_id IS NOT NULL AND module_id IS NULL)
                    OR (scope_kind = 'module' AND module_id IS NOT NULL)
                )
            )
            """,
            """
            CREATE TABLE claim_revisions (
                claim_revision_id TEXT PRIMARY KEY,
                claim_id TEXT NOT NULL REFERENCES claims(claim_id),
                revision_no INTEGER NOT NULL CHECK (revision_no >= 1),
                revision_sha256 TEXT NOT NULL,
                statement TEXT NOT NULL CHECK (length(statement) <= 4000),
                statement_tokens TEXT NOT NULL,
                facets TEXT NOT NULL,
                support_level TEXT NOT NULL CHECK (
                    support_level IN (
                        'single_source', 'cross_checked', 'user_confirmed', 'conflicted'
                    )
                ),
                review_semantic_projection TEXT NOT NULL,
                review_semantic_sha256 TEXT NOT NULL,
                supersedes_id TEXT REFERENCES claim_revisions(claim_revision_id),
                created_at TEXT NOT NULL,
                UNIQUE (claim_id, revision_no),
                UNIQUE (claim_id, revision_sha256)
            )
            """,
            """
            CREATE TABLE claim_evidence (
                claim_revision_id TEXT NOT NULL REFERENCES
                    claim_revisions(claim_revision_id),
                evidence_id TEXT NOT NULL REFERENCES evidence(evidence_id),
                relation TEXT NOT NULL CHECK (
                    relation IN ('supports', 'contradicts', 'contextualizes')
                ),
                supported_facets TEXT NOT NULL,
                PRIMARY KEY (claim_revision_id, evidence_id, relation)
            )
            """,
            """
            CREATE TABLE preparation_claims (
                preparation_run_id TEXT NOT NULL REFERENCES
                    preparation_runs(preparation_run_id),
                claim_revision_id TEXT NOT NULL REFERENCES
                    claim_revisions(claim_revision_id),
                project_id TEXT NOT NULL REFERENCES projects(project_id),
                worktree_id TEXT REFERENCES worktrees(worktree_id),
                module_id TEXT REFERENCES modules(module_id),
                rank INTEGER NOT NULL CHECK (rank >= 1),
                section TEXT NOT NULL,
                PRIMARY KEY (preparation_run_id, claim_revision_id),
                UNIQUE (preparation_run_id, rank)
            )
            """,
            """
            CREATE TABLE knowledge_gaps (
                gap_id TEXT PRIMARY KEY,
                gap_key TEXT NOT NULL,
                preparation_run_id TEXT NOT NULL REFERENCES
                    preparation_runs(preparation_run_id),
                scope_kind TEXT NOT NULL CHECK (
                    scope_kind IN ('role_global', 'project', 'module')
                ),
                scope_id TEXT NOT NULL,
                project_id TEXT REFERENCES projects(project_id),
                module_id TEXT REFERENCES modules(module_id),
                dimension TEXT NOT NULL,
                stable_gap_concept_key TEXT NOT NULL,
                gap_contract_version TEXT NOT NULL,
                description TEXT NOT NULL CHECK (length(description) <= 4000),
                description_tokens TEXT NOT NULL,
                severity TEXT NOT NULL CHECK (
                    severity IN ('low', 'medium', 'high', 'critical')
                ),
                resolution_kind TEXT NOT NULL,
                status TEXT NOT NULL CHECK (status IN ('open', 'resolved', 'superseded')),
                UNIQUE (preparation_run_id, gap_key)
            )
            """,
            """
            CREATE TABLE project_assessments (
                preparation_run_id TEXT NOT NULL REFERENCES
                    preparation_runs(preparation_run_id),
                project_id TEXT NOT NULL REFERENCES projects(project_id),
                project_snapshot_id TEXT NOT NULL REFERENCES
                    project_snapshots(project_snapshot_id),
                snapshot_disposition TEXT NOT NULL CHECK (
                    snapshot_disposition IN ('fresh', 'carried_forward')
                ),
                dimension_scores_milli TEXT NOT NULL,
                evidence_and_gap_refs TEXT NOT NULL,
                rationale TEXT NOT NULL CHECK (length(rationale) <= 4000),
                rationale_tokens TEXT NOT NULL,
                coverage_bps INTEGER NOT NULL CHECK (coverage_bps BETWEEN 0 AND 10000),
                base_score_milli INTEGER NOT NULL CHECK (base_score_milli BETWEEN 0 AND 1000),
                final_score_milli INTEGER NOT NULL CHECK (final_score_milli BETWEEN 0 AND 1000),
                rank INTEGER NOT NULL CHECK (rank >= 1),
                PRIMARY KEY (preparation_run_id, project_id),
                UNIQUE (preparation_run_id, rank)
            )
            """,
            """
            CREATE INDEX claims_project_scope_idx
            ON claims(project_id, scope_kind, claim_key)
            """,
            """
            CREATE INDEX claim_revisions_claim_idx
            ON claim_revisions(claim_id, revision_no)
            """,
            """
            CREATE INDEX knowledge_gaps_run_scope_idx
            ON knowledge_gaps(preparation_run_id, scope_kind, scope_id)
            """,
            """
            CREATE INDEX evidence_preparation_run_idx
            ON evidence(preparation_run_id, project_id)
            """,
        ),
    ),
    Migration(
        version=7,
        name="immutable_artifact_rendering",
        statements=(
            """
            ALTER TABLE claim_revisions ADD COLUMN personal_attribution TEXT NOT NULL
            DEFAULT 'legacy_unknown' CHECK (
                personal_attribution IN (
                    'legacy_unknown', 'none', 'capability', 'personal_learning', 'implemented',
                    'responsible', 'led', 'personal_outcome'
                )
            )
            """,
            """
            CREATE TABLE render_attempts (
                render_attempt_id TEXT PRIMARY KEY,
                preparation_run_id TEXT NOT NULL REFERENCES
                    preparation_runs(preparation_run_id),
                owner_process_identity TEXT NOT NULL,
                report_bundle_sha256 TEXT NOT NULL,
                generator_version TEXT NOT NULL,
                temp_relative_path TEXT NOT NULL UNIQUE,
                latest_temp_relative_path TEXT NOT NULL UNIQUE,
                final_relative_path TEXT NOT NULL,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                status TEXT NOT NULL CHECK (
                    status IN ('running', 'succeeded', 'failed', 'interrupted')
                ),
                error_summary TEXT
            )
            """,
            """
            CREATE TABLE artifact_snapshots (
                artifact_snapshot_id TEXT PRIMARY KEY,
                preparation_run_id TEXT NOT NULL UNIQUE REFERENCES
                    preparation_runs(preparation_run_id),
                render_attempt_id TEXT NOT NULL UNIQUE REFERENCES
                    render_attempts(render_attempt_id),
                report_contract_version TEXT NOT NULL,
                report_bundle_sha256 TEXT NOT NULL,
                manifest_sha256 TEXT NOT NULL,
                report_markdown_path TEXT NOT NULL UNIQUE,
                resume_markdown_path TEXT NOT NULL UNIQUE,
                html_path TEXT NOT NULL UNIQUE,
                primary_language TEXT NOT NULL CHECK (primary_language = 'zh-CN'),
                created_at TEXT NOT NULL
            )
            """,
            """
            CREATE INDEX render_attempts_run_status_idx
            ON render_attempts(preparation_run_id, status, started_at)
            """,
        ),
    ),
    Migration(
        version=8,
        name="review_state_lineage",
        statements=(
            """
            CREATE TABLE review_targets (
                review_target_id TEXT PRIMARY KEY,
                target_kind TEXT NOT NULL CHECK (target_kind IN ('claim', 'topic')),
                stable_subject_id TEXT NOT NULL,
                topic_contract_version TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE (target_kind, stable_subject_id, topic_contract_version)
            )
            """,
            """
            CREATE TABLE review_target_bindings (
                review_target_binding_id TEXT PRIMARY KEY,
                review_target_id TEXT NOT NULL REFERENCES
                    review_targets(review_target_id),
                preparation_run_id TEXT NOT NULL REFERENCES
                    preparation_runs(preparation_run_id),
                subject_projection TEXT NOT NULL,
                subject_projection_sha256 TEXT NOT NULL,
                subject_fingerprint TEXT NOT NULL,
                continuity_status TEXT NOT NULL CHECK (
                    continuity_status IN ('new', 'continued', 'reassess_required')
                ),
                bound_at TEXT NOT NULL,
                UNIQUE (preparation_run_id, review_target_id),
                UNIQUE (review_target_binding_id, preparation_run_id)
            )
            """,
            """
            CREATE TABLE interview_reviews (
                review_id TEXT PRIMARY KEY,
                request_id TEXT NOT NULL UNIQUE,
                request_sha256 TEXT NOT NULL,
                preparation_run_id TEXT NOT NULL REFERENCES
                    preparation_runs(preparation_run_id),
                review_target_binding_id TEXT NOT NULL,
                question_id TEXT NOT NULL,
                summary TEXT NOT NULL CHECK (length(summary) BETWEEN 1 AND 4000),
                mastery_level TEXT NOT NULL CHECK (
                    mastery_level IN ('unfamiliar', 'developing', 'solid', 'mastered')
                ),
                weak_points TEXT NOT NULL,
                next_review_at TEXT CHECK (
                    next_review_at IS NULL OR (
                        length(next_review_at) = 10
                        AND substr(next_review_at, 5, 1) = '-'
                        AND substr(next_review_at, 8, 1) = '-'
                    )
                ),
                created_at TEXT NOT NULL,
                FOREIGN KEY (review_target_binding_id, preparation_run_id)
                    REFERENCES review_target_bindings(
                        review_target_binding_id, preparation_run_id
                    )
            )
            """,
            """
            CREATE INDEX review_target_bindings_lineage_idx
            ON review_target_bindings(
                review_target_id, subject_fingerprint, preparation_run_id
            )
            """,
            """
            CREATE INDEX interview_reviews_projection_idx
            ON interview_reviews(review_target_binding_id, created_at)
            """,
        ),
    ),
    Migration(
        version=9,
        name="review_causal_order",
        statements=(
            """
            ALTER TABLE preparation_runs
            ADD COLUMN review_lineage_sequence INTEGER NOT NULL DEFAULT 0
                CHECK (review_lineage_sequence >= 0)
            """,
            """
            UPDATE preparation_runs
            SET review_lineage_sequence = rowid
            """,
            """
            CREATE UNIQUE INDEX preparation_runs_review_lineage_idx
            ON preparation_runs(review_lineage_sequence)
            """,
            """
            ALTER TABLE preparation_runs
            ADD COLUMN review_cutoff_sequence INTEGER NOT NULL DEFAULT 0
                CHECK (review_cutoff_sequence >= 0)
            """,
            """
            ALTER TABLE interview_reviews
            ADD COLUMN review_sequence INTEGER
                CHECK (review_sequence IS NULL OR review_sequence > 0)
            """,
            """
            UPDATE interview_reviews
            SET review_sequence = rowid
            """,
            """
            CREATE UNIQUE INDEX interview_reviews_sequence_idx
            ON interview_reviews(review_sequence)
            """,
            """
            CREATE TRIGGER preparation_runs_require_review_lineage_sequence
            BEFORE INSERT ON preparation_runs
            WHEN NEW.review_lineage_sequence IS NULL
              OR NEW.review_lineage_sequence != COALESCE(
                    (SELECT MAX(review_lineage_sequence) FROM preparation_runs), 0
                 ) + 1
              OR NEW.review_cutoff_sequence != COALESCE(
                    (SELECT MAX(review_sequence) FROM interview_reviews), 0
                 )
            BEGIN
                SELECT RAISE(ABORT, 'review_lineage_sequence or review cutoff is invalid');
            END
            """,
            """
            CREATE TRIGGER interview_reviews_require_review_sequence
            BEFORE INSERT ON interview_reviews
            WHEN NEW.review_sequence IS NULL
              OR NEW.review_sequence != COALESCE(
                    (SELECT MAX(review_sequence) FROM interview_reviews), 0
                 ) + 1
            BEGIN
                SELECT RAISE(ABORT, 'review_sequence must be the next sequence');
            END
            """,
            """
            UPDATE preparation_runs
            SET review_cutoff_sequence = COALESCE(
                (
                    SELECT MAX(ir.review_sequence)
                    FROM interview_reviews AS ir
                    WHERE ir.created_at < preparation_runs.started_at
                ),
                0
            )
            """,
        ),
    ),
    Migration(
        version=10,
        name="atomic_english_exports",
        statements=(
            """
            CREATE TABLE export_attempts (
                export_attempt_id TEXT PRIMARY KEY,
                derived_export_id TEXT NOT NULL UNIQUE,
                source_artifact_snapshot_id TEXT NOT NULL REFERENCES
                    artifact_snapshots(artifact_snapshot_id),
                source_projection_sha256 TEXT NOT NULL,
                generator_version TEXT NOT NULL,
                owner_process_identity TEXT NOT NULL,
                temp_relative_path TEXT NOT NULL UNIQUE,
                final_relative_path TEXT NOT NULL UNIQUE,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                status TEXT NOT NULL CHECK (
                    status IN ('running', 'succeeded', 'failed', 'interrupted')
                ),
                error_summary TEXT
            )
            """,
            """
            CREATE TABLE derived_exports (
                derived_export_id TEXT PRIMARY KEY,
                export_attempt_id TEXT NOT NULL UNIQUE REFERENCES
                    export_attempts(export_attempt_id),
                source_artifact_snapshot_id TEXT NOT NULL REFERENCES
                    artifact_snapshots(artifact_snapshot_id),
                source_report_bundle_sha256 TEXT NOT NULL,
                source_projection_sha256 TEXT NOT NULL,
                language TEXT NOT NULL CHECK (language = 'en'),
                export_kinds TEXT NOT NULL,
                manifest_sha256 TEXT NOT NULL,
                output_path TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL
            )
            """,
            """
            CREATE INDEX export_attempts_source_status_idx
            ON export_attempts(source_artifact_snapshot_id, status, started_at)
            """,
            """
            CREATE INDEX derived_exports_source_idx
            ON derived_exports(source_artifact_snapshot_id, created_at)
            """,
        ),
    ),
    Migration(
        version=11,
        name="remove_issuer_kind_check",
        handler=_migrate_v11_remove_issuer_kind_check,
    ),
)


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


class Database:
    """Versioned SQLite access with one non-blocking writer at a time."""

    def __init__(self, paths: DataPaths) -> None:
        self.paths = paths

    def migrate(self) -> int:
        """Prepare schema and recovery state under one writer-lock acquisition."""
        with self._prepared_writer_connection() as (_, current_version):
            return current_version

    @contextmanager
    def write_transaction(self) -> Generator[sqlite3.Connection, None, None]:
        """Open a short atomic write transaction after applying known schema changes."""
        with self._prepared_writer_connection() as (connection, _):
            try:
                connection.execute("BEGIN IMMEDIATE")
                yield connection
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    @contextmanager
    def exclusive_writer_connection(self) -> Generator[sqlite3.Connection, None, None]:
        """Hold the single writer lock across a multi-transaction publication session."""
        with self._prepared_writer_connection() as (connection, _):
            yield connection

    @contextmanager
    def _prepared_writer_connection(
        self,
    ) -> Generator[tuple[sqlite3.Connection, int], None, None]:
        """Hold one lock across schema migration, recovery, and the caller's write."""
        with ExclusiveWriterLock(self.paths.writer_lock_file):
            self.paths.ensure_layout()
            connection = self._connect()
            try:
                current_version = self._prepare_writer_state(connection)
                yield connection, current_version
            finally:
                if connection.in_transaction:
                    connection.rollback()
                connection.close()

    def _prepare_writer_state(self, connection: sqlite3.Connection) -> int:
        """Apply schema migrations, then recover dead publications before new writes."""
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
                if migration.handler is not None:
                    connection.commit()
                    migration.handler(connection, self.paths.database_file)
                else:
                    if not connection.in_transaction:
                        connection.execute("BEGIN IMMEDIATE")
                    for statement in migration.statements:
                        connection.execute(statement)
                    connection.execute(
                        "INSERT INTO schema_migrations(version, name, applied_at) VALUES (?, ?, ?)",
                        (migration.version, migration.name, _utc_now()),
                    )
                current_version = migration.version
            if connection.in_transaction:
                connection.commit()
        except Exception:
            if connection.in_transaction:
                connection.rollback()
            raise
        recover_interrupted_exports(connection, self.paths)
        return current_version

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

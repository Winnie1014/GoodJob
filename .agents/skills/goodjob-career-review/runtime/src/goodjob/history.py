"""Task-scoped, bounded older-history queries for one frozen scan baseline."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path, PurePosixPath

from goodjob.db import Database
from goodjob.errors import InvalidInputError
from goodjob.scanner import (
    HISTORY_WINDOW_DAYS,
    MAX_HISTORY_METADATA_BYTES,
    InternalGitBinding,
    WorkspaceScanner,
    _safe_history_path,
)

MAX_HISTORY_QUERY_PATHS = 32
MAX_HISTORY_QUERY_CANDIDATES = 20
MAX_HISTORY_DEEP_READ_BYTES = 256 * 1024
HISTORY_QUERY_CONTRACT_VERSION = "history-query-v1"


@dataclass(frozen=True)
class HistoryQueryContext:
    preparation_run_id: str
    scan_run_id: str
    role_lens_id: str
    project_id: str
    worktree_id: str
    relative_paths: tuple[str, ...]
    query_reason: str
    baseline_started_at: str
    head_commit: str
    binding: InternalGitBinding


@dataclass(frozen=True)
class HistoryCandidate:
    candidate_id: str
    commit: str
    committed_at: str
    author_name: str
    author_email: str
    subject: str
    changed_paths: tuple[str, ...]
    metadata_sha256: str

    def as_json(self) -> dict[str, object]:
        return {
            "candidate_id": self.candidate_id,
            "commit": self.commit,
            "committed_at": self.committed_at,
            "author_name": self.author_name,
            "author_email": self.author_email,
            "subject": self.subject,
            "changed_paths": list(self.changed_paths),
            "metadata_sha256": self.metadata_sha256,
        }


class HistoryQueryService:
    """Reconstruct a frozen internal Git binding for each short-lived query."""

    def __init__(self, database: Database) -> None:
        self._database = database
        self._scanner = WorkspaceScanner(database)

    def query_candidates(
        self,
        *,
        workspace_path: Path,
        preparation_run_id: str,
        scan_run_id: str,
        role_lens_id: str,
        project_id: str,
        worktree_id: str | None,
        relative_paths: tuple[str, ...],
        query_reason: str,
        maximum_candidates: int,
    ) -> dict[str, object]:
        context = self._context(
            workspace_path=workspace_path,
            preparation_run_id=preparation_run_id,
            scan_run_id=scan_run_id,
            role_lens_id=role_lens_id,
            project_id=project_id,
            worktree_id=worktree_id,
            relative_paths=relative_paths,
            query_reason=query_reason,
        )
        if not 1 <= maximum_candidates <= MAX_HISTORY_QUERY_CANDIDATES:
            raise InvalidInputError(
                f"maximum_candidates must be between 1 and {MAX_HISTORY_QUERY_CANDIDATES}"
            )
        candidates, truncated = self._query(context, maximum_candidates)
        return {
            "status": "ok",
            "history_query": {
                "contract_version": HISTORY_QUERY_CONTRACT_VERSION,
                "preparation_run_id": context.preparation_run_id,
                "scan_run_id": context.scan_run_id,
                "role_lens_id": context.role_lens_id,
                "project_id": context.project_id,
                "worktree_id": context.worktree_id,
                "relative_paths": list(context.relative_paths),
                "query_reason": context.query_reason,
                "history_basis": "targeted_before_initial_window",
                "candidate_limit": maximum_candidates,
                "truncated": truncated,
                "candidates": [candidate.as_json() for candidate in candidates],
            },
        }

    def read_candidate(
        self,
        *,
        workspace_path: Path,
        preparation_run_id: str,
        scan_run_id: str,
        role_lens_id: str,
        project_id: str,
        worktree_id: str | None,
        relative_paths: tuple[str, ...],
        query_reason: str,
        candidate_id: str,
        selected_path: str,
    ) -> dict[str, object]:
        context = self._context(
            workspace_path=workspace_path,
            preparation_run_id=preparation_run_id,
            scan_run_id=scan_run_id,
            role_lens_id=role_lens_id,
            project_id=project_id,
            worktree_id=worktree_id,
            relative_paths=relative_paths,
            query_reason=query_reason,
        )
        candidates, _ = self._query(context, MAX_HISTORY_QUERY_CANDIDATES)
        candidate = next((item for item in candidates if item.candidate_id == candidate_id), None)
        if candidate is None:
            raise InvalidInputError("history candidate is not valid for this frozen query")
        if selected_path not in candidate.changed_paths or not self._path_matches_query(
            selected_path, context.relative_paths
        ):
            raise InvalidInputError("selected_path is not a queried path in this candidate")
        try:
            diff_result = self._scanner._git_bounded_bytes(
                context.binding,
                "show",
                "--format=",
                "--no-ext-diff",
                "--no-textconv",
                "--no-renames",
                candidate.commit,
                "--",
                selected_path,
                maximum_output_bytes=MAX_HISTORY_DEEP_READ_BYTES,
            )
            blob_result = self._scanner._git_bounded_bytes(
                context.binding,
                "cat-file",
                "blob",
                f"{candidate.commit}:{selected_path}",
                maximum_output_bytes=MAX_HISTORY_DEEP_READ_BYTES,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise InvalidInputError("bounded history candidate content could not be read") from exc
        if diff_result[0] != 0:
            raise InvalidInputError("bounded history candidate diff could not be read")
        diff_bytes = diff_result[1]
        blob_bytes = blob_result[1] if blob_result[0] == 0 else None
        try:
            blob_text = blob_bytes.decode("utf-8") if blob_bytes is not None else None
        except UnicodeDecodeError:
            blob_text = None
        return {
            "status": "ok",
            "history_candidate_read": {
                "contract_version": HISTORY_QUERY_CONTRACT_VERSION,
                "candidate": candidate.as_json(),
                "selected_path": selected_path,
                "query_reason": context.query_reason,
                "diff_sha256": hashlib.sha256(diff_bytes).hexdigest(),
                "blob_sha256": (
                    hashlib.sha256(blob_bytes).hexdigest() if blob_bytes is not None else None
                ),
                "diff_text": diff_bytes.decode("utf-8", errors="replace"),
                "blob_text": blob_text,
                "persisted": False,
            },
        }

    def _context(
        self,
        *,
        workspace_path: Path,
        preparation_run_id: str,
        scan_run_id: str,
        role_lens_id: str,
        project_id: str,
        worktree_id: str | None,
        relative_paths: tuple[str, ...],
        query_reason: str,
    ) -> HistoryQueryContext:
        if not preparation_run_id.strip() or len(preparation_run_id) > 200:
            raise InvalidInputError("preparation_run_id must be a bounded non-empty value")
        if not role_lens_id.strip() or len(role_lens_id) > 200:
            raise InvalidInputError("role_lens_id must be a bounded non-empty value")
        if not query_reason.strip() or len(query_reason) > 500:
            raise InvalidInputError("query_reason must be a bounded non-empty explanation")
        normalized_paths = self._normalize_paths(relative_paths)
        authorized_root = workspace_path.resolve(strict=False)
        with self._database.read_connection() as connection:
            rows = connection.execute(
                """
                SELECT sr.scan_run_id, basis_sr.started_at AS baseline_started_at,
                       sr.status, ws.canonical_root,
                       srp.snapshot_disposition, p.identity_kind, p.identity_key,
                       w.worktree_id, w.canonical_root AS worktree_root, w.git_dir,
                       wo.head_commit, wo.external_common_dir
                FROM scan_runs AS sr
                JOIN workspaces AS ws ON ws.workspace_id = sr.workspace_id
                JOIN preparation_runs AS pr
                  ON pr.scan_run_id = sr.scan_run_id
                 AND pr.preparation_run_id = ?
                 AND pr.role_lens_id = ?
                 AND pr.status IN ('analyzing', 'awaiting_context')
                JOIN scan_run_projects AS srp ON srp.scan_run_id = sr.scan_run_id
                JOIN projects AS p ON p.project_id = srp.project_id
                JOIN project_snapshots AS ps
                  ON ps.project_snapshot_id = srp.project_snapshot_id
                JOIN scan_runs AS basis_sr ON basis_sr.scan_run_id = ps.scan_run_id
                JOIN worktrees AS w ON w.project_id = p.project_id
                JOIN worktree_observations AS wo
                  ON wo.worktree_id = w.worktree_id AND wo.scan_run_id = ps.scan_run_id
                WHERE sr.scan_run_id = ? AND p.project_id = ?
                  AND ws.canonical_root = ?
                  AND sr.status IN ('completed', 'partial')
                  AND srp.snapshot_disposition IN ('fresh', 'carried_forward')
                ORDER BY w.worktree_id
                """,
                (
                    preparation_run_id,
                    role_lens_id,
                    scan_run_id,
                    project_id,
                    str(authorized_root),
                ),
            ).fetchall()
            if worktree_id is not None:
                rows = [row for row in rows if str(row["worktree_id"]) == worktree_id]
            if not rows:
                raise InvalidInputError(
                    "history query requires an eligible internal Git project snapshot"
                )
            if len(rows) != 1:
                raise InvalidInputError(
                    "worktree_id is required when a project has multiple worktrees"
                )
            row = rows[0]
            if (
                str(row["identity_kind"]) != "git_common_dir"
                or row["external_common_dir"] is not None
                or row["head_commit"] is None
                or row["git_dir"] is None
            ):
                raise InvalidInputError(
                    "root-external or non-Git projects do not support targeted history reads"
                )
            snapshot_id_row = connection.execute(
                """
                SELECT srp.project_snapshot_id
                FROM scan_run_projects AS srp
                WHERE srp.scan_run_id = ? AND srp.project_id = ?
                """,
                (scan_run_id, project_id),
            ).fetchone()
            assert snapshot_id_row is not None
            self._validate_query_paths(
                connection,
                project_snapshot_id=str(snapshot_id_row["project_snapshot_id"]),
                worktree_id=str(row["worktree_id"]),
                relative_paths=normalized_paths,
            )
        workspace_root = Path(str(row["canonical_root"]))
        worktree_root = Path(str(row["worktree_root"]))
        binding = self._scanner._bind_internal_git(worktree_root, workspace_root)
        if (
            binding is None
            or str(binding.git_dir) != str(row["git_dir"])
            or str(binding.common_dir) != str(row["identity_key"])
        ):
            raise InvalidInputError("Git repository identity changed after the frozen scan")
        return HistoryQueryContext(
            preparation_run_id=preparation_run_id,
            scan_run_id=scan_run_id,
            role_lens_id=role_lens_id,
            project_id=project_id,
            worktree_id=str(row["worktree_id"]),
            relative_paths=normalized_paths,
            query_reason=query_reason,
            baseline_started_at=str(row["baseline_started_at"]),
            head_commit=str(row["head_commit"]),
            binding=binding,
        )

    def _query(
        self, context: HistoryQueryContext, maximum_candidates: int
    ) -> tuple[tuple[HistoryCandidate, ...], bool]:
        try:
            started_at = datetime.fromisoformat(
                context.baseline_started_at.removesuffix("Z") + "+00:00"
            )
        except ValueError as exc:
            raise InvalidInputError("frozen scan start time is invalid") from exc
        cutoff = (
            (started_at - timedelta(days=HISTORY_WINDOW_DAYS)).isoformat().replace("+00:00", "Z")
        )
        try:
            result = self._scanner._git_bounded_bytes(
                context.binding,
                "log",
                "--no-ext-diff",
                "--no-textconv",
                "--date-order",
                f"--max-count={maximum_candidates + 1}",
                f"--until={cutoff}",
                "-z",
                "--format=%H%x00%ct%x00%an%x00%ae%x00%s",
                context.head_commit,
                "--",
                *context.relative_paths,
                maximum_output_bytes=MAX_HISTORY_METADATA_BYTES,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise InvalidInputError("bounded targeted Git history could not be read") from exc
        if result[0] != 0:
            raise InvalidInputError("bounded targeted Git history query failed")
        try:
            records = WorkspaceScanner._parse_history_metadata(result[1])
        except ValueError as exc:
            raise InvalidInputError("targeted Git history metadata is invalid") from exc
        truncated = len(records) > maximum_candidates
        candidates: list[HistoryCandidate] = []
        for commit, committed_at, author_name, author_email, subject in records[
            :maximum_candidates
        ]:
            try:
                changed_paths, _ = self._scanner._history_paths(
                    context.binding, commit, *context.relative_paths
                )
            except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
                raise InvalidInputError("targeted history path range could not be read") from exc
            selected_paths = tuple(
                path
                for path in changed_paths
                if self._path_matches_query(path, context.relative_paths)
            )
            if not selected_paths:
                continue
            metadata = {
                "commit": commit,
                "committed_at": committed_at,
                "author_name": author_name,
                "author_email": author_email,
                "subject": subject,
                "changed_paths": selected_paths,
            }
            metadata_json = json.dumps(
                metadata, ensure_ascii=False, separators=(",", ":"), sort_keys=True
            )
            metadata_sha256 = hashlib.sha256(metadata_json.encode("utf-8")).hexdigest()
            candidate_id = hashlib.sha256(
                "\0".join(
                    (
                        HISTORY_QUERY_CONTRACT_VERSION,
                        context.scan_run_id,
                        context.role_lens_id,
                        context.project_id,
                        context.worktree_id,
                        *context.relative_paths,
                        context.query_reason,
                        metadata_sha256,
                    )
                ).encode("utf-8")
            ).hexdigest()
            candidates.append(
                HistoryCandidate(
                    candidate_id=candidate_id,
                    commit=commit,
                    committed_at=committed_at,
                    author_name=author_name,
                    author_email=author_email,
                    subject=subject,
                    changed_paths=selected_paths,
                    metadata_sha256=metadata_sha256,
                )
            )
        return tuple(candidates), truncated

    @staticmethod
    def _normalize_paths(relative_paths: tuple[str, ...]) -> tuple[str, ...]:
        if not 1 <= len(relative_paths) <= MAX_HISTORY_QUERY_PATHS:
            raise InvalidInputError(
                f"relative_paths must contain between 1 and {MAX_HISTORY_QUERY_PATHS} entries"
            )
        normalized: set[str] = set()
        for value in relative_paths:
            path = PurePosixPath(value)
            if (
                not value
                or path.is_absolute()
                or ".." in path.parts
                or not _safe_history_path(value)
            ):
                raise InvalidInputError("relative_paths contains an unsafe project path")
            normalized.add(path.as_posix().removesuffix("/"))
        return tuple(sorted(normalized))

    @staticmethod
    def _validate_query_paths(
        connection: sqlite3.Connection,
        *,
        project_snapshot_id: str,
        worktree_id: str,
        relative_paths: tuple[str, ...],
    ) -> None:
        rows = connection.execute(
            """
            SELECT a.relative_path
            FROM project_snapshot_source_revisions AS pssr
            JOIN source_revisions AS sr
              ON sr.source_revision_id = pssr.source_revision_id
            JOIN source_artifacts AS a ON a.artifact_id = sr.artifact_id
            WHERE pssr.project_snapshot_id = ? AND a.worktree_id = ?
            """,
            (project_snapshot_id, worktree_id),
        ).fetchall()
        indexed_paths = {str(row["relative_path"]) for row in rows}
        if any(
            not any(path == query or path.startswith(f"{query}/") for path in indexed_paths)
            for query in relative_paths
        ):
            raise InvalidInputError("relative_paths must refer to indexed snapshot content")

    @staticmethod
    def _path_matches_query(path: str, queries: tuple[str, ...]) -> bool:
        return any(path == query or path.startswith(f"{query}/") for query in queries)

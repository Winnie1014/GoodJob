"""Deterministic, local-only workspace discovery and evidence indexing."""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import re
import selectors
import signal
import sqlite3
import stat
import subprocess
import sys
import time
import uuid
from collections import Counter
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Literal, cast

from goodjob.adapters import (
    AnalysisDiagnostic,
    AnalysisFact,
    AnalysisResult,
    adapter_version,
    analyze_file,
)
from goodjob.db import Database
from goodjob.errors import InvalidInputError
from goodjob.source_io import (
    MAX_SOURCE_FILE_BYTES,
    open_regular_file,
    read_open_file,
)

MAX_FILE_BYTES = MAX_SOURCE_FILE_BYTES
ANALYZER_VERSION = "scan-v2"
HISTORY_WINDOW_DAYS = 180
MAX_HISTORY_COMMITS = 250
MAX_HISTORY_PATHS_PER_COMMIT = 200
MAX_HISTORY_METADATA_BYTES = 512 * 1024
MAX_HISTORY_PATH_BYTES = 256 * 1024
MAX_REMOTE_HEAD_BYTES = 512 * 1024
MAX_GIT_COMMAND_BYTES = 8 * 1024 * 1024
MAX_HISTORY_FIELD_BYTES = 512
ROLE_LENS_CONTEXT_CONTRACT_VERSION = "role-lens-context-v1"
MAX_ROLE_LENS_CONTEXT_PROJECTS = 200
MAX_ROLE_LENS_CONTEXT_MODULES = 500
MAX_ROLE_LENS_CONTEXT_MODULES_PER_PROJECT = 20
MAX_ROLE_LENS_CONTEXT_EVIDENCE_SAMPLES = 500
MAX_ROLE_LENS_CONTEXT_EVIDENCE_PER_PROJECT = 10
SCAN_OVERVIEW_CONTRACT_VERSION = "scan-overview-v1"
MAX_SCAN_OVERVIEW_ISSUES = 200
GIT_COMMAND_TIMEOUT_SECONDS = 10.0
GIT_EXECUTABLE_CANDIDATES = (
    Path("/Applications/Xcode.app/Contents/Developer/usr/bin/git"),
    Path("/Library/Developer/CommandLineTools/usr/bin/git"),
    Path("/usr/bin/git"),
    Path("/bin/git"),
)
GIT_EXECUTABLE = next(
    (str(candidate) for candidate in GIT_EXECUTABLE_CANDIDATES if candidate.is_file()),
    "/usr/bin/git",
)
SANDBOX_EXECUTABLE = Path("/usr/bin/sandbox-exec")
GIT_SANDBOX_PROFILE = " ".join(
    (
        "(version 1)",
        '(import "system.sb")',
        "(deny default)",
        "(deny network*)",
        '(allow process-exec (literal (param "GIT_EXECUTABLE")))',
        "(allow process-fork)",
        "("
        "allow file-read* file-test-existence file-map-executable "
        '(subpath (param "AUTHORIZED_ROOT")) '
        '(literal (param "GIT_EXECUTABLE"))'
        ")",
        '(allow file-read-metadata file-test-existence (path-ancestors (param "AUTHORIZED_ROOT")))',
        '(allow file-write-data (literal "/dev/null"))',
    )
)
GIT_ENV = {
    "PATH": "/usr/bin:/bin",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_NO_LAZY_FETCH": "1",
    "GIT_PAGER": "cat",
    "GIT_LITERAL_PATHSPECS": "1",
    "GIT_NOGLOB_PATHSPECS": "1",
    "GIT_OPTIONAL_LOCKS": "0",
    "GIT_TERMINAL_PROMPT": "0",
    "LC_ALL": "C",
}
HARD_EXCLUDED_DIRECTORIES = frozenset(
    {
        ".git",
        ".cache",
        ".aws",
        ".dart_tool",
        ".gnupg",
        ".idea",
        ".next",
        ".pytest_cache",
        ".terraform",
        ".venv",
        ".ssh",
        "__pycache__",
        "bower_components",
        "build",
        "coverage",
        "credentials",
        "dist",
        "env",
        "node_modules",
        "secrets",
        "target",
        "venv",
    }
)
MANIFEST_NAMES = frozenset(
    {
        "Cargo.toml",
        "CMakeLists.txt",
        "Package.swift",
        "build.gradle",
        "build.gradle.kts",
        "go.mod",
        "package.json",
        "pom.xml",
        "pubspec.yaml",
        "pyproject.toml",
    }
)
SOURCE_EXTENSIONS = {
    ".dart": "dart",
    ".py": "python",
    ".rs": "rust",
    ".sql": "sql",
    ".ts": "typescript",
    ".tsx": "typescript",
}
CONFIG_EXTENSIONS = frozenset({".json", ".toml", ".yaml", ".yml"})


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _new_id() -> str:
    return str(uuid.uuid4())


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _relative_path(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def _short(value: str, maximum: int = 500) -> str:
    return value[:maximum]


def _diagnostics_json(diagnostics: tuple[AnalysisDiagnostic, ...]) -> str:
    return json.dumps(
        [
            {
                "kind": diagnostic.kind,
                "message": diagnostic.message,
                "remediation": diagnostic.remediation,
            }
            for diagnostic in diagnostics
        ],
        separators=(",", ":"),
        sort_keys=True,
    )


def _analysis_diagnostics(raw: str) -> tuple[AnalysisDiagnostic, ...]:
    try:
        values = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("stored analysis diagnostics are invalid") from exc
    if not isinstance(values, list) or len(values) > 8:
        raise ValueError("stored analysis diagnostics are invalid")
    diagnostics: list[AnalysisDiagnostic] = []
    for value in values:
        if not isinstance(value, dict):
            raise ValueError("stored analysis diagnostic is invalid")
        kind = value.get("kind")
        message = value.get("message")
        remediation = value.get("remediation")
        if (
            not isinstance(kind, str)
            or not isinstance(message, str)
            or not isinstance(remediation, str)
            or not kind
            or max(len(kind), len(message), len(remediation)) > 500
        ):
            raise ValueError("stored analysis diagnostic is invalid")
        diagnostics.append(AnalysisDiagnostic(kind, message, remediation))
    return tuple(diagnostics)


def _open_regular_file(root: Path, relative_path: str) -> tuple[int, os.stat_result]:
    return open_regular_file(root, relative_path)


def _read_open_file(file_fd: int, *, maximum_bytes: int = MAX_FILE_BYTES) -> bytes:
    return read_open_file(file_fd, maximum_bytes=maximum_bytes)


def _open_directory(root: Path, relative_path: str = ".") -> int:
    """Open a directory below the authorized root without following links."""
    parts = PurePosixPath(relative_path).parts
    if relative_path == ".":
        parts = ()
    if any(part in {"", ".", ".."} for part in parts):
        raise OSError("relative directory path is not safe to open")
    flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
    root_fd = os.open(root, flags)
    directory_fd = root_fd
    try:
        for part in parts:
            next_fd = os.open(part, flags, dir_fd=directory_fd)
            os.close(directory_fd)
            directory_fd = next_fd
        return directory_fd
    except Exception:
        os.close(directory_fd)
        raise


def _open_absolute_directory(path: Path) -> int:
    """Open one exact absolute directory without following any component symlink."""
    if not path.is_absolute():
        raise OSError("directory path must be absolute")
    parts = path.parts
    if not parts or any(part in {"", ".", ".."} for part in parts[1:]):
        raise OSError("absolute directory path is not safe to open")
    flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
    directory_fd = os.open(parts[0], flags)
    try:
        for part in parts[1:]:
            next_fd = os.open(part, flags, dir_fd=directory_fd)
            os.close(directory_fd)
            directory_fd = next_fd
        return directory_fd
    except Exception:
        os.close(directory_fd)
        raise


def _open_regular_file_at(directory_fd: int, relative_path: str) -> tuple[int, os.stat_result]:
    """Open a regular file below an already-bound directory descriptor."""
    parts = PurePosixPath(relative_path).parts
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise OSError("relative path is not safe to open")
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | nofollow
    parent_fd = os.dup(directory_fd)
    try:
        for part in parts[:-1]:
            next_fd = os.open(part, directory_flags, dir_fd=parent_fd)
            os.close(parent_fd)
            parent_fd = next_fd
        file_fd = os.open(parts[-1], os.O_RDONLY | nofollow, dir_fd=parent_fd)
        file_stat = os.fstat(file_fd)
        if not stat.S_ISREG(file_stat.st_mode):
            os.close(file_fd)
            raise OSError("path is not a regular file")
        return file_fd, file_stat
    finally:
        os.close(parent_fd)


def _read_text_at(directory_fd: int, relative_path: str, *, maximum_bytes: int = 4096) -> str:
    file_fd, _ = _open_regular_file_at(directory_fd, relative_path)
    try:
        return _read_open_file(file_fd, maximum_bytes=maximum_bytes).decode("utf-8")
    finally:
        os.close(file_fd)


def _directory_identity(directory_fd: int) -> tuple[int, int]:
    directory_stat = os.fstat(directory_fd)
    if not stat.S_ISDIR(directory_stat.st_mode):
        raise OSError("bound path is not a directory")
    return directory_stat.st_dev, directory_stat.st_ino


def _safe_lstat(root: Path, relative_path: str) -> os.stat_result:
    """Stat a child through an already-authorized directory descriptor."""
    relative = PurePosixPath(relative_path)
    if not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
        raise OSError("relative path is not safe to stat")
    parent = relative.parent
    parent_relative = "." if str(parent) == "." else parent.as_posix()
    directory_fd = _open_directory(root, parent_relative)
    try:
        return os.stat(relative.name, dir_fd=directory_fd, follow_symlinks=False)
    finally:
        os.close(directory_fd)


def _child_relative(directory_relative: str, child_name: str) -> str:
    return child_name if directory_relative == "." else f"{directory_relative}/{child_name}"


def _relative_to_root(path: Path, root: Path) -> str:
    relative = path.relative_to(root).as_posix()
    return "." if relative == "." else relative


def _valid_git_commit(value: str) -> bool:
    return len(value) in {40, 64} and all(character in "0123456789abcdef" for character in value)


def _history_timestamp(raw_seconds: bytes) -> str:
    try:
        seconds = int(raw_seconds.decode("ascii"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ValueError("Git history returned an invalid commit timestamp") from exc
    if seconds < 0:
        raise ValueError("Git history returned a negative commit timestamp")
    return datetime.fromtimestamp(seconds, UTC).isoformat().replace("+00:00", "Z")


def _safe_history_path(path: str) -> bool:
    pure_path = PurePosixPath(path)
    if not path or pure_path.is_absolute() or ".." in pure_path.parts:
        return False
    if any(part.lower() in HARD_EXCLUDED_DIRECTORIES for part in pure_path.parts[:-1]):
        return False
    return not WorkspaceScanner._is_sensitive(pure_path.name)


def _symlink_may_escape(directory_relative: str, target: str) -> bool:
    target_path = PurePosixPath(target)
    if target_path.is_absolute():
        return True
    components = [] if directory_relative == "." else directory_relative.split("/")
    for part in target_path.parts:
        if part in {"", "."}:
            continue
        if part == "..":
            if not components:
                return True
            components.pop()
        else:
            components.append(part)
    return False


def _process_identity() -> str:
    pid = os.getpid()
    started = _process_start_marker(pid)
    return f"pid:{pid};started:{started}" if started is not None else f"pid:{pid};started:unknown"


def _process_start_marker(pid: int) -> str | None:
    result = subprocess.run(
        ["ps", "-o", "lstart=", "-p", str(pid)],
        check=False,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        env={"PATH": GIT_ENV["PATH"], "LC_ALL": "C"},
        text=True,
    )
    marker = result.stdout.strip()
    return marker if result.returncode == 0 and marker else None


def _owner_process_stopped(identity: str) -> bool:
    prefix, separator, started = identity.partition(";started:")
    if not separator or not prefix.startswith("pid:") or started == "unknown":
        return False
    try:
        pid = int(prefix.removeprefix("pid:"))
    except ValueError:
        return False
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    current_start = _process_start_marker(pid)
    return current_start is not None and current_start != started


@dataclass(frozen=True)
class ScanIssueDraft:
    issue_id: str
    kind: str
    severity: Literal["info", "warning", "error"]
    relative_path: str | None
    message: str
    remediation: str


def _issue(
    kind: str,
    severity: Literal["info", "warning", "error"],
    message: str,
    remediation: str,
    relative_path: str | None = None,
) -> ScanIssueDraft:
    return ScanIssueDraft(
        issue_id=_new_id(),
        kind=kind,
        severity=severity,
        relative_path=_short(relative_path) if relative_path else None,
        message=_short(message),
        remediation=_short(remediation),
    )


@dataclass(frozen=True)
class GitHistoryEntry:
    commit: str
    committed_at: str
    author_name: str
    author_email: str
    subject: str
    changed_paths: tuple[str, ...]
    paths_truncated: bool


@dataclass(frozen=True)
class GitState:
    git_dir: Path
    common_dir: Path
    branch: str | None
    head_commit: str | None
    dirty_state: str
    path_states: dict[str, str]
    history_basis: str
    history_entries: tuple[GitHistoryEntry, ...]
    external_metadata_only: bool
    external_git_grant: ExternalGitGrant | None
    external_metadata_read_fields: tuple[str, ...]
    history_issues: tuple[ScanIssueDraft, ...]


@dataclass(frozen=True)
class InternalGitBinding:
    """Descriptor identities that bind every Git command to one authorized repository."""

    workspace_root: Path
    worktree_root: Path
    git_dir: Path
    common_dir: Path
    worktree_identity: tuple[int, int]
    git_dir_identity: tuple[int, int]
    common_dir_identity: tuple[int, int]


@dataclass(frozen=True)
class ExternalGitCandidate:
    """Root-internal marker data that may be shown before any external read."""

    git_pointer_path: Path
    marker_kind: Literal["file", "directory"]
    git_dir_candidate: Path
    common_dir_candidate: Path | None

    def as_json(self) -> dict[str, object]:
        return {
            "git_pointer_path": str(self.git_pointer_path),
            "marker_kind": self.marker_kind,
            "git_dir_candidate": str(self.git_dir_candidate),
            "common_dir_candidate": (
                str(self.common_dir_candidate) if self.common_dir_candidate is not None else None
            ),
            "read_fields": ["git_marker", "gitdir_candidate", "commondir_candidate"],
        }


@dataclass(frozen=True)
class ExternalGitGrant:
    """A metadata-only authorization for one root-external linked worktree relation."""

    git_pointer_path: Path
    marker_kind: Literal["file", "directory"]
    git_dir: Path
    common_dir: Path
    git_dir_device: int
    git_dir_inode: int
    common_dir_device: int
    common_dir_inode: int
    authorization_receipt_id: str
    confirmed_at: str


def inspect_external_git_candidate(
    workspace_root: Path, git_pointer_path: Path
) -> dict[str, object]:
    """Inspect only a root-internal Git marker before relation authorization."""
    workspace = workspace_root.resolve(strict=True)
    raw_pointer = (
        git_pointer_path if git_pointer_path.is_absolute() else workspace / git_pointer_path
    )
    pointer = Path(os.path.normpath(str(raw_pointer)))
    if pointer.name != ".git" or not _is_within(pointer, workspace):
        raise InvalidInputError("git_pointer must name a .git file inside the authorized workspace")
    try:
        pointer_relative = _relative_to_root(pointer, workspace)
        marker_stat = _safe_lstat(workspace, pointer_relative)
    except OSError as exc:
        raise InvalidInputError("git_pointer is not readable") from exc
    if stat.S_ISREG(marker_stat.st_mode):
        lexical_git_dir = WorkspaceScanner._git_pointer_target_at(workspace, pointer_relative)
        if lexical_git_dir is None:
            raise InvalidInputError("git_pointer is not a valid linked-worktree pointer")
        common_candidate: Path | None = None
        if _is_within(lexical_git_dir, workspace):
            git_dir_relative = _relative_to_root(lexical_git_dir, workspace)
            common_candidate = WorkspaceScanner._relation_target_at(
                workspace, git_dir_relative, "commondir"
            )
            if common_candidate is None or _is_within(common_candidate, workspace):
                raise InvalidInputError("git_pointer does not expose a root-external relation")
        candidate = ExternalGitCandidate(pointer, "file", lexical_git_dir, common_candidate)
        return candidate.as_json()
    if stat.S_ISDIR(marker_stat.st_mode):
        common_candidate = WorkspaceScanner._relation_target_at(
            workspace, pointer_relative, "commondir"
        )
        if common_candidate is None or _is_within(common_candidate, workspace):
            raise InvalidInputError("git_pointer does not expose a root-external relation")
        return ExternalGitCandidate(pointer, "directory", pointer, common_candidate).as_json()
    raise InvalidInputError("git_pointer must be a regular file or directory .git marker")


def probe_external_git_relation(
    workspace_root: Path,
    git_pointer_path: Path,
    *,
    marker_kind: Literal["file", "directory"],
    git_dir_candidate: Path,
    common_dir_candidate: Path | None,
) -> dict[str, object]:
    """Resolve one candidate-bound relation; never invoke Git or read project source."""
    inspected = inspect_external_git_candidate(workspace_root, git_pointer_path)
    expected_common = str(common_dir_candidate) if common_dir_candidate is not None else None
    if (
        inspected.get("marker_kind") != marker_kind
        or inspected.get("git_dir_candidate") != str(git_dir_candidate)
        or inspected.get("common_dir_candidate") != expected_common
    ):
        raise InvalidInputError("Git marker candidate changed after relation authorization")
    pointer = Path(str(inspected["git_pointer_path"]))
    try:
        git_dir_fd = _open_absolute_directory(git_dir_candidate)
    except OSError as exc:
        raise InvalidInputError("linked-worktree gitdir is not readable") from exc
    try:
        lexical_common_dir = WorkspaceScanner._relation_target_from_fd(
            git_dir_fd, git_dir_candidate, "commondir"
        )
        if lexical_common_dir is None:
            raise InvalidInputError("linked-worktree commondir is not valid")
        if common_dir_candidate is not None and lexical_common_dir != common_dir_candidate:
            raise InvalidInputError("linked-worktree commondir candidate changed")
        if marker_kind == "file":
            lexical_back_pointer = WorkspaceScanner._relation_target_from_fd(
                git_dir_fd, git_dir_candidate, "gitdir"
            )
            if lexical_back_pointer != pointer:
                raise InvalidInputError(
                    "linked-worktree relation does not bind back to the authorized pointer"
                )
        try:
            common_dir_fd = _open_absolute_directory(lexical_common_dir)
        except OSError as exc:
            raise InvalidInputError("linked-worktree commondir is not readable") from exc
        try:
            git_identity = _directory_identity(git_dir_fd)
            common_identity = _directory_identity(common_dir_fd)
        finally:
            os.close(common_dir_fd)
    finally:
        os.close(git_dir_fd)
    return {
        "git_pointer_path": str(pointer),
        "marker_kind": marker_kind,
        "git_dir": str(git_dir_candidate),
        "common_dir": str(lexical_common_dir),
        "git_dir_device": git_identity[0],
        "git_dir_inode": git_identity[1],
        "common_dir_device": common_identity[0],
        "common_dir_inode": common_identity[1],
        "read_fields": ["gitdir", "commondir", "directory_identity"],
    }


@dataclass(frozen=True)
class WorktreePlan:
    root: Path
    git_state: GitState | None


@dataclass(frozen=True)
class ProjectPlan:
    identity_kind: Literal["git_common_dir", "non_git_root"]
    identity_key: str
    display_name: str
    worktrees: tuple[WorktreePlan, ...]


@dataclass(frozen=True)
class PriorObservation:
    artifact_id: str
    source_revision_id: str
    byte_size: int
    mtime_ns: int
    adapter_id: str
    adapter_version: str
    config_revision: str
    commit_state: str
    analysis_diagnostics: tuple[AnalysisDiagnostic, ...]


@dataclass(frozen=True)
class FileObservation:
    relative_path: str
    artifact_kind: str
    adapter_id: str
    adapter_version: str
    evidence_kind: str
    commit_state: str
    byte_size: int
    mtime_ns: int
    content_sha256: str | None
    reused_source_revision_id: str | None
    analysis_facts: tuple[AnalysisFact, ...] | None
    analysis_diagnostics: tuple[AnalysisDiagnostic, ...]


@dataclass(frozen=True)
class WorktreeData:
    plan: WorktreePlan
    files: tuple[FileObservation, ...]
    issues: tuple[ScanIssueDraft, ...]
    excluded: Counter[str]


@dataclass(frozen=True)
class ProjectData:
    plan: ProjectPlan
    worktrees: tuple[WorktreeData, ...]


@dataclass(frozen=True)
class ProjectRecord:
    project_id: str
    display_name: str


@dataclass(frozen=True)
class ScanResult:
    scan_run_id: str
    workspace_id: str
    status: str
    mode: str
    change_detection_mode: str | None
    coverage: dict[str, object]
    issues: tuple[ScanIssueDraft, ...]

    def as_json(self) -> dict[str, object]:
        return {
            "status": "ok",
            "scan_run": {
                "scan_run_id": self.scan_run_id,
                "workspace_id": self.workspace_id,
                "status": self.status,
                "mode": self.mode,
                "change_detection_mode": self.change_detection_mode,
            },
            "coverage": self.coverage,
            "issues": [
                {
                    "issue_id": issue.issue_id,
                    "kind": issue.kind,
                    "severity": issue.severity,
                    "relative_path": issue.relative_path,
                    "message": issue.message,
                    "remediation": issue.remediation,
                }
                for issue in self.issues
            ],
        }


class IgnoreMatcher:
    """A deliberately small, deterministic subset of .gitignore semantics."""

    def __init__(self, patterns: tuple[tuple[str, str, bool, bool], ...]) -> None:
        self._patterns = patterns

    @classmethod
    def load(
        cls, root: Path, ignore_files: list[str]
    ) -> tuple[IgnoreMatcher, list[ScanIssueDraft]]:
        patterns: list[tuple[str, str, bool, bool]] = []
        issues: list[ScanIssueDraft] = []
        for relative in sorted(ignore_files):
            try:
                file_fd, _ = _open_regular_file(root, relative)
                try:
                    text = _read_open_file(file_fd).decode("utf-8")
                finally:
                    os.close(file_fd)
            except (OSError, UnicodeError):
                issues.append(
                    _issue(
                        "ignore_unreadable",
                        "warning",
                        "Could not decode a project ignore file.",
                        "Use UTF-8 for .gitignore or review files selected by the scanner.",
                        relative,
                    )
                )
                continue
            parent = PurePosixPath(relative).parent
            base = "." if str(parent) == "." else parent.as_posix()
            for raw_line in text.splitlines():
                line = raw_line.strip()
                if not line or line.startswith("#"):
                    continue
                include = line.startswith("!")
                if include:
                    line = line[1:]
                directory_only = line.endswith("/")
                line = line.strip("/")
                if line:
                    patterns.append((base, line, include, directory_only))
        return cls(tuple(patterns)), issues

    def matches(self, relative_path: str) -> bool:
        ignored = False
        for base, pattern, include, directory_only in self._patterns:
            if base == ".":
                candidate = relative_path
            elif relative_path.startswith(f"{base}/"):
                candidate = relative_path.removeprefix(f"{base}/")
            else:
                continue
            parts = candidate.split("/")
            matched = fnmatch.fnmatchcase(candidate, pattern)
            if not matched and "/" not in pattern:
                matched = any(fnmatch.fnmatchcase(part, pattern) for part in parts)
            if not matched and directory_only:
                matched = any(fnmatch.fnmatchcase(part, pattern) for part in parts[:-1])
            if matched:
                ignored = not include
        return ignored


class WorkspaceScanner:
    """Build immutable scan snapshots after the caller has verified authorization."""

    def __init__(self, database: Database, *, git_executable: str = GIT_EXECUTABLE) -> None:
        self._database = database
        self._git_executable = str(Path(git_executable).resolve(strict=True))
        self._analysis_cache: dict[tuple[str, str, str, str, str, str], AnalysisResult] = {}

    def scan(
        self,
        *,
        workspace_path: str,
        config_revision: str,
        authorization_receipt_id: str,
        external_git_grants: tuple[ExternalGitGrant, ...] = (),
    ) -> ScanResult:
        root = Path(workspace_path).expanduser().resolve(strict=False)
        return self._run(
            root=root,
            workspace_id=None,
            mode="full",
            change_detection_mode=None,
            config_revision=config_revision,
            authorization_receipt_id=authorization_receipt_id,
            external_git_grants=external_git_grants,
        )

    def refresh(
        self,
        *,
        workspace_id: str,
        config_revision: str,
        change_detection_mode: Literal["fast", "verify_content"],
        authorization_receipt_id: str,
        external_git_grants: tuple[ExternalGitGrant, ...] = (),
    ) -> ScanResult:
        with self._database.read_connection() as connection:
            row = connection.execute(
                "SELECT canonical_root FROM workspaces WHERE workspace_id = ?", (workspace_id,)
            ).fetchone()
        if row is None:
            raise InvalidInputError("workspace_id is not registered")
        return self._run(
            root=Path(str(row["canonical_root"])),
            workspace_id=workspace_id,
            mode="refresh",
            change_detection_mode=change_detection_mode,
            config_revision=config_revision,
            authorization_receipt_id=authorization_receipt_id,
            external_git_grants=external_git_grants,
        )

    def overview(
        self,
        *,
        workspace_path: str,
        scan_run_id: str | None = None,
    ) -> dict[str, object]:
        """Return one reusable terminal scan without reading project sources."""
        root = Path(workspace_path).expanduser().resolve(strict=False)
        with self._database.read_connection() as connection:
            parameters: list[object] = [str(root)]
            requested_filter = ""
            if scan_run_id is not None:
                if not scan_run_id.strip() or len(scan_run_id) > 200:
                    raise InvalidInputError("scan_run_id must be a bounded non-empty value")
                requested_filter = " AND sr.scan_run_id = ?"
                parameters.append(scan_run_id)
            row = connection.execute(
                f"""
                SELECT sr.scan_run_id, sr.workspace_id, sr.status, sr.mode,
                       sr.change_detection_mode, sr.config_revision, sr.started_at,
                       sr.finished_at, sro.coverage_json
                FROM scan_runs AS sr
                JOIN workspaces AS w ON w.workspace_id = sr.workspace_id
                LEFT JOIN scan_run_overviews AS sro ON sro.scan_run_id = sr.scan_run_id
                WHERE w.canonical_root = ?
                  AND sr.status IN ('completed', 'partial', 'failed')
                  AND (sr.status IN ('completed', 'partial') OR sro.scan_run_id IS NOT NULL)
                  {requested_filter}
                ORDER BY sr.started_at DESC, sr.scan_run_id DESC
                LIMIT 1
                """,
                tuple(parameters),
            ).fetchone()
            if row is None:
                return {
                    "status": "ok",
                    "scan_overview": {
                        "contract_version": SCAN_OVERVIEW_CONTRACT_VERSION,
                        "found": False,
                        "workspace_path": str(root),
                        "scan_run": None,
                        "coverage": None,
                        "issues": [],
                        "limits": {
                            "issue_limit": MAX_SCAN_OVERVIEW_ISSUES,
                            "available_issues": 0,
                            "issues_truncated": False,
                        },
                    },
                }
            selected_scan_run_id = str(row["scan_run_id"])
            coverage = self._overview_coverage(connection, row, selected_scan_run_id)
            issue_count = int(
                connection.execute(
                    "SELECT COUNT(*) AS count FROM scan_issues WHERE scan_run_id = ?",
                    (selected_scan_run_id,),
                ).fetchone()["count"]
            )
            issue_rows = connection.execute(
                """
                SELECT issue_id, project_id, artifact_id, kind, severity,
                       relative_path, message, remediation
                FROM scan_issues
                WHERE scan_run_id = ?
                ORDER BY CASE severity
                    WHEN 'error' THEN 0
                    WHEN 'warning' THEN 1
                    ELSE 2
                END, issue_id
                LIMIT ?
                """,
                (selected_scan_run_id, MAX_SCAN_OVERVIEW_ISSUES),
            ).fetchall()
        return {
            "status": "ok",
            "scan_overview": {
                "contract_version": SCAN_OVERVIEW_CONTRACT_VERSION,
                "found": True,
                "workspace_path": str(root),
                "scan_run": {
                    "scan_run_id": selected_scan_run_id,
                    "workspace_id": str(row["workspace_id"]),
                    "status": str(row["status"]),
                    "mode": str(row["mode"]),
                    "change_detection_mode": row["change_detection_mode"],
                    "config_revision": str(row["config_revision"]),
                    "started_at": str(row["started_at"]),
                    "finished_at": row["finished_at"],
                },
                "coverage": coverage,
                "issues": [
                    {
                        "issue_id": str(issue["issue_id"]),
                        "project_id": issue["project_id"],
                        "artifact_id": issue["artifact_id"],
                        "kind": str(issue["kind"]),
                        "severity": str(issue["severity"]),
                        "relative_path": issue["relative_path"],
                        "message": str(issue["message"]),
                        "remediation": str(issue["remediation"]),
                    }
                    for issue in issue_rows
                ],
                "limits": {
                    "issue_limit": MAX_SCAN_OVERVIEW_ISSUES,
                    "available_issues": issue_count,
                    "issues_truncated": issue_count > len(issue_rows),
                },
            },
        }

    def _overview_coverage(
        self,
        connection: sqlite3.Connection,
        row: sqlite3.Row,
        scan_run_id: str,
    ) -> dict[str, object]:
        stored = row["coverage_json"]
        if stored is not None:
            try:
                value = json.loads(str(stored))
            except (json.JSONDecodeError, RecursionError) as exc:
                raise InvalidInputError("stored scan overview is not valid JSON") from exc
            if not isinstance(value, dict):
                raise InvalidInputError("stored scan overview is not a JSON object")
            return cast(dict[str, object], value)
        dispositions = Counter(
            {
                str(disposition["snapshot_disposition"]): int(disposition["count"])
                for disposition in connection.execute(
                    """
                    SELECT snapshot_disposition, COUNT(*) AS count
                    FROM scan_run_projects WHERE scan_run_id = ?
                    GROUP BY snapshot_disposition
                    """,
                    (scan_run_id,),
                ).fetchall()
            }
        )
        coverage = self._coverage(scan_run_id, dispositions, Counter())
        coverage["overview_provenance"] = "reconstructed_without_exclusion_counts"
        coverage["excluded_by_category_available"] = False
        return coverage

    def _run(
        self,
        *,
        root: Path,
        workspace_id: str | None,
        mode: Literal["full", "refresh"],
        change_detection_mode: Literal["fast", "verify_content"] | None,
        config_revision: str,
        authorization_receipt_id: str,
        external_git_grants: tuple[ExternalGitGrant, ...],
    ) -> ScanResult:
        if not config_revision.strip():
            raise InvalidInputError("config_revision must not be empty")
        root_issue = self._workspace_root_issue(root)
        if root_issue is not None:
            raise InvalidInputError(root_issue.message)
        scan_started_at = _now()
        workspace_id, scan_run_id = self._start_run(
            root=root,
            workspace_id=workspace_id,
            mode=mode,
            change_detection_mode=change_detection_mode,
            config_revision=config_revision,
            authorization_receipt_id=authorization_receipt_id,
            started_at=scan_started_at,
        )
        try:
            return self._run_started(
                root=root,
                workspace_id=workspace_id,
                scan_run_id=scan_run_id,
                mode=mode,
                change_detection_mode=change_detection_mode,
                config_revision=config_revision,
                external_git_grants=external_git_grants,
                scan_started_at=scan_started_at,
            )
        except BaseException:
            with suppress(Exception):
                self._finish_run(scan_run_id, "failed", None)
            raise

    def _run_started(
        self,
        *,
        root: Path,
        workspace_id: str,
        scan_run_id: str,
        mode: Literal["full", "refresh"],
        change_detection_mode: Literal["fast", "verify_content"] | None,
        config_revision: str,
        external_git_grants: tuple[ExternalGitGrant, ...],
        scan_started_at: str,
    ) -> ScanResult:
        plans, discovery_issues = self._discover(root, external_git_grants, scan_started_at)
        self._persist_issues(scan_run_id, None, tuple(discovery_issues))
        all_issues = list(discovery_issues)
        dispositions: Counter[str] = Counter()
        excluded: Counter[str] = Counter()
        discovered_ids: set[str] = set()

        for plan in plans:
            project = self._upsert_project(workspace_id, scan_run_id, root, plan)
            discovered_ids.add(project.project_id)
            try:
                project_data = self._read_project(
                    plan=plan,
                    config_revision=config_revision,
                    change_detection_mode=change_detection_mode,
                    nested_project_roots={
                        worktree.root
                        for candidate in plans
                        for worktree in candidate.worktrees
                        if worktree.root != plan.worktrees[0].root
                    },
                )
                project_issues, project_excluded = self._persist_project(
                    scan_run_id=scan_run_id,
                    project=project,
                    data=project_data,
                    config_revision=config_revision,
                    workspace_root=root,
                )
                all_issues.extend(project_issues)
                excluded.update(project_excluded)
                dispositions["fresh"] += 1
            except (OSError, sqlite3.Error, ValueError, UnicodeError):
                issue = _issue(
                    "project_index_failed",
                    "error",
                    "This project could not be indexed during the current run.",
                    "Fix local access or repository metadata, then run refresh.",
                    _relative_path(plan.worktrees[0].root, root),
                )
                disposition = self._record_project_failure(scan_run_id, project.project_id, issue)
                dispositions[disposition] += 1
                all_issues.append(issue)

        carried_issues, carried_dispositions = self._carry_forward_missing_projects(
            scan_run_id=scan_run_id,
            workspace_id=workspace_id,
            discovered_project_ids=discovered_ids,
        )
        all_issues.extend(carried_issues)
        dispositions.update(carried_dispositions)
        status = self._final_status(scan_run_id, dispositions, all_issues)
        coverage = self._coverage(scan_run_id, dispositions, excluded)
        self._finish_run(scan_run_id, status, coverage)
        return ScanResult(
            scan_run_id=scan_run_id,
            workspace_id=workspace_id,
            status=status,
            mode=mode,
            change_detection_mode=change_detection_mode,
            coverage=coverage,
            issues=tuple(all_issues),
        )

    @staticmethod
    def _workspace_root_issue(root: Path) -> ScanIssueDraft | None:
        try:
            root_fd = _open_directory(root)
            try:
                root_stat = os.fstat(root_fd)
            finally:
                os.close(root_fd)
        except OSError:
            return _issue(
                "workspace_unavailable",
                "error",
                "The authorized workspace is not a readable directory.",
                "Choose an existing local directory and run scan again.",
            )
        if not stat.S_ISDIR(root_stat.st_mode):
            return _issue(
                "workspace_root_changed",
                "error",
                "The workspace root changed after authorization and was not scanned.",
                "Reauthorize the current canonical workspace path and run scan again.",
            )
        return None

    def _start_run(
        self,
        *,
        root: Path,
        workspace_id: str | None,
        mode: str,
        change_detection_mode: str | None,
        config_revision: str,
        authorization_receipt_id: str,
        started_at: str,
    ) -> tuple[str, str]:
        now = started_at
        with self._database.write_transaction() as connection:
            self._recover_interrupted_runs(connection, now)
            if workspace_id is None:
                row = connection.execute(
                    "SELECT workspace_id FROM workspaces WHERE canonical_root = ?", (str(root),)
                ).fetchone()
                workspace_id = str(row["workspace_id"]) if row else _new_id()
                if row is None:
                    connection.execute(
                        """
                        INSERT INTO workspaces(
                            workspace_id, canonical_root, display_name,
                            registered_at, config_revision
                        )
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (workspace_id, str(root), root.name or str(root), now, config_revision),
                    )
                else:
                    connection.execute(
                        "UPDATE workspaces SET config_revision = ? WHERE workspace_id = ?",
                        (config_revision, workspace_id),
                    )
            else:
                connection.execute(
                    "UPDATE workspaces SET config_revision = ? WHERE workspace_id = ?",
                    (config_revision, workspace_id),
                )
            scan_run_id = _new_id()
            connection.execute(
                """
                INSERT INTO scan_runs(
                    scan_run_id, workspace_id, authorization_receipt_id, owner_process_identity,
                    mode, change_detection_mode, config_revision, started_at, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'running')
                """,
                (
                    scan_run_id,
                    workspace_id,
                    authorization_receipt_id,
                    _process_identity(),
                    mode,
                    change_detection_mode,
                    config_revision,
                    now,
                ),
            )
        return workspace_id, scan_run_id

    @staticmethod
    def _recover_interrupted_runs(connection: sqlite3.Connection, now: str) -> None:
        rows = connection.execute(
            """
            SELECT scan_run_id, owner_process_identity
            FROM scan_runs
            WHERE status = 'running'
            """
        ).fetchall()
        for row in rows:
            if not _owner_process_stopped(str(row["owner_process_identity"])):
                continue
            connection.execute(
                """
                UPDATE scan_runs
                SET status = 'interrupted', finished_at = ?
                WHERE scan_run_id = ? AND status = 'running'
                """,
                (now, str(row["scan_run_id"])),
            )

    def _discover(
        self,
        root: Path,
        external_git_grants: tuple[ExternalGitGrant, ...],
        scan_started_at: str,
    ) -> tuple[list[ProjectPlan], list[ScanIssueDraft]]:
        directories, issues = self._walk_directories(root)
        git_worktrees: list[WorktreePlan] = []
        blocked_roots: set[Path] = set()
        grants_by_pointer = {grant.git_pointer_path: grant for grant in external_git_grants}
        for directory in directories:
            marker = directory / ".git"
            marker_relative = _child_relative(_relative_to_root(directory, root), ".git")
            try:
                marker_stat = _safe_lstat(root, marker_relative)
            except FileNotFoundError:
                continue
            except OSError:
                issues.append(
                    _issue(
                        "git_marker_unreadable",
                        "warning",
                        "A Git marker could not be inspected.",
                        "Fix local permissions and run refresh.",
                        _relative_path(directory, root),
                    )
                )
                blocked_roots.add(directory)
                continue
            blocked_roots.add(directory)
            if stat.S_ISLNK(marker_stat.st_mode):
                issues.append(
                    _issue(
                        "untrusted_git_pointer",
                        "warning",
                        "A symbolic-link .git marker was not followed.",
                        (
                            "Use a normal repository root or grant a future explicit Git relation "
                            "probe."
                        ),
                        _relative_path(directory, root),
                    )
                )
                continue
            if stat.S_ISREG(marker_stat.st_mode):
                pointer_target = self._git_pointer_target_at(root, marker_relative)
                if pointer_target is None:
                    issues.append(
                        _issue(
                            "untrusted_git_pointer",
                            "warning",
                            "A linked-worktree .git pointer could not be parsed safely.",
                            "Repair the local Git pointer and run refresh.",
                            _relative_path(directory, root),
                        )
                    )
                    continue
                relation_state = self._linked_worktree_relation_state(root, marker, pointer_target)
                if relation_state == "trusted":
                    git_state, git_issue = self._git_state(
                        directory, root, scan_started_at=scan_started_at
                    )
                    if git_issue is not None:
                        issues.append(git_issue)
                        continue
                    assert git_state is not None
                    git_worktrees.append(WorktreePlan(directory, git_state))
                    continue
                if relation_state == "invalid":
                    issues.append(
                        _issue(
                            "untrusted_git_pointer",
                            "warning",
                            "A root-internal linked-worktree relation is malformed or changed.",
                            "Repair the local Git pointer relation and run refresh.",
                            _relative_path(directory, root),
                        )
                    )
                    continue
                grant = grants_by_pointer.get(marker)
                if grant is None:
                    issues.append(
                        _issue(
                            "external_git_authorization_required",
                            "warning",
                            (
                                "A linked-worktree .git pointer requires explicit two-stage "
                                "external Git authorization for its untrusted metadata candidate: "
                                f"{_short(str(pointer_target), 220)}."
                            ),
                            "Authorize the Git relation probe before indexing this worktree.",
                            _relative_path(directory, root),
                        )
                    )
                    continue
                git_state, git_issue = self._external_git_state(directory, root, marker, grant)
                if git_issue is not None:
                    issues.append(git_issue)
                    continue
                assert git_state is not None
                git_worktrees.append(WorktreePlan(directory, git_state))
                continue
            if not stat.S_ISDIR(marker_stat.st_mode):
                issues.append(
                    _issue(
                        "broken_repository",
                        "warning",
                        "The .git marker is neither a directory nor a supported pointer file.",
                        "Repair the local repository metadata and run refresh.",
                        _relative_path(directory, root),
                    )
                )
                continue
            directory_relation = self._git_directory_relation_state(root, marker_relative)
            if directory_relation == "invalid":
                issues.append(
                    _issue(
                        "untrusted_git_pointer",
                        "warning",
                        "A .git directory has a malformed or changing commondir relation.",
                        "Repair the local Git metadata and run refresh.",
                        _relative_path(directory, root),
                    )
                )
                continue
            if directory_relation == "external":
                grant = grants_by_pointer.get(marker)
                if grant is None:
                    common_candidate = self._relation_target_at(root, marker_relative, "commondir")
                    issues.append(
                        _issue(
                            "external_git_authorization_required",
                            "warning",
                            (
                                "A .git directory requires explicit two-stage external Git "
                                "authorization for its commondir candidate: "
                                f"{_short(str(common_candidate), 220)}."
                            ),
                            "Inspect and authorize the Git relation before indexing this worktree.",
                            _relative_path(directory, root),
                        )
                    )
                    continue
                git_state, git_issue = self._external_git_state(directory, root, marker, grant)
                if git_issue is not None:
                    issues.append(git_issue)
                    continue
                assert git_state is not None
                git_worktrees.append(WorktreePlan(directory, git_state))
                continue
            git_state, git_issue = self._git_state(directory, root, scan_started_at=scan_started_at)
            if git_issue is not None:
                issues.append(git_issue)
                continue
            assert git_state is not None
            git_worktrees.append(WorktreePlan(directory, git_state))

        grouped: dict[str, list[WorktreePlan]] = {}
        for worktree in git_worktrees:
            assert worktree.git_state is not None
            grouped.setdefault(str(worktree.git_state.common_dir), []).append(worktree)
        projects: list[ProjectPlan] = [
            ProjectPlan(
                identity_kind="git_common_dir",
                identity_key=identity_key,
                display_name=sorted(worktrees, key=lambda item: str(item.root))[0].root.name,
                worktrees=tuple(sorted(worktrees, key=lambda item: str(item.root))),
            )
            for identity_key, worktrees in sorted(grouped.items())
        ]

        git_roots = {worktree.root for worktree in git_worktrees} | blocked_roots
        for directory in directories:
            if any(_is_within(directory, git_root) for git_root in git_roots):
                continue
            if not self._non_git_manifest(root, _relative_to_root(directory, root)):
                continue
            projects.append(
                ProjectPlan(
                    identity_kind="non_git_root",
                    identity_key=str(directory),
                    display_name=directory.name,
                    worktrees=(WorktreePlan(directory, None),),
                )
            )
        projects.sort(key=lambda project: (project.identity_kind, project.identity_key))
        return projects, issues

    @staticmethod
    def _linked_worktree_relation_state(
        workspace_root: Path, marker: Path, git_dir: Path
    ) -> Literal["trusted", "external", "invalid"]:
        if not _is_within(git_dir, workspace_root):
            return "external"
        try:
            git_dir_relative = _relative_to_root(git_dir, workspace_root)
            git_dir_fd = _open_directory(workspace_root, git_dir_relative)
        except OSError:
            return "invalid"
        else:
            os.close(git_dir_fd)
        common_dir = WorkspaceScanner._relation_target_at(
            workspace_root, git_dir_relative, "commondir"
        )
        back_pointer = WorkspaceScanner._relation_target_at(
            workspace_root, git_dir_relative, "gitdir"
        )
        if common_dir is None or back_pointer != marker:
            return "invalid"
        if not _is_within(common_dir, workspace_root):
            return "external"
        try:
            common_dir_fd = _open_directory(
                workspace_root, _relative_to_root(common_dir, workspace_root)
            )
        except OSError:
            return "invalid"
        else:
            os.close(common_dir_fd)
        return "trusted"

    @staticmethod
    def _git_directory_relation_state(
        workspace_root: Path, marker_relative: str
    ) -> Literal["trusted", "external", "invalid"]:
        commondir_relative = _child_relative(marker_relative, "commondir")
        try:
            commondir_stat = _safe_lstat(workspace_root, commondir_relative)
        except FileNotFoundError:
            return "trusted"
        except OSError:
            return "invalid"
        if not stat.S_ISREG(commondir_stat.st_mode):
            return "invalid"
        common_dir = WorkspaceScanner._relation_target_at(
            workspace_root, marker_relative, "commondir"
        )
        if common_dir is None:
            return "invalid"
        if not _is_within(common_dir, workspace_root):
            return "external"
        try:
            common_fd = _open_directory(
                workspace_root, _relative_to_root(common_dir, workspace_root)
            )
        except OSError:
            return "invalid"
        else:
            os.close(common_fd)
        return "trusted"

    @staticmethod
    def _bind_internal_git(worktree_root: Path, workspace_root: Path) -> InternalGitBinding | None:
        """Bind an internal repository without letting Git rediscover a mutable marker."""
        marker = worktree_root / ".git"
        marker_relative = _child_relative(_relative_to_root(worktree_root, workspace_root), ".git")
        try:
            marker_stat = _safe_lstat(workspace_root, marker_relative)
        except OSError:
            return None

        if stat.S_ISREG(marker_stat.st_mode):
            git_dir = WorkspaceScanner._git_pointer_target_at(workspace_root, marker_relative)
            if git_dir is None or not _is_within(git_dir, workspace_root):
                return None
        elif stat.S_ISDIR(marker_stat.st_mode):
            git_dir = marker
        else:
            return None

        try:
            worktree_fd = _open_directory(
                workspace_root, _relative_to_root(worktree_root, workspace_root)
            )
        except OSError:
            return None
        try:
            git_dir_fd = _open_directory(workspace_root, _relative_to_root(git_dir, workspace_root))
        except OSError:
            os.close(worktree_fd)
            return None
        try:
            if stat.S_ISREG(marker_stat.st_mode):
                common_dir = WorkspaceScanner._relation_target_from_fd(
                    git_dir_fd, git_dir, "commondir"
                )
                back_pointer = WorkspaceScanner._relation_target_from_fd(
                    git_dir_fd, git_dir, "gitdir"
                )
                if common_dir is None or back_pointer != marker:
                    return None
            else:
                try:
                    common_stat = os.stat("commondir", dir_fd=git_dir_fd, follow_symlinks=False)
                except FileNotFoundError:
                    common_dir = git_dir
                except OSError:
                    return None
                else:
                    if not stat.S_ISREG(common_stat.st_mode):
                        return None
                    common_dir = WorkspaceScanner._relation_target_from_fd(
                        git_dir_fd, git_dir, "commondir"
                    )
                    if common_dir is None:
                        return None
            if not _is_within(common_dir, workspace_root):
                return None
            try:
                common_dir_fd = _open_directory(
                    workspace_root, _relative_to_root(common_dir, workspace_root)
                )
            except OSError:
                return None
            try:
                return InternalGitBinding(
                    workspace_root=workspace_root,
                    worktree_root=worktree_root,
                    git_dir=git_dir,
                    common_dir=common_dir,
                    worktree_identity=_directory_identity(worktree_fd),
                    git_dir_identity=_directory_identity(git_dir_fd),
                    common_dir_identity=_directory_identity(common_dir_fd),
                )
            finally:
                os.close(common_dir_fd)
        finally:
            os.close(git_dir_fd)
            os.close(worktree_fd)

    def _walk_directories(self, root: Path) -> tuple[list[Path], list[ScanIssueDraft]]:
        directories: list[Path] = []
        issues: list[ScanIssueDraft] = []
        stack = ["."]
        seen: set[str] = set()
        while stack:
            relative_directory = stack.pop()
            if relative_directory in seen:
                continue
            seen.add(relative_directory)
            directory = root if relative_directory == "." else root / relative_directory
            directories.append(directory)
            try:
                directory_fd = _open_directory(root, relative_directory)
            except OSError:
                issues.append(
                    _issue(
                        "directory_unreadable",
                        "warning",
                        "A directory could not be listed during discovery.",
                        "Fix local permissions and run refresh.",
                        _relative_path(directory, root),
                    )
                )
                continue
            try:
                try:
                    with os.scandir(os.dup(directory_fd)) as scanned:
                        entries = sorted(scanned, key=lambda entry: entry.name)
                except OSError:
                    issues.append(
                        _issue(
                            "directory_unreadable",
                            "warning",
                            "A directory could not be listed during discovery.",
                            "Fix local permissions and run refresh.",
                            _relative_path(directory, root),
                        )
                    )
                    continue
                for entry in reversed(entries):
                    try:
                        entry_stat = entry.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    child_relative = _child_relative(relative_directory, entry.name)
                    if stat.S_ISLNK(entry_stat.st_mode):
                        try:
                            target = os.readlink(entry.name, dir_fd=directory_fd)
                        except OSError:
                            target = ".."
                        kind = (
                            "symlink_outside_authorized_root"
                            if _symlink_may_escape(relative_directory, target)
                            else "symlink_skipped"
                        )
                        issues.append(
                            _issue(
                                kind,
                                "warning" if kind == "symlink_outside_authorized_root" else "info",
                                "A symbolic link was not followed during workspace discovery.",
                                "Use a regular file or directory when it must be indexed.",
                                child_relative,
                            )
                        )
                        continue
                    if (
                        stat.S_ISDIR(entry_stat.st_mode)
                        and entry.name.lower() not in HARD_EXCLUDED_DIRECTORIES
                    ):
                        stack.append(child_relative)
            finally:
                os.close(directory_fd)
        return directories, issues

    def _external_git_state(
        self,
        root: Path,
        workspace_root: Path,
        marker: Path,
        grant: ExternalGitGrant,
    ) -> tuple[GitState | None, ScanIssueDraft | None]:
        relative_root = _relative_path(root, workspace_root)
        try:
            inspected = inspect_external_git_candidate(workspace_root, marker)
            expected_common_candidate = (
                None if not _is_within(grant.git_dir, workspace_root) else str(grant.common_dir)
            )
            if (
                inspected.get("marker_kind") != grant.marker_kind
                or inspected.get("git_dir_candidate") != str(grant.git_dir)
                or inspected.get("common_dir_candidate") != expected_common_candidate
            ):
                raise ValueError("the root-internal Git candidate changed")
            git_dir_fd = _open_absolute_directory(grant.git_dir)
            try:
                common_dir_fd = _open_absolute_directory(grant.common_dir)
                try:
                    if _directory_identity(git_dir_fd) != (
                        grant.git_dir_device,
                        grant.git_dir_inode,
                    ) or _directory_identity(common_dir_fd) != (
                        grant.common_dir_device,
                        grant.common_dir_inode,
                    ):
                        raise ValueError("an authorized external Git directory changed identity")
                    common_target = self._relation_target_from_fd(
                        git_dir_fd, grant.git_dir, "commondir"
                    )
                    if common_target != grant.common_dir:
                        raise ValueError("the authorized commondir relation changed")
                    if grant.marker_kind == "file":
                        root_target = self._relation_target_from_fd(
                            git_dir_fd, grant.git_dir, "gitdir"
                        )
                        if root_target != marker:
                            raise ValueError("the authorized gitdir back-pointer changed")
                    branch, head, read_fields = self._external_head_state(git_dir_fd, common_dir_fd)
                    if not self._external_directory_identity_matches(
                        grant.git_dir,
                        (grant.git_dir_device, grant.git_dir_inode),
                    ) or not self._external_directory_identity_matches(
                        grant.common_dir,
                        (grant.common_dir_device, grant.common_dir_inode),
                    ):
                        raise ValueError("an authorized external Git directory path changed")
                finally:
                    os.close(common_dir_fd)
            finally:
                os.close(git_dir_fd)
            inspected_after = inspect_external_git_candidate(workspace_root, marker)
            if inspected_after != inspected:
                raise ValueError("the root-internal Git marker changed during metadata reading")
        except (InvalidInputError, OSError, UnicodeError, ValueError):
            return None, _issue(
                "external_git_relation_mismatch",
                "warning",
                "The authorized Git relation changed or contains invalid bounded metadata.",
                "Run a new candidate inspection and confirm the current exact Git relation.",
                relative_root,
            )
        return (
            GitState(
                grant.git_dir,
                grant.common_dir,
                branch,
                head,
                "not_applicable",
                {},
                "external_metadata_only",
                (),
                True,
                grant,
                read_fields,
                (
                    _issue(
                        "external_git_history_unavailable",
                        "warning",
                        "External Git metadata authorization excludes history and object reads.",
                        "Expand the workspace explicitly before requesting Git history analysis.",
                        relative_root,
                    ),
                    _issue(
                        "external_git_worktree_state_unavailable",
                        "warning",
                        "External metadata-only mode does not infer index or dirty-state details.",
                        "Treat source commit state as unknown unless the workspace scope expands.",
                        relative_root,
                    ),
                ),
            ),
            None,
        )

    @staticmethod
    def _external_head_state(
        git_dir_fd: int, common_dir_fd: int
    ) -> tuple[str | None, str | None, tuple[str, ...]]:
        raw_head = _read_text_at(git_dir_fd, "HEAD", maximum_bytes=4096)
        lines = raw_head.splitlines()
        if len(lines) != 1:
            raise ValueError("external Git HEAD must contain one line")
        head_value = lines[0].strip()
        if head_value.startswith("ref: "):
            reference = head_value.removeprefix("ref: ").strip()
            if not WorkspaceScanner._safe_git_reference(reference):
                raise ValueError("external Git HEAD contains an unsafe reference")
            head = WorkspaceScanner._external_ref_commit(common_dir_fd, reference)
            branch = (
                reference.removeprefix("refs/heads/")
                if reference.startswith("refs/heads/")
                else None
            )
            return branch, head, ("gitdir", "commondir", "head", "ref")
        if not _valid_git_commit(head_value):
            raise ValueError("external Git HEAD contains an invalid commit identity")
        return None, head_value, ("gitdir", "commondir", "head")

    @staticmethod
    def _safe_git_reference(reference: str) -> bool:
        if not reference.startswith("refs/") or len(reference) > 1024:
            return False
        if reference.endswith(("/", ".", ".lock")) or ".." in reference or "@{" in reference:
            return False
        forbidden = set(" ~^:?*[\\")
        return all(
            32 < ord(character) < 127 and character not in forbidden for character in reference
        ) and all(part not in {"", ".", ".."} for part in reference.split("/"))

    @staticmethod
    def _external_ref_commit(common_dir_fd: int, reference: str) -> str | None:
        try:
            raw_reference = _read_text_at(common_dir_fd, reference, maximum_bytes=4096)
        except FileNotFoundError:
            raw_reference = ""
        if raw_reference:
            lines = raw_reference.splitlines()
            if len(lines) != 1 or not _valid_git_commit(lines[0].strip()):
                raise ValueError("external Git loose reference is invalid")
            return lines[0].strip()
        try:
            packed = _read_text_at(common_dir_fd, "packed-refs", maximum_bytes=MAX_FILE_BYTES)
        except FileNotFoundError:
            return None
        match: str | None = None
        for line in packed.splitlines():
            if not line or line.startswith(("#", "^")):
                continue
            commit, separator, name = line.partition(" ")
            if not separator or not _valid_git_commit(commit) or not name:
                raise ValueError("external Git packed-refs contains invalid data")
            if name == reference:
                if match is not None and match != commit:
                    raise ValueError("external Git packed reference is ambiguous")
                match = commit
        return match

    @staticmethod
    def _external_directory_identity_matches(path: Path, expected: tuple[int, int]) -> bool:
        try:
            directory_fd = _open_absolute_directory(path)
        except OSError:
            return False
        try:
            return _directory_identity(directory_fd) == expected
        finally:
            os.close(directory_fd)

    @staticmethod
    def _git_pointer_target(root: Path) -> Path | None:
        return WorkspaceScanner._git_pointer_target_at(root, ".git")

    @staticmethod
    def _git_pointer_target_at(root: Path, relative_pointer: str) -> Path | None:
        try:
            file_fd, _ = _open_regular_file(root, relative_pointer)
            try:
                payload = _read_open_file(file_fd, maximum_bytes=4096).decode("utf-8")
            finally:
                os.close(file_fd)
        except (OSError, UnicodeError):
            return None
        lines = payload.splitlines()
        if len(lines) != 1 or not lines[0].startswith("gitdir: "):
            return None
        raw_target = lines[0].removeprefix("gitdir: ").strip()
        if not raw_target or "\x00" in raw_target:
            return None
        target = Path(raw_target)
        if not target.is_absolute():
            target = root / PurePosixPath(relative_pointer).parent / target
        return Path(os.path.normpath(str(target)))

    @staticmethod
    def _relation_target_from_fd(git_dir_fd: int, git_dir: Path, filename: str) -> Path | None:
        try:
            raw_target = _read_text_at(git_dir_fd, filename, maximum_bytes=4096).strip()
        except (OSError, UnicodeError):
            return None
        if not raw_target or "\x00" in raw_target:
            return None
        target = Path(raw_target)
        if not target.is_absolute():
            target = git_dir / target
        return Path(os.path.normpath(str(target)))

    @staticmethod
    def _relation_target_at(root: Path, relative_git_dir: str, filename: str) -> Path | None:
        try:
            relative_file = _child_relative(relative_git_dir, filename)
            file_fd, _ = _open_regular_file(root, relative_file)
            try:
                raw_target = _read_open_file(file_fd, maximum_bytes=4096).decode("utf-8").strip()
            finally:
                os.close(file_fd)
        except (OSError, UnicodeError):
            return None
        if not raw_target or "\x00" in raw_target:
            return None
        target = Path(raw_target)
        if not target.is_absolute():
            target = root / relative_git_dir / target
        return Path(os.path.normpath(str(target)))

    def _git_state(
        self,
        root: Path,
        workspace_root: Path,
        *,
        scan_started_at: str,
    ) -> tuple[GitState | None, ScanIssueDraft | None]:
        binding = self._bind_internal_git(root, workspace_root)
        if binding is None:
            return None, _issue(
                "broken_repository",
                "warning",
                "Local Git metadata changed or could not be bound to the authorized workspace.",
                "Repair the repository metadata and run refresh.",
                _relative_path(root, workspace_root),
            )
        try:
            result = self._git(binding, "rev-parse", "--is-inside-work-tree")
            if result.returncode != 0 or result.stdout.strip() != "true":
                boundary_denied = any(
                    marker in result.stderr
                    for marker in ("Operation not permitted", "Permission denied")
                )
                return None, _issue(
                    "git_repository_boundary_violation" if boundary_denied else "broken_repository",
                    "warning",
                    (
                        "Repository metadata requested a path outside the authorized Git sandbox."
                        if boundary_denied
                        else "Local Git metadata could not be read through its bound repository "
                        "identity."
                    ),
                    (
                        "Remove root-external Git config or object indirection, then run refresh."
                        if boundary_denied
                        else "Repair the repository metadata and run refresh."
                    ),
                    _relative_path(root, workspace_root),
                )
            branch_result = self._git(binding, "symbolic-ref", "--quiet", "--short", "HEAD")
            branch = branch_result.stdout.strip() if branch_result.returncode == 0 else None
            head_result = self._git(binding, "rev-parse", "--verify", "HEAD")
            head = head_result.stdout.strip() if head_result.returncode == 0 else None
            if head is not None and not _valid_git_commit(head):
                return None, _issue(
                    "broken_repository",
                    "warning",
                    "Local Git metadata returned an invalid HEAD commit identity.",
                    "Repair the repository metadata and run refresh.",
                    _relative_path(root, workspace_root),
                )
            status_result = self._git(
                binding,
                "status",
                "--porcelain=v1",
                "-z",
                "--untracked-files=all",
                maximum_output_bytes=MAX_GIT_COMMAND_BYTES,
            )
        except subprocess.TimeoutExpired:
            return None, _issue(
                "git_command_timeout",
                "warning",
                "A bounded local Git command exceeded the scan time limit.",
                "Reduce repository pressure or repair Git integrations, then run refresh.",
                _relative_path(root, workspace_root),
            )
        except OSError:
            return None, _issue(
                "git_command_resource_limit",
                "warning",
                "A local Git command exceeded an output limit or lost its bound repository.",
                "Repair the repository metadata or reduce generated files, then run refresh.",
                _relative_path(root, workspace_root),
            )
        if status_result.returncode != 0:
            return None, _issue(
                "git_status_unavailable",
                "warning",
                "Git working-tree state could not be read.",
                "Repair local Git metadata and run refresh.",
                _relative_path(root, workspace_root),
            )
        states = self._parse_git_status(status_result.stdout)
        state_values = set(states.values())
        dirty_state = (
            "mixed"
            if "modified" in state_values and "untracked" in state_values
            else "modified"
            if "modified" in state_values
            else "untracked"
            if "untracked" in state_values
            else "clean"
        )
        history_basis, history_entries, history_issues = self._recent_git_history(
            binding, head, branch, scan_started_at
        )
        return (
            GitState(
                binding.git_dir,
                binding.common_dir,
                branch,
                head,
                dirty_state,
                states,
                history_basis,
                history_entries,
                False,
                None,
                (),
                history_issues,
            ),
            None,
        )

    def _recent_git_history(
        self,
        binding: InternalGitBinding,
        head: str | None,
        branch: str | None,
        scan_started_at: str,
    ) -> tuple[str, tuple[GitHistoryEntry, ...], tuple[ScanIssueDraft, ...]]:
        relative_root = _relative_path(binding.worktree_root, binding.workspace_root)
        revisions: tuple[str, ...]
        if head is None:
            return "head_only_unborn", (), ()
        try:
            if branch is None:
                history_basis = "head_only_detached"
                revisions = (head,)
            else:
                default_commit, history_basis = self._default_history_commit(binding)
                revisions = (
                    (head,)
                    if default_commit is None or default_commit == head
                    else (
                        head,
                        default_commit,
                    )
                )
            entries, truncated = self._read_recent_history(
                binding, revisions, head, scan_started_at
            )
        except (OSError, ValueError, subprocess.TimeoutExpired):
            return (
                "history_unavailable",
                (),
                (
                    _issue(
                        "git_history_unavailable",
                        "warning",
                        "Recent local Git history could not be read with the fixed bounded "
                        "commands.",
                        "Repair local Git metadata and run refresh; no network fetch was "
                        "attempted.",
                        relative_root,
                    ),
                ),
            )
        if not truncated:
            return history_basis, entries, ()
        return (
            history_basis,
            entries,
            (
                _issue(
                    "git_history_truncated",
                    "warning",
                    "Recent Git history exceeded a local metadata safety limit.",
                    "Use a narrower later evidence query when the omitted history is needed.",
                    relative_root,
                ),
            ),
        )

    def _default_history_commit(self, binding: InternalGitBinding) -> tuple[str | None, str]:
        return_code, raw_targets, _ = self._git_bounded_bytes(
            binding,
            "for-each-ref",
            "--format=%(symref)",
            "refs/remotes/*/HEAD",
            maximum_output_bytes=MAX_REMOTE_HEAD_BYTES,
        )
        if return_code != 0:
            raise ValueError("could not list local remote HEAD references")
        try:
            decoded_targets = raw_targets.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("local remote HEAD references are not UTF-8") from exc
        targets = {line.strip() for line in decoded_targets.splitlines() if line.strip()}
        if len(targets) > 1:
            return None, "head_only_ambiguous_remote_head"
        if len(targets) == 1:
            target = next(iter(targets))
            if not target.startswith("refs/remotes/"):
                return None, "head_only_invalid_remote_head"
            commit = self._verified_git_commit(binding, target)
            if commit is None:
                return None, "head_only_invalid_remote_head"
            return commit, "head_plus_remote_head"
        main = self._verified_git_commit(binding, "refs/heads/main")
        if main is not None:
            return main, "head_plus_main"
        master = self._verified_git_commit(binding, "refs/heads/master")
        if master is not None:
            return master, "head_plus_master"
        return None, "head_only_no_default_ref"

    def _verified_git_commit(self, binding: InternalGitBinding, reference: str) -> str | None:
        result = self._git(
            binding,
            "rev-parse",
            "--verify",
            "--quiet",
            "--end-of-options",
            f"{reference}^{{commit}}",
        )
        candidate = result.stdout.strip()
        if result.returncode != 0 or not _valid_git_commit(candidate):
            return None
        return candidate

    def _read_recent_history(
        self,
        binding: InternalGitBinding,
        revisions: tuple[str, ...],
        head: str,
        scan_started_at: str,
    ) -> tuple[tuple[GitHistoryEntry, ...], bool]:
        try:
            started_at = datetime.fromisoformat(scan_started_at.removesuffix("Z") + "+00:00")
        except ValueError as exc:
            raise ValueError("scan start time is invalid") from exc
        cutoff = (
            (started_at - timedelta(days=HISTORY_WINDOW_DAYS)).isoformat().replace("+00:00", "Z")
        )
        metadata = self._git_bounded_bytes(
            binding,
            "log",
            "--no-ext-diff",
            "--no-textconv",
            "--date-order",
            f"--max-count={MAX_HISTORY_COMMITS + 1}",
            f"--since={cutoff}",
            "-z",
            "--format=%H%x00%ct%x00%an%x00%ae%x00%s",
            *revisions,
            "--",
            maximum_output_bytes=MAX_HISTORY_METADATA_BYTES,
        )
        if metadata[0] != 0:
            raise ValueError("Git history metadata command failed")
        records = self._parse_history_metadata(metadata[1])
        if head not in {record[0] for record in records}:
            head_metadata = self._git_bounded_bytes(
                binding,
                "log",
                "--no-ext-diff",
                "--no-textconv",
                "--no-walk",
                "-z",
                "--format=%H%x00%ct%x00%an%x00%ae%x00%s",
                head,
                "--",
                maximum_output_bytes=MAX_HISTORY_METADATA_BYTES,
            )
            if head_metadata[0] != 0:
                raise ValueError("Git HEAD metadata command failed")
            records = self._parse_history_metadata(head_metadata[1]) + records
        ordered: list[tuple[str, str, str, str, str]] = []
        seen_commits: set[str] = set()
        for record in records:
            if record[0] in seen_commits:
                continue
            seen_commits.add(record[0])
            ordered.append(record)
        truncated = len(ordered) > MAX_HISTORY_COMMITS
        selected = ordered[:MAX_HISTORY_COMMITS]
        entries: list[GitHistoryEntry] = []
        for commit, committed_at, author_name, author_email, subject in selected:
            paths, paths_truncated = self._history_paths(binding, commit)
            entries.append(
                GitHistoryEntry(
                    commit=commit,
                    committed_at=committed_at,
                    author_name=author_name,
                    author_email=author_email,
                    subject=subject,
                    changed_paths=paths,
                    paths_truncated=paths_truncated,
                )
            )
            truncated = truncated or paths_truncated
        return tuple(entries), truncated

    @staticmethod
    def _parse_history_metadata(raw: bytes) -> list[tuple[str, str, str, str, str]]:
        fields = raw.split(b"\0")
        if fields and fields[-1] == b"":
            fields.pop()
        if len(fields) % 5 != 0:
            raise ValueError("Git history metadata has an invalid record shape")
        records: list[tuple[str, str, str, str, str]] = []
        for index in range(0, len(fields), 5):
            commit_raw, timestamp_raw, author_raw, email_raw, subject_raw = fields[
                index : index + 5
            ]
            if any(
                len(value) > MAX_HISTORY_FIELD_BYTES
                for value in (author_raw, email_raw, subject_raw)
            ):
                raise ValueError("Git history metadata field exceeded the local limit")
            try:
                commit = commit_raw.decode("ascii")
            except UnicodeDecodeError as exc:
                raise ValueError("Git history returned a non-ASCII commit identity") from exc
            if not _valid_git_commit(commit):
                raise ValueError("Git history returned an invalid commit identity")
            records.append(
                (
                    commit,
                    _history_timestamp(timestamp_raw),
                    author_raw.decode("utf-8", errors="replace"),
                    email_raw.decode("utf-8", errors="replace"),
                    subject_raw.decode("utf-8", errors="replace"),
                )
            )
        return records

    def _history_paths(
        self, binding: InternalGitBinding, commit: str, *pathspecs: str
    ) -> tuple[tuple[str, ...], bool]:
        result = self._git_bounded_bytes(
            binding,
            "diff-tree",
            "--no-ext-diff",
            "--no-textconv",
            "--no-commit-id",
            "--root",
            "--name-only",
            "-r",
            "-z",
            commit,
            "--",
            *pathspecs,
            maximum_output_bytes=MAX_HISTORY_PATH_BYTES,
        )
        if result[0] != 0:
            raise ValueError("Git history path-range command failed")
        paths: list[str] = []
        truncated = False
        for raw_path in result[1].split(b"\0"):
            if not raw_path:
                continue
            if len(raw_path) > MAX_HISTORY_FIELD_BYTES:
                truncated = True
                continue
            try:
                path = raw_path.decode("utf-8")
            except UnicodeDecodeError:
                truncated = True
                continue
            if not _safe_history_path(path):
                truncated = True
                continue
            if len(paths) == MAX_HISTORY_PATHS_PER_COMMIT:
                truncated = True
                continue
            paths.append(path)
        return tuple(paths), truncated

    def _git_command(
        self,
        binding: InternalGitBinding,
        arguments: tuple[str, ...],
    ) -> list[str]:
        if sys.platform != "darwin" or not SANDBOX_EXECUTABLE.is_file():
            raise OSError("a supported local Git filesystem sandbox is unavailable")
        git_command = [
            self._git_executable,
            "--no-lazy-fetch",
            "--no-replace-objects",
            f"--git-dir={binding.git_dir}",
            f"--work-tree={binding.worktree_root}",
            "-c",
            "core.fsmonitor=false",
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "core.pager=cat",
            "--no-pager",
            *arguments,
        ]
        return [
            str(SANDBOX_EXECUTABLE),
            "-p",
            GIT_SANDBOX_PROFILE,
            "-D",
            f"AUTHORIZED_ROOT={binding.workspace_root}",
            "-D",
            f"GIT_EXECUTABLE={self._git_executable}",
            *git_command,
        ]

    @staticmethod
    def _open_bound_git_directory(
        binding: InternalGitBinding, path: Path, expected_identity: tuple[int, int]
    ) -> int:
        if not _is_within(path, binding.workspace_root):
            raise OSError("bound Git directory escaped the authorized workspace")
        directory_fd = _open_directory(
            binding.workspace_root, _relative_to_root(path, binding.workspace_root)
        )
        if _directory_identity(directory_fd) != expected_identity:
            os.close(directory_fd)
            raise OSError("bound Git directory identity changed")
        return directory_fd

    def _git_bounded_bytes(
        self, binding: InternalGitBinding, *arguments: str, maximum_output_bytes: int
    ) -> tuple[int, bytes, bytes]:
        worktree_fd = self._open_bound_git_directory(
            binding, binding.worktree_root, binding.worktree_identity
        )
        try:
            git_dir_fd = self._open_bound_git_directory(
                binding, binding.git_dir, binding.git_dir_identity
            )
        except Exception:
            os.close(worktree_fd)
            raise
        try:
            common_dir_fd = self._open_bound_git_directory(
                binding, binding.common_dir, binding.common_dir_identity
            )
        except Exception:
            os.close(git_dir_fd)
            os.close(worktree_fd)
            raise
        process: subprocess.Popen[bytes] | None = None
        selector = selectors.DefaultSelector()
        try:
            command = self._git_command(
                binding,
                arguments,
            )
            environment = {
                **GIT_ENV,
                "GIT_COMMON_DIR": str(binding.common_dir),
            }
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=environment,
                pass_fds=(worktree_fd, git_dir_fd, common_dir_fd),
                preexec_fn=partial(os.fchdir, worktree_fd),
                start_new_session=True,
            )
        finally:
            os.close(common_dir_fd)
            os.close(git_dir_fd)
            os.close(worktree_fd)
        assert process is not None
        assert process.stdout is not None
        assert process.stderr is not None
        outputs: dict[str, bytearray] = {"stdout": bytearray(), "stderr": bytearray()}
        try:
            selector.register(process.stdout, selectors.EVENT_READ, "stdout")
            selector.register(process.stderr, selectors.EVENT_READ, "stderr")
            deadline = time.monotonic() + GIT_COMMAND_TIMEOUT_SECONDS
            while selector.get_map():
                timeout = deadline - time.monotonic()
                if timeout <= 0:
                    raise subprocess.TimeoutExpired(process.args, GIT_COMMAND_TIMEOUT_SECONDS)
                events = selector.select(timeout)
                if not events:
                    raise subprocess.TimeoutExpired(process.args, GIT_COMMAND_TIMEOUT_SECONDS)
                for key, _ in events:
                    stream = cast(BinaryIO, key.fileobj)
                    chunk = os.read(stream.fileno(), 64 * 1024)
                    if not chunk:
                        selector.unregister(stream)
                        stream.close()
                        continue
                    total = sum(len(value) for value in outputs.values()) + len(chunk)
                    if total > maximum_output_bytes:
                        raise OSError("Git command exceeded its local output limit")
                    outputs[str(key.data)].extend(chunk)
            timeout = deadline - time.monotonic()
            if timeout <= 0:
                raise subprocess.TimeoutExpired(process.args, GIT_COMMAND_TIMEOUT_SECONDS)
            return_code = process.wait(timeout=timeout)
            self._verify_git_binding(binding)
            return return_code, bytes(outputs["stdout"]), bytes(outputs["stderr"])
        except BaseException:
            if process.poll() is None:
                with suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=GIT_COMMAND_TIMEOUT_SECONDS)
            raise
        finally:
            selector.close()
            for stream in (cast(BinaryIO, process.stdout), cast(BinaryIO, process.stderr)):
                if not stream.closed:
                    stream.close()

    def _verify_git_binding(self, binding: InternalGitBinding) -> None:
        for path, identity in (
            (binding.worktree_root, binding.worktree_identity),
            (binding.git_dir, binding.git_dir_identity),
            (binding.common_dir, binding.common_dir_identity),
        ):
            directory_fd = self._open_bound_git_directory(binding, path, identity)
            os.close(directory_fd)

    def _git(
        self,
        binding: InternalGitBinding,
        *arguments: str,
        maximum_output_bytes: int = MAX_GIT_COMMAND_BYTES,
    ) -> subprocess.CompletedProcess[str]:
        return_code, stdout, stderr = self._git_bounded_bytes(
            binding, *arguments, maximum_output_bytes=maximum_output_bytes
        )
        return subprocess.CompletedProcess(
            args=list(arguments),
            returncode=return_code,
            stdout=stdout.decode("utf-8", errors="replace"),
            stderr=stderr.decode("utf-8", errors="replace"),
        )

    @staticmethod
    def _parse_git_status(output: str) -> dict[str, str]:
        records = output.split("\0")
        states: dict[str, str] = {}
        index = 0
        while index < len(records):
            record = records[index]
            index += 1
            if not record:
                continue
            if len(record) < 4:
                continue
            status, path = record[:2], record[3:]
            if status[0] in "RC" or status[1] in "RC":
                index += 1
            states[path] = "untracked" if status == "??" else "modified"
        return states

    @staticmethod
    def _non_git_manifest(root: Path, relative_directory: str) -> bool:
        try:
            directory_fd = _open_directory(root, relative_directory)
        except OSError:
            return False
        try:
            try:
                with os.scandir(os.dup(directory_fd)) as entries:
                    for entry in entries:
                        try:
                            entry_stat = entry.stat(follow_symlinks=False)
                        except OSError:
                            continue
                        if stat.S_ISREG(entry_stat.st_mode) and (
                            entry.name in MANIFEST_NAMES
                            or entry.name.endswith(".sln")
                            or entry.name.endswith(".csproj")
                        ):
                            return True
            except OSError:
                return False
            return False
        finally:
            os.close(directory_fd)

    def _upsert_project(
        self, workspace_id: str, scan_run_id: str, workspace_root: Path, plan: ProjectPlan
    ) -> ProjectRecord:
        now = _now()
        with self._database.write_transaction() as connection:
            row = connection.execute(
                "SELECT project_id, display_name FROM projects WHERE identity_key = ?",
                (plan.identity_key,),
            ).fetchone()
            if row is None:
                project_id = _new_id()
                connection.execute(
                    """
                    INSERT INTO projects(
                        project_id, identity_kind, identity_key, display_name, first_seen_at
                    )
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (project_id, plan.identity_kind, plan.identity_key, plan.display_name, now),
                )
            else:
                project_id = str(row["project_id"])
            relative_location = _relative_path(plan.worktrees[0].root, workspace_root)
            connection.execute(
                """
                INSERT INTO workspace_projects(
                    workspace_id, project_id, relative_location, first_seen_run_id
                )
                VALUES (?, ?, ?, ?)
                ON CONFLICT(workspace_id, project_id) DO NOTHING
                """,
                (workspace_id, project_id, relative_location, scan_run_id),
            )
        return ProjectRecord(project_id, plan.display_name)

    def _read_project(
        self,
        *,
        plan: ProjectPlan,
        config_revision: str,
        change_detection_mode: Literal["fast", "verify_content"] | None,
        nested_project_roots: set[Path],
    ) -> ProjectData:
        worktrees: list[WorktreeData] = []
        for worktree in plan.worktrees:
            prior = self._prior_observations(plan.identity_key, worktree.root)
            files, issues, excluded = self._read_worktree(
                worktree=worktree,
                config_revision=config_revision,
                change_detection_mode=change_detection_mode,
                prior=prior,
                nested_project_roots=nested_project_roots,
            )
            worktrees.append(WorktreeData(worktree, tuple(files), tuple(issues), excluded))
        return ProjectData(plan, tuple(worktrees))

    def _prior_observations(
        self, identity_key: str, worktree_root: Path
    ) -> dict[str, PriorObservation]:
        with self._database.read_connection() as connection:
            project = connection.execute(
                "SELECT project_id FROM projects WHERE identity_key = ?", (identity_key,)
            ).fetchone()
            if project is None:
                return {}
            worktree = connection.execute(
                """
                SELECT worktree_id FROM worktrees
                WHERE project_id = ? AND canonical_root = ?
                """,
                (str(project["project_id"]), str(worktree_root)),
            ).fetchone()
            if worktree is None:
                return {}
            rows = connection.execute(
                """
                SELECT a.relative_path, ao.artifact_id, ao.source_revision_id, ao.byte_size,
                       ao.mtime_ns, source.adapter_id, source.adapter_version,
                       source.config_revision, source.analysis_diagnostics,
                       (
                           SELECT evidence.commit_state
                           FROM project_snapshot_evidence AS pse
                           JOIN evidence ON evidence.evidence_id = pse.evidence_id
                           WHERE pse.project_snapshot_id = ao.project_snapshot_id
                             AND evidence.source_revision_id = ao.source_revision_id
                           LIMIT 1
                       ) AS commit_state
                FROM artifact_observations AS ao
                JOIN source_artifacts AS a ON a.artifact_id = ao.artifact_id
                JOIN source_revisions AS source
                  ON source.source_revision_id = ao.source_revision_id
                JOIN project_snapshots AS ps ON ps.project_snapshot_id = ao.project_snapshot_id
                JOIN scan_runs AS sr ON sr.scan_run_id = ps.scan_run_id
                WHERE a.worktree_id = ? AND sr.status IN ('completed', 'partial')
                ORDER BY ps.created_at DESC
                """,
                (str(worktree["worktree_id"]),),
            ).fetchall()
        observations: dict[str, PriorObservation] = {}
        for row in rows:
            relative = str(row["relative_path"])
            observations.setdefault(
                relative,
                PriorObservation(
                    artifact_id=str(row["artifact_id"]),
                    source_revision_id=str(row["source_revision_id"]),
                    byte_size=int(row["byte_size"]),
                    mtime_ns=int(row["mtime_ns"]),
                    adapter_id=str(row["adapter_id"]),
                    adapter_version=str(row["adapter_version"]),
                    config_revision=str(row["config_revision"]),
                    commit_state=str(row["commit_state"]),
                    analysis_diagnostics=_analysis_diagnostics(str(row["analysis_diagnostics"])),
                ),
            )
        return observations

    def _read_worktree(
        self,
        *,
        worktree: WorktreePlan,
        config_revision: str,
        change_detection_mode: Literal["fast", "verify_content"] | None,
        prior: dict[str, PriorObservation],
        nested_project_roots: set[Path],
    ) -> tuple[list[FileObservation], list[ScanIssueDraft], Counter[str]]:
        issues: list[ScanIssueDraft] = list(
            worktree.git_state.history_issues if worktree.git_state else ()
        )
        excluded: Counter[str] = Counter()
        files: list[FileObservation] = []
        statuses = worktree.git_state.path_states if worktree.git_state else {}
        candidate_paths = self._iter_project_files(
            project_root=worktree.root,
            nested_project_roots=nested_project_roots,
            issues=issues,
            excluded=excluded,
        )
        matcher, matcher_issues = IgnoreMatcher.load(
            worktree.root,
            [
                _relative_path(path, worktree.root)
                for path in candidate_paths
                if path.name == ".gitignore"
            ],
        )
        issues.extend(matcher_issues)
        for path in candidate_paths:
            relative = _relative_path(path, worktree.root)
            if self._is_sensitive(path.name):
                excluded["sensitive"] += 1
                continue
            if matcher.matches(relative):
                excluded["gitignore"] += 1
                continue
            try:
                file_fd, file_stat = _open_regular_file(worktree.root, relative)
            except OSError:
                issues.append(
                    _issue(
                        "file_unreadable",
                        "warning",
                        "A file could not be statted during indexing.",
                        "Fix local permissions and run refresh.",
                        relative,
                    )
                )
                continue
            try:
                if file_stat.st_size > MAX_FILE_BYTES:
                    excluded["oversized"] += 1
                    issues.append(
                        _issue(
                            "file_too_large",
                            "warning",
                            "A file exceeded the configured local indexing limit.",
                            "Add a narrow safe exception only if this file is source material.",
                            relative,
                        )
                    )
                    continue
                if relative in statuses:
                    commit_state = statuses[relative]
                elif worktree.git_state is None or worktree.git_state.external_metadata_only:
                    commit_state = "not_applicable"
                else:
                    commit_state = "committed"
                artifact_kind, adapter_id, evidence_kind = self._classify(path, relative)
                current_adapter_version = f"{ANALYZER_VERSION}:{adapter_version(adapter_id)}"
                previous = prior.get(relative)
                should_reuse = (
                    change_detection_mode == "fast"
                    and previous is not None
                    and previous.byte_size == file_stat.st_size
                    and previous.mtime_ns == file_stat.st_mtime_ns
                    and previous.adapter_id == adapter_id
                    and previous.adapter_version == current_adapter_version
                    and previous.config_revision == config_revision
                    and previous.commit_state == commit_state
                    and commit_state not in {"modified", "untracked"}
                )
                if should_reuse:
                    assert previous is not None
                    for diagnostic in previous.analysis_diagnostics:
                        issues.append(
                            _issue(
                                diagnostic.kind,
                                "warning",
                                diagnostic.message,
                                diagnostic.remediation,
                                relative,
                            )
                        )
                    files.append(
                        FileObservation(
                            relative_path=relative,
                            artifact_kind=artifact_kind,
                            adapter_id=adapter_id,
                            adapter_version=current_adapter_version,
                            evidence_kind=evidence_kind,
                            commit_state=commit_state,
                            byte_size=file_stat.st_size,
                            mtime_ns=file_stat.st_mtime_ns,
                            content_sha256=None,
                            reused_source_revision_id=previous.source_revision_id,
                            analysis_facts=None,
                            analysis_diagnostics=previous.analysis_diagnostics,
                        )
                    )
                    continue
                content = _read_open_file(file_fd)
                decoded_content = content.decode("utf-8")
            except UnicodeDecodeError:
                excluded["binary_or_undecodable"] += 1
                issues.append(
                    _issue(
                        "file_not_utf8",
                        "warning",
                        "A non-text file was not retained as source evidence.",
                        "Keep binary artifacts outside the source index.",
                        relative,
                    )
                )
                continue
            except OSError:
                issues.append(
                    _issue(
                        "file_unreadable",
                        "warning",
                        "A file could not be read during indexing.",
                        "Fix local permissions and run refresh.",
                        relative,
                    )
                )
                continue
            finally:
                os.close(file_fd)
            content_sha256 = hashlib.sha256(content).hexdigest()
            cache_key = (
                content_sha256,
                adapter_id,
                current_adapter_version,
                config_revision,
                artifact_kind,
                Path(relative).name,
            )
            analysis_result = self._analysis_cache.get(cache_key)
            if analysis_result is None:
                analysis_result = analyze_file(
                    relative_path=relative,
                    text=decoded_content,
                    artifact_kind=artifact_kind,
                    adapter_id=adapter_id,
                    base_evidence_kind=evidence_kind,
                )
                self._analysis_cache[cache_key] = analysis_result
            for diagnostic in analysis_result.diagnostics:
                issues.append(
                    _issue(
                        diagnostic.kind,
                        "warning",
                        diagnostic.message,
                        diagnostic.remediation,
                        relative,
                    )
                )
            files.append(
                FileObservation(
                    relative_path=relative,
                    artifact_kind=artifact_kind,
                    adapter_id=adapter_id,
                    adapter_version=current_adapter_version,
                    evidence_kind=evidence_kind,
                    commit_state=commit_state,
                    byte_size=file_stat.st_size,
                    mtime_ns=file_stat.st_mtime_ns,
                    content_sha256=content_sha256,
                    reused_source_revision_id=None,
                    analysis_facts=analysis_result.facts,
                    analysis_diagnostics=analysis_result.diagnostics,
                )
            )
        return files, issues, excluded

    def _iter_project_files(
        self,
        *,
        project_root: Path,
        nested_project_roots: set[Path],
        issues: list[ScanIssueDraft],
        excluded: Counter[str],
    ) -> list[Path]:
        files: list[Path] = []
        stack = ["."]
        while stack:
            relative_directory = stack.pop()
            directory = (
                project_root if relative_directory == "." else project_root / relative_directory
            )
            try:
                directory_fd = _open_directory(project_root, relative_directory)
            except OSError:
                issues.append(
                    _issue(
                        "directory_unreadable",
                        "warning",
                        "A project directory could not be listed.",
                        "Fix local permissions and run refresh.",
                        _relative_path(directory, project_root),
                    )
                )
                continue
            try:
                try:
                    with os.scandir(os.dup(directory_fd)) as entries:
                        children = sorted(entries, key=lambda entry: entry.name)
                except OSError:
                    issues.append(
                        _issue(
                            "directory_unreadable",
                            "warning",
                            "A project directory could not be listed.",
                            "Fix local permissions and run refresh.",
                            _relative_path(directory, project_root),
                        )
                    )
                    continue
                for entry in reversed(children):
                    relative = _child_relative(relative_directory, entry.name)
                    path = project_root / relative
                    try:
                        entry_stat = entry.stat(follow_symlinks=False)
                    except OSError:
                        issues.append(
                            _issue(
                                "path_unreadable",
                                "warning",
                                "A filesystem entry could not be inspected.",
                                "Fix local permissions and run refresh.",
                                relative,
                            )
                        )
                        continue
                    if stat.S_ISLNK(entry_stat.st_mode):
                        excluded["symlink"] += 1
                        continue
                    if stat.S_ISDIR(entry_stat.st_mode):
                        if path in nested_project_roots:
                            excluded["nested_project"] += 1
                        elif entry.name.lower() in HARD_EXCLUDED_DIRECTORIES:
                            excluded["hard_excluded"] += 1
                        else:
                            stack.append(relative)
                    elif stat.S_ISREG(entry_stat.st_mode):
                        files.append(path)
            finally:
                os.close(directory_fd)
        files.sort(key=lambda path: str(path))
        return files

    @staticmethod
    def _is_sensitive(filename: str) -> bool:
        lower = filename.lower()
        return (
            lower == ".env"
            or lower.startswith(".env.")
            or lower
            in {
                ".envrc",
                ".netrc",
                ".npmrc",
                ".pypirc",
                "auth.json",
                "credentials",
                "credentials.json",
                "credentials.yaml",
                "credentials.yml",
                "id_dsa",
                "id_ecdsa",
                "id_ed25519",
                "id_rsa",
                "secret.json",
                "secret.yaml",
                "secret.yml",
                "secrets.json",
                "secrets.yaml",
                "secrets.yml",
                "tokens.json",
            }
            or lower.endswith((".key", ".p12", ".pem", ".pfx"))
        )

    @staticmethod
    def _classify(path: Path, relative: str) -> tuple[str, str, str]:
        lower_name = path.name.lower()
        suffix = path.suffix.lower()
        manifest_adapters = {
            "cargo.toml": "rust",
            "package.json": "typescript",
            "pubspec.yaml": "dart",
            "pyproject.toml": "python",
        }
        if lower_name.startswith("requirements") and lower_name.endswith(".txt"):
            return "manifest", "python", "manifest"
        if path.name in MANIFEST_NAMES or lower_name.endswith((".sln", ".csproj")):
            return "manifest", manifest_adapters.get(lower_name, "generic"), "manifest"
        if suffix in SOURCE_EXTENSIONS:
            adapter = SOURCE_EXTENSIONS[suffix]
            path_parts = {part.lower() for part in Path(relative).parts[:-1]}
            if adapter == "sql" and path_parts.intersection(
                {
                    "design",
                    "designs",
                    "doc",
                    "docs",
                    "plan",
                    "plans",
                    "proposal",
                    "proposals",
                }
            ):
                return "documentation", "generic", "documentation"
            sql_name_tokens = set(re.split(r"[._-]+", lower_name))
            if adapter == "sql" and sql_name_tokens.intersection(
                {"design", "draft", "plan", "planned", "proposal", "proposed"}
            ):
                return "documentation", "generic", "documentation"
            if (
                path_parts.intersection({"test", "tests", "__tests__"})
                or lower_name.startswith("test_")
                or ".test." in lower_name
                or ".spec." in lower_name
            ):
                return "test", adapter, "test_definition"
            if adapter == "sql":
                if path_parts.intersection({"migration", "migrations"}):
                    return "source", adapter, "migration_definition"
                if "schema" in path_parts or lower_name.startswith("schema."):
                    return "source", adapter, "schema_definition"
                return "source", adapter, "query_definition"
            return "source", adapter, "implementation"
        if suffix in {".md", ".rst", ".txt"} or lower_name.startswith("readme"):
            return "documentation", "generic", "documentation"
        if suffix in CONFIG_EXTENSIONS or path.name.startswith("."):
            return "configuration", "generic", "configuration"
        return "unknown", "generic", "structure"

    def _persist_project(
        self,
        *,
        scan_run_id: str,
        project: ProjectRecord,
        data: ProjectData,
        config_revision: str,
        workspace_root: Path,
    ) -> tuple[tuple[ScanIssueDraft, ...], Counter[str]]:
        issues = tuple(issue for worktree in data.worktrees for issue in worktree.issues)
        excluded: Counter[str] = Counter()
        for worktree in data.worktrees:
            excluded.update(worktree.excluded)
        now = _now()
        with self._database.write_transaction() as connection:
            worktree_ids = {
                worktree.plan.root: self._upsert_worktree(
                    connection, project.project_id, worktree.plan
                )
                for worktree in data.worktrees
            }
            for worktree in data.worktrees:
                state = worktree.plan.git_state
                connection.execute(
                    """
                    INSERT INTO worktree_observations(
                        worktree_id, scan_run_id, branch, head_commit, dirty_state, history_basis,
                        external_git_dir, external_common_dir, external_metadata_receipt_id,
                        external_metadata_confirmed_at, external_metadata_read_fields, observed_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        worktree_ids[worktree.plan.root],
                        scan_run_id,
                        state.branch if state else None,
                        state.head_commit if state else None,
                        state.dirty_state if state else "not_applicable",
                        state.history_basis if state else "not_applicable",
                        str(state.external_git_grant.git_dir)
                        if state and state.external_git_grant
                        else None,
                        str(state.external_git_grant.common_dir)
                        if state and state.external_git_grant
                        else None,
                        state.external_git_grant.authorization_receipt_id
                        if state and state.external_git_grant
                        else None,
                        state.external_git_grant.confirmed_at
                        if state and state.external_git_grant
                        else None,
                        json.dumps(state.external_metadata_read_fields, separators=(",", ":"))
                        if state and state.external_git_grant
                        else None,
                        now,
                    ),
                )
            coverage_status = (
                "partial" if any(issue.severity != "info" for issue in issues) else "complete"
            )
            snapshot_id = _new_id()
            connection.execute(
                """
                INSERT INTO project_snapshots(
                    project_snapshot_id, project_id, scan_run_id, created_at, coverage_status
                )
                VALUES (?, ?, ?, ?, ?)
                """,
                (snapshot_id, project.project_id, scan_run_id, now, coverage_status),
            )
            connection.execute(
                """
                INSERT INTO scan_run_projects(
                    scan_run_id, project_id, snapshot_disposition, project_snapshot_id
                )
                VALUES (?, ?, 'fresh', ?)
                """,
                (scan_run_id, project.project_id, snapshot_id),
            )
            module_ids = self._upsert_modules(connection, project.project_id, data)
            current_revisions: dict[str, str] = {}
            current_history_evidence: dict[tuple[str, str], str] = {}
            for worktree in data.worktrees:
                state = worktree.plan.git_state
                if state is None:
                    continue
                worktree_root = _relative_path(worktree.plan.root, workspace_root)
                for entry in state.history_entries:
                    evidence_id = self._history_evidence(
                        connection,
                        project_id=project.project_id,
                        project_snapshot_id=snapshot_id,
                        worktree_root=worktree_root,
                        history_basis=state.history_basis,
                        entry=entry,
                        now=now,
                    )
                    connection.execute(
                        """
                        INSERT INTO project_snapshot_evidence(project_snapshot_id, evidence_id)
                        VALUES (?, ?)
                        ON CONFLICT(project_snapshot_id, evidence_id) DO NOTHING
                        """,
                        (snapshot_id, evidence_id),
                    )
                    current_history_evidence[(worktree_root, entry.commit)] = evidence_id
            for worktree in data.worktrees:
                worktree_id = worktree_ids[worktree.plan.root]
                current_paths = {file.relative_path for file in worktree.files}
                for file in worktree.files:
                    artifact_id = self._upsert_artifact(
                        connection,
                        project.project_id,
                        worktree_id,
                        file.relative_path,
                        file.artifact_kind,
                        file.content_sha256,
                        current_paths,
                    )
                    revision_id = self._source_revision(
                        connection,
                        artifact_id,
                        file,
                        config_revision,
                        now,
                    )
                    connection.execute(
                        """
                        INSERT INTO project_snapshot_source_revisions(
                            project_snapshot_id, source_revision_id
                        )
                        VALUES (?, ?)
                        ON CONFLICT(project_snapshot_id, source_revision_id) DO NOTHING
                        """,
                        (snapshot_id, revision_id),
                    )
                    connection.execute(
                        """
                        INSERT INTO artifact_observations(
                            project_snapshot_id, artifact_id, source_revision_id,
                            byte_size, mtime_ns
                        ) VALUES (?, ?, ?, ?, ?)
                        """,
                        (snapshot_id, artifact_id, revision_id, file.byte_size, file.mtime_ns),
                    )
                    module_id = self._module_for_file(module_ids, file.relative_path)
                    evidence_ids = self._evidence_entries(
                        connection,
                        project_id=project.project_id,
                        project_snapshot_id=snapshot_id,
                        module_id=module_id,
                        source_revision_id=revision_id,
                        file=file,
                        now=now,
                    )
                    for evidence_id in evidence_ids:
                        connection.execute(
                            """
                            INSERT INTO project_snapshot_evidence(project_snapshot_id, evidence_id)
                            VALUES (?, ?)
                            ON CONFLICT(project_snapshot_id, evidence_id) DO NOTHING
                            """,
                            (snapshot_id, evidence_id),
                        )
                    current_revisions[artifact_id] = revision_id
            for module_key, module_id in module_ids.items():
                adapter_id = self._module_adapter(data, module_key)
                connection.execute(
                    """
                    INSERT INTO module_observations(
                        module_id, project_snapshot_id, relative_root,
                        manifest_evidence_id, adapter_id
                    ) VALUES (?, ?, ?, NULL, ?)
                    """,
                    (module_id, snapshot_id, module_key, adapter_id),
                )
            self._insert_issues(connection, scan_run_id, project.project_id, issues)
            self._resolve_evidence_validity(
                connection,
                scan_run_id=scan_run_id,
                project_id=project.project_id,
                project_snapshot_id=snapshot_id,
                current_revisions=current_revisions,
                now=now,
            )
            self._resolve_history_evidence_validity(
                connection,
                scan_run_id=scan_run_id,
                project_id=project.project_id,
                current_history_evidence=current_history_evidence,
                now=now,
            )
        return issues, excluded

    def _upsert_worktree(
        self, connection: sqlite3.Connection, project_id: str, plan: WorktreePlan
    ) -> str:
        row = connection.execute(
            """
            SELECT worktree_id FROM worktrees
            WHERE project_id = ? AND canonical_root = ?
            """,
            (project_id, str(plan.root)),
        ).fetchone()
        if row is not None:
            return str(row["worktree_id"])
        worktree_id = _new_id()
        connection.execute(
            """
            INSERT INTO worktrees(worktree_id, project_id, canonical_root, git_dir)
            VALUES (?, ?, ?, ?)
            """,
            (
                worktree_id,
                project_id,
                str(plan.root),
                str(plan.git_state.git_dir) if plan.git_state else None,
            ),
        )
        return worktree_id

    def _upsert_modules(
        self, connection: sqlite3.Connection, project_id: str, data: ProjectData
    ) -> dict[str, str]:
        module_specs: dict[str, tuple[str, str]] = {".": (data.plan.display_name, "project_root")}
        for worktree in data.worktrees:
            for file in worktree.files:
                if file.artifact_kind != "manifest":
                    continue
                parent = str(Path(file.relative_path).parent)
                module_key = "." if parent == "." else parent.replace("\\", "/")
                module_specs.setdefault((module_key), (Path(module_key).name, "manifest_module"))
        modules: dict[str, str] = {}
        for key, (name, kind) in module_specs.items():
            row = connection.execute(
                "SELECT module_id FROM modules WHERE project_id = ? AND module_key = ?",
                (project_id, key),
            ).fetchone()
            if row is None:
                module_id = _new_id()
                connection.execute(
                    """
                    INSERT INTO modules(module_id, project_id, module_key, name, kind)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (module_id, project_id, key, name, kind),
                )
            else:
                module_id = str(row["module_id"])
            modules[key] = module_id
        return modules

    @staticmethod
    def _module_for_file(module_ids: dict[str, str], relative_path: str) -> str:
        parent = Path(relative_path).parent.as_posix()
        candidates = [
            key for key in module_ids if key == "." or parent == key or parent.startswith(f"{key}/")
        ]
        return module_ids[max(candidates, key=len)]

    @staticmethod
    def _module_adapter(data: ProjectData, module_key: str) -> str:
        prefix = "" if module_key == "." else f"{module_key}/"
        files = [
            file
            for worktree in data.worktrees
            for file in worktree.files
            if file.relative_path.startswith(prefix)
        ]
        root_manifest_adapters = {
            file.adapter_id
            for file in files
            if file.artifact_kind == "manifest"
            and Path(file.relative_path).parent.as_posix() == module_key
            and file.adapter_id != "generic"
        }
        if root_manifest_adapters:
            return sorted(root_manifest_adapters)[0]
        adapter_counts = Counter(file.adapter_id for file in files if file.adapter_id != "generic")
        if not adapter_counts:
            return "generic"
        return min(adapter_counts, key=lambda adapter: (-adapter_counts[adapter], adapter))

    @staticmethod
    def _upsert_artifact(
        connection: sqlite3.Connection,
        project_id: str,
        worktree_id: str,
        relative_path: str,
        artifact_kind: str,
        content_sha256: str | None,
        current_paths: set[str],
    ) -> str:
        row = connection.execute(
            """
            SELECT artifact_id FROM source_artifacts
            WHERE project_id = ? AND worktree_id = ? AND relative_path = ?
            """,
            (project_id, worktree_id, relative_path),
        ).fetchone()
        if row is not None:
            return str(row["artifact_id"])
        supersedes_artifact_id: str | None = None
        if content_sha256 is not None:
            candidates = connection.execute(
                """
                SELECT a.artifact_id, a.relative_path, MAX(ps.created_at) AS last_seen_at
                FROM source_artifacts AS a
                JOIN source_revisions AS sr ON sr.artifact_id = a.artifact_id
                JOIN artifact_observations AS ao
                  ON ao.source_revision_id = sr.source_revision_id
                JOIN project_snapshots AS ps
                  ON ps.project_snapshot_id = ao.project_snapshot_id
                WHERE a.project_id = ? AND a.worktree_id = ?
                  AND a.relative_path <> ? AND sr.content_sha256 = ?
                GROUP BY a.artifact_id, a.relative_path
                ORDER BY last_seen_at DESC, a.artifact_id
                """,
                (project_id, worktree_id, relative_path, content_sha256),
            ).fetchall()
            missing_candidates = [
                candidate
                for candidate in candidates
                if str(candidate["relative_path"]) not in current_paths
            ]
            if missing_candidates and (
                len(missing_candidates) == 1
                or missing_candidates[0]["last_seen_at"] != missing_candidates[1]["last_seen_at"]
            ):
                supersedes_artifact_id = str(missing_candidates[0]["artifact_id"])
        artifact_id = _new_id()
        connection.execute(
            """
            INSERT INTO source_artifacts(
                artifact_id, project_id, worktree_id, relative_path, artifact_kind,
                supersedes_artifact_id
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                artifact_id,
                project_id,
                worktree_id,
                relative_path,
                artifact_kind,
                supersedes_artifact_id,
            ),
        )
        return artifact_id

    @staticmethod
    def _source_revision(
        connection: sqlite3.Connection,
        artifact_id: str,
        file: FileObservation,
        config_revision: str,
        now: str,
    ) -> str:
        if file.reused_source_revision_id is not None:
            return file.reused_source_revision_id
        assert file.content_sha256 is not None
        fingerprint = hashlib.sha256(
            "\0".join(
                (file.content_sha256, file.adapter_id, file.adapter_version, config_revision)
            ).encode("utf-8")
        ).hexdigest()
        row = connection.execute(
            """
            SELECT source_revision_id FROM source_revisions
            WHERE artifact_id = ? AND analysis_fingerprint = ?
            """,
            (artifact_id, fingerprint),
        ).fetchone()
        if row is not None:
            return str(row["source_revision_id"])
        revision_id = _new_id()
        connection.execute(
            """
            INSERT INTO source_revisions(
                source_revision_id, artifact_id, content_sha256, byte_size,
                analysis_fingerprint, adapter_id, adapter_version, config_revision,
                analysis_diagnostics, observed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                revision_id,
                artifact_id,
                file.content_sha256,
                file.byte_size,
                fingerprint,
                file.adapter_id,
                file.adapter_version,
                config_revision,
                _diagnostics_json(file.analysis_diagnostics),
                now,
            ),
        )
        return revision_id

    @staticmethod
    def _evidence_entries(
        connection: sqlite3.Connection,
        *,
        project_id: str,
        project_snapshot_id: str,
        module_id: str,
        source_revision_id: str,
        file: FileObservation,
        now: str,
    ) -> tuple[str, ...]:
        if file.analysis_facts is None:
            rows = connection.execute(
                """
                SELECT evidence_id FROM evidence
                WHERE source_revision_id = ? AND commit_state = ?
                ORDER BY evidence_kind, locator
                """,
                (source_revision_id, file.commit_state),
            ).fetchall()
            if rows:
                return tuple(str(row["evidence_id"]) for row in rows)
            if file.analysis_diagnostics:
                return ()
            raise ValueError("reused source revision has no reusable evidence")
        revision = connection.execute(
            "SELECT analysis_fingerprint FROM source_revisions WHERE source_revision_id = ?",
            (source_revision_id,),
        ).fetchone()
        assert revision is not None
        evidence_ids: list[str] = []
        for fact in file.analysis_facts:
            locator_object = fact.locator(file.relative_path)
            locator = json.dumps(locator_object, separators=(",", ":"), sort_keys=True)
            row = connection.execute(
                """
                SELECT evidence_id FROM evidence
                WHERE source_revision_id = ? AND evidence_kind = ?
                  AND locator = ? AND commit_state = ?
                """,
                (source_revision_id, fact.evidence_kind, locator, file.commit_state),
            ).fetchone()
            if row is not None:
                evidence_ids.append(str(row["evidence_id"]))
                continue
            semantic_locator = {
                key: value for key, value in locator_object.items() if key != "relative_path"
            }
            equivalence = hashlib.sha256(
                "\0".join(
                    (
                        str(revision["analysis_fingerprint"]),
                        fact.evidence_kind,
                        json.dumps(semantic_locator, separators=(",", ":"), sort_keys=True),
                    )
                ).encode("utf-8")
            ).hexdigest()
            evidence_id = _new_id()
            connection.execute(
                """
                INSERT INTO evidence(
                    evidence_id, project_id, acquisition_scope, project_snapshot_id, module_id,
                    source_revision_id, content_equivalence_key, origin_kind, evidence_kind,
                    locator, summary, commit_state, created_at
                ) VALUES (?, ?, 'scan', ?, ?, ?, ?, 'source_revision', ?, ?, ?, ?, ?)
                """,
                (
                    evidence_id,
                    project_id,
                    project_snapshot_id,
                    module_id,
                    source_revision_id,
                    equivalence,
                    fact.evidence_kind,
                    locator,
                    _short(fact.summary),
                    file.commit_state,
                    now,
                ),
            )
            evidence_ids.append(evidence_id)
        return tuple(evidence_ids)

    @staticmethod
    def _history_evidence(
        connection: sqlite3.Connection,
        *,
        project_id: str,
        project_snapshot_id: str,
        worktree_root: str,
        history_basis: str,
        entry: GitHistoryEntry,
        now: str,
    ) -> str:
        locator = json.dumps(
            {
                "author_email": entry.author_email,
                "author_name": entry.author_name,
                "changed_paths": list(entry.changed_paths),
                "commit": entry.commit,
                "committed_at": entry.committed_at,
                "history_basis": history_basis,
                "paths_truncated": entry.paths_truncated,
                "subject": entry.subject,
                "worktree_root": worktree_root,
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        row = connection.execute(
            """
            SELECT evidence_id FROM evidence
            WHERE project_snapshot_id = ? AND origin_kind = 'git_commit'
              AND evidence_kind = 'git_history' AND locator = ? AND commit_state = 'historical'
            """,
            (project_snapshot_id, locator),
        ).fetchone()
        if row is not None:
            return str(row["evidence_id"])
        evidence_id = _new_id()
        connection.execute(
            """
            INSERT INTO evidence(
                evidence_id, project_id, acquisition_scope, project_snapshot_id, module_id,
                source_revision_id, content_equivalence_key, origin_kind, evidence_kind, locator,
                summary, commit_state, created_at
            ) VALUES (?, ?, 'scan', ?, NULL, NULL, NULL, 'git_commit', 'git_history', ?, ?,
                      'historical', ?)
            """,
            (
                evidence_id,
                project_id,
                project_snapshot_id,
                locator,
                _short(
                    "Indexed bounded local Git commit metadata and changed-path range "
                    f"for {entry.commit[:12]}."
                ),
                now,
            ),
        )
        return evidence_id

    def _resolve_evidence_validity(
        self,
        connection: sqlite3.Connection,
        *,
        scan_run_id: str,
        project_id: str,
        project_snapshot_id: str,
        current_revisions: dict[str, str],
        now: str,
    ) -> None:
        current_rows = connection.execute(
            """
            SELECT e.evidence_id, e.evidence_kind, e.locator, sr.artifact_id
            FROM project_snapshot_evidence AS pse
            JOIN evidence AS e ON e.evidence_id = pse.evidence_id
            JOIN source_revisions AS sr ON sr.source_revision_id = e.source_revision_id
            WHERE pse.project_snapshot_id = ? AND e.origin_kind = 'source_revision'
            """,
            (project_snapshot_id,),
        ).fetchall()
        current_ids = {str(row["evidence_id"]) for row in current_rows}
        current_by_key = {
            (
                str(row["artifact_id"]),
                str(row["evidence_kind"]),
                str(row["locator"]),
            ): str(row["evidence_id"])
            for row in current_rows
        }
        rows = connection.execute(
            """
            SELECT e.evidence_id, e.evidence_kind, e.locator, sr.artifact_id
            FROM evidence AS e
            JOIN source_revisions AS sr ON sr.source_revision_id = e.source_revision_id
            WHERE e.project_id = ? AND e.acquisition_scope = 'scan'
              AND e.origin_kind = 'source_revision'
            """,
            (project_id,),
        ).fetchall()
        for row in rows:
            evidence_id = str(row["evidence_id"])
            artifact_id = str(row["artifact_id"])
            current_revision = current_revisions.get(artifact_id)
            validity = (
                "current"
                if evidence_id in current_ids
                else "missing"
                if current_revision is None
                else "stale"
            )
            replacement = (
                current_by_key.get(
                    (
                        artifact_id,
                        str(row["evidence_kind"]),
                        str(row["locator"]),
                    )
                )
                if validity == "stale"
                else None
            )
            connection.execute(
                """
                INSERT INTO evidence_validities(
                    scan_run_id, evidence_id, validity, replacement_evidence_id, resolved_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (scan_run_id, evidence_id, validity, replacement, now),
            )

    @staticmethod
    def _resolve_history_evidence_validity(
        connection: sqlite3.Connection,
        *,
        scan_run_id: str,
        project_id: str,
        current_history_evidence: dict[tuple[str, str], str],
        now: str,
    ) -> None:
        rows = connection.execute(
            """
            SELECT evidence_id, locator
            FROM evidence
            WHERE project_id = ? AND acquisition_scope = 'scan'
              AND origin_kind = 'git_commit' AND evidence_kind = 'git_history'
            """,
            (project_id,),
        ).fetchall()
        for row in rows:
            evidence_id = str(row["evidence_id"])
            try:
                locator = json.loads(str(row["locator"]))
                if not isinstance(locator, dict):
                    raise ValueError("history locator is not an object")
                worktree_root = locator["worktree_root"]
                commit = locator["commit"]
                if not isinstance(worktree_root, str) or not isinstance(commit, str):
                    raise ValueError("history locator is incomplete")
            except (KeyError, ValueError, json.JSONDecodeError):
                key: tuple[str, str] | None = None
            else:
                key = (worktree_root, commit)
            replacement = current_history_evidence.get(key) if key is not None else None
            validity = "current" if replacement == evidence_id else "stale"
            connection.execute(
                """
                INSERT INTO evidence_validities(
                    scan_run_id, evidence_id, validity, replacement_evidence_id, resolved_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (scan_run_id, evidence_id, validity, replacement, now),
            )

    def _record_project_failure(
        self, scan_run_id: str, project_id: str, issue: ScanIssueDraft
    ) -> str:
        with self._database.write_transaction() as connection:
            baseline = connection.execute(
                """
                SELECT ps.project_snapshot_id
                FROM project_snapshots AS ps
                JOIN scan_runs AS sr ON sr.scan_run_id = ps.scan_run_id
                WHERE ps.project_id = ? AND sr.status IN ('completed', 'partial')
                ORDER BY ps.created_at DESC
                LIMIT 1
                """,
                (project_id,),
            ).fetchone()
            if baseline is None:
                disposition = "failed_no_baseline"
                snapshot_id = None
            else:
                disposition = "carried_forward"
                snapshot_id = str(baseline["project_snapshot_id"])
            connection.execute(
                """
                INSERT INTO scan_run_projects(
                    scan_run_id, project_id, snapshot_disposition, project_snapshot_id
                )
                VALUES (?, ?, ?, ?)
                """,
                (scan_run_id, project_id, disposition, snapshot_id),
            )
            self._insert_issues(connection, scan_run_id, project_id, (issue,))
        return disposition

    def _carry_forward_missing_projects(
        self,
        *,
        scan_run_id: str,
        workspace_id: str,
        discovered_project_ids: set[str],
    ) -> tuple[list[ScanIssueDraft], Counter[str]]:
        with self._database.read_connection() as connection:
            rows = connection.execute(
                """
                SELECT project_id, relative_location
                FROM workspace_projects
                WHERE workspace_id = ?
                """,
                (workspace_id,),
            ).fetchall()
        issues: list[ScanIssueDraft] = []
        dispositions: Counter[str] = Counter()
        for row in rows:
            project_id = str(row["project_id"])
            if project_id in discovered_project_ids:
                continue
            issue = _issue(
                "project_not_discovered",
                "warning",
                "A previously indexed project was not discovered in this workspace run.",
                (
                    "Restore the project directory or explicitly remove it in a future maintenance "
                    "workflow."
                ),
                str(row["relative_location"]),
            )
            disposition = self._record_project_failure(scan_run_id, project_id, issue)
            issues.append(issue)
            dispositions[disposition] += 1
        return issues, dispositions

    def _persist_issues(
        self, scan_run_id: str, project_id: str | None, issues: tuple[ScanIssueDraft, ...]
    ) -> None:
        if not issues:
            return
        with self._database.write_transaction() as connection:
            self._insert_issues(connection, scan_run_id, project_id, issues)

    @staticmethod
    def _insert_issues(
        connection: sqlite3.Connection,
        scan_run_id: str,
        project_id: str | None,
        issues: tuple[ScanIssueDraft, ...],
    ) -> None:
        for issue in issues:
            connection.execute(
                """
                INSERT INTO scan_issues(
                    issue_id, scan_run_id, project_id, artifact_id, kind, severity,
                    relative_path, message, remediation
                ) VALUES (?, ?, ?, NULL, ?, ?, ?, ?, ?)
                """,
                (
                    issue.issue_id,
                    scan_run_id,
                    project_id,
                    issue.kind,
                    issue.severity,
                    issue.relative_path,
                    issue.message,
                    issue.remediation,
                ),
            )

    @staticmethod
    def _final_status(
        scan_run_id: str, dispositions: Counter[str], issues: list[ScanIssueDraft]
    ) -> str:
        del scan_run_id
        available = dispositions["fresh"] + dispositions["carried_forward"]
        if available == 0:
            return "failed"
        if dispositions["carried_forward"] or dispositions["failed_no_baseline"]:
            return "partial"
        if any(issue.severity != "info" for issue in issues):
            return "partial"
        return "completed"

    def _finish_run(
        self,
        scan_run_id: str,
        status: str,
        coverage: dict[str, object] | None,
    ) -> None:
        with self._database.write_transaction() as connection:
            updated = connection.execute(
                """
                UPDATE scan_runs
                SET status = ?, finished_at = ?
                WHERE scan_run_id = ? AND status = 'running'
                """,
                (status, _now(), scan_run_id),
            )
            if coverage is not None and updated.rowcount == 1:
                connection.execute(
                    """
                    INSERT INTO scan_run_overviews(scan_run_id, coverage_json, created_at)
                    VALUES (?, ?, ?)
                    """,
                    (
                        scan_run_id,
                        json.dumps(
                            coverage,
                            ensure_ascii=False,
                            separators=(",", ":"),
                            sort_keys=True,
                            allow_nan=False,
                        ),
                        _now(),
                    ),
                )

    def _coverage(
        self,
        scan_run_id: str,
        dispositions: Counter[str],
        excluded: Counter[str],
    ) -> dict[str, object]:
        with self._database.read_connection() as connection:
            worktrees = int(
                connection.execute(
                    "SELECT COUNT(*) AS count FROM worktree_observations WHERE scan_run_id = ?",
                    (scan_run_id,),
                ).fetchone()["count"]
            )
            modules = int(
                connection.execute(
                    """
                    SELECT COUNT(*) AS count
                    FROM module_observations AS mo
                    JOIN project_snapshots AS ps ON ps.project_snapshot_id = mo.project_snapshot_id
                    WHERE ps.scan_run_id = ?
                    """,
                    (scan_run_id,),
                ).fetchone()["count"]
            )
            files = int(
                connection.execute(
                    """
                    SELECT COUNT(*) AS count
                    FROM artifact_observations AS ao
                    JOIN project_snapshots AS ps ON ps.project_snapshot_id = ao.project_snapshot_id
                    WHERE ps.scan_run_id = ?
                    """,
                    (scan_run_id,),
                ).fetchone()["count"]
            )
            history_evidence = int(
                connection.execute(
                    """
                    SELECT COUNT(*) AS count
                    FROM project_snapshot_evidence AS pse
                    JOIN evidence AS e ON e.evidence_id = pse.evidence_id
                    JOIN project_snapshots AS ps ON ps.project_snapshot_id = pse.project_snapshot_id
                    WHERE ps.scan_run_id = ? AND e.origin_kind = 'git_commit'
                      AND e.evidence_kind = 'git_history'
                    """,
                    (scan_run_id,),
                ).fetchone()["count"]
            )
            observations = connection.execute(
                """
                SELECT w.canonical_root, wo.history_basis, wo.external_git_dir,
                       wo.external_common_dir, wo.external_metadata_receipt_id,
                       wo.external_metadata_confirmed_at, wo.external_metadata_read_fields
                FROM worktree_observations AS wo
                JOIN worktrees AS w ON w.worktree_id = wo.worktree_id
                WHERE wo.scan_run_id = ?
                ORDER BY w.canonical_root
                """,
                (scan_run_id,),
            ).fetchall()
            role_lens_context = self._role_lens_context(connection, scan_run_id)
        history_basis_by_worktree = {
            str(row["canonical_root"]): str(row["history_basis"]) for row in observations
        }
        external_git_metadata = {
            str(row["canonical_root"]): {
                "git_dir": str(row["external_git_dir"]),
                "common_dir": str(row["external_common_dir"]),
                "authorization_receipt_id": str(row["external_metadata_receipt_id"]),
                "confirmed_at": str(row["external_metadata_confirmed_at"]),
                "read_fields": json.loads(str(row["external_metadata_read_fields"])),
            }
            for row in observations
            if row["external_git_dir"] is not None
            and row["external_common_dir"] is not None
            and row["external_metadata_receipt_id"] is not None
            and row["external_metadata_confirmed_at"] is not None
            and row["external_metadata_read_fields"] is not None
        }
        return {
            "overview_provenance": "recorded",
            "excluded_by_category_available": True,
            "projects": sum(dispositions.values()),
            "fresh_projects": dispositions["fresh"],
            "carried_forward_projects": dispositions["carried_forward"],
            "failed_no_baseline_projects": dispositions["failed_no_baseline"],
            "excluded_projects": dispositions["excluded"],
            "worktrees": worktrees,
            "modules": modules,
            "indexed_files": files,
            "git_history_evidence": history_evidence,
            "history_basis_by_worktree": history_basis_by_worktree,
            "external_git_metadata": external_git_metadata,
            "excluded_by_category": dict(sorted(excluded.items())),
            "role_lens_context": role_lens_context,
        }

    @staticmethod
    def _role_lens_context(connection: sqlite3.Connection, scan_run_id: str) -> dict[str, object]:
        total_projects = int(
            connection.execute(
                "SELECT COUNT(*) AS count FROM scan_run_projects WHERE scan_run_id = ?",
                (scan_run_id,),
            ).fetchone()["count"]
        )
        project_rows = connection.execute(
            """
            SELECT srp.project_id, p.display_name, srp.snapshot_disposition,
                   srp.project_snapshot_id, ps.coverage_status
            FROM scan_run_projects AS srp
            JOIN projects AS p ON p.project_id = srp.project_id
            LEFT JOIN project_snapshots AS ps
              ON ps.project_snapshot_id = srp.project_snapshot_id
            WHERE srp.scan_run_id = ?
            ORDER BY srp.project_id
            LIMIT ?
            """,
            (scan_run_id, MAX_ROLE_LENS_CONTEXT_PROJECTS),
        ).fetchall()
        available_modules = int(
            connection.execute(
                """
                SELECT COUNT(*) AS count
                FROM scan_run_projects AS srp
                JOIN module_observations AS mo
                  ON mo.project_snapshot_id = srp.project_snapshot_id
                WHERE srp.scan_run_id = ?
                """,
                (scan_run_id,),
            ).fetchone()["count"]
        )
        available_evidence = int(
            connection.execute(
                """
                SELECT COUNT(*) AS count
                FROM scan_run_projects AS srp
                JOIN project_snapshot_evidence AS pse
                  ON pse.project_snapshot_id = srp.project_snapshot_id
                WHERE srp.scan_run_id = ?
                """,
                (scan_run_id,),
            ).fetchone()["count"]
        )
        projects: list[dict[str, object]] = []
        returned_modules = 0
        returned_evidence_samples = 0
        for project in project_rows:
            snapshot_id = project["project_snapshot_id"]
            module_kind_counts: dict[str, int] = {}
            adapter_counts: dict[str, int] = {}
            modules: list[dict[str, object]] = []
            evidence_kind_counts: dict[str, int] = {}
            evidence_samples: list[dict[str, object]] = []
            project_module_count = 0
            project_evidence_count = 0
            if snapshot_id is not None:
                module_count_row = connection.execute(
                    """
                    SELECT COUNT(*) AS count
                    FROM module_observations
                    WHERE project_snapshot_id = ?
                    """,
                    (snapshot_id,),
                ).fetchone()
                assert module_count_row is not None
                project_module_count = int(module_count_row["count"])
                for row in connection.execute(
                    """
                    SELECT m.kind, mo.adapter_id, COUNT(*) AS count
                    FROM module_observations AS mo
                    JOIN modules AS m ON m.module_id = mo.module_id
                    WHERE mo.project_snapshot_id = ?
                    GROUP BY m.kind, mo.adapter_id
                    ORDER BY m.kind, mo.adapter_id
                    """,
                    (snapshot_id,),
                ).fetchall():
                    count = int(row["count"])
                    kind = str(row["kind"])
                    adapter = str(row["adapter_id"])
                    module_kind_counts[kind] = module_kind_counts.get(kind, 0) + count
                    adapter_counts[adapter] = adapter_counts.get(adapter, 0) + count
                module_limit = min(
                    MAX_ROLE_LENS_CONTEXT_MODULES_PER_PROJECT,
                    MAX_ROLE_LENS_CONTEXT_MODULES - returned_modules,
                )
                if module_limit > 0:
                    module_rows = connection.execute(
                        """
                        SELECT m.module_id, m.name, m.kind, mo.relative_root, mo.adapter_id
                        FROM module_observations AS mo
                        JOIN modules AS m ON m.module_id = mo.module_id
                        WHERE mo.project_snapshot_id = ?
                        ORDER BY m.module_id
                        LIMIT ?
                        """,
                        (snapshot_id, module_limit),
                    ).fetchall()
                    modules = [
                        {
                            "module_id": str(row["module_id"]),
                            "name": str(row["name"]),
                            "kind": str(row["kind"]),
                            "relative_root": str(row["relative_root"]),
                            "adapter_id": str(row["adapter_id"]),
                        }
                        for row in module_rows
                    ]
                    returned_modules += len(modules)
                for row in connection.execute(
                    """
                    SELECT e.evidence_kind, COUNT(*) AS count
                    FROM project_snapshot_evidence AS pse
                    JOIN evidence AS e ON e.evidence_id = pse.evidence_id
                    WHERE pse.project_snapshot_id = ?
                    GROUP BY e.evidence_kind
                    ORDER BY e.evidence_kind
                    """,
                    (snapshot_id,),
                ).fetchall():
                    evidence_kind_counts[str(row["evidence_kind"])] = int(row["count"])
                project_evidence_count = sum(evidence_kind_counts.values())
                evidence_limit = min(
                    MAX_ROLE_LENS_CONTEXT_EVIDENCE_PER_PROJECT,
                    MAX_ROLE_LENS_CONTEXT_EVIDENCE_SAMPLES - returned_evidence_samples,
                )
                if evidence_limit > 0:
                    evidence_rows = connection.execute(
                        """
                        WITH representatives AS (
                            SELECT e.evidence_kind, MIN(e.evidence_id) AS evidence_id
                            FROM project_snapshot_evidence AS pse
                            JOIN evidence AS e ON e.evidence_id = pse.evidence_id
                            WHERE pse.project_snapshot_id = ?
                            GROUP BY e.evidence_kind
                        )
                        SELECT e.evidence_id, e.module_id, e.evidence_kind,
                               e.summary, e.commit_state
                        FROM representatives AS representative
                        JOIN evidence AS e ON e.evidence_id = representative.evidence_id
                        ORDER BY e.evidence_kind, e.evidence_id
                        LIMIT ?
                        """,
                        (snapshot_id, evidence_limit),
                    ).fetchall()
                    evidence_samples = [
                        {
                            "evidence_id": str(row["evidence_id"]),
                            "module_id": row["module_id"],
                            "evidence_kind": str(row["evidence_kind"]),
                            "summary": str(row["summary"]),
                            "commit_state": str(row["commit_state"]),
                        }
                        for row in evidence_rows
                    ]
                    returned_evidence_samples += len(evidence_samples)
            projects.append(
                {
                    "project_id": str(project["project_id"]),
                    "display_name": str(project["display_name"]),
                    "snapshot_disposition": str(project["snapshot_disposition"]),
                    "project_snapshot_id": snapshot_id,
                    "coverage_status": project["coverage_status"],
                    "module_count": project_module_count,
                    "module_kind_counts": module_kind_counts,
                    "adapter_counts": adapter_counts,
                    "modules": modules,
                    "modules_truncated": project_module_count > len(modules),
                    "evidence_count": project_evidence_count,
                    "evidence_kind_counts": evidence_kind_counts,
                    "evidence_samples": evidence_samples,
                    "evidence_samples_truncated": project_evidence_count > len(evidence_samples),
                }
            )
        return {
            "contract_version": ROLE_LENS_CONTEXT_CONTRACT_VERSION,
            "untrusted_data": True,
            "scan_run_id": scan_run_id,
            "projects": projects,
            "limits": {
                "maximum_projects": MAX_ROLE_LENS_CONTEXT_PROJECTS,
                "available_projects": total_projects,
                "returned_projects": len(projects),
                "projects_truncated": total_projects > len(projects),
                "maximum_modules": MAX_ROLE_LENS_CONTEXT_MODULES,
                "available_modules": available_modules,
                "returned_modules": returned_modules,
                "modules_truncated": available_modules > returned_modules,
                "maximum_evidence_samples": MAX_ROLE_LENS_CONTEXT_EVIDENCE_SAMPLES,
                "evidence_sample_strategy": "one_per_evidence_kind_per_project",
                "available_evidence": available_evidence,
                "returned_evidence_samples": returned_evidence_samples,
                "evidence_samples_truncated": available_evidence > returned_evidence_samples,
            },
        }

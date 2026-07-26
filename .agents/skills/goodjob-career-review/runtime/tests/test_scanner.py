from __future__ import annotations

import json
import os
import shlex
import shutil
import sqlite3
import subprocess
import sys
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import pytest

from goodjob.auth import AuthorizationRepository, AuthorizationRequest, generate_capability
from goodjob.cli import run
from goodjob.db import Database
from goodjob.paths import DataPaths
from goodjob.scanner import _open_regular_file

RUNTIME_DIR = Path(__file__).resolve().parents[1]
NOTICE_VERSION = "goodjob-source-analysis-v1"


def _scope(workspace: Path) -> dict[str, object]:
    return {
        "workspace_path": str(workspace.resolve()),
        "allowed_categories": ["source_analysis"],
    }


def _send_json(process: subprocess.Popen[str], payload: dict[str, object]) -> dict[str, object]:
    assert process.stdin is not None
    assert process.stdout is not None
    process.stdin.write(json.dumps(payload) + "\n")
    process.stdin.flush()
    response = json.loads(process.stdout.readline())
    assert isinstance(response, dict)
    return response


def _object_field(payload: dict[str, object], field: str) -> dict[str, object]:
    value = payload[field]
    assert isinstance(value, dict)
    return cast(dict[str, object], value)


def _broker(data_dir: Path, *, extra_env: Mapping[str, str] | None = None) -> subprocess.Popen[str]:
    environment = {**os.environ, "PYTHONPATH": str(RUNTIME_DIR / "src")}
    if extra_env is not None:
        environment.update(extra_env)
    return subprocess.Popen(
        [sys.executable, "scripts/session.py", "--data-dir", str(data_dir)],
        cwd=RUNTIME_DIR,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=environment,
    )


def _close_broker(process: subprocess.Popen[str]) -> None:
    assert process.stdin is not None
    process.stdin.close()
    assert process.wait(timeout=5) == 0
    assert process.stderr is not None
    assert process.stderr.read() == ""


def _git_init(path: Path) -> None:
    subprocess.run(["git", "init", str(path)], check=True, capture_output=True, text=True)


def _git(
    path: Path, *arguments: str, extra_env: Mapping[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    if extra_env is not None:
        environment.update(extra_env)
    return subprocess.run(
        ["git", "-C", str(path), *arguments],
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    )


def _git_wrapper(tmp_path: Path) -> tuple[dict[str, str], Path]:
    git_binary = shutil.which("git")
    assert git_binary is not None
    audit = tmp_path / "git-audit.log"
    wrapper = tmp_path / "git"
    wrapper.write_text(
        "#!/bin/sh\n"
        f"printf '%s\\n' \"$*\" >> {shlex.quote(str(audit))}\n"
        f'exec {shlex.quote(git_binary)} "$@"\n',
        encoding="utf-8",
    )
    wrapper.chmod(0o755)
    return (
        {
            "PATH": f"{tmp_path}{os.pathsep}{os.environ.get('PATH', '')}",
        },
        audit,
    )


def test_scan_discovers_isolated_projects_and_keeps_sensitive_bytes_out_of_sqlite(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    repository = workspace / "repository"
    nested = repository / "nested"
    standalone = workspace / "standalone"
    nested.mkdir(parents=True)
    standalone.mkdir()
    _git_init(repository)
    _git_init(nested)
    (repository / ".gitignore").write_text("nested/\n", encoding="utf-8")
    (repository / "app.py").write_text("print('outer')\n", encoding="utf-8")
    (repository / "tests").mkdir()
    (repository / "tests" / "test_app.py").write_text(
        "def test_app() -> None:\n    pass\n", encoding="utf-8"
    )
    (repository / ".env").write_text("TOP_SECRET=should_not_persist\n", encoding="utf-8")
    (repository / ".envrc").write_text("export ENVRC_SECRET=should_not_persist\n", encoding="utf-8")
    (repository / "credentials.json").write_text(
        '{"token":"should_not_persist"}\n', encoding="utf-8"
    )
    (repository / "node_modules").mkdir()
    (repository / "node_modules" / "ignored.js").write_text("ignored\n", encoding="utf-8")
    (repository / "Secrets").mkdir()
    (repository / "Secrets" / "hidden.py").write_text(
        "SECRET_DIRECTORY_BYTES = 'should_not_persist'\n", encoding="utf-8"
    )
    (nested / "main.ts").write_text("export const nested = true\n", encoding="utf-8")
    (nested / ".gitignore").write_text("hidden.ts\n", encoding="utf-8")
    (nested / "hidden.ts").write_text("export const hidden = true\n", encoding="utf-8")
    (standalone / "pyproject.toml").write_text("[project]\nname='standalone'\n", encoding="utf-8")
    (standalone / "tool.py").write_text("def main() -> None:\n    pass\n", encoding="utf-8")
    outside_source = tmp_path / "outside.py"
    outside_source.write_text("print('outside')\n", encoding="utf-8")
    (repository / "outside.py").symlink_to(outside_source)

    data_dir = tmp_path / "data"
    broker = _broker(data_dir)
    authorized = _send_json(
        broker,
        {"op": "authorize_source_analysis", "workspace": str(workspace), "confirmed": True},
    )
    assert authorized["status"] == "ok"
    receipt = _object_field(authorized, "receipt")
    scanned = _send_json(
        broker,
        {
            "op": "scan",
            "workspace": str(workspace),
            "authorization_receipt_id": receipt["authorization_receipt_id"],
        },
    )
    _close_broker(broker)

    assert scanned["status"] == "ok"
    scan_run = _object_field(scanned, "scan_run")
    coverage = _object_field(scanned, "coverage")
    assert scan_run["status"] in {"completed", "partial"}
    assert coverage["fresh_projects"] == 3
    excluded = _object_field(coverage, "excluded_by_category")
    assert excluded["sensitive"] == 3
    hard_excluded = excluded["hard_excluded"]
    assert isinstance(hard_excluded, int)
    assert hard_excluded >= 1
    issues = scanned["issues"]
    assert isinstance(issues, list)
    assert any(
        isinstance(issue, dict) and issue.get("kind") == "symlink_outside_authorized_root"
        for issue in issues
    )

    connection = sqlite3.connect(data_dir / "goodjob.sqlite3")
    project_count = connection.execute("SELECT COUNT(*) FROM projects").fetchone()[0]
    test_evidence_count = connection.execute(
        "SELECT COUNT(*) FROM evidence WHERE evidence_kind = 'test_definition'"
    ).fetchone()[0]
    stored_text = "\n".join(
        value
        for row in connection.execute("SELECT locator, summary FROM evidence")
        for value in row
    )
    stored_paths = {
        str(row[0]) for row in connection.execute("SELECT relative_path FROM source_artifacts")
    }
    connection.close()
    assert project_count == 3
    assert test_evidence_count == 1
    assert "TOP_SECRET" not in stored_text
    assert "should_not_persist" not in stored_text
    assert "hidden.ts" not in stored_text
    assert ".envrc" not in stored_paths
    assert "Secrets/hidden.py" not in stored_paths


def test_refresh_fast_reuses_metadata_but_verify_content_detects_same_stat_change(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    project = workspace / "project"
    project.mkdir(parents=True)
    source = project / "main.py"
    (project / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")
    source.write_text("one\n", encoding="utf-8")
    data_dir = tmp_path / "data"
    broker = _broker(data_dir)
    authorized = _send_json(
        broker,
        {"op": "authorize_source_analysis", "workspace": str(workspace), "confirmed": True},
    )
    receipt = _object_field(authorized, "receipt")
    scanned = _send_json(
        broker,
        {
            "op": "scan",
            "workspace": str(workspace),
            "authorization_receipt_id": receipt["authorization_receipt_id"],
        },
    )
    workspace_id = _object_field(scanned, "scan_run")["workspace_id"]
    assert isinstance(workspace_id, str)

    original_stat = source.stat()
    source.write_text("two\n", encoding="utf-8")
    os.utime(source, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
    fast = _send_json(
        broker,
        {
            "op": "refresh",
            "workspace": str(workspace),
            "workspace_id": workspace_id,
            "authorization_receipt_id": receipt["authorization_receipt_id"],
            "change_detection_mode": "fast",
        },
    )
    verify = _send_json(
        broker,
        {
            "op": "refresh",
            "workspace": str(workspace),
            "workspace_id": workspace_id,
            "authorization_receipt_id": receipt["authorization_receipt_id"],
            "change_detection_mode": "verify_content",
        },
    )
    _close_broker(broker)

    assert fast["status"] == "ok"
    assert verify["status"] == "ok"
    connection = sqlite3.connect(data_dir / "goodjob.sqlite3")
    revision_count = connection.execute(
        """
        SELECT COUNT(*)
        FROM source_revisions AS sr
        JOIN source_artifacts AS sa ON sa.artifact_id = sr.artifact_id
        WHERE sa.relative_path = 'main.py'
        """
    ).fetchone()[0]
    stale_count = connection.execute(
        "SELECT COUNT(*) FROM evidence_validities WHERE validity = 'stale'"
    ).fetchone()[0]
    reused_evidence_count = connection.execute(
        """
        SELECT COUNT(*)
        FROM evidence AS e
        JOIN source_revisions AS sr ON sr.source_revision_id = e.source_revision_id
        JOIN source_artifacts AS sa ON sa.artifact_id = sr.artifact_id
        WHERE sa.relative_path = 'main.py'
        """
    ).fetchone()[0]
    snapshot_references = connection.execute(
        """
        SELECT COUNT(*)
        FROM project_snapshot_source_revisions AS pssr
        JOIN source_revisions AS sr ON sr.source_revision_id = pssr.source_revision_id
        JOIN source_artifacts AS sa ON sa.artifact_id = sr.artifact_id
        WHERE sa.relative_path = 'main.py'
        """
    ).fetchone()[0]
    connection.close()
    assert revision_count == 2
    assert reused_evidence_count == 2
    assert snapshot_references == 3
    assert stale_count >= 1


def test_scan_rejects_a_receipt_scope_for_a_different_workspace_before_creating_a_run(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    authorized_workspace = tmp_path / "authorized"
    other_workspace = tmp_path / "other"
    authorized_workspace.mkdir()
    other_workspace.mkdir()
    (other_workspace / "pyproject.toml").write_text("[project]\nname='other'\n", encoding="utf-8")
    data_paths = DataPaths.from_argument(str(tmp_path / "data"))
    capability = generate_capability()
    request = AuthorizationRequest.from_values(
        receipt_kind="source_analysis",
        scope=_scope(authorized_workspace),
        notice_version=NOTICE_VERSION,
    )
    receipt = AuthorizationRepository(Database(data_paths)).issue(
        capability=capability,
        request=request,
    )
    read_fd, write_fd = os.pipe()
    try:
        os.write(write_fd, capability)
    finally:
        os.close(write_fd)
    try:
        exit_code = run(
            [
                "--data-dir",
                str(data_paths.root),
                "scan",
                "--workspace",
                str(other_workspace),
                "--config-revision",
                "goodjob-scan-config-v1",
                "--authorization-receipt-id",
                receipt.authorization_receipt_id,
                "--receipt-kind",
                "source_analysis",
                "--scope-json",
                json.dumps(_scope(authorized_workspace)),
                "--notice-version",
                NOTICE_VERSION,
                "--capability-fd",
                str(read_fd),
            ]
        )
    finally:
        os.close(read_fd)
    captured = capsys.readouterr()
    assert exit_code == 2
    assert "authorization scope does not match" in captured.err
    with Database(data_paths).read_connection() as connection:
        scan_runs = connection.execute("SELECT COUNT(*) FROM scan_runs").fetchone()[0]
    assert scan_runs == 0


def test_scan_with_an_authorized_missing_root_performs_no_workspace_or_run_write(
    tmp_path: Path,
) -> None:
    missing_workspace = tmp_path / "missing"
    data_dir = tmp_path / "data"
    broker = _broker(data_dir)
    authorized = _send_json(
        broker,
        {
            "op": "authorize_source_analysis",
            "workspace": str(missing_workspace),
            "confirmed": True,
        },
    )
    receipt = _object_field(authorized, "receipt")
    response = _send_json(
        broker,
        {
            "op": "scan",
            "workspace": str(missing_workspace),
            "authorization_receipt_id": receipt["authorization_receipt_id"],
        },
    )
    _close_broker(broker)

    assert response["status"] == "error"
    assert response["code"] == "invalid_input"
    connection = sqlite3.connect(data_dir / "goodjob.sqlite3")
    workspace_count = connection.execute("SELECT COUNT(*) FROM workspaces").fetchone()[0]
    scan_run_count = connection.execute("SELECT COUNT(*) FROM scan_runs").fetchone()[0]
    connection.close()
    assert workspace_count == 0
    assert scan_run_count == 0


def test_descriptor_reader_rejects_a_file_or_directory_symlink(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    (outside / "secret.py").write_text("secret\n", encoding="utf-8")
    (workspace / "file.py").symlink_to(outside / "secret.py")
    (workspace / "directory").symlink_to(outside, target_is_directory=True)

    with pytest.raises(OSError):
        _open_regular_file(workspace, "file.py")
    with pytest.raises(OSError):
        _open_regular_file(workspace, "directory/secret.py")


def test_refresh_marks_confirmed_dead_run_interrupted_and_never_uses_it_as_fast_baseline(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    project = workspace / "project"
    project.mkdir(parents=True)
    source = project / "main.py"
    (project / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")
    source.write_text("one\n", encoding="utf-8")
    data_dir = tmp_path / "data"
    broker = _broker(data_dir)
    authorized = _send_json(
        broker,
        {"op": "authorize_source_analysis", "workspace": str(workspace), "confirmed": True},
    )
    receipt = _object_field(authorized, "receipt")
    initial = _send_json(
        broker,
        {
            "op": "scan",
            "workspace": str(workspace),
            "authorization_receipt_id": receipt["authorization_receipt_id"],
        },
    )
    initial_run = _object_field(initial, "scan_run")["scan_run_id"]
    workspace_id = _object_field(initial, "scan_run")["workspace_id"]
    assert isinstance(initial_run, str)
    assert isinstance(workspace_id, str)
    connection = sqlite3.connect(data_dir / "goodjob.sqlite3")
    connection.execute(
        """
        UPDATE scan_runs
        SET status = 'running', finished_at = NULL,
            owner_process_identity = 'pid:999999;started:Thu Jan  1 00:00:00 1970'
        WHERE scan_run_id = ?
        """,
        (initial_run,),
    )
    connection.commit()
    connection.close()
    original_stat = source.stat()
    source.write_text("two\n", encoding="utf-8")
    os.utime(source, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
    refreshed = _send_json(
        broker,
        {
            "op": "refresh",
            "workspace": str(workspace),
            "workspace_id": workspace_id,
            "authorization_receipt_id": receipt["authorization_receipt_id"],
            "change_detection_mode": "fast",
        },
    )
    _close_broker(broker)

    assert refreshed["status"] == "ok"
    connection = sqlite3.connect(data_dir / "goodjob.sqlite3")
    old_status = connection.execute(
        "SELECT status FROM scan_runs WHERE scan_run_id = ?", (initial_run,)
    ).fetchone()[0]
    revisions = connection.execute(
        """
        SELECT COUNT(*)
        FROM source_revisions AS sr
        JOIN source_artifacts AS sa ON sa.artifact_id = sr.artifact_id
        WHERE sa.relative_path = 'main.py'
        """
    ).fetchone()[0]
    connection.close()
    assert old_status == "interrupted"
    assert revisions == 2


def test_internal_git_history_uses_remote_head_and_persists_bounded_commit_evidence(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    repository = workspace / "repository"
    repository.mkdir(parents=True)
    _git_init(repository)
    _git(repository, "config", "user.name", "Test Author")
    _git(repository, "config", "user.email", "author@example.test")
    _git(repository, "branch", "-M", "main")
    (repository / "pyproject.toml").write_text("[project]\nname='history'\n", encoding="utf-8")
    (repository / "main.py").write_text("print('main')\n", encoding="utf-8")
    _git(repository, "add", ".")
    _git(repository, "commit", "-m", "main baseline")
    main_commit = _git(repository, "rev-parse", "HEAD").stdout.strip()
    _git(repository, "checkout", "-b", "feature")
    (repository / "main.py").write_text("print('feature')\n", encoding="utf-8")
    _git(repository, "add", "main.py")
    _git(repository, "commit", "-m", "feature implementation")
    feature_commit = _git(repository, "rev-parse", "HEAD").stdout.strip()
    _git(repository, "update-ref", "refs/remotes/origin/main", main_commit)
    _git(repository, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")

    data_dir = tmp_path / "data"
    audit_env, audit = _git_wrapper(tmp_path)
    broker = _broker(data_dir, extra_env=audit_env)
    authorized = _send_json(
        broker,
        {"op": "authorize_source_analysis", "workspace": str(workspace), "confirmed": True},
    )
    receipt = _object_field(authorized, "receipt")
    scanned = _send_json(
        broker,
        {
            "op": "scan",
            "workspace": str(workspace),
            "authorization_receipt_id": receipt["authorization_receipt_id"],
        },
    )
    _close_broker(broker)

    assert scanned["status"] == "ok"
    coverage = _object_field(scanned, "coverage")
    assert coverage["git_history_evidence"] == 2
    history_basis = _object_field(coverage, "history_basis_by_worktree")
    assert history_basis[str(repository)] == "head_plus_remote_head"
    assert not audit.exists(), "the scanner must not resolve Git through inherited PATH"

    connection = sqlite3.connect(data_dir / "goodjob.sqlite3")
    rows = connection.execute(
        """
        SELECT evidence_id, locator, summary
        FROM evidence
        WHERE origin_kind = 'git_commit' AND evidence_kind = 'git_history'
        ORDER BY locator
        """
    ).fetchall()
    basis = connection.execute("SELECT history_basis FROM worktree_observations").fetchone()[0]
    validity_count = connection.execute(
        """
        SELECT COUNT(*) FROM evidence_validities AS ev
        JOIN evidence AS e ON e.evidence_id = ev.evidence_id
        WHERE e.origin_kind = 'git_commit' AND ev.validity = 'current'
        """
    ).fetchone()[0]
    connection.close()
    locators = [json.loads(str(row[1])) for row in rows]
    assert basis == "head_plus_remote_head"
    assert {locator["commit"] for locator in locators} == {main_commit, feature_commit}
    assert all(locator["changed_paths"] for locator in locators)
    assert all("print('" not in str(row[2]) for row in rows)
    assert validity_count == 2


def test_root_external_linked_worktree_requires_two_stage_authorization_and_never_reads_history(
    tmp_path: Path,
) -> None:
    outside_repository = tmp_path / "outside-repository"
    workspace = tmp_path / "workspace"
    linked_worktree = workspace / "linked"
    outside_repository.mkdir()
    workspace.mkdir()
    _git_init(outside_repository)
    _git(outside_repository, "config", "user.name", "Test Author")
    _git(outside_repository, "config", "user.email", "author@example.test")
    (outside_repository / "pyproject.toml").write_text(
        "[project]\nname='linked'\n", encoding="utf-8"
    )
    (outside_repository / "main.py").write_text("print('linked')\n", encoding="utf-8")
    _git(outside_repository, "add", ".")
    _git(outside_repository, "commit", "-m", "linked baseline")
    _git(outside_repository, "worktree", "add", str(linked_worktree))
    pointer = linked_worktree / ".git"
    assert pointer.is_file()

    data_dir = tmp_path / "data"
    audit_env, audit = _git_wrapper(tmp_path)
    broker = _broker(data_dir, extra_env=audit_env)
    authorized = _send_json(
        broker,
        {"op": "authorize_source_analysis", "workspace": str(workspace), "confirmed": True},
    )
    source_receipt = _object_field(authorized, "receipt")
    denied = _send_json(
        broker,
        {
            "op": "scan",
            "workspace": str(workspace),
            "authorization_receipt_id": source_receipt["authorization_receipt_id"],
        },
    )
    assert denied["status"] == "ok"
    denied_issues = denied["issues"]
    assert isinstance(denied_issues, list)
    assert any(
        isinstance(issue, dict) and issue.get("kind") == "external_git_authorization_required"
        for issue in denied_issues
    )

    inspected = _send_json(
        broker,
        {
            "op": "inspect_external_git_candidate",
            "workspace": str(workspace),
            "git_pointer": str(pointer),
            "authorization_receipt_id": source_receipt["authorization_receipt_id"],
        },
    )
    assert inspected["status"] == "ok"
    candidate = _object_field(inspected, "candidate")
    assert candidate["marker_kind"] == "file"
    assert candidate["git_pointer_path"] == str(pointer)
    assert candidate["common_dir_candidate"] is None

    relation_authorized = _send_json(
        broker,
        {
            "op": "authorize_external_git_relation_probe",
            "workspace": str(workspace),
            "git_pointer": str(pointer),
            "confirmed": True,
        },
    )
    relation_receipt = _object_field(relation_authorized, "receipt")
    relation_scope = json.loads(str(relation_receipt["scope_descriptor"]))
    assert relation_scope["marker_kind"] == "file"
    assert relation_scope["git_dir_candidate"] == candidate["git_dir_candidate"]
    assert relation_scope["common_dir_candidate"] is None
    original_pointer = pointer.read_text(encoding="utf-8")
    pointer.write_text(f"gitdir: {tmp_path / 'replacement-git-dir'}\n", encoding="utf-8")
    rejected_replacement = _send_json(
        broker,
        {
            "op": "probe_external_git_relation",
            "workspace": str(workspace),
            "git_pointer": str(pointer),
            "authorization_receipt_id": source_receipt["authorization_receipt_id"],
            "relation_authorization_receipt_id": relation_receipt["authorization_receipt_id"],
        },
    )
    assert rejected_replacement["status"] == "error"
    assert rejected_replacement["code"] == "invalid_input"
    assert not audit.exists()
    pointer.write_text(original_pointer, encoding="utf-8")
    probed = _send_json(
        broker,
        {
            "op": "probe_external_git_relation",
            "workspace": str(workspace),
            "git_pointer": str(pointer),
            "authorization_receipt_id": source_receipt["authorization_receipt_id"],
            "relation_authorization_receipt_id": relation_receipt["authorization_receipt_id"],
        },
    )
    assert probed["status"] == "ok"
    relation = _object_field(probed, "relation")
    assert relation["git_pointer_path"] == str(pointer)
    assert relation["marker_kind"] == "file"
    assert isinstance(relation["git_dir"], str)
    assert isinstance(relation["common_dir"], str)
    assert isinstance(relation["git_dir_device"], int)
    assert isinstance(relation["git_dir_inode"], int)
    assert not audit.exists()
    rejected_metadata = _send_json(
        broker,
        {
            "op": "authorize_external_git_metadata",
            "workspace": str(workspace),
            "git_pointer": str(pointer),
            "git_dir": str(tmp_path / "forged-git-dir"),
            "common_dir": relation["common_dir"],
            "confirmed": True,
        },
    )
    assert rejected_metadata["status"] == "error"
    assert rejected_metadata["code"] == "invalid_input"
    metadata_authorized = _send_json(
        broker,
        {
            "op": "authorize_external_git_metadata",
            "workspace": str(workspace),
            "git_pointer": str(pointer),
            "git_dir": relation["git_dir"],
            "common_dir": relation["common_dir"],
            "confirmed": True,
        },
    )
    metadata_receipt = _object_field(metadata_authorized, "receipt")
    metadata_scope = json.loads(str(metadata_receipt["scope_descriptor"]))
    assert metadata_scope["marker_kind"] == "file"
    assert metadata_scope["git_dir_device"] == relation["git_dir_device"]
    assert metadata_scope["git_dir_inode"] == relation["git_dir_inode"]
    pointer.write_text(f"gitdir: {tmp_path / 'post-grant-replacement'}\n", encoding="utf-8")
    rejected_after_metadata = _send_json(
        broker,
        {
            "op": "scan",
            "workspace": str(workspace),
            "authorization_receipt_id": source_receipt["authorization_receipt_id"],
            "external_git_metadata_receipt_ids": [metadata_receipt["authorization_receipt_id"]],
        },
    )
    rejected_after_metadata_issues = rejected_after_metadata["issues"]
    assert isinstance(rejected_after_metadata_issues, list)
    assert any(
        isinstance(issue, dict) and issue.get("kind") == "external_git_relation_mismatch"
        for issue in rejected_after_metadata_issues
    )
    assert not audit.exists()
    pointer.write_text(original_pointer, encoding="utf-8")
    (outside_repository / ".git" / "config").write_text(
        "this is intentionally invalid Git config\n", encoding="utf-8"
    )
    accepted = _send_json(
        broker,
        {
            "op": "scan",
            "workspace": str(workspace),
            "authorization_receipt_id": source_receipt["authorization_receipt_id"],
            "external_git_metadata_receipt_ids": [metadata_receipt["authorization_receipt_id"]],
        },
    )
    _close_broker(broker)

    assert accepted["status"] == "ok"
    accepted_coverage = _object_field(accepted, "coverage")
    assert accepted_coverage["fresh_projects"] == 1
    assert accepted_coverage["git_history_evidence"] == 0
    history_basis = _object_field(accepted_coverage, "history_basis_by_worktree")
    assert history_basis[str(linked_worktree)] == "external_metadata_only"
    external_metadata = _object_field(accepted_coverage, "external_git_metadata")
    linked_metadata = external_metadata[str(linked_worktree)]
    assert isinstance(linked_metadata, dict)
    assert linked_metadata["git_dir"] == relation["git_dir"]
    assert linked_metadata["read_fields"] == ["gitdir", "commondir", "head", "ref"]
    accepted_issues = accepted["issues"]
    assert isinstance(accepted_issues, list)
    assert any(
        isinstance(issue, dict) and issue.get("kind") == "external_git_history_unavailable"
        for issue in accepted_issues
    )
    assert not audit.exists(), "external metadata mode must not invoke inherited Git"

    connection = sqlite3.connect(data_dir / "goodjob.sqlite3")
    observation = connection.execute(
        """
        SELECT history_basis, external_git_dir, external_common_dir, external_metadata_receipt_id
        FROM worktree_observations
        """
    ).fetchone()
    history_count = connection.execute(
        "SELECT COUNT(*) FROM evidence WHERE evidence_kind = 'git_history'"
    ).fetchone()[0]
    source_commit_states = {
        str(row[0])
        for row in connection.execute(
            "SELECT DISTINCT commit_state FROM evidence WHERE origin_kind = 'source_revision'"
        )
    }
    connection.close()
    assert observation == (
        "external_metadata_only",
        relation["git_dir"],
        relation["common_dir"],
        metadata_receipt["authorization_receipt_id"],
    )
    assert history_count == 0
    assert source_commit_states == {"not_applicable"}


def test_root_internal_linked_worktree_is_grouped_without_external_authorization(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    primary = workspace / "primary"
    linked = workspace / "linked"
    primary.mkdir(parents=True)
    _git_init(primary)
    _git(primary, "config", "user.name", "Test Author")
    _git(primary, "config", "user.email", "author@example.test")
    (primary / "pyproject.toml").write_text("[project]\nname='internal-linked'\n", encoding="utf-8")
    (primary / "main.py").write_text("print('primary')\n", encoding="utf-8")
    _git(primary, "add", ".")
    _git(primary, "commit", "-m", "initial")
    _git(primary, "worktree", "add", "-b", "linked-branch", str(linked))

    data_dir = tmp_path / "data"
    broker = _broker(data_dir)
    authorized = _send_json(
        broker,
        {"op": "authorize_source_analysis", "workspace": str(workspace), "confirmed": True},
    )
    receipt = _object_field(authorized, "receipt")
    scanned = _send_json(
        broker,
        {
            "op": "scan",
            "workspace": str(workspace),
            "authorization_receipt_id": receipt["authorization_receipt_id"],
        },
    )
    _close_broker(broker)

    assert scanned["status"] == "ok"
    coverage = _object_field(scanned, "coverage")
    assert coverage["fresh_projects"] == 1
    assert coverage["worktrees"] == 2
    assert coverage["external_git_metadata"] == {}
    issues = scanned["issues"]
    assert isinstance(issues, list)
    assert not any(
        isinstance(issue, dict) and issue.get("kind") == "external_git_authorization_required"
        for issue in issues
    )
    connection = sqlite3.connect(data_dir / "goodjob.sqlite3")
    project_count = connection.execute("SELECT COUNT(*) FROM projects").fetchone()[0]
    worktree_count = connection.execute("SELECT COUNT(*) FROM worktrees").fetchone()[0]
    connection.close()
    assert project_count == 1
    assert worktree_count == 2


def test_git_directory_with_external_commondir_uses_the_same_candidate_bound_protocol(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    repository = workspace / "repository"
    repository.mkdir(parents=True)
    _git_init(repository)
    _git(repository, "config", "user.name", "Test Author")
    _git(repository, "config", "user.email", "author@example.test")
    _git(repository, "branch", "-M", "main")
    (repository / "pyproject.toml").write_text(
        "[project]\nname='external-common'\n", encoding="utf-8"
    )
    _git(repository, "add", ".")
    _git(repository, "commit", "-m", "external common baseline")

    git_dir = repository / ".git"
    common_dir = tmp_path / "outside-common"
    common_dir.mkdir()
    for child in tuple(git_dir.iterdir()):
        if child.name not in {"HEAD", "index"}:
            shutil.move(str(child), common_dir / child.name)
    (git_dir / "commondir").write_text(str(common_dir) + "\n", encoding="utf-8")

    data_dir = tmp_path / "data"
    broker = _broker(data_dir)
    authorized = _send_json(
        broker,
        {"op": "authorize_source_analysis", "workspace": str(workspace), "confirmed": True},
    )
    source_receipt = _object_field(authorized, "receipt")
    denied = _send_json(
        broker,
        {
            "op": "scan",
            "workspace": str(workspace),
            "authorization_receipt_id": source_receipt["authorization_receipt_id"],
        },
    )
    assert any(
        isinstance(issue, dict) and issue.get("kind") == "external_git_authorization_required"
        for issue in cast(list[object], denied["issues"])
    )

    inspected = _send_json(
        broker,
        {
            "op": "inspect_external_git_candidate",
            "workspace": str(workspace),
            "git_pointer": str(git_dir),
            "authorization_receipt_id": source_receipt["authorization_receipt_id"],
        },
    )
    candidate = _object_field(inspected, "candidate")
    assert candidate["marker_kind"] == "directory"
    assert candidate["git_dir_candidate"] == str(git_dir)
    assert candidate["common_dir_candidate"] == str(common_dir)
    relation_authorized = _send_json(
        broker,
        {
            "op": "authorize_external_git_relation_probe",
            "workspace": str(workspace),
            "git_pointer": str(git_dir),
            "confirmed": True,
        },
    )
    relation_receipt = _object_field(relation_authorized, "receipt")
    probed = _send_json(
        broker,
        {
            "op": "probe_external_git_relation",
            "workspace": str(workspace),
            "git_pointer": str(git_dir),
            "authorization_receipt_id": source_receipt["authorization_receipt_id"],
            "relation_authorization_receipt_id": relation_receipt["authorization_receipt_id"],
        },
    )
    relation = _object_field(probed, "relation")
    assert relation["marker_kind"] == "directory"
    assert relation["git_dir"] == str(git_dir)
    assert relation["common_dir"] == str(common_dir)
    metadata_authorized = _send_json(
        broker,
        {
            "op": "authorize_external_git_metadata",
            "workspace": str(workspace),
            "git_pointer": str(git_dir),
            "git_dir": str(git_dir),
            "common_dir": str(common_dir),
            "confirmed": True,
        },
    )
    metadata_receipt = _object_field(metadata_authorized, "receipt")
    (common_dir / "config").write_text("invalid config is never read\n", encoding="utf-8")
    accepted = _send_json(
        broker,
        {
            "op": "scan",
            "workspace": str(workspace),
            "authorization_receipt_id": source_receipt["authorization_receipt_id"],
            "external_git_metadata_receipt_ids": [metadata_receipt["authorization_receipt_id"]],
        },
    )
    _close_broker(broker)

    assert accepted["status"] == "ok"
    coverage = _object_field(accepted, "coverage")
    assert coverage["fresh_projects"] == 1
    assert coverage["git_history_evidence"] == 0
    metadata = _object_field(coverage, "external_git_metadata")
    assert str(repository) in metadata


def test_git_history_falls_back_to_main_master_or_head_only_and_handles_detached_head(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    repositories = {
        "main": "main",
        "master": "master",
        "custom": "release",
    }
    for name, branch in repositories.items():
        repository = workspace / name
        repository.mkdir(parents=True)
        _git_init(repository)
        _git(repository, "config", "user.name", "Test Author")
        _git(repository, "config", "user.email", "author@example.test")
        _git(repository, "branch", "-M", branch)
        (repository / "pyproject.toml").write_text(f"[project]\nname='{name}'\n", encoding="utf-8")
        _git(repository, "add", ".")
        _git(repository, "commit", "-m", f"{name} baseline")

    data_dir = tmp_path / "data"
    broker = _broker(data_dir)
    authorized = _send_json(
        broker,
        {"op": "authorize_source_analysis", "workspace": str(workspace), "confirmed": True},
    )
    receipt = _object_field(authorized, "receipt")
    initial = _send_json(
        broker,
        {
            "op": "scan",
            "workspace": str(workspace),
            "authorization_receipt_id": receipt["authorization_receipt_id"],
        },
    )
    initial_run = _object_field(initial, "scan_run")
    workspace_id = initial_run["workspace_id"]
    assert isinstance(workspace_id, str)

    connection = sqlite3.connect(data_dir / "goodjob.sqlite3")
    initial_basis = dict(
        connection.execute(
            """
            SELECT w.canonical_root, wo.history_basis
            FROM worktree_observations AS wo
            JOIN worktrees AS w ON w.worktree_id = wo.worktree_id
            WHERE wo.scan_run_id = ?
            """,
            (initial_run["scan_run_id"],),
        ).fetchall()
    )
    connection.close()
    assert initial_basis[str(workspace / "main")] == "head_plus_main"
    assert initial_basis[str(workspace / "master")] == "head_plus_master"
    assert initial_basis[str(workspace / "custom")] == "head_only_no_default_ref"

    _git(workspace / "main", "checkout", "--detach", "HEAD")
    refreshed = _send_json(
        broker,
        {
            "op": "refresh",
            "workspace": str(workspace),
            "workspace_id": workspace_id,
            "authorization_receipt_id": receipt["authorization_receipt_id"],
            "change_detection_mode": "verify_content",
        },
    )
    _close_broker(broker)

    assert refreshed["status"] == "ok"
    refresh_run = _object_field(refreshed, "scan_run")
    connection = sqlite3.connect(data_dir / "goodjob.sqlite3")
    detached_basis = connection.execute(
        """
        SELECT wo.history_basis
        FROM worktree_observations AS wo
        JOIN worktrees AS w ON w.worktree_id = wo.worktree_id
        WHERE wo.scan_run_id = ? AND w.canonical_root = ?
        """,
        (refresh_run["scan_run_id"], str(workspace / "main")),
    ).fetchone()[0]
    connection.close()
    assert detached_basis == "head_only_detached"


def test_git_history_checks_every_remote_head_before_declaring_a_unique_default(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    repository = workspace / "repository"
    repository.mkdir(parents=True)
    _git_init(repository)
    _git(repository, "config", "user.name", "Test Author")
    _git(repository, "config", "user.email", "author@example.test")
    _git(repository, "branch", "-M", "main")
    (repository / "pyproject.toml").write_text("[project]\nname='remote-heads'\n", encoding="utf-8")
    _git(repository, "add", ".")
    _git(repository, "commit", "-m", "remote head baseline")
    head = _git(repository, "rev-parse", "HEAD").stdout.strip()
    _git(repository, "update-ref", "refs/remotes/origin/main", head)
    _git(repository, "update-ref", "refs/remotes/other/feature", head)
    for index in range(33):
        _git(
            repository,
            "symbolic-ref",
            f"refs/remotes/a{index:02d}/HEAD",
            "refs/remotes/origin/main",
        )
    _git(
        repository,
        "symbolic-ref",
        "refs/remotes/z-other/HEAD",
        "refs/remotes/other/feature",
    )

    data_dir = tmp_path / "data"
    broker = _broker(data_dir)
    authorized = _send_json(
        broker,
        {"op": "authorize_source_analysis", "workspace": str(workspace), "confirmed": True},
    )
    receipt = _object_field(authorized, "receipt")
    scanned = _send_json(
        broker,
        {
            "op": "scan",
            "workspace": str(workspace),
            "authorization_receipt_id": receipt["authorization_receipt_id"],
        },
    )
    _close_broker(broker)

    assert scanned["status"] == "ok"
    coverage = _object_field(scanned, "coverage")
    basis = _object_field(coverage, "history_basis_by_worktree")
    assert basis[str(repository)] == "head_only_ambiguous_remote_head"
    assert coverage["git_history_evidence"] == 1


def test_git_history_keeps_the_head_but_excludes_non_head_commits_older_than_180_days(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    repository = workspace / "repository"
    repository.mkdir(parents=True)
    _git_init(repository)
    _git(repository, "config", "user.name", "Test Author")
    _git(repository, "config", "user.email", "author@example.test")
    _git(repository, "branch", "-M", "main")
    (repository / "pyproject.toml").write_text("[project]\nname='window'\n", encoding="utf-8")
    _git(repository, "add", ".")
    old_time = (datetime.now(UTC) - timedelta(days=181)).strftime("%Y-%m-%dT%H:%M:%S+0000")
    _git(
        repository,
        "commit",
        "-m",
        "old baseline",
        extra_env={"GIT_AUTHOR_DATE": old_time, "GIT_COMMITTER_DATE": old_time},
    )
    old_commit = _git(repository, "rev-parse", "HEAD").stdout.strip()
    (repository / "main.py").write_text("print('recent')\n", encoding="utf-8")
    _git(repository, "add", "main.py")
    _git(repository, "commit", "-m", "recent implementation")
    recent_commit = _git(repository, "rev-parse", "HEAD").stdout.strip()

    data_dir = tmp_path / "data"
    broker = _broker(data_dir)
    authorized = _send_json(
        broker,
        {"op": "authorize_source_analysis", "workspace": str(workspace), "confirmed": True},
    )
    receipt = _object_field(authorized, "receipt")
    scanned = _send_json(
        broker,
        {
            "op": "scan",
            "workspace": str(workspace),
            "authorization_receipt_id": receipt["authorization_receipt_id"],
        },
    )
    _close_broker(broker)

    assert scanned["status"] == "ok"
    connection = sqlite3.connect(data_dir / "goodjob.sqlite3")
    commits = {
        json.loads(str(row[0]))["commit"]
        for row in connection.execute(
            "SELECT locator FROM evidence WHERE origin_kind = 'git_commit'"
        ).fetchall()
    }
    connection.close()
    assert recent_commit in commits
    assert old_commit not in commits


def test_git_history_keeps_an_old_head_as_the_current_worktree_anchor(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    repository = workspace / "repository"
    repository.mkdir(parents=True)
    _git_init(repository)
    _git(repository, "config", "user.name", "Test Author")
    _git(repository, "config", "user.email", "author@example.test")
    _git(repository, "branch", "-M", "main")
    (repository / "pyproject.toml").write_text("[project]\nname='old-head'\n", encoding="utf-8")
    _git(repository, "add", ".")
    old_time = (datetime.now(UTC) - timedelta(days=181)).strftime("%Y-%m-%dT%H:%M:%S+0000")
    _git(
        repository,
        "commit",
        "-m",
        "old current head",
        extra_env={"GIT_AUTHOR_DATE": old_time, "GIT_COMMITTER_DATE": old_time},
    )
    head_commit = _git(repository, "rev-parse", "HEAD").stdout.strip()

    data_dir = tmp_path / "data"
    broker = _broker(data_dir)
    authorized = _send_json(
        broker,
        {"op": "authorize_source_analysis", "workspace": str(workspace), "confirmed": True},
    )
    receipt = _object_field(authorized, "receipt")
    scanned = _send_json(
        broker,
        {
            "op": "scan",
            "workspace": str(workspace),
            "authorization_receipt_id": receipt["authorization_receipt_id"],
        },
    )
    _close_broker(broker)

    assert scanned["status"] == "ok"
    connection = sqlite3.connect(data_dir / "goodjob.sqlite3")
    commits = {
        json.loads(str(row[0]))["commit"]
        for row in connection.execute(
            "SELECT locator FROM evidence WHERE origin_kind = 'git_commit'"
        ).fetchall()
    }
    connection.close()
    assert commits == {head_commit}

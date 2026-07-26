from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

RUNTIME_DIR = Path(__file__).resolve().parents[1]


def _git(
    repository: Path,
    *arguments: str,
    extra_env: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    if extra_env is not None:
        environment.update(extra_env)
    return subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    )


def _commit(repository: Path, message: str, committed_at: datetime) -> str:
    timestamp = committed_at.isoformat()
    environment = {
        "GIT_AUTHOR_DATE": timestamp,
        "GIT_COMMITTER_DATE": timestamp,
    }
    _git(repository, "add", ".")
    _git(repository, "commit", "-m", message, extra_env=environment)
    return _git(repository, "rev-parse", "HEAD").stdout.strip()


def _broker(data_dir: Path) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [sys.executable, "scripts/session.py", "--data-dir", str(data_dir)],
        cwd=RUNTIME_DIR,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env={**os.environ, "PYTHONPATH": str(RUNTIME_DIR / "src")},
    )


def _send_json(
    process: subprocess.Popen[str], payload: dict[str, object]
) -> dict[str, object]:
    assert process.stdin is not None
    assert process.stdout is not None
    process.stdin.write(json.dumps(payload) + "\n")
    process.stdin.flush()
    response = json.loads(process.stdout.readline())
    assert isinstance(response, dict)
    return cast(dict[str, object], response)


def _close_broker(process: subprocess.Popen[str]) -> None:
    assert process.stdin is not None
    process.stdin.close()
    assert process.wait(timeout=5) == 0
    assert process.stderr is not None
    assert process.stderr.read() == ""


def _object(payload: dict[str, object], key: str) -> dict[str, object]:
    value = payload[key]
    assert isinstance(value, dict)
    return cast(dict[str, object], value)


def test_targeted_history_is_bounded_transient_and_session_scoped(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    repository = workspace / "repository"
    repository.mkdir(parents=True)
    _git(repository, "init")
    _git(repository, "config", "user.name", "Test Author")
    _git(repository, "config", "user.email", "author@example.test")
    (repository / "pyproject.toml").write_text(
        "[project]\nname='history-query'\n", encoding="utf-8"
    )
    historical_source = "HISTORY_ONLY_SECRET = 'never-persist-this-source'\n"
    (repository / "main.py").write_text(historical_source, encoding="utf-8")
    magic_path = ":(glob)*"
    historical_magic_source = "MAGIC_PATH_SECRET = 'literal-path-only'\n"
    (repository / magic_path).write_text(historical_magic_source, encoding="utf-8")
    old_commit = _commit(
        repository,
        "legacy implementation",
        datetime.now(UTC) - timedelta(days=365),
    )
    (repository / "main.py").write_text(
        "def current_implementation() -> str:\n    return 'current'\n",
        encoding="utf-8",
    )
    (repository / magic_path).write_text("MAGIC_PATH_CURRENT = True\n", encoding="utf-8")
    recent_commit = _commit(
        repository,
        "current implementation",
        datetime.now(UTC) - timedelta(days=5),
    )

    data_dir = tmp_path / "data"
    broker = _broker(data_dir)
    authorized = _send_json(
        broker,
        {"op": "authorize_source_analysis", "workspace": str(workspace), "confirmed": True},
    )
    receipt = _object(authorized, "receipt")
    receipt_id = receipt["authorization_receipt_id"]
    assert isinstance(receipt_id, str)
    scanned = _send_json(
        broker,
        {
            "op": "scan",
            "workspace": str(workspace),
            "authorization_receipt_id": receipt_id,
        },
    )
    scan_run = _object(scanned, "scan_run")
    scan_run_id = scan_run["scan_run_id"]
    assert isinstance(scan_run_id, str)

    connection = sqlite3.connect(data_dir / "goodjob.sqlite3")
    identifiers = connection.execute(
        """
        SELECT p.project_id, w.worktree_id
        FROM projects AS p
        JOIN worktrees AS w ON w.project_id = p.project_id
        WHERE w.canonical_root = ?
        """,
        (str(repository),),
    ).fetchone()
    assert identifiers is not None
    project_id, worktree_id = map(str, identifiers)
    persisted_history = "\n".join(
        str(row[0])
        for row in connection.execute(
            "SELECT locator FROM evidence WHERE origin_kind = 'git_commit'"
        )
    )
    connection.close()
    assert recent_commit in persisted_history
    assert old_commit not in persisted_history

    queried = _send_json(
        broker,
        {
            "op": "query_history_candidates",
            "workspace": str(workspace),
            "authorization_receipt_id": receipt_id,
            "scan_run_id": scan_run_id,
            "role_lens_id": "role-lens-test-v1",
            "project_id": project_id,
            "worktree_id": worktree_id,
            "relative_paths": ["main.py"],
            "query_reason": "验证旧实现的技术演进",
            "maximum_candidates": 5,
        },
    )
    history_query = _object(queried, "history_query")
    candidates = history_query["candidates"]
    assert isinstance(candidates, list)
    assert len(candidates) == 1
    candidate = candidates[0]
    assert isinstance(candidate, dict)
    candidate = cast(dict[str, object], candidate)
    assert candidate["commit"] == old_commit
    candidate_id = candidate["candidate_id"]
    assert isinstance(candidate_id, str)

    forged = _send_json(
        broker,
        {
            "op": "read_history_candidate",
            "workspace": str(workspace),
            "authorization_receipt_id": receipt_id,
            "candidate_id": "0" * 64,
            "selected_path": "main.py",
        },
    )
    assert forged["status"] == "error"
    assert forged["code"] == "invalid_input"

    selected = _send_json(
        broker,
        {
            "op": "read_history_candidate",
            "workspace": str(workspace),
            "authorization_receipt_id": receipt_id,
            "candidate_id": candidate_id,
            "selected_path": "main.py",
        },
    )
    candidate_read = _object(selected, "history_candidate_read")
    assert candidate_read["persisted"] is False
    assert candidate_read["blob_text"] == historical_source
    assert isinstance(candidate_read["diff_sha256"], str)
    assert isinstance(candidate_read["blob_sha256"], str)

    magic_query = _send_json(
        broker,
        {
            "op": "query_history_candidates",
            "workspace": str(workspace),
            "authorization_receipt_id": receipt_id,
            "scan_run_id": scan_run_id,
            "role_lens_id": "role-lens-test-v1",
            "project_id": project_id,
            "worktree_id": worktree_id,
            "relative_paths": [magic_path],
            "query_reason": "验证 Git pathspec magic 文件名按字面量处理",
            "maximum_candidates": 5,
        },
    )
    magic_candidates = _object(magic_query, "history_query")["candidates"]
    assert isinstance(magic_candidates, list)
    assert len(magic_candidates) == 1
    magic_candidate = magic_candidates[0]
    assert isinstance(magic_candidate, dict)
    magic_candidate = cast(dict[str, object], magic_candidate)
    assert magic_candidate["changed_paths"] == [magic_path]
    magic_candidate_id = magic_candidate["candidate_id"]
    assert isinstance(magic_candidate_id, str)
    magic_read = _send_json(
        broker,
        {
            "op": "read_history_candidate",
            "workspace": str(workspace),
            "authorization_receipt_id": receipt_id,
            "candidate_id": magic_candidate_id,
            "selected_path": magic_path,
        },
    )
    magic_content = _object(magic_read, "history_candidate_read")
    assert magic_content["blob_text"] == historical_magic_source
    assert "main.py" not in str(magic_content["diff_text"])

    database_bytes = (data_dir / "goodjob.sqlite3").read_bytes()
    assert historical_source.encode() not in database_bytes
    assert historical_magic_source.encode() not in database_bytes
    _close_broker(broker)

    fresh_broker = _broker(data_dir)
    other_workspace = tmp_path / "other-workspace"
    other_workspace.mkdir()
    other_authorized = _send_json(
        fresh_broker,
        {
            "op": "authorize_source_analysis",
            "workspace": str(other_workspace),
            "confirmed": True,
        },
    )
    other_receipt = _object(other_authorized, "receipt")
    rejected_cross_workspace = _send_json(
        fresh_broker,
        {
            "op": "query_history_candidates",
            "workspace": str(other_workspace),
            "authorization_receipt_id": other_receipt["authorization_receipt_id"],
            "scan_run_id": scan_run_id,
            "role_lens_id": "role-lens-test-v1",
            "project_id": project_id,
            "worktree_id": worktree_id,
            "relative_paths": ["main.py"],
            "query_reason": "不得借用另一工作区的冻结快照",
            "maximum_candidates": 5,
        },
    )
    assert rejected_cross_workspace["status"] == "error"
    assert rejected_cross_workspace["code"] == "invalid_input"

    fresh_authorized = _send_json(
        fresh_broker,
        {"op": "authorize_source_analysis", "workspace": str(workspace), "confirmed": True},
    )
    fresh_receipt = _object(fresh_authorized, "receipt")
    rejected_reuse = _send_json(
        fresh_broker,
        {
            "op": "read_history_candidate",
            "workspace": str(workspace),
            "authorization_receipt_id": fresh_receipt["authorization_receipt_id"],
            "candidate_id": candidate_id,
            "selected_path": "main.py",
        },
    )
    assert rejected_reuse["status"] == "error"
    assert rejected_reuse["code"] == "invalid_input"
    _close_broker(fresh_broker)

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

RUNTIME_DIR = Path(__file__).resolve().parents[1]


def test_session_wrapper_uses_fd_and_does_not_echo_capability(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    data_dir = tmp_path / "data"
    result = subprocess.run(
        [
            sys.executable,
            "scripts/session.py",
            "--data-dir",
            str(data_dir),
            "--workspace",
            str(workspace),
            "--verify",
        ],
        cwd=RUNTIME_DIR,
        check=False,
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": str(RUNTIME_DIR / "src")},
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["status"] == "ok"
    assert payload["verified"] is True
    assert "session_capability" not in result.stdout
    assert "session_capability" not in result.stderr

    status = subprocess.run(
        [sys.executable, "-m", "goodjob", "--data-dir", str(data_dir), "data-status"],
        cwd=RUNTIME_DIR,
        check=False,
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": str(RUNTIME_DIR / "src")},
    )
    assert status.returncode == 0, status.stderr
    assert json.loads(status.stdout)["usage_bytes"]["sqlite"] > 0

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

RUNTIME_DIR = Path(__file__).resolve().parents[1]


def _send_json(process: subprocess.Popen[str], payload: dict[str, object]) -> dict[str, object]:
    assert process.stdin is not None
    assert process.stdout is not None
    process.stdin.write(json.dumps(payload) + "\n")
    process.stdin.flush()
    response = json.loads(process.stdout.readline())
    assert isinstance(response, dict)
    return response


def test_session_broker_reuses_one_fd_capability_until_stdin_closes(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    data_dir = tmp_path / "data"
    broker = subprocess.Popen(
        [
            sys.executable,
            "scripts/session.py",
            "--data-dir",
            str(data_dir),
        ],
        cwd=RUNTIME_DIR,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env={**os.environ, "PYTHONPATH": str(RUNTIME_DIR / "src")},
    )

    authorized = _send_json(
        broker,
        {"op": "authorize_source_analysis", "workspace": str(workspace), "confirmed": True},
    )
    assert authorized["status"] == "ok"
    receipt = authorized["receipt"]
    assert isinstance(receipt, dict)
    verified = _send_json(
        broker,
        {
            "op": "verify_source_analysis",
            "workspace": str(workspace),
            "authorization_receipt_id": receipt["authorization_receipt_id"],
        },
    )
    assert verified["status"] == "ok"
    assert "session_capability" not in json.dumps(authorized)
    assert "session_capability" not in json.dumps(verified)

    assert broker.stdin is not None
    broker.stdin.close()
    assert broker.wait(timeout=5) == 0
    assert broker.stderr is not None
    assert broker.stderr.read() == ""

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

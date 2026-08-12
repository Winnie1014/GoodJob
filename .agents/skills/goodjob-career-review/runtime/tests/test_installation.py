from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

RUNTIME_DIR = Path(__file__).resolve().parents[1]


def test_isolated_installed_copy_runs_real_sandboxed_scan_without_skill_venv(
    tmp_path: Path, git_sandbox_available: None
) -> None:
    uv = shutil.which("uv")
    assert uv is not None
    installed_runtime = tmp_path / "installed-skill" / "runtime"
    shutil.copytree(
        RUNTIME_DIR,
        installed_runtime,
        ignore=shutil.ignore_patterns(
            ".venv",
            "node_modules",
            "__pycache__",
            "*.pyc",
            ".mypy_cache",
            ".pytest_cache",
        ),
    )
    assert (installed_runtime / "src/goodjob/dashboard_assets/dashboard.js").is_file()
    assert (installed_runtime / "src/goodjob/dashboard_assets/dashboard.css").is_file()
    assert (installed_runtime / "src/goodjob/review.py").is_file()
    assert (installed_runtime / "src/goodjob/exporting.py").is_file()
    assert not (installed_runtime / "frontend/node_modules").exists()
    data_dir = tmp_path / "owner-data"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "pyproject.toml").write_text(
        "[project]\nname='installed-copy'\n", encoding="utf-8"
    )
    (workspace / "main.py").write_text("VALUE = 1\n", encoding="utf-8")
    subprocess.run(["git", "init"], cwd=workspace, check=True, capture_output=True)
    subprocess.run(
        ["git", "add", "pyproject.toml", "main.py"],
        cwd=workspace,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=GoodJob Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-m",
            "fixture",
        ],
        cwd=workspace,
        check=True,
        capture_output=True,
    )
    process = subprocess.Popen(
        [
            uv,
            "run",
            "--isolated",
            "--no-project",
            "--no-config",
            "--offline",
            "--no-python-downloads",
            "--python",
            "3.12",
            "python",
            "-I",
            "-B",
            str(installed_runtime / "scripts" / "session.py"),
            "--data-dir",
            str(data_dir),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=workspace,
        env={
            **os.environ,
            "UV_CACHE_DIR": str(tmp_path / "empty-uv-cache"),
            "UV_NO_PROGRESS": "1",
        },
    )

    def request(payload: dict[str, object]) -> dict[str, object]:
        assert process.stdin is not None
        assert process.stdout is not None
        process.stdin.write(json.dumps(payload) + "\n")
        process.stdin.flush()
        response = json.loads(process.stdout.readline())
        assert isinstance(response, dict)
        return response

    authorized = request(
        {
            "op": "authorize_source_analysis",
            "workspace": str(workspace),
            "confirmed": True,
        }
    )
    assert authorized["status"] == "ok"
    receipt = authorized["receipt"]
    assert isinstance(receipt, dict)
    receipt_id = receipt["authorization_receipt_id"]
    assert isinstance(receipt_id, str)
    validated = request(
        {
            "op": "validate_job_input",
            "workspace": str(workspace),
            "authorization_receipt_id": receipt_id,
            "job_input": {
                "contract_version": "job-input-v1",
                "target_role": "Platform Engineer",
                "jd_input": {"kind": "none"},
            },
        }
    )
    validated_job_input = validated["job_input"]
    assert isinstance(validated_job_input, dict)
    validation_sha256 = validated_job_input["validation_sha256"]
    scanned = request(
        {
            "op": "scan",
            "workspace": str(workspace),
            "authorization_receipt_id": receipt_id,
            "job_input_validation_sha256": validation_sha256,
        }
    )
    assert scanned["status"] == "ok"
    coverage = scanned["coverage"]
    assert isinstance(coverage, dict)
    assert coverage["git_history_evidence"] == 1
    assert process.stdin is not None
    process.stdin.close()
    assert process.wait(timeout=30) == 0
    assert process.stderr is not None
    assert process.stderr.read() == ""
    assert not (installed_runtime / ".venv").exists()
    assert not list(installed_runtime.rglob("__pycache__"))
    assert not list(installed_runtime.rglob("*.pyc"))

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

RUNTIME_DIR = Path(__file__).resolve().parents[1]


def test_isolated_installed_copy_does_not_create_a_skill_venv(tmp_path: Path) -> None:
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
    assert not (installed_runtime / "frontend/node_modules").exists()
    data_dir = tmp_path / "owner-data"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    result = subprocess.run(
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
        check=False,
        capture_output=True,
        input=json.dumps(
            {
                "op": "authorize_source_analysis",
                "workspace": str(workspace),
                "confirmed": True,
            }
        )
        + "\n",
        text=True,
        cwd=workspace,
        env={
            **os.environ,
            "UV_CACHE_DIR": str(tmp_path / "empty-uv-cache"),
            "UV_NO_PROGRESS": "1",
        },
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["status"] == "ok"
    assert not (installed_runtime / ".venv").exists()
    assert not list(installed_runtime.rglob("__pycache__"))
    assert not list(installed_runtime.rglob("*.pyc"))

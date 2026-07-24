from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

RUNTIME_DIR = Path(__file__).resolve().parents[1]


def test_isolated_installed_copy_does_not_create_a_skill_venv(tmp_path: Path) -> None:
    installed_runtime = tmp_path / "installed-skill" / "runtime"
    shutil.copytree(
        RUNTIME_DIR,
        installed_runtime,
        ignore=shutil.ignore_patterns(".venv", "__pycache__", ".mypy_cache", ".pytest_cache"),
    )
    data_dir = tmp_path / "owner-data"
    result = subprocess.run(
        [
            "uv",
            "run",
            "--isolated",
            "--with",
            str(installed_runtime),
            "python",
            str(installed_runtime / "scripts" / "session.py"),
            "--data-dir",
            str(data_dir),
        ],
        check=False,
        capture_output=True,
        input=json.dumps(
            {
                "op": "authorize_source_analysis",
                "workspace": str(tmp_path / "workspace"),
                "confirmed": True,
            }
        )
        + "\n",
        text=True,
        env=os.environ.copy(),
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["status"] == "ok"
    assert not (installed_runtime / ".venv").exists()

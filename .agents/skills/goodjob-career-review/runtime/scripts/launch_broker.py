#!/usr/bin/env python3
"""Launcher for the GoodJob session broker with uv detection and python fallback."""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
SESSION_SCRIPT = SCRIPTS_DIR / "session.py"


def _find_python312() -> str | None:
    candidate = shutil.which("python3.12")
    if candidate is not None:
        return candidate
    fallback = shutil.which("python3")
    if fallback is not None:
        try:
            version = subprocess.run(
                [fallback, "--version"], capture_output=True, text=True, check=False
            )
        except OSError:
            return None
        parts = version.stdout.strip().split()
        if len(parts) == 2 and parts[0] == "Python":
            try:
                major, minor = parts[1].split(".")[0:2]
                if int(major) == 3 and int(minor) >= 12:
                    return fallback
            except (ValueError, IndexError):
                pass
    return None


def main() -> None:
    parser = argparse.ArgumentParser(description="Launch the GoodJob session broker")
    parser.add_argument(
        "--agent-runtime",
        default="codex_task_runtime",
        help="host agent runtime identifier (default: codex_task_runtime)",
    )
    parser.add_argument(
        "--data-dir",
        default=None,
        help="owner-local state directory override",
    )
    args = parser.parse_args()

    broker_args = [str(SESSION_SCRIPT)]
    if args.data_dir:
        broker_args.extend(["--data-dir", args.data_dir])
    broker_args.extend(["--agent-runtime", args.agent_runtime])

    uv = shutil.which("uv")
    if uv is not None:
        command = [
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
            *broker_args,
        ]
    else:
        python = _find_python312()
        if python is None:
            sys.stderr.write(
                "error: uv is not installed and python3.12 was not found on PATH\n"
                "install uv (https://docs.astral.sh/uv/) or Python 3.12+\n"
            )
            sys.exit(1)
        command = [python, "-I", "-B", *broker_args]

    try:
        return_code = subprocess.call(command)
    except OSError:
        sys.stderr.write("error: failed to start the GoodJob session broker\n")
        raise SystemExit(1) from None
    raise SystemExit(return_code)


if __name__ == "__main__":
    main()

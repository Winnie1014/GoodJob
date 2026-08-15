#!/usr/bin/env python3
"""Launcher for the GoodJob session broker with fail-closed platform preflight."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import cast

RUNTIME_DIR = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = RUNTIME_DIR / "scripts"
SESSION_SCRIPT = SCRIPTS_DIR / "session.py"
WINDOWS_PREFLIGHT_SCRIPT = SCRIPTS_DIR / "windows_preflight.py"
TRUSTED_SOURCE_DIR = RUNTIME_DIR / "src"
sys.path.insert(0, str(TRUSTED_SOURCE_DIR))

from goodjob.platform.preflight_windows import (  # noqa: E402
    WindowsPreflightReportDict,
    missing_python_runtime_report,
)
from goodjob.platform.runtime_bootstrap import (  # noqa: E402
    PythonRuntime,
    discover_python312,
)


def _uv_command(uv: str) -> tuple[str, ...]:
    return (
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
    )


def _discover_runtime(platform_name: str) -> PythonRuntime | None:
    if platform_name == "win32":
        return discover_python312(platform_name=platform_name)
    uv = shutil.which("uv")
    if uv is not None:
        return PythonRuntime(_uv_command(uv), "uv", (3, 12, 0))
    return discover_python312(platform_name=platform_name)


def _preflight_protocol_failure(message: str) -> WindowsPreflightReportDict:
    return {
        "contract_version": "windows-prerequisite-preflight-v1",
        "status": "error",
        "can_start_broker": False,
        "checks": [
            {
                "id": "windows_preflight",
                "status": "failed",
                "code": "unsupported_capability",
                "message": message,
                "remediation": {
                    "action": "repair_skill_or_use_wsl2",
                    "purpose": "complete every mandatory prerequisite before protected execution",
                    "requires_explicit_consent": False,
                },
            }
        ],
        "notices": [
            "Native Windows Git subprocesses do not have filesystem read isolation; "
            "use WSL2 when complete Git filesystem isolation is required."
        ],
    }


def _run_windows_preflight(runtime: PythonRuntime, workspace: str) -> WindowsPreflightReportDict:
    command = [
        *runtime.command,
        "-I",
        "-B",
        str(WINDOWS_PREFLIGHT_SCRIPT),
        "--workspace",
        workspace,
        "--launcher-kind",
        runtime.kind,
    ]
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
            timeout=30.0,
        )
    except (OSError, subprocess.SubprocessError):
        return _preflight_protocol_failure("the Windows prerequisite probe could not start")
    try:
        raw: object = json.loads(result.stdout)
    except (json.JSONDecodeError, RecursionError):
        return _preflight_protocol_failure("the Windows prerequisite probe returned invalid JSON")
    if (
        not isinstance(raw, dict)
        or raw.get("contract_version") != "windows-prerequisite-preflight-v1"
        or not isinstance(raw.get("can_start_broker"), bool)
        or not isinstance(raw.get("checks"), list)
    ):
        return _preflight_protocol_failure("the Windows prerequisite report is incomplete")
    can_start = raw["can_start_broker"]
    expected_status = "ok" if can_start else "error"
    expected_returncode = 0 if can_start else 2
    if raw.get("status") != expected_status or result.returncode != expected_returncode:
        return _preflight_protocol_failure(
            "the Windows prerequisite process and report status disagree"
        )
    return cast(WindowsPreflightReportDict, raw)


def _write_report(report: WindowsPreflightReportDict, *, standard_output: bool) -> None:
    stream = sys.stdout if standard_output else sys.stderr
    stream.write(json.dumps(report, sort_keys=True) + "\n")


def run(argv: list[str] | None = None, *, platform_name: str = sys.platform) -> int:
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
    parser.add_argument(
        "--workspace",
        default=None,
        help="workspace used by the native Windows prerequisite preflight",
    )
    parser.add_argument(
        "--windows-preflight-only",
        action="store_true",
        help="emit the native Windows prerequisite report without starting the broker",
    )
    args = parser.parse_args(argv)

    runtime = _discover_runtime(platform_name)
    if runtime is None:
        if platform_name == "win32":
            report = missing_python_runtime_report().as_dict()
            _write_report(report, standard_output=args.windows_preflight_only)
            return 2
        sys.stderr.write(
            "error: uv is not installed and Python 3.12 or newer was not found on PATH\n"
            "install uv (https://docs.astral.sh/uv/) or Python 3.12+\n"
        )
        return 1

    if platform_name == "win32":
        if not args.workspace:
            report = _preflight_protocol_failure(
                "a workspace path is required for the Windows NTFS prerequisite check"
            )
            _write_report(report, standard_output=args.windows_preflight_only)
            return 2
        report = _run_windows_preflight(runtime, args.workspace)
        _write_report(report, standard_output=args.windows_preflight_only)
        if not report["can_start_broker"] or args.windows_preflight_only:
            return 0 if report["can_start_broker"] else 2
    elif args.windows_preflight_only:
        parser.error("--windows-preflight-only is available only on native Windows")

    broker_args = [str(SESSION_SCRIPT)]
    if args.data_dir:
        broker_args.extend(["--data-dir", args.data_dir])
    broker_args.extend(["--agent-runtime", args.agent_runtime])
    if platform_name == "win32":
        broker_args.extend(["--preflight-workspace", args.workspace])

    command = [*runtime.command, "-I", "-B", *broker_args]

    try:
        return_code = subprocess.call(command)
    except OSError:
        sys.stderr.write("error: failed to start the GoodJob session broker\n")
        return 1
    return return_code


def main() -> None:
    raise SystemExit(run())


if __name__ == "__main__":
    main()

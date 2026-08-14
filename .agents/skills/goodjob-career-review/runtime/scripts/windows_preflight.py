#!/usr/bin/env python3
"""Emit the native Windows prerequisite report before the broker can start."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

RUNTIME_DIR = Path(__file__).resolve().parents[1]
TRUSTED_SOURCE_DIR = RUNTIME_DIR / "src"
sys.path.insert(0, str(TRUSTED_SOURCE_DIR))

from goodjob.platform.detect import NATIVE_WINDOWS_RELEASE_ENABLED  # noqa: E402
from goodjob.platform.preflight_windows import (  # noqa: E402
    SystemWindowsPrerequisiteProbes,
    evaluate_windows_preflight,
)


def run(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check native Windows GoodJob prerequisites")
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--launcher-kind", required=True)
    args = parser.parse_args(argv)
    report = evaluate_windows_preflight(
        workspace=Path(args.workspace).expanduser().resolve(strict=False),
        runtime_dir=RUNTIME_DIR,
        python_version=(sys.version_info.major, sys.version_info.minor, sys.version_info.micro),
        launcher_kind=args.launcher_kind,
        uv_available=shutil.which("uv") is not None,
        release_enabled=NATIVE_WINDOWS_RELEASE_ENABLED,
        probes=SystemWindowsPrerequisiteProbes(),
    ).as_dict()
    sys.stdout.write(json.dumps(report, sort_keys=True) + "\n")
    return 0 if report["can_start_broker"] else 2


def main() -> None:
    raise SystemExit(run())


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Create a volatile capability and pass it to core children over inherited FDs."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, cast

from goodjob.auth import generate_capability

NOTICE_VERSION = "goodjob-source-analysis-v1"


def _scope(workspace: str) -> str:
    return json.dumps(
        {
            "workspace_path": str(Path(workspace).expanduser().resolve(strict=False)),
            "allowed_categories": ["source_analysis"],
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _run_with_capability(command: list[str], capability: bytes) -> subprocess.CompletedProcess[str]:
    read_fd, write_fd = os.pipe()
    try:
        process = subprocess.Popen(
            [*command, "--capability-fd", str(read_fd)],
            close_fds=True,
            pass_fds=(read_fd,),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    finally:
        os.close(read_fd)
    try:
        os.write(write_fd, capability)
    finally:
        os.close(write_fd)
    stdout, stderr = process.communicate()
    return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)


def _command_base(data_dir: str | None) -> list[str]:
    command = [sys.executable, "-m", "goodjob"]
    if data_dir:
        command.extend(["--data-dir", data_dir])
    return command


def _parse_json_output(result: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    if result.returncode != 0:
        sys.stderr.write(result.stderr)
        raise SystemExit(result.returncode)
    payload = json.loads(result.stdout)
    if not isinstance(payload, dict):
        raise SystemExit("GoodJob child returned a non-object JSON response")
    return cast(dict[str, Any], payload)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir")
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--notice-version", default=NOTICE_VERSION)
    parser.add_argument(
        "--verify",
        action="store_true",
        help="verify the new receipt in a second child before ending the session",
    )
    args = parser.parse_args()

    capability = generate_capability()
    scope = _scope(args.workspace)
    issued = _parse_json_output(
        _run_with_capability(
            [
                *_command_base(args.data_dir),
                "authorize",
                "--receipt-kind",
                "source_analysis",
                "--scope-json",
                scope,
                "--notice-version",
                args.notice_version,
                "--confirmed",
            ],
            capability,
        )
    )
    result: dict[str, Any] = {"status": "ok", "receipt": issued["receipt"]}
    if args.verify:
        receipt_id = str(issued["receipt"]["authorization_receipt_id"])
        verified = _parse_json_output(
            _run_with_capability(
                [
                    *_command_base(args.data_dir),
                    "verify-authorization",
                    "--authorization-receipt-id",
                    receipt_id,
                    "--receipt-kind",
                    "source_analysis",
                    "--scope-json",
                    scope,
                    "--notice-version",
                    args.notice_version,
                ],
                capability,
            )
        )
        result["verified"] = verified["status"] == "ok"
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()

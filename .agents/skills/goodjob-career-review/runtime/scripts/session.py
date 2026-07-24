#!/usr/bin/env python3
"""A task-scoped, stdin-bound broker for protected GoodJob core commands."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, cast

from goodjob.auth import generate_capability
from goodjob.errors import GoodJobError, InvalidInputError

NOTICE_VERSION = "goodjob-source-analysis-v1"


def _json_object(raw: str) -> dict[str, Any]:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise InvalidInputError("session broker input must be valid JSON") from exc
    if not isinstance(payload, dict):
        raise InvalidInputError("session broker input must be a JSON object")
    return cast(dict[str, Any], payload)


def _required_text(message: dict[str, Any], key: str) -> str:
    value = message.get(key)
    if not isinstance(value, str) or not value.strip():
        raise InvalidInputError(f"session broker field {key!r} must be a non-empty string")
    return value


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


def _child_response(result: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    raw_output = result.stdout if result.returncode == 0 else result.stderr
    try:
        return _json_object(raw_output)
    except InvalidInputError:
        return {
            "status": "error",
            "code": "core_protocol_error",
            "message": "GoodJob core returned an invalid JSON response",
        }


class SessionBroker:
    """Hold one raw capability only until the parent task closes standard input."""

    def __init__(self, data_dir: str | None) -> None:
        self._capability = generate_capability()
        self._data_dir = data_dir

    def dispatch(self, message: dict[str, Any]) -> dict[str, Any]:
        operation = _required_text(message, "op")
        if operation == "authorize_source_analysis":
            return self._authorize_source_analysis(message)
        if operation == "verify_source_analysis":
            return self._verify_source_analysis(message)
        raise InvalidInputError(f"unsupported session broker operation: {operation}")

    def _authorize_source_analysis(self, message: dict[str, Any]) -> dict[str, Any]:
        if message.get("confirmed") is not True:
            raise InvalidInputError("owner confirmation is required before source authorization")
        workspace = _required_text(message, "workspace")
        notice_version = str(message.get("notice_version", NOTICE_VERSION))
        return self._run_protected_child(
            [
                "authorize",
                "--receipt-kind",
                "source_analysis",
                "--scope-json",
                _scope(workspace),
                "--notice-version",
                notice_version,
                "--confirmed",
            ]
        )

    def _verify_source_analysis(self, message: dict[str, Any]) -> dict[str, Any]:
        workspace = _required_text(message, "workspace")
        receipt_id = _required_text(message, "authorization_receipt_id")
        notice_version = str(message.get("notice_version", NOTICE_VERSION))
        return self._run_protected_child(
            [
                "verify-authorization",
                "--authorization-receipt-id",
                receipt_id,
                "--receipt-kind",
                "source_analysis",
                "--scope-json",
                _scope(workspace),
                "--notice-version",
                notice_version,
            ]
        )

    def _run_protected_child(self, arguments: list[str]) -> dict[str, Any]:
        read_fd, write_fd = os.pipe()
        command = [sys.executable, "-m", "goodjob"]
        if self._data_dir:
            command.extend(["--data-dir", self._data_dir])
        try:
            process = subprocess.Popen(
                [*command, *arguments, "--capability-fd", str(read_fd)],
                close_fds=True,
                pass_fds=(read_fd,),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
        finally:
            os.close(read_fd)
        try:
            os.write(write_fd, self._capability)
        finally:
            os.close(write_fd)
        stdout, stderr = process.communicate()
        return _child_response(
            subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
        )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir")
    args = parser.parse_args()
    broker = SessionBroker(args.data_dir)
    for line in sys.stdin:
        try:
            response = broker.dispatch(_json_object(line))
        except GoodJobError as exc:
            response = {"status": "error", "code": exc.code, "message": str(exc)}
        print(json.dumps(response, ensure_ascii=False, sort_keys=True), flush=True)


if __name__ == "__main__":
    main()

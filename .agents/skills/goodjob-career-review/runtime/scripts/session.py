#!/usr/bin/env python3
"""A task-scoped, stdin-bound broker for protected GoodJob core commands."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TypeGuard, cast

from goodjob.auth import ReceiptKind, generate_capability
from goodjob.errors import GoodJobError, InvalidInputError

NOTICE_VERSION = "goodjob-source-analysis-v1"
type JsonValue = None | bool | int | float | str | list[JsonValue] | dict[str, JsonValue]
type JsonObject = dict[str, JsonValue]


def _is_json_value(value: object) -> TypeGuard[JsonValue]:
    if value is None or isinstance(value, bool | int | float | str):
        return True
    if isinstance(value, list):
        return all(_is_json_value(item) for item in value)
    if isinstance(value, dict):
        return all(isinstance(key, str) and _is_json_value(item) for key, item in value.items())
    return False


def _json_object(raw: str) -> JsonObject:
    try:
        payload: object = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise InvalidInputError("session broker input must be valid JSON") from exc
    if not isinstance(payload, dict) or not _is_json_value(payload):
        raise InvalidInputError("session broker input must be a JSON object")
    return cast(JsonObject, payload)


def _required_text(message: JsonObject, key: str) -> str:
    value = message.get(key)
    if not isinstance(value, str) or not value.strip():
        raise InvalidInputError(f"session broker field {key!r} must be a non-empty string")
    return _require_utf8(value, key)


def _optional_text(message: JsonObject, key: str, default: str) -> str:
    value = message.get(key, default)
    if not isinstance(value, str) or not value.strip():
        raise InvalidInputError(f"session broker field {key!r} must be a non-empty string")
    return _require_utf8(value, key)


def _require_utf8(value: str, field_name: str) -> str:
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise InvalidInputError(
            f"session broker field {field_name!r} must contain valid UTF-8 text"
        ) from exc
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


@dataclass(frozen=True)
class CoreResponse:
    """The validated JSON envelope returned by a short-lived core child."""

    payload: JsonObject

    @property
    def status(self) -> str:
        return _required_text(self.payload, "status")

    @classmethod
    def from_process(cls, result: subprocess.CompletedProcess[str]) -> CoreResponse:
        raw_output = result.stdout if result.returncode == 0 else result.stderr
        try:
            payload = _json_object(raw_output)
            status = _required_text(payload, "status")
        except InvalidInputError:
            return cls(
                {
                    "status": "error",
                    "code": "core_protocol_error",
                    "message": "GoodJob core returned an invalid JSON response",
                }
            )
        if result.returncode == 0 and status != "ok":
            return cls(
                {
                    "status": "error",
                    "code": "core_protocol_error",
                    "message": "GoodJob core returned an unexpected successful response",
                }
            )
        if result.returncode != 0 and status != "error":
            return cls(
                {
                    "status": "error",
                    "code": "core_protocol_error",
                    "message": "GoodJob core returned an unexpected error response",
                }
            )
        return cls(payload)

    def require_receipt(self) -> CoreResponse:
        if self.status != "ok":
            return self
        try:
            ReceiptEnvelope.from_response(self)
        except InvalidInputError:
            return _protocol_error("GoodJob core returned an invalid authorization receipt")
        return self


@dataclass(frozen=True)
class ReceiptEnvelope:
    """The complete stable receipt response required by the session protocol."""

    authorization_receipt_id: str
    receipt_kind: ReceiptKind
    scope_descriptor: str
    notice_version: str
    confirmed_at: str

    @classmethod
    def from_response(cls, response: CoreResponse) -> ReceiptEnvelope:
        receipt = response.payload.get("receipt")
        if not isinstance(receipt, dict) or not _is_json_value(receipt):
            raise InvalidInputError("authorization receipt is missing")
        payload = cast(JsonObject, receipt)
        required = {
            field: _required_text(payload, field)
            for field in (
                "authorization_receipt_id",
                "receipt_kind",
                "scope_descriptor",
                "notice_version",
                "confirmed_at",
            )
        }
        try:
            kind = ReceiptKind(required["receipt_kind"])
        except ValueError as exc:
            raise InvalidInputError("authorization receipt kind is invalid") from exc
        return cls(
            authorization_receipt_id=required["authorization_receipt_id"],
            receipt_kind=kind,
            scope_descriptor=required["scope_descriptor"],
            notice_version=required["notice_version"],
            confirmed_at=required["confirmed_at"],
        )


def _protocol_error(message: str) -> CoreResponse:
    return CoreResponse({"status": "error", "code": "core_protocol_error", "message": message})


class SessionBroker:
    """Hold one raw capability only until the parent task closes standard input."""

    def __init__(self, data_dir: str | None) -> None:
        self._capability = generate_capability()
        self._data_dir = data_dir

    def dispatch(self, message: JsonObject) -> JsonObject:
        operation = _required_text(message, "op")
        if operation == "authorize_source_analysis":
            return self._authorize_source_analysis(message).payload
        if operation == "verify_source_analysis":
            return self._verify_source_analysis(message).payload
        raise InvalidInputError(f"unsupported session broker operation: {operation}")

    def _authorize_source_analysis(self, message: JsonObject) -> CoreResponse:
        if message.get("confirmed") is not True:
            raise InvalidInputError("owner confirmation is required before source authorization")
        workspace = _required_text(message, "workspace")
        notice_version = _optional_text(message, "notice_version", NOTICE_VERSION)
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
        ).require_receipt()

    def _verify_source_analysis(self, message: JsonObject) -> CoreResponse:
        workspace = _required_text(message, "workspace")
        receipt_id = _required_text(message, "authorization_receipt_id")
        notice_version = _optional_text(message, "notice_version", NOTICE_VERSION)
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
        ).require_receipt()

    def _run_protected_child(self, arguments: list[str]) -> CoreResponse:
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
        return CoreResponse.from_process(
            subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
        )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir")
    args = parser.parse_args()
    broker = SessionBroker(args.data_dir)
    for line in sys.stdin:
        response: JsonObject
        try:
            response = broker.dispatch(_json_object(line))
        except GoodJobError as exc:
            response = {"status": "error", "code": exc.code, "message": str(exc)}
        except (UnicodeError, OSError):
            response = {
                "status": "error",
                "code": "invalid_input",
                "message": "session broker input could not be processed safely",
            }
        print(json.dumps(response, ensure_ascii=False, sort_keys=True), flush=True)


if __name__ == "__main__":
    main()

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
RELATION_NOTICE_VERSION = "goodjob-external-git-relation-probe-v1"
METADATA_NOTICE_VERSION = "goodjob-external-git-metadata-v1"
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
    if "\x00" in value:
        raise InvalidInputError(f"session broker field {field_name!r} must not contain NUL")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise InvalidInputError(
            f"session broker field {field_name!r} must contain valid UTF-8 text"
        ) from exc
    return value


def _required_nonnegative_integer(message: JsonObject, key: str) -> int:
    value = message.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise InvalidInputError(f"session broker field {key!r} must be a non-negative integer")
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


def _workspace_path(workspace: str) -> Path:
    return Path(workspace).expanduser().resolve(strict=False)


def _workspace_child(workspace: str, raw_path: str, field_name: str) -> Path:
    root = _workspace_path(workspace)
    candidate = Path(raw_path).expanduser()
    if not candidate.is_absolute():
        candidate = root / candidate
    normalized = Path(os.path.normpath(str(candidate)))
    try:
        normalized.relative_to(root)
    except ValueError as exc:
        raise InvalidInputError(
            f"session broker field {field_name!r} must remain inside the authorized workspace"
        ) from exc
    return normalized


def _relation_scope(workspace: str, git_pointer: str, candidate: ExternalGitCandidate) -> str:
    pointer = _workspace_child(workspace, git_pointer, "git_pointer")
    if pointer.name != ".git":
        raise InvalidInputError("session broker field 'git_pointer' must name a .git file")
    return json.dumps(
        {
            "workspace_path": str(_workspace_path(workspace)),
            "git_pointer_path": str(pointer),
            "marker_kind": candidate.marker_kind,
            "git_dir_candidate": candidate.git_dir_candidate,
            "common_dir_candidate": candidate.common_dir_candidate,
            "allowed_categories": ["external_git_relation_probe"],
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _metadata_scope(workspace: str, git_pointer: str, relation: ExternalGitRelation) -> str:
    pointer = _workspace_child(workspace, git_pointer, "git_pointer")
    if pointer.name != ".git":
        raise InvalidInputError("session broker field 'git_pointer' must name a .git file")
    return json.dumps(
        {
            "workspace_path": str(_workspace_path(workspace)),
            "git_pointer_path": str(pointer),
            "marker_kind": relation.marker_kind,
            "git_dir": relation.git_dir,
            "common_dir": relation.common_dir,
            "git_dir_device": relation.git_dir_device,
            "git_dir_inode": relation.git_dir_inode,
            "common_dir_device": relation.common_dir_device,
            "common_dir_inode": relation.common_dir_inode,
            "allowed_categories": ["external_git_metadata"],
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


@dataclass(frozen=True)
class ExternalGitCandidate:
    workspace_path: str
    git_pointer_path: str
    marker_kind: str
    git_dir_candidate: str
    common_dir_candidate: str | None


@dataclass(frozen=True)
class ExternalGitRelation:
    workspace_path: str
    git_pointer_path: str
    marker_kind: str
    git_dir: str
    common_dir: str
    git_dir_device: int
    git_dir_inode: int
    common_dir_device: int
    common_dir_inode: int


def _receipt_arguments(receipt: ReceiptEnvelope) -> list[str]:
    return [
        "--authorization-receipt-id",
        receipt.authorization_receipt_id,
        "--receipt-kind",
        receipt.receipt_kind.value,
        "--scope-json",
        receipt.scope_descriptor,
        "--notice-version",
        receipt.notice_version,
    ]


def _protocol_error(message: str) -> CoreResponse:
    return CoreResponse({"status": "error", "code": "core_protocol_error", "message": message})


class SessionBroker:
    """Hold one raw capability only until the parent task closes standard input."""

    def __init__(self, data_dir: str | None) -> None:
        self._capability = generate_capability()
        self._data_dir = data_dir
        self._source_receipts: dict[str, ReceiptEnvelope] = {}
        self._relation_receipts: dict[str, ReceiptEnvelope] = {}
        self._metadata_receipts: dict[str, ReceiptEnvelope] = {}
        self._candidates: dict[tuple[str, str], ExternalGitCandidate] = {}
        self._relations: dict[tuple[str, str], ExternalGitRelation] = {}

    def dispatch(self, message: JsonObject) -> JsonObject:
        operation = _required_text(message, "op")
        if operation == "authorize_source_analysis":
            return self._authorize_source_analysis(message).payload
        if operation == "verify_source_analysis":
            return self._verify_source_analysis(message).payload
        if operation == "inspect_external_git_candidate":
            return self._inspect_external_git_candidate(message).payload
        if operation == "authorize_external_git_relation_probe":
            return self._authorize_external_git_relation_probe(message).payload
        if operation == "probe_external_git_relation":
            return self._probe_external_git_relation(message).payload
        if operation == "authorize_external_git_metadata":
            return self._authorize_external_git_metadata(message).payload
        if operation == "scan":
            return self._scan(message).payload
        if operation == "refresh":
            return self._refresh(message).payload
        raise InvalidInputError(f"unsupported session broker operation: {operation}")

    def _authorize_source_analysis(self, message: JsonObject) -> CoreResponse:
        if message.get("confirmed") is not True:
            raise InvalidInputError("owner confirmation is required before source authorization")
        workspace = _required_text(message, "workspace")
        notice_version = _optional_text(message, "notice_version", NOTICE_VERSION)
        response = self._run_protected_child(
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
        self._remember_receipt(response, ReceiptKind.SOURCE_ANALYSIS, self._source_receipts)
        return response

    def _verify_source_analysis(self, message: JsonObject) -> CoreResponse:
        workspace = _required_text(message, "workspace")
        receipt = self._source_receipt(message)
        expected_scope = _scope(workspace)
        if receipt.scope_descriptor != expected_scope:
            raise InvalidInputError(
                "source authorization receipt does not match the requested workspace"
            )
        return self._run_protected_child(
            ["verify-authorization", *_receipt_arguments(receipt)]
        ).require_receipt()

    def _authorize_external_git_relation_probe(self, message: JsonObject) -> CoreResponse:
        if message.get("confirmed") is not True:
            raise InvalidInputError("owner confirmation is required before relation authorization")
        workspace = _required_text(message, "workspace")
        pointer = _required_text(message, "git_pointer")
        canonical_workspace = str(_workspace_path(workspace))
        canonical_pointer = str(_workspace_child(workspace, pointer, "git_pointer"))
        candidate = self._candidates.get((canonical_workspace, canonical_pointer))
        if candidate is None:
            raise InvalidInputError(
                "inspect the root-internal Git candidate in this session before relation "
                "authorization"
            )
        notice_version = _optional_text(message, "notice_version", RELATION_NOTICE_VERSION)
        response = self._run_protected_child(
            [
                "authorize",
                "--receipt-kind",
                ReceiptKind.EXTERNAL_GIT_RELATION_PROBE.value,
                "--scope-json",
                _relation_scope(workspace, pointer, candidate),
                "--notice-version",
                notice_version,
                "--confirmed",
            ]
        ).require_receipt()
        self._remember_receipt(
            response, ReceiptKind.EXTERNAL_GIT_RELATION_PROBE, self._relation_receipts
        )
        return response

    def _inspect_external_git_candidate(self, message: JsonObject) -> CoreResponse:
        workspace = _required_text(message, "workspace")
        pointer = _required_text(message, "git_pointer")
        source = self._source_receipt(message)
        self._require_source_scope(source, workspace)
        canonical_pointer = str(_workspace_child(workspace, pointer, "git_pointer"))
        response = self._run_protected_child(
            [
                "inspect-external-git-candidate",
                "--workspace",
                workspace,
                "--git-pointer",
                canonical_pointer,
                *_receipt_arguments(source),
            ]
        )
        if response.status != "ok":
            return response
        candidate_value = response.payload.get("candidate")
        if not isinstance(candidate_value, dict) or not _is_json_value(candidate_value):
            return _protocol_error("GoodJob core returned an invalid external Git candidate")
        candidate_object = cast(JsonObject, candidate_value)
        common_value = candidate_object.get("common_dir_candidate")
        if common_value is not None and not isinstance(common_value, str):
            return _protocol_error("GoodJob core returned an invalid commondir candidate")
        try:
            marker_kind = _required_text(candidate_object, "marker_kind")
            if marker_kind not in {"file", "directory"}:
                raise InvalidInputError("external Git marker kind is invalid")
            candidate = ExternalGitCandidate(
                workspace_path=str(_workspace_path(workspace)),
                git_pointer_path=_required_text(candidate_object, "git_pointer_path"),
                marker_kind=marker_kind,
                git_dir_candidate=_required_text(candidate_object, "git_dir_candidate"),
                common_dir_candidate=(
                    _require_utf8(common_value, "common_dir_candidate")
                    if isinstance(common_value, str)
                    else None
                ),
            )
        except InvalidInputError:
            return _protocol_error("GoodJob core returned an incomplete external Git candidate")
        if candidate.git_pointer_path != canonical_pointer:
            return _protocol_error("GoodJob core returned a candidate for another Git marker")
        self._candidates[(candidate.workspace_path, candidate.git_pointer_path)] = candidate
        return response

    def _probe_external_git_relation(self, message: JsonObject) -> CoreResponse:
        workspace = _required_text(message, "workspace")
        pointer = _required_text(message, "git_pointer")
        source = self._source_receipt(message)
        relation_id = _required_text(message, "relation_authorization_receipt_id")
        relation = self._relation_receipts.get(relation_id)
        if relation is None:
            raise InvalidInputError(
                "external Git relation receipt is not registered in this session"
            )
        try:
            relation_scope = json.loads(relation.scope_descriptor)
        except json.JSONDecodeError:
            raise AssertionError("registered receipt scopes are canonical JSON") from None
        canonical_pointer = str(_workspace_child(workspace, pointer, "git_pointer"))
        if (
            not isinstance(relation_scope, dict)
            or relation_scope.get("workspace_path") != str(_workspace_path(workspace))
            or relation_scope.get("git_pointer_path") != canonical_pointer
        ):
            raise InvalidInputError(
                "external Git relation receipt does not match the requested pointer"
            )
        response = self._run_protected_child(
            [
                "probe-external-git-relation",
                "--workspace",
                workspace,
                "--git-pointer",
                str(_workspace_child(workspace, pointer, "git_pointer")),
                *_receipt_arguments(source),
                "--relation-authorization-receipt-id",
                relation.authorization_receipt_id,
                "--relation-scope-json",
                relation.scope_descriptor,
                "--relation-notice-version",
                relation.notice_version,
            ]
        )
        if response.status != "ok":
            return response
        relation_value = response.payload.get("relation")
        if not isinstance(relation_value, dict) or not _is_json_value(relation_value):
            return _protocol_error("GoodJob core returned an invalid external Git relation")
        relation_object = cast(JsonObject, relation_value)
        try:
            resolved = ExternalGitRelation(
                workspace_path=str(_workspace_path(workspace)),
                git_pointer_path=_required_text(relation_object, "git_pointer_path"),
                marker_kind=_required_text(relation_object, "marker_kind"),
                git_dir=_required_text(relation_object, "git_dir"),
                common_dir=_required_text(relation_object, "common_dir"),
                git_dir_device=_required_nonnegative_integer(relation_object, "git_dir_device"),
                git_dir_inode=_required_nonnegative_integer(relation_object, "git_dir_inode"),
                common_dir_device=_required_nonnegative_integer(
                    relation_object, "common_dir_device"
                ),
                common_dir_inode=_required_nonnegative_integer(relation_object, "common_dir_inode"),
            )
        except InvalidInputError:
            return _protocol_error("GoodJob core returned an incomplete external Git relation")
        if resolved.git_pointer_path != str(_workspace_child(workspace, pointer, "git_pointer")):
            return _protocol_error("GoodJob core returned a relation for a different Git pointer")
        if resolved.marker_kind not in {"file", "directory"}:
            return _protocol_error("GoodJob core returned an invalid Git marker kind")
        self._relations[(resolved.workspace_path, resolved.git_pointer_path)] = resolved
        return response

    def _authorize_external_git_metadata(self, message: JsonObject) -> CoreResponse:
        if message.get("confirmed") is not True:
            raise InvalidInputError("owner confirmation is required before metadata authorization")
        workspace = _required_text(message, "workspace")
        pointer = _required_text(message, "git_pointer")
        git_dir = _required_text(message, "git_dir")
        common_dir = _required_text(message, "common_dir")
        canonical_pointer = str(_workspace_child(workspace, pointer, "git_pointer"))
        relation = self._relations.get((str(_workspace_path(workspace)), canonical_pointer))
        if relation is None:
            raise InvalidInputError(
                "complete an external Git relation probe in this session before "
                "metadata authorization"
            )
        if (
            str(Path(git_dir).expanduser().resolve(strict=False)) != relation.git_dir
            or str(Path(common_dir).expanduser().resolve(strict=False)) != relation.common_dir
        ):
            raise InvalidInputError(
                "external Git metadata paths do not match the completed relation probe"
            )
        notice_version = _optional_text(message, "notice_version", METADATA_NOTICE_VERSION)
        response = self._run_protected_child(
            [
                "authorize",
                "--receipt-kind",
                ReceiptKind.EXTERNAL_GIT_METADATA.value,
                "--scope-json",
                _metadata_scope(workspace, pointer, relation),
                "--notice-version",
                notice_version,
                "--confirmed",
            ]
        ).require_receipt()
        self._remember_receipt(response, ReceiptKind.EXTERNAL_GIT_METADATA, self._metadata_receipts)
        return response

    def _scan(self, message: JsonObject) -> CoreResponse:
        workspace = _required_text(message, "workspace")
        source = self._source_receipt(message)
        self._require_source_scope(source, workspace)
        config_revision = _optional_text(message, "config_revision", "goodjob-scan-config-v1")
        return self._run_protected_child(
            [
                "scan",
                "--workspace",
                workspace,
                "--config-revision",
                config_revision,
                *_receipt_arguments(source),
                "--external-git-metadata-grants-json",
                self._metadata_grants_json(message),
            ]
        )

    def _refresh(self, message: JsonObject) -> CoreResponse:
        workspace = _required_text(message, "workspace")
        workspace_id = _required_text(message, "workspace_id")
        source = self._source_receipt(message)
        self._require_source_scope(source, workspace)
        config_revision = _optional_text(message, "config_revision", "goodjob-scan-config-v1")
        mode = _optional_text(message, "change_detection_mode", "fast")
        if mode not in {"fast", "verify_content"}:
            raise InvalidInputError("change_detection_mode must be fast or verify_content")
        return self._run_protected_child(
            [
                "refresh",
                "--workspace-id",
                workspace_id,
                "--config-revision",
                config_revision,
                "--change-detection-mode",
                mode,
                *_receipt_arguments(source),
                "--external-git-metadata-grants-json",
                self._metadata_grants_json(message),
            ]
        )

    def _source_receipt(self, message: JsonObject) -> ReceiptEnvelope:
        receipt_id = _required_text(message, "authorization_receipt_id")
        receipt = self._source_receipts.get(receipt_id)
        if receipt is None:
            raise InvalidInputError(
                "source authorization receipt is not registered in this session"
            )
        return receipt

    @staticmethod
    def _require_source_scope(receipt: ReceiptEnvelope, workspace: str) -> None:
        if receipt.scope_descriptor != _scope(workspace):
            raise InvalidInputError(
                "source authorization receipt does not match the requested workspace"
            )

    @staticmethod
    def _remember_receipt(
        response: CoreResponse,
        expected_kind: ReceiptKind,
        destination: dict[str, ReceiptEnvelope],
    ) -> None:
        if response.status != "ok":
            return
        try:
            receipt = ReceiptEnvelope.from_response(response)
        except InvalidInputError:
            return
        if receipt.receipt_kind == expected_kind:
            destination[receipt.authorization_receipt_id] = receipt

    def _metadata_grants_json(self, message: JsonObject) -> str:
        raw_ids = message.get("external_git_metadata_receipt_ids", [])
        if not isinstance(raw_ids, list) or len(raw_ids) > 64:
            raise InvalidInputError("external_git_metadata_receipt_ids must be a bounded JSON list")
        grants: list[dict[str, object]] = []
        seen: set[str] = set()
        for value in raw_ids:
            if not isinstance(value, str):
                raise InvalidInputError("external Git metadata receipt IDs must be strings")
            receipt_id = _require_utf8(value, "external_git_metadata_receipt_ids")
            if receipt_id in seen:
                raise InvalidInputError("external Git metadata receipt IDs must not repeat")
            seen.add(receipt_id)
            receipt = self._metadata_receipts.get(receipt_id)
            if receipt is None:
                raise InvalidInputError(
                    "external Git metadata receipt is not registered in this session"
                )
            try:
                scope = json.loads(receipt.scope_descriptor)
            except json.JSONDecodeError:
                raise AssertionError("registered receipt scopes are canonical JSON") from None
            grants.append(
                {
                    "authorization_receipt_id": receipt.authorization_receipt_id,
                    "scope": scope,
                    "notice_version": receipt.notice_version,
                }
            )
        return json.dumps(grants, ensure_ascii=False, separators=(",", ":"), sort_keys=True)

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

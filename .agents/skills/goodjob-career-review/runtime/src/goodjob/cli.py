"""JSON-only command boundary for the GoodJob local runtime."""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from goodjob.auth import (
    AuthorizationRepository,
    AuthorizationRequest,
    ReceiptKind,
    decode_scope,
    read_capability_from_fd,
    receipt_kind_values,
)
from goodjob.db import Database
from goodjob.errors import GoodJobError, InvalidInputError
from goodjob.paths import DataPaths
from goodjob.scanner import (
    ExternalGitGrant,
    WorkspaceScanner,
    inspect_external_git_candidate,
    probe_external_git_relation,
)


def _write_json(stream: Any, payload: dict[str, Any]) -> None:
    stream.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")


def _data_usage(path: Path) -> int:
    if not path.exists():
        return 0
    if path.is_file():
        return path.stat().st_size
    return sum(entry.stat().st_size for entry in path.rglob("*") if entry.is_file())


def _add_authorization_arguments(
    parser: argparse.ArgumentParser, *, needs_receipt_id: bool, needs_confirmation: bool
) -> None:
    if needs_receipt_id:
        parser.add_argument("--authorization-receipt-id", required=True)
    parser.add_argument("--receipt-kind", choices=receipt_kind_values(), required=True)
    parser.add_argument("--scope-json", required=True)
    parser.add_argument("--notice-version", required=True)
    parser.add_argument("--capability-fd", type=int, required=True)
    if needs_confirmation:
        parser.add_argument(
            "--confirmed",
            action="store_true",
            help="record that the Skill already showed and received required owner confirmation",
        )


def _authorization_request(args: argparse.Namespace) -> AuthorizationRequest:
    return AuthorizationRequest.from_values(
        receipt_kind=args.receipt_kind,
        scope=decode_scope(args.scope_json),
        notice_version=args.notice_version,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="goodjob")
    parser.add_argument(
        "--data-dir",
        help="owner-local state directory; defaults to ~/.codex/goodjob-career-review",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("bootstrap", help="create the owner-local layout and apply migrations")
    subparsers.add_parser(
        "data-status", help="show owner-local storage usage without project contents"
    )

    authorize = subparsers.add_parser(
        "authorize", help="record a session-bound authorization receipt"
    )
    _add_authorization_arguments(authorize, needs_receipt_id=False, needs_confirmation=True)

    verify = subparsers.add_parser(
        "verify-authorization", help="verify a protected request's current session binding"
    )
    _add_authorization_arguments(verify, needs_receipt_id=True, needs_confirmation=False)

    scan = subparsers.add_parser("scan", help="create a full immutable workspace scan snapshot")
    scan.add_argument("--workspace", required=True)
    scan.add_argument("--config-revision", required=True)
    scan.add_argument("--external-git-metadata-grants-json", default="[]")
    _add_authorization_arguments(scan, needs_receipt_id=True, needs_confirmation=False)

    refresh = subparsers.add_parser("refresh", help="create an explicit incremental scan snapshot")
    refresh.add_argument("--workspace-id", required=True)
    refresh.add_argument("--config-revision", required=True)
    refresh.add_argument(
        "--change-detection-mode", choices=("fast", "verify_content"), required=True
    )
    refresh.add_argument("--external-git-metadata-grants-json", default="[]")
    _add_authorization_arguments(refresh, needs_receipt_id=True, needs_confirmation=False)

    candidate_inspection = subparsers.add_parser(
        "inspect-external-git-candidate",
        help="inspect only a root-internal .git marker before relation authorization",
    )
    candidate_inspection.add_argument("--workspace", required=True)
    candidate_inspection.add_argument("--git-pointer", required=True)
    _add_authorization_arguments(
        candidate_inspection, needs_receipt_id=True, needs_confirmation=False
    )

    relation_probe = subparsers.add_parser(
        "probe-external-git-relation",
        help="resolve one explicitly authorized linked-worktree relation without invoking Git",
    )
    relation_probe.add_argument("--workspace", required=True)
    relation_probe.add_argument("--git-pointer", required=True)
    _add_authorization_arguments(relation_probe, needs_receipt_id=True, needs_confirmation=False)
    relation_probe.add_argument("--relation-authorization-receipt-id", required=True)
    relation_probe.add_argument("--relation-scope-json", required=True)
    relation_probe.add_argument("--relation-notice-version", required=True)
    return parser


def _handle_bootstrap(paths: DataPaths) -> dict[str, Any]:
    schema_version = Database(paths).migrate()
    return {
        "status": "ok",
        "schema_version": schema_version,
        "data_dir": str(paths.root),
    }


def _handle_data_status(paths: DataPaths) -> dict[str, Any]:
    database = Database(paths)
    database.migrate()
    with database.read_connection() as connection:
        snapshot_count = int(
            connection.execute("SELECT COUNT(*) AS count FROM project_snapshots").fetchone()[
                "count"
            ]
        )
    return {
        "status": "ok",
        "data_dir": str(paths.root),
        "usage_bytes": {
            "sqlite": _data_usage(paths.database_file),
            "artifacts": _data_usage(paths.artifacts_dir),
            "exports": _data_usage(paths.exports_dir),
            "drafts": _data_usage(paths.drafts_dir),
        },
        "snapshot_count": snapshot_count,
    }


def _handle_authorize(args: argparse.Namespace, paths: DataPaths) -> dict[str, Any]:
    if not args.confirmed:
        raise InvalidInputError(
            "owner confirmation is required before recording an authorization receipt"
        )
    capability = read_capability_from_fd(args.capability_fd)
    receipt = AuthorizationRepository(Database(paths)).issue(
        capability=capability,
        request=_authorization_request(args),
    )
    return {"status": "ok", "receipt": receipt.as_json()}


def _handle_verify(args: argparse.Namespace, paths: DataPaths) -> dict[str, Any]:
    capability = read_capability_from_fd(args.capability_fd)
    receipt = AuthorizationRepository(Database(paths)).require_valid(
        authorization_receipt_id=args.authorization_receipt_id,
        capability=capability,
        request=_authorization_request(args),
    )
    return {"status": "ok", "receipt": receipt.as_json()}


def _scope_workspace(request: AuthorizationRequest) -> Path:
    try:
        scope = json.loads(request.scope_descriptor)
    except json.JSONDecodeError as exc:
        raise InvalidInputError("source-analysis scope must be a JSON object") from exc
    if not isinstance(scope, dict):
        raise InvalidInputError("source-analysis scope must be a JSON object")
    workspace = scope.get("workspace_path")
    categories = scope.get("allowed_categories")
    if (
        not isinstance(workspace, str)
        or not isinstance(categories, list)
        or categories != ["source_analysis"]
    ):
        raise InvalidInputError("source-analysis scope is missing its authorized workspace")
    return Path(workspace).expanduser().resolve(strict=False)


def _verify_scan_authorization(
    args: argparse.Namespace, paths: DataPaths, expected_workspace: Path
) -> bytes:
    capability = read_capability_from_fd(args.capability_fd)
    request = _authorization_request(args)
    if request.receipt_kind.value != "source_analysis":
        raise InvalidInputError("scan and refresh require a source_analysis authorization receipt")
    if _scope_workspace(request) != expected_workspace.resolve(strict=False):
        raise InvalidInputError("authorization scope does not match the requested workspace")
    AuthorizationRepository(Database(paths)).require_valid(
        authorization_receipt_id=args.authorization_receipt_id,
        capability=capability,
        request=request,
    )
    return capability


def _scope_object(request: AuthorizationRequest, label: str) -> dict[str, object]:
    try:
        scope = json.loads(request.scope_descriptor)
    except json.JSONDecodeError as exc:
        raise InvalidInputError(f"{label} scope must be a JSON object") from exc
    if not isinstance(scope, dict):
        raise InvalidInputError(f"{label} scope must be a JSON object")
    return scope


def _normal_workspace_child(workspace: Path, raw_path: str, label: str) -> Path:
    raw = Path(raw_path).expanduser()
    candidate = raw if raw.is_absolute() else workspace / raw
    canonical = Path(os.path.normpath(str(candidate)))
    if not _is_within_path(canonical, workspace):
        raise InvalidInputError(f"{label} must remain inside the authorized workspace")
    return canonical


def _is_within_path(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _metadata_grants(
    raw_grants: str,
    *,
    paths: DataPaths,
    capability: bytes,
    workspace: Path,
) -> tuple[ExternalGitGrant, ...]:
    try:
        values = json.loads(raw_grants)
    except json.JSONDecodeError as exc:
        raise InvalidInputError("external_git_metadata_grants_json must be valid JSON") from exc
    if not isinstance(values, list) or len(values) > 64:
        raise InvalidInputError("external_git_metadata_grants_json must be a bounded JSON list")
    repository = AuthorizationRepository(Database(paths))
    grants: list[ExternalGitGrant] = []
    seen_pointers: set[Path] = set()
    for value in values:
        if not isinstance(value, dict):
            raise InvalidInputError("external Git metadata grant must be a JSON object")
        receipt_id = value.get("authorization_receipt_id")
        scope = value.get("scope")
        notice_version = value.get("notice_version")
        if not isinstance(receipt_id, str) or not isinstance(notice_version, str):
            raise InvalidInputError("external Git metadata grant is incomplete")
        request = AuthorizationRequest.from_values(
            receipt_kind=ReceiptKind.EXTERNAL_GIT_METADATA.value,
            scope=scope,
            notice_version=notice_version,
        )
        scope_object = _scope_object(request, "external Git metadata")
        allowed = scope_object.get("allowed_categories")
        scope_workspace = scope_object.get("workspace_path")
        pointer_text = scope_object.get("git_pointer_path")
        git_dir_text = scope_object.get("git_dir")
        common_dir_text = scope_object.get("common_dir")
        marker_kind = scope_object.get("marker_kind")
        git_dir_device = scope_object.get("git_dir_device")
        git_dir_inode = scope_object.get("git_dir_inode")
        common_dir_device = scope_object.get("common_dir_device")
        common_dir_inode = scope_object.get("common_dir_inode")
        if (
            allowed != ["external_git_metadata"]
            or not isinstance(scope_workspace, str)
            or Path(scope_workspace).expanduser().resolve(strict=False) != workspace
            or not isinstance(pointer_text, str)
            or not isinstance(git_dir_text, str)
            or not isinstance(common_dir_text, str)
            or marker_kind not in {"file", "directory"}
            or not isinstance(git_dir_device, int)
            or isinstance(git_dir_device, bool)
            or not isinstance(git_dir_inode, int)
            or isinstance(git_dir_inode, bool)
            or not isinstance(common_dir_device, int)
            or isinstance(common_dir_device, bool)
            or not isinstance(common_dir_inode, int)
            or isinstance(common_dir_inode, bool)
            or min(git_dir_device, git_dir_inode, common_dir_device, common_dir_inode) < 0
        ):
            raise InvalidInputError(
                "external Git metadata grant scope is not valid for this workspace"
            )
        receipt = repository.require_valid(
            authorization_receipt_id=receipt_id,
            capability=capability,
            request=request,
        )
        pointer = _normal_workspace_child(workspace, pointer_text, "git_pointer_path")
        if pointer.name != ".git":
            raise InvalidInputError("git_pointer_path must name a .git file")
        git_dir = Path(git_dir_text).expanduser().resolve(strict=False)
        common_dir = Path(common_dir_text).expanduser().resolve(strict=False)
        if pointer in seen_pointers:
            raise InvalidInputError("external Git metadata grant repeats a .git pointer")
        seen_pointers.add(pointer)
        grants.append(
            ExternalGitGrant(
                pointer,
                marker_kind,
                git_dir,
                common_dir,
                git_dir_device,
                git_dir_inode,
                common_dir_device,
                common_dir_inode,
                receipt.authorization_receipt_id,
                receipt.confirmed_at,
            )
        )
    return tuple(grants)


def _handle_scan(args: argparse.Namespace, paths: DataPaths) -> dict[str, Any]:
    workspace = Path(args.workspace).expanduser().resolve(strict=False)
    capability = _verify_scan_authorization(args, paths, workspace)
    grants = _metadata_grants(
        args.external_git_metadata_grants_json,
        paths=paths,
        capability=capability,
        workspace=workspace,
    )
    return (
        WorkspaceScanner(Database(paths))
        .scan(
            workspace_path=str(workspace),
            config_revision=args.config_revision,
            authorization_receipt_id=args.authorization_receipt_id,
            external_git_grants=grants,
        )
        .as_json()
    )


def _handle_refresh(args: argparse.Namespace, paths: DataPaths) -> dict[str, Any]:
    database = Database(paths)
    with database.read_connection() as connection:
        row = connection.execute(
            "SELECT canonical_root FROM workspaces WHERE workspace_id = ?", (args.workspace_id,)
        ).fetchone()
    if row is None:
        raise InvalidInputError("workspace_id is not registered")
    workspace = Path(str(row["canonical_root"])).resolve(strict=False)
    capability = _verify_scan_authorization(args, paths, workspace)
    grants = _metadata_grants(
        args.external_git_metadata_grants_json,
        paths=paths,
        capability=capability,
        workspace=workspace,
    )
    return (
        WorkspaceScanner(database)
        .refresh(
            workspace_id=args.workspace_id,
            config_revision=args.config_revision,
            change_detection_mode=args.change_detection_mode,
            authorization_receipt_id=args.authorization_receipt_id,
            external_git_grants=grants,
        )
        .as_json()
    )


def _handle_relation_probe(args: argparse.Namespace, paths: DataPaths) -> dict[str, Any]:
    workspace = Path(args.workspace).expanduser().resolve(strict=False)
    capability = _verify_scan_authorization(args, paths, workspace)
    relation_request = AuthorizationRequest.from_values(
        receipt_kind=ReceiptKind.EXTERNAL_GIT_RELATION_PROBE.value,
        scope=decode_scope(args.relation_scope_json),
        notice_version=args.relation_notice_version,
    )
    scope = _scope_object(relation_request, "external Git relation")
    pointer = _normal_workspace_child(workspace, args.git_pointer, "git_pointer")
    marker_kind = scope.get("marker_kind")
    git_dir_candidate_text = scope.get("git_dir_candidate")
    common_dir_candidate_text = scope.get("common_dir_candidate")
    if (
        scope.get("allowed_categories") != ["external_git_relation_probe"]
        or scope.get("workspace_path") != str(workspace)
        or scope.get("git_pointer_path") != str(pointer)
        or marker_kind not in {"file", "directory"}
        or not isinstance(git_dir_candidate_text, str)
        or not (common_dir_candidate_text is None or isinstance(common_dir_candidate_text, str))
    ):
        raise InvalidInputError("external Git relation scope does not match the requested pointer")
    git_dir_candidate = Path(git_dir_candidate_text)
    common_dir_candidate = (
        Path(common_dir_candidate_text) if isinstance(common_dir_candidate_text, str) else None
    )
    if not git_dir_candidate.is_absolute() or (
        common_dir_candidate is not None and not common_dir_candidate.is_absolute()
    ):
        raise InvalidInputError("external Git relation candidates must be absolute paths")
    AuthorizationRepository(Database(paths)).require_valid(
        authorization_receipt_id=args.relation_authorization_receipt_id,
        capability=capability,
        request=relation_request,
    )
    return {
        "status": "ok",
        "relation": probe_external_git_relation(
            workspace,
            pointer,
            marker_kind=marker_kind,
            git_dir_candidate=git_dir_candidate,
            common_dir_candidate=common_dir_candidate,
        ),
    }


def _handle_candidate_inspection(args: argparse.Namespace, paths: DataPaths) -> dict[str, Any]:
    workspace = Path(args.workspace).expanduser().resolve(strict=False)
    _verify_scan_authorization(args, paths, workspace)
    pointer = _normal_workspace_child(workspace, args.git_pointer, "git_pointer")
    return {
        "status": "ok",
        "candidate": inspect_external_git_candidate(workspace, pointer),
    }


def run(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    paths = DataPaths.from_argument(args.data_dir)
    try:
        if args.command == "bootstrap":
            payload = _handle_bootstrap(paths)
        elif args.command == "data-status":
            payload = _handle_data_status(paths)
        elif args.command == "authorize":
            payload = _handle_authorize(args, paths)
        elif args.command == "verify-authorization":
            payload = _handle_verify(args, paths)
        elif args.command == "scan":
            payload = _handle_scan(args, paths)
        elif args.command == "refresh":
            payload = _handle_refresh(args, paths)
        elif args.command == "inspect-external-git-candidate":
            payload = _handle_candidate_inspection(args, paths)
        elif args.command == "probe-external-git-relation":
            payload = _handle_relation_probe(args, paths)
        else:
            raise AssertionError(f"unexpected command: {args.command}")
    except GoodJobError as exc:
        _write_json(sys.stderr, {"status": "error", "code": exc.code, "message": str(exc)})
        return 2
    _write_json(sys.stdout, payload)
    return 0


def main() -> None:
    sys.exit(run())

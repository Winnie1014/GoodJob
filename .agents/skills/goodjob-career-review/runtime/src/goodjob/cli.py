"""JSON-only command boundary for the GoodJob local runtime."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from goodjob.auth import AuthorizationRepository, decode_scope, read_capability_from_fd
from goodjob.db import Database
from goodjob.errors import GoodJobError, InvalidInputError
from goodjob.paths import DataPaths


def _write_json(stream: Any, payload: dict[str, Any]) -> None:
    stream.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")


def _data_usage(path: Path) -> int:
    if not path.exists():
        return 0
    if path.is_file():
        return path.stat().st_size
    return sum(entry.stat().st_size for entry in path.rglob("*") if entry.is_file())


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
    authorize.add_argument(
        "--receipt-kind",
        choices=("source_analysis", "external_git_relation_probe", "external_git_metadata"),
        required=True,
    )
    authorize.add_argument("--scope-json", required=True)
    authorize.add_argument("--notice-version", required=True)
    authorize.add_argument("--capability-fd", type=int, required=True)
    authorize.add_argument(
        "--confirmed",
        action="store_true",
        help="record that the Skill already showed and received required owner confirmation",
    )

    verify = subparsers.add_parser(
        "verify-authorization", help="verify a protected request's current session binding"
    )
    verify.add_argument("--authorization-receipt-id", required=True)
    verify.add_argument(
        "--receipt-kind",
        choices=("source_analysis", "external_git_relation_probe", "external_git_metadata"),
        required=True,
    )
    verify.add_argument("--scope-json", required=True)
    verify.add_argument("--notice-version", required=True)
    verify.add_argument("--capability-fd", type=int, required=True)
    return parser


def _handle_bootstrap(paths: DataPaths) -> dict[str, Any]:
    schema_version = Database(paths).migrate()
    return {
        "status": "ok",
        "schema_version": schema_version,
        "data_dir": str(paths.root),
    }


def _handle_data_status(paths: DataPaths) -> dict[str, Any]:
    Database(paths).migrate()
    return {
        "status": "ok",
        "data_dir": str(paths.root),
        "usage_bytes": {
            "sqlite": _data_usage(paths.database_file),
            "artifacts": _data_usage(paths.artifacts_dir),
            "exports": _data_usage(paths.exports_dir),
            "drafts": _data_usage(paths.drafts_dir),
        },
    }


def _handle_authorize(args: argparse.Namespace, paths: DataPaths) -> dict[str, Any]:
    if not args.confirmed:
        raise InvalidInputError(
            "owner confirmation is required before recording an authorization receipt"
        )
    capability = read_capability_from_fd(args.capability_fd)
    receipt = AuthorizationRepository(Database(paths)).issue(
        receipt_kind=args.receipt_kind,
        capability=capability,
        scope=decode_scope(args.scope_json),
        notice_version=args.notice_version,
    )
    return {"status": "ok", "receipt": receipt.as_json()}


def _handle_verify(args: argparse.Namespace, paths: DataPaths) -> dict[str, Any]:
    capability = read_capability_from_fd(args.capability_fd)
    receipt = AuthorizationRepository(Database(paths)).require_valid(
        authorization_receipt_id=args.authorization_receipt_id,
        capability=capability,
        receipt_kind=args.receipt_kind,
        scope=decode_scope(args.scope_json),
        notice_version=args.notice_version,
    )
    return {"status": "ok", "receipt": receipt.as_json()}


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
        else:
            raise AssertionError(f"unexpected command: {args.command}")
    except GoodJobError as exc:
        _write_json(sys.stderr, {"status": "error", "code": exc.code, "message": str(exc)})
        return 2
    _write_json(sys.stdout, payload)
    return 0


def main() -> None:
    sys.exit(run())

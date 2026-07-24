"""Session-bound authorization receipts without persisting bearer capabilities."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import uuid
from dataclasses import dataclass
from typing import Any

from goodjob.db import Database
from goodjob.errors import CapabilityError, InvalidInputError

CAPABILITY_BYTES = 32
SESSION_BINDING_PREFIX = b"goodjob-session-binding-v1"
RECEIPT_KINDS = frozenset(
    {"source_analysis", "external_git_relation_probe", "external_git_metadata"}
)


def canonical_json(value: Any) -> str:
    """Serialize a scope as a stable, non-secret value object."""
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise InvalidInputError("scope_json must contain only JSON values") from exc


def decode_scope(raw_scope: str) -> Any:
    try:
        return json.loads(raw_scope)
    except json.JSONDecodeError as exc:
        raise InvalidInputError("scope_json must be valid JSON") from exc


def generate_capability() -> bytes:
    """Generate a 256-bit task-scoped capability in volatile process memory."""
    return secrets.token_bytes(CAPABILITY_BYTES)


def read_capability_from_fd(fd: int) -> bytes:
    """Read exactly one capability from a dedicated FD and never echo it."""
    if fd < 0:
        raise CapabilityError("capability file descriptor must be non-negative")
    chunks: list[bytes] = []
    remaining = CAPABILITY_BYTES + 1
    try:
        while remaining > 0:
            chunk = os.read(fd, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
    except OSError as exc:
        raise CapabilityError("unable to read session capability") from exc
    raw = b"".join(chunks)
    if len(raw) != CAPABILITY_BYTES:
        raise CapabilityError("session capability is missing or malformed")
    return raw


def session_binding_digest(capability: bytes) -> bytes:
    if len(capability) != CAPABILITY_BYTES:
        raise CapabilityError("session capability is missing or malformed")
    return hashlib.sha256(SESSION_BINDING_PREFIX + capability).digest()


@dataclass(frozen=True)
class AuthorizationReceipt:
    authorization_receipt_id: str
    receipt_kind: str
    scope_descriptor: str
    notice_version: str
    confirmed_at: str

    def as_json(self) -> dict[str, str]:
        return {
            "authorization_receipt_id": self.authorization_receipt_id,
            "receipt_kind": self.receipt_kind,
            "scope_descriptor": self.scope_descriptor,
            "notice_version": self.notice_version,
            "confirmed_at": self.confirmed_at,
        }


class AuthorizationRepository:
    """Create and validate receipts using the caller's ephemeral capability."""

    def __init__(self, database: Database) -> None:
        self._database = database

    def issue(
        self,
        *,
        receipt_kind: str,
        capability: bytes,
        scope: Any,
        notice_version: str,
    ) -> AuthorizationReceipt:
        if receipt_kind not in RECEIPT_KINDS:
            raise InvalidInputError("unsupported authorization receipt kind")
        if not notice_version.strip():
            raise InvalidInputError("notice_version must not be empty")
        descriptor = canonical_json(scope)
        receipt_id = str(uuid.uuid4())
        digest = session_binding_digest(capability)
        with self._database.write_transaction() as connection:
            connection.execute(
                """
                INSERT INTO authorization_receipts(
                    authorization_receipt_id, receipt_kind, session_binding_digest,
                    issuer_kind, scope_descriptor, notice_version, confirmed_at
                ) VALUES (?, ?, ?, 'codex_task_runtime', ?, ?,
                    strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
                """,
                (receipt_id, receipt_kind, digest, descriptor, notice_version),
            )
            row = connection.execute(
                """
                SELECT authorization_receipt_id, receipt_kind, scope_descriptor,
                       notice_version, confirmed_at
                FROM authorization_receipts
                WHERE authorization_receipt_id = ?
                """,
                (receipt_id,),
            ).fetchone()
        assert row is not None
        return AuthorizationReceipt(
            authorization_receipt_id=str(row["authorization_receipt_id"]),
            receipt_kind=str(row["receipt_kind"]),
            scope_descriptor=str(row["scope_descriptor"]),
            notice_version=str(row["notice_version"]),
            confirmed_at=str(row["confirmed_at"]),
        )

    def require_valid(
        self,
        *,
        authorization_receipt_id: str,
        capability: bytes,
        receipt_kind: str,
        scope: Any,
        notice_version: str,
    ) -> AuthorizationReceipt:
        """Reject unless task binding, scope, kind, and notice exactly match."""
        expected_digest = session_binding_digest(capability)
        expected_scope = canonical_json(scope)
        with self._database.read_connection() as connection:
            row = connection.execute(
                """
                SELECT authorization_receipt_id, receipt_kind, session_binding_digest,
                       scope_descriptor, notice_version, confirmed_at
                FROM authorization_receipts
                WHERE authorization_receipt_id = ?
                """,
                (authorization_receipt_id,),
            ).fetchone()
        if row is None:
            raise CapabilityError("authorization receipt is not valid for this session")
        stored_digest = bytes(row["session_binding_digest"])
        digest_matches = hmac.compare_digest(stored_digest, expected_digest)
        values_match = (
            str(row["receipt_kind"]) == receipt_kind
            and str(row["scope_descriptor"]) == expected_scope
            and str(row["notice_version"]) == notice_version
        )
        if not digest_matches or not values_match:
            raise CapabilityError("authorization receipt is not valid for this session")
        return AuthorizationReceipt(
            authorization_receipt_id=str(row["authorization_receipt_id"]),
            receipt_kind=str(row["receipt_kind"]),
            scope_descriptor=str(row["scope_descriptor"]),
            notice_version=str(row["notice_version"]),
            confirmed_at=str(row["confirmed_at"]),
        )

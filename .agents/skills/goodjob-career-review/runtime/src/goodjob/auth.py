"""Session-bound authorization receipts without persisting bearer capabilities."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import uuid
from dataclasses import dataclass
from enum import StrEnum
from typing import Self

from goodjob.db import Database
from goodjob.errors import CapabilityError, InvalidInputError

CAPABILITY_BYTES = 32
SESSION_BINDING_PREFIX = b"goodjob-session-binding-v1"


class ReceiptKind(StrEnum):
    SOURCE_ANALYSIS = "source_analysis"
    EXTERNAL_GIT_RELATION_PROBE = "external_git_relation_probe"
    EXTERNAL_GIT_METADATA = "external_git_metadata"


def receipt_kind_values() -> tuple[str, ...]:
    return tuple(kind.value for kind in ReceiptKind)


def canonical_json(value: object) -> str:
    """Serialize a scope as a stable, non-secret value object."""
    try:
        serialized = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise InvalidInputError("scope_json must contain only JSON values") from exc
    _utf8_bytes(serialized, "scope_json")
    return serialized


def decode_scope(raw_scope: str) -> object:
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


def read_capability_from_handle(handle: int) -> bytes:
    """Take and close one allowlisted Windows capability HANDLE."""
    from goodjob.platform.capability_windows import read_bytes_from_handle

    raw = read_bytes_from_handle(handle, maximum_bytes=CAPABILITY_BYTES + 1)
    if len(raw) != CAPABILITY_BYTES:
        raise CapabilityError("session capability is missing or malformed")
    return raw


def session_binding_digest(capability: bytes) -> bytes:
    if len(capability) != CAPABILITY_BYTES:
        raise CapabilityError("session capability is missing or malformed")
    return hashlib.sha256(SESSION_BINDING_PREFIX + capability).digest()


def _utf8_bytes(value: str, field_name: str) -> bytes:
    try:
        return value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise InvalidInputError(f"{field_name} must contain valid UTF-8 text") from exc


@dataclass(frozen=True)
class AuthorizationRequest:
    """Non-secret protected-operation inputs that must agree with a receipt."""

    receipt_kind: ReceiptKind
    scope_descriptor: str
    notice_version: str

    @classmethod
    def from_values(cls, *, receipt_kind: str, scope: object, notice_version: str) -> Self:
        try:
            kind = ReceiptKind(receipt_kind)
        except ValueError as exc:
            raise InvalidInputError("unsupported authorization receipt kind") from exc
        if not notice_version.strip():
            raise InvalidInputError("notice_version must not be empty")
        _utf8_bytes(notice_version, "notice_version")
        return cls(
            receipt_kind=kind,
            scope_descriptor=canonical_json(scope),
            notice_version=notice_version,
        )


@dataclass(frozen=True)
class AuthorizationReceipt:
    authorization_receipt_id: str
    receipt_kind: ReceiptKind
    scope_descriptor: str
    notice_version: str
    confirmed_at: str

    def as_json(self) -> dict[str, str]:
        return {
            "authorization_receipt_id": self.authorization_receipt_id,
            "receipt_kind": self.receipt_kind.value,
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
        capability: bytes,
        request: AuthorizationRequest,
        issuer_kind: str = "codex_task_runtime",
    ) -> AuthorizationReceipt:
        if not issuer_kind.strip():
            raise InvalidInputError("issuer_kind must not be empty")
        receipt_id = str(uuid.uuid4())
        digest = session_binding_digest(capability)
        with self._database.write_transaction() as connection:
            connection.execute(
                """
                INSERT INTO authorization_receipts(
                    authorization_receipt_id, receipt_kind, session_binding_digest,
                    issuer_kind, scope_descriptor, notice_version, confirmed_at
                ) VALUES (?, ?, ?, ?, ?, ?,
                    strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
                """,
                (
                    receipt_id,
                    request.receipt_kind.value,
                    digest,
                    issuer_kind,
                    request.scope_descriptor,
                    request.notice_version,
                ),
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
        return _receipt_from_row(row)

    def require_valid(
        self,
        *,
        authorization_receipt_id: str,
        capability: bytes,
        request: AuthorizationRequest,
    ) -> AuthorizationReceipt:
        """Reject unless task binding, scope, kind, and notice exactly match."""
        _utf8_bytes(authorization_receipt_id, "authorization_receipt_id")
        expected_digest = session_binding_digest(capability)
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
            hmac.compare_digest(
                _utf8_bytes(str(row["receipt_kind"]), "stored receipt kind"),
                _utf8_bytes(request.receipt_kind.value, "receipt_kind"),
            )
            and hmac.compare_digest(
                _utf8_bytes(str(row["scope_descriptor"]), "stored scope descriptor"),
                _utf8_bytes(request.scope_descriptor, "scope_descriptor"),
            )
            and hmac.compare_digest(
                _utf8_bytes(str(row["notice_version"]), "stored notice version"),
                _utf8_bytes(request.notice_version, "notice_version"),
            )
        )
        if not digest_matches or not values_match:
            raise CapabilityError("authorization receipt is not valid for this session")
        return _receipt_from_row(row)


def _receipt_from_row(row: object) -> AuthorizationReceipt:
    """Create the typed public receipt only after SQLite has enforced its schema."""
    from sqlite3 import Row

    assert isinstance(row, Row)
    return AuthorizationReceipt(
        authorization_receipt_id=str(row["authorization_receipt_id"]),
        receipt_kind=ReceiptKind(str(row["receipt_kind"])),
        scope_descriptor=str(row["scope_descriptor"]),
        notice_version=str(row["notice_version"]),
        confirmed_at=str(row["confirmed_at"]),
    )

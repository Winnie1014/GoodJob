from __future__ import annotations

import sqlite3

import pytest

from goodjob.auth import (
    AuthorizationRepository,
    AuthorizationRequest,
    generate_capability,
    session_binding_digest,
)
from goodjob.db import Database
from goodjob.errors import CapabilityError
from goodjob.paths import DataPaths


def test_receipt_validates_only_for_same_capability_scope_and_notice(data_paths: DataPaths) -> None:
    repository = AuthorizationRepository(Database(data_paths))
    capability = generate_capability()
    scope = {"workspace_path": "/tmp/workspace", "allowed_categories": ["source_analysis"]}
    request = AuthorizationRequest.from_values(
        receipt_kind="source_analysis", scope=scope, notice_version="notice-v1"
    )
    receipt = repository.issue(
        capability=capability,
        request=request,
    )

    verified = repository.require_valid(
        authorization_receipt_id=receipt.authorization_receipt_id,
        capability=capability,
        request=request,
    )

    assert verified.authorization_receipt_id == receipt.authorization_receipt_id
    with pytest.raises(CapabilityError):
        repository.require_valid(
            authorization_receipt_id=receipt.authorization_receipt_id,
            capability=generate_capability(),
            request=request,
        )
    with pytest.raises(CapabilityError):
        repository.require_valid(
            authorization_receipt_id=receipt.authorization_receipt_id,
            capability=capability,
            request=AuthorizationRequest.from_values(
                receipt_kind="source_analysis",
                scope={"workspace_path": "/tmp/other", "allowed_categories": ["source_analysis"]},
                notice_version="notice-v1",
            ),
        )
    with pytest.raises(CapabilityError):
        repository.require_valid(
            authorization_receipt_id=receipt.authorization_receipt_id,
            capability=capability,
            request=AuthorizationRequest.from_values(
                receipt_kind="source_analysis", scope=scope, notice_version="notice-v2"
            ),
        )


def test_database_contains_digest_not_raw_capability(data_paths: DataPaths) -> None:
    repository = AuthorizationRepository(Database(data_paths))
    capability = bytes(range(32))
    receipt = repository.issue(
        capability=capability,
        request=AuthorizationRequest.from_values(
            receipt_kind="source_analysis",
            scope={"workspace_path": "/tmp/workspace"},
            notice_version="notice-v1",
        ),
    )

    connection = sqlite3.connect(data_paths.database_file)
    stored = connection.execute(
        (
            "SELECT session_binding_digest FROM authorization_receipts "
            "WHERE authorization_receipt_id = ?"
        ),
        (receipt.authorization_receipt_id,),
    ).fetchone()
    connection.close()

    assert stored is not None
    assert stored[0] == session_binding_digest(capability)
    assert stored[0] != capability

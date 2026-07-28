from __future__ import annotations

import json
import os
import sqlite3
import stat
from pathlib import Path
from typing import cast

import pytest
from test_reporting import _authorize, _dict, _list, _prepare_and_analyze, _workspace

from goodjob.db import Database
from goodjob.errors import InvalidInputError, WriterBusyError
from goodjob.exporting import ExportService, _InjectedExportInterruption
from goodjob.locks import ExclusiveWriterLock
from goodjob.paths import DataPaths
from goodjob.reporting import ArtifactSnapshotService


def _published_snapshot(
    tmp_path: Path,
    data_paths: DataPaths,
) -> tuple[Path, Database, str, dict[str, object]]:
    workspace = _workspace(tmp_path)
    database = Database(data_paths)
    receipt_id = _authorize(database, workspace)
    run_id, _ = _prepare_and_analyze(database, workspace, receipt_id)
    snapshot = _dict(ArtifactSnapshotService(database).render(run_id)["artifact_snapshot"])
    return workspace, database, receipt_id, snapshot


def _translation_request(
    source: dict[str, object],
    *,
    items: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    source_items = [_dict(value) for value in _list(source["items"])]
    candidates: list[dict[str, object]] = []
    for item in source_items:
        target = (
            {
                "text": (
                    "Explains a testable Python function boundary while preserving "
                    "the recorded value (1)."
                )
            }
            if item["export_kind"] == "resume"
            else {
                "question": "How is the testable function boundary implemented?",
                "answer": "It uses Python and preserves the recorded value (1).",
            }
        )
        candidates.append(
            {
                "source_item_id": item["source_item_id"],
                "export_kind": item["export_kind"],
                "claim_refs": item["claim_refs"],
                "evidence_refs": item["evidence_refs"],
                "role_lens_refs": item["role_lens_refs"],
                "anchors": item["anchors"],
                "project_id": item["project_id"],
                "module_id": item["module_id"],
                "target": target,
            }
        )
    return {
        "contract_version": "translation-export-request-v1",
        "action": "publish",
        "source_artifact_snapshot_id": source["source_artifact_snapshot_id"],
        "source_projection_sha256": source["source_projection_sha256"],
        "target_language": "en",
        "export_kinds": ["resume", "interview_qa"],
        "items": items if items is not None else candidates,
    }


def _prepare_translation(
    service: ExportService,
    workspace: Path,
    receipt_id: str,
    snapshot: dict[str, object],
) -> dict[str, object]:
    prepared = service.translate_export(
        workspace_path=workspace,
        authorization_receipt_id=receipt_id,
        request_value={
            "contract_version": "translation-export-request-v1",
            "action": "prepare",
            "source_artifact_snapshot_id": snapshot["artifact_snapshot_id"],
            "target_language": "en",
            "export_kinds": ["resume", "interview_qa"],
        },
    )
    return _dict(prepared["translation_source"])


def test_translation_prepare_reads_one_frozen_projection_without_writing_export_state(
    tmp_path: Path,
    data_paths: DataPaths,
) -> None:
    workspace, database, receipt_id, snapshot = _published_snapshot(tmp_path, data_paths)

    prepared = ExportService(database).translate_export(
        workspace_path=workspace,
        authorization_receipt_id=receipt_id,
        request_value={
            "contract_version": "translation-export-request-v1",
            "action": "prepare",
            "source_artifact_snapshot_id": snapshot["artifact_snapshot_id"],
            "target_language": "en",
            "export_kinds": ["resume", "interview_qa"],
        },
    )

    source = _dict(prepared["translation_source"])
    items = [_dict(value) for value in _list(source["items"])]
    assert source["source_artifact_snapshot_id"] == snapshot["artifact_snapshot_id"]
    assert source["source_report_bundle_sha256"] == snapshot["report_bundle_sha256"]
    assert isinstance(source["source_projection_sha256"], str)
    assert {item["export_kind"] for item in items} == {"resume", "interview_qa"}
    assert all(isinstance(item["source_text"], str) and item["source_text"] for item in items)
    assert all(
        set(item)
        == {
            "source_item_id",
            "export_kind",
            "source_text",
            "claim_refs",
            "evidence_refs",
            "role_lens_refs",
            "anchors",
            "project_id",
            "module_id",
        }
        for item in items
    )
    assert not any(data_paths.export_tmp_dir.iterdir())
    assert not [path for path in data_paths.exports_dir.iterdir() if path.name != ".tmp"]
    connection = sqlite3.connect(data_paths.database_file)
    try:
        assert connection.execute("SELECT COUNT(*) FROM export_attempts").fetchone() == (0,)
        assert connection.execute("SELECT COUNT(*) FROM derived_exports").fetchone() == (0,)
    finally:
        connection.close()
    assert Path(cast(str, snapshot["html_path"])).is_file()


def test_translation_publish_atomically_creates_one_immutable_derived_export(
    tmp_path: Path,
    data_paths: DataPaths,
) -> None:
    workspace, database, receipt_id, snapshot = _published_snapshot(tmp_path, data_paths)
    service = ExportService(database)
    latest_before = data_paths.latest_artifact_file.read_bytes()
    source_files = {
        key: Path(cast(str, snapshot[key])).read_bytes()
        for key in ("report_markdown_path", "resume_markdown_path", "html_path")
    }
    source = _prepare_translation(service, workspace, receipt_id, snapshot)

    published = service.translate_export(
        workspace_path=workspace,
        authorization_receipt_id=receipt_id,
        request_value=_translation_request(source),
    )

    derived = _dict(published["derived_export"])
    output_path = Path(cast(str, derived["output_path"]))
    resume_path = Path(cast(str, derived["resume_markdown_path"]))
    interview_path = Path(cast(str, derived["interview_qa_markdown_path"]))
    manifest_path = Path(cast(str, derived["manifest_path"]))
    assert output_path.is_dir()
    assert {path.name for path in output_path.iterdir()} == {
        "resume.en.md",
        "interview.en.md",
        "manifest.json",
    }
    assert all(path.is_file() for path in (resume_path, interview_path, manifest_path))
    assert all(
        stat.S_IMODE(path.stat().st_mode) == stat.S_IRUSR
        for path in (resume_path, interview_path, manifest_path)
    )
    assert stat.S_IMODE(output_path.stat().st_mode) == stat.S_IRUSR | stat.S_IXUSR
    assert "Python" in resume_path.read_text(encoding="utf-8")
    assert "How is" in interview_path.read_text(encoding="utf-8")
    assert not list(output_path.glob("*.html"))
    manifest = cast(
        dict[str, object],
        json.loads(manifest_path.read_text(encoding="utf-8")),
    )
    assert manifest["source_artifact_snapshot_id"] == snapshot["artifact_snapshot_id"]
    assert manifest["source_report_bundle_sha256"] == snapshot["report_bundle_sha256"]
    assert manifest["source_projection_sha256"] == source["source_projection_sha256"]
    mappings = [_dict(value) for value in _list(manifest["items"])]
    assert {mapping["source_item_id"] for mapping in mappings} == {
        item["source_item_id"] for item in map(_dict, _list(source["items"]))
    }
    assert data_paths.latest_artifact_file.read_bytes() == latest_before
    assert all(
        Path(cast(str, snapshot[key])).read_bytes() == content
        for key, content in source_files.items()
    )
    assert not any(data_paths.export_tmp_dir.iterdir())
    connection = sqlite3.connect(data_paths.database_file)
    try:
        assert connection.execute("SELECT status FROM export_attempts").fetchone() == ("succeeded",)
        row = connection.execute(
            """
            SELECT source_artifact_snapshot_id, source_report_bundle_sha256,
                   source_projection_sha256, language, export_kinds, output_path
            FROM derived_exports
            """
        ).fetchone()
    finally:
        connection.close()
    assert row == (
        snapshot["artifact_snapshot_id"],
        snapshot["report_bundle_sha256"],
        source["source_projection_sha256"],
        "en",
        '["resume","interview_qa"]',
        f"exports/{derived['derived_export_id']}",
    )


@pytest.mark.parametrize(
    "damage",
    ["missing_item", "extra_item", "anchor_tamper", "number_omission", "extra_technology"],
)
def test_translation_publish_rejects_non_equivalent_candidate_batches_without_writes(
    tmp_path: Path,
    data_paths: DataPaths,
    damage: str,
) -> None:
    workspace, database, receipt_id, snapshot = _published_snapshot(tmp_path, data_paths)
    service = ExportService(database)
    source = _prepare_translation(service, workspace, receipt_id, snapshot)
    request = _translation_request(source)
    items = cast(list[dict[str, object]], request["items"])
    if damage == "missing_item":
        request["items"] = items[:-1]
    elif damage == "extra_item":
        extra = cast(dict[str, object], json.loads(json.dumps(items[0])))
        extra["source_item_id"] = "unknown-source-item"
        request["items"] = [*items, extra]
    elif damage == "anchor_tamper":
        anchors = _dict(items[0]["anchors"])
        anchors["numbers_and_units"] = [*_list(anchors["numbers_and_units"]), "999"]
    elif damage == "number_omission":
        target = _dict(items[0]["target"])
        for key in target:
            target[key] = "Explains the testable Python function boundary without a new fact."
    else:
        target = _dict(items[0]["target"])
        first_key = next(iter(target))
        target[first_key] = f"{target[first_key]} Redis"

    latest_before = data_paths.latest_artifact_file.read_bytes()
    with pytest.raises(InvalidInputError):
        service.translate_export(
            workspace_path=workspace,
            authorization_receipt_id=receipt_id,
            request_value=request,
        )

    assert data_paths.latest_artifact_file.read_bytes() == latest_before
    assert not any(data_paths.export_tmp_dir.iterdir())
    assert not [path for path in data_paths.exports_dir.iterdir() if path.name != ".tmp"]
    connection = sqlite3.connect(data_paths.database_file)
    try:
        assert connection.execute("SELECT COUNT(*) FROM export_attempts").fetchone() == (0,)
        assert connection.execute("SELECT COUNT(*) FROM derived_exports").fetchone() == (0,)
    finally:
        connection.close()


def test_translation_publish_rejects_snapshot_tampering_after_prepare_without_attempt(
    tmp_path: Path,
    data_paths: DataPaths,
) -> None:
    workspace, database, receipt_id, snapshot = _published_snapshot(tmp_path, data_paths)
    service = ExportService(database)
    source = _prepare_translation(service, workspace, receipt_id, snapshot)
    html_path = Path(cast(str, snapshot["html_path"]))
    html_path.parent.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
    html_path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    html_path.write_text("tampered after translation prepare", encoding="utf-8")

    with pytest.raises(InvalidInputError, match="digest does not match"):
        service.translate_export(
            workspace_path=workspace,
            authorization_receipt_id=receipt_id,
            request_value=_translation_request(source),
        )

    connection = sqlite3.connect(data_paths.database_file)
    try:
        assert connection.execute("SELECT COUNT(*) FROM export_attempts").fetchone() == (0,)
        assert connection.execute("SELECT COUNT(*) FROM derived_exports").fetchone() == (0,)
    finally:
        connection.close()


@pytest.mark.parametrize("fault_at", ["after_temp", "after_publish", "before_database_commit"])
def test_dead_export_owner_recovery_cleans_only_registered_paths_and_retries_fresh(
    tmp_path: Path,
    data_paths: DataPaths,
    monkeypatch: pytest.MonkeyPatch,
    fault_at: str,
) -> None:
    workspace, database, receipt_id, snapshot = _published_snapshot(tmp_path, data_paths)
    service = ExportService(database)
    source = _prepare_translation(service, workspace, receipt_id, snapshot)
    request = _translation_request(source)
    first_success = _dict(
        service.translate_export(
            workspace_path=workspace,
            authorization_receipt_id=receipt_id,
            request_value=request,
        )["derived_export"]
    )
    first_output = Path(cast(str, first_success["output_path"]))
    first_manifest = Path(cast(str, first_success["manifest_path"])).read_bytes()
    unknown = data_paths.exports_dir / "owner-unknown-directory"
    unknown.mkdir()
    (unknown / "keep.txt").write_text("keep", encoding="utf-8")
    latest_before = data_paths.latest_artifact_file.read_bytes()
    monkeypatch.setattr(
        "goodjob.exporting.process_identity",
        lambda: "pid:999999;started:Thu Jan  1 00:00:00 1970",
    )

    with pytest.raises(_InjectedExportInterruption):
        service.translate_export(
            workspace_path=workspace,
            authorization_receipt_id=receipt_id,
            request_value=request,
            _fault_at=fault_at,
        )
    connection = sqlite3.connect(data_paths.database_file)
    try:
        interrupted_row = connection.execute(
            """
            SELECT export_attempt_id, derived_export_id, temp_relative_path,
                   final_relative_path, status
            FROM export_attempts ORDER BY rowid DESC LIMIT 1
            """
        ).fetchone()
    finally:
        connection.close()
    assert interrupted_row is not None
    old_attempt_id, old_export_id, temp_relative, final_relative, status = interrupted_row
    assert status == "running"
    if fault_at == "after_temp":
        assert (data_paths.root / temp_relative).is_dir()
        assert not (data_paths.root / final_relative).exists()
    else:
        assert not (data_paths.root / temp_relative).exists()
        assert (data_paths.root / final_relative).is_dir()

    retried = _dict(
        service.translate_export(
            workspace_path=workspace,
            authorization_receipt_id=receipt_id,
            request_value=request,
        )["derived_export"]
    )

    assert retried["export_attempt_id"] != old_attempt_id
    assert retried["derived_export_id"] != old_export_id
    assert not (data_paths.root / temp_relative).exists()
    assert not (data_paths.root / final_relative).exists()
    assert first_output.is_dir()
    assert Path(cast(str, first_success["manifest_path"])).read_bytes() == first_manifest
    assert (unknown / "keep.txt").read_text(encoding="utf-8") == "keep"
    assert data_paths.latest_artifact_file.read_bytes() == latest_before
    connection = sqlite3.connect(data_paths.database_file)
    try:
        statuses = connection.execute(
            "SELECT status FROM export_attempts ORDER BY rowid"
        ).fetchall()
        export_count = connection.execute("SELECT COUNT(*) FROM derived_exports").fetchone()
    finally:
        connection.close()
    assert statuses == [("succeeded",), ("interrupted",), ("succeeded",)]
    assert export_count == (2,)


def test_export_recovery_does_not_interrupt_or_clean_an_unproven_live_owner(
    tmp_path: Path,
    data_paths: DataPaths,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace, database, receipt_id, snapshot = _published_snapshot(tmp_path, data_paths)
    service = ExportService(database)
    source = _prepare_translation(service, workspace, receipt_id, snapshot)
    request = _translation_request(source)
    monkeypatch.setattr(
        "goodjob.exporting.process_identity",
        lambda: f"pid:{os.getpid()};started:unverified-test-marker",
    )
    with pytest.raises(_InjectedExportInterruption):
        service.translate_export(
            workspace_path=workspace,
            authorization_receipt_id=receipt_id,
            request_value=request,
            _fault_at="after_temp",
        )
    connection = sqlite3.connect(data_paths.database_file)
    try:
        row = connection.execute(
            "SELECT export_attempt_id, temp_relative_path FROM export_attempts"
        ).fetchone()
    finally:
        connection.close()
    assert row is not None
    old_attempt_id, temp_relative = row
    assert (data_paths.root / temp_relative).is_dir()
    monkeypatch.setattr("goodjob.exporting.owner_process_stopped", lambda _identity: False)

    succeeded = _dict(
        service.translate_export(
            workspace_path=workspace,
            authorization_receipt_id=receipt_id,
            request_value=request,
        )["derived_export"]
    )

    assert succeeded["export_attempt_id"] != old_attempt_id
    assert (data_paths.root / temp_relative).is_dir()
    connection = sqlite3.connect(data_paths.database_file)
    try:
        statuses = connection.execute(
            "SELECT status FROM export_attempts ORDER BY rowid"
        ).fetchall()
    finally:
        connection.close()
    assert statuses == [("running",), ("succeeded",)]


def test_translation_publish_writer_busy_creates_no_attempt_or_files(
    tmp_path: Path,
    data_paths: DataPaths,
) -> None:
    workspace, database, receipt_id, snapshot = _published_snapshot(tmp_path, data_paths)
    service = ExportService(database)
    source = _prepare_translation(service, workspace, receipt_id, snapshot)

    with (
        ExclusiveWriterLock(data_paths.writer_lock_file),
        pytest.raises(WriterBusyError),
    ):
        service.translate_export(
            workspace_path=workspace,
            authorization_receipt_id=receipt_id,
            request_value=_translation_request(source),
        )

    assert not any(data_paths.export_tmp_dir.iterdir())
    assert not [path for path in data_paths.exports_dir.iterdir() if path.name != ".tmp"]
    connection = sqlite3.connect(data_paths.database_file)
    try:
        assert connection.execute("SELECT COUNT(*) FROM export_attempts").fetchone() == (0,)
    finally:
        connection.close()


def test_normal_export_failure_is_diagnostic_and_leaves_no_visible_partial_output(
    tmp_path: Path,
    data_paths: DataPaths,
) -> None:
    workspace, database, receipt_id, snapshot = _published_snapshot(tmp_path, data_paths)
    service = ExportService(database)
    source = _prepare_translation(service, workspace, receipt_id, snapshot)
    latest_before = data_paths.latest_artifact_file.read_bytes()

    with pytest.raises(InvalidInputError, match="ExportAttempt diagnostic"):
        service.translate_export(
            workspace_path=workspace,
            authorization_receipt_id=receipt_id,
            request_value=_translation_request(source),
            _fault_at="fail_after_temp",
        )

    assert data_paths.latest_artifact_file.read_bytes() == latest_before
    assert not any(data_paths.export_tmp_dir.iterdir())
    assert not [path for path in data_paths.exports_dir.iterdir() if path.name != ".tmp"]
    connection = sqlite3.connect(data_paths.database_file)
    try:
        row = connection.execute(
            "SELECT status, finished_at, error_summary FROM export_attempts"
        ).fetchone()
        assert connection.execute("SELECT COUNT(*) FROM derived_exports").fetchone() == (0,)
    finally:
        connection.close()
    assert row is not None
    assert row[0] == "failed"
    assert row[1]
    assert "injected normal export failure" in row[2]

from __future__ import annotations

import json
import sqlite3
import stat
import uuid
from pathlib import Path
from typing import cast

import pytest

from goodjob.analysis import AnalysisService
from goodjob.auth import AuthorizationRepository, AuthorizationRequest, generate_capability
from goodjob.db import Database
from goodjob.errors import InvalidInputError, WriterBusyError
from goodjob.locks import ExclusiveWriterLock
from goodjob.paths import DataPaths
from goodjob.preparation import PreparationService, validate_job_input
from goodjob.reporting import (
    ArtifactSnapshotService,
    ReportBundleBuilder,
    _artifact_snapshot_id,
    canonical_report_bundle,
    render_dashboard_html,
    render_report_markdown,
)
from goodjob.scanner import WorkspaceScanner


def _workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "dashboard-injection-onerror"
    (workspace / "tests").mkdir(parents=True)
    (workspace / "pyproject.toml").write_text(
        '[project]\nname = "reporting-demo"\nversion = "0.1.0"\n',
        encoding="utf-8",
    )
    (workspace / "app.py").write_text(
        "def calculate(value: int) -> int:\n    return value + 1\n",
        encoding="utf-8",
    )
    (workspace / "tests" / "test_app.py").write_text(
        "from app import calculate\n\ndef test_calculate():\n    assert calculate(1) == 2\n",
        encoding="utf-8",
    )
    return workspace


def _authorize(database: Database, workspace: Path) -> str:
    receipt = AuthorizationRepository(database).issue(
        capability=generate_capability(),
        request=AuthorizationRequest.from_values(
            receipt_kind="source_analysis",
            scope={
                "workspace_path": str(workspace.resolve()),
                "allowed_categories": ["source_analysis"],
            },
            notice_version="goodjob-source-analysis-v1",
        ),
    )
    return receipt.authorization_receipt_id


def _lens() -> dict[str, object]:
    return {
        "contract_version": "role-lens-v1",
        "dimensions": [
            {
                "key": "implementation_depth",
                "display_name": "实现深度",
                "weight_bps": 6000,
                "evaluation_criteria": "评价实际实现机制与边界",
                "required_evidence_kinds": ["implementation"],
            },
            {
                "key": "verification",
                "display_name": "验证能力",
                "weight_bps": 4000,
                "evaluation_criteria": "评价测试定义与验证路径",
                "required_evidence_kinds": ["test_definition"],
            },
        ],
        "evidence_requirements": ["当前实现", "测试定义"],
        "ranking_rules": ["证据覆盖与岗位相关性共同决定排序"],
        "output_sections": ["项目讲解", "学习要点", "简历材料", "面试题库"],
        "question_strategy": {"primary": "追问实现机制、验证边界与替代方案"},
        "gap_rules": ["缺少角色或结果时保留知识缺口"],
        "assumptions": ["未提供 JD"],
        "generator_id": "reporting-test",
        "prompt_contract_version": "reporting-test-v1",
    }


def _dict(value: object) -> dict[str, object]:
    assert isinstance(value, dict)
    return cast(dict[str, object], value)


def _list(value: object) -> list[object]:
    assert isinstance(value, list)
    return cast(list[object], value)


def _prepare_and_analyze(
    database: Database,
    workspace: Path,
    receipt_id: str,
    *,
    scan_run_id: str | None = None,
    role_name: str = "应用软件工程师 </script><img onerror=alert(1)> \u2028\u2029\u202e",
    knowledge_gap_severity: str | None = None,
) -> tuple[str, str]:
    if scan_run_id is None:
        scan = WorkspaceScanner(database).scan(
            workspace_path=str(workspace),
            config_revision="reporting-scan-v1",
            authorization_receipt_id=receipt_id,
        )
        scan_run_id = scan.scan_run_id
    validated = _dict(
        validate_job_input(
            {
                "contract_version": "job-input-v1",
                "target_role": role_name,
                "jd_input": {"kind": "none"},
            }
        )["job_input"]
    )
    prepared = PreparationService(database).start(
        workspace_path=workspace,
        authorization_receipt_id=receipt_id,
        request_value={
            "contract_version": "preparation-request-v1",
            "request_id": str(uuid.uuid4()),
            "scan_run_id": scan_run_id,
            "config_revision": "reporting-prepare-v1",
            "target_role": role_name,
            "jd_input": {"kind": "none"},
            "job_input_validation_sha256": validated["validation_sha256"],
            "requested_exports": [],
            "evidence_limit_per_project": 100,
            "role_lens": _lens(),
        },
    )
    run = _dict(prepared["preparation_run"])
    lens = _dict(prepared["role_lens"])
    evidence_bundle = _dict(prepared["evidence_bundle"])
    coverage = [_dict(value) for value in _list(evidence_bundle["coverage"])]
    project_id = cast(str, next(item["project_id"] for item in coverage if item["eligible"]))
    evidence = [_dict(value) for value in _list(evidence_bundle["evidence_items"])]
    by_kind: dict[str, dict[str, object]] = {}
    for item in evidence:
        by_kind.setdefault(cast(str, item["evidence_kind"]), item)
    implementation_id = cast(str, by_kind["implementation"]["evidence_id"])
    test_id = cast(str, by_kind["test_definition"]["evidence_id"])
    statement_tokens = [
        {
            "kind": "text",
            "value": "我可以解释该项目如何通过可测试的 Python 函数边界实现核心流程，并安全展示 ",
        },
        {
            "kind": "text",
            "value": "</script><img onerror=alert(1)> \u2028\u2029\u202e 与超长输入：" + "x" * 320,
        },
        {"kind": "inert_url", "value": "javascript:alert(1)"},
    ]
    gap_drafts: list[dict[str, object]] = []
    gap_refs: list[str] = []
    if knowledge_gap_severity is not None:
        gap_drafts.append(
            {
                "draft_id": "gap-owner-context",
                "scope_kind": "project",
                "project_id": project_id,
                "dimension": "implementation_depth",
                "stable_gap_concept_key": "owner-context",
                "gap_contract_version": "reporting-context-gap-v1",
                "description_tokens": [{"kind": "text", "value": "尚未补充个人角色与学习上下文。"}],
                "severity": knowledge_gap_severity,
                "resolution_kind": "owner_follow_up",
                "status": "open",
            }
        )
        gap_refs.append("gap-owner-context")
    AnalysisService(database).record_analysis(
        workspace_path=workspace,
        authorization_receipt_id=receipt_id,
        request_value={
            "contract_version": "analysis-commit-v1",
            "request_id": str(uuid.uuid4()),
            "preparation_run_id": run["preparation_run_id"],
            "role_lens_id": lens["role_lens_id"],
            "evidence_drafts": [],
            "claim_drafts": [
                {
                    "draft_id": "claim-main",
                    "claim_key": "core-calculation",
                    "category": "implementation_method",
                    "scope_kind": "project",
                    "project_id": project_id,
                    "section": "project_story",
                    "statement_tokens": statement_tokens,
                    "facets": ["implemented", "test_defined"],
                    "support_level": "cross_checked",
                    "personal_attribution": "capability",
                    "review_semantic_projection": {
                        "concept_keys": ["calculation-boundary"],
                        "mechanism_keys": ["function-call"],
                        "behavior_contract_keys": ["input-to-output"],
                        "tradeoff_keys": [],
                        "technology_identifiers": ["python"],
                    },
                    "evidence_relations": [
                        {
                            "evidence_ref": implementation_id,
                            "relation": "supports",
                            "supported_facets": ["implemented"],
                        },
                        {
                            "evidence_ref": test_id,
                            "relation": "supports",
                            "supported_facets": ["test_defined"],
                        },
                    ],
                }
            ],
            "project_assessments": [
                {
                    "project_id": project_id,
                    "dimension_scores_milli": {
                        "implementation_depth": 800,
                        "verification": 700,
                    },
                    "coverage_bps": 10000,
                    "evidence_refs": [implementation_id, test_id],
                    "gap_refs": gap_refs,
                    "rationale_tokens": [{"kind": "text", "value": "实现和测试定义均有当前证据。"}],
                }
            ],
            "knowledge_gaps": gap_drafts,
        },
    )
    return cast(str, run["preparation_run_id"]), scan_run_id


def _latest(paths: DataPaths) -> dict[str, object]:
    return cast(
        dict[str, object],
        json.loads(paths.latest_artifact_file.read_text(encoding="utf-8")),
    )


def test_report_bundle_and_snapshot_are_deterministic_safe_and_idempotent(
    tmp_path: Path,
    data_paths: DataPaths,
) -> None:
    workspace = _workspace(tmp_path)
    database = Database(data_paths)
    receipt_id = _authorize(database, workspace)
    run_id, _ = _prepare_and_analyze(database, workspace, receipt_id)
    workspace.rename(tmp_path / "workspace-moved-after-analysis")

    builder = ReportBundleBuilder(database)
    first_bundle = builder.build(run_id)
    second_bundle = builder.build(run_id)
    assert canonical_report_bundle(first_bundle) == canonical_report_bundle(second_bundle)
    assert first_bundle["bundle_sha256"] == second_bundle["bundle_sha256"]
    claims = [_dict(value) for value in _list(first_bundle["claims"])]
    assert claims[0]["personal_attribution"] == "capability"
    assert _dict(first_bundle["export_projection"])["projection_sha256"]

    html = render_dashboard_html(first_bundle)
    assert html == render_dashboard_html(second_bundle)
    assert '<meta http-equiv="Content-Security-Policy"' in html
    assert "connect-src 'none'" in html
    assert "unsafe-inline" not in html
    assert "unsafe-eval" not in html
    assert "<img onerror=alert(1)>" not in html
    assert "\\u003cimg onerror=alert(1)\\u003e" in html
    assert "\\u2028" in html
    assert "\\u2029" in html
    assert "\\u202e" in html
    assert "\u202e" not in html
    assert "x" * 320 in html
    assert "overflow-wrap: anywhere" in html
    assert 'href="javascript:' not in html
    markdown = render_report_markdown(first_bundle)
    assert "&lt;/script&gt;" in markdown
    assert "Evidence" in markdown
    assert "capability" in markdown
    assert "\\[U\\+2028\\]\\[U\\+2029\\]\\[U\\+202E\\]" in markdown
    assert "#### 学习要点" in markdown
    assert "#### 如何实现" in markdown
    assert "### STAR 素材" in markdown
    assert "**T · 任务与职责**：未冻结可用 Claim，不补造内容。" in markdown
    assert "**R · 结果**：未冻结可用 Claim，不补造内容。" in markdown

    service = ArtifactSnapshotService(database)
    rendered = service.render(run_id)
    snapshot = _dict(rendered["artifact_snapshot"])
    paths = [
        Path(cast(str, snapshot["report_markdown_path"])),
        Path(cast(str, snapshot["resume_markdown_path"])),
        Path(cast(str, snapshot["html_path"])),
        Path(cast(str, snapshot["manifest_path"])),
    ]
    assert all(path.is_file() for path in paths)
    assert all(stat.S_IMODE(path.stat().st_mode) == stat.S_IRUSR for path in paths)
    assert stat.S_IMODE(paths[0].parent.stat().st_mode) == stat.S_IRUSR | stat.S_IXUSR
    manifest = cast(dict[str, object], json.loads(paths[-1].read_text(encoding="utf-8")))
    assert manifest["report_bundle_sha256"] == first_bundle["bundle_sha256"]
    assert _latest(data_paths)["artifact_snapshot_id"] == snapshot["artifact_snapshot_id"]

    repeated = service.render(run_id)
    assert (
        _dict(repeated["artifact_snapshot"])["artifact_snapshot_id"]
        == snapshot["artifact_snapshot_id"]
    )
    connection = sqlite3.connect(data_paths.database_file)
    assert connection.execute("SELECT COUNT(*) FROM render_attempts").fetchone() == (1,)
    assert connection.execute("SELECT COUNT(*) FROM artifact_snapshots").fetchone() == (1,)
    assert connection.execute("SELECT personal_attribution FROM claim_revisions").fetchone() == (
        "capability",
    )
    connection.close()


@pytest.mark.parametrize("fault_at", ["after_temp", "after_publish"])
def test_render_failure_preserves_latest_and_retries_the_same_bundle(
    tmp_path: Path,
    data_paths: DataPaths,
    fault_at: str,
) -> None:
    workspace = _workspace(tmp_path)
    database = Database(data_paths)
    receipt_id = _authorize(database, workspace)
    first_run, scan_run_id = _prepare_and_analyze(database, workspace, receipt_id)
    service = ArtifactSnapshotService(database)
    first_snapshot = _dict(service.render(first_run)["artifact_snapshot"])
    first_latest = _latest(data_paths)

    second_run, _ = _prepare_and_analyze(
        database,
        workspace,
        receipt_id,
        scan_run_id=scan_run_id,
        role_name="应用软件工程师二次准备",
    )
    with pytest.raises(InvalidInputError, match="artifact rendering failed"):
        service.render(second_run, _fault_at=fault_at)
    assert _latest(data_paths) == first_latest
    assert not any(data_paths.artifact_tmp_dir.iterdir())
    connection = sqlite3.connect(data_paths.database_file)
    assert connection.execute(
        "SELECT status FROM preparation_runs WHERE preparation_run_id = ?",
        (second_run,),
    ).fetchone() == ("render_failed",)
    assert connection.execute(
        "SELECT status FROM render_attempts WHERE preparation_run_id = ?",
        (second_run,),
    ).fetchone() == ("failed",)
    connection.close()

    second_snapshot = _dict(service.render(second_run)["artifact_snapshot"])
    assert second_snapshot["artifact_snapshot_id"] != first_snapshot["artifact_snapshot_id"]
    assert _latest(data_paths)["artifact_snapshot_id"] == second_snapshot["artifact_snapshot_id"]
    connection = sqlite3.connect(data_paths.database_file)
    hashes = connection.execute(
        "SELECT DISTINCT report_bundle_sha256 FROM render_attempts WHERE preparation_run_id = ?",
        (second_run,),
    ).fetchall()
    statuses = connection.execute(
        "SELECT status FROM render_attempts WHERE preparation_run_id = ? ORDER BY started_at",
        (second_run,),
    ).fetchall()
    connection.close()
    assert len(hashes) == 1
    assert statuses == [("failed",), ("succeeded",)]


def test_committed_snapshot_repairs_latest_without_duplicate_render(
    tmp_path: Path,
    data_paths: DataPaths,
) -> None:
    workspace = _workspace(tmp_path)
    database = Database(data_paths)
    receipt_id = _authorize(database, workspace)
    run_id, _ = _prepare_and_analyze(database, workspace, receipt_id)
    service = ArtifactSnapshotService(database)

    with pytest.raises(InvalidInputError, match="artifact rendering failed"):
        service.render(run_id, _fault_at="after_database_commit")
    assert not data_paths.latest_artifact_file.exists()
    connection = sqlite3.connect(data_paths.database_file)
    assert connection.execute("SELECT status FROM render_attempts").fetchone() == ("succeeded",)
    assert connection.execute("SELECT COUNT(*) FROM artifact_snapshots").fetchone() == (1,)
    connection.close()

    rendered = service.render(run_id)

    snapshot = _dict(rendered["artifact_snapshot"])
    assert _latest(data_paths)["artifact_snapshot_id"] == snapshot["artifact_snapshot_id"]
    connection = sqlite3.connect(data_paths.database_file)
    assert connection.execute("SELECT COUNT(*) FROM render_attempts").fetchone() == (1,)
    assert connection.execute("SELECT COUNT(*) FROM artifact_snapshots").fetchone() == (1,)
    connection.close()


def test_repeated_render_rejects_tampered_or_linked_snapshot_files(
    tmp_path: Path,
    data_paths: DataPaths,
) -> None:
    workspace = _workspace(tmp_path)
    database = Database(data_paths)
    receipt_id = _authorize(database, workspace)
    run_id, _ = _prepare_and_analyze(database, workspace, receipt_id)
    service = ArtifactSnapshotService(database)
    snapshot = _dict(service.render(run_id)["artifact_snapshot"])
    html_path = Path(cast(str, snapshot["html_path"]))
    snapshot_directory = html_path.parent

    snapshot_directory.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
    unexpected = snapshot_directory / "unregistered.txt"
    unexpected.write_text("not in manifest", encoding="utf-8")
    with pytest.raises(InvalidInputError, match="file set does not match"):
        service.render(run_id)
    unexpected.unlink()

    html_path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    html_path.write_text("tampered", encoding="utf-8")
    with pytest.raises(InvalidInputError, match="digest does not match"):
        service.render(run_id)

    html_path.unlink()
    linked_target = snapshot_directory / "manifest.json"
    html_path.symlink_to(linked_target.name)
    with pytest.raises(InvalidInputError, match="unavailable or linked"):
        service.render(run_id)
    assert linked_target.is_file()


def test_render_writer_busy_creates_no_attempt_or_artifact(
    tmp_path: Path,
    data_paths: DataPaths,
) -> None:
    workspace = _workspace(tmp_path)
    database = Database(data_paths)
    receipt_id = _authorize(database, workspace)
    run_id, _ = _prepare_and_analyze(database, workspace, receipt_id)

    with (
        ExclusiveWriterLock(data_paths.writer_lock_file),
        pytest.raises(WriterBusyError),
    ):
        ArtifactSnapshotService(database).render(run_id)

    connection = sqlite3.connect(data_paths.database_file)
    assert connection.execute("SELECT COUNT(*) FROM render_attempts").fetchone() == (0,)
    assert connection.execute(
        "SELECT status FROM preparation_runs WHERE preparation_run_id = ?",
        (run_id,),
    ).fetchone() == ("ready",)
    connection.close()
    assert not any(data_paths.artifact_tmp_dir.iterdir())


def test_carried_forward_evidence_keeps_snapshot_worktree_provenance(
    tmp_path: Path,
    data_paths: DataPaths,
) -> None:
    workspace = _workspace(tmp_path)
    database = Database(data_paths)
    receipt_id = _authorize(database, workspace)
    run_id, old_scan_run_id = _prepare_and_analyze(database, workspace, receipt_id)
    current_scan_run_id = "current-scan-with-different-branch"
    with database.write_transaction() as connection:
        observation = connection.execute(
            """
            SELECT wo.worktree_id, srp.project_id, srp.project_snapshot_id
            FROM worktree_observations AS wo
            JOIN worktrees AS wt ON wt.worktree_id = wo.worktree_id
            JOIN scan_run_projects AS srp
              ON srp.scan_run_id = wo.scan_run_id AND srp.project_id = wt.project_id
            WHERE wo.scan_run_id = ?
            LIMIT 1
            """,
            (old_scan_run_id,),
        ).fetchone()
        assert observation is not None
        worktree_id = str(observation["worktree_id"])
        project_id = str(observation["project_id"])
        snapshot_id = str(observation["project_snapshot_id"])
        connection.execute(
            """
            UPDATE worktree_observations
            SET branch = 'old-frozen-branch', head_commit = ?
            WHERE scan_run_id = ? AND worktree_id = ?
            """,
            ("a" * 40, old_scan_run_id, worktree_id),
        )
        connection.execute(
            """
            INSERT INTO scan_runs(
                scan_run_id, workspace_id, authorization_receipt_id,
                owner_process_identity, mode, change_detection_mode,
                config_revision, started_at, finished_at, status
            )
            SELECT ?, workspace_id, authorization_receipt_id, 'provenance-test',
                   'refresh', 'fast', config_revision,
                   '2026-07-27T01:00:00Z', '2026-07-27T01:00:01Z', 'partial'
            FROM scan_runs WHERE scan_run_id = ?
            """,
            (current_scan_run_id, old_scan_run_id),
        )
        connection.execute(
            """
            INSERT INTO worktree_observations(
                worktree_id, scan_run_id, branch, head_commit, dirty_state,
                history_basis, external_git_dir, external_common_dir,
                external_metadata_receipt_id, external_metadata_confirmed_at,
                external_metadata_read_fields, observed_at
            )
            SELECT worktree_id, ?, 'new-current-branch', ?, dirty_state,
                   history_basis, external_git_dir, external_common_dir,
                   external_metadata_receipt_id, external_metadata_confirmed_at,
                   external_metadata_read_fields, '2026-07-27T01:00:00Z'
            FROM worktree_observations
            WHERE scan_run_id = ? AND worktree_id = ?
            """,
            (current_scan_run_id, "b" * 40, old_scan_run_id, worktree_id),
        )
        connection.execute(
            """
            INSERT INTO scan_run_projects(
                scan_run_id, project_id, snapshot_disposition, project_snapshot_id
            ) VALUES (?, ?, 'carried_forward', ?)
            """,
            (current_scan_run_id, project_id, snapshot_id),
        )
        connection.execute(
            """
            UPDATE preparation_runs SET scan_run_id = ? WHERE preparation_run_id = ?
            """,
            (current_scan_run_id, run_id),
        )
        connection.execute(
            """
            UPDATE preparation_run_projects
            SET snapshot_disposition = 'carried_forward'
            WHERE preparation_run_id = ? AND project_id = ?
            """,
            (run_id, project_id),
        )

    bundle = ReportBundleBuilder(database).build(run_id)
    evidence = [
        _dict(value) for value in _list(bundle["evidence"]) if _dict(value)["worktree"] is not None
    ]
    assert evidence
    worktree = _dict(evidence[0]["worktree"])
    assert worktree["branch"] == "old-frozen-branch"
    assert worktree["head_commit"] == "a" * 40
    assert worktree["observed_scan_run_id"] == old_scan_run_id
    projects = [_dict(value) for value in _list(bundle["projects"])]
    assert projects[0]["snapshot_disposition"] == "carried_forward"


def test_open_context_gap_is_visible_and_downgrades_the_package(
    tmp_path: Path,
    data_paths: DataPaths,
) -> None:
    workspace = _workspace(tmp_path)
    database = Database(data_paths)
    receipt_id = _authorize(database, workspace)
    run_id, _ = _prepare_and_analyze(
        database,
        workspace,
        receipt_id,
        knowledge_gap_severity="medium",
    )

    bundle = ReportBundleBuilder(database).build(run_id)
    limitations = [_dict(value) for value in _list(_dict(bundle["coverage"])["limitations"])]

    assert bundle["package_status"] == "partial"
    assert any(
        limitation["kind"] == "knowledge_gap" and limitation["severity"] == "medium"
        for limitation in limitations
    )
    assert "尚未补充个人角色与学习上下文" in render_report_markdown(bundle)
    snapshot = _dict(ArtifactSnapshotService(database).render(run_id)["artifact_snapshot"])
    assert snapshot["preparation_run_id"] == run_id
    assert _latest(data_paths)["package_status"] == "partial"


def test_dead_render_owner_is_interrupted_and_only_registered_paths_are_cleaned(
    tmp_path: Path,
    data_paths: DataPaths,
) -> None:
    workspace = _workspace(tmp_path)
    database = Database(data_paths)
    receipt_id = _authorize(database, workspace)
    run_id, _ = _prepare_and_analyze(database, workspace, receipt_id)
    bundle = ReportBundleBuilder(database).build(run_id)
    bundle_hash = cast(str, bundle["bundle_sha256"])
    old_attempt_id = "dead-render-attempt"
    snapshot_id = _artifact_snapshot_id(run_id, bundle_hash)
    temp_relative = f"artifacts/.tmp/{old_attempt_id}"
    latest_temp_relative = f"artifacts/.tmp/{old_attempt_id}.latest.tmp"
    final_relative = f"artifacts/{snapshot_id}"
    with database.write_transaction() as connection:
        connection.execute(
            """
            INSERT INTO render_attempts(
                render_attempt_id, preparation_run_id, owner_process_identity,
                report_bundle_sha256, generator_version, temp_relative_path,
                latest_temp_relative_path, final_relative_path, started_at, status
            ) VALUES (?, ?, 'pid:999999;started:Thu Jan  1 00:00:00 1970', ?,
                      'interrupted-test', ?, ?, ?, '2026-07-27T00:00:00Z', 'running')
            """,
            (
                old_attempt_id,
                run_id,
                bundle_hash,
                temp_relative,
                latest_temp_relative,
                final_relative,
            ),
        )
        connection.execute(
            "UPDATE preparation_runs SET status = 'rendering' WHERE preparation_run_id = ?",
            (run_id,),
        )
    protected_marker = data_paths.root / "must-survive-cleanup.txt"
    protected_marker.write_text("keep-root", encoding="utf-8")
    old_temp = data_paths.root / temp_relative
    old_temp.symlink_to(data_paths.root, target_is_directory=True)
    old_latest_temp = data_paths.root / latest_temp_relative
    old_latest_temp.write_text("partial", encoding="utf-8")
    old_final = data_paths.root / final_relative
    old_final.mkdir()
    (old_final / "orphan.tmp").write_text("partial", encoding="utf-8")
    unrelated = data_paths.artifacts_dir / "owner-file.txt"
    unrelated.write_text("keep", encoding="utf-8")

    rendered = ArtifactSnapshotService(database).render(run_id)
    assert _dict(rendered["artifact_snapshot"])["artifact_snapshot_id"] == snapshot_id
    assert protected_marker.read_text(encoding="utf-8") == "keep-root"
    assert data_paths.database_file.is_file()
    assert not old_temp.is_symlink()
    assert unrelated.read_text(encoding="utf-8") == "keep"
    assert not (old_final / "orphan.tmp").exists()
    assert not old_latest_temp.exists()
    connection = sqlite3.connect(data_paths.database_file)
    old_status = connection.execute(
        "SELECT status FROM render_attempts WHERE render_attempt_id = ?",
        (old_attempt_id,),
    ).fetchone()
    new_statuses = connection.execute(
        "SELECT status FROM render_attempts WHERE preparation_run_id = ? ORDER BY started_at",
        (run_id,),
    ).fetchall()
    connection.close()
    assert old_status == ("interrupted",)
    assert new_statuses == [("interrupted",), ("succeeded",)]


@pytest.mark.parametrize("old_status", ["failed", "interrupted"])
def test_terminal_unsnapshotted_attempt_cleanup_is_retried(
    tmp_path: Path,
    data_paths: DataPaths,
    old_status: str,
) -> None:
    workspace = _workspace(tmp_path)
    database = Database(data_paths)
    receipt_id = _authorize(database, workspace)
    run_id, _ = _prepare_and_analyze(database, workspace, receipt_id)
    bundle = ReportBundleBuilder(database).build(run_id)
    bundle_hash = cast(str, bundle["bundle_sha256"])
    old_attempt_id = f"terminal-{old_status}"
    snapshot_id = _artifact_snapshot_id(run_id, bundle_hash)
    temp_relative = f"artifacts/.tmp/{old_attempt_id}"
    latest_temp_relative = f"artifacts/.tmp/{old_attempt_id}.latest.tmp"
    final_relative = f"artifacts/{snapshot_id}"
    with database.write_transaction() as connection:
        connection.execute(
            """
            INSERT INTO render_attempts(
                render_attempt_id, preparation_run_id, owner_process_identity,
                report_bundle_sha256, generator_version, temp_relative_path,
                latest_temp_relative_path, final_relative_path, started_at,
                finished_at, status, error_summary
            ) VALUES (?, ?, 'pid:999999;started:Thu Jan  1 00:00:00 1970', ?,
                      'terminal-cleanup-test', ?, ?, ?, '2026-07-27T00:00:00Z',
                      '2026-07-27T00:00:01Z', ?, 'cleanup was interrupted')
            """,
            (
                old_attempt_id,
                run_id,
                bundle_hash,
                temp_relative,
                latest_temp_relative,
                final_relative,
                old_status,
            ),
        )
        connection.execute(
            """
            UPDATE preparation_runs
            SET status = 'render_failed', status_reason = 'cleanup interrupted'
            WHERE preparation_run_id = ?
            """,
            (run_id,),
        )
    old_temp = data_paths.root / temp_relative
    old_temp.mkdir()
    (old_temp / "partial.tmp").write_text("partial", encoding="utf-8")
    old_latest_temp = data_paths.root / latest_temp_relative
    old_latest_temp.write_text("partial", encoding="utf-8")
    old_final = data_paths.root / final_relative
    old_final.mkdir()
    (old_final / "partial.tmp").write_text("partial", encoding="utf-8")

    snapshot = _dict(ArtifactSnapshotService(database).render(run_id)["artifact_snapshot"])

    assert snapshot["artifact_snapshot_id"] == snapshot_id
    assert not old_temp.exists()
    assert not old_latest_temp.exists()
    connection = sqlite3.connect(data_paths.database_file)
    statuses = connection.execute(
        "SELECT status FROM render_attempts WHERE preparation_run_id = ? ORDER BY started_at",
        (run_id,),
    ).fetchall()
    connection.close()
    assert statuses == [(old_status,), ("succeeded",)]

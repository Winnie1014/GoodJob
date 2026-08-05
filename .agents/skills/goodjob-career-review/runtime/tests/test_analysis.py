from __future__ import annotations

import sqlite3
import uuid
from pathlib import Path
from typing import cast

import pytest

from goodjob.analysis import (
    AnalysisService,
    ClaimDraft,
    EvidenceRelationDraft,
    InlineToken,
    _EvidenceState,
    _reject_verbatim_source_summary,
)
from goodjob.auth import AuthorizationRepository, AuthorizationRequest, generate_capability
from goodjob.context import ContextInterviewService
from goodjob.db import Database
from goodjob.errors import CapabilityError, InvalidInputError
from goodjob.paths import DataPaths
from goodjob.preparation import PreparationService, validate_job_input
from goodjob.scanner import WorkspaceScanner


def _workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "workspace"
    (workspace / "tests").mkdir(parents=True)
    (workspace / "docs").mkdir()
    (workspace / "pyproject.toml").write_text(
        '[project]\nname = "analysis-demo"\nversion = "0.1.0"\n',
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
    (workspace / "docs" / "plan.md").write_text(
        "# Future plan\n\nThis feature is planned but not implemented.\n",
        encoding="utf-8",
    )
    return workspace


def _authorize(
    database: Database,
    workspace: Path,
    capability: bytes | None = None,
) -> tuple[str, bytes]:
    session_capability = capability or generate_capability()
    receipt = AuthorizationRepository(database).issue(
        capability=session_capability,
        request=AuthorizationRequest.from_values(
            receipt_kind="source_analysis",
            scope={
                "workspace_path": str(workspace.resolve()),
                "allowed_categories": ["source_analysis"],
            },
            notice_version="goodjob-source-analysis-v1",
        ),
    )
    return receipt.authorization_receipt_id, session_capability


def _lens() -> dict[str, object]:
    return {
        "contract_version": "role-lens-v1",
        "dimensions": [
            {
                "key": "implementation_depth",
                "display_name": "实现深度",
                "weight_bps": 6000,
                "evaluation_criteria": "评价当前实现机制",
                "required_evidence_kinds": ["implementation"],
            },
            {
                "key": "verification",
                "display_name": "验证能力",
                "weight_bps": 4000,
                "evaluation_criteria": "评价测试定义与结果",
                "required_evidence_kinds": ["test_definition"],
            },
        ],
        "evidence_requirements": ["当前实现", "测试定义"],
        "ranking_rules": ["证据覆盖与岗位相关性共同决定排序"],
        "output_sections": ["项目讲解", "学习要点"],
        "question_strategy": {"primary": "追问实现机制与验证边界"},
        "gap_rules": ["缺少角色或结果时保留知识缺口"],
        "assumptions": ["未提供 JD"],
        "generator_id": "analysis-test",
        "prompt_contract_version": "analysis-test-v1",
    }


def _prepare(
    database: Database,
    workspace: Path,
    receipt_id: str,
) -> tuple[dict[str, object], str, str, str]:
    scan = WorkspaceScanner(database).scan(
        workspace_path=str(workspace),
        config_revision="analysis-scan-v1",
        authorization_receipt_id=receipt_id,
    )
    job = validate_job_input(
        {
            "contract_version": "job-input-v1",
            "target_role": "应用软件工程师",
            "jd_input": {"kind": "none"},
        }
    )["job_input"]
    assert isinstance(job, dict)
    result = PreparationService(database).start(
        workspace_path=workspace,
        authorization_receipt_id=receipt_id,
        request_value={
            "contract_version": "preparation-request-v1",
            "request_id": str(uuid.uuid4()),
            "scan_run_id": scan.scan_run_id,
            "config_revision": "analysis-prepare-v1",
            "target_role": "应用软件工程师",
            "jd_input": {"kind": "none"},
            "job_input_validation_sha256": job["validation_sha256"],
            "requested_exports": [],
            "evidence_limit_per_project": 100,
            "role_lens": _lens(),
        },
    )
    run = _dict(result["preparation_run"])
    lens = _dict(result["role_lens"])
    bundle = _dict(result["evidence_bundle"])
    coverage = [_dict(item) for item in _list(bundle["coverage"])]
    project_id = cast(str, next(item["project_id"] for item in coverage if item["eligible"]))
    return result, cast(str, run["preparation_run_id"]), cast(str, lens["role_lens_id"]), project_id


def _evidence_by_kind(result: dict[str, object]) -> dict[str, dict[str, object]]:
    bundle = _dict(result["evidence_bundle"])
    evidence = [_dict(item) for item in _list(bundle["evidence_items"])]
    selected: dict[str, dict[str, object]] = {}
    for item in evidence:
        selected.setdefault(cast(str, item["evidence_kind"]), item)
    return selected


def _token(text: str) -> list[dict[str, str]]:
    return [{"kind": "text", "value": text}]


def _claim_tokens(
    statement: str,
    *,
    attribution: str,
    scoped_subject: bool,
) -> list[dict[str, str]]:
    if attribution != "none" or not scoped_subject:
        return _token(statement)
    if statement.startswith("该项目"):
        return [
            {"kind": "text", "value": "该项目"},
            {"kind": "text", "value": statement[len("该项目") :]},
        ]
    return [
        {"kind": "text", "value": "该项目"},
        {"kind": "text", "value": f"：{statement}"},
    ]


def _analysis_request(
    *,
    run_id: str,
    lens_id: str,
    project_id: str,
    implementation_id: str,
    test_id: str,
    request_id: str | None = None,
    extra_relations: list[dict[str, object]] | None = None,
    statement: str = "该项目通过可测试的函数边界实现核心计算流程。",
    attribution: str = "none",
    scoped_subject: bool = True,
) -> dict[str, object]:
    relations: list[dict[str, object]] = [
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
    ]
    relations.extend(extra_relations or [])
    evidence_refs = [cast(str, relation["evidence_ref"]) for relation in relations]
    return {
        "contract_version": "analysis-commit-v1",
        "request_id": request_id or str(uuid.uuid4()),
        "preparation_run_id": run_id,
        "role_lens_id": lens_id,
        "evidence_drafts": [],
        "claim_drafts": [
            {
                "draft_id": "claim-main",
                "claim_key": "core-calculation",
                "category": "implementation_method",
                "scope_kind": "project",
                "project_id": project_id,
                "section": "project_story",
                "statement_tokens": _claim_tokens(
                    statement,
                    attribution=attribution,
                    scoped_subject=scoped_subject,
                ),
                "facets": ["implemented", "test_defined"],
                "support_level": "cross_checked",
                "personal_attribution": attribution,
                "review_semantic_projection": {
                    "concept_keys": ["calculation-boundary"],
                    "mechanism_keys": ["function-call"],
                    "behavior_contract_keys": ["input-to-output"],
                    "tradeoff_keys": [],
                    "technology_identifiers": ["python"],
                },
                "evidence_relations": relations,
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
                "evidence_refs": evidence_refs,
                "gap_refs": [],
                "rationale_tokens": _token("实现和测试定义均有当前证据。"),
            }
        ],
        "knowledge_gaps": [],
    }


def _dict(value: object) -> dict[str, object]:
    assert isinstance(value, dict)
    return cast(dict[str, object], value)


def _list(value: object) -> list[object]:
    assert isinstance(value, list)
    return cast(list[object], value)


def test_context_interview_appends_facts_and_freezes_them_for_later_runs(
    tmp_path: Path,
    data_paths: DataPaths,
) -> None:
    workspace = _workspace(tmp_path)
    database = Database(data_paths)
    receipt_id, capability = _authorize(database, workspace)
    _, run_id, _, project_id = _prepare(database, workspace, receipt_id)
    service = ContextInterviewService(database)
    interview_request = {
        "contract_version": "context-interview-request-v1",
        "request_id": str(uuid.uuid4()),
        "preparation_run_id": run_id,
        "question_set_version": "project-context-v1",
        "cards": [
            {
                "project_id": project_id,
                "questions": [
                    {
                        "question_id": "role-and-learning",
                        "fact_kinds": ["role", "learning"],
                        "prompt": "你在项目中的职责以及主要学习是什么？",
                    }
                ],
            }
        ],
    }
    interview = service.request_context(
        authorization_receipt_id=receipt_id,
        request_value=interview_request,
    )
    interview_id = cast(str, _dict(interview["context_interview"])["context_interview_id"])
    assert _dict(interview["context_interview"])["status"] == "open"

    with pytest.raises(InvalidInputError, match="fact kind requested"):
        service.record_context(
            authorization_receipt_id=receipt_id,
            request_value={
                "contract_version": "interview-input-v1",
                "request_id": str(uuid.uuid4()),
                "mode": "context",
                "preparation_run_id": run_id,
                "context_interview_id": interview_id,
                "answers": [
                    {
                        "project_id": project_id,
                        "status": "answered",
                        "structured_answer": {"metric": "并未被提问"},
                        "facts": [
                            {
                                "fact_key": "unasked-metric",
                                "fact_kind": "metric",
                                "statement": "这个指标没有对应访谈问题。",
                            }
                        ],
                    }
                ],
            },
        )

    answer_request = {
        "contract_version": "interview-input-v1",
        "request_id": str(uuid.uuid4()),
        "mode": "context",
        "preparation_run_id": run_id,
        "context_interview_id": interview_id,
        "answers": [
            {
                "project_id": project_id,
                "status": "answered",
                "structured_answer": {"role": "负责核心计算模块", "learning": "边界测试"},
                "facts": [
                    {
                        "fact_key": "owner-role",
                        "fact_kind": "role",
                        "statement": "负责核心计算模块的实现与验证。",
                    },
                    {
                        "fact_key": "owner-learning",
                        "fact_kind": "learning",
                        "statement": "从项目中系统学习了边界测试。",
                    },
                ],
            }
        ],
    }
    answered = service.record_context(
        authorization_receipt_id=receipt_id,
        request_value=answer_request,
    )
    assert answered["run_status"] == "analyzing"
    answer_result = _dict(_list(_dict(answered["context_answer_batch"])["answers"])[0])
    returned_facts = [_dict(item) for item in _list(answer_result["facts"])]
    assert {fact["fact_kind"] for fact in returned_facts} == {"role", "learning"}
    assert all(isinstance(fact["evidence_id"], str) for fact in returned_facts)

    connection = sqlite3.connect(data_paths.database_file)
    assert connection.execute("SELECT COUNT(*) FROM context_answers").fetchone()[0] == 1
    assert connection.execute("SELECT COUNT(*) FROM project_context_facts").fetchone()[0] == 2
    assert connection.execute("SELECT COUNT(*) FROM evidence_contexts").fetchone()[0] == 2
    assert (
        connection.execute(
            "SELECT COUNT(*) FROM preparation_context_facts WHERE preparation_run_id = ?",
            (run_id,),
        ).fetchone()[0]
        == 2
    )
    connection.close()

    other_receipt, _ = _authorize(database, workspace)
    with pytest.raises(CapabilityError):
        service.record_context(
            authorization_receipt_id=other_receipt,
            request_value=answer_request,
        )

    same_session_receipt, _ = _authorize(database, workspace, capability)
    next_prepared, next_run_id, _, _ = _prepare(database, workspace, same_session_receipt)
    connection = sqlite3.connect(data_paths.database_file)
    assert (
        connection.execute(
            "SELECT COUNT(*) FROM preparation_context_facts WHERE preparation_run_id = ?",
            (next_run_id,),
        ).fetchone()[0]
        == 2
    )
    connection.close()
    next_bundle = _dict(next_prepared["evidence_bundle"])
    context_items = [
        _dict(item)
        for item in _list(next_bundle["evidence_items"])
        if _dict(item)["evidence_kind"] == "user_statement"
    ]
    assert len(context_items) == 2
    assert {_dict(item["context_fact"])["fact_kind"] for item in context_items} == {
        "role",
        "learning",
    }
    assert any(
        "核心计算模块" in str(_dict(item["context_fact"])["statement"]) for item in context_items
    )


def test_record_analysis_atomically_freezes_claims_assessment_and_retry(
    tmp_path: Path,
    data_paths: DataPaths,
) -> None:
    workspace = _workspace(tmp_path)
    database = Database(data_paths)
    receipt_id, _ = _authorize(database, workspace)
    prepared, run_id, lens_id, project_id = _prepare(database, workspace, receipt_id)
    evidence = _evidence_by_kind(prepared)
    request_id = str(uuid.uuid4())
    request = _analysis_request(
        run_id=run_id,
        lens_id=lens_id,
        project_id=project_id,
        implementation_id=cast(str, evidence["implementation"]["evidence_id"]),
        test_id=cast(str, evidence["test_definition"]["evidence_id"]),
        request_id=request_id,
    )

    service = AnalysisService(database)
    first = service.record_analysis(
        workspace_path=workspace,
        authorization_receipt_id=receipt_id,
        request_value=request,
    )
    repeated = service.record_analysis(
        workspace_path=workspace,
        authorization_receipt_id=receipt_id,
        request_value=request,
    )

    assert first == repeated
    assert first["run_status"] == "ready"
    assessment = _dict(_list(first["project_assessments"])[0])
    assert assessment["coverage_bps"] == 10000
    assert assessment["base_score_milli"] == 760
    assert assessment["final_score_milli"] == 760
    assert assessment["rank"] == 1
    connection = sqlite3.connect(data_paths.database_file)
    counts = {
        table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in (
            "analysis_commits",
            "claims",
            "claim_revisions",
            "claim_evidence",
            "preparation_claims",
            "project_assessments",
        )
    }
    stored_tokens = connection.execute("SELECT statement_tokens FROM claim_revisions").fetchone()[0]
    connection.close()
    assert counts == {
        "analysis_commits": 1,
        "claims": 1,
        "claim_revisions": 1,
        "claim_evidence": 2,
        "preparation_claims": 1,
        "project_assessments": 1,
    }
    assert "<script>" not in stored_tokens


@pytest.mark.parametrize(
    ("prose_before", "code_value", "prose_after"),
    (
        ("通过 ", "rg -i", " 做大小写不敏感检索并汇总候选文件。"),
        ("在遍历中使用 ", "for (i = 0; i < n; i++)", " 控制候选批次。"),
        ("详见 ", "rg -i", " 的用法。"),
    ),
)
def test_non_personal_claim_ignores_code_tokens_for_prose_attribution(
    tmp_path: Path,
    data_paths: DataPaths,
    prose_before: str,
    code_value: str,
    prose_after: str,
) -> None:
    workspace = _workspace(tmp_path)
    database = Database(data_paths)
    receipt_id, _ = _authorize(database, workspace)
    prepared, run_id, lens_id, project_id = _prepare(database, workspace, receipt_id)
    evidence = _evidence_by_kind(prepared)
    request = _analysis_request(
        run_id=run_id,
        lens_id=lens_id,
        project_id=project_id,
        implementation_id=cast(str, evidence["implementation"]["evidence_id"]),
        test_id=cast(str, evidence["test_definition"]["evidence_id"]),
    )
    statement_tokens = [
        {"kind": "text", "value": "该项目"},
        {"kind": "text", "value": prose_before},
        {"kind": "code", "value": code_value},
        {"kind": "text", "value": prose_after},
    ]
    _dict(_list(request["claim_drafts"])[0])["statement_tokens"] = statement_tokens

    result = AnalysisService(database).record_analysis(
        workspace_path=workspace,
        authorization_receipt_id=receipt_id,
        request_value=request,
    )

    assert result["run_status"] == "ready"
    connection = sqlite3.connect(data_paths.database_file)
    stored_statement = connection.execute("SELECT statement FROM claim_revisions").fetchone()[0]
    connection.close()
    assert stored_statement == "该项目" + prose_before + code_value + prose_after


def test_non_personal_claim_rejects_personal_attribution_across_code_seam(
    tmp_path: Path,
    data_paths: DataPaths,
) -> None:
    workspace = _workspace(tmp_path)
    database = Database(data_paths)
    receipt_id, _ = _authorize(database, workspace)
    prepared, run_id, lens_id, project_id = _prepare(database, workspace, receipt_id)
    evidence = _evidence_by_kind(prepared)
    request = _analysis_request(
        run_id=run_id,
        lens_id=lens_id,
        project_id=project_id,
        implementation_id=cast(str, evidence["implementation"]["evidence_id"]),
        test_id=cast(str, evidence["test_definition"]["evidence_id"]),
    )
    _dict(_list(request["claim_drafts"])[0])["statement_tokens"] = [
        {"kind": "text", "value": "该项目"},
        {"kind": "text", "value": "结果"},
        {"kind": "code", "value": "X"},
        {"kind": "text", "value": "I led this migration."},
    ]

    with pytest.raises(InvalidInputError, match="stronger personal attribution"):
        AnalysisService(database).record_analysis(
            workspace_path=workspace,
            authorization_receipt_id=receipt_id,
            request_value=request,
        )


@pytest.mark.parametrize(
    ("token_values", "expected"),
    (
        (
            (
                {"kind": "text", "value": "我"},
                {"kind": "emphasis", "value": "实现了核心计算流程。"},
            ),
            "implemented",
        ),
        (
            (
                {"kind": "text", "value": "我负"},
                {"kind": "code", "value": "X"},
                {"kind": "emphasis", "value": "责核心计算流程。"},
            ),
            "responsible",
        ),
        (
            (
                {"kind": "text", "value": "我主"},
                {"kind": "code", "value": "X"},
                {"kind": "emphasis", "value": "导了架构重构。"},
            ),
            "led",
        ),
        (
            (
                {"kind": "text", "value": "我当时"},
                {"kind": "code", "value": "X"},
                {"kind": "emphasis", "value": "学到了很多。"},
            ),
            "personal_learning",
        ),
    ),
)
def test_personal_attribution_projection_union_preserves_existing_classification(
    token_values: tuple[dict[str, str], ...],
    expected: str,
) -> None:
    tokens = tuple(
        InlineToken.from_value(value, f"tokens[{index}]")
        for index, value in enumerate(token_values)
    )
    projections = AnalysisService._personal_attribution_projections(tokens)

    assert AnalysisService._detected_personal_attribution(*projections) == expected


def test_responsible_declaration_does_not_cover_split_implemented_statement(
    tmp_path: Path,
    data_paths: DataPaths,
) -> None:
    workspace = _workspace(tmp_path)
    database = Database(data_paths)
    receipt_id, _ = _authorize(database, workspace)
    prepared, run_id, lens_id, project_id = _prepare(database, workspace, receipt_id)
    evidence = _evidence_by_kind(prepared)
    request = _analysis_request(
        run_id=run_id,
        lens_id=lens_id,
        project_id=project_id,
        implementation_id=cast(str, evidence["implementation"]["evidence_id"]),
        test_id=cast(str, evidence["test_definition"]["evidence_id"]),
        attribution="responsible",
    )
    _dict(_list(request["claim_drafts"])[0])["statement_tokens"] = [
        {"kind": "text", "value": "我"},
        {"kind": "emphasis", "value": "实现了核心计算流程。"},
    ]

    with pytest.raises(InvalidInputError, match="stronger personal attribution"):
        AnalysisService(database).record_analysis(
            workspace_path=workspace,
            authorization_receipt_id=receipt_id,
            request_value=request,
        )


def test_non_personal_claim_still_requires_text_scope_subject_before_code(
    tmp_path: Path,
    data_paths: DataPaths,
) -> None:
    workspace = _workspace(tmp_path)
    database = Database(data_paths)
    receipt_id, _ = _authorize(database, workspace)
    prepared, run_id, lens_id, project_id = _prepare(database, workspace, receipt_id)
    evidence = _evidence_by_kind(prepared)
    request = _analysis_request(
        run_id=run_id,
        lens_id=lens_id,
        project_id=project_id,
        implementation_id=cast(str, evidence["implementation"]["evidence_id"]),
        test_id=cast(str, evidence["test_definition"]["evidence_id"]),
    )
    _dict(_list(request["claim_drafts"])[0])["statement_tokens"] = [
        {"kind": "code", "value": "我实现了"},
        {"kind": "text", "value": "核心计算流程。"},
    ]

    with pytest.raises(InvalidInputError):
        AnalysisService(database).record_analysis(
            workspace_path=workspace,
            authorization_receipt_id=receipt_id,
            request_value=request,
        )


def test_record_analysis_rejects_another_session_before_history_revalidation(
    tmp_path: Path,
    data_paths: DataPaths,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = _workspace(tmp_path)
    database = Database(data_paths)
    receipt_id, _ = _authorize(database, workspace)
    prepared, run_id, lens_id, project_id = _prepare(database, workspace, receipt_id)
    evidence = _evidence_by_kind(prepared)
    request = _analysis_request(
        run_id=run_id,
        lens_id=lens_id,
        project_id=project_id,
        implementation_id=cast(str, evidence["implementation"]["evidence_id"]),
        test_id=cast(str, evidence["test_definition"]["evidence_id"]),
    )
    other_receipt, _ = _authorize(database, workspace)

    def unexpected_history_revalidation(*_: object) -> dict[tuple[str, str], dict[str, object]]:
        raise AssertionError("history must not be touched before session validation")

    monkeypatch.setattr(
        AnalysisService,
        "_verify_history_proofs",
        unexpected_history_revalidation,
    )
    with pytest.raises(CapabilityError):
        AnalysisService(database).record_analysis(
            workspace_path=workspace,
            authorization_receipt_id=other_receipt,
            request_value=request,
        )


def test_deep_read_evidence_requires_before_read_and_persists_only_pointer_data(
    tmp_path: Path,
    data_paths: DataPaths,
) -> None:
    workspace = _workspace(tmp_path)
    database = Database(data_paths)
    receipt_id, _ = _authorize(database, workspace)
    prepared, run_id, lens_id, project_id = _prepare(database, workspace, receipt_id)
    bundle = _dict(prepared["evidence_bundle"])
    suggestions = [_dict(item) for item in _list(bundle["deep_read_suggestions"])]
    app_suggestion = next(item for item in suggestions if item["relative_path"] == "app.py")
    source_revision_id = cast(str, app_suggestion["source_revision_id"])
    PreparationService(database).verify_source_revisions(
        preparation_run_id=run_id,
        authorization_receipt_id=receipt_id,
        source_revision_ids=(source_revision_id,),
        phase="before_read",
    )
    evidence = _evidence_by_kind(prepared)
    test_id = cast(str, evidence["test_definition"]["evidence_id"])
    request = _analysis_request(
        run_id=run_id,
        lens_id=lens_id,
        project_id=project_id,
        implementation_id="deep-app",
        test_id=test_id,
        statement="该实现保留 </script><img onerror=alert(1)> 作为普通不可信文本。",
    )
    request["evidence_drafts"] = [
        {
            "draft_id": "deep-app",
            "origin_kind": "source_revision",
            "project_id": project_id,
            "worktree_id": app_suggestion["worktree_id"],
            "module_id": app_suggestion["module_id"],
            "evidence_kind": "implementation",
            "locator": {
                "worktree_id": app_suggestion["worktree_id"],
                "relative_path": "app.py",
                "workspace_relative_path": app_suggestion["workspace_relative_path"],
                "start_line": 1,
                "end_line": 2,
                "symbol": "calculate",
            },
            "summary": "The selected function defines the bounded calculation implementation.",
            "commit_state": evidence["implementation"]["commit_state"],
            "source_revision_id": source_revision_id,
            "observed_sha256": app_suggestion["content_sha256"],
        }
    ]

    coverage = [_dict(item) for item in _list(bundle["coverage"])]
    project_snapshot_id = cast(
        str,
        next(item["project_snapshot_id"] for item in coverage if item["project_id"] == project_id),
    )
    unrelated_module_id = str(uuid.uuid4())
    connection = sqlite3.connect(data_paths.database_file)
    adapter_id = connection.execute(
        "SELECT adapter_id FROM module_observations WHERE project_snapshot_id = ? LIMIT 1",
        (project_snapshot_id,),
    ).fetchone()[0]
    connection.execute(
        "INSERT INTO modules(module_id, project_id, module_key, name, kind) VALUES (?, ?, ?, ?, ?)",
        (unrelated_module_id, project_id, "unrelated", "unrelated", "package"),
    )
    connection.execute(
        """
        INSERT INTO module_observations(
            module_id, project_snapshot_id, relative_root, manifest_evidence_id, adapter_id
        ) VALUES (?, ?, 'unrelated', NULL, ?)
        """,
        (unrelated_module_id, project_snapshot_id, adapter_id),
    )
    connection.commit()
    connection.close()
    wrong_module_request = dict(request)
    wrong_module_draft = dict(_dict(_list(request["evidence_drafts"])[0]))
    wrong_module_draft["module_id"] = unrelated_module_id
    wrong_module_request["request_id"] = str(uuid.uuid4())
    wrong_module_request["evidence_drafts"] = [wrong_module_draft]
    with pytest.raises(InvalidInputError, match="scanner-observed file module"):
        AnalysisService(database).record_analysis(
            workspace_path=workspace,
            authorization_receipt_id=receipt_id,
            request_value=wrong_module_request,
        )

    source_excerpt_request = dict(request)
    source_excerpt_draft = dict(_dict(_list(request["evidence_drafts"])[0]))
    source_excerpt_draft["summary"] = "def calculate(value: int) -> int: return value + 1"
    source_excerpt_request["request_id"] = str(uuid.uuid4())
    source_excerpt_request["evidence_drafts"] = [source_excerpt_draft]
    with pytest.raises(InvalidInputError, match="source or diff text"):
        AnalysisService(database).record_analysis(
            workspace_path=workspace,
            authorization_receipt_id=receipt_id,
            request_value=source_excerpt_request,
        )

    result = AnalysisService(database).record_analysis(
        workspace_path=workspace,
        authorization_receipt_id=receipt_id,
        request_value=request,
    )

    assert result["run_status"] == "ready"
    database_bytes = data_paths.database_file.read_bytes()
    assert b"def calculate(value: int)" not in database_bytes
    connection = sqlite3.connect(data_paths.database_file)
    stored = connection.execute(
        """
        SELECT acquisition_scope, preparation_run_id, source_revision_id,
               locator, summary
        FROM evidence WHERE preparation_run_id = ?
        """,
        (run_id,),
    ).fetchone()
    connection.close()
    assert stored is not None
    assert stored[0:3] == ("preparation", run_id, source_revision_id)
    assert "app.py" in stored[3]
    assert "def calculate" not in stored[4]


@pytest.mark.parametrize("invalid_case", ["plan_as_implemented", "definition_as_verified"])
def test_invalid_facets_reject_the_entire_batch_without_commit_checks(
    tmp_path: Path,
    data_paths: DataPaths,
    invalid_case: str,
) -> None:
    workspace = _workspace(tmp_path)
    database = Database(data_paths)
    receipt_id, _ = _authorize(database, workspace)
    prepared, run_id, lens_id, project_id = _prepare(database, workspace, receipt_id)
    evidence = _evidence_by_kind(prepared)
    selected = (
        evidence["documentation"]
        if invalid_case == "plan_as_implemented"
        else evidence["test_definition"]
    )
    facet = "implemented" if invalid_case == "plan_as_implemented" else "test_verified"
    request = _analysis_request(
        run_id=run_id,
        lens_id=lens_id,
        project_id=project_id,
        implementation_id=cast(str, evidence["implementation"]["evidence_id"]),
        test_id=cast(str, evidence["test_definition"]["evidence_id"]),
    )
    claim = _dict(_list(request["claim_drafts"])[0])
    claim["facets"] = [facet]
    claim["support_level"] = "single_source"
    claim["evidence_relations"] = [
        {
            "evidence_ref": selected["evidence_id"],
            "relation": "supports",
            "supported_facets": [facet],
        }
    ]
    assessment = _dict(_list(request["project_assessments"])[0])
    assessment["evidence_refs"] = [selected["evidence_id"]]
    assessment["coverage_bps"] = 0 if invalid_case == "plan_as_implemented" else 4000

    with pytest.raises(InvalidInputError):
        AnalysisService(database).record_analysis(
            workspace_path=workspace,
            authorization_receipt_id=receipt_id,
            request_value=request,
        )

    connection = sqlite3.connect(data_paths.database_file)
    assert connection.execute("SELECT COUNT(*) FROM analysis_commits").fetchone()[0] == 0
    assert connection.execute("SELECT COUNT(*) FROM claims").fetchone()[0] == 0
    assert connection.execute("SELECT COUNT(*) FROM project_assessments").fetchone()[0] == 0
    assert (
        connection.execute(
            """
            SELECT COUNT(*) FROM preparation_source_checks
            WHERE preparation_run_id = ? AND phase = 'commit'
            """,
            (run_id,),
        ).fetchone()[0]
        == 0
    )
    assert (
        connection.execute(
            "SELECT status FROM preparation_runs WHERE preparation_run_id = ?",
            (run_id,),
        ).fetchone()[0]
        == "analyzing"
    )
    connection.close()


def test_commit_source_drift_requires_refresh_and_leaves_zero_analysis_entities(
    tmp_path: Path,
    data_paths: DataPaths,
) -> None:
    workspace = _workspace(tmp_path)
    database = Database(data_paths)
    receipt_id, _ = _authorize(database, workspace)
    prepared, run_id, lens_id, project_id = _prepare(database, workspace, receipt_id)
    evidence = _evidence_by_kind(prepared)
    request = _analysis_request(
        run_id=run_id,
        lens_id=lens_id,
        project_id=project_id,
        implementation_id=cast(str, evidence["implementation"]["evidence_id"]),
        test_id=cast(str, evidence["test_definition"]["evidence_id"]),
    )
    (workspace / "app.py").write_text("def changed():\n    return False\n", encoding="utf-8")

    result = AnalysisService(database).record_analysis(
        workspace_path=workspace,
        authorization_receipt_id=receipt_id,
        request_value=request,
    )

    assert result["run_status"] == "refresh_required"
    assert result["analysis_commit"] is None
    connection = sqlite3.connect(data_paths.database_file)
    assert connection.execute("SELECT COUNT(*) FROM analysis_commits").fetchone()[0] == 0
    assert connection.execute("SELECT COUNT(*) FROM claims").fetchone()[0] == 0
    assert connection.execute("SELECT COUNT(*) FROM project_assessments").fetchone()[0] == 0
    assert (
        connection.execute(
            """
            SELECT COUNT(*) FROM preparation_source_mismatches AS sm
            JOIN preparation_source_checks AS sc ON sc.source_check_id = sm.source_check_id
            WHERE sc.preparation_run_id = ? AND sc.phase = 'commit'
            """,
            (run_id,),
        ).fetchone()[0]
        == 1
    )
    connection.close()


def test_personal_implementation_requires_and_accepts_bound_role_context(
    tmp_path: Path,
    data_paths: DataPaths,
) -> None:
    workspace = _workspace(tmp_path)
    database = Database(data_paths)
    receipt_id, _ = _authorize(database, workspace)
    prepared, run_id, lens_id, project_id = _prepare(database, workspace, receipt_id)
    evidence = _evidence_by_kind(prepared)
    implementation_id = cast(str, evidence["implementation"]["evidence_id"])
    test_id = cast(str, evidence["test_definition"]["evidence_id"])
    service = AnalysisService(database)
    for statement in (
        "核心计算流程由我独立完成。",
        "核心计算流程由本人打造。",
        "I created the core calculation flow.",
        "独立完成核心计算流程。",
        "Built the core calculation flow.",
        "Delivered the production pipeline.",
        "1. Built the core calculation flow.",
        "在该项目中独立完成核心计算流程。",
        "核心职责包括独立完成核心计算流程。",
        "In this project, built the core calculation flow.",
    ):
        undeclared = _analysis_request(
            run_id=run_id,
            lens_id=lens_id,
            project_id=project_id,
            implementation_id=implementation_id,
            test_id=test_id,
            statement=statement,
            scoped_subject=False,
        )
        with pytest.raises(InvalidInputError, match="scope subject token"):
            service.record_analysis(
                workspace_path=workspace,
                authorization_receipt_id=receipt_id,
                request_value=undeclared,
            )
    for deceptive_tokens, error_pattern in (
        (
            [
                {"kind": "text", "value": "在该项目中"},
                {"kind": "text", "value": "独立完成核心计算流程。"},
            ],
            "scope subject token",
        ),
        (
            [
                {"kind": "text", "value": "该模块"},
                {"kind": "text", "value": "完成核心计算流程。"},
            ],
            "scope subject token",
        ),
        (
            [
                {"kind": "scope_ref", "value": "该项目", "ref_id": project_id},
                {"kind": "text", "value": "完成核心计算流程。"},
            ],
            "supported ReportInlineToken",
        ),
    ):
        deceptive = _analysis_request(
            run_id=run_id,
            lens_id=lens_id,
            project_id=project_id,
            implementation_id=implementation_id,
            test_id=test_id,
        )
        _dict(_list(deceptive["claim_drafts"])[0])["statement_tokens"] = deceptive_tokens
        with pytest.raises(InvalidInputError, match=error_pattern):
            service.record_analysis(
                workspace_path=workspace,
                authorization_receipt_id=receipt_id,
                request_value=deceptive,
            )
    unsupported = _analysis_request(
        run_id=run_id,
        lens_id=lens_id,
        project_id=project_id,
        implementation_id=implementation_id,
        test_id=test_id,
        statement="我实现了核心计算流程。",
        attribution="implemented",
    )
    with pytest.raises(InvalidInputError, match="role/ownership"):
        service.record_analysis(
            workspace_path=workspace,
            authorization_receipt_id=receipt_id,
            request_value=unsupported,
        )

    context = ContextInterviewService(database)
    requested = context.request_context(
        authorization_receipt_id=receipt_id,
        request_value={
            "contract_version": "context-interview-request-v1",
            "request_id": str(uuid.uuid4()),
            "preparation_run_id": run_id,
            "question_set_version": "role-v1",
            "cards": [
                {
                    "project_id": project_id,
                    "questions": [
                        {
                            "question_id": "owner-role",
                            "fact_kinds": ["role"],
                            "prompt": "你在项目中承担什么职责？",
                        }
                    ],
                }
            ],
        },
    )
    interview_id = cast(str, _dict(requested["context_interview"])["context_interview_id"])
    context_result = context.record_context(
        authorization_receipt_id=receipt_id,
        request_value={
            "contract_version": "interview-input-v1",
            "request_id": str(uuid.uuid4()),
            "mode": "context",
            "preparation_run_id": run_id,
            "context_interview_id": interview_id,
            "answers": [
                {
                    "project_id": project_id,
                    "status": "answered",
                    "structured_answer": {"role": "负责核心计算流程"},
                    "facts": [
                        {
                            "fact_key": "owner-role",
                            "fact_kind": "role",
                            "statement": "负责核心计算流程。",
                        }
                    ],
                }
            ],
        },
    )
    returned_answer = _dict(_list(_dict(context_result["context_answer_batch"])["answers"])[0])
    role_fact = _dict(_list(returned_answer["facts"])[0])
    role_evidence_id = cast(str, role_fact["evidence_id"])

    contradicted = _analysis_request(
        run_id=run_id,
        lens_id=lens_id,
        project_id=project_id,
        implementation_id=implementation_id,
        test_id=test_id,
        statement="我实现了核心计算流程。",
        attribution="implemented",
        extra_relations=[
            {
                "evidence_ref": role_evidence_id,
                "relation": "contradicts",
                "supported_facets": [],
            }
        ],
    )
    _dict(_list(contradicted["claim_drafts"])[0])["support_level"] = "conflicted"
    with pytest.raises(InvalidInputError):
        service.record_analysis(
            workspace_path=workspace,
            authorization_receipt_id=receipt_id,
            request_value=contradicted,
        )

    supported = _analysis_request(
        run_id=run_id,
        lens_id=lens_id,
        project_id=project_id,
        implementation_id=implementation_id,
        test_id=test_id,
        statement="我实现了核心计算流程。",
        attribution="implemented",
        extra_relations=[
            {
                "evidence_ref": role_evidence_id,
                "relation": "contextualizes",
                "supported_facets": [],
            }
        ],
    )

    result = service.record_analysis(
        workspace_path=workspace,
        authorization_receipt_id=receipt_id,
        request_value=supported,
    )

    assert result["run_status"] == "ready"


def test_git_summary_rejects_transient_diff_or_blob_excerpt() -> None:
    for summary, source in (
        (
            "def issue_token(secret): return sign(secret)",
            "+def issue_token(secret): return sign(secret)",
        ),
        (
            "API_TOKEN=do-not-persist-this-value",
            "API_TOKEN=do-not-persist-this-value",
        ),
        (
            "Observed excerpt: issue_token(secret): return sign(secret)",
            "def issue_token(secret): return sign(secret) # complete implementation",
        ),
    ):
        with pytest.raises(InvalidInputError, match="source or diff text"):
            _reject_verbatim_source_summary(summary, source)


def test_skipped_context_requires_a_visible_open_project_gap(
    tmp_path: Path,
    data_paths: DataPaths,
) -> None:
    workspace = _workspace(tmp_path)
    database = Database(data_paths)
    receipt_id, _ = _authorize(database, workspace)
    prepared, run_id, lens_id, project_id = _prepare(database, workspace, receipt_id)
    evidence = _evidence_by_kind(prepared)
    context = ContextInterviewService(database)
    requested = context.request_context(
        authorization_receipt_id=receipt_id,
        request_value={
            "contract_version": "context-interview-request-v1",
            "request_id": str(uuid.uuid4()),
            "preparation_run_id": run_id,
            "question_set_version": "outcome-v1",
            "cards": [
                {
                    "project_id": project_id,
                    "questions": [
                        {
                            "question_id": "project-outcome",
                            "fact_kinds": ["outcome", "metric"],
                            "prompt": "这个项目取得了什么结果？",
                        }
                    ],
                }
            ],
        },
    )
    interview_id = cast(str, _dict(requested["context_interview"])["context_interview_id"])
    context.record_context(
        authorization_receipt_id=receipt_id,
        request_value={
            "contract_version": "interview-input-v1",
            "request_id": str(uuid.uuid4()),
            "mode": "context",
            "preparation_run_id": run_id,
            "context_interview_id": interview_id,
            "answers": [
                {
                    "project_id": project_id,
                    "status": "skipped",
                    "structured_answer": {},
                    "facts": [],
                }
            ],
        },
    )
    request = _analysis_request(
        run_id=run_id,
        lens_id=lens_id,
        project_id=project_id,
        implementation_id=cast(str, evidence["implementation"]["evidence_id"]),
        test_id=cast(str, evidence["test_definition"]["evidence_id"]),
    )
    service = AnalysisService(database)
    with pytest.raises(InvalidInputError, match="visible open project gap"):
        service.record_analysis(
            workspace_path=workspace,
            authorization_receipt_id=receipt_id,
            request_value=request,
        )

    request["request_id"] = str(uuid.uuid4())
    request["knowledge_gaps"] = [
        {
            "draft_id": "gap-project-outcome",
            "scope_kind": "project",
            "project_id": project_id,
            "dimension": "implementation_depth",
            "stable_gap_concept_key": "project-outcome",
            "gap_contract_version": "project-context-gap-v1",
            "description_tokens": _token("用户跳过了项目结果问题。"),
            "severity": "medium",
            "resolution_kind": "owner_follow_up",
            "status": "open",
        }
    ]
    assessment = _dict(_list(request["project_assessments"])[0])
    assessment["gap_refs"] = ["gap-project-outcome"]
    result = service.record_analysis(
        workspace_path=workspace,
        authorization_receipt_id=receipt_id,
        request_value=request,
    )
    assert result["run_status"] == "ready"


def test_verified_review_semantics_survive_wording_only_claim_revision(
    tmp_path: Path,
    data_paths: DataPaths,
) -> None:
    workspace = _workspace(tmp_path)
    database = Database(data_paths)
    receipt_id, _ = _authorize(database, workspace)
    service = AnalysisService(database)
    observed: list[dict[str, object]] = []
    for statement in (
        "该项目使用 Python 建立可测试的计算边界。",
        "基于 Python 的计算入口具备独立测试定义。",
    ):
        prepared, run_id, lens_id, project_id = _prepare(database, workspace, receipt_id)
        evidence = _evidence_by_kind(prepared)
        implementation_id = cast(str, evidence["implementation"]["evidence_id"])
        request = _analysis_request(
            run_id=run_id,
            lens_id=lens_id,
            project_id=project_id,
            implementation_id=implementation_id,
            test_id=cast(str, evidence["test_definition"]["evidence_id"]),
            statement=statement,
        )
        claim = _dict(_list(request["claim_drafts"])[0])
        claim["review_semantic_projection"] = {
            "concept_keys": [],
            "mechanism_keys": [],
            "behavior_contract_keys": [],
            "tradeoff_keys": [],
            "technology_identifiers": ["python"],
            "verification_anchors": {"python": [implementation_id]},
        }
        result = service.record_analysis(
            workspace_path=workspace,
            authorization_receipt_id=receipt_id,
            request_value=request,
        )
        observed.append(_dict(_list(result["claims"])[0]))

    assert observed[0]["claim_id"] == observed[1]["claim_id"]
    assert observed[0]["claim_revision_id"] != observed[1]["claim_revision_id"]
    assert observed[0]["review_semantic_sha256"] == observed[1]["review_semantic_sha256"]


def test_module_claim_requires_worktree_scope_without_equivalent_branch_coverage() -> None:
    claim_value: dict[str, object] = {
        "draft_id": "claim-module",
        "claim_key": "module-mechanism",
        "category": "implementation_method",
        "scope_kind": "module",
        "project_id": "project",
        "module_id": "module",
        "section": "project_story",
        "statement_tokens": _token("模块实现了核心机制。"),
        "facets": ["implemented"],
        "support_level": "single_source",
        "personal_attribution": "none",
        "review_semantic_projection": {
            "concept_keys": [],
            "mechanism_keys": [],
            "behavior_contract_keys": [],
            "tradeoff_keys": [],
            "technology_identifiers": [],
        },
        "evidence_relations": [
            {
                "evidence_ref": "evidence-a",
                "relation": "supports",
                "supported_facets": ["implemented"],
            }
        ],
    }

    def evidence_state(
        reference: str,
        worktree_id: str,
        equivalence_key: str,
    ) -> _EvidenceState:
        return _EvidenceState(
            reference=reference,
            evidence_id=reference,
            project_id="project",
            worktree_id=worktree_id,
            module_id="module",
            source_revision_id=f"revision-{reference}",
            origin_kind="source_revision",
            evidence_kind="implementation",
            artifact_kind="source",
            locator={"relative_path": "src/main.py"},
            summary="module implementation",
            commit_state="committed",
            validity="current",
            content_equivalence_key=equivalence_key,
            context_fact_id=None,
            context_fact_kind=None,
            anchor_sha256="a" * 64,
        )

    def relation_value(reference: str) -> dict[str, object]:
        return {
            "evidence_ref": reference,
            "relation": "supports",
            "supported_facets": ["implemented"],
        }

    def scoped_claim(
        scope_kind: str,
        relation_values: list[dict[str, object]],
        *,
        worktree_id: str | None = None,
    ) -> ClaimDraft:
        value = {
            **claim_value,
            "scope_kind": scope_kind,
            "evidence_relations": relation_values,
        }
        if scope_kind in {"project", "worktree"}:
            value.pop("module_id")
        if worktree_id is not None:
            value["worktree_id"] = worktree_id
        return ClaimDraft.from_value(value, 0)

    project_worktrees = {"worktree-a", "worktree-b", "worktree-c"}
    common_relations_values = [
        relation_value("evidence-a"),
        relation_value("evidence-b"),
        relation_value("evidence-c"),
    ]
    common_relations = tuple(
        EvidenceRelationDraft.from_value(value, index)
        for index, value in enumerate(common_relations_values)
    )
    common_states = (
        evidence_state("evidence-a", "worktree-a", "equivalence-common"),
        evidence_state("evidence-b", "worktree-b", "equivalence-common"),
        evidence_state("evidence-c", "worktree-c", "equivalence-common"),
    )
    common_relation_states = tuple(zip(common_relations, common_states, strict=True))
    divergent_relation_states = (
        *common_relation_states[:2],
        (
            common_relations[2],
            evidence_state("evidence-c", "worktree-c", "equivalence-divergent"),
        ),
    )

    for scope_kind in ("module", "project"):
        incomplete_claim = scoped_claim(scope_kind, common_relations_values[:2])
        with pytest.raises(InvalidInputError, match="every worktree"):
            AnalysisService._validate_claim(
                incomplete_claim,
                common_relation_states[:2],
                project_worktrees,
            )

        with pytest.raises(InvalidInputError, match="every worktree"):
            AnalysisService._validate_claim(
                scoped_claim(scope_kind, common_relations_values),
                divergent_relation_states,
                project_worktrees,
            )

        promoted = AnalysisService._validate_claim(
            scoped_claim(scope_kind, common_relations_values),
            common_relation_states,
            project_worktrees,
        )
        assert promoted["conflicted"] is False

    branch_relation_value = relation_value("evidence-branch-b")
    branch_relation = EvidenceRelationDraft.from_value(branch_relation_value, 0)
    branch_state = evidence_state(
        "evidence-branch-b",
        "worktree-b",
        "equivalence-branch-b",
    )
    with pytest.raises(InvalidInputError, match="every worktree"):
        AnalysisService._validate_claim(
            scoped_claim("module", [branch_relation_value]),
            ((branch_relation, branch_state),),
            project_worktrees,
        )

    for scope_kind in ("module", "worktree"):
        scoped = AnalysisService._validate_claim(
            scoped_claim(scope_kind, [branch_relation_value], worktree_id="worktree-b"),
            ((branch_relation, branch_state),),
            project_worktrees,
        )
        assert scoped["conflicted"] is False

    for scope_kind in ("module", "worktree"):
        with pytest.raises(InvalidInputError, match="another worktree"):
            AnalysisService._validate_claim(
                scoped_claim(scope_kind, [branch_relation_value], worktree_id="worktree-a"),
                ((branch_relation, branch_state),),
                project_worktrees,
            )


def test_verified_review_semantics_survive_equivalent_source_path_move(
    tmp_path: Path,
    data_paths: DataPaths,
) -> None:
    workspace = _workspace(tmp_path)
    database = Database(data_paths)
    receipt_id, _ = _authorize(database, workspace)
    service = AnalysisService(database)
    observed: list[dict[str, object]] = []

    for index in range(2):
        prepared, run_id, lens_id, project_id = _prepare(database, workspace, receipt_id)
        evidence = _evidence_by_kind(prepared)
        implementation_id = cast(str, evidence["implementation"]["evidence_id"])
        request = _analysis_request(
            run_id=run_id,
            lens_id=lens_id,
            project_id=project_id,
            implementation_id=implementation_id,
            test_id=cast(str, evidence["test_definition"]["evidence_id"]),
            statement="该项目使用 Python 建立可测试的计算边界。",
        )
        claim = _dict(_list(request["claim_drafts"])[0])
        claim["review_semantic_projection"] = {
            "concept_keys": [],
            "mechanism_keys": [],
            "behavior_contract_keys": [],
            "tradeoff_keys": [],
            "technology_identifiers": ["python"],
            "verification_anchors": {"python": [implementation_id]},
        }
        result = service.record_analysis(
            workspace_path=workspace,
            authorization_receipt_id=receipt_id,
            request_value=request,
        )
        observed.append(_dict(_list(result["claims"])[0]))
        if index == 0:
            (workspace / "app.py").rename(workspace / "renamed_app.py")

    assert observed[0]["claim_id"] == observed[1]["claim_id"]
    assert observed[0]["claim_revision_id"] != observed[1]["claim_revision_id"]
    assert observed[0]["review_semantic_sha256"] == observed[1]["review_semantic_sha256"]

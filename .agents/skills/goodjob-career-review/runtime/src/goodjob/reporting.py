"""Deterministic ReportBundle construction and immutable artifact publication."""

from __future__ import annotations

import base64
import hashlib
import json
import re
import sqlite3
import stat
import unicodedata
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path, PurePosixPath
from typing import cast

from goodjob import __version__
from goodjob.analysis import INLINE_TOKEN_KINDS, InlineToken
from goodjob.db import Database
from goodjob.errors import InvalidInputError
from goodjob.preparation import _now, _stored_json
from goodjob.process_identity import owner_process_stopped, process_identity
from goodjob.review import ReviewService
from goodjob.safe_fs import SafeDataTree

REPORT_BUNDLE_CONTRACT_VERSION = "report-bundle-v1"
REPORT_CONTRACT_VERSION = "goodjob-report-v1"
MANIFEST_CONTRACT_VERSION = "artifact-manifest-v1"
GENERATOR_VERSION = f"goodjob-runtime-{__version__}"
REPORT_FILENAME = "report.zh-CN.md"
RESUME_FILENAME = "resume.zh-CN.md"
HTML_FILENAME = "index.html"
MANIFEST_FILENAME = "manifest.json"

_RENDERABLE_RUN_STATUSES = frozenset(
    {"ready", "render_failed", "rendering", "completed", "partial"}
)
_DISPOSITIONS = ("fresh", "carried_forward", "failed_no_baseline", "excluded")
_NON_CURRENT_VALIDITIES = frozenset({"stale", "missing", "plan"})
_URL_SCHEME = re.compile(r"(?i)\b(https?|file|javascript):")
_NUMERIC_ANCHOR = re.compile(
    (
        r"(?<![\w.])[+-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?"
        r"(?:\s*(?:%|ms|s|秒|分钟|小时|天|倍|个|次|条|人|MB|GB))?"
    ),
    re.IGNORECASE,
)
_VISIBLE_CONTROL_LABELS = {
    "\u2028": "[U+2028]",
    "\u2029": "[U+2029]",
    "\u202a": "[U+202A]",
    "\u202b": "[U+202B]",
    "\u202c": "[U+202C]",
    "\u202d": "[U+202D]",
    "\u202e": "[U+202E]",
    "\u2066": "[U+2066]",
    "\u2067": "[U+2067]",
    "\u2068": "[U+2068]",
    "\u2069": "[U+2069]",
}

type JSONObject = dict[str, object]
type TokenValue = dict[str, object]


def _canonical_bytes(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError, RecursionError) as exc:
        raise InvalidInputError("report data is not canonical JSON") from exc


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_value(value: object) -> str:
    return _sha256_bytes(_canonical_bytes(value))


def _stored_object(raw: object, field_name: str) -> JSONObject:
    value = _stored_json(raw, field_name)
    if not isinstance(value, dict):
        raise InvalidInputError(f"{field_name} must be a stored JSON object")
    return cast(JSONObject, value)


def _stored_list(raw: object, field_name: str) -> list[object]:
    value = _stored_json(raw, field_name)
    if not isinstance(value, list):
        raise InvalidInputError(f"{field_name} must be a stored JSON list")
    return cast(list[object], value)


def _tokens_from_stored(raw: object, field_name: str) -> list[TokenValue]:
    values = _stored_list(raw, field_name)
    if not values:
        raise InvalidInputError(f"{field_name} must contain at least one token")
    return [
        cast(TokenValue, InlineToken.from_value(value, f"{field_name}[{index}]").as_json())
        for index, value in enumerate(values)
    ]


def _text_tokens(value: str) -> list[TokenValue]:
    if not value:
        raise InvalidInputError("report text token must not be empty")
    return [{"kind": "text", "value": value}]


def _token_text(tokens: list[TokenValue]) -> str:
    return "".join(str(token["value"]) for token in tokens)


def _normalized_search_text(*values: str) -> str:
    return " ".join(
        unicodedata.normalize("NFKC", value).casefold().strip() for value in values if value.strip()
    )


def _bundle_without_hash(bundle: JSONObject) -> JSONObject:
    return {key: value for key, value in bundle.items() if key != "bundle_sha256"}


def report_bundle_sha256(bundle: JSONObject) -> str:
    """Recompute the bundle digest over canonical JSON with its digest field omitted."""
    return _sha256_value(_bundle_without_hash(bundle))


def canonical_report_bundle(bundle: JSONObject) -> bytes:
    if bundle.get("contract_version") != REPORT_BUNDLE_CONTRACT_VERSION:
        raise InvalidInputError("unsupported ReportBundle contract version")
    digest = bundle.get("bundle_sha256")
    if not isinstance(digest, str) or digest != report_bundle_sha256(bundle):
        raise InvalidInputError("ReportBundle digest does not match canonical content")
    return _canonical_bytes(bundle)


def _deterministic_id(kind: str, *parts: str) -> str:
    digest = hashlib.sha256("\0".join((kind, *parts)).encode("utf-8")).hexdigest()
    return f"{kind}-{digest[:24]}"


class ReportBundleBuilder:
    """Project a frozen analysis commit into the sole report rendering contract."""

    def __init__(self, database: Database) -> None:
        self._database = database

    def build(self, preparation_run_id: str) -> JSONObject:
        with self._database.write_transaction() as connection:
            ReviewService.ensure_bindings_in_connection(connection, preparation_run_id)
            return self._build_from_connection(connection, preparation_run_id)

    def _build_from_connection(
        self,
        connection: sqlite3.Connection,
        preparation_run_id: str,
    ) -> JSONObject:
        run = self._run_row(connection, preparation_run_id)
        projects, assessment_evidence_ids = self._projects(connection, run)
        modules = self._modules(connection, preparation_run_id)
        gaps = self._gaps(connection, preparation_run_id)
        claims, relation_evidence_ids, planned_evidence_ids = self._claims(
            connection,
            preparation_run_id,
        )
        evidence_ids = assessment_evidence_ids | relation_evidence_ids
        evidence = self._evidence(
            connection,
            scan_run_id=str(run["scan_run_id"]),
            evidence_ids=evidence_ids,
            planned_evidence_ids=planned_evidence_ids,
        )
        issues = self._issues(connection, str(run["scan_run_id"]))

        evidence_by_id = {str(item["evidence_id"]): item for item in evidence}
        gap_by_id = {str(item["gap_id"]): item for item in gaps}
        project_by_id = {str(item["project_id"]): item for item in projects}
        for module in modules:
            project = project_by_id.get(str(module["project_id"]))
            if project is not None:
                cast(list[object], project["modules"]).append(module)
        for claim in claims:
            project = project_by_id.get(str(claim["project_id"]))
            if project is not None:
                cast(list[object], project["claim_ids"]).append(claim["claim_id"])
        for gap in gaps:
            project_id = gap.get("project_id")
            project = project_by_id.get(str(project_id)) if project_id is not None else None
            if project is not None:
                cast(list[object], project["gap_ids"]).append(gap["gap_id"])

        self._validate_token_references(claims, projects, gaps, evidence_by_id, gap_by_id)
        limitations = self._limitations(projects, evidence, issues, gaps)
        package_status = self._package_status(str(run["scan_status"]), limitations)
        export_items = self._export_projection(claims, evidence_by_id, str(run["role_lens_id"]))
        search_index = self._search_index(projects, claims, evidence, gaps)
        review = ReviewService.report_projection_from_connection(
            connection,
            preparation_run_id,
            cutoff_at=str(run["preparation_started_at"]),
        )
        dimensions = _stored_list(run["dimensions"], "RoleLens dimensions")
        role_lens: JSONObject = {
            "role_lens_id": str(run["role_lens_id"]),
            "contract_version": str(run["role_lens_contract_version"]),
            "lens_sha256": str(run["lens_sha256"]),
            "dimensions": dimensions,
            "evidence_requirements": _stored_json(
                run["evidence_requirements"], "RoleLens evidence requirements"
            ),
            "ranking_rules": _stored_json(run["ranking_rules"], "RoleLens ranking rules"),
            "output_sections": _stored_json(run["output_sections"], "RoleLens output sections"),
            "question_strategy": _stored_json(
                run["question_strategy"], "RoleLens question strategy"
            ),
            "gap_rules": _stored_json(run["gap_rules"], "RoleLens gap rules"),
            "assumptions": _stored_json(run["assumptions"], "RoleLens assumptions"),
            "generator_id": str(run["role_lens_generator_id"]),
            "prompt_contract_version": str(run["prompt_contract_version"]),
        }
        counts = {
            disposition: sum(
                1 for project in projects if project["snapshot_disposition"] == disposition
            )
            for disposition in _DISPOSITIONS
        }
        bundle: JSONObject = {
            "contract_version": REPORT_BUNDLE_CONTRACT_VERSION,
            "preparation_run_id": preparation_run_id,
            "analysis_commit_id": str(run["analysis_commit_id"]),
            "analysis_contract_version": str(run["analysis_contract_version"]),
            "scan_run_id": str(run["scan_run_id"]),
            "generated_at": str(run["analysis_committed_at"]),
            "primary_language": str(run["primary_language"]),
            "package_status": package_status,
            "role": {
                "name": str(run["role_name"]),
                "inferred_level": run["inferred_level"],
                "level_override": run["level_override"],
                "applied_level": run["level_override"] or run["inferred_level"],
                "level_source": (
                    "owner_override"
                    if run["level_override"] is not None
                    else "inferred"
                    if run["inferred_level"] is not None
                    else "unspecified"
                ),
                "jd": {
                    "input_kind": str(run["jd_input_kind"]),
                    "source_path": run["jd_source_path"],
                    "content_sha256": run["jd_content_sha256"],
                    "has_jd": run["jd_content_sha256"] is not None,
                },
            },
            "role_lens": role_lens,
            "coverage": {
                "projects_total": len(projects),
                "eligible_projects": counts["fresh"] + counts["carried_forward"],
                "disposition_counts": counts,
                "limitations": limitations,
            },
            "projects": projects,
            "claims": claims,
            "evidence": evidence,
            "knowledge_gaps": gaps,
            "review": review,
            "interview": self._interview_projection(claims, gaps, review),
            "export_projection": {
                "contract_version": "export-projection-v1",
                "items": export_items,
                "projection_sha256": _sha256_value(export_items),
            },
            "search_index": search_index,
        }
        bundle["bundle_sha256"] = report_bundle_sha256(bundle)
        canonical_report_bundle(bundle)
        return bundle

    @staticmethod
    def _run_row(connection: sqlite3.Connection, preparation_run_id: str) -> sqlite3.Row:
        row = connection.execute(
            """
            SELECT pr.preparation_run_id, pr.status AS preparation_status,
                   pr.started_at AS preparation_started_at,
                   pr.workspace_id, pr.scan_run_id, pr.role_lens_id,
                   sr.status AS scan_status, ac.analysis_commit_id,
                   ac.contract_version AS analysis_contract_version,
                   ac.committed_at AS analysis_committed_at,
                   ji.role_name, ji.jd_input_kind, ji.jd_source_path,
                   ji.jd_content_sha256, ji.inferred_level, ji.level_override,
                   ji.primary_language,
                   rl.contract_version AS role_lens_contract_version,
                   rl.dimensions, rl.evidence_requirements, rl.ranking_rules,
                   rl.output_sections, rl.question_strategy, rl.gap_rules,
                   rl.assumptions, rl.generator_id AS role_lens_generator_id,
                   rl.prompt_contract_version, rl.lens_sha256
            FROM preparation_runs AS pr
            JOIN scan_runs AS sr ON sr.scan_run_id = pr.scan_run_id
            JOIN analysis_commits AS ac
              ON ac.preparation_run_id = pr.preparation_run_id
            JOIN role_lenses AS rl ON rl.role_lens_id = pr.role_lens_id
            JOIN job_inputs AS ji ON ji.job_input_id = rl.job_input_id
            WHERE pr.preparation_run_id = ?
            """,
            (preparation_run_id,),
        ).fetchone()
        if row is None:
            raise InvalidInputError("PreparationRun has no frozen analysis commit")
        if str(row["preparation_status"]) not in _RENDERABLE_RUN_STATUSES:
            raise InvalidInputError("PreparationRun is not ready for deterministic rendering")
        if str(row["scan_status"]) not in {"completed", "partial"}:
            raise InvalidInputError("PreparationRun scan is not a reusable terminal snapshot")
        if str(row["primary_language"]) != "zh-CN":
            raise InvalidInputError("the primary report language must be zh-CN")
        return cast(sqlite3.Row, row)

    @staticmethod
    def _projects(
        connection: sqlite3.Connection,
        run: sqlite3.Row,
    ) -> tuple[list[JSONObject], set[str]]:
        rows = connection.execute(
            """
            SELECT prp.project_id, p.display_name, wp.relative_location,
                   prp.project_snapshot_id, prp.snapshot_disposition,
                   ps.coverage_status, pa.dimension_scores_milli,
                   pa.evidence_and_gap_refs, pa.rationale_tokens,
                   pa.coverage_bps, pa.base_score_milli, pa.final_score_milli,
                   pa.rank
            FROM preparation_run_projects AS prp
            JOIN projects AS p ON p.project_id = prp.project_id
            LEFT JOIN workspace_projects AS wp
              ON wp.workspace_id = ? AND wp.project_id = prp.project_id
            LEFT JOIN project_snapshots AS ps
              ON ps.project_snapshot_id = prp.project_snapshot_id
            LEFT JOIN project_assessments AS pa
              ON pa.preparation_run_id = prp.preparation_run_id
             AND pa.project_id = prp.project_id
            WHERE prp.preparation_run_id = ?
            ORDER BY CASE WHEN pa.rank IS NULL THEN 1 ELSE 0 END,
                     pa.rank, p.display_name, prp.project_id
            """,
            (str(run["workspace_id"]), str(run["preparation_run_id"])),
        ).fetchall()
        projects: list[JSONObject] = []
        evidence_ids: set[str] = set()
        for row in rows:
            disposition = str(row["snapshot_disposition"])
            eligible = disposition in {"fresh", "carried_forward"}
            assessment: JSONObject | None = None
            if eligible:
                if row["rank"] is None:
                    raise InvalidInputError("eligible project has no frozen ProjectAssessment")
                refs = _stored_object(row["evidence_and_gap_refs"], "assessment references")
                raw_evidence_ids = refs.get("evidence_ids")
                raw_gap_ids = refs.get("gap_ids")
                if not isinstance(raw_evidence_ids, list) or not all(
                    isinstance(value, str) for value in raw_evidence_ids
                ):
                    raise InvalidInputError("assessment evidence references are invalid")
                if not isinstance(raw_gap_ids, list) or not all(
                    isinstance(value, str) for value in raw_gap_ids
                ):
                    raise InvalidInputError("assessment gap references are invalid")
                evidence_ids.update(cast(list[str], raw_evidence_ids))
                assessment = {
                    "rank": int(row["rank"]),
                    "dimension_scores_milli": _stored_object(
                        row["dimension_scores_milli"], "dimension scores"
                    ),
                    "coverage_bps": int(row["coverage_bps"]),
                    "base_score_milli": int(row["base_score_milli"]),
                    "final_score_milli": int(row["final_score_milli"]),
                    "rationale_tokens": _tokens_from_stored(
                        row["rationale_tokens"], "assessment rationale tokens"
                    ),
                    "evidence_ids": list(cast(list[str], raw_evidence_ids)),
                    "gap_ids": list(cast(list[str], raw_gap_ids)),
                }
            elif row["rank"] is not None:
                raise InvalidInputError("ineligible project must not have a ProjectAssessment")
            projects.append(
                {
                    "project_id": str(row["project_id"]),
                    "display_name": str(row["display_name"]),
                    "workspace_relative_location": row["relative_location"],
                    "project_snapshot_id": row["project_snapshot_id"],
                    "snapshot_disposition": disposition,
                    "coverage_status": row["coverage_status"],
                    "eligible": eligible,
                    "assessment": assessment,
                    "modules": [],
                    "claim_ids": [],
                    "gap_ids": [],
                }
            )
        if not projects:
            raise InvalidInputError("PreparationRun contains no project coverage")
        return projects, evidence_ids

    @staticmethod
    def _modules(
        connection: sqlite3.Connection,
        preparation_run_id: str,
    ) -> list[JSONObject]:
        return [
            {
                "module_id": str(row["module_id"]),
                "project_id": str(row["project_id"]),
                "name": str(row["name"]),
                "kind": str(row["kind"]),
                "relative_root": str(row["relative_root"]),
                "adapter_id": str(row["adapter_id"]),
            }
            for row in connection.execute(
                """
                SELECT m.module_id, m.project_id, m.name, m.kind,
                       mo.relative_root, mo.adapter_id
                FROM preparation_run_projects AS prp
                JOIN module_observations AS mo
                  ON mo.project_snapshot_id = prp.project_snapshot_id
                JOIN modules AS m ON m.module_id = mo.module_id
                WHERE prp.preparation_run_id = ?
                ORDER BY m.project_id, mo.relative_root, m.module_id
                """,
                (preparation_run_id,),
            ).fetchall()
        ]

    @staticmethod
    def _gaps(connection: sqlite3.Connection, preparation_run_id: str) -> list[JSONObject]:
        return [
            {
                "gap_id": str(row["gap_id"]),
                "gap_key": str(row["gap_key"]),
                "scope_kind": str(row["scope_kind"]),
                "scope_id": str(row["scope_id"]),
                "project_id": row["project_id"],
                "module_id": row["module_id"],
                "dimension": str(row["dimension"]),
                "description_tokens": _tokens_from_stored(
                    row["description_tokens"], "KnowledgeGap description tokens"
                ),
                "severity": str(row["severity"]),
                "resolution_kind": str(row["resolution_kind"]),
                "status": str(row["status"]),
            }
            for row in connection.execute(
                """
                SELECT gap_id, gap_key, scope_kind, scope_id, project_id,
                       module_id, dimension, description_tokens, severity,
                       resolution_kind, status
                FROM knowledge_gaps
                WHERE preparation_run_id = ?
                ORDER BY scope_kind, scope_id, dimension, gap_key
                """,
                (preparation_run_id,),
            ).fetchall()
        ]

    @staticmethod
    def _claims(
        connection: sqlite3.Connection,
        preparation_run_id: str,
    ) -> tuple[list[JSONObject], set[str], set[str]]:
        relation_rows = connection.execute(
            """
            SELECT ce.claim_revision_id, ce.evidence_id, ce.relation,
                   ce.supported_facets
            FROM preparation_claims AS pc
            JOIN claim_evidence AS ce
              ON ce.claim_revision_id = pc.claim_revision_id
            WHERE pc.preparation_run_id = ?
            ORDER BY ce.claim_revision_id, ce.relation, ce.evidence_id
            """,
            (preparation_run_id,),
        ).fetchall()
        relations: dict[str, list[JSONObject]] = {}
        evidence_ids: set[str] = set()
        evidence_facets: dict[str, set[str]] = {}
        for row in relation_rows:
            supported_facets = _stored_list(row["supported_facets"], "supported facets")
            if not all(isinstance(value, str) for value in supported_facets):
                raise InvalidInputError("supported facets must be strings")
            evidence_id = str(row["evidence_id"])
            evidence_ids.add(evidence_id)
            evidence_facets.setdefault(evidence_id, set()).update(cast(list[str], supported_facets))
            relations.setdefault(str(row["claim_revision_id"]), []).append(
                {
                    "evidence_id": evidence_id,
                    "relation": str(row["relation"]),
                    "supported_facets": supported_facets,
                }
            )
        claims: list[JSONObject] = []
        for row in connection.execute(
            """
            SELECT c.claim_id, c.claim_key, c.category, c.scope_kind,
                   c.project_id, c.worktree_id, c.module_id,
                   cr.claim_revision_id, cr.revision_no, cr.revision_sha256,
                   cr.statement_tokens, cr.facets, cr.support_level,
                   cr.personal_attribution, cr.review_semantic_projection,
                   cr.review_semantic_sha256, pc.rank, pc.section
            FROM preparation_claims AS pc
            JOIN claim_revisions AS cr
              ON cr.claim_revision_id = pc.claim_revision_id
            JOIN claims AS c ON c.claim_id = cr.claim_id
            WHERE pc.preparation_run_id = ?
            ORDER BY pc.rank
            """,
            (preparation_run_id,),
        ).fetchall():
            revision_id = str(row["claim_revision_id"])
            claim_relations = relations.get(revision_id, [])
            if not claim_relations:
                raise InvalidInputError("ClaimRevision has no frozen Evidence relation")
            if str(row["personal_attribution"]) == "legacy_unknown":
                raise InvalidInputError(
                    "legacy ClaimRevision attribution is unknown; create and analyze a new "
                    "PreparationRun before rendering"
                )
            claims.append(
                {
                    "claim_id": str(row["claim_id"]),
                    "claim_key": str(row["claim_key"]),
                    "claim_revision_id": revision_id,
                    "revision_no": int(row["revision_no"]),
                    "revision_sha256": str(row["revision_sha256"]),
                    "rank": int(row["rank"]),
                    "section": str(row["section"]),
                    "category": str(row["category"]),
                    "scope_kind": str(row["scope_kind"]),
                    "project_id": str(row["project_id"]),
                    "worktree_id": row["worktree_id"],
                    "module_id": row["module_id"],
                    "statement_tokens": _tokens_from_stored(
                        row["statement_tokens"], "Claim statement tokens"
                    ),
                    "facets": _stored_list(row["facets"], "Claim facets"),
                    "support_level": str(row["support_level"]),
                    "personal_attribution": str(row["personal_attribution"]),
                    "review_semantic_projection": _stored_object(
                        row["review_semantic_projection"], "review semantic projection"
                    ),
                    "review_semantic_sha256": str(row["review_semantic_sha256"]),
                    "evidence_relations": claim_relations,
                }
            )
        if not claims:
            raise InvalidInputError("PreparationRun has no frozen Claims")
        planned_evidence_ids = {
            evidence_id
            for evidence_id, facets in evidence_facets.items()
            if "planned" in facets
            and not {"implemented", "test_defined", "test_verified"}.intersection(facets)
        }
        return claims, evidence_ids, planned_evidence_ids

    @staticmethod
    def _evidence(
        connection: sqlite3.Connection,
        *,
        scan_run_id: str,
        evidence_ids: set[str],
        planned_evidence_ids: set[str],
    ) -> list[JSONObject]:
        if not evidence_ids:
            raise InvalidInputError("frozen analysis does not reference any Evidence")
        placeholders = ",".join("?" for _ in evidence_ids)
        rows = connection.execute(
            f"""
            SELECT e.evidence_id, e.project_id, e.project_snapshot_id,
                   e.acquisition_scope, e.module_id, e.source_revision_id,
                   e.content_equivalence_key, e.origin_kind, e.evidence_kind,
                   e.locator, e.summary, e.commit_state, e.created_at,
                   e.preparation_run_id, e.query_reason,
                   COALESCE(ev.validity, 'current') AS validity,
                   sr.content_sha256, sa.worktree_id AS source_worktree_id,
                   sa.relative_path, sa.artifact_kind,
                   ps.scan_run_id AS provenance_scan_run_id,
                   ec.context_fact_id, pcf.fact_kind AS context_fact_kind,
                   pcf.source_kind AS context_source_kind
            FROM evidence AS e
            LEFT JOIN evidence_validities AS ev
              ON ev.scan_run_id = ? AND ev.evidence_id = e.evidence_id
            LEFT JOIN source_revisions AS sr
              ON sr.source_revision_id = e.source_revision_id
            LEFT JOIN source_artifacts AS sa ON sa.artifact_id = sr.artifact_id
            LEFT JOIN project_snapshots AS ps
              ON ps.project_snapshot_id = e.project_snapshot_id
            LEFT JOIN evidence_contexts AS ec ON ec.evidence_id = e.evidence_id
            LEFT JOIN project_context_facts AS pcf
              ON pcf.context_fact_id = ec.context_fact_id
            WHERE e.evidence_id IN ({placeholders})
            ORDER BY e.project_id, e.evidence_kind, e.evidence_id
            """,
            (scan_run_id, *sorted(evidence_ids)),
        ).fetchall()
        if len(rows) != len(evidence_ids):
            raise InvalidInputError("frozen analysis references missing Evidence")
        provenance_scan_ids = sorted(
            {
                str(row["provenance_scan_run_id"])
                for row in rows
                if row["provenance_scan_run_id"] is not None
            }
        )
        worktrees: dict[tuple[str, str], JSONObject] = {}
        if provenance_scan_ids:
            scan_placeholders = ",".join("?" for _ in provenance_scan_ids)
            worktree_rows = connection.execute(
                f"""
                SELECT wo.scan_run_id, wo.worktree_id, wo.branch, wo.head_commit,
                       wo.dirty_state, wt.project_id
                FROM worktree_observations AS wo
                JOIN worktrees AS wt ON wt.worktree_id = wo.worktree_id
                WHERE wo.scan_run_id IN ({scan_placeholders})
                ORDER BY wo.scan_run_id, wo.worktree_id
                """,
                provenance_scan_ids,
            ).fetchall()
            worktrees = {
                (str(row["scan_run_id"]), str(row["worktree_id"])): {
                    "worktree_id": str(row["worktree_id"]),
                    "project_id": str(row["project_id"]),
                    "branch": row["branch"],
                    "head_commit": row["head_commit"],
                    "dirty_state": str(row["dirty_state"]),
                    "observed_scan_run_id": str(row["scan_run_id"]),
                }
                for row in worktree_rows
            }
        result: list[JSONObject] = []
        for row in rows:
            locator = _stored_object(row["locator"], "Evidence locator")
            raw_worktree_id = row["source_worktree_id"] or locator.get("worktree_id")
            worktree_id = str(raw_worktree_id) if raw_worktree_id is not None else None
            provenance_scan_run_id = (
                str(row["provenance_scan_run_id"])
                if row["provenance_scan_run_id"] is not None
                else None
            )
            worktree = (
                worktrees.get((provenance_scan_run_id, worktree_id))
                if provenance_scan_run_id is not None and worktree_id is not None
                else None
            )
            validity = (
                "plan" if str(row["evidence_id"]) in planned_evidence_ids else str(row["validity"])
            )
            if validity not in {"current", "stale", "missing", "plan"}:
                raise InvalidInputError("Evidence validity is not supported by ReportBundle v1")
            relative_path = row["relative_path"] or locator.get("relative_path")
            result.append(
                {
                    "evidence_id": str(row["evidence_id"]),
                    "project_id": str(row["project_id"]),
                    "project_snapshot_id": row["project_snapshot_id"],
                    "preparation_run_id": row["preparation_run_id"],
                    "acquisition_scope": str(row["acquisition_scope"]),
                    "origin_kind": str(row["origin_kind"]),
                    "evidence_kind": str(row["evidence_kind"]),
                    "module_id": row["module_id"],
                    "worktree": worktree,
                    "relative_path": relative_path,
                    "locator": locator,
                    "summary_tokens": _text_tokens(str(row["summary"])),
                    "commit_state": str(row["commit_state"]),
                    "validity": validity,
                    "content_sha256": row["content_sha256"],
                    "content_equivalence_key": row["content_equivalence_key"],
                    "query_reason_tokens": (
                        _text_tokens(str(row["query_reason"]))
                        if row["query_reason"] is not None
                        else []
                    ),
                    "context_fact": (
                        {
                            "context_fact_id": str(row["context_fact_id"]),
                            "fact_kind": str(row["context_fact_kind"]),
                            "source_kind": str(row["context_source_kind"]),
                        }
                        if row["context_fact_id"] is not None
                        else None
                    ),
                    "artifact_kind": row["artifact_kind"],
                    "created_at": str(row["created_at"]),
                }
            )
        return result

    @staticmethod
    def _issues(connection: sqlite3.Connection, scan_run_id: str) -> list[JSONObject]:
        return [
            {
                "issue_id": str(row["issue_id"]),
                "project_id": row["project_id"],
                "kind": str(row["kind"]),
                "severity": str(row["severity"]),
                "relative_path": row["relative_path"],
                "message_tokens": _text_tokens(str(row["message"])),
                "remediation_tokens": _text_tokens(str(row["remediation"])),
            }
            for row in connection.execute(
                """
                SELECT issue_id, project_id, kind, severity, relative_path,
                       message, remediation
                FROM scan_issues
                WHERE scan_run_id = ?
                ORDER BY severity DESC, project_id, issue_id
                """,
                (scan_run_id,),
            ).fetchall()
        ]

    @staticmethod
    def _limitations(
        projects: list[JSONObject],
        evidence: list[JSONObject],
        issues: list[JSONObject],
        gaps: list[JSONObject],
    ) -> list[JSONObject]:
        limitations: list[JSONObject] = []
        for project in projects:
            disposition = str(project["snapshot_disposition"])
            if disposition == "fresh":
                continue
            name = str(project["display_name"])
            if disposition == "carried_forward":
                message = f"{name} 沿用最近一次成功项目快照。"
                impact = "该项目参与评分，但可能不反映工作区当前状态。"
                remediation = "完成可读扫描后显式 refresh 并创建新的 PreparationRun。"
                severity = "warning"
            elif disposition == "failed_no_baseline":
                message = f"{name} 扫描失败且没有可沿用基线。"
                impact = "该项目仅计入覆盖范围，不参与岗位评分与叙事。"
                remediation = "修复权限、损坏或解析问题后重新扫描。"
                severity = "error"
            else:
                message = f"{name} 被当前扫描配置排除。"
                impact = "该项目仅计入覆盖范围，不参与岗位评分与叙事。"
                remediation = "确认忽略规则是否符合本次岗位准备范围。"
                severity = "info"
            limitations.append(
                {
                    "limitation_id": _deterministic_id(
                        "limit", str(project["project_id"]), disposition
                    ),
                    "kind": "snapshot_disposition",
                    "severity": severity,
                    "project_id": project["project_id"],
                    "message_tokens": _text_tokens(message),
                    "impact_tokens": _text_tokens(impact),
                    "remediation_tokens": _text_tokens(remediation),
                    "filter_route": f"#/v1/project/{project['project_id']}",
                }
            )
        for issue in issues:
            limitations.append(
                {
                    "limitation_id": str(issue["issue_id"]),
                    "kind": f"scan_issue:{issue['kind']}",
                    "severity": issue["severity"],
                    "project_id": issue["project_id"],
                    "message_tokens": issue["message_tokens"],
                    "impact_tokens": _text_tokens("相关路径的证据覆盖可能不完整。"),
                    "remediation_tokens": issue["remediation_tokens"],
                    "filter_route": (
                        f"#/v1/project/{issue['project_id']}"
                        if issue["project_id"] is not None
                        else "#/v1/overview"
                    ),
                }
            )
        non_current_by_project: dict[tuple[str, str], int] = {}
        for item in evidence:
            validity = str(item["validity"])
            if validity in _NON_CURRENT_VALIDITIES:
                key = (str(item["project_id"]), validity)
                non_current_by_project[key] = non_current_by_project.get(key, 0) + 1
        for (project_id, validity), count in sorted(non_current_by_project.items()):
            wording = {
                "stale": "只作历史限制",
                "missing": "当前定位已缺失",
                "plan": "仅为已规划或已文档化",
            }[validity]
            limitations.append(
                {
                    "limitation_id": _deterministic_id("limit", project_id, validity, str(count)),
                    "kind": f"evidence_validity:{validity}",
                    "severity": "warning" if validity != "missing" else "error",
                    "project_id": project_id,
                    "message_tokens": _text_tokens(f"该项目有 {count} 条 Evidence {wording}。"),
                    "impact_tokens": _text_tokens("这些证据不得单独支撑当前强结论。"),
                    "remediation_tokens": _text_tokens(
                        "核对工作区状态并显式 refresh，或保留为历史/计划表述。"
                    ),
                    "filter_route": f"#/v1/evidence?validity={validity}&project={project_id}",
                }
            )
        for gap in gaps:
            if gap["status"] != "open":
                continue
            limitations.append(
                {
                    "limitation_id": _deterministic_id("limit", str(gap["gap_id"])),
                    "kind": "knowledge_gap",
                    "severity": gap["severity"],
                    "project_id": gap["project_id"],
                    "message_tokens": gap["description_tokens"],
                    "impact_tokens": _text_tokens("该缺口会降低相关叙事或面试回答的可信度。"),
                    "remediation_tokens": _text_tokens(
                        f"按 {gap['resolution_kind']} 补充证据或上下文。"
                    ),
                    "filter_route": f"#/v1/gaps?gap={gap['gap_id']}",
                }
            )
        return limitations

    @staticmethod
    def _package_status(scan_status: str, limitations: list[JSONObject]) -> str:
        consequential = any(
            limitation["severity"] in {"warning", "error", "medium", "high", "critical"}
            for limitation in limitations
        )
        return "partial" if scan_status == "partial" or consequential else "completed"

    @staticmethod
    def _export_projection(
        claims: list[JSONObject],
        evidence_by_id: dict[str, JSONObject],
        role_lens_id: str,
    ) -> list[JSONObject]:
        items: list[JSONObject] = []
        resume_categories = {
            "business",
            "technology",
            "architecture",
            "implementation_method",
            "challenge",
            "tradeoff",
            "contribution",
            "outcome",
        }
        for claim in claims:
            statement_tokens = cast(list[TokenValue], claim["statement_tokens"])
            statement = _token_text(statement_tokens)
            projection = cast(JSONObject, claim["review_semantic_projection"])
            evidence_refs = sorted(
                str(relation["evidence_id"])
                for relation in cast(list[JSONObject], claim["evidence_relations"])
            )
            anchors = {
                "numbers_and_units": sorted(set(_NUMERIC_ANCHOR.findall(statement))),
                "technology_identifiers": projection.get("technology_identifiers", []),
                "implementation_status": claim["facets"],
                "personal_attribution": claim["personal_attribution"],
                "role_anchor_ids": projection.get("role_anchor_ids", []),
                "outcome_anchor_ids": projection.get("outcome_anchor_ids", []),
            }
            common: JSONObject = {
                "claim_refs": [claim["claim_id"]],
                "evidence_refs": evidence_refs,
                "role_lens_refs": [role_lens_id],
                "anchors": anchors,
                "project_id": claim["project_id"],
                "module_id": claim["module_id"],
            }
            if claim["category"] in resume_categories:
                items.append(
                    {
                        "source_item_id": _deterministic_id("resume", str(claim["claim_id"])),
                        "export_kind": "resume",
                        "source_tokens": statement_tokens,
                        **common,
                    }
                )
            items.append(
                {
                    "source_item_id": _deterministic_id("interview", str(claim["claim_id"])),
                    "export_kind": "interview_qa",
                    "source_tokens": statement_tokens,
                    **common,
                }
            )
        referenced = {
            evidence_id for item in items for evidence_id in cast(list[str], item["evidence_refs"])
        }
        if not referenced <= set(evidence_by_id):
            raise InvalidInputError("export projection references missing Evidence")
        return sorted(items, key=lambda item: str(item["source_item_id"]))

    @staticmethod
    def _interview_projection(
        claims: list[JSONObject],
        gaps: list[JSONObject],
        review: JSONObject,
    ) -> JSONObject:
        raw_bindings = review.get("bindings")
        if not isinstance(raw_bindings, list):
            raise InvalidInputError("review projection bindings are invalid")
        binding_by_subject = {
            str(binding["stable_subject_id"]): cast(JSONObject, binding)
            for binding in raw_bindings
            if isinstance(binding, dict) and "stable_subject_id" in binding
        }
        questions: list[JSONObject] = []
        for claim in claims:
            binding = binding_by_subject.get(str(claim["claim_id"]))
            if binding is None:
                raise InvalidInputError("Claim is missing its ReviewTargetBinding")
            questions.append(
                {
                    "question_id": _deterministic_id("question", str(claim["claim_id"])),
                    "level": "project_deep_dive",
                    "project_id": claim["project_id"],
                    "module_id": claim["module_id"],
                    "claim_id": claim["claim_id"],
                    "prompt_tokens": [
                        {"kind": "text", "value": "请解释这个要点的实现机制、证据与取舍："},
                        *cast(list[TokenValue], claim["statement_tokens"]),
                    ],
                    "follow_up_tokens": _text_tokens(
                        "哪些证据支持当前状态？边界、失败路径和替代方案是什么？"
                    ),
                    **ReportBundleBuilder._question_review_projection(binding),
                }
            )
        for gap in gaps:
            if gap["status"] != "open":
                continue
            binding = binding_by_subject.get(f"gap:{gap['gap_key']}")
            if binding is None:
                raise InvalidInputError("KnowledgeGap is missing its ReviewTargetBinding")
            questions.append(
                {
                    "question_id": _deterministic_id("question", str(gap["gap_id"])),
                    "level": "knowledge_gap",
                    "project_id": gap["project_id"],
                    "module_id": gap["module_id"],
                    "gap_id": gap["gap_id"],
                    "prompt_tokens": [
                        {"kind": "text", "value": "请补全或明确以下知识缺口："},
                        *cast(list[TokenValue], gap["description_tokens"]),
                    ],
                    "follow_up_tokens": _text_tokens(
                        f"需要哪类证据或上下文才能按 {gap['resolution_kind']} 解决？"
                    ),
                    **ReportBundleBuilder._question_review_projection(binding),
                }
            )
        return {
            "two_minute_pitch_claim_ids": [claim["claim_id"] for claim in claims[:4]],
            "questions": questions,
        }

    @staticmethod
    def _question_review_projection(binding: JSONObject) -> JSONObject:
        return {
            "review_target_binding_id": binding["review_target_binding_id"],
            "review_target_id": binding["review_target_id"],
            "continuity_status": binding["continuity_status"],
            "mastery_level": binding["mastery_level"],
            "next_review_at": binding["next_review_at"],
        }

    @staticmethod
    def _search_index(
        projects: list[JSONObject],
        claims: list[JSONObject],
        evidence: list[JSONObject],
        gaps: list[JSONObject],
    ) -> JSONObject:
        project_names = {
            str(project["project_id"]): str(project["display_name"]) for project in projects
        }
        entries: list[JSONObject] = []
        for project in projects:
            entries.append(
                {
                    "item_id": f"project:{project['project_id']}",
                    "kind": "project",
                    "route": f"#/v1/project/{project['project_id']}",
                    "project_id": project["project_id"],
                    "search_text": _normalized_search_text(str(project["display_name"])),
                }
            )
        for claim in claims:
            entries.append(
                {
                    "item_id": f"claim:{claim['claim_id']}",
                    "kind": "claim",
                    "route": f"#/v1/evidence?claim={claim['claim_id']}",
                    "project_id": claim["project_id"],
                    "module_id": claim["module_id"],
                    "search_text": _normalized_search_text(
                        project_names.get(str(claim["project_id"]), ""),
                        _token_text(cast(list[TokenValue], claim["statement_tokens"])),
                        str(claim["category"]),
                        *cast(list[str], claim["facets"]),
                    ),
                }
            )
        for item in evidence:
            entries.append(
                {
                    "item_id": f"evidence:{item['evidence_id']}",
                    "kind": "evidence",
                    "route": f"#/v1/evidence?evidence={item['evidence_id']}",
                    "project_id": item["project_id"],
                    "module_id": item["module_id"],
                    "search_text": _normalized_search_text(
                        project_names.get(str(item["project_id"]), ""),
                        _token_text(cast(list[TokenValue], item["summary_tokens"])),
                        str(item["evidence_kind"]),
                        str(item.get("relative_path") or ""),
                        str(item["commit_state"]),
                        str(item["validity"]),
                    ),
                }
            )
        for gap in gaps:
            entries.append(
                {
                    "item_id": f"gap:{gap['gap_id']}",
                    "kind": "gap",
                    "route": f"#/v1/gaps?gap={gap['gap_id']}",
                    "project_id": gap["project_id"],
                    "module_id": gap["module_id"],
                    "search_text": _normalized_search_text(
                        _token_text(cast(list[TokenValue], gap["description_tokens"])),
                        str(gap["dimension"]),
                        str(gap["severity"]),
                    ),
                }
            )
        return {"index_version": "substring-index-v1", "entries": entries}

    @staticmethod
    def _validate_token_references(
        claims: list[JSONObject],
        projects: list[JSONObject],
        gaps: list[JSONObject],
        evidence_by_id: dict[str, JSONObject],
        gap_by_id: dict[str, JSONObject],
    ) -> None:
        claim_ids = {str(claim["claim_id"]) for claim in claims}
        token_groups: list[list[TokenValue]] = []
        token_groups.extend(cast(list[TokenValue], claim["statement_tokens"]) for claim in claims)
        token_groups.extend(cast(list[TokenValue], gap["description_tokens"]) for gap in gaps)
        for project in projects:
            assessment = project["assessment"]
            if isinstance(assessment, dict):
                token_groups.append(cast(list[TokenValue], assessment["rationale_tokens"]))
        for tokens in token_groups:
            for token in tokens:
                kind = str(token.get("kind"))
                if kind not in INLINE_TOKEN_KINDS:
                    raise InvalidInputError("ReportBundle contains an unknown inline token")
                ref_id = token.get("ref_id")
                if kind == "claim_ref" and str(ref_id) not in claim_ids:
                    raise InvalidInputError("ReportBundle token references a missing Claim")
                if kind == "evidence_ref" and str(ref_id) not in evidence_by_id:
                    raise InvalidInputError("ReportBundle token references missing Evidence")
                if kind == "gap_ref" and str(ref_id) not in gap_by_id:
                    raise InvalidInputError("ReportBundle token references a missing KnowledgeGap")


def _visible_text(value: str) -> str:
    return "".join(_VISIBLE_CONTROL_LABELS.get(character, character) for character in value)


def _escape_markdown_text(value: str) -> str:
    value = _visible_text(value)
    escaped = value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    escaped = re.sub(r"([\\`*_{}\[\]()#+.!|~-])", r"\\\1", escaped)
    return _URL_SCHEME.sub(lambda match: f"{match.group(1)}\\:", escaped)


def _inline_code(value: str) -> str:
    visible = _visible_text(value).replace("\r", "\\r").replace("\n", "\\n")
    visible = visible.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    runs = [len(match.group(0)) for match in re.finditer(r"`+", visible)]
    delimiter = "`" * (max(runs, default=0) + 1)
    padding = " " if runs else ""
    return f"{delimiter}{padding}{visible}{padding}{delimiter}"


def _markdown_tokens(tokens: list[TokenValue]) -> str:
    fragments: list[str] = []
    for token in tokens:
        kind = str(token["kind"])
        value = str(token["value"])
        if kind == "code":
            fragments.append(_inline_code(value))
        elif kind == "emphasis":
            fragments.append(f"**{_escape_markdown_text(value)}**")
        elif kind in {"claim_ref", "evidence_ref", "gap_ref"}:
            fragments.append(
                f"{_escape_markdown_text(value)} "
                + _inline_code(f"{kind.removesuffix('_ref')}:{token['ref_id']}")
            )
        elif kind == "inert_url":
            fragments.append(f"{_inline_code(value)}（外部链接，未激活）")
        else:
            fragments.append(_escape_markdown_text(value))
    return "".join(fragments)


def _markdown_json(value: object) -> str:
    return _inline_code(_canonical_bytes(value).decode("utf-8"))


def _format_bps(value: int) -> str:
    return f"{value // 100}.{value % 100:02d}%"


def _review_mastery_label(value: object) -> str:
    labels: dict[object, str] = {
        None: "未评估",
        "unfamiliar": "不熟悉",
        "developing": "正在掌握",
        "solid": "较扎实",
        "mastered": "已掌握",
    }
    return labels.get(value, str(value))


def _review_continuity_label(value: object) -> str:
    labels: dict[object, str] = {
        "new": "待首次复习",
        "continued": "状态延续",
        "reassess_required": "需要重评",
    }
    return labels.get(value, str(value))


def render_report_markdown(bundle: JSONObject) -> str:
    canonical_report_bundle(bundle)
    role = cast(JSONObject, bundle["role"])
    role_lens = cast(JSONObject, bundle["role_lens"])
    coverage = cast(JSONObject, bundle["coverage"])
    projects = cast(list[JSONObject], bundle["projects"])
    claims = cast(list[JSONObject], bundle["claims"])
    evidence = cast(list[JSONObject], bundle["evidence"])
    gaps = cast(list[JSONObject], bundle["knowledge_gaps"])
    evidence_by_id = {str(item["evidence_id"]): item for item in evidence}
    modules_by_id = {
        str(module["module_id"]): module
        for project in projects
        for module in cast(list[JSONObject], project["modules"])
    }
    claims_by_project: dict[str, list[JSONObject]] = {}
    gaps_by_project: dict[str, list[JSONObject]] = {}
    for claim in claims:
        claims_by_project.setdefault(str(claim["project_id"]), []).append(claim)
    for gap in gaps:
        if gap["project_id"] is not None:
            gaps_by_project.setdefault(str(gap["project_id"]), []).append(gap)

    lines = [
        f"# {_escape_markdown_text(str(role['name']))} 岗位准备包",
        "",
        (
            f"> 状态：`{bundle['package_status']}` · 运行："
            f"`{bundle['preparation_run_id']}` · Bundle：`{bundle['bundle_sha256']}` · "
            f"生成：`{bundle['generated_at']}` · 已冻结、只读"
        ),
        "",
        "## 岗位总览",
        "",
        f"- 目标岗位：{_escape_markdown_text(str(role['name']))}",
        (
            f"- 职级：{_escape_markdown_text(str(role.get('applied_level') or '未指定'))}"
            f"（来源：`{role['level_source']}`）"
        ),
    ]
    jd = cast(JSONObject, role["jd"])
    if jd["has_jd"]:
        lines.append(f"- JD：`{jd['input_kind']}`，内容哈希 `{jd['content_sha256']}`")
    else:
        lines.append("- JD：未提供；以下岗位镜头明确依赖冻结假设。")
    assumptions = role_lens["assumptions"]
    if isinstance(assumptions, list):
        for assumption in assumptions:
            lines.append(f"- 假设：{_escape_markdown_text(str(assumption))}")
    lines.extend(["", "### 岗位镜头", ""])
    for dimension in cast(list[JSONObject], role_lens["dimensions"]):
        lines.append(
            f"- **{_escape_markdown_text(str(dimension['display_name']))}**："
            f"`{_format_bps(int(cast(int, dimension['weight_bps'])))}` "
            f"(`{dimension['weight_bps']} bps`)；"
            f"{_escape_markdown_text(str(dimension['evaluation_criteria']))}"
        )
    lines.extend(
        [
            "",
            "## 覆盖与限制",
            "",
            (
                f"共 `{coverage['projects_total']}` 个项目，"
                f"`{coverage['eligible_projects']}` 个参与评分。"
            ),
            f"Disposition：{_markdown_json(coverage['disposition_counts'])}",
            "",
        ]
    )
    limitations = cast(list[JSONObject], coverage["limitations"])
    if limitations:
        for limitation in limitations:
            lines.extend(
                [
                    f"- **{limitation['severity']} · {limitation['kind']}**："
                    f"{_markdown_tokens(cast(list[TokenValue], limitation['message_tokens']))}",
                    "  - 影响："
                    f"{_markdown_tokens(cast(list[TokenValue], limitation['impact_tokens']))}",
                    "  - 补救："
                    f"{_markdown_tokens(cast(list[TokenValue], limitation['remediation_tokens']))}",
                ]
            )
    else:
        lines.append("- 本次未记录覆盖限制。")
    lines.extend(["", "## 项目排序与讲解", ""])
    for project in projects:
        assessment = project["assessment"]
        prefix = (
            f"{cast(JSONObject, assessment)['rank']}. " if isinstance(assessment, dict) else "- "
        )
        lines.append(
            f"{prefix}**{_escape_markdown_text(str(project['display_name']))}** · "
            f"`{project['snapshot_disposition']}`"
        )
        if isinstance(assessment, dict):
            lines.append(
                f"   - 分数：`{assessment['base_score_milli']}` × "
                f"`{_format_bps(int(cast(int, assessment['coverage_bps'])))}` → "
                f"`{assessment['final_score_milli']}`"
            )
            lines.append(
                "   - 排序理由："
                + _markdown_tokens(cast(list[TokenValue], assessment["rationale_tokens"]))
            )
        else:
            lines.append("   - 仅进入覆盖，不评分。")
        modules = cast(list[JSONObject], project["modules"])
        if modules:
            lines.append(
                "   - 模块："
                + "、".join(
                    f"{_escape_markdown_text(str(module['name']))} "
                    f"({_inline_code(str(module['relative_root']))})"
                    for module in modules
                )
            )
    for project in projects:
        project_id = str(project["project_id"])
        lines.extend(
            [
                "",
                f"### {_escape_markdown_text(str(project['display_name']))}",
                "",
                f"- Project ID：`{project_id}`",
                f"- Snapshot：`{project['project_snapshot_id'] or '无基线'}`",
                f"- Disposition：`{project['snapshot_disposition']}`",
            ]
        )
        project_claims = claims_by_project.get(project_id, [])
        if not project_claims:
            lines.append("- 本次无可发布 Claim。")
            continue
        learning_claims = [claim for claim in project_claims if claim["category"] == "learning"]
        candidate_learning = (
            []
            if learning_claims
            else [
                claim for claim in project_claims if claim["personal_attribution"] == "capability"
            ]
        )
        implementation_claims = [
            claim for claim in project_claims if claim["category"] == "implementation_method"
        ]
        lines.extend(["", "#### 学习要点", ""])
        if learning_claims:
            for claim in learning_claims:
                lines.append(
                    "- "
                    + _markdown_tokens(cast(list[TokenValue], claim["statement_tokens"]))
                    + f"（Claim `{claim['claim_id']}`）"
                )
        elif candidate_learning:
            lines.append("- 未冻结个人学习复盘；以下仅为可复习的候选学习要点：")
            for claim in candidate_learning:
                lines.append(
                    "  - "
                    + _markdown_tokens(cast(list[TokenValue], claim["statement_tokens"]))
                    + f"（Claim `{claim['claim_id']}`）"
                )
        else:
            lines.append("- 本次没有可由当前 Evidence 支撑的学习 Claim，保留为知识缺口。")
        lines.extend(["", "#### 如何实现", ""])
        if implementation_claims:
            for claim in implementation_claims:
                lines.append(
                    "- "
                    + _markdown_tokens(cast(list[TokenValue], claim["statement_tokens"]))
                    + f"（Claim `{claim['claim_id']}`）"
                )
        else:
            lines.append("- 本次未冻结 implementation_method Claim，不从其他类别推断实现方式。")
        lines.extend(["", "#### 全部 Claim 与证据", ""])
        for claim in project_claims:
            lines.extend(
                [
                    "",
                    f"#### {_escape_markdown_text(str(claim['category']))} · "
                    f"Claim `{claim['claim_id']}`",
                    "",
                    _markdown_tokens(cast(list[TokenValue], claim["statement_tokens"])),
                    "",
                    (
                        f"- 范围：`{claim['scope_kind']}`；支持："
                        f"`{claim['support_level']}`；归因："
                        f"`{claim['personal_attribution']}`"
                    ),
                    "- Facets："
                    + "、".join(f"`{facet}`" for facet in cast(list[str], claim["facets"])),
                ]
            )
            for relation in cast(list[JSONObject], claim["evidence_relations"]):
                item = evidence_by_id[str(relation["evidence_id"])]
                module = (
                    modules_by_id.get(str(item["module_id"]))
                    if item["module_id"] is not None
                    else None
                )
                worktree = cast(JSONObject | None, item["worktree"])
                worktree_label = "not_applicable"
                if worktree is not None:
                    branch_suffix = f" · {worktree['branch']}" if worktree["branch"] else ""
                    worktree_label = f"{worktree['worktree_id']}{branch_suffix}"
                validity_note = {
                    "current": "当前",
                    "stale": "只作历史限制",
                    "missing": "定位已缺失",
                    "plan": "已规划/已文档化",
                }[str(item["validity"])]
                lines.extend(
                    [
                        f"- Evidence `{item['evidence_id']}` · `{relation['relation']}` · "
                        f"`{item['evidence_kind']}` · `{item['commit_state']}` · "
                        f"`{item['validity']}`（{validity_note}）",
                        "  - 概述："
                        + _markdown_tokens(cast(list[TokenValue], item["summary_tokens"])),
                        "  - 模块："
                        + _inline_code(
                            str(module["name"]) if module is not None else "not_applicable"
                        ),
                        "  - Worktree：" + _inline_code(worktree_label),
                        "  - 路径：" + _inline_code(str(item["relative_path"] or "not_applicable")),
                        f"  - Locator：{_markdown_json(item['locator'])}",
                        f"  - 内容哈希：`{item['content_sha256'] or 'not_applicable'}`",
                        "  - Supported facets："
                        + "、".join(
                            f"`{facet}`" for facet in cast(list[str], relation["supported_facets"])
                        ),
                    ]
                )
        project_gaps = gaps_by_project.get(project_id, [])
        if project_gaps:
            lines.extend(["", "#### 知识缺口", ""])
            for gap in project_gaps:
                lines.append(
                    f"- `{gap['severity']}` · `{gap['status']}` · "
                    + _markdown_tokens(cast(list[TokenValue], gap["description_tokens"]))
                    + f"（解决方式：`{gap['resolution_kind']}`）"
                )
    lines.extend(["", "## 简历材料", ""])
    export_projection = cast(JSONObject, bundle["export_projection"])
    resume_item_count = 0
    for item in cast(list[JSONObject], export_projection["items"]):
        if item["export_kind"] != "resume":
            continue
        resume_item_count += 1
        lines.append(
            f"- {_markdown_tokens(cast(list[TokenValue], item['source_tokens']))} "
            f"<!-- source_item_id: {item['source_item_id']} -->"
        )
        lines.append(
            "  - 证据："
            + "、".join(
                f"`{evidence_id}`" for evidence_id in cast(list[str], item["evidence_refs"])
            )
        )
    if resume_item_count == 0:
        lines.append("- 当前没有满足证据门槛的候选简历 bullet。")
    lines.extend(["", "### STAR 素材", ""])
    star_sections = (
        ("S · 情境", {"business", "challenge"}),
        ("T · 任务与职责", {"contribution"}),
        ("A · 行动", {"implementation_method", "architecture", "technology", "tradeoff"}),
        ("R · 结果", {"outcome"}),
    )
    for project in projects:
        project_id = str(project["project_id"])
        project_claims = claims_by_project.get(project_id, [])
        lines.append(f"#### {_escape_markdown_text(str(project['display_name']))}")
        for label, categories in star_sections:
            matching = [claim for claim in project_claims if claim["category"] in categories]
            if matching:
                lines.append(f"- **{label}**")
                for claim in matching:
                    lines.append(
                        "  - "
                        + _markdown_tokens(cast(list[TokenValue], claim["statement_tokens"]))
                        + f"（Claim `{claim['claim_id']}`）"
                    )
            else:
                lines.append(f"- **{label}**：未冻结可用 Claim，不补造内容。")
        lines.append("")
    lines.extend(["", "## 面试与复习", ""])
    interview = cast(JSONObject, bundle["interview"])
    for question in cast(list[JSONObject], interview["questions"]):
        lines.extend(
            [
                f"- **{_markdown_tokens(cast(list[TokenValue], question['prompt_tokens']))}**",
                "  - 追问："
                + _markdown_tokens(cast(list[TokenValue], question["follow_up_tokens"])),
            ]
        )
    review = cast(JSONObject, bundle["review"])
    lines.extend(
        [
            "",
            f"复习状态截止：`{review['cutoff_at']}`；`{review['status']}`。",
        ]
    )
    for binding in cast(list[JSONObject], review["bindings"]):
        mastery = _review_mastery_label(binding["mastery_level"])
        continuity = _review_continuity_label(binding["continuity_status"])
        next_review = binding["next_review_at"] or "未设置复习日期"
        lines.append(
            "- "
            + _inline_code(str(binding["review_target_id"]))
            + f" · {continuity} · 当前掌握度：{mastery} · {next_review}"
        )
        if binding["summary"]:
            lines.append("  - 摘要：" + _escape_markdown_text(str(binding["summary"])))
        weak_points = cast(list[object], binding["weak_points"])
        if weak_points:
            lines.append(
                "  - 薄弱点："
                + "；".join(_escape_markdown_text(str(value)) for value in weak_points)
            )
        historical_value = binding.get("historical_review")
        if isinstance(historical_value, dict):
            historical = cast(JSONObject, historical_value)
            historical_next = historical["next_review_at"] or "未设置复习日期"
            lines.append(
                "  - 上次复盘（仅供历史参考，不代表当前掌握度）："
                + _review_mastery_label(historical["mastery_level"])
                + f" · {historical['reviewed_at']} · {historical_next}"
            )
            lines.append("    - 历史摘要：" + _escape_markdown_text(str(historical["summary"])))
            historical_weak_points = cast(list[object], historical["weak_points"])
            if historical_weak_points:
                lines.append(
                    "    - 历史薄弱点："
                    + "；".join(
                        _escape_markdown_text(str(value)) for value in historical_weak_points
                    )
                )
    lines.extend(
        [
            "",
            f"更新出口：{_inline_code(str(review['skill_invocation']))}",
            "",
        ]
    )
    return "\n".join(lines)


def render_resume_markdown(bundle: JSONObject) -> str:
    canonical_report_bundle(bundle)
    role = cast(JSONObject, bundle["role"])
    projects = {
        str(project["project_id"]): project
        for project in cast(list[JSONObject], bundle["projects"])
    }
    export_projection = cast(JSONObject, bundle["export_projection"])
    items = [
        item
        for item in cast(list[JSONObject], export_projection["items"])
        if item["export_kind"] == "resume"
    ]
    by_project: dict[str, list[JSONObject]] = {}
    for item in items:
        by_project.setdefault(str(item["project_id"]), []).append(item)
    lines = [
        f"# {_escape_markdown_text(str(role['name']))} 简历源稿",
        "",
        (
            f"> 冻结来源：`{bundle['preparation_run_id']}` · "
            f"`{bundle['bundle_sha256']}` · 状态 `{bundle['package_status']}`"
        ),
        "",
    ]
    for project_id, project_items in by_project.items():
        project = projects[project_id]
        lines.extend(
            [
                f"## {_escape_markdown_text(str(project['display_name']))}",
                "",
                f"> Disposition：`{project['snapshot_disposition']}`",
                "",
            ]
        )
        for item in project_items:
            lines.append(f"- {_markdown_tokens(cast(list[TokenValue], item['source_tokens']))}")
            lines.append(f"  <!-- source_item_id: {item['source_item_id']} -->")
            lines.append(
                "  <!-- evidence_refs: " + ",".join(cast(list[str], item["evidence_refs"])) + " -->"
            )
        lines.append("")
    if not items:
        lines.extend(["当前没有满足证据门槛的候选简历 bullet。", ""])
    return "\n".join(lines)


def _asset_text(name: str) -> str:
    resource = files("goodjob.dashboard_assets").joinpath(name)
    try:
        return resource.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError, UnicodeDecodeError) as exc:
        raise InvalidInputError(f"dashboard build asset is unavailable: {name}") from exc


def _embedded_json(bundle: JSONObject) -> str:
    value = canonical_report_bundle(bundle).decode("utf-8")
    escaped = (
        value.replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )
    for character in _VISIBLE_CONTROL_LABELS:
        escaped = escaped.replace(character, f"\\u{ord(character):04x}")
    return escaped


def _csp_hash(value: str) -> str:
    digest = hashlib.sha256(value.encode("utf-8")).digest()
    return base64.b64encode(digest).decode("ascii")


def render_dashboard_html(bundle: JSONObject) -> str:
    script = _asset_text("dashboard.js")
    style = _asset_text("dashboard.css")
    data = _embedded_json(bundle)
    script_hash = _csp_hash(script)
    data_hash = _csp_hash(data)
    style_hash = _csp_hash(style)
    csp = "; ".join(
        (
            "default-src 'none'",
            f"script-src 'sha256-{script_hash}' 'sha256-{data_hash}'",
            f"style-src 'sha256-{style_hash}'",
            "img-src 'none'",
            "font-src 'none'",
            "connect-src 'none'",
            "object-src 'none'",
            "frame-src 'none'",
            "form-action 'none'",
            "base-uri 'none'",
        )
    )
    html = (
        "<!doctype html>\n"
        '<html lang="zh-CN">\n'
        "<head>\n"
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width,initial-scale=1">\n'
        f'<meta http-equiv="Content-Security-Policy" content="{csp}">\n'
        "<title>GoodJob 岗位准备卷宗</title>\n"
        f"<style>{style}</style>\n"
        "</head>\n"
        "<body>\n"
        '<a class="skip-link" href="#main">跳到主要内容</a>\n'
        '<div id="app"><p class="boot-status">正在校验冻结报告…</p></div>\n'
        f'<script id="report-data" type="application/json">{data}</script>\n'
        f"<script>{script}</script>\n"
        "</body>\n"
        "</html>\n"
    )
    _validate_dashboard_document(html, script, style, data, csp)
    return html


def _validate_dashboard_document(
    html: str,
    script: str,
    style: str,
    data: str,
    csp: str,
) -> None:
    if re.search(r"\sstyle\s*=", html, re.IGNORECASE):
        raise InvalidInputError("dashboard output contains a forbidden style attribute")
    if re.search(r"<(?:img|iframe|object|embed|link)\b", html, re.IGNORECASE):
        raise InvalidInputError("dashboard output contains a forbidden external-resource element")
    if "unsafe-inline" in csp or "unsafe-eval" in csp:
        raise InvalidInputError("dashboard CSP contains an unsafe allowance")
    required = (
        f"'sha256-{_csp_hash(script)}'",
        f"'sha256-{_csp_hash(style)}'",
        f"'sha256-{_csp_hash(data)}'",
        "connect-src 'none'",
        "base-uri 'none'",
    )
    if not all(value in csp for value in required):
        raise InvalidInputError("dashboard CSP hash does not match inline content")


@dataclass(frozen=True)
class _RenderAttempt:
    render_attempt_id: str
    artifact_snapshot_id: str
    preparation_run_id: str
    report_bundle_sha256: str
    started_at: str
    temp_relative_path: str
    latest_temp_relative_path: str
    final_relative_path: str


def _artifact_snapshot_id(preparation_run_id: str, bundle_sha256: str) -> str:
    return str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"goodjob:artifact-snapshot:{preparation_run_id}:{bundle_sha256}",
        )
    )


def _snapshot_manifest(
    *,
    bundle: JSONObject,
    attempt: _RenderAttempt,
    file_hashes: dict[str, str],
) -> JSONObject:
    role_lens = cast(JSONObject, bundle["role_lens"])
    coverage = cast(JSONObject, bundle["coverage"])
    export_projection = cast(JSONObject, bundle["export_projection"])
    review = cast(JSONObject, bundle["review"])
    return {
        "contract_version": MANIFEST_CONTRACT_VERSION,
        "artifact_snapshot_id": attempt.artifact_snapshot_id,
        "render_attempt_id": attempt.render_attempt_id,
        "preparation_run_id": bundle["preparation_run_id"],
        "scan_run_id": bundle["scan_run_id"],
        "report_contract_version": REPORT_CONTRACT_VERSION,
        "report_bundle_contract_version": bundle["contract_version"],
        "report_bundle_sha256": bundle["bundle_sha256"],
        "package_status": bundle["package_status"],
        "primary_language": bundle["primary_language"],
        "generator_version": GENERATOR_VERSION,
        "created_at": attempt.started_at,
        "role_lens": {
            "role_lens_id": role_lens["role_lens_id"],
            "contract_version": role_lens["contract_version"],
            "lens_sha256": role_lens["lens_sha256"],
            "dimensions": role_lens["dimensions"],
        },
        "project_snapshots": [
            {
                "project_id": project["project_id"],
                "project_snapshot_id": project["project_snapshot_id"],
                "snapshot_disposition": project["snapshot_disposition"],
                "coverage_status": project["coverage_status"],
            }
            for project in cast(list[JSONObject], bundle["projects"])
        ],
        "claim_revisions": [
            {
                "claim_id": claim["claim_id"],
                "claim_revision_id": claim["claim_revision_id"],
                "revision_sha256": claim["revision_sha256"],
                "review_semantic_sha256": claim["review_semantic_sha256"],
            }
            for claim in cast(list[JSONObject], bundle["claims"])
        ],
        "evidence": [
            {
                "evidence_id": item["evidence_id"],
                "content_sha256": item["content_sha256"],
                "validity": item["validity"],
                "commit_state": item["commit_state"],
            }
            for item in cast(list[JSONObject], bundle["evidence"])
        ],
        "coverage_summary": {
            "projects_total": coverage["projects_total"],
            "eligible_projects": coverage["eligible_projects"],
            "disposition_counts": coverage["disposition_counts"],
            "limitation_count": len(cast(list[JSONObject], coverage["limitations"])),
        },
        "review_projection": {
            "contract_version": review["contract_version"],
            "cutoff_at": review["cutoff_at"],
            "binding_count": len(cast(list[object], review["bindings"])),
            "projection_sha256": _sha256_value(review),
        },
        "export_projection_sha256": export_projection["projection_sha256"],
        "files": [
            {"path": name, "sha256": file_hashes[name]}
            for name in (REPORT_FILENAME, RESUME_FILENAME, HTML_FILENAME)
        ],
    }


@contextmanager
def _connection_transaction(connection: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    connection.execute("BEGIN IMMEDIATE")
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise


class ArtifactSnapshotService:
    """Render and atomically publish one immutable Chinese snapshot per run."""

    def __init__(self, database: Database) -> None:
        self._database = database
        self._paths = database.paths
        self._files = SafeDataTree(
            self._paths.root,
            "artifacts",
            "artifact",
            frozenset({("artifacts", ".tmp")}),
        )

    def render(
        self,
        preparation_run_id: str,
        *,
        _fault_at: str | None = None,
    ) -> JSONObject:
        with self._database.exclusive_writer_connection() as connection:
            return self._render_locked(connection, preparation_run_id, _fault_at=_fault_at)

    def _render_locked(
        self,
        connection: sqlite3.Connection,
        preparation_run_id: str,
        *,
        _fault_at: str | None,
    ) -> JSONObject:
        self._recover_interrupted_attempts(connection)
        self._reconcile_latest(connection)
        existing = self._snapshot_result(connection, preparation_run_id)
        if existing is not None:
            return existing
        with _connection_transaction(connection):
            ReviewService.ensure_bindings_in_connection(connection, preparation_run_id)
        bundle = ReportBundleBuilder(self._database)._build_from_connection(
            connection, preparation_run_id
        )
        attempt = self._start_attempt(connection, preparation_run_id, str(bundle["bundle_sha256"]))
        try:
            report_bytes = (render_report_markdown(bundle) + "\n").encode("utf-8")
            resume_bytes = (render_resume_markdown(bundle) + "\n").encode("utf-8")
            html_bytes = render_dashboard_html(bundle).encode("utf-8")
            file_hashes = {
                REPORT_FILENAME: _sha256_bytes(report_bytes),
                RESUME_FILENAME: _sha256_bytes(resume_bytes),
                HTML_FILENAME: _sha256_bytes(html_bytes),
            }
            manifest = _snapshot_manifest(
                bundle=bundle,
                attempt=attempt,
                file_hashes=file_hashes,
            )
            manifest_bytes = _canonical_bytes(manifest) + b"\n"
            manifest_sha256 = _sha256_bytes(manifest_bytes)
            self._publish_directory(
                attempt,
                {
                    REPORT_FILENAME: report_bytes,
                    RESUME_FILENAME: resume_bytes,
                    HTML_FILENAME: html_bytes,
                    MANIFEST_FILENAME: manifest_bytes,
                },
                manifest,
                _fault_at=_fault_at,
            )
            if _fault_at == "after_publish":
                raise RuntimeError("injected render interruption after directory publication")
            self._record_success(
                connection, attempt, manifest_sha256, str(bundle["package_status"])
            )
            if _fault_at == "after_database_commit":
                raise RuntimeError("injected render interruption before latest reconciliation")
            self._reconcile_latest(connection)
        except Exception as exc:
            self._record_failure_if_running(connection, attempt, exc)
            self._cleanup_failed_attempt(connection, attempt)
            if isinstance(exc, InvalidInputError):
                raise
            raise InvalidInputError(
                "artifact rendering failed; inspect the RenderAttempt diagnostic"
            ) from exc
        result = self._snapshot_result(connection, preparation_run_id)
        if result is None:
            raise InvalidInputError("artifact publication completed without a snapshot record")
        return result

    def _recover_interrupted_attempts(self, connection: sqlite3.Connection) -> None:
        recovered: list[_RenderAttempt] = []
        timestamp = _now()
        with _connection_transaction(connection):
            rows = connection.execute(
                """
                SELECT ra.render_attempt_id, ra.preparation_run_id, ra.status,
                       ra.report_bundle_sha256, ra.started_at,
                       ra.temp_relative_path, ra.latest_temp_relative_path,
                       ra.final_relative_path, ra.owner_process_identity
                FROM render_attempts AS ra
                LEFT JOIN artifact_snapshots AS a
                  ON a.render_attempt_id = ra.render_attempt_id
                WHERE ra.status IN ('running', 'failed', 'interrupted')
                  AND a.artifact_snapshot_id IS NULL
                ORDER BY ra.started_at, ra.render_attempt_id
                """
            ).fetchall()
            for row in rows:
                status = str(row["status"])
                if status == "running" and not owner_process_stopped(
                    str(row["owner_process_identity"])
                ):
                    continue
                artifact_snapshot_id = _artifact_snapshot_id(
                    str(row["preparation_run_id"]), str(row["report_bundle_sha256"])
                )
                recovered.append(
                    _RenderAttempt(
                        str(row["render_attempt_id"]),
                        artifact_snapshot_id,
                        str(row["preparation_run_id"]),
                        str(row["report_bundle_sha256"]),
                        str(row["started_at"]),
                        str(row["temp_relative_path"]),
                        str(row["latest_temp_relative_path"]),
                        str(row["final_relative_path"]),
                    )
                )
                if status == "running":
                    connection.execute(
                        """
                        UPDATE render_attempts
                        SET status = 'interrupted', finished_at = ?,
                            error_summary = 'owner process stopped before publication completed'
                        WHERE render_attempt_id = ? AND status = 'running'
                        """,
                        (timestamp, str(row["render_attempt_id"])),
                    )
                    connection.execute(
                        """
                        UPDATE preparation_runs
                        SET status = 'render_failed',
                            status_reason = 'render attempt interrupted',
                            last_transition_at = ?, finished_at = ?
                        WHERE preparation_run_id = ? AND status = 'rendering'
                        """,
                        (timestamp, timestamp, str(row["preparation_run_id"])),
                    )
        for attempt in recovered:
            self._cleanup_failed_attempt(connection, attempt)

    def _start_attempt(
        self,
        connection: sqlite3.Connection,
        preparation_run_id: str,
        bundle_sha256: str,
    ) -> _RenderAttempt:
        timestamp = _now()
        render_attempt_id = str(uuid.uuid4())
        artifact_snapshot_id = _artifact_snapshot_id(preparation_run_id, bundle_sha256)
        attempt = _RenderAttempt(
            render_attempt_id,
            artifact_snapshot_id,
            preparation_run_id,
            bundle_sha256,
            timestamp,
            f"artifacts/.tmp/{render_attempt_id}",
            f"artifacts/.tmp/{render_attempt_id}.latest.tmp",
            f"artifacts/{artifact_snapshot_id}",
        )
        with _connection_transaction(connection):
            row = connection.execute(
                """
                SELECT status FROM preparation_runs WHERE preparation_run_id = ?
                """,
                (preparation_run_id,),
            ).fetchone()
            if row is None:
                raise InvalidInputError("PreparationRun does not exist")
            if str(row["status"]) not in {"ready", "render_failed"}:
                raise InvalidInputError("PreparationRun is not available for a new RenderAttempt")
            existing_hashes = {
                str(item["report_bundle_sha256"])
                for item in connection.execute(
                    """
                    SELECT report_bundle_sha256 FROM render_attempts
                    WHERE preparation_run_id = ?
                    """,
                    (preparation_run_id,),
                ).fetchall()
            }
            if existing_hashes and existing_hashes != {bundle_sha256}:
                raise InvalidInputError("render retry changed the frozen ReportBundle hash")
            connection.execute(
                """
                INSERT INTO render_attempts(
                    render_attempt_id, preparation_run_id, owner_process_identity,
                    report_bundle_sha256, generator_version, temp_relative_path,
                    latest_temp_relative_path, final_relative_path, started_at, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'running')
                """,
                (
                    attempt.render_attempt_id,
                    attempt.preparation_run_id,
                    process_identity(),
                    attempt.report_bundle_sha256,
                    GENERATOR_VERSION,
                    attempt.temp_relative_path,
                    attempt.latest_temp_relative_path,
                    attempt.final_relative_path,
                    attempt.started_at,
                ),
            )
            connection.execute(
                """
                UPDATE preparation_runs
                SET status = 'rendering', status_reason = NULL,
                    last_transition_at = ?, finished_at = NULL
                WHERE preparation_run_id = ?
                """,
                (timestamp, preparation_run_id),
            )
        return attempt

    def _publish_directory(
        self,
        attempt: _RenderAttempt,
        rendered_files: dict[str, bytes],
        manifest: JSONObject,
        *,
        _fault_at: str | None,
    ) -> None:
        temp_relative = self._attempt_relative(attempt, "temp")
        final_relative = self._attempt_relative(attempt, "final")

        def before_rename() -> None:
            if _fault_at == "after_temp":
                raise RuntimeError("injected render interruption after temporary files")

        self._files.publish_directory(
            temp_relative,
            final_relative,
            rendered_files,
            verify=lambda relative: self._verify_rendered_files(relative, manifest),
            before_rename=before_rename,
        )

    def _verify_rendered_files(self, directory_relative: str, manifest: JSONObject) -> None:
        if self._list_directory_relative(directory_relative) != {
            REPORT_FILENAME,
            RESUME_FILENAME,
            HTML_FILENAME,
            MANIFEST_FILENAME,
        }:
            raise InvalidInputError("artifact directory contains an unexpected file set")
        expected_files = {
            str(item["path"]): str(item["sha256"])
            for item in cast(list[JSONObject], manifest["files"])
        }
        if set(expected_files) != {REPORT_FILENAME, RESUME_FILENAME, HTML_FILENAME}:
            raise InvalidInputError("artifact manifest file set is incomplete")
        for name, expected_hash in expected_files.items():
            content = self._read_regular_relative(f"{directory_relative}/{name}")
            if _sha256_bytes(content) != expected_hash:
                raise InvalidInputError("rendered artifact hash does not match manifest")
        manifest_bytes = self._read_regular_relative(f"{directory_relative}/{MANIFEST_FILENAME}")
        try:
            parsed = json.loads(manifest_bytes)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise InvalidInputError("rendered artifact manifest is invalid") from exc
        if parsed != manifest or manifest_bytes != _canonical_bytes(manifest) + b"\n":
            raise InvalidInputError("artifact manifest is not canonical to rendered state")

    def _record_success(
        self,
        connection: sqlite3.Connection,
        attempt: _RenderAttempt,
        manifest_sha256: str,
        package_status: str,
    ) -> None:
        if package_status not in {"completed", "partial"}:
            raise InvalidInputError("artifact package status is not publishable")
        timestamp = _now()
        final_prefix = attempt.final_relative_path
        with _connection_transaction(connection):
            row = connection.execute(
                """
                SELECT status, report_bundle_sha256 FROM render_attempts
                WHERE render_attempt_id = ?
                """,
                (attempt.render_attempt_id,),
            ).fetchone()
            if row is None or str(row["status"]) != "running":
                raise InvalidInputError("RenderAttempt is no longer publishable")
            if str(row["report_bundle_sha256"]) != attempt.report_bundle_sha256:
                raise InvalidInputError("RenderAttempt bundle hash changed during publication")
            connection.execute(
                """
                INSERT INTO artifact_snapshots(
                    artifact_snapshot_id, preparation_run_id, render_attempt_id,
                    report_contract_version, report_bundle_sha256, manifest_sha256,
                    report_markdown_path, resume_markdown_path, html_path,
                    primary_language, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'zh-CN', ?)
                """,
                (
                    attempt.artifact_snapshot_id,
                    attempt.preparation_run_id,
                    attempt.render_attempt_id,
                    REPORT_CONTRACT_VERSION,
                    attempt.report_bundle_sha256,
                    manifest_sha256,
                    f"{final_prefix}/{REPORT_FILENAME}",
                    f"{final_prefix}/{RESUME_FILENAME}",
                    f"{final_prefix}/{HTML_FILENAME}",
                    timestamp,
                ),
            )
            connection.execute(
                """
                UPDATE render_attempts
                SET status = 'succeeded', finished_at = ?, error_summary = NULL
                WHERE render_attempt_id = ? AND status = 'running'
                """,
                (timestamp, attempt.render_attempt_id),
            )
            connection.execute(
                """
                UPDATE preparation_runs
                SET status = ?, status_reason = NULL,
                    last_transition_at = ?, finished_at = ?
                WHERE preparation_run_id = ? AND status = 'rendering'
                """,
                (package_status, timestamp, timestamp, attempt.preparation_run_id),
            )

    def _record_failure_if_running(
        self,
        connection: sqlite3.Connection,
        attempt: _RenderAttempt,
        exc: Exception,
    ) -> None:
        timestamp = _now()
        summary = f"{type(exc).__name__}: {str(exc)}"[:500]
        with _connection_transaction(connection):
            snapshot = connection.execute(
                """
                SELECT 1 FROM artifact_snapshots WHERE render_attempt_id = ?
                """,
                (attempt.render_attempt_id,),
            ).fetchone()
            if snapshot is not None:
                return
            cursor = connection.execute(
                """
                UPDATE render_attempts
                SET status = 'failed', finished_at = ?, error_summary = ?
                WHERE render_attempt_id = ? AND status = 'running'
                """,
                (timestamp, summary, attempt.render_attempt_id),
            )
            if cursor.rowcount:
                connection.execute(
                    """
                    UPDATE preparation_runs
                    SET status = 'render_failed', status_reason = ?,
                        last_transition_at = ?, finished_at = ?
                    WHERE preparation_run_id = ? AND status = 'rendering'
                    """,
                    (
                        "required artifact rendering failed",
                        timestamp,
                        timestamp,
                        attempt.preparation_run_id,
                    ),
                )

    def _cleanup_failed_attempt(
        self,
        connection: sqlite3.Connection,
        attempt: _RenderAttempt,
    ) -> None:
        snapshot = connection.execute(
            """
            SELECT 1 FROM artifact_snapshots WHERE artifact_snapshot_id = ?
            """,
            (attempt.artifact_snapshot_id,),
        ).fetchone()
        for kind in ("temp", "latest_temp"):
            self._remove_owned_relative(self._attempt_relative(attempt, kind))
        if snapshot is None:
            self._remove_owned_relative(self._attempt_relative(attempt, "final"))

    def _reconcile_latest(self, connection: sqlite3.Connection) -> None:
        row = connection.execute(
            """
            SELECT a.artifact_snapshot_id, a.preparation_run_id,
                   a.render_attempt_id, a.report_bundle_sha256,
                   a.report_contract_version, a.manifest_sha256,
                   a.report_markdown_path, a.resume_markdown_path, a.html_path,
                   a.created_at, pr.status AS package_status,
                   ra.latest_temp_relative_path
            FROM artifact_snapshots AS a
            JOIN preparation_runs AS pr
              ON pr.preparation_run_id = a.preparation_run_id
            JOIN render_attempts AS ra
              ON ra.render_attempt_id = a.render_attempt_id
            WHERE pr.status IN ('completed', 'partial')
              AND ra.status = 'succeeded'
            ORDER BY a.created_at DESC, a.artifact_snapshot_id DESC
            LIMIT 1
            """
        ).fetchone()
        if row is None:
            return
        self._validated_snapshot_files(row)
        payload: JSONObject = {
            "contract_version": "artifact-latest-v1",
            "artifact_snapshot_id": str(row["artifact_snapshot_id"]),
            "preparation_run_id": str(row["preparation_run_id"]),
            "report_bundle_sha256": str(row["report_bundle_sha256"]),
            "report_contract_version": str(row["report_contract_version"]),
            "package_status": str(row["package_status"]),
            "html_path": str(row["html_path"]),
            "created_at": str(row["created_at"]),
        }
        content = _canonical_bytes(payload) + b"\n"
        latest_relative = "artifacts/latest.json"
        try:
            current = self._read_regular_relative(latest_relative)
        except InvalidInputError:
            current = None
        if current == content:
            return
        temp_relative = str(row["latest_temp_relative_path"])
        self._remove_owned_relative(temp_relative)
        self._write_new_relative(temp_relative, content)
        self._replace_relative_file(
            temp_relative,
            latest_relative,
            mode=stat.S_IRUSR | stat.S_IWUSR,
        )

    def _snapshot_result(
        self,
        connection: sqlite3.Connection,
        preparation_run_id: str,
    ) -> JSONObject | None:
        row = connection.execute(
            """
            SELECT a.artifact_snapshot_id, a.preparation_run_id,
                   a.render_attempt_id, a.report_contract_version,
                   a.report_bundle_sha256, a.manifest_sha256,
                   a.report_markdown_path, a.resume_markdown_path,
                   a.html_path, a.primary_language, a.created_at,
                   pr.status AS package_status
            FROM artifact_snapshots AS a
            JOIN preparation_runs AS pr
              ON pr.preparation_run_id = a.preparation_run_id
            WHERE a.preparation_run_id = ?
            """,
            (preparation_run_id,),
        ).fetchone()
        if row is None:
            return None
        paths, manifest_path, _ = self._validated_snapshot_files(row)
        return {
            "status": "ok",
            "run_status": str(row["package_status"]),
            "artifact_snapshot": {
                "artifact_snapshot_id": str(row["artifact_snapshot_id"]),
                "preparation_run_id": str(row["preparation_run_id"]),
                "render_attempt_id": str(row["render_attempt_id"]),
                "report_contract_version": str(row["report_contract_version"]),
                "report_bundle_sha256": str(row["report_bundle_sha256"]),
                "manifest_sha256": str(row["manifest_sha256"]),
                "report_markdown_path": str(paths["report_markdown"]),
                "resume_markdown_path": str(paths["resume_markdown"]),
                "html_path": str(paths["html"]),
                "manifest_path": str(manifest_path),
                "primary_language": str(row["primary_language"]),
                "created_at": str(row["created_at"]),
            },
            "latest_path": str(self._paths.latest_artifact_file),
        }

    def require_valid_snapshot_from_connection(
        self,
        connection: sqlite3.Connection,
        preparation_run_id: str,
    ) -> None:
        """Reject a missing, linked, tampered, or incomplete published snapshot."""
        if self._snapshot_result(connection, preparation_run_id) is None:
            raise InvalidInputError(
                "mock review requires a successfully published ArtifactSnapshot"
            )

    def read_verified_html_from_connection(
        self,
        connection: sqlite3.Connection,
        artifact_snapshot_id: str,
    ) -> tuple[JSONObject, bytes]:
        """Return one fully verified frozen dashboard and its export identity."""
        row = connection.execute(
            """
            SELECT a.artifact_snapshot_id, a.preparation_run_id,
                   a.render_attempt_id, a.report_contract_version,
                   a.report_bundle_sha256, a.manifest_sha256,
                   a.report_markdown_path, a.resume_markdown_path,
                   a.html_path, a.primary_language, a.created_at,
                   pr.status AS package_status, w.canonical_root,
                   ra.status AS render_status
            FROM artifact_snapshots AS a
            JOIN preparation_runs AS pr
              ON pr.preparation_run_id = a.preparation_run_id
            JOIN workspaces AS w ON w.workspace_id = pr.workspace_id
            JOIN render_attempts AS ra ON ra.render_attempt_id = a.render_attempt_id
            WHERE a.artifact_snapshot_id = ?
            """,
            (artifact_snapshot_id,),
        ).fetchone()
        if row is None:
            raise InvalidInputError("source ArtifactSnapshot does not exist")
        if str(row["render_status"]) != "succeeded" or str(row["package_status"]) not in {
            "completed",
            "partial",
        }:
            raise InvalidInputError("source ArtifactSnapshot is not successfully published")
        paths, _, verified_contents = self._validated_snapshot_files(row)
        payload: JSONObject = {
            "artifact_snapshot_id": str(row["artifact_snapshot_id"]),
            "preparation_run_id": str(row["preparation_run_id"]),
            "report_bundle_sha256": str(row["report_bundle_sha256"]),
            "report_contract_version": str(row["report_contract_version"]),
            "canonical_workspace_root": str(row["canonical_root"]),
            "html_path": str(paths["html"]),
            "created_at": str(row["created_at"]),
        }
        return payload, verified_contents["html"]

    def _validated_snapshot_files(
        self,
        row: sqlite3.Row,
    ) -> tuple[dict[str, Path], Path, dict[str, bytes]]:
        snapshot_id = str(row["artifact_snapshot_id"])
        base = f"artifacts/{snapshot_id}"
        relative_paths = {
            "report_markdown": str(row["report_markdown_path"]),
            "resume_markdown": str(row["resume_markdown_path"]),
            "html": str(row["html_path"]),
        }
        expected_relatives = {
            "report_markdown": f"{base}/{REPORT_FILENAME}",
            "resume_markdown": f"{base}/{RESUME_FILENAME}",
            "html": f"{base}/{HTML_FILENAME}",
        }
        if relative_paths != expected_relatives:
            raise InvalidInputError("ArtifactSnapshot paths do not match its immutable identity")
        manifest_relative = f"{base}/{MANIFEST_FILENAME}"
        if self._list_directory_relative(base) != {
            REPORT_FILENAME,
            RESUME_FILENAME,
            HTML_FILENAME,
            MANIFEST_FILENAME,
        }:
            raise InvalidInputError("ArtifactSnapshot directory file set does not match")
        manifest_bytes = self._read_regular_relative(manifest_relative)
        if _sha256_bytes(manifest_bytes) != str(row["manifest_sha256"]):
            raise InvalidInputError("ArtifactSnapshot manifest digest does not match")
        try:
            manifest_value = json.loads(manifest_bytes)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise InvalidInputError("ArtifactSnapshot manifest is not valid JSON") from exc
        if not isinstance(manifest_value, dict):
            raise InvalidInputError("ArtifactSnapshot manifest is not an object")
        manifest = cast(JSONObject, manifest_value)
        if _canonical_bytes(manifest) + b"\n" != manifest_bytes:
            raise InvalidInputError("ArtifactSnapshot manifest is not canonical")
        if (
            manifest.get("artifact_snapshot_id") != snapshot_id
            or manifest.get("preparation_run_id") != str(row["preparation_run_id"])
            or manifest.get("render_attempt_id") != str(row["render_attempt_id"])
            or manifest.get("report_bundle_sha256") != str(row["report_bundle_sha256"])
            or manifest.get("report_contract_version") != str(row["report_contract_version"])
        ):
            raise InvalidInputError(
                "ArtifactSnapshot manifest identity does not match the database"
            )
        files_value = manifest.get("files")
        if not isinstance(files_value, list):
            raise InvalidInputError("ArtifactSnapshot manifest file list is missing")
        expected_hashes: dict[str, str] = {}
        for item in files_value:
            if not isinstance(item, dict) or set(item) != {"path", "sha256"}:
                raise InvalidInputError("ArtifactSnapshot manifest file entry is invalid")
            name = item.get("path")
            digest = item.get("sha256")
            if not isinstance(name, str) or not isinstance(digest, str) or name in expected_hashes:
                raise InvalidInputError("ArtifactSnapshot manifest file entry is invalid")
            expected_hashes[name] = digest
        expected_names = {REPORT_FILENAME, RESUME_FILENAME, HTML_FILENAME}
        if set(expected_hashes) != expected_names:
            raise InvalidInputError("ArtifactSnapshot manifest file set is incomplete")
        verified_contents: dict[str, bytes] = {}
        for key, relative in relative_paths.items():
            name = {
                "report_markdown": REPORT_FILENAME,
                "resume_markdown": RESUME_FILENAME,
                "html": HTML_FILENAME,
            }[key]
            content = self._read_regular_relative(relative)
            if _sha256_bytes(content) != expected_hashes[name]:
                raise InvalidInputError(f"ArtifactSnapshot {name} digest does not match")
            verified_contents[key] = content
        return (
            {key: self._safe_data_path(relative) for key, relative in relative_paths.items()},
            self._safe_data_path(manifest_relative),
            verified_contents,
        )

    def _attempt_relative(self, attempt: _RenderAttempt, kind: str) -> str:
        relative = {
            "temp": attempt.temp_relative_path,
            "latest_temp": attempt.latest_temp_relative_path,
            "final": attempt.final_relative_path,
        }.get(kind)
        if relative is None:
            raise AssertionError(f"unknown attempt path kind: {kind}")
        expected = {
            "temp": ("artifacts", ".tmp", attempt.render_attempt_id),
            "latest_temp": (
                "artifacts",
                ".tmp",
                f"{attempt.render_attempt_id}.latest.tmp",
            ),
            "final": ("artifacts", attempt.artifact_snapshot_id),
        }[kind]
        pure = PurePosixPath(relative)
        if pure.is_absolute() or pure.parts != expected:
            raise InvalidInputError("RenderAttempt path is outside its registered ownership")
        return relative

    def _safe_data_path(self, relative: str) -> Path:
        return self._files.path(relative)

    def _read_regular_relative(self, relative: str) -> bytes:
        return self._files.read_regular(relative)

    def _list_directory_relative(self, relative: str) -> set[str]:
        return self._files.list_directory(relative)

    def _write_new_relative(self, relative: str, content: bytes) -> None:
        self._files.write_new(relative, content)

    def _replace_relative_file(self, source: str, destination: str, *, mode: int) -> None:
        self._files.replace_file(source, destination, mode=mode)

    def _remove_owned_relative(self, relative: str) -> None:
        self._files.remove(relative)

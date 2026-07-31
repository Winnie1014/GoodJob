"""Validated, all-or-nothing freezing of model-produced career analysis."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from goodjob.db import Database
from goodjob.errors import InvalidInputError
from goodjob.history import HistoryQueryService
from goodjob.preparation import (
    MAX_PRIVATE_PAYLOAD_BYTES,
    MAX_SOURCE_REVISION_BATCH,
    AssessmentScoreDraft,
    JsonObject,
    JsonValue,
    PreparationService,
    RoleDimension,
    SourceCheckResult,
    _canonical_json,
    _check_source,
    _dimensions,
    _new_id,
    _now,
    _object,
    _reject_unknown_fields,
    _stored_json,
    _text,
    _workspace_source_locator,
    score_project_assessments,
)
from goodjob.review import ReviewService
from goodjob.source_io import open_regular_file, read_open_file

ANALYSIS_COMMIT_VERSION = "analysis-commit-v1"
MAX_EVIDENCE_DRAFTS = 400
MAX_CLAIMS = 600
MAX_ASSESSMENTS = 200
MAX_GAPS = 600
MAX_RELATIONS_PER_CLAIM = 80
MAX_INLINE_TOKENS = 160
MAX_REFERENCE_LIST = 1000
MAX_SEMANTIC_KEYS = 100

CLAIM_CATEGORIES = frozenset(
    {
        "technology",
        "business",
        "architecture",
        "implementation_method",
        "challenge",
        "tradeoff",
        "contribution",
        "outcome",
        "learning",
        "knowledge_gap",
    }
)
FACETS = frozenset(
    {"implemented", "test_defined", "test_verified", "documented", "planned", "user_reported"}
)
SUPPORT_LEVELS = frozenset({"single_source", "cross_checked", "user_confirmed", "conflicted"})
RELATIONS = frozenset({"supports", "contradicts", "contextualizes"})
INLINE_TOKEN_KINDS = frozenset(
    {
        "text",
        "code",
        "emphasis",
        "claim_ref",
        "evidence_ref",
        "gap_ref",
        "inert_url",
    }
)
ORIGIN_KINDS = frozenset({"source_revision", "git_commit"})
COMMIT_STATES = frozenset({"committed", "modified", "untracked", "historical", "not_applicable"})
PERSONAL_ATTRIBUTIONS = frozenset(
    {
        "none",
        "capability",
        "personal_learning",
        "implemented",
        "responsible",
        "led",
        "personal_outcome",
    }
)

_PERSONAL_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "personal_learning",
        re.compile(r"我(?:当时|从[^，。]{0,40})?学(?:到|会|习)|\bI\s+learned\b", re.IGNORECASE),
    ),
    (
        "personal_outcome",
        re.compile(
            r"我(?:推动|取得|达成|实现了[^，。]{0,30}(?:提升|降低|增长|减少))|"
            r"\bI\s+(?:achieved|drove|delivered)\b",
            re.IGNORECASE,
        ),
    ),
    ("led", re.compile(r"我(?:主导|牵头)|\bI\s+led\b", re.IGNORECASE)),
    (
        "responsible",
        re.compile(r"我(?:负责|承担)|\bI\s+(?:owned|was responsible for)\b", re.IGNORECASE),
    ),
    (
        "implemented",
        re.compile(
            r"(?:由我|我|本人)(?:独立)?(?:实现|开发|编写|搭建|打造|创建|完成|交付|落地|"
            r"设计并实现)|"
            r"\bI\s+(?:independently\s+)?(?:implemented|built|developed|created|authored|"
            r"completed|shipped)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "capability",
        re.compile(r"我(?:能|可以)(?:解释|讲解|复习)|\bI\s+can\s+explain\b", re.IGNORECASE),
    ),
)
_CLAIM_BULLET_PREFIX = r"^\s*(?:(?:[-*+•]|\d+[.)])\s*)?"
_OMITTED_SUBJECT_PERSONAL_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "personal_learning",
        re.compile(
            _CLAIM_BULLET_PREFIX + r"(?:(?:学习|学会|掌握)|(?:Learned|Mastered)\b)",
            re.IGNORECASE,
        ),
    ),
    (
        "personal_outcome",
        re.compile(
            _CLAIM_BULLET_PREFIX + r"(?:(?:推动|取得|达成|提升|降低|增长|减少)|"
            r"(?:Drove|Achieved|Improved|Reduced|Increased)\b)",
            re.IGNORECASE,
        ),
    ),
    (
        "led",
        re.compile(
            _CLAIM_BULLET_PREFIX + r"(?:(?:主导|牵头)|(?:Led|Spearheaded)\b)",
            re.IGNORECASE,
        ),
    ),
    (
        "responsible",
        re.compile(
            _CLAIM_BULLET_PREFIX + r"(?:(?:负责|承担)|(?:Owned|Managed)\b)",
            re.IGNORECASE,
        ),
    ),
    (
        "implemented",
        re.compile(
            _CLAIM_BULLET_PREFIX + r"(?:独立)?(?:(?:实现|开发|编写|搭建|打造|创建|完成|交付|"
            r"落地|设计并实现)|(?:Implemented|Built|Developed|Created|Authored|Completed|"
            r"Shipped|Delivered|Designed)\b)",
            re.IGNORECASE,
        ),
    ),
    (
        "capability",
        re.compile(
            _CLAIM_BULLET_PREFIX + r"(?:(?:能|能够|可以)(?:解释|讲解|复习)|(?:Can explain)\b)",
            re.IGNORECASE,
        ),
    ),
)
_FIRST_PERSON_PATTERN = re.compile(r"(?:我|本人)|\b(?:I|my|me)\b", re.IGNORECASE)
_SOURCE_EXCERPT_NGRAM_CHARS = 24


def _bounded_list(
    value: object,
    field_name: str,
    *,
    maximum: int,
    allow_empty: bool = False,
) -> list[object]:
    if not isinstance(value, list) or (not allow_empty and not value) or len(value) > maximum:
        qualifier = "bounded JSON list" if allow_empty else "bounded non-empty JSON list"
        raise InvalidInputError(f"{field_name} must be a {qualifier}")
    return cast(list[object], value)


def _stable_key(value: object, field_name: str, *, maximum: int = 160) -> str:
    key = _text(value, field_name, maximum=maximum)
    if any(character not in "abcdefghijklmnopqrstuvwxyz0123456789_.-" for character in key):
        raise InvalidInputError(f"{field_name} must use lowercase stable-key characters")
    return key


def _optional_text(value: object, field_name: str, *, maximum: int = 2000) -> str | None:
    if value is None:
        return None
    return _text(value, field_name, maximum=maximum)


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _sha256_value(value: JsonValue, field_name: str) -> str:
    return _sha256_text(_canonical_json(value, field_name, maximum_bytes=MAX_PRIVATE_PAYLOAD_BYTES))


def _normalized_excerpt(value: str) -> str:
    return " ".join(value.casefold().split())


def _reject_verbatim_source_summary(summary: str, *transient_sources: str) -> None:
    """Keep source/diff/blob text transient by rejecting verbatim summary overlap."""
    normalized_summary = _normalized_excerpt(summary)
    for source in transient_sources:
        normalized_source = _normalized_excerpt(source)
        if len(normalized_summary) >= 16 and normalized_summary in normalized_source:
            raise InvalidInputError("evidence summary must not contain source or diff text")
        if len(normalized_summary) >= _SOURCE_EXCERPT_NGRAM_CHARS:
            final_start = len(normalized_summary) - _SOURCE_EXCERPT_NGRAM_CHARS
            if any(
                normalized_summary[start : start + _SOURCE_EXCERPT_NGRAM_CHARS] in normalized_source
                for start in range(final_start + 1)
            ):
                raise InvalidInputError("evidence summary must not contain source or diff text")
        for raw_line in source.splitlines():
            line = raw_line.strip()
            if line.startswith(("+", "-")) and not line.startswith(("+++", "---")):
                line = line[1:].strip()
            normalized_line = _normalized_excerpt(line)
            if len(normalized_line) >= 16 and normalized_line in normalized_summary:
                raise InvalidInputError("evidence summary must not contain source or diff text")


def _digest(value: object, field_name: str) -> str:
    digest = _text(value, field_name, maximum=64)
    if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
        raise InvalidInputError(f"{field_name} must be a lowercase SHA-256 digest")
    return digest


def _string_set(
    value: object,
    field_name: str,
    *,
    allowed: frozenset[str] | None = None,
    maximum: int = 100,
    stable_keys: bool = False,
) -> tuple[str, ...]:
    raw = _bounded_list(value, field_name, maximum=maximum, allow_empty=True)
    result = tuple(
        _stable_key(item, field_name, maximum=160)
        if stable_keys
        else _text(item, field_name, maximum=200)
        for item in raw
    )
    if len(set(result)) != len(result):
        raise InvalidInputError(f"{field_name} must not contain duplicates")
    if allowed is not None and not set(result) <= allowed:
        raise InvalidInputError(f"{field_name} contains unsupported values")
    return tuple(sorted(result))


@dataclass(frozen=True)
class InlineToken:
    kind: str
    value: str
    ref_id: str | None

    @classmethod
    def from_value(cls, value: object, field_name: str) -> InlineToken:
        token = _object(value, field_name)
        _reject_unknown_fields(token, frozenset({"kind", "value", "ref_id"}), field_name)
        kind = _text(token.get("kind"), f"{field_name}.kind", maximum=40)
        if kind not in INLINE_TOKEN_KINDS:
            raise InvalidInputError(f"{field_name}.kind is not a supported ReportInlineToken")
        token_value = _text(
            token.get("value"),
            f"{field_name}.value",
            maximum=1000,
            allow_empty=False,
        )
        ref_id = _optional_text(token.get("ref_id"), f"{field_name}.ref_id", maximum=200)
        if kind in {"claim_ref", "evidence_ref", "gap_ref"}:
            if ref_id is None:
                raise InvalidInputError(f"{field_name}.ref_id is required for a reference token")
        elif ref_id is not None:
            raise InvalidInputError(f"{field_name}.ref_id is only valid for reference tokens")
        return cls(kind, token_value, ref_id)

    def as_json(self, resolved_ref_id: str | None = None) -> JsonObject:
        result: JsonObject = {"kind": self.kind, "value": self.value}
        if self.ref_id is not None:
            result["ref_id"] = resolved_ref_id or self.ref_id
        return result


def _tokens(value: object, field_name: str) -> tuple[InlineToken, ...]:
    result = tuple(
        InlineToken.from_value(item, f"{field_name}[{index}]")
        for index, item in enumerate(_bounded_list(value, field_name, maximum=MAX_INLINE_TOKENS))
    )
    if sum(len(token.value) for token in result) > 4000:
        raise InvalidInputError(f"{field_name} exceeds the rich-text boundary")
    return result


@dataclass(frozen=True)
class EvidenceDraft:
    draft_id: str
    origin_kind: str
    project_id: str
    worktree_id: str | None
    module_id: str | None
    evidence_kind: str
    locator: JsonObject
    summary: str
    commit_state: str
    source_revision_id: str | None
    observed_sha256: str | None
    candidate_id: str | None
    selected_path: str | None
    query_reason: str | None
    commit: str | None
    metadata_sha256: str | None
    diff_sha256: str | None
    blob_sha256: str | None

    @classmethod
    def from_value(cls, value: object, index: int) -> EvidenceDraft:
        draft = _object(value, f"evidence_drafts[{index}]")
        _reject_unknown_fields(
            draft,
            frozenset(
                {
                    "draft_id",
                    "origin_kind",
                    "project_id",
                    "worktree_id",
                    "module_id",
                    "evidence_kind",
                    "locator",
                    "summary",
                    "commit_state",
                    "source_revision_id",
                    "observed_sha256",
                    "candidate_id",
                    "selected_path",
                    "query_reason",
                    "commit",
                    "metadata_sha256",
                    "diff_sha256",
                    "blob_sha256",
                }
            ),
            f"evidence_drafts[{index}]",
        )
        origin_kind = _text(draft.get("origin_kind"), "origin_kind", maximum=40)
        if origin_kind not in ORIGIN_KINDS:
            raise InvalidInputError("EvidenceDraft origin_kind is not supported")
        commit_state = _text(draft.get("commit_state"), "commit_state", maximum=40)
        if commit_state not in COMMIT_STATES:
            raise InvalidInputError("EvidenceDraft commit_state is not supported")
        summary = _text(draft.get("summary"), "evidence summary", maximum=500)
        if summary.count("\n") > 8:
            raise InvalidInputError("evidence summary must not contain a source-sized excerpt")
        locator = _object(draft.get("locator"), "evidence locator")
        _canonical_json(locator, "evidence locator")
        source_revision_id = _optional_text(
            draft.get("source_revision_id"), "source_revision_id", maximum=200
        )
        observed_sha256 = (
            _digest(draft.get("observed_sha256"), "observed_sha256")
            if draft.get("observed_sha256") is not None
            else None
        )
        candidate_id = _optional_text(draft.get("candidate_id"), "candidate_id", maximum=200)
        selected_path = _optional_text(draft.get("selected_path"), "selected_path", maximum=1000)
        query_reason = _optional_text(draft.get("query_reason"), "query_reason", maximum=500)
        commit = _optional_text(draft.get("commit"), "commit", maximum=100)
        metadata_sha256 = (
            _digest(draft.get("metadata_sha256"), "metadata_sha256")
            if draft.get("metadata_sha256") is not None
            else None
        )
        diff_sha256 = (
            _digest(draft.get("diff_sha256"), "diff_sha256")
            if draft.get("diff_sha256") is not None
            else None
        )
        blob_sha256 = (
            _digest(draft.get("blob_sha256"), "blob_sha256")
            if draft.get("blob_sha256") is not None
            else None
        )
        if origin_kind == "source_revision":
            if source_revision_id is None or observed_sha256 is None:
                raise InvalidInputError("source EvidenceDraft requires revision and observed hash")
            if any(
                item is not None
                for item in (
                    candidate_id,
                    selected_path,
                    query_reason,
                    commit,
                    metadata_sha256,
                    diff_sha256,
                    blob_sha256,
                )
            ):
                raise InvalidInputError("source EvidenceDraft cannot contain Git candidate fields")
            if commit_state == "historical":
                raise InvalidInputError("source EvidenceDraft cannot claim historical commit state")
        else:
            if (
                candidate_id is None
                or selected_path is None
                or query_reason is None
                or commit is None
                or metadata_sha256 is None
                or diff_sha256 is None
            ):
                raise InvalidInputError("Git EvidenceDraft requires its complete candidate proof")
            if source_revision_id is not None or observed_sha256 is not None:
                raise InvalidInputError("Git EvidenceDraft cannot contain SourceRevision fields")
            if commit_state != "historical":
                raise InvalidInputError("Git EvidenceDraft commit_state must be historical")
            if locator:
                raise InvalidInputError("Git EvidenceDraft locator is repository-generated")
        return cls(
            _text(draft.get("draft_id"), "evidence draft_id", maximum=200),
            origin_kind,
            _text(draft.get("project_id"), "project_id", maximum=200),
            _optional_text(draft.get("worktree_id"), "worktree_id", maximum=200),
            _optional_text(draft.get("module_id"), "module_id", maximum=200),
            _text(draft.get("evidence_kind"), "evidence_kind", maximum=100),
            locator,
            summary,
            commit_state,
            source_revision_id,
            observed_sha256,
            candidate_id,
            selected_path,
            query_reason,
            commit,
            metadata_sha256,
            diff_sha256,
            blob_sha256,
        )


@dataclass(frozen=True)
class EvidenceRelationDraft:
    evidence_ref: str
    relation: str
    supported_facets: tuple[str, ...]

    @classmethod
    def from_value(cls, value: object, index: int) -> EvidenceRelationDraft:
        relation = _object(value, f"evidence_relations[{index}]")
        _reject_unknown_fields(
            relation,
            frozenset({"evidence_ref", "relation", "supported_facets"}),
            f"evidence_relations[{index}]",
        )
        relation_kind = _text(relation.get("relation"), "relation", maximum=40)
        if relation_kind not in RELATIONS:
            raise InvalidInputError("Claim evidence relation is not supported")
        supported = _string_set(
            relation.get("supported_facets", []),
            "supported_facets",
            allowed=FACETS,
        )
        return cls(
            _text(relation.get("evidence_ref"), "evidence_ref", maximum=200),
            relation_kind,
            supported,
        )


@dataclass(frozen=True)
class ReviewSemanticDraft:
    concept_keys: tuple[str, ...]
    mechanism_keys: tuple[str, ...]
    behavior_contract_keys: tuple[str, ...]
    tradeoff_keys: tuple[str, ...]
    technology_identifiers: tuple[str, ...]
    verification_anchors: dict[str, tuple[str, ...]]

    @classmethod
    def from_value(cls, value: object) -> ReviewSemanticDraft:
        projection = _object(value, "review_semantic_projection")
        allowed = frozenset(
            {
                "concept_keys",
                "mechanism_keys",
                "behavior_contract_keys",
                "tradeoff_keys",
                "technology_identifiers",
                "verification_anchors",
            }
        )
        _reject_unknown_fields(projection, allowed, "review_semantic_projection")
        values: list[tuple[str, ...]] = []
        semantic_fields = sorted(allowed - {"verification_anchors"})
        for field_name in semantic_fields:
            raw = _bounded_list(
                projection.get(field_name, []),
                f"review_semantic_projection.{field_name}",
                maximum=MAX_SEMANTIC_KEYS,
                allow_empty=True,
            )
            normalized = tuple(
                sorted({_text(item, field_name, maximum=200).strip().casefold() for item in raw})
            )
            if len(normalized) != len(raw):
                raise InvalidInputError(f"review semantic {field_name} contains duplicates")
            values.append(normalized)
        by_name = dict(zip(semantic_fields, values, strict=True))
        raw_anchors = projection.get("verification_anchors", {})
        anchors_object = _object(raw_anchors, "review_semantic_projection.verification_anchors")
        anchors: dict[str, tuple[str, ...]] = {}
        for raw_key, raw_refs in anchors_object.items():
            key = _text(raw_key, "semantic verification key", maximum=200).strip().casefold()
            refs = tuple(
                _text(item, "semantic verification evidence_ref", maximum=200)
                for item in _bounded_list(
                    raw_refs,
                    "semantic verification anchors",
                    maximum=MAX_RELATIONS_PER_CLAIM,
                )
            )
            if key in anchors or len(set(refs)) != len(refs):
                raise InvalidInputError("semantic verification anchors contain duplicates")
            anchors[key] = tuple(sorted(refs))
        semantic_keys = set().union(*by_name.values())
        if anchors and set(anchors) != semantic_keys:
            raise InvalidInputError(
                "semantic verification anchors must cover every semantic key exactly"
            )
        return cls(
            by_name["concept_keys"],
            by_name["mechanism_keys"],
            by_name["behavior_contract_keys"],
            by_name["tradeoff_keys"],
            by_name["technology_identifiers"],
            anchors,
        )

    def as_json(self) -> JsonObject:
        return {
            "concept_keys": list(self.concept_keys),
            "mechanism_keys": list(self.mechanism_keys),
            "behavior_contract_keys": list(self.behavior_contract_keys),
            "tradeoff_keys": list(self.tradeoff_keys),
            "technology_identifiers": list(self.technology_identifiers),
        }


@dataclass(frozen=True)
class ClaimDraft:
    draft_id: str
    claim_key: str
    category: str
    scope_kind: str
    project_id: str
    worktree_id: str | None
    module_id: str | None
    section: str
    statement_tokens: tuple[InlineToken, ...]
    facets: tuple[str, ...]
    support_level: str
    personal_attribution: str
    review_semantic: ReviewSemanticDraft
    evidence_relations: tuple[EvidenceRelationDraft, ...]

    @classmethod
    def from_value(cls, value: object, index: int) -> ClaimDraft:
        draft = _object(value, f"claim_drafts[{index}]")
        _reject_unknown_fields(
            draft,
            frozenset(
                {
                    "draft_id",
                    "claim_key",
                    "category",
                    "scope_kind",
                    "project_id",
                    "worktree_id",
                    "module_id",
                    "section",
                    "statement_tokens",
                    "facets",
                    "support_level",
                    "personal_attribution",
                    "review_semantic_projection",
                    "evidence_relations",
                }
            ),
            f"claim_drafts[{index}]",
        )
        category = _text(draft.get("category"), "claim.category", maximum=80)
        if category not in CLAIM_CATEGORIES:
            raise InvalidInputError("Claim category is not supported")
        scope_kind = _text(draft.get("scope_kind"), "claim.scope_kind", maximum=40)
        if scope_kind not in {"project", "worktree", "module"}:
            raise InvalidInputError("Claim scope_kind is not supported")
        worktree_id = _optional_text(draft.get("worktree_id"), "worktree_id", maximum=200)
        module_id = _optional_text(draft.get("module_id"), "module_id", maximum=200)
        if scope_kind == "project" and (worktree_id is not None or module_id is not None):
            raise InvalidInputError("project Claim scope cannot name a worktree or module")
        if scope_kind == "worktree" and (worktree_id is None or module_id is not None):
            raise InvalidInputError("worktree Claim scope requires exactly one worktree")
        if scope_kind == "module" and module_id is None:
            raise InvalidInputError("module Claim scope requires a module")
        facets = _string_set(draft.get("facets", []), "claim.facets", allowed=FACETS)
        support_level = _text(draft.get("support_level"), "support_level", maximum=40)
        if support_level not in SUPPORT_LEVELS:
            raise InvalidInputError("Claim support_level is not supported")
        attribution = _text(
            draft.get("personal_attribution", "none"),
            "personal_attribution",
            maximum=40,
        )
        if attribution not in PERSONAL_ATTRIBUTIONS:
            raise InvalidInputError("Claim personal_attribution is not supported")
        relations = tuple(
            EvidenceRelationDraft.from_value(item, relation_index)
            for relation_index, item in enumerate(
                _bounded_list(
                    draft.get("evidence_relations"),
                    "evidence_relations",
                    maximum=MAX_RELATIONS_PER_CLAIM,
                )
            )
        )
        evidence_refs = [relation.evidence_ref for relation in relations]
        if len(set(evidence_refs)) != len(evidence_refs):
            raise InvalidInputError("one Claim may relate each Evidence reference only once")
        return cls(
            _text(draft.get("draft_id"), "claim draft_id", maximum=200),
            _stable_key(draft.get("claim_key"), "claim_key"),
            category,
            scope_kind,
            _text(draft.get("project_id"), "project_id", maximum=200),
            worktree_id,
            module_id,
            _stable_key(draft.get("section"), "claim.section"),
            _tokens(draft.get("statement_tokens"), "claim.statement_tokens"),
            facets,
            support_level,
            attribution,
            ReviewSemanticDraft.from_value(draft.get("review_semantic_projection")),
            relations,
        )


@dataclass(frozen=True)
class KnowledgeGapDraft:
    draft_id: str
    scope_kind: str
    project_id: str | None
    module_id: str | None
    dimension: str
    stable_gap_concept_key: str
    gap_contract_version: str
    description_tokens: tuple[InlineToken, ...]
    severity: str
    resolution_kind: str
    status: str

    @classmethod
    def from_value(cls, value: object, index: int) -> KnowledgeGapDraft:
        draft = _object(value, f"knowledge_gaps[{index}]")
        _reject_unknown_fields(
            draft,
            frozenset(
                {
                    "draft_id",
                    "scope_kind",
                    "project_id",
                    "module_id",
                    "dimension",
                    "stable_gap_concept_key",
                    "gap_contract_version",
                    "description_tokens",
                    "severity",
                    "resolution_kind",
                    "status",
                }
            ),
            f"knowledge_gaps[{index}]",
        )
        scope_kind = _text(draft.get("scope_kind"), "gap.scope_kind", maximum=40)
        if scope_kind not in {"role_global", "project", "module"}:
            raise InvalidInputError("KnowledgeGap scope_kind is not supported")
        project_id = _optional_text(draft.get("project_id"), "project_id", maximum=200)
        module_id = _optional_text(draft.get("module_id"), "module_id", maximum=200)
        if scope_kind == "role_global" and (project_id is not None or module_id is not None):
            raise InvalidInputError("role_global KnowledgeGap cannot name a project or module")
        if scope_kind == "project" and (project_id is None or module_id is not None):
            raise InvalidInputError("project KnowledgeGap requires exactly one project")
        if scope_kind == "module" and (project_id is None or module_id is None):
            raise InvalidInputError("module KnowledgeGap requires a project and module")
        severity = _text(draft.get("severity"), "gap.severity", maximum=40)
        if severity not in {"low", "medium", "high", "critical"}:
            raise InvalidInputError("KnowledgeGap severity is not supported")
        status = _text(draft.get("status"), "gap.status", maximum=40)
        if status not in {"open", "resolved", "superseded"}:
            raise InvalidInputError("KnowledgeGap status is not supported")
        return cls(
            _text(draft.get("draft_id"), "gap draft_id", maximum=200),
            scope_kind,
            project_id,
            module_id,
            _stable_key(draft.get("dimension"), "gap.dimension"),
            _stable_key(
                draft.get("stable_gap_concept_key"),
                "stable_gap_concept_key",
            ),
            _text(
                draft.get("gap_contract_version"),
                "gap_contract_version",
                maximum=200,
            ),
            _tokens(draft.get("description_tokens"), "gap.description_tokens"),
            severity,
            _stable_key(draft.get("resolution_kind"), "resolution_kind"),
            status,
        )


@dataclass(frozen=True)
class ProjectAssessmentDraft:
    project_id: str
    dimension_scores_milli: dict[str, int]
    coverage_bps: int
    evidence_refs: tuple[str, ...]
    gap_refs: tuple[str, ...]
    rationale_tokens: tuple[InlineToken, ...]

    @classmethod
    def from_value(cls, value: object, index: int) -> ProjectAssessmentDraft:
        draft = _object(value, f"project_assessments[{index}]")
        _reject_unknown_fields(
            draft,
            frozenset(
                {
                    "project_id",
                    "dimension_scores_milli",
                    "coverage_bps",
                    "evidence_refs",
                    "gap_refs",
                    "rationale_tokens",
                }
            ),
            f"project_assessments[{index}]",
        )
        raw_scores = _object(draft.get("dimension_scores_milli"), "dimension_scores_milli")
        scores: dict[str, int] = {}
        for key, score in raw_scores.items():
            stable = _stable_key(key, "dimension score key")
            if not isinstance(score, int) or isinstance(score, bool) or not 0 <= score <= 1000:
                raise InvalidInputError("dimension scores must be integers from 0 to 1000")
            scores[stable] = score
        coverage = draft.get("coverage_bps")
        if (
            not isinstance(coverage, int)
            or isinstance(coverage, bool)
            or not 0 <= coverage <= 10000
        ):
            raise InvalidInputError("coverage_bps must be an integer from 0 to 10000")
        evidence_refs = tuple(
            _text(item, "assessment.evidence_refs", maximum=200)
            for item in _bounded_list(
                draft.get("evidence_refs", []),
                "assessment.evidence_refs",
                maximum=MAX_REFERENCE_LIST,
                allow_empty=True,
            )
        )
        gap_refs = tuple(
            _text(item, "assessment.gap_refs", maximum=200)
            for item in _bounded_list(
                draft.get("gap_refs", []),
                "assessment.gap_refs",
                maximum=MAX_REFERENCE_LIST,
                allow_empty=True,
            )
        )
        if len(set(evidence_refs)) != len(evidence_refs) or len(set(gap_refs)) != len(gap_refs):
            raise InvalidInputError("assessment references must not contain duplicates")
        return cls(
            _text(draft.get("project_id"), "project_id", maximum=200),
            scores,
            coverage,
            evidence_refs,
            gap_refs,
            _tokens(draft.get("rationale_tokens"), "assessment.rationale_tokens"),
        )


@dataclass(frozen=True)
class HistoryReadProof:
    candidate_id: str
    preparation_run_id: str
    scan_run_id: str
    role_lens_id: str
    project_id: str
    worktree_id: str
    relative_paths: tuple[str, ...]
    query_reason: str
    selected_path: str
    commit: str
    metadata_sha256: str
    diff_sha256: str
    blob_sha256: str | None

    @classmethod
    def from_value(cls, value: object, index: int) -> HistoryReadProof:
        proof = _object(value, f"_history_proofs[{index}]")
        allowed = frozenset(
            {
                "candidate_id",
                "preparation_run_id",
                "scan_run_id",
                "role_lens_id",
                "project_id",
                "worktree_id",
                "relative_paths",
                "query_reason",
                "selected_path",
                "commit",
                "metadata_sha256",
                "diff_sha256",
                "blob_sha256",
            }
        )
        _reject_unknown_fields(proof, allowed, f"_history_proofs[{index}]")
        relative_paths = tuple(
            _text(item, "history relative_paths", maximum=1000)
            for item in _bounded_list(
                proof.get("relative_paths"),
                "history relative_paths",
                maximum=32,
            )
        )
        if tuple(sorted(set(relative_paths))) != relative_paths:
            raise InvalidInputError("history proof paths must be sorted and unique")
        blob_value = proof.get("blob_sha256")
        return cls(
            _text(proof.get("candidate_id"), "candidate_id", maximum=200),
            _text(proof.get("preparation_run_id"), "preparation_run_id", maximum=200),
            _text(proof.get("scan_run_id"), "scan_run_id", maximum=200),
            _text(proof.get("role_lens_id"), "role_lens_id", maximum=200),
            _text(proof.get("project_id"), "project_id", maximum=200),
            _text(proof.get("worktree_id"), "worktree_id", maximum=200),
            relative_paths,
            _text(proof.get("query_reason"), "query_reason", maximum=500),
            _text(proof.get("selected_path"), "selected_path", maximum=1000),
            _text(proof.get("commit"), "commit", maximum=100),
            _digest(proof.get("metadata_sha256"), "metadata_sha256"),
            _digest(proof.get("diff_sha256"), "diff_sha256"),
            _digest(blob_value, "blob_sha256") if blob_value is not None else None,
        )


@dataclass(frozen=True)
class AnalysisCommitRequest:
    request_id: str
    preparation_run_id: str
    role_lens_id: str
    evidence_drafts: tuple[EvidenceDraft, ...]
    claim_drafts: tuple[ClaimDraft, ...]
    project_assessments: tuple[ProjectAssessmentDraft, ...]
    knowledge_gaps: tuple[KnowledgeGapDraft, ...]
    history_proofs: tuple[HistoryReadProof, ...]
    request_sha256: str

    @classmethod
    def from_value(cls, value: object) -> AnalysisCommitRequest:
        request = _object(value, "analysis_commit_request")
        _reject_unknown_fields(
            request,
            frozenset(
                {
                    "contract_version",
                    "request_id",
                    "preparation_run_id",
                    "role_lens_id",
                    "evidence_drafts",
                    "claim_drafts",
                    "project_assessments",
                    "knowledge_gaps",
                    "_history_proofs",
                }
            ),
            "analysis_commit_request",
        )
        _canonical_json(
            request,
            "analysis_commit_request",
            maximum_bytes=MAX_PRIVATE_PAYLOAD_BYTES,
        )
        version = _text(request.get("contract_version"), "contract_version", maximum=80)
        if version != ANALYSIS_COMMIT_VERSION:
            raise InvalidInputError("unsupported AnalysisCommitRequest version")
        evidence_drafts = tuple(
            EvidenceDraft.from_value(item, index)
            for index, item in enumerate(
                _bounded_list(
                    request.get("evidence_drafts", []),
                    "evidence_drafts",
                    maximum=MAX_EVIDENCE_DRAFTS,
                    allow_empty=True,
                )
            )
        )
        claim_drafts = tuple(
            ClaimDraft.from_value(item, index)
            for index, item in enumerate(
                _bounded_list(request.get("claim_drafts"), "claim_drafts", maximum=MAX_CLAIMS)
            )
        )
        assessments = tuple(
            ProjectAssessmentDraft.from_value(item, index)
            for index, item in enumerate(
                _bounded_list(
                    request.get("project_assessments"),
                    "project_assessments",
                    maximum=MAX_ASSESSMENTS,
                )
            )
        )
        gaps = tuple(
            KnowledgeGapDraft.from_value(item, index)
            for index, item in enumerate(
                _bounded_list(
                    request.get("knowledge_gaps", []),
                    "knowledge_gaps",
                    maximum=MAX_GAPS,
                    allow_empty=True,
                )
            )
        )
        proofs = tuple(
            HistoryReadProof.from_value(item, index)
            for index, item in enumerate(
                _bounded_list(
                    request.get("_history_proofs", []),
                    "_history_proofs",
                    maximum=MAX_EVIDENCE_DRAFTS,
                    allow_empty=True,
                )
            )
        )
        for items, label in (
            (evidence_drafts, "EvidenceDraft"),
            (claim_drafts, "ClaimDraft"),
            (gaps, "KnowledgeGap draft"),
        ):
            identifiers = [item.draft_id for item in items]
            if len(set(identifiers)) != len(identifiers):
                raise InvalidInputError(f"{label} IDs must be unique")
        assessment_projects = [item.project_id for item in assessments]
        if len(set(assessment_projects)) != len(assessment_projects):
            raise InvalidInputError("ProjectAssessment drafts must have unique projects")
        external_value = dict(request)
        external_value.pop("_history_proofs", None)
        return cls(
            _text(request.get("request_id"), "request_id", maximum=200),
            _text(
                request.get("preparation_run_id"),
                "preparation_run_id",
                maximum=200,
            ),
            _text(request.get("role_lens_id"), "role_lens_id", maximum=200),
            evidence_drafts,
            claim_drafts,
            assessments,
            gaps,
            proofs,
            _sha256_value(external_value, "analysis_commit_request"),
        )


@dataclass(frozen=True)
class _EligibleProject:
    project_id: str
    project_snapshot_id: str
    snapshot_disposition: str


@dataclass(frozen=True)
class _EvidenceState:
    reference: str
    evidence_id: str
    project_id: str
    worktree_id: str | None
    module_id: str | None
    source_revision_id: str | None
    origin_kind: str
    evidence_kind: str
    artifact_kind: str | None
    locator: JsonObject
    summary: str
    commit_state: str
    validity: str
    content_equivalence_key: str | None
    context_fact_id: str | None
    context_fact_kind: str | None
    anchor_sha256: str


@dataclass(frozen=True)
class _PreparedEvidence:
    state: _EvidenceState
    project_snapshot_id: str | None
    preparation_run_id: str
    query_reason: str | None


@dataclass(frozen=True)
class _PreparedGap:
    draft: KnowledgeGapDraft
    gap_id: str
    gap_key: str
    scope_id: str


@dataclass(frozen=True)
class _ResolvedGap:
    prepared: _PreparedGap
    description: str
    description_tokens_json: str


@dataclass(frozen=True)
class _PreparedClaim:
    draft: ClaimDraft
    claim_id: str
    identity_sha256: str
    claim_revision_id: str
    revision_no: int
    revision_sha256: str
    supersedes_id: str | None
    statement: str
    statement_tokens_json: str
    review_projection_json: str
    review_semantic_sha256: str
    relations: tuple[tuple[str, str, tuple[str, ...]], ...]
    claim_is_new: bool
    revision_is_new: bool


@dataclass(frozen=True)
class _PreparedAssessment:
    draft: ProjectAssessmentDraft
    project: _EligibleProject
    rationale: str
    rationale_tokens_json: str
    evidence_ids: tuple[str, ...]
    gap_ids: tuple[str, ...]
    base_score_milli: int
    final_score_milli: int
    rank: int


class AnalysisService:
    """Validate one complete analysis batch and freeze it in a single transaction."""

    def __init__(self, database: Database) -> None:
        self._database = database

    def record_analysis(
        self,
        *,
        workspace_path: Path,
        authorization_receipt_id: str,
        request_value: object,
    ) -> dict[str, object]:
        request = AnalysisCommitRequest.from_value(request_value)
        canonical_workspace = workspace_path.expanduser().resolve(strict=False)
        self._database.migrate()
        existing_id = self._existing_commit(
            request,
            authorization_receipt_id=authorization_receipt_id,
        )
        if existing_id is not None:
            return self._result(existing_id)
        self._preflight_run(
            request,
            canonical_workspace=canonical_workspace,
            authorization_receipt_id=authorization_receipt_id,
        )
        verified_history = self._verify_history_proofs(request, canonical_workspace)
        refresh_result: tuple[tuple[SourceCheckResult, ...], Path] | None = None
        analysis_commit_id: str | None = None
        with self._database.write_transaction() as connection:
            existing_id = self._existing_commit_in_connection(
                connection,
                request,
                authorization_receipt_id,
            )
            if existing_id is not None:
                analysis_commit_id = existing_id
            else:
                run_status, stored_workspace = PreparationService._require_active_run_and_session(
                    connection,
                    request.preparation_run_id,
                    authorization_receipt_id,
                )
                if stored_workspace.resolve(strict=False) != canonical_workspace:
                    raise InvalidInputError("PreparationRun belongs to another workspace")
                if run_status != "analyzing":
                    raise InvalidInputError("record_analysis requires an analyzing PreparationRun")
                run_row = connection.execute(
                    """
                    SELECT scan_run_id, role_lens_id
                    FROM preparation_runs WHERE preparation_run_id = ?
                    """,
                    (request.preparation_run_id,),
                ).fetchone()
                assert run_row is not None
                if str(run_row["role_lens_id"]) != request.role_lens_id:
                    raise InvalidInputError("role_lens_id does not match the frozen PreparationRun")
                eligible = self._eligible_projects(connection, request.preparation_run_id)
                if not eligible:
                    raise InvalidInputError(
                        "record_analysis requires at least one eligible project"
                    )
                if {draft.project_id for draft in request.project_assessments} != set(eligible):
                    raise InvalidInputError(
                        "ProjectAssessment drafts must cover every eligible project exactly once"
                    )
                dimensions = self._role_dimensions(connection, request.role_lens_id)
                dimension_keys = {dimension.key for dimension in dimensions}
                prepared_gaps = self._prepare_gaps(
                    connection,
                    request,
                    eligible,
                    dimension_keys,
                )
                incomplete_context_projects = self._incomplete_context_projects(
                    connection,
                    request.preparation_run_id,
                )
                for project_id in incomplete_context_projects:
                    if not any(
                        gap.draft.project_id == project_id and gap.draft.status == "open"
                        for gap in prepared_gaps
                    ):
                        raise InvalidInputError(
                            "partial or skipped context requires a visible open project gap"
                        )
                gap_by_ref = {gap.draft.draft_id: gap for gap in prepared_gaps}

                used_evidence_refs = {
                    relation.evidence_ref
                    for claim in request.claim_drafts
                    for relation in claim.evidence_relations
                }
                used_evidence_refs.update(
                    evidence_ref
                    for assessment in request.project_assessments
                    for evidence_ref in assessment.evidence_refs
                )
                draft_by_ref = {draft.draft_id: draft for draft in request.evidence_drafts}
                unused_drafts = set(draft_by_ref) - used_evidence_refs
                if unused_drafts:
                    raise InvalidInputError(
                        "every EvidenceDraft must be used by a Claim or Assessment"
                    )
                existing_refs = used_evidence_refs - set(draft_by_ref)
                evidence_by_ref = self._load_existing_evidence(
                    connection,
                    request.preparation_run_id,
                    existing_refs,
                )
                prepared_evidence: list[_PreparedEvidence] = []
                for draft in request.evidence_drafts:
                    if draft.origin_kind == "source_revision":
                        prepared = self._prepare_source_evidence(
                            connection,
                            request.preparation_run_id,
                            canonical_workspace,
                            eligible,
                            draft,
                        )
                    else:
                        prepared = self._prepare_git_evidence(
                            connection,
                            request,
                            eligible,
                            draft,
                            verified_history,
                        )
                    prepared_evidence.append(prepared)
                    evidence_by_ref[draft.draft_id] = prepared.state
                missing_refs = used_evidence_refs - set(evidence_by_ref)
                if missing_refs:
                    raise InvalidInputError("analysis references Evidence outside the frozen run")

                project_worktrees = self._project_worktrees(connection, eligible)
                claim_identities = self._claim_identities(connection, request.claim_drafts)
                claim_id_by_ref = {
                    draft.draft_id: claim_identities[draft.draft_id][0]
                    for draft in request.claim_drafts
                }
                resolved_gaps = self._resolve_gaps(
                    prepared_gaps,
                    evidence_by_ref,
                    claim_id_by_ref,
                    request.claim_drafts,
                )
                prepared_claims = self._prepare_claims(
                    connection,
                    request.claim_drafts,
                    eligible,
                    evidence_by_ref,
                    prepared_gaps,
                    claim_identities,
                    claim_id_by_ref,
                    project_worktrees,
                )
                prepared_assessments = self._prepare_assessments(
                    request.project_assessments,
                    dimensions,
                    eligible,
                    evidence_by_ref,
                    gap_by_ref,
                    claim_id_by_ref,
                    request.claim_drafts,
                )

                source_revision_ids = tuple(
                    sorted(
                        {
                            state.source_revision_id
                            for reference in used_evidence_refs
                            if (state := evidence_by_ref[reference]).source_revision_id is not None
                        }
                    )
                )
                if len(source_revision_ids) > MAX_SOURCE_REVISION_BATCH:
                    raise InvalidInputError("analysis uses too many SourceRevisions in one commit")
                targets = (
                    PreparationService._source_targets_for_run(
                        connection,
                        request.preparation_run_id,
                        source_revision_ids=source_revision_ids,
                    )
                    if source_revision_ids
                    else ()
                )
                if {target.source_revision_id for target in targets} != set(source_revision_ids):
                    raise InvalidInputError("analysis uses a SourceRevision outside the frozen run")
                PreparationService._require_targets_within_workspace(canonical_workspace, targets)
                checks = tuple(_check_source(target) for target in targets)
                PreparationService._insert_source_checks(
                    connection,
                    request.preparation_run_id,
                    "commit",
                    checks,
                )
                if any(check.status == "mismatch" for check in checks):
                    timestamp = _now()
                    connection.execute(
                        """
                        UPDATE preparation_runs
                        SET status = 'refresh_required',
                            status_reason = 'source_revision_mismatch',
                            last_transition_at = ?, finished_at = ?
                        WHERE preparation_run_id = ?
                        """,
                        (timestamp, timestamp, request.preparation_run_id),
                    )
                    refresh_result = checks, canonical_workspace
                else:
                    analysis_commit_id = self._persist_analysis(
                        connection,
                        request,
                        prepared_evidence,
                        resolved_gaps,
                        prepared_claims,
                        prepared_assessments,
                    )
        if refresh_result is not None:
            checks, workspace_root = refresh_result
            return {
                "status": "ok",
                "preparation_run_id": request.preparation_run_id,
                "run_status": "refresh_required",
                "analysis_commit": None,
                "source_checks": [check.as_json(workspace_root) for check in checks],
            }
        assert analysis_commit_id is not None
        return self._result(analysis_commit_id)

    def _existing_commit(
        self,
        request: AnalysisCommitRequest,
        *,
        authorization_receipt_id: str,
    ) -> str | None:
        with self._database.read_connection() as connection:
            return self._existing_commit_in_connection(
                connection,
                request,
                authorization_receipt_id,
            )

    def _preflight_run(
        self,
        request: AnalysisCommitRequest,
        *,
        canonical_workspace: Path,
        authorization_receipt_id: str,
    ) -> None:
        with self._database.read_connection() as connection:
            run_status, stored_workspace = PreparationService._require_active_run_and_session(
                connection,
                request.preparation_run_id,
                authorization_receipt_id,
            )
            if stored_workspace.resolve(strict=False) != canonical_workspace:
                raise InvalidInputError("PreparationRun belongs to another workspace")
            row = connection.execute(
                "SELECT role_lens_id FROM preparation_runs WHERE preparation_run_id = ?",
                (request.preparation_run_id,),
            ).fetchone()
            assert row is not None
            if str(row["role_lens_id"]) != request.role_lens_id:
                raise InvalidInputError("role_lens_id does not match the frozen PreparationRun")
            if run_status != "analyzing":
                raise InvalidInputError("record_analysis requires an analyzing PreparationRun")

    @staticmethod
    def _existing_commit_in_connection(
        connection: sqlite3.Connection,
        request: AnalysisCommitRequest,
        authorization_receipt_id: str,
    ) -> str | None:
        by_request = connection.execute(
            """
            SELECT analysis_commit_id, request_sha256, preparation_run_id, role_lens_id
            FROM analysis_commits WHERE request_id = ?
            """,
            (request.request_id,),
        ).fetchone()
        if by_request is not None:
            if (
                not hmac.compare_digest(str(by_request["request_sha256"]), request.request_sha256)
                or str(by_request["preparation_run_id"]) != request.preparation_run_id
                or str(by_request["role_lens_id"]) != request.role_lens_id
            ):
                raise InvalidInputError("request_id is already bound to another analysis payload")
            PreparationService._require_active_run_and_session(
                connection,
                request.preparation_run_id,
                authorization_receipt_id,
            )
            return str(by_request["analysis_commit_id"])
        frozen = connection.execute(
            """
            SELECT analysis_commit_id FROM analysis_commits
            WHERE preparation_run_id = ?
            """,
            (request.preparation_run_id,),
        ).fetchone()
        if frozen is not None:
            raise InvalidInputError("PreparationRun already has an immutable analysis commit")
        return None

    def _verify_history_proofs(
        self,
        request: AnalysisCommitRequest,
        workspace_path: Path,
    ) -> dict[tuple[str, str], JsonObject]:
        git_drafts = [
            draft for draft in request.evidence_drafts if draft.origin_kind == "git_commit"
        ]
        proofs = {
            (proof.candidate_id, proof.selected_path): proof for proof in request.history_proofs
        }
        if len(proofs) != len(request.history_proofs):
            raise InvalidInputError("history candidate proofs must be unique")
        expected_keys = {
            (cast(str, draft.candidate_id), cast(str, draft.selected_path)) for draft in git_drafts
        }
        if set(proofs) != expected_keys:
            raise InvalidInputError(
                "every Git EvidenceDraft requires one same-task candidate-read proof"
            )
        verified: dict[tuple[str, str], JsonObject] = {}
        service = HistoryQueryService(self._database)
        for key, proof in proofs.items():
            if (
                proof.preparation_run_id != request.preparation_run_id
                or proof.role_lens_id != request.role_lens_id
            ):
                raise InvalidInputError("history proof does not match the analysis run")
            result = service.read_candidate(
                workspace_path=workspace_path,
                preparation_run_id=proof.preparation_run_id,
                scan_run_id=proof.scan_run_id,
                role_lens_id=proof.role_lens_id,
                project_id=proof.project_id,
                worktree_id=proof.worktree_id,
                relative_paths=proof.relative_paths,
                query_reason=proof.query_reason,
                candidate_id=proof.candidate_id,
                selected_path=proof.selected_path,
            )
            raw_read = result.get("history_candidate_read")
            if not isinstance(raw_read, dict):
                raise InvalidInputError("history candidate revalidation returned an invalid proof")
            read = cast(JsonObject, raw_read)
            raw_candidate = read.get("candidate")
            if not isinstance(raw_candidate, dict):
                raise InvalidInputError("history candidate revalidation is incomplete")
            candidate = raw_candidate
            expected = (
                candidate.get("candidate_id") == proof.candidate_id
                and candidate.get("commit") == proof.commit
                and candidate.get("metadata_sha256") == proof.metadata_sha256
                and read.get("selected_path") == proof.selected_path
                and read.get("query_reason") == proof.query_reason
                and read.get("diff_sha256") == proof.diff_sha256
                and read.get("blob_sha256") == proof.blob_sha256
            )
            if not expected:
                raise InvalidInputError("history candidate proof changed before analysis commit")
            verified[key] = read
        return verified

    @staticmethod
    def _eligible_projects(
        connection: sqlite3.Connection,
        preparation_run_id: str,
    ) -> dict[str, _EligibleProject]:
        rows = connection.execute(
            """
            SELECT project_id, project_snapshot_id, snapshot_disposition
            FROM preparation_run_projects
            WHERE preparation_run_id = ?
              AND snapshot_disposition IN ('fresh', 'carried_forward')
            ORDER BY project_id
            """,
            (preparation_run_id,),
        ).fetchall()
        return {
            str(row["project_id"]): _EligibleProject(
                str(row["project_id"]),
                str(row["project_snapshot_id"]),
                str(row["snapshot_disposition"]),
            )
            for row in rows
        }

    @staticmethod
    def _role_dimensions(
        connection: sqlite3.Connection,
        role_lens_id: str,
    ) -> tuple[RoleDimension, ...]:
        row = connection.execute(
            "SELECT dimensions FROM role_lenses WHERE role_lens_id = ?",
            (role_lens_id,),
        ).fetchone()
        if row is None:
            raise InvalidInputError("RoleLens does not exist")
        return _dimensions(_stored_json(row["dimensions"], "RoleLens dimensions"))

    @staticmethod
    def _incomplete_context_projects(
        connection: sqlite3.Connection,
        preparation_run_id: str,
    ) -> set[str]:
        return {
            str(row["project_id"])
            for row in connection.execute(
                """
                SELECT ca.project_id
                FROM context_answer_batches AS cab
                JOIN context_answers AS ca
                  ON ca.context_answer_batch_id = cab.context_answer_batch_id
                WHERE cab.preparation_run_id = ?
                  AND ca.answer_status IN ('partial', 'skipped')
                """,
                (preparation_run_id,),
            ).fetchall()
        }

    @staticmethod
    def _prepare_gaps(
        connection: sqlite3.Connection,
        request: AnalysisCommitRequest,
        eligible: dict[str, _EligibleProject],
        dimension_keys: set[str],
    ) -> tuple[_PreparedGap, ...]:
        result: list[_PreparedGap] = []
        gap_keys: set[str] = set()
        for draft in request.knowledge_gaps:
            if draft.dimension not in dimension_keys:
                raise InvalidInputError("KnowledgeGap dimension is outside the frozen RoleLens")
            if draft.scope_kind == "role_global":
                scope_id = "role_global"
                project_component = ""
            elif draft.scope_kind == "project":
                assert draft.project_id is not None
                if draft.project_id not in eligible:
                    raise InvalidInputError("KnowledgeGap project is outside the eligible set")
                scope_id = draft.project_id
                project_component = draft.project_id
            else:
                assert draft.project_id is not None and draft.module_id is not None
                project = eligible.get(draft.project_id)
                if project is None or not AnalysisService._module_in_snapshot(
                    connection,
                    draft.module_id,
                    draft.project_id,
                    project.project_snapshot_id,
                ):
                    raise InvalidInputError("KnowledgeGap module is outside the frozen project")
                scope_id = draft.module_id
                project_component = draft.project_id
            gap_key = _sha256_text(
                "\0".join(
                    (
                        project_component,
                        draft.scope_kind,
                        scope_id,
                        draft.dimension,
                        draft.stable_gap_concept_key,
                        draft.gap_contract_version,
                    )
                )
            )
            if gap_key in gap_keys:
                raise InvalidInputError("KnowledgeGap semantic keys must be unique in one run")
            gap_keys.add(gap_key)
            result.append(_PreparedGap(draft, _new_id(), gap_key, scope_id))
        return tuple(result)

    @staticmethod
    def _module_in_snapshot(
        connection: sqlite3.Connection,
        module_id: str,
        project_id: str,
        project_snapshot_id: str,
    ) -> bool:
        return (
            connection.execute(
                """
                SELECT 1
                FROM modules AS m
                JOIN module_observations AS mo ON mo.module_id = m.module_id
                WHERE m.module_id = ? AND m.project_id = ?
                  AND mo.project_snapshot_id = ?
                """,
                (module_id, project_id, project_snapshot_id),
            ).fetchone()
            is not None
        )

    @staticmethod
    def _load_existing_evidence(
        connection: sqlite3.Connection,
        preparation_run_id: str,
        evidence_refs: set[str],
    ) -> dict[str, _EvidenceState]:
        result: dict[str, _EvidenceState] = {}
        for evidence_id in sorted(evidence_refs):
            row = connection.execute(
                """
                SELECT e.evidence_id, e.project_id, e.acquisition_scope,
                       e.preparation_run_id, e.module_id, e.source_revision_id,
                       e.content_equivalence_key, e.origin_kind, e.evidence_kind,
                       e.locator, e.summary, e.commit_state, sa.worktree_id,
                       sa.artifact_kind, sr.content_sha256,
                       COALESCE(ev.validity, 'current') AS validity,
                       ec.context_fact_id, pcf.fact_kind, pcf.statement AS context_statement,
                       CASE
                         WHEN e.acquisition_scope = 'scan' AND EXISTS (
                           SELECT 1
                           FROM project_snapshot_evidence AS pse
                           JOIN preparation_run_projects AS prp
                             ON prp.project_snapshot_id = pse.project_snapshot_id
                           WHERE pse.evidence_id = e.evidence_id
                             AND prp.preparation_run_id = ?
                             AND prp.snapshot_disposition IN ('fresh', 'carried_forward')
                         ) THEN 1
                         WHEN e.acquisition_scope = 'context' AND EXISTS (
                           SELECT 1
                           FROM preparation_context_facts AS pcf_bound
                           WHERE pcf_bound.preparation_run_id = ?
                             AND pcf_bound.context_fact_id = ec.context_fact_id
                             AND pcf_bound.bound_status = 'current'
                         ) THEN 1
                         WHEN e.acquisition_scope = 'preparation'
                              AND e.preparation_run_id = ? THEN 1
                         ELSE 0
                       END AS accessible
                FROM evidence AS e
                JOIN preparation_runs AS pr ON pr.preparation_run_id = ?
                LEFT JOIN source_revisions AS sr
                  ON sr.source_revision_id = e.source_revision_id
                LEFT JOIN source_artifacts AS sa ON sa.artifact_id = sr.artifact_id
                LEFT JOIN evidence_validities AS ev
                  ON ev.scan_run_id = pr.scan_run_id AND ev.evidence_id = e.evidence_id
                LEFT JOIN evidence_contexts AS ec ON ec.evidence_id = e.evidence_id
                LEFT JOIN project_context_facts AS pcf
                  ON pcf.context_fact_id = ec.context_fact_id
                WHERE e.evidence_id = ?
                """,
                (
                    preparation_run_id,
                    preparation_run_id,
                    preparation_run_id,
                    preparation_run_id,
                    evidence_id,
                ),
            ).fetchone()
            if row is None or int(row["accessible"]) != 1:
                raise InvalidInputError("analysis references Evidence outside the frozen run")
            try:
                raw_locator = json.loads(str(row["locator"]))
            except json.JSONDecodeError as exc:
                raise InvalidInputError("stored Evidence locator is invalid") from exc
            locator = _object(raw_locator, "stored Evidence locator")
            anchor_value: JsonObject = {
                "origin_kind": str(row["origin_kind"]),
                "evidence_kind": str(row["evidence_kind"]),
                "content_equivalence_key": row["content_equivalence_key"],
                "source_content_sha256": row["content_sha256"],
                "context_fact_id": row["context_fact_id"],
                "locator": AnalysisService._semantic_locator(locator),
            }
            result[evidence_id] = _EvidenceState(
                reference=evidence_id,
                evidence_id=evidence_id,
                project_id=str(row["project_id"]),
                worktree_id=(str(row["worktree_id"]) if row["worktree_id"] is not None else None),
                module_id=str(row["module_id"]) if row["module_id"] is not None else None,
                source_revision_id=(
                    str(row["source_revision_id"])
                    if row["source_revision_id"] is not None
                    else None
                ),
                origin_kind=str(row["origin_kind"]),
                evidence_kind=str(row["evidence_kind"]),
                artifact_kind=(
                    str(row["artifact_kind"]) if row["artifact_kind"] is not None else None
                ),
                locator=locator,
                summary=(
                    str(row["context_statement"])
                    if row["context_statement"] is not None
                    else str(row["summary"])
                ),
                commit_state=str(row["commit_state"]),
                validity=str(row["validity"]),
                content_equivalence_key=(
                    str(row["content_equivalence_key"])
                    if row["content_equivalence_key"] is not None
                    else None
                ),
                context_fact_id=(
                    str(row["context_fact_id"]) if row["context_fact_id"] is not None else None
                ),
                context_fact_kind=(str(row["fact_kind"]) if row["fact_kind"] is not None else None),
                anchor_sha256=_sha256_value(anchor_value, "Evidence anchor"),
            )
        return result

    @staticmethod
    def _semantic_locator(locator: JsonObject) -> JsonObject:
        ignored = {
            "start_line",
            "end_line",
            "line",
            "relative_path",
            "workspace_relative_path",
            "worktree_id",
            "worktree_relative_root",
            "preparation_run_id",
        }
        return {key: value for key, value in locator.items() if key not in ignored}

    @staticmethod
    def _equivalence_locator(locator: JsonObject) -> JsonObject:
        """Match scan-time equivalence while removing runtime-only path bindings."""
        ignored = {
            "relative_path",
            "workspace_relative_path",
            "worktree_id",
            "worktree_relative_root",
            "preparation_run_id",
        }
        return {key: value for key, value in locator.items() if key not in ignored}

    @staticmethod
    def _prepare_source_evidence(
        connection: sqlite3.Connection,
        preparation_run_id: str,
        workspace_root: Path,
        eligible: dict[str, _EligibleProject],
        draft: EvidenceDraft,
    ) -> _PreparedEvidence:
        assert draft.source_revision_id is not None and draft.observed_sha256 is not None
        project = eligible.get(draft.project_id)
        if project is None:
            raise InvalidInputError("source EvidenceDraft project is outside the eligible set")
        row = connection.execute(
            """
            SELECT sr.content_sha256, sr.analysis_fingerprint, sa.project_id,
                   sa.worktree_id, sa.relative_path, sa.artifact_kind,
                   wt.canonical_root AS worktree_root
            FROM project_snapshot_source_revisions AS pssr
            JOIN source_revisions AS sr
              ON sr.source_revision_id = pssr.source_revision_id
            JOIN source_artifacts AS sa ON sa.artifact_id = sr.artifact_id
            JOIN worktrees AS wt ON wt.worktree_id = sa.worktree_id
            WHERE pssr.project_snapshot_id = ? AND sr.source_revision_id = ?
            """,
            (project.project_snapshot_id, draft.source_revision_id),
        ).fetchone()
        if row is None or str(row["project_id"]) != draft.project_id:
            raise InvalidInputError("SourceRevision is outside the frozen project snapshot")
        if draft.worktree_id is None or draft.worktree_id != str(row["worktree_id"]):
            raise InvalidInputError("source EvidenceDraft must name the exact frozen worktree")
        if not hmac.compare_digest(str(row["content_sha256"]), draft.observed_sha256):
            raise InvalidInputError("source EvidenceDraft observed hash is not the frozen hash")
        before_read = connection.execute(
            """
            SELECT 1 FROM preparation_source_checks
            WHERE preparation_run_id = ? AND source_revision_id = ?
              AND phase = 'before_read' AND status = 'passed'
            LIMIT 1
            """,
            (preparation_run_id, draft.source_revision_id),
        ).fetchone()
        if before_read is None:
            raise InvalidInputError(
                "source EvidenceDraft requires a passed before_read check in this task"
            )
        observed_rows = connection.execute(
            """
            SELECT DISTINCT e.evidence_kind, e.commit_state, e.module_id
            FROM project_snapshot_evidence AS pse
            JOIN evidence AS e ON e.evidence_id = pse.evidence_id
            WHERE pse.project_snapshot_id = ? AND e.source_revision_id = ?
            """,
            (project.project_snapshot_id, draft.source_revision_id),
        ).fetchall()
        observed_kinds = {str(observed["evidence_kind"]) for observed in observed_rows}
        if draft.evidence_kind not in observed_kinds:
            raise InvalidInputError(
                "source EvidenceDraft cannot relabel the scanner's observed evidence kind"
            )
        observed_states = {str(observed["commit_state"]) for observed in observed_rows}
        if draft.commit_state not in observed_states:
            raise InvalidInputError("source EvidenceDraft commit_state is not frozen scan state")
        if draft.module_id is not None and not any(
            observed["module_id"] is not None
            and str(observed["module_id"]) == draft.module_id
            and str(observed["evidence_kind"]) == draft.evidence_kind
            and str(observed["commit_state"]) == draft.commit_state
            for observed in observed_rows
        ):
            raise InvalidInputError(
                "source EvidenceDraft module does not match the scanner-observed file module"
            )
        allowed_locator_fields = frozenset(
            {
                "worktree_id",
                "relative_path",
                "workspace_relative_path",
                "start_line",
                "end_line",
                "symbol",
                "key",
                "status",
                "related_source_revision_ids",
                "related_commits",
            }
        )
        _reject_unknown_fields(
            draft.locator, allowed_locator_fields, "source EvidenceDraft locator"
        )
        if draft.locator.get("worktree_id") != draft.worktree_id:
            raise InvalidInputError("source EvidenceDraft locator worktree_id is not exact")
        relative_path = str(row["relative_path"])
        if draft.locator.get("relative_path") != relative_path:
            raise InvalidInputError("source EvidenceDraft locator relative_path is not exact")
        worktree_relative_root, workspace_relative_path = _workspace_source_locator(
            workspace_root,
            Path(str(row["worktree_root"])),
            relative_path,
        )
        supplied_workspace_path = draft.locator.get("workspace_relative_path")
        if (
            supplied_workspace_path is not None
            and supplied_workspace_path != workspace_relative_path
        ):
            raise InvalidInputError("source EvidenceDraft workspace path is not exact")
        normalized_locator: JsonObject = {
            **draft.locator,
            "worktree_id": draft.worktree_id,
            "worktree_relative_root": worktree_relative_root,
            "relative_path": relative_path,
            "workspace_relative_path": workspace_relative_path,
            "preparation_run_id": preparation_run_id,
        }
        AnalysisService._validate_precise_locator(normalized_locator)
        source_content = AnalysisService._read_matching_source(
            Path(str(row["worktree_root"])),
            relative_path,
            draft.observed_sha256,
        )
        AnalysisService._validate_locator_line_range(
            source_content,
            normalized_locator,
        )
        if source_content is not None:
            _reject_verbatim_source_summary(
                draft.summary,
                source_content.decode("utf-8", errors="replace"),
            )
        equivalence = _sha256_text(
            "\0".join(
                (
                    str(row["analysis_fingerprint"]),
                    draft.evidence_kind,
                    _canonical_json(
                        AnalysisService._equivalence_locator(normalized_locator),
                        "source Evidence equivalence locator",
                    ),
                )
            )
        )
        evidence_id = _new_id()
        anchor_value: JsonObject = {
            "origin_kind": "source_revision",
            "evidence_kind": draft.evidence_kind,
            "content_equivalence_key": equivalence,
            "source_content_sha256": draft.observed_sha256,
            "locator": AnalysisService._semantic_locator(normalized_locator),
        }
        state = _EvidenceState(
            draft.draft_id,
            evidence_id,
            draft.project_id,
            draft.worktree_id,
            draft.module_id,
            draft.source_revision_id,
            "source_revision",
            draft.evidence_kind,
            str(row["artifact_kind"]),
            normalized_locator,
            draft.summary,
            draft.commit_state,
            "current",
            equivalence,
            None,
            None,
            _sha256_value(anchor_value, "Evidence anchor"),
        )
        return _PreparedEvidence(
            state,
            project.project_snapshot_id,
            preparation_run_id,
            None,
        )

    @staticmethod
    def _validate_precise_locator(locator: JsonObject) -> None:
        start = locator.get("start_line")
        end = locator.get("end_line")
        if start is not None and (
            not isinstance(start, int) or isinstance(start, bool) or start < 1
        ):
            raise InvalidInputError("start_line must be a positive integer")
        if end is not None and (not isinstance(end, int) or isinstance(end, bool) or end < 1):
            raise InvalidInputError("end_line must be a positive integer")
        if start is None and end is not None:
            raise InvalidInputError("end_line requires start_line")
        if isinstance(start, int) and isinstance(end, int) and start > end:
            raise InvalidInputError("source EvidenceDraft line range is reversed")
        for key in ("symbol", "key", "status"):
            if key in locator:
                _text(locator[key], f"locator.{key}", maximum=500)
        related_revisions = locator.get("related_source_revision_ids")
        if related_revisions is not None:
            for item in _bounded_list(
                related_revisions,
                "locator.related_source_revision_ids",
                maximum=MAX_SOURCE_REVISION_BATCH,
            ):
                _text(item, "related_source_revision_id", maximum=200)
        related_commits = locator.get("related_commits")
        if related_commits is not None:
            for item in _bounded_list(
                related_commits,
                "locator.related_commits",
                maximum=100,
            ):
                _text(item, "related_commit", maximum=100)

    @staticmethod
    def _read_matching_source(
        worktree_root: Path,
        relative_path: str,
        expected_sha256: str,
    ) -> bytes | None:
        try:
            file_fd, _ = open_regular_file(worktree_root, relative_path)
            try:
                content = read_open_file(file_fd)
            finally:
                os.close(file_fd)
        except OSError:
            return None
        if not hmac.compare_digest(hashlib.sha256(content).hexdigest(), expected_sha256):
            return None
        return content

    @staticmethod
    def _validate_locator_line_range(content: bytes | None, locator: JsonObject) -> None:
        end_line = locator.get("end_line") or locator.get("start_line")
        if not isinstance(end_line, int) or content is None:
            return
        line_count = content.count(b"\n") + (0 if content.endswith(b"\n") else 1)
        if end_line > max(1, line_count):
            raise InvalidInputError("source EvidenceDraft line range exceeds the frozen file")

    @staticmethod
    def _prepare_git_evidence(
        connection: sqlite3.Connection,
        request: AnalysisCommitRequest,
        eligible: dict[str, _EligibleProject],
        draft: EvidenceDraft,
        verified_history: dict[tuple[str, str], JsonObject],
    ) -> _PreparedEvidence:
        assert draft.candidate_id is not None and draft.selected_path is not None
        assert draft.query_reason is not None and draft.commit is not None
        assert draft.metadata_sha256 is not None and draft.diff_sha256 is not None
        project = eligible.get(draft.project_id)
        if project is None:
            raise InvalidInputError("Git EvidenceDraft project is outside the eligible set")
        if draft.evidence_kind != "git_history":
            raise InvalidInputError("targeted Git EvidenceDraft evidence_kind must be git_history")
        read = verified_history.get((draft.candidate_id, draft.selected_path))
        if read is None:
            raise InvalidInputError("Git EvidenceDraft lacks a verified candidate read")
        raw_candidate = read.get("candidate")
        assert isinstance(raw_candidate, dict)
        candidate = raw_candidate
        proof = next(
            proof
            for proof in request.history_proofs
            if proof.candidate_id == draft.candidate_id
            and proof.selected_path == draft.selected_path
        )
        if (
            proof.project_id != draft.project_id
            or proof.worktree_id != draft.worktree_id
            or proof.query_reason != draft.query_reason
            or proof.commit != draft.commit
            or proof.metadata_sha256 != draft.metadata_sha256
            or proof.diff_sha256 != draft.diff_sha256
            or proof.blob_sha256 != draft.blob_sha256
        ):
            raise InvalidInputError("Git EvidenceDraft does not match its same-task proof")
        _reject_verbatim_source_summary(
            draft.summary,
            *(
                text
                for field_name in ("diff_text", "blob_text")
                if isinstance((text := read.get(field_name)), str)
            ),
        )
        expected_module_id = AnalysisService._module_for_snapshot_path(
            connection,
            project.project_snapshot_id,
            draft.selected_path,
        )
        if draft.module_id is not None and draft.module_id != expected_module_id:
            raise InvalidInputError(
                "Git EvidenceDraft module does not match the selected path's frozen module"
            )
        locator: JsonObject = {
            "history_basis": "targeted_before_initial_window",
            "candidate_id": draft.candidate_id,
            "commit": draft.commit,
            "committed_at": candidate.get("committed_at"),
            "author_name": candidate.get("author_name"),
            "author_email": candidate.get("author_email"),
            "subject": candidate.get("subject"),
            "changed_paths": candidate.get("changed_paths"),
            "selected_path": draft.selected_path,
            "worktree_id": draft.worktree_id,
            "metadata_sha256": draft.metadata_sha256,
            "diff_sha256": draft.diff_sha256,
            "blob_sha256": draft.blob_sha256,
            "preparation_run_id": request.preparation_run_id,
        }
        evidence_id = _new_id()
        equivalence = _sha256_value(
            {
                "candidate_id": draft.candidate_id,
                "selected_path": draft.selected_path,
                "diff_sha256": draft.diff_sha256,
                "blob_sha256": draft.blob_sha256,
            },
            "Git Evidence equivalence",
        )
        anchor_value: JsonObject = {
            "origin_kind": "git_commit",
            "evidence_kind": "git_history",
            "commit": draft.commit,
            "selected_path": draft.selected_path,
            "diff_sha256": draft.diff_sha256,
            "blob_sha256": draft.blob_sha256,
        }
        state = _EvidenceState(
            draft.draft_id,
            evidence_id,
            draft.project_id,
            draft.worktree_id,
            draft.module_id,
            None,
            "git_commit",
            "git_history",
            None,
            locator,
            draft.summary,
            "historical",
            "current",
            equivalence,
            None,
            None,
            _sha256_value(anchor_value, "Git Evidence anchor"),
        )
        return _PreparedEvidence(state, None, request.preparation_run_id, draft.query_reason)

    @staticmethod
    def _module_for_snapshot_path(
        connection: sqlite3.Connection,
        project_snapshot_id: str,
        relative_path: str,
    ) -> str | None:
        candidates: list[tuple[int, str]] = []
        for row in connection.execute(
            """
            SELECT mo.module_id, mo.relative_root
            FROM module_observations AS mo
            WHERE mo.project_snapshot_id = ?
            """,
            (project_snapshot_id,),
        ).fetchall():
            root = str(row["relative_root"]).strip("/") or "."
            if root == "." or relative_path == root or relative_path.startswith(f"{root}/"):
                candidates.append((0 if root == "." else len(root), str(row["module_id"])))
        if not candidates:
            return None
        return max(candidates)[1]

    @staticmethod
    def _project_worktrees(
        connection: sqlite3.Connection,
        eligible: dict[str, _EligibleProject],
    ) -> dict[str, set[str]]:
        result: dict[str, set[str]] = {}
        for project in eligible.values():
            rows = connection.execute(
                """
                SELECT DISTINCT wt.worktree_id
                FROM project_snapshots AS ps
                JOIN worktrees AS wt ON wt.project_id = ps.project_id
                JOIN worktree_observations AS wo
                  ON wo.worktree_id = wt.worktree_id
                 AND wo.scan_run_id = ps.scan_run_id
                WHERE ps.project_snapshot_id = ?
                """,
                (project.project_snapshot_id,),
            ).fetchall()
            result[project.project_id] = {str(row["worktree_id"]) for row in rows}
        return result

    @staticmethod
    def _claim_identities(
        connection: sqlite3.Connection,
        drafts: tuple[ClaimDraft, ...],
    ) -> dict[str, tuple[str, str, bool]]:
        result: dict[str, tuple[str, str, bool]] = {}
        identities: set[str] = set()
        for draft in drafts:
            identity_value: JsonObject = {
                "claim_key": draft.claim_key,
                "category": draft.category,
                "scope_kind": draft.scope_kind,
                "project_id": draft.project_id,
                "worktree_id": draft.worktree_id,
                "module_id": draft.module_id,
            }
            identity_sha256 = _sha256_value(identity_value, "Claim identity")
            if identity_sha256 in identities:
                raise InvalidInputError("one analysis batch may contain each logical Claim once")
            identities.add(identity_sha256)
            row = connection.execute(
                "SELECT claim_id FROM claims WHERE identity_sha256 = ?",
                (identity_sha256,),
            ).fetchone()
            claim_id = str(row["claim_id"]) if row is not None else _new_id()
            result[draft.draft_id] = (claim_id, identity_sha256, row is None)
        return result

    @staticmethod
    def _resolve_gaps(
        prepared_gaps: tuple[_PreparedGap, ...],
        evidence_by_ref: dict[str, _EvidenceState],
        claim_id_by_ref: dict[str, str],
        claim_drafts: tuple[ClaimDraft, ...],
    ) -> tuple[_ResolvedGap, ...]:
        gap_by_ref = {gap.draft.draft_id: gap for gap in prepared_gaps}
        claim_project_by_ref = {draft.draft_id: draft.project_id for draft in claim_drafts}
        result: list[_ResolvedGap] = []
        for gap in prepared_gaps:
            draft = gap.draft
            if draft.scope_kind == "role_global":
                allowed_evidence = set(evidence_by_ref)
                allowed_claims = set(claim_id_by_ref)
                allowed_gaps = set(gap_by_ref) - {draft.draft_id}
            else:
                assert draft.project_id is not None
                allowed_evidence = {
                    reference
                    for reference, state in evidence_by_ref.items()
                    if state.project_id == draft.project_id
                    and (
                        draft.scope_kind != "module"
                        or state.module_id is None
                        or state.module_id == draft.module_id
                    )
                }
                allowed_claims = {
                    reference
                    for reference, project_id in claim_project_by_ref.items()
                    if project_id == draft.project_id
                }
                allowed_gaps = {
                    reference
                    for reference, other in gap_by_ref.items()
                    if reference != draft.draft_id
                    and (
                        other.draft.scope_kind == "role_global"
                        or other.draft.project_id == draft.project_id
                    )
                }
            tokens, description = AnalysisService._resolve_tokens(
                draft.description_tokens,
                evidence_by_ref=evidence_by_ref,
                gap_by_ref=gap_by_ref,
                claim_id_by_ref=claim_id_by_ref,
                allowed_evidence_refs=allowed_evidence,
                allowed_gap_refs=allowed_gaps,
                allowed_claim_refs=allowed_claims,
            )
            if not description.strip():
                raise InvalidInputError("KnowledgeGap description must not be empty")
            result.append(
                _ResolvedGap(
                    gap,
                    description,
                    _canonical_json(
                        cast(list[JsonValue], tokens),
                        "KnowledgeGap description tokens",
                    ),
                )
            )
        return tuple(result)

    @staticmethod
    def _prepare_claims(
        connection: sqlite3.Connection,
        drafts: tuple[ClaimDraft, ...],
        eligible: dict[str, _EligibleProject],
        evidence_by_ref: dict[str, _EvidenceState],
        prepared_gaps: tuple[_PreparedGap, ...],
        claim_identities: dict[str, tuple[str, str, bool]],
        claim_id_by_ref: dict[str, str],
        project_worktrees: dict[str, set[str]],
    ) -> tuple[_PreparedClaim, ...]:
        gap_by_ref = {gap.draft.draft_id: gap for gap in prepared_gaps}
        claim_project_by_ref = {draft.draft_id: draft.project_id for draft in drafts}
        result: list[_PreparedClaim] = []
        for draft in drafts:
            project = eligible.get(draft.project_id)
            if project is None:
                raise InvalidInputError("Claim project is outside the eligible set")
            if draft.worktree_id is not None and draft.worktree_id not in project_worktrees.get(
                draft.project_id, set()
            ):
                raise InvalidInputError("Claim worktree is outside the frozen project snapshot")
            if draft.module_id is not None and not AnalysisService._module_in_snapshot(
                connection,
                draft.module_id,
                draft.project_id,
                project.project_snapshot_id,
            ):
                raise InvalidInputError("Claim module is outside the frozen project snapshot")
            relation_states = tuple(
                (relation, evidence_by_ref[relation.evidence_ref])
                for relation in draft.evidence_relations
            )
            projection = AnalysisService._validate_claim(
                draft,
                relation_states,
                project_worktrees.get(draft.project_id, set()),
            )
            allowed_gap_refs = {
                gap.draft.draft_id
                for gap in prepared_gaps
                if gap.draft.scope_kind == "role_global" or gap.draft.project_id == draft.project_id
            }
            statement_tokens, statement = AnalysisService._resolve_tokens(
                draft.statement_tokens,
                evidence_by_ref=evidence_by_ref,
                gap_by_ref=gap_by_ref,
                claim_id_by_ref=claim_id_by_ref,
                allowed_evidence_refs={
                    relation.evidence_ref for relation in draft.evidence_relations
                },
                allowed_gap_refs=allowed_gap_refs,
                allowed_claim_refs={
                    reference
                    for reference, project_id in claim_project_by_ref.items()
                    if project_id == draft.project_id and reference != draft.draft_id
                },
            )
            if draft.personal_attribution == "none" and (
                not draft.statement_tokens
                or draft.statement_tokens[0].kind != "text"
                or draft.statement_tokens[0].ref_id is not None
                or draft.statement_tokens[0].value != AnalysisService._non_personal_subject(draft)
            ):
                raise InvalidInputError(
                    "non-personal Claim statement must begin with its exact scope subject token"
                )
            attribution_projections = AnalysisService._personal_attribution_projections(
                draft.statement_tokens
            )
            detected = AnalysisService._detected_personal_attribution(*attribution_projections)
            if detected is not None and not AnalysisService._attribution_covers(
                draft.personal_attribution,
                detected,
            ):
                raise InvalidInputError(
                    "Claim statement contains stronger personal attribution than declared"
                )
            AnalysisService._validate_personal_attribution(
                draft,
                statement,
                relation_states,
            )
            evidence_atoms = [
                {
                    "anchor_sha256": state.anchor_sha256,
                    "validity": state.validity,
                    "relation": relation.relation,
                }
                for relation, state in sorted(
                    relation_states,
                    key=lambda item: (item[1].anchor_sha256, item[0].relation),
                )
            ]
            role_anchors = sorted(
                {
                    state.context_fact_id
                    for relation, state in relation_states
                    if relation.relation in {"supports", "contextualizes"}
                    if state.context_fact_id is not None
                    and state.context_fact_kind in {"role", "ownership"}
                }
            )
            outcome_anchors = sorted(
                {
                    state.context_fact_id
                    for relation, state in relation_states
                    if relation.relation in {"supports", "contextualizes"}
                    if state.context_fact_id is not None
                    and state.context_fact_kind in {"outcome", "metric"}
                }
            )
            equivalence_status, semantic_anchor_atoms = (
                AnalysisService._semantic_equivalence_status(
                    draft.review_semantic,
                    statement,
                    relation_states,
                )
            )
            review_projection: JsonObject = {
                **projection,
                **draft.review_semantic.as_json(),
                "role_anchor_ids": cast(list[JsonValue], role_anchors),
                "outcome_anchor_ids": cast(list[JsonValue], outcome_anchors),
                "evidence_atoms": cast(list[JsonValue], evidence_atoms),
                "semantic_anchor_atoms": semantic_anchor_atoms,
                "equivalence_status": equivalence_status,
            }
            if equivalence_status == "unverified":
                normalized_statement = re.sub(r"\s+", " ", statement).strip().casefold()
                review_projection["fallback_semantic_sha256"] = _sha256_text(
                    "\0".join(
                        (
                            normalized_statement,
                            *(atom["anchor_sha256"] for atom in evidence_atoms),
                            *role_anchors,
                            *outcome_anchors,
                        )
                    )
                )
            review_projection_json = _canonical_json(
                review_projection,
                "review semantic projection",
                maximum_bytes=MAX_PRIVATE_PAYLOAD_BYTES,
            )
            review_semantic_sha256 = _sha256_text(review_projection_json)
            relations = tuple(
                sorted(
                    (
                        state.evidence_id,
                        relation.relation,
                        relation.supported_facets,
                    )
                    for relation, state in relation_states
                )
            )
            revision_value: JsonObject = {
                "statement_tokens": cast(list[JsonValue], statement_tokens),
                "facets": list(draft.facets),
                "support_level": draft.support_level,
                "personal_attribution": draft.personal_attribution,
                "review_semantic_sha256": review_semantic_sha256,
                "relations": [
                    {
                        "evidence_id": evidence_id,
                        "relation": relation,
                        "supported_facets": list(supported_facets),
                    }
                    for evidence_id, relation, supported_facets in relations
                ],
            }
            revision_sha256 = _sha256_value(revision_value, "ClaimRevision")
            claim_id, identity_sha256, claim_is_new = claim_identities[draft.draft_id]
            existing_revision = connection.execute(
                """
                SELECT claim_revision_id, revision_no, supersedes_id
                FROM claim_revisions
                WHERE claim_id = ? AND revision_sha256 = ?
                """,
                (claim_id, revision_sha256),
            ).fetchone()
            if existing_revision is not None:
                claim_revision_id = str(existing_revision["claim_revision_id"])
                revision_no = int(existing_revision["revision_no"])
                supersedes_id = (
                    str(existing_revision["supersedes_id"])
                    if existing_revision["supersedes_id"] is not None
                    else None
                )
                revision_is_new = False
            else:
                previous = connection.execute(
                    """
                    SELECT claim_revision_id, revision_no
                    FROM claim_revisions
                    WHERE claim_id = ? ORDER BY revision_no DESC LIMIT 1
                    """,
                    (claim_id,),
                ).fetchone()
                claim_revision_id = _new_id()
                revision_no = int(previous["revision_no"]) + 1 if previous is not None else 1
                supersedes_id = str(previous["claim_revision_id"]) if previous is not None else None
                revision_is_new = True
            result.append(
                _PreparedClaim(
                    draft,
                    claim_id,
                    identity_sha256,
                    claim_revision_id,
                    revision_no,
                    revision_sha256,
                    supersedes_id,
                    statement,
                    _canonical_json(
                        cast(list[JsonValue], statement_tokens),
                        "Claim statement tokens",
                    ),
                    review_projection_json,
                    review_semantic_sha256,
                    relations,
                    claim_is_new,
                    revision_is_new,
                )
            )
        return tuple(result)

    @staticmethod
    def _semantic_equivalence_status(
        semantic: ReviewSemanticDraft,
        statement: str,
        relation_states: tuple[tuple[EvidenceRelationDraft, _EvidenceState], ...],
    ) -> tuple[str, JsonObject]:
        if not semantic.verification_anchors:
            return "unverified", {}
        state_by_ref = {relation.evidence_ref: state for relation, state in relation_states}
        atoms: JsonObject = {}
        normalized_statement = statement.casefold()
        for key, references in sorted(semantic.verification_anchors.items()):
            if any(reference not in state_by_ref for reference in references):
                return "unverified", {}
            states = [state_by_ref[reference] for reference in references]
            corpus = "\n".join(
                (
                    normalized_statement,
                    *(state.summary.casefold() for state in states),
                    *(
                        _canonical_json(
                            AnalysisService._semantic_locator(state.locator),
                            "semantic locator verification",
                        ).casefold()
                        for state in states
                    ),
                )
            )
            variants = {
                key,
                re.sub(r"[._-]+", " ", key),
                re.sub(r"[._-]+", "", key),
            }
            if not any(variant and variant in corpus for variant in variants):
                return "unverified", {}
            atoms[key] = cast(
                list[JsonValue],
                sorted({state.anchor_sha256 for state in states}),
            )
        return "verified", atoms

    @staticmethod
    def _resolve_tokens(
        tokens: tuple[InlineToken, ...],
        *,
        evidence_by_ref: dict[str, _EvidenceState],
        gap_by_ref: dict[str, _PreparedGap],
        claim_id_by_ref: dict[str, str],
        allowed_evidence_refs: set[str],
        allowed_gap_refs: set[str],
        allowed_claim_refs: set[str],
    ) -> tuple[list[JsonObject], str]:
        resolved: list[JsonObject] = []
        for token in tokens:
            resolved_ref: str | None = None
            if token.kind == "evidence_ref":
                assert token.ref_id is not None
                if token.ref_id not in allowed_evidence_refs or token.ref_id not in evidence_by_ref:
                    raise InvalidInputError(
                        "Evidence token points outside the containing analysis item"
                    )
                resolved_ref = evidence_by_ref[token.ref_id].evidence_id
            elif token.kind == "gap_ref":
                assert token.ref_id is not None
                if token.ref_id not in allowed_gap_refs or token.ref_id not in gap_by_ref:
                    raise InvalidInputError(
                        "KnowledgeGap token points outside the containing scope"
                    )
                resolved_ref = gap_by_ref[token.ref_id].gap_id
            elif token.kind == "claim_ref":
                assert token.ref_id is not None
                if token.ref_id not in allowed_claim_refs or token.ref_id not in claim_id_by_ref:
                    raise InvalidInputError("Claim token points outside the containing project")
                resolved_ref = claim_id_by_ref[token.ref_id]
            resolved.append(token.as_json(resolved_ref))
        return resolved, "".join(token.value for token in tokens)

    @staticmethod
    def _personal_attribution_projections(
        tokens: tuple[InlineToken, ...],
    ) -> tuple[str, str]:
        values = tuple(token.value for token in tokens if token.kind in {"text", "emphasis"})
        return "".join(values), "\n".join(values)

    @staticmethod
    def _non_personal_subject(draft: ClaimDraft) -> str:
        if draft.scope_kind == "project":
            return "该项目"
        if draft.scope_kind == "worktree":
            return "该工作树"
        return "该模块"

    @staticmethod
    def _validate_claim(
        draft: ClaimDraft,
        relation_states: tuple[tuple[EvidenceRelationDraft, _EvidenceState], ...],
        project_worktrees: set[str],
    ) -> JsonObject:
        supports = [
            (relation, state)
            for relation, state in relation_states
            if relation.relation == "supports"
        ]
        contradicts = [
            (relation, state)
            for relation, state in relation_states
            if relation.relation == "contradicts"
        ]
        if not supports:
            raise InvalidInputError("every Claim requires at least one supporting Evidence")
        if any(state.project_id != draft.project_id for _, state in relation_states):
            raise InvalidInputError("Claim cannot combine Evidence from another project")
        if any(
            state.validity != "current"
            for relation, state in relation_states
            if relation.relation in {"supports", "contradicts"}
        ):
            raise InvalidInputError("stale or missing Evidence cannot support a current Claim")
        if draft.scope_kind == "worktree":
            if any(
                state.worktree_id is not None and state.worktree_id != draft.worktree_id
                for _, state in relation_states
            ):
                raise InvalidInputError("worktree Claim combines Evidence from another worktree")
            if not any(state.worktree_id == draft.worktree_id for _, state in supports):
                raise InvalidInputError("worktree Claim needs supporting Evidence in that worktree")
        if draft.scope_kind == "module":
            if any(
                state.module_id is not None and state.module_id != draft.module_id
                for _, state in relation_states
            ):
                raise InvalidInputError("module Claim combines Evidence from another module")
            if not any(state.module_id == draft.module_id for _, state in supports):
                raise InvalidInputError("module Claim needs supporting Evidence in that module")
            if draft.worktree_id is not None:
                if any(
                    state.worktree_id is not None and state.worktree_id != draft.worktree_id
                    for _, state in relation_states
                ):
                    raise InvalidInputError(
                        "module worktree Claim combines Evidence from another worktree"
                    )
                if not any(
                    state.worktree_id == draft.worktree_id and state.module_id == draft.module_id
                    for _, state in supports
                ):
                    raise InvalidInputError(
                        "module worktree Claim needs supporting Evidence in that worktree"
                    )
            else:
                AnalysisService._require_promotable_worktree_support(
                    supports,
                    project_worktrees,
                    scope_label="module",
                )
        if draft.scope_kind == "project":
            AnalysisService._require_promotable_worktree_support(
                supports,
                project_worktrees,
                scope_label="project",
            )
        if any(
            not set(relation.supported_facets) <= set(draft.facets)
            for relation, _ in relation_states
        ):
            raise InvalidInputError("ClaimEvidence supported_facets must be declared by the Claim")
        for facet in draft.facets:
            supporting = [
                state for relation, state in supports if facet in relation.supported_facets
            ]
            if not supporting:
                raise InvalidInputError(
                    f"Claim facet {facet} lacks an explicit supporting relation"
                )
            if facet == "implemented" and not any(
                AnalysisService._is_implementation(state) for state in supporting
            ):
                raise InvalidInputError("implemented requires current implementation Evidence")
            if facet == "test_defined" and not (
                any(AnalysisService._is_test_definition(state) for state in supporting)
                and any(AnalysisService._is_implementation(state) for _, state in supports)
            ):
                raise InvalidInputError(
                    "test_defined requires test-definition and implementation Evidence"
                )
            if facet == "test_verified" and not AnalysisService._has_verified_test(
                supporting,
                [state for _, state in supports],
            ):
                raise InvalidInputError(
                    "test_verified requires a passed result tied to supporting implementation"
                )
            if facet == "documented" and not any(
                AnalysisService._is_documentation(state) for state in supporting
            ):
                raise InvalidInputError(
                    "documented requires documentation/config/manifest Evidence"
                )
            if facet == "planned" and not any(
                state.evidence_kind in {"plan", "documentation"}
                or state.artifact_kind == "documentation"
                for state in supporting
            ):
                raise InvalidInputError("planned requires plan or documentation Evidence")
            if facet == "user_reported" and not any(
                state.evidence_kind == "user_statement" and state.context_fact_id is not None
                for state in supporting
            ):
                raise InvalidInputError("user_reported requires a bound context fact")
        if contradicts and draft.support_level != "conflicted":
            raise InvalidInputError("a Claim with current contradiction must be conflicted")
        if not contradicts and draft.support_level == "conflicted":
            raise InvalidInputError("conflicted support requires contradictory Evidence")
        independent = {state.anchor_sha256 for _, state in supports}
        has_context = any(state.context_fact_id is not None for _, state in supports)
        if draft.support_level == "cross_checked" and (
            len(independent) < 2
            or not any(AnalysisService._is_implementation(state) for _, state in supports)
            or not any(AnalysisService._is_cross_check(state) for _, state in supports)
        ):
            raise InvalidInputError(
                "cross_checked requires independent implementation and validation Evidence"
            )
        if draft.support_level == "user_confirmed" and not has_context:
            raise InvalidInputError("user_confirmed requires bound user context Evidence")
        if draft.support_level == "single_source" and len(independent) > 1:
            raise InvalidInputError("single_source cannot hide multiple independent sources")
        return {
            "facets": list(draft.facets),
            "support_level": draft.support_level,
            "conflicted": bool(contradicts),
            "evidence_validity": cast(
                list[JsonValue],
                sorted({state.validity for _, state in relation_states}),
            ),
        }

    @staticmethod
    def _require_promotable_worktree_support(
        supports: list[tuple[EvidenceRelationDraft, _EvidenceState]],
        project_worktrees: set[str],
        *,
        scope_label: str,
    ) -> None:
        if len(project_worktrees) <= 1:
            return
        by_equivalence: dict[str, set[str]] = {}
        for _, state in supports:
            if state.worktree_id is not None and state.source_revision_id is None:
                raise InvalidInputError(
                    f"worktree-bound non-source Evidence cannot be promoted to {scope_label} scope"
                )
            if state.source_revision_id is None:
                continue
            if state.content_equivalence_key is None or state.worktree_id is None:
                raise InvalidInputError(
                    f"multi-worktree source Evidence cannot be promoted to {scope_label} scope"
                )
            by_equivalence.setdefault(state.content_equivalence_key, set()).add(state.worktree_id)
        if any(worktrees != project_worktrees for worktrees in by_equivalence.values()):
            raise InvalidInputError(
                f"{scope_label} Claim must be supported by equivalent Evidence in every worktree"
            )

    @staticmethod
    def _is_implementation(state: _EvidenceState) -> bool:
        return state.evidence_kind == "implementation" or (
            state.artifact_kind == "source"
            and state.evidence_kind not in {"plan", "documentation", "test_result"}
        )

    @staticmethod
    def _is_test_definition(state: _EvidenceState) -> bool:
        return state.evidence_kind == "test_definition" or state.artifact_kind == "test"

    @staticmethod
    def _is_documentation(state: _EvidenceState) -> bool:
        return state.evidence_kind in {
            "documentation",
            "manifest",
            "configuration",
        } or state.artifact_kind in {
            "documentation",
            "manifest",
            "configuration",
        }

    @staticmethod
    def _is_cross_check(state: _EvidenceState) -> bool:
        return AnalysisService._is_test_definition(state) or state.evidence_kind in {
            "test_result",
            "configuration",
            "manifest",
            "result_record",
        }

    @staticmethod
    def _has_verified_test(
        test_states: list[_EvidenceState],
        all_supporting: list[_EvidenceState],
    ) -> bool:
        implementation_revisions = {
            state.source_revision_id
            for state in all_supporting
            if AnalysisService._is_implementation(state) and state.source_revision_id is not None
        }
        if not implementation_revisions:
            return False
        for state in test_states:
            if state.evidence_kind != "test_result" or state.locator.get("status") != "passed":
                continue
            related = state.locator.get("related_source_revision_ids")
            if isinstance(related, list) and implementation_revisions.intersection(
                item for item in related if isinstance(item, str)
            ):
                return True
        return False

    @staticmethod
    def _detected_personal_attribution(*projections: str) -> str | None:
        for attribution, pattern in _PERSONAL_PATTERNS:
            if any(pattern.search(projection) for projection in projections):
                return attribution
        for attribution, pattern in _OMITTED_SUBJECT_PERSONAL_PATTERNS:
            if any(pattern.search(projection) for projection in projections):
                return attribution
        if any(_FIRST_PERSON_PATTERN.search(projection) for projection in projections):
            return "personal_assertion"
        return None

    @staticmethod
    def _attribution_covers(declared: str, detected: str) -> bool:
        if declared == detected:
            return True
        if detected == "capability" and declared != "none":
            return True
        if detected == "personal_assertion":
            return declared in {"implemented", "responsible", "led", "personal_outcome"}
        return detected == "responsible" and declared == "led"

    @staticmethod
    def _validate_personal_attribution(
        draft: ClaimDraft,
        statement: str,
        relation_states: tuple[tuple[EvidenceRelationDraft, _EvidenceState], ...],
    ) -> None:
        supporting_states = [
            state for relation, state in relation_states if relation.relation == "supports"
        ]
        context_kinds = {
            state.context_fact_kind
            for relation, state in relation_states
            if relation.relation in {"supports", "contextualizes"}
            if state.context_fact_kind is not None
        }
        has_implementation = any(
            AnalysisService._is_implementation(state) for state in supporting_states
        )
        has_role = bool(context_kinds.intersection({"role", "ownership"}))
        has_learning = "learning" in context_kinds
        has_result = bool(context_kinds.intersection({"outcome", "metric"})) or any(
            state.evidence_kind == "result_record" for state in supporting_states
        )
        attribution = draft.personal_attribution
        if attribution == "capability" and not has_implementation:
            raise InvalidInputError("capability narrative requires implementation Evidence")
        if attribution == "personal_learning" and not (
            draft.category == "learning" and has_learning and has_implementation
        ):
            raise InvalidInputError(
                "past personal learning requires learning context and implementation Evidence"
            )
        if attribution == "implemented" and not (has_implementation and has_role):
            raise InvalidInputError(
                "I-implemented narrative requires implementation and role/ownership context"
            )
        if attribution in {"responsible", "led"} and not has_role:
            raise InvalidInputError("responsibility or leadership requires role/ownership context")
        if attribution == "personal_outcome" and not (has_result and has_role):
            raise InvalidInputError(
                "personal outcome requires objective result and role/ownership context"
            )
        if draft.category == "contribution" and (attribution == "none" or not has_role):
            raise InvalidInputError("contribution Claim requires explicit supported personal role")
        if draft.category == "outcome" and not has_result:
            raise InvalidInputError("outcome Claim requires objective result or metric Evidence")
        if not statement.strip():
            raise InvalidInputError("Claim statement must not be empty")

    @staticmethod
    def _prepare_assessments(
        drafts: tuple[ProjectAssessmentDraft, ...],
        dimensions: tuple[RoleDimension, ...],
        eligible: dict[str, _EligibleProject],
        evidence_by_ref: dict[str, _EvidenceState],
        gap_by_ref: dict[str, _PreparedGap],
        claim_id_by_ref: dict[str, str],
        claim_drafts: tuple[ClaimDraft, ...],
    ) -> tuple[_PreparedAssessment, ...]:
        dimension_keys = {dimension.key for dimension in dimensions}
        claim_project_by_ref = {draft.draft_id: draft.project_id for draft in claim_drafts}
        claim_evidence_by_project: dict[str, set[str]] = {}
        for claim in claim_drafts:
            claim_evidence_by_project.setdefault(claim.project_id, set()).update(
                relation.evidence_ref for relation in claim.evidence_relations
            )
        pending: list[
            tuple[
                ProjectAssessmentDraft,
                _EligibleProject,
                str,
                str,
                tuple[str, ...],
                tuple[str, ...],
            ]
        ] = []
        score_drafts: list[AssessmentScoreDraft] = []
        for draft in drafts:
            project = eligible[draft.project_id]
            if set(draft.dimension_scores_milli) != dimension_keys:
                raise InvalidInputError(
                    "assessment dimension scores must exactly match the frozen RoleLens"
                )
            if any(reference not in evidence_by_ref for reference in draft.evidence_refs):
                raise InvalidInputError("assessment references Evidence outside the analysis set")
            if any(
                evidence_by_ref[reference].project_id != draft.project_id
                for reference in draft.evidence_refs
            ):
                raise InvalidInputError("assessment cannot reference Evidence from another project")
            if not claim_evidence_by_project.get(draft.project_id, set()) <= set(
                draft.evidence_refs
            ):
                raise InvalidInputError(
                    "assessment must retain every Evidence reference used by its project Claims"
                )
            applicable_gaps = {
                reference
                for reference, gap in gap_by_ref.items()
                if gap.draft.scope_kind == "role_global" or gap.draft.project_id == draft.project_id
            }
            if set(draft.gap_refs) != applicable_gaps:
                raise InvalidInputError(
                    "assessment must retain every applicable KnowledgeGap exactly once"
                )
            evidence_states = [evidence_by_ref[reference] for reference in draft.evidence_refs]
            critical_dimensions = {
                gap_by_ref[reference].draft.dimension
                for reference in draft.gap_refs
                if gap_by_ref[reference].draft.status == "open"
                and gap_by_ref[reference].draft.severity == "critical"
            }
            recomputed_coverage = sum(
                dimension.weight_bps
                for dimension in dimensions
                if dimension.key not in critical_dimensions
                and any(
                    state.validity == "current"
                    and AnalysisService._matches_required_kind(
                        state,
                        dimension.required_evidence_kinds,
                    )
                    for state in evidence_states
                )
            )
            if draft.coverage_bps != recomputed_coverage:
                raise InvalidInputError(
                    "assessment coverage_bps does not match Repository evidence coverage"
                )
            rationale_tokens, rationale = AnalysisService._resolve_tokens(
                draft.rationale_tokens,
                evidence_by_ref=evidence_by_ref,
                gap_by_ref=gap_by_ref,
                claim_id_by_ref=claim_id_by_ref,
                allowed_evidence_refs=set(draft.evidence_refs),
                allowed_gap_refs=set(draft.gap_refs),
                allowed_claim_refs={
                    reference
                    for reference, project_id in claim_project_by_ref.items()
                    if project_id == draft.project_id
                },
            )
            if not rationale.strip():
                raise InvalidInputError("ProjectAssessment rationale must not be empty")
            evidence_ids = tuple(
                sorted(evidence_by_ref[reference].evidence_id for reference in draft.evidence_refs)
            )
            gap_ids = tuple(sorted(gap_by_ref[reference].gap_id for reference in draft.gap_refs))
            pending.append(
                (
                    draft,
                    project,
                    rationale,
                    _canonical_json(
                        cast(list[JsonValue], rationale_tokens),
                        "ProjectAssessment rationale tokens",
                    ),
                    evidence_ids,
                    gap_ids,
                )
            )
            score_drafts.append(
                AssessmentScoreDraft(
                    draft.project_id,
                    draft.dimension_scores_milli,
                    recomputed_coverage,
                )
            )
        scores = {
            score.project_id: score
            for score in score_project_assessments(dimensions, tuple(score_drafts))
        }
        return tuple(
            _PreparedAssessment(
                draft,
                project,
                rationale,
                rationale_tokens_json,
                evidence_ids,
                gap_ids,
                scores[draft.project_id].base_score_milli,
                scores[draft.project_id].final_score_milli,
                scores[draft.project_id].rank,
            )
            for draft, project, rationale, rationale_tokens_json, evidence_ids, gap_ids in pending
        )

    @staticmethod
    def _matches_required_kind(
        state: _EvidenceState,
        required_kinds: tuple[str, ...],
    ) -> bool:
        for required in required_kinds:
            if required == state.evidence_kind:
                return True
            if required == "implementation" and AnalysisService._is_implementation(state):
                return True
            if required == "test_definition" and AnalysisService._is_test_definition(state):
                return True
            if required in {
                "documentation",
                "manifest",
                "configuration",
            } and AnalysisService._is_documentation(state):
                return True
        return False

    @staticmethod
    def _persist_analysis(
        connection: sqlite3.Connection,
        request: AnalysisCommitRequest,
        prepared_evidence: list[_PreparedEvidence],
        resolved_gaps: tuple[_ResolvedGap, ...],
        prepared_claims: tuple[_PreparedClaim, ...],
        prepared_assessments: tuple[_PreparedAssessment, ...],
    ) -> str:
        timestamp = _now()
        for prepared in prepared_evidence:
            state = prepared.state
            connection.execute(
                """
                INSERT INTO evidence(
                    evidence_id, project_id, acquisition_scope, project_snapshot_id,
                    module_id, source_revision_id, content_equivalence_key, origin_kind,
                    evidence_kind, locator, summary, commit_state, created_at,
                    preparation_run_id, query_reason
                ) VALUES (?, ?, 'preparation', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    state.evidence_id,
                    state.project_id,
                    prepared.project_snapshot_id,
                    state.module_id,
                    state.source_revision_id,
                    state.content_equivalence_key,
                    state.origin_kind,
                    state.evidence_kind,
                    _canonical_json(state.locator, "Evidence locator"),
                    state.summary,
                    state.commit_state,
                    timestamp,
                    prepared.preparation_run_id,
                    prepared.query_reason,
                ),
            )
        for claim in prepared_claims:
            draft = claim.draft
            if claim.claim_is_new:
                connection.execute(
                    """
                    INSERT INTO claims(
                        claim_id, identity_sha256, claim_key, category, scope_kind,
                        project_id, worktree_id, module_id, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        claim.claim_id,
                        claim.identity_sha256,
                        draft.claim_key,
                        draft.category,
                        draft.scope_kind,
                        draft.project_id,
                        draft.worktree_id,
                        draft.module_id,
                        timestamp,
                    ),
                )
            if claim.revision_is_new:
                connection.execute(
                    """
                    INSERT INTO claim_revisions(
                        claim_revision_id, claim_id, revision_no, revision_sha256,
                        statement, statement_tokens, facets, support_level,
                        personal_attribution, review_semantic_projection,
                        review_semantic_sha256,
                        supersedes_id, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        claim.claim_revision_id,
                        claim.claim_id,
                        claim.revision_no,
                        claim.revision_sha256,
                        claim.statement,
                        claim.statement_tokens_json,
                        _canonical_json(list(draft.facets), "Claim facets"),
                        draft.support_level,
                        draft.personal_attribution,
                        claim.review_projection_json,
                        claim.review_semantic_sha256,
                        claim.supersedes_id,
                        timestamp,
                    ),
                )
                connection.executemany(
                    """
                    INSERT INTO claim_evidence(
                        claim_revision_id, evidence_id, relation, supported_facets
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (
                        (
                            claim.claim_revision_id,
                            evidence_id,
                            relation,
                            _canonical_json(list(supported_facets), "supported facets"),
                        )
                        for evidence_id, relation, supported_facets in claim.relations
                    ),
                )
        for gap in resolved_gaps:
            prepared_gap = gap.prepared
            gap_draft = prepared_gap.draft
            connection.execute(
                """
                INSERT INTO knowledge_gaps(
                    gap_id, gap_key, preparation_run_id, scope_kind, scope_id,
                    project_id, module_id, dimension, stable_gap_concept_key,
                    gap_contract_version, description, description_tokens,
                    severity, resolution_kind, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    prepared_gap.gap_id,
                    prepared_gap.gap_key,
                    request.preparation_run_id,
                    gap_draft.scope_kind,
                    prepared_gap.scope_id,
                    gap_draft.project_id,
                    gap_draft.module_id,
                    gap_draft.dimension,
                    gap_draft.stable_gap_concept_key,
                    gap_draft.gap_contract_version,
                    gap.description,
                    gap.description_tokens_json,
                    gap_draft.severity,
                    gap_draft.resolution_kind,
                    gap_draft.status,
                ),
            )
        for rank, claim in enumerate(prepared_claims, start=1):
            draft = claim.draft
            connection.execute(
                """
                INSERT INTO preparation_claims(
                    preparation_run_id, claim_revision_id, project_id,
                    worktree_id, module_id, rank, section
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    request.preparation_run_id,
                    claim.claim_revision_id,
                    draft.project_id,
                    draft.worktree_id,
                    draft.module_id,
                    rank,
                    draft.section,
                ),
            )
        for assessment in prepared_assessments:
            connection.execute(
                """
                INSERT INTO project_assessments(
                    preparation_run_id, project_id, project_snapshot_id,
                    snapshot_disposition, dimension_scores_milli,
                    evidence_and_gap_refs, rationale, rationale_tokens,
                    coverage_bps, base_score_milli, final_score_milli, rank
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    request.preparation_run_id,
                    assessment.draft.project_id,
                    assessment.project.project_snapshot_id,
                    assessment.project.snapshot_disposition,
                    _canonical_json(
                        cast(JsonObject, assessment.draft.dimension_scores_milli),
                        "dimension scores",
                    ),
                    _canonical_json(
                        {
                            "evidence_ids": list(assessment.evidence_ids),
                            "gap_ids": list(assessment.gap_ids),
                        },
                        "assessment references",
                    ),
                    assessment.rationale,
                    assessment.rationale_tokens_json,
                    assessment.draft.coverage_bps,
                    assessment.base_score_milli,
                    assessment.final_score_milli,
                    assessment.rank,
                ),
            )
        ReviewService.ensure_bindings_in_connection(
            connection,
            request.preparation_run_id,
            bound_at=timestamp,
        )
        analysis_commit_id = _new_id()
        connection.execute(
            """
            INSERT INTO analysis_commits(
                analysis_commit_id, request_id, request_sha256, preparation_run_id,
                role_lens_id, contract_version, evidence_count, claim_count,
                assessment_count, gap_count, committed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                analysis_commit_id,
                request.request_id,
                request.request_sha256,
                request.preparation_run_id,
                request.role_lens_id,
                ANALYSIS_COMMIT_VERSION,
                len(prepared_evidence),
                len(prepared_claims),
                len(prepared_assessments),
                len(resolved_gaps),
                timestamp,
            ),
        )
        connection.execute(
            """
            UPDATE preparation_runs
            SET status = 'ready', status_reason = NULL,
                last_transition_at = ?, finished_at = NULL
            WHERE preparation_run_id = ?
            """,
            (timestamp, request.preparation_run_id),
        )
        return analysis_commit_id

    def _result(self, analysis_commit_id: str) -> dict[str, object]:
        with self._database.read_connection() as connection:
            commit = connection.execute(
                """
                SELECT ac.analysis_commit_id, ac.preparation_run_id, ac.role_lens_id,
                       ac.contract_version, ac.evidence_count, ac.claim_count,
                       ac.assessment_count, ac.gap_count, ac.committed_at,
                       pr.status AS run_status
                FROM analysis_commits AS ac
                JOIN preparation_runs AS pr
                  ON pr.preparation_run_id = ac.preparation_run_id
                WHERE ac.analysis_commit_id = ?
                """,
                (analysis_commit_id,),
            ).fetchone()
            if commit is None:
                raise InvalidInputError("analysis commit does not exist")
            claims = [
                {
                    "claim_id": str(row["claim_id"]),
                    "claim_revision_id": str(row["claim_revision_id"]),
                    "project_id": str(row["project_id"]),
                    "rank": int(row["rank"]),
                    "section": str(row["section"]),
                    "facets": json.loads(str(row["facets"])),
                    "support_level": str(row["support_level"]),
                    "review_semantic_sha256": str(row["review_semantic_sha256"]),
                }
                for row in connection.execute(
                    """
                    SELECT c.claim_id, pc.claim_revision_id, pc.project_id,
                           pc.rank, pc.section, cr.facets, cr.support_level,
                           cr.review_semantic_sha256
                    FROM preparation_claims AS pc
                    JOIN claim_revisions AS cr
                      ON cr.claim_revision_id = pc.claim_revision_id
                    JOIN claims AS c ON c.claim_id = cr.claim_id
                    WHERE pc.preparation_run_id = ? ORDER BY pc.rank
                    """,
                    (str(commit["preparation_run_id"]),),
                ).fetchall()
            ]
            assessments = [
                {
                    "project_id": str(row["project_id"]),
                    "coverage_bps": int(row["coverage_bps"]),
                    "base_score_milli": int(row["base_score_milli"]),
                    "final_score_milli": int(row["final_score_milli"]),
                    "rank": int(row["rank"]),
                }
                for row in connection.execute(
                    """
                    SELECT project_id, coverage_bps, base_score_milli,
                           final_score_milli, rank
                    FROM project_assessments
                    WHERE preparation_run_id = ? ORDER BY rank
                    """,
                    (str(commit["preparation_run_id"]),),
                ).fetchall()
            ]
            gaps = [
                {
                    "gap_id": str(row["gap_id"]),
                    "gap_key": str(row["gap_key"]),
                    "scope_kind": str(row["scope_kind"]),
                    "project_id": row["project_id"],
                    "dimension": str(row["dimension"]),
                    "severity": str(row["severity"]),
                    "resolution_kind": str(row["resolution_kind"]),
                    "status": str(row["status"]),
                }
                for row in connection.execute(
                    """
                    SELECT gap_id, gap_key, scope_kind, project_id, dimension,
                           severity, resolution_kind, status
                    FROM knowledge_gaps
                    WHERE preparation_run_id = ?
                    ORDER BY scope_kind, scope_id, dimension, gap_key
                    """,
                    (str(commit["preparation_run_id"]),),
                ).fetchall()
            ]
            source_checks = [
                {
                    "source_revision_id": str(row["source_revision_id"]),
                    "expected_sha256": str(row["expected_sha256"]),
                    "status": str(row["status"]),
                    "observed_at": str(row["observed_at"]),
                }
                for row in connection.execute(
                    """
                    SELECT source_revision_id, expected_sha256, status, observed_at
                    FROM preparation_source_checks
                    WHERE preparation_run_id = ? AND phase = 'commit'
                    ORDER BY source_revision_id
                    """,
                    (str(commit["preparation_run_id"]),),
                ).fetchall()
            ]
        return {
            "status": "ok",
            "run_status": str(commit["run_status"]),
            "analysis_commit": {
                "analysis_commit_id": str(commit["analysis_commit_id"]),
                "preparation_run_id": str(commit["preparation_run_id"]),
                "role_lens_id": str(commit["role_lens_id"]),
                "contract_version": str(commit["contract_version"]),
                "evidence_count": int(commit["evidence_count"]),
                "claim_count": int(commit["claim_count"]),
                "assessment_count": int(commit["assessment_count"]),
                "gap_count": int(commit["gap_count"]),
                "committed_at": str(commit["committed_at"]),
            },
            "claims": claims,
            "project_assessments": assessments,
            "knowledge_gaps": gaps,
            "source_checks": source_checks,
        }

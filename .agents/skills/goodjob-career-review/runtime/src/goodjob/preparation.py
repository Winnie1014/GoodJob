"""Role-oriented preparation contracts and deterministic repository operations."""

from __future__ import annotations

import errno
import hashlib
import hmac
import json
import math
import os
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import TypeGuard, cast

from goodjob.db import Database
from goodjob.errors import CapabilityError, InvalidInputError
from goodjob.source_io import hash_regular_file, open_absolute_regular_file, read_open_file

ROLE_LENS_CONTRACT_VERSION = "role-lens-v1"
JOB_INPUT_CONTRACT_VERSION = "job-input-v1"
PREPARATION_REQUEST_CONTRACT_VERSION = "preparation-request-v1"
EVIDENCE_BUNDLE_CONTRACT_VERSION = "evidence-bundle-v1"
MAX_JD_BYTES = 512 * 1024
MAX_STRUCTURED_JSON_BYTES = 256 * 1024
MAX_DIMENSIONS = 20
MAX_EVIDENCE_KINDS_PER_DIMENSION = 32
MAX_EVIDENCE_PER_PROJECT = 200
MAX_BUNDLE_EVIDENCE = 1000
MAX_BUNDLE_ISSUES = 200
MAX_SOURCE_REVISION_BATCH = 200
MAX_PRIVATE_PAYLOAD_BYTES = 12 * 1024 * 1024
ALLOWED_EXPORTS = frozenset({"english_resume", "english_interview_qa"})
PUBLIC_SOURCE_CHECK_PHASE = "before_read"
type JsonValue = None | bool | int | float | str | list[JsonValue] | dict[str, JsonValue]
type JsonObject = dict[str, JsonValue]


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _new_id() -> str:
    return str(uuid.uuid4())


def _constant_time_text_equal(left: str, right: str) -> bool:
    try:
        return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))
    except UnicodeEncodeError:
        return False


def _is_json_value(value: object) -> TypeGuard[JsonValue]:
    if isinstance(value, float):
        return math.isfinite(value)
    if value is None or isinstance(value, bool | int | str):
        return True
    if isinstance(value, list):
        return all(_is_json_value(item) for item in value)
    if isinstance(value, dict):
        return all(isinstance(key, str) and _is_json_value(item) for key, item in value.items())
    return False


def _object(value: object, field_name: str) -> JsonObject:
    try:
        is_object = isinstance(value, dict) and _is_json_value(value)
    except RecursionError as exc:
        raise InvalidInputError(f"{field_name} exceeds the JSON nesting limit") from exc
    if not is_object:
        raise InvalidInputError(f"{field_name} must be a JSON object")
    return cast(JsonObject, value)


def _reject_unknown_fields(
    value: JsonObject,
    allowed: frozenset[str],
    field_name: str,
) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise InvalidInputError(f"{field_name} contains unsupported fields: {', '.join(unknown)}")


def _text(
    value: object,
    field_name: str,
    *,
    maximum: int = 2000,
    allow_empty: bool = False,
) -> str:
    if not isinstance(value, str) or (not allow_empty and not value.strip()):
        qualifier = "a string" if allow_empty else "a non-empty string"
        raise InvalidInputError(f"{field_name} must be {qualifier}")
    if "\x00" in value or len(value) > maximum:
        raise InvalidInputError(f"{field_name} is outside the accepted text boundary")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise InvalidInputError(f"{field_name} must contain valid UTF-8 text") from exc
    return value


def _optional_text(value: object, field_name: str, *, maximum: int = 2000) -> str | None:
    if value is None:
        return None
    return _text(value, field_name, maximum=maximum)


def _canonical_json(
    value: JsonValue,
    field_name: str,
    *,
    maximum_bytes: int = MAX_STRUCTURED_JSON_BYTES,
) -> str:
    try:
        serialized = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        encoded = serialized.encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError, RecursionError) as exc:
        raise InvalidInputError(f"{field_name} must contain canonical JSON values") from exc
    if len(encoded) > maximum_bytes:
        raise InvalidInputError(f"{field_name} exceeds the structured input limit")
    return serialized


def _structured(value: object, field_name: str, *, allow_empty: bool = False) -> JsonValue:
    if not isinstance(value, list | dict):
        raise InvalidInputError(f"{field_name} must be a JSON array or object")
    try:
        is_structured = _is_json_value(value)
    except RecursionError as exc:
        raise InvalidInputError(f"{field_name} exceeds the JSON nesting limit") from exc
    if not is_structured:
        raise InvalidInputError(f"{field_name} must be a JSON array or object")
    if not allow_empty and not value:
        raise InvalidInputError(f"{field_name} must not be empty")
    _canonical_json(value, field_name)
    return value


def _text_list(
    value: object,
    field_name: str,
    *,
    maximum_items: int,
    allow_empty: bool = False,
) -> tuple[str, ...]:
    if not isinstance(value, list) or (not allow_empty and not value):
        raise InvalidInputError(f"{field_name} must be a JSON string list")
    if len(value) > maximum_items:
        raise InvalidInputError(f"{field_name} exceeds the item limit")
    result = tuple(_text(item, field_name, maximum=500) for item in value)
    if len(set(result)) != len(result):
        raise InvalidInputError(f"{field_name} must not contain duplicates")
    return result


def _read_jd_file(raw_path: str) -> tuple[str, str, str]:
    try:
        canonical_path = Path(raw_path).expanduser().resolve(strict=True)
        file_fd, file_stat = open_absolute_regular_file(canonical_path)
    except (OSError, RuntimeError) as exc:
        raise InvalidInputError(f"JD file cannot be opened safely: {raw_path}") from exc
    try:
        if file_stat.st_size > MAX_JD_BYTES:
            raise InvalidInputError(f"JD file exceeds the {MAX_JD_BYTES}-byte limit")
        try:
            content = read_open_file(file_fd, maximum_bytes=MAX_JD_BYTES)
            text = content.decode("utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise InvalidInputError(
                f"JD file is not bounded readable UTF-8: {canonical_path}"
            ) from exc
    finally:
        os.close(file_fd)
    if not text.strip():
        raise InvalidInputError(f"JD file must not be empty: {canonical_path}")
    return text, str(canonical_path), hashlib.sha256(content).hexdigest()


@dataclass(frozen=True)
class JobInputDraft:
    role_name: str
    jd_input_kind: str
    jd_text: str | None
    jd_source_path: str | None
    jd_content_sha256: str | None
    inferred_level: str | None
    level_override: str | None

    @classmethod
    def from_request(cls, request: JsonObject) -> JobInputDraft:
        role_name = _text(request.get("target_role"), "target_role", maximum=300)
        inferred_level = _optional_text(
            request.get("inferred_level"), "inferred_level", maximum=200
        )
        level_override = _optional_text(
            request.get("level_override"), "level_override", maximum=200
        )
        jd_value = request.get("jd_input", {"kind": "none"})
        jd_input = _object(jd_value, "jd_input")
        kind = _text(jd_input.get("kind"), "jd_input.kind", maximum=40)
        if kind in {"none", "continue_without_jd"}:
            _reject_unknown_fields(jd_input, frozenset({"kind"}), "jd_input")
            return cls(role_name, kind, None, None, None, inferred_level, level_override)
        if kind == "text":
            _reject_unknown_fields(jd_input, frozenset({"kind", "text"}), "jd_input")
            jd_text = _text(
                jd_input.get("text"),
                "jd_input.text",
                maximum=MAX_JD_BYTES,
            )
            encoded = jd_text.encode("utf-8")
            if len(encoded) > MAX_JD_BYTES:
                raise InvalidInputError(f"JD text exceeds the {MAX_JD_BYTES}-byte limit")
            return cls(
                role_name,
                kind,
                jd_text,
                None,
                hashlib.sha256(encoded).hexdigest(),
                inferred_level,
                level_override,
            )
        if kind == "file":
            _reject_unknown_fields(jd_input, frozenset({"kind", "path"}), "jd_input")
            raw_path = _text(jd_input.get("path"), "jd_input.path", maximum=4096)
            jd_text, source_path, digest = _read_jd_file(raw_path)
            return cls(
                role_name,
                kind,
                jd_text,
                source_path,
                digest,
                inferred_level,
                level_override,
            )
        raise InvalidInputError("jd_input.kind must be none, text, file, or continue_without_jd")

    @property
    def has_jd(self) -> bool:
        return self.jd_text is not None

    @property
    def applied_level(self) -> str | None:
        return self.level_override or self.inferred_level

    def as_hash_value(self) -> JsonObject:
        return {
            "role_name": self.role_name,
            "jd_input_kind": self.jd_input_kind,
            "jd_source_path": self.jd_source_path,
            "jd_content_sha256": self.jd_content_sha256,
            "inferred_level": self.inferred_level,
            "level_override": self.level_override,
        }

    @property
    def validation_sha256(self) -> str:
        value = _canonical_json(self.as_hash_value(), "job_input")
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    def as_public_json(self) -> JsonObject:
        return {
            "contract_version": JOB_INPUT_CONTRACT_VERSION,
            "role_name": self.role_name,
            "jd_input_kind": self.jd_input_kind,
            "jd_source_path": self.jd_source_path,
            "jd_content_sha256": self.jd_content_sha256,
            "inferred_level": self.inferred_level,
            "level_override": self.level_override,
            "applied_level": self.applied_level,
            "has_jd": self.has_jd,
            "validation_sha256": self.validation_sha256,
        }


def validate_job_input(value: object) -> dict[str, object]:
    """Validate private role/JD inputs without creating scan or preparation state."""
    request = _object(value, "job_input")
    _reject_unknown_fields(
        request,
        frozenset(
            {
                "contract_version",
                "target_role",
                "jd_input",
                "inferred_level",
                "level_override",
            }
        ),
        "job_input",
    )
    version = _text(request.get("contract_version"), "contract_version", maximum=80)
    if version != JOB_INPUT_CONTRACT_VERSION:
        raise InvalidInputError("unsupported JobInput contract version")
    return {"status": "ok", "job_input": JobInputDraft.from_request(request).as_public_json()}


@dataclass(frozen=True)
class RoleDimension:
    key: str
    display_name: str
    weight_bps: int
    evaluation_criteria: str
    required_evidence_kinds: tuple[str, ...]
    value: JsonObject

    @classmethod
    def from_value(cls, value: object, index: int) -> RoleDimension:
        dimension = _object(value, f"role_lens.dimensions[{index}]")
        _reject_unknown_fields(
            dimension,
            frozenset(
                {
                    "key",
                    "display_name",
                    "weight_bps",
                    "evaluation_criteria",
                    "required_evidence_kinds",
                }
            ),
            f"role_lens.dimensions[{index}]",
        )
        key = _text(dimension.get("key"), "dimension.key", maximum=80)
        if any(character not in "abcdefghijklmnopqrstuvwxyz0123456789_.-" for character in key):
            raise InvalidInputError("dimension.key must use lowercase stable-key characters")
        display_name = _text(dimension.get("display_name"), "dimension.display_name", maximum=200)
        weight = dimension.get("weight_bps")
        if not isinstance(weight, int) or isinstance(weight, bool) or not 0 <= weight <= 10000:
            raise InvalidInputError("dimension.weight_bps must be an integer from 0 to 10000")
        criteria = _text(
            dimension.get("evaluation_criteria"),
            "dimension.evaluation_criteria",
            maximum=2000,
        )
        evidence_kinds = _text_list(
            dimension.get("required_evidence_kinds"),
            "dimension.required_evidence_kinds",
            maximum_items=MAX_EVIDENCE_KINDS_PER_DIMENSION,
        )
        _canonical_json(dimension, "role_lens.dimension")
        return cls(key, display_name, weight, criteria, evidence_kinds, dimension)


def _dimensions(value: object) -> tuple[RoleDimension, ...]:
    if not isinstance(value, list) or not 1 <= len(value) <= MAX_DIMENSIONS:
        raise InvalidInputError("role_lens.dimensions must be a bounded non-empty JSON list")
    dimensions = tuple(RoleDimension.from_value(item, index) for index, item in enumerate(value))
    keys = [dimension.key for dimension in dimensions]
    if len(set(keys)) != len(keys):
        raise InvalidInputError("RoleLens dimension keys must be unique")
    if sum(dimension.weight_bps for dimension in dimensions) != 10000:
        raise InvalidInputError("RoleLens dimension weights must sum exactly to 10000")
    return dimensions


@dataclass(frozen=True)
class RoleLensDraft:
    contract_version: str
    dimensions: tuple[RoleDimension, ...]
    evidence_requirements: JsonValue
    ranking_rules: JsonValue
    output_sections: JsonValue
    question_strategy: JsonValue
    gap_rules: JsonValue
    assumptions: tuple[str, ...]
    generator_id: str
    prompt_contract_version: str

    @classmethod
    def from_value(cls, value: object, *, has_jd: bool) -> RoleLensDraft:
        lens = _object(value, "role_lens")
        _reject_unknown_fields(
            lens,
            frozenset(
                {
                    "contract_version",
                    "dimensions",
                    "evidence_requirements",
                    "ranking_rules",
                    "output_sections",
                    "question_strategy",
                    "gap_rules",
                    "assumptions",
                    "generator_id",
                    "prompt_contract_version",
                }
            ),
            "role_lens",
        )
        contract_version = _text(
            lens.get("contract_version"), "role_lens.contract_version", maximum=80
        )
        if contract_version != ROLE_LENS_CONTRACT_VERSION:
            raise InvalidInputError("unsupported RoleLens contract version")
        dimensions = _dimensions(lens.get("dimensions"))
        evidence_requirements = _structured(
            lens.get("evidence_requirements"), "role_lens.evidence_requirements"
        )
        ranking_rules = _structured(lens.get("ranking_rules"), "role_lens.ranking_rules")
        output_sections = _structured(lens.get("output_sections"), "role_lens.output_sections")
        question_strategy = _structured(
            lens.get("question_strategy"), "role_lens.question_strategy"
        )
        gap_rules = _structured(lens.get("gap_rules"), "role_lens.gap_rules")
        assumptions = _text_list(
            lens.get("assumptions", []),
            "role_lens.assumptions",
            maximum_items=100,
            allow_empty=has_jd,
        )
        if not has_jd and not assumptions:
            raise InvalidInputError("RoleLens without a JD must record at least one assumption")
        generator_id = _text(lens.get("generator_id"), "role_lens.generator_id", maximum=200)
        prompt_contract_version = _text(
            lens.get("prompt_contract_version"),
            "role_lens.prompt_contract_version",
            maximum=200,
        )
        return cls(
            contract_version,
            dimensions,
            evidence_requirements,
            ranking_rules,
            output_sections,
            question_strategy,
            gap_rules,
            assumptions,
            generator_id,
            prompt_contract_version,
        )

    def as_json(self) -> JsonObject:
        return {
            "contract_version": self.contract_version,
            "dimensions": [dimension.value for dimension in self.dimensions],
            "evidence_requirements": self.evidence_requirements,
            "ranking_rules": self.ranking_rules,
            "output_sections": self.output_sections,
            "question_strategy": self.question_strategy,
            "gap_rules": self.gap_rules,
            "assumptions": list(self.assumptions),
            "generator_id": self.generator_id,
            "prompt_contract_version": self.prompt_contract_version,
        }

    @property
    def sha256(self) -> str:
        return hashlib.sha256(
            _canonical_json(
                self.as_json(),
                "role_lens",
                maximum_bytes=MAX_PRIVATE_PAYLOAD_BYTES,
            ).encode()
        ).hexdigest()


@dataclass(frozen=True)
class PreparationRequestDraft:
    request_id: str
    scan_run_id: str
    config_revision: str
    requested_exports: tuple[str, ...]
    evidence_limit_per_project: int
    job_input_validation_sha256: str
    job_input: JobInputDraft
    role_lens: RoleLensDraft

    @classmethod
    def from_value(cls, value: object) -> PreparationRequestDraft:
        request = _object(value, "preparation_request")
        _reject_unknown_fields(
            request,
            frozenset(
                {
                    "contract_version",
                    "request_id",
                    "scan_run_id",
                    "config_revision",
                    "requested_exports",
                    "evidence_limit_per_project",
                    "job_input_validation_sha256",
                    "target_role",
                    "jd_input",
                    "inferred_level",
                    "level_override",
                    "role_lens",
                }
            ),
            "preparation_request",
        )
        _canonical_json(
            request,
            "preparation_request",
            maximum_bytes=MAX_PRIVATE_PAYLOAD_BYTES,
        )
        version = _text(request.get("contract_version"), "contract_version", maximum=80)
        if version != PREPARATION_REQUEST_CONTRACT_VERSION:
            raise InvalidInputError("unsupported PreparationRequest contract version")
        request_id = _text(request.get("request_id"), "request_id", maximum=200)
        scan_run_id = _text(request.get("scan_run_id"), "scan_run_id", maximum=200)
        config_revision = _text(request.get("config_revision"), "config_revision", maximum=200)
        exports = _text_list(
            request.get("requested_exports", []),
            "requested_exports",
            maximum_items=len(ALLOWED_EXPORTS),
            allow_empty=True,
        )
        if not set(exports) <= ALLOWED_EXPORTS:
            raise InvalidInputError("requested_exports contains an unsupported export kind")
        limit = request.get("evidence_limit_per_project", 80)
        if (
            not isinstance(limit, int)
            or isinstance(limit, bool)
            or not 1 <= limit <= MAX_EVIDENCE_PER_PROJECT
        ):
            raise InvalidInputError("evidence_limit_per_project must be an integer from 1 to 200")
        job_input = JobInputDraft.from_request(request)
        job_input_validation_sha256 = _text(
            request.get("job_input_validation_sha256"),
            "job_input_validation_sha256",
            maximum=64,
        )
        if (
            len(job_input_validation_sha256) != 64
            or any(character not in "0123456789abcdef" for character in job_input_validation_sha256)
            or not hmac.compare_digest(job_input_validation_sha256, job_input.validation_sha256)
        ):
            raise InvalidInputError("job input changed since validate_job_input")
        role_lens = RoleLensDraft.from_value(request.get("role_lens"), has_jd=job_input.has_jd)
        return cls(
            request_id,
            scan_run_id,
            config_revision,
            exports,
            limit,
            job_input_validation_sha256,
            job_input,
            role_lens,
        )

    def sha256(self, workspace_path: Path) -> str:
        value: JsonObject = {
            "contract_version": PREPARATION_REQUEST_CONTRACT_VERSION,
            "workspace_path": str(workspace_path),
            "request_id": self.request_id,
            "scan_run_id": self.scan_run_id,
            "config_revision": self.config_revision,
            "requested_exports": list(self.requested_exports),
            "evidence_limit_per_project": self.evidence_limit_per_project,
            "job_input_validation_sha256": self.job_input_validation_sha256,
            "job_input": self.job_input.as_hash_value(),
            "role_lens": self.role_lens.as_json(),
        }
        return hashlib.sha256(
            _canonical_json(
                value,
                "preparation_request",
                maximum_bytes=MAX_PRIVATE_PAYLOAD_BYTES,
            ).encode()
        ).hexdigest()


@dataclass(frozen=True)
class AssessmentScoreDraft:
    project_id: str
    dimension_scores_milli: dict[str, int]
    coverage_bps: int


@dataclass(frozen=True)
class ScoredAssessment:
    project_id: str
    dimension_scores_milli: dict[str, int]
    coverage_bps: int
    base_score_milli: int
    final_score_milli: int
    rank: int

    def as_json(self) -> dict[str, object]:
        return {
            "project_id": self.project_id,
            "dimension_scores_milli": self.dimension_scores_milli,
            "coverage_bps": self.coverage_bps,
            "base_score_milli": self.base_score_milli,
            "final_score_milli": self.final_score_milli,
            "rank": self.rank,
        }


def score_project_assessments(
    dimensions: tuple[RoleDimension, ...],
    drafts: tuple[AssessmentScoreDraft, ...],
) -> tuple[ScoredAssessment, ...]:
    """Recompute fixed-point totals and assign a deterministic continuous rank."""
    dimension_keys = {dimension.key for dimension in dimensions}
    if not dimension_keys or sum(dimension.weight_bps for dimension in dimensions) != 10000:
        raise InvalidInputError("cannot score against an invalid RoleLens")
    project_ids = [draft.project_id for draft in drafts]
    if len(set(project_ids)) != len(project_ids):
        raise InvalidInputError("ProjectAssessment drafts must have unique project IDs")
    pending: list[tuple[str, dict[str, int], int, int, int]] = []
    for draft in drafts:
        project_id = _text(draft.project_id, "project_id", maximum=200)
        if set(draft.dimension_scores_milli) != dimension_keys:
            raise InvalidInputError("dimension scores must exactly match the frozen RoleLens")
        for score in draft.dimension_scores_milli.values():
            if not isinstance(score, int) or isinstance(score, bool) or not 0 <= score <= 1000:
                raise InvalidInputError("dimension scores must be integers from 0 to 1000")
        if (
            not isinstance(draft.coverage_bps, int)
            or isinstance(draft.coverage_bps, bool)
            or not 0 <= draft.coverage_bps <= 10000
        ):
            raise InvalidInputError("coverage_bps must be an integer from 0 to 10000")
        weighted_total = sum(
            dimension.weight_bps * draft.dimension_scores_milli[dimension.key]
            for dimension in dimensions
        )
        base_score = (weighted_total + 5000) // 10000
        final_score = (base_score * draft.coverage_bps + 5000) // 10000
        pending.append(
            (
                project_id,
                dict(sorted(draft.dimension_scores_milli.items())),
                draft.coverage_bps,
                base_score,
                final_score,
            )
        )
    pending.sort(key=lambda item: (-item[4], item[0]))
    return tuple(
        ScoredAssessment(project_id, scores, coverage, base, final, rank)
        for rank, (project_id, scores, coverage, base, final) in enumerate(pending, start=1)
    )


@dataclass(frozen=True)
class _ProjectBinding:
    project_id: str
    display_name: str
    snapshot_disposition: str
    project_snapshot_id: str | None

    @property
    def eligible(self) -> bool:
        return self.snapshot_disposition in {"fresh", "carried_forward"}


@dataclass(frozen=True)
class _SourceTarget:
    source_revision_id: str
    expected_sha256: str
    worktree_id: str
    worktree_root: Path
    relative_path: str


@dataclass(frozen=True)
class SourceCheckResult:
    source_revision_id: str
    expected_sha256: str
    worktree_id: str
    worktree_root: Path
    relative_path: str
    observed_at: str
    status: str
    mismatch_kind: str | None
    observed_sha256: str | None

    def as_json(self, workspace_root: Path) -> dict[str, object]:
        worktree_relative_root, workspace_relative_path = _workspace_source_locator(
            workspace_root, self.worktree_root, self.relative_path
        )
        return {
            "source_revision_id": self.source_revision_id,
            "expected_sha256": self.expected_sha256,
            "worktree_id": self.worktree_id,
            "worktree_relative_root": worktree_relative_root,
            "relative_path": self.relative_path,
            "workspace_relative_path": workspace_relative_path,
            "observed_at": self.observed_at,
            "status": self.status,
            "mismatch_kind": self.mismatch_kind,
            "observed_sha256": self.observed_sha256,
        }


def _check_source(target: _SourceTarget) -> SourceCheckResult:
    try:
        observed_sha256, _ = hash_regular_file(target.worktree_root, target.relative_path)
    except OSError as exc:
        kind = "missing" if exc.errno in {errno.ENOENT, errno.ENOTDIR} else "unreadable"
        return SourceCheckResult(
            target.source_revision_id,
            target.expected_sha256,
            target.worktree_id,
            target.worktree_root,
            target.relative_path,
            _now(),
            "mismatch",
            kind,
            None,
        )
    if not hmac.compare_digest(observed_sha256, target.expected_sha256):
        return SourceCheckResult(
            target.source_revision_id,
            target.expected_sha256,
            target.worktree_id,
            target.worktree_root,
            target.relative_path,
            _now(),
            "mismatch",
            "sha256_mismatch",
            observed_sha256,
        )
    return SourceCheckResult(
        target.source_revision_id,
        target.expected_sha256,
        target.worktree_id,
        target.worktree_root,
        target.relative_path,
        _now(),
        "passed",
        None,
        observed_sha256,
    )


def _workspace_source_locator(
    workspace_root: Path,
    worktree_root: Path,
    relative_path: str,
) -> tuple[str, str]:
    path = PurePosixPath(relative_path)
    if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        raise CapabilityError("frozen source path is not a safe relative path")
    try:
        canonical_workspace = workspace_root.resolve(strict=False)
        canonical_worktree = worktree_root.resolve(strict=False)
        relative_root = canonical_worktree.relative_to(canonical_workspace)
    except (OSError, RuntimeError, ValueError) as exc:
        raise CapabilityError("frozen source target is outside the authorized workspace") from exc
    worktree_relative_root = "." if not relative_root.parts else relative_root.as_posix()
    workspace_relative_path = (
        path.as_posix()
        if worktree_relative_root == "."
        else (relative_root / Path(*path.parts)).as_posix()
    )
    return worktree_relative_root, workspace_relative_path


def _stored_json(raw: object, field_name: str) -> JsonValue:
    try:
        value: object = json.loads(str(raw))
    except (json.JSONDecodeError, RecursionError) as exc:
        raise InvalidInputError(f"stored {field_name} is not valid JSON") from exc
    if not _is_json_value(value):
        raise InvalidInputError(f"stored {field_name} is not a JSON value")
    return value


def _directory_usage(path: Path) -> int:
    if not path.exists():
        return 0
    if path.is_file():
        return path.stat().st_size
    total = 0
    for entry in path.rglob("*"):
        try:
            if entry.is_file():
                total += entry.stat().st_size
        except OSError:
            continue
    return total


class PreparationService:
    """Freeze one scan and dynamic RoleLens before model-driven analysis."""

    def __init__(self, database: Database) -> None:
        self._database = database

    def start(
        self,
        *,
        workspace_path: Path,
        authorization_receipt_id: str,
        request_value: object,
    ) -> dict[str, object]:
        canonical_workspace = workspace_path.expanduser().resolve(strict=False)
        request = PreparationRequestDraft.from_value(request_value)
        request_sha256 = request.sha256(canonical_workspace)
        self._database.migrate()
        with self._database.read_connection() as connection:
            existing = connection.execute(
                """
                SELECT preparation_run_id, request_sha256, authorization_receipt_id
                FROM preparation_runs WHERE request_id = ?
                """,
                (request.request_id,),
            ).fetchone()
            if existing is not None:
                if not hmac.compare_digest(str(existing["request_sha256"]), request_sha256):
                    raise InvalidInputError("request_id is already bound to different inputs")
                self._require_same_session_binding(
                    connection,
                    str(existing["authorization_receipt_id"]),
                    authorization_receipt_id,
                )
                return self._result(str(existing["preparation_run_id"]))
            workspace_id, projects = self._scan_context(
                connection,
                canonical_workspace,
                request.scan_run_id,
            )
            self._require_source_receipt(connection, authorization_receipt_id, canonical_workspace)
            source_targets = self._source_targets_for_scan(connection, request.scan_run_id)
        self._require_targets_within_workspace(canonical_workspace, source_targets)
        checks = tuple(_check_source(target) for target in source_targets)
        preparation_run_id = self._persist_start(
            canonical_workspace=canonical_workspace,
            workspace_id=workspace_id,
            authorization_receipt_id=authorization_receipt_id,
            request=request,
            request_sha256=request_sha256,
            expected_projects=projects,
            checks=checks,
        )
        return self._result(preparation_run_id)

    def verify_source_revisions(
        self,
        *,
        preparation_run_id: str,
        authorization_receipt_id: str,
        source_revision_ids: tuple[str, ...],
        phase: str,
    ) -> dict[str, object]:
        run_id = _text(preparation_run_id, "preparation_run_id", maximum=200)
        if phase != PUBLIC_SOURCE_CHECK_PHASE:
            raise InvalidInputError("public source checks only support the before_read phase")
        if not 1 <= len(source_revision_ids) <= MAX_SOURCE_REVISION_BATCH or len(
            set(source_revision_ids)
        ) != len(source_revision_ids):
            raise InvalidInputError("source_revision_ids must be a bounded unique non-empty list")
        for source_revision_id in source_revision_ids:
            _text(source_revision_id, "source_revision_id", maximum=200)
        self._database.migrate()
        with self._database.read_connection() as connection:
            status, workspace_root = self._require_active_run_and_session(
                connection, run_id, authorization_receipt_id
            )
            if status not in {"analyzing", "awaiting_context"}:
                raise InvalidInputError("source revisions can only be checked for an active run")
            targets = self._source_targets_for_run(
                connection, run_id, source_revision_ids=source_revision_ids
            )
        if {target.source_revision_id for target in targets} != set(source_revision_ids):
            raise InvalidInputError("a source revision is outside the frozen PreparationRun")
        self._require_targets_within_workspace(workspace_root, targets)
        checks = tuple(_check_source(target) for target in targets)
        with self._database.write_transaction() as connection:
            status, _ = self._require_active_run_and_session(
                connection, run_id, authorization_receipt_id
            )
            if status not in {"analyzing", "awaiting_context"}:
                raise InvalidInputError("source revisions can only be checked for an active run")
            self._insert_source_checks(connection, run_id, phase, checks)
            if any(check.status == "mismatch" for check in checks):
                transition_at = _now()
                connection.execute(
                    """
                    UPDATE preparation_runs
                    SET status = 'refresh_required', status_reason = 'source_revision_mismatch',
                        last_transition_at = ?, finished_at = ?
                    WHERE preparation_run_id = ?
                    """,
                    (transition_at, transition_at, run_id),
                )
                status = "refresh_required"
        return {
            "status": "ok",
            "preparation_run_id": run_id,
            "run_status": status,
            "phase": phase,
            "checks": [check.as_json(workspace_root) for check in checks],
        }

    @staticmethod
    def _scan_context(
        connection: sqlite3.Connection,
        workspace_path: Path,
        scan_run_id: str,
    ) -> tuple[str, tuple[_ProjectBinding, ...]]:
        row = connection.execute(
            """
            SELECT sr.workspace_id, sr.status, w.canonical_root,
                   sro.scan_run_id AS overview_scan_run_id
            FROM scan_runs AS sr
            JOIN workspaces AS w ON w.workspace_id = sr.workspace_id
            LEFT JOIN scan_run_overviews AS sro ON sro.scan_run_id = sr.scan_run_id
            WHERE sr.scan_run_id = ?
            """,
            (scan_run_id,),
        ).fetchone()
        if row is None:
            raise InvalidInputError("scan_run_id does not exist")
        status = str(row["status"])
        if status not in {"completed", "partial"} and not (
            status == "failed" and row["overview_scan_run_id"] is not None
        ):
            raise InvalidInputError(
                "PreparationRun requires a reusable completed, partial, or normally failed ScanRun"
            )
        stored_workspace = Path(str(row["canonical_root"])).resolve(strict=False)
        if stored_workspace != workspace_path:
            raise InvalidInputError("scan_run_id belongs to another workspace")
        project_rows = connection.execute(
            """
            SELECT srp.project_id, p.display_name, srp.snapshot_disposition,
                   srp.project_snapshot_id
            FROM scan_run_projects AS srp
            JOIN projects AS p ON p.project_id = srp.project_id
            WHERE srp.scan_run_id = ?
            ORDER BY srp.project_id
            """,
            (scan_run_id,),
        ).fetchall()
        projects = tuple(
            _ProjectBinding(
                str(project["project_id"]),
                str(project["display_name"]),
                str(project["snapshot_disposition"]),
                (
                    str(project["project_snapshot_id"])
                    if project["project_snapshot_id"] is not None
                    else None
                ),
            )
            for project in project_rows
        )
        return str(row["workspace_id"]), projects

    @staticmethod
    def _require_targets_within_workspace(
        workspace_root: Path, targets: tuple[_SourceTarget, ...]
    ) -> None:
        for target in targets:
            _workspace_source_locator(
                workspace_root,
                target.worktree_root,
                target.relative_path,
            )

    @staticmethod
    def _source_targets_for_scan(
        connection: sqlite3.Connection, scan_run_id: str
    ) -> tuple[_SourceTarget, ...]:
        rows = connection.execute(
            """
            SELECT DISTINCT sr.source_revision_id, sr.content_sha256,
                   wt.worktree_id, wt.canonical_root, sa.relative_path
            FROM scan_run_projects AS srp
            JOIN project_snapshot_source_revisions AS pssr
              ON pssr.project_snapshot_id = srp.project_snapshot_id
            JOIN source_revisions AS sr ON sr.source_revision_id = pssr.source_revision_id
            JOIN source_artifacts AS sa ON sa.artifact_id = sr.artifact_id
            JOIN worktrees AS wt ON wt.worktree_id = sa.worktree_id
            WHERE srp.scan_run_id = ?
              AND srp.snapshot_disposition IN ('fresh', 'carried_forward')
            ORDER BY sr.source_revision_id
            """,
            (scan_run_id,),
        ).fetchall()
        return tuple(
            _SourceTarget(
                str(row["source_revision_id"]),
                str(row["content_sha256"]),
                str(row["worktree_id"]),
                Path(str(row["canonical_root"])),
                str(row["relative_path"]),
            )
            for row in rows
        )

    @staticmethod
    def _source_targets_for_run(
        connection: sqlite3.Connection,
        preparation_run_id: str,
        *,
        source_revision_ids: tuple[str, ...] | None = None,
    ) -> tuple[_SourceTarget, ...]:
        filters = ""
        parameters: list[object] = [preparation_run_id]
        if source_revision_ids is not None:
            placeholders = ",".join("?" for _ in source_revision_ids)
            filters = f" AND sr.source_revision_id IN ({placeholders})"
            parameters.extend(source_revision_ids)
        rows = connection.execute(
            f"""
            SELECT DISTINCT sr.source_revision_id, sr.content_sha256,
                   wt.worktree_id, wt.canonical_root, sa.relative_path
            FROM preparation_run_projects AS prp
            JOIN project_snapshot_source_revisions AS pssr
              ON pssr.project_snapshot_id = prp.project_snapshot_id
            JOIN source_revisions AS sr ON sr.source_revision_id = pssr.source_revision_id
            JOIN source_artifacts AS sa ON sa.artifact_id = sr.artifact_id
            JOIN worktrees AS wt ON wt.worktree_id = sa.worktree_id
            WHERE prp.preparation_run_id = ?
              AND prp.snapshot_disposition IN ('fresh', 'carried_forward')
              {filters}
            ORDER BY sr.source_revision_id
            """,
            tuple(parameters),
        ).fetchall()
        return tuple(
            _SourceTarget(
                str(row["source_revision_id"]),
                str(row["content_sha256"]),
                str(row["worktree_id"]),
                Path(str(row["canonical_root"])),
                str(row["relative_path"]),
            )
            for row in rows
        )

    @staticmethod
    def _require_source_receipt(
        connection: sqlite3.Connection,
        authorization_receipt_id: str,
        workspace_path: Path,
    ) -> None:
        row = connection.execute(
            """
            SELECT receipt_kind, scope_descriptor
            FROM authorization_receipts WHERE authorization_receipt_id = ?
            """,
            (authorization_receipt_id,),
        ).fetchone()
        if row is None or str(row["receipt_kind"]) != "source_analysis":
            raise CapabilityError("source authorization receipt is not valid for preparation")
        scope = _stored_json(row["scope_descriptor"], "authorization scope")
        if not isinstance(scope, dict) or (
            scope.get("workspace_path") != str(workspace_path)
            or scope.get("allowed_categories") != ["source_analysis"]
        ):
            raise CapabilityError("source authorization receipt does not match the workspace")

    @staticmethod
    def _require_same_session_binding(
        connection: sqlite3.Connection,
        original_receipt_id: str,
        current_receipt_id: str,
    ) -> None:
        rows = connection.execute(
            """
            SELECT authorization_receipt_id, session_binding_digest, receipt_kind,
                   scope_descriptor, notice_version
            FROM authorization_receipts
            WHERE authorization_receipt_id IN (?, ?)
            """,
            (original_receipt_id, current_receipt_id),
        ).fetchall()
        by_id = {str(row["authorization_receipt_id"]): row for row in rows}
        original = by_id.get(original_receipt_id)
        current = by_id.get(current_receipt_id)
        if (
            original is None
            or current is None
            or str(original["receipt_kind"]) != "source_analysis"
            or str(current["receipt_kind"]) != "source_analysis"
            or not hmac.compare_digest(
                bytes(original["session_binding_digest"]),
                bytes(current["session_binding_digest"]),
            )
            or not _constant_time_text_equal(
                str(original["scope_descriptor"]), str(current["scope_descriptor"])
            )
            or not _constant_time_text_equal(
                str(original["notice_version"]), str(current["notice_version"])
            )
        ):
            raise CapabilityError("PreparationRun is not bound to the current session")

    @classmethod
    def _require_active_run_and_session(
        cls,
        connection: sqlite3.Connection,
        preparation_run_id: str,
        authorization_receipt_id: str,
    ) -> tuple[str, Path]:
        row = connection.execute(
            """
            SELECT pr.status, pr.authorization_receipt_id, w.canonical_root
            FROM preparation_runs AS pr
            JOIN workspaces AS w ON w.workspace_id = pr.workspace_id
            WHERE pr.preparation_run_id = ?
            """,
            (preparation_run_id,),
        ).fetchone()
        if row is None:
            raise InvalidInputError("preparation_run_id does not exist")
        cls._require_same_session_binding(
            connection,
            str(row["authorization_receipt_id"]),
            authorization_receipt_id,
        )
        return str(row["status"]), Path(str(row["canonical_root"]))

    def _persist_start(
        self,
        *,
        canonical_workspace: Path,
        workspace_id: str,
        authorization_receipt_id: str,
        request: PreparationRequestDraft,
        request_sha256: str,
        expected_projects: tuple[_ProjectBinding, ...],
        checks: tuple[SourceCheckResult, ...],
    ) -> str:
        job_input_id = _new_id()
        role_lens_id = _new_id()
        preparation_run_id = _new_id()
        timestamp = _now()
        with self._database.write_transaction() as connection:
            existing = connection.execute(
                """
                SELECT preparation_run_id, request_sha256, authorization_receipt_id
                FROM preparation_runs WHERE request_id = ?
                """,
                (request.request_id,),
            ).fetchone()
            if existing is not None:
                if not hmac.compare_digest(str(existing["request_sha256"]), request_sha256):
                    raise InvalidInputError("request_id is already bound to different inputs")
                self._require_same_session_binding(
                    connection,
                    str(existing["authorization_receipt_id"]),
                    authorization_receipt_id,
                )
                return str(existing["preparation_run_id"])
            current_workspace_id, current_projects = self._scan_context(
                connection, canonical_workspace, request.scan_run_id
            )
            if current_workspace_id != workspace_id or current_projects != expected_projects:
                raise InvalidInputError("the frozen ScanRun changed while preparation was starting")
            self._require_source_receipt(connection, authorization_receipt_id, canonical_workspace)
            job = request.job_input
            connection.execute(
                """
                INSERT INTO job_inputs(
                    job_input_id, role_name, jd_input_kind, jd_text, jd_source_path,
                    jd_content_sha256, inferred_level, level_override, primary_language, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'zh-CN', ?)
                """,
                (
                    job_input_id,
                    job.role_name,
                    job.jd_input_kind,
                    job.jd_text,
                    job.jd_source_path,
                    job.jd_content_sha256,
                    job.inferred_level,
                    job.level_override,
                    timestamp,
                ),
            )
            lens = request.role_lens
            connection.execute(
                """
                INSERT INTO role_lenses(
                    role_lens_id, job_input_id, contract_version, dimensions,
                    evidence_requirements, ranking_rules, output_sections, question_strategy,
                    gap_rules, assumptions, generator_id, prompt_contract_version,
                    lens_sha256, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    role_lens_id,
                    job_input_id,
                    lens.contract_version,
                    _canonical_json(
                        [dimension.value for dimension in lens.dimensions],
                        "dimensions",
                        maximum_bytes=MAX_PRIVATE_PAYLOAD_BYTES,
                    ),
                    _canonical_json(lens.evidence_requirements, "evidence_requirements"),
                    _canonical_json(lens.ranking_rules, "ranking_rules"),
                    _canonical_json(lens.output_sections, "output_sections"),
                    _canonical_json(lens.question_strategy, "question_strategy"),
                    _canonical_json(lens.gap_rules, "gap_rules"),
                    _canonical_json(list(lens.assumptions), "assumptions"),
                    lens.generator_id,
                    lens.prompt_contract_version,
                    lens.sha256,
                    timestamp,
                ),
            )
            eligible_count = sum(project.eligible for project in current_projects)
            has_mismatch = any(check.status == "mismatch" for check in checks)
            if eligible_count == 0:
                run_status = "failed"
                reason = "no_eligible_projects"
            elif has_mismatch:
                run_status = "refresh_required"
                reason = "source_revision_mismatch"
            else:
                run_status = "analyzing"
                reason = None
            finished_at = timestamp if run_status in {"failed", "refresh_required"} else None
            connection.execute(
                """
                INSERT INTO preparation_runs(
                    preparation_run_id, request_id, request_sha256, workspace_id, scan_run_id,
                    role_lens_id, authorization_receipt_id, config_revision, requested_exports,
                    evidence_limit_per_project, status, status_reason, started_at,
                    last_transition_at, finished_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    preparation_run_id,
                    request.request_id,
                    request_sha256,
                    workspace_id,
                    request.scan_run_id,
                    role_lens_id,
                    authorization_receipt_id,
                    request.config_revision,
                    _canonical_json(list(request.requested_exports), "requested_exports"),
                    request.evidence_limit_per_project,
                    run_status,
                    reason,
                    timestamp,
                    timestamp,
                    finished_at,
                ),
            )
            connection.executemany(
                """
                INSERT INTO preparation_run_projects(
                    preparation_run_id, project_id, project_snapshot_id, snapshot_disposition
                ) VALUES (?, ?, ?, ?)
                """,
                (
                    (
                        preparation_run_id,
                        project.project_id,
                        project.project_snapshot_id,
                        project.snapshot_disposition,
                    )
                    for project in current_projects
                ),
            )
            self._insert_source_checks(connection, preparation_run_id, "preflight", checks)
        return preparation_run_id

    @staticmethod
    def _insert_source_checks(
        connection: sqlite3.Connection,
        preparation_run_id: str,
        phase: str,
        checks: tuple[SourceCheckResult, ...],
    ) -> None:
        for check in checks:
            source_check_id = _new_id()
            connection.execute(
                """
                INSERT INTO preparation_source_checks(
                    source_check_id, preparation_run_id, source_revision_id, phase,
                    expected_sha256, observed_at, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    source_check_id,
                    preparation_run_id,
                    check.source_revision_id,
                    phase,
                    check.expected_sha256,
                    check.observed_at,
                    check.status,
                ),
            )
            if check.mismatch_kind is not None:
                connection.execute(
                    """
                    INSERT INTO preparation_source_mismatches(
                        source_mismatch_id, source_check_id, mismatch_kind,
                        observed_sha256, detected_at
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        _new_id(),
                        source_check_id,
                        check.mismatch_kind,
                        check.observed_sha256,
                        check.observed_at,
                    ),
                )

    def _result(self, preparation_run_id: str) -> dict[str, object]:
        with self._database.read_connection() as connection:
            row = connection.execute(
                """
                SELECT pr.preparation_run_id, pr.request_id, pr.scan_run_id, pr.role_lens_id,
                       pr.status, pr.status_reason, pr.config_revision, pr.requested_exports,
                       pr.evidence_limit_per_project, pr.started_at, pr.last_transition_at,
                       pr.finished_at, ji.role_name, ji.jd_input_kind, ji.jd_source_path,
                       ji.jd_content_sha256, ji.inferred_level, ji.level_override,
                       ji.primary_language, rl.contract_version, rl.dimensions,
                       rl.evidence_requirements, rl.ranking_rules, rl.output_sections,
                       rl.question_strategy, rl.gap_rules, rl.assumptions, rl.generator_id,
                       rl.prompt_contract_version, rl.lens_sha256
                FROM preparation_runs AS pr
                JOIN role_lenses AS rl ON rl.role_lens_id = pr.role_lens_id
                JOIN job_inputs AS ji ON ji.job_input_id = rl.job_input_id
                WHERE pr.preparation_run_id = ?
                """,
                (preparation_run_id,),
            ).fetchone()
            if row is None:
                raise InvalidInputError("preparation_run_id does not exist")
            dimensions_value = _stored_json(row["dimensions"], "RoleLens dimensions")
            dimensions = _dimensions(dimensions_value)
            role_lens: dict[str, object] = {
                "role_lens_id": str(row["role_lens_id"]),
                "contract_version": str(row["contract_version"]),
                "lens_sha256": str(row["lens_sha256"]),
                "dimensions": dimensions_value,
                "evidence_requirements": _stored_json(
                    row["evidence_requirements"], "RoleLens evidence requirements"
                ),
                "ranking_rules": _stored_json(row["ranking_rules"], "RoleLens ranking rules"),
                "output_sections": _stored_json(row["output_sections"], "RoleLens output sections"),
                "question_strategy": _stored_json(
                    row["question_strategy"], "RoleLens question strategy"
                ),
                "gap_rules": _stored_json(row["gap_rules"], "RoleLens gap rules"),
                "assumptions": _stored_json(row["assumptions"], "RoleLens assumptions"),
                "generator_id": str(row["generator_id"]),
                "prompt_contract_version": str(row["prompt_contract_version"]),
            }
            status = str(row["status"])
            run = {
                "preparation_run_id": preparation_run_id,
                "request_id": str(row["request_id"]),
                "scan_run_id": str(row["scan_run_id"]),
                "role_lens_id": str(row["role_lens_id"]),
                "status": status,
                "status_reason": row["status_reason"],
                "config_revision": str(row["config_revision"]),
                "requested_exports": _stored_json(row["requested_exports"], "requested exports"),
                "started_at": str(row["started_at"]),
                "last_transition_at": str(row["last_transition_at"]),
                "finished_at": row["finished_at"],
            }
            job_input = {
                "role_name": str(row["role_name"]),
                "jd_input_kind": str(row["jd_input_kind"]),
                "jd_source_path": row["jd_source_path"],
                "jd_content_sha256": row["jd_content_sha256"],
                "inferred_level": row["inferred_level"],
                "level_override": row["level_override"],
                "applied_level": row["level_override"] or row["inferred_level"],
                "primary_language": str(row["primary_language"]),
            }
            mismatch_rows = connection.execute(
                """
                SELECT sc.source_revision_id, sc.expected_sha256, sc.observed_at,
                       sm.mismatch_kind, sm.observed_sha256, sa.worktree_id,
                       sa.relative_path
                FROM preparation_source_checks AS sc
                JOIN preparation_source_mismatches AS sm
                  ON sm.source_check_id = sc.source_check_id
                JOIN source_revisions AS sr
                  ON sr.source_revision_id = sc.source_revision_id
                JOIN source_artifacts AS sa ON sa.artifact_id = sr.artifact_id
                WHERE sc.preparation_run_id = ?
                ORDER BY sc.observed_at, sc.source_revision_id
                """,
                (preparation_run_id,),
            ).fetchall()
            mismatches = [
                {
                    "source_revision_id": str(mismatch["source_revision_id"]),
                    "expected_sha256": str(mismatch["expected_sha256"]),
                    "observed_sha256": mismatch["observed_sha256"],
                    "mismatch_kind": str(mismatch["mismatch_kind"]),
                    "worktree_id": str(mismatch["worktree_id"]),
                    "relative_path": str(mismatch["relative_path"]),
                    "observed_at": str(mismatch["observed_at"]),
                }
                for mismatch in mismatch_rows
            ]
            evidence_bundle = (
                self._evidence_bundle(
                    connection,
                    preparation_run_id,
                    str(row["scan_run_id"]),
                    str(row["role_lens_id"]),
                    dimensions,
                    int(row["evidence_limit_per_project"]),
                )
                if status in {"analyzing", "awaiting_context"}
                else None
            )
        return {
            "status": "ok",
            "preparation_run": run,
            "job_input": job_input,
            "role_lens": role_lens,
            "source_mismatches": mismatches,
            "evidence_bundle": evidence_bundle,
            "storage": self._storage_usage(),
        }

    @staticmethod
    def _evidence_bundle(
        connection: sqlite3.Connection,
        preparation_run_id: str,
        scan_run_id: str,
        role_lens_id: str,
        dimensions: tuple[RoleDimension, ...],
        requested_limit: int,
    ) -> dict[str, object]:
        workspace_row = connection.execute(
            """
            SELECT w.canonical_root
            FROM preparation_runs AS pr
            JOIN workspaces AS w ON w.workspace_id = pr.workspace_id
            WHERE pr.preparation_run_id = ?
            """,
            (preparation_run_id,),
        ).fetchone()
        if workspace_row is None:
            raise InvalidInputError("PreparationRun workspace does not exist")
        workspace_root = Path(str(workspace_row["canonical_root"]))
        project_rows = connection.execute(
            """
            SELECT prp.project_id, p.display_name, prp.snapshot_disposition,
                   prp.project_snapshot_id
            FROM preparation_run_projects AS prp
            JOIN projects AS p ON p.project_id = prp.project_id
            WHERE prp.preparation_run_id = ?
            ORDER BY prp.project_id
            """,
            (preparation_run_id,),
        ).fetchall()
        coverage = [
            {
                "project_id": str(project["project_id"]),
                "display_name": str(project["display_name"]),
                "snapshot_disposition": str(project["snapshot_disposition"]),
                "project_snapshot_id": project["project_snapshot_id"],
                "eligible": str(project["snapshot_disposition"]) in {"fresh", "carried_forward"},
            }
            for project in project_rows
        ]
        eligible_projects = [
            project
            for project in project_rows
            if str(project["snapshot_disposition"]) in {"fresh", "carried_forward"}
        ]
        effective_limit = requested_limit
        if eligible_projects:
            effective_limit = min(
                requested_limit,
                max(1, MAX_BUNDLE_EVIDENCE // len(eligible_projects)),
            )
        dimension_by_kind: dict[str, list[RoleDimension]] = {}
        for dimension in dimensions:
            for evidence_kind in dimension.required_evidence_kinds:
                dimension_by_kind.setdefault(evidence_kind, []).append(dimension)
        kind_weights = {
            evidence_kind: max(dimension.weight_bps for dimension in matching_dimensions)
            for evidence_kind, matching_dimensions in dimension_by_kind.items()
        }
        evidence_items: list[dict[str, object]] = []
        for project in eligible_projects:
            remaining = MAX_BUNDLE_EVIDENCE - len(evidence_items)
            if remaining <= 0:
                break
            project_limit = min(effective_limit, remaining)
            priority_expression = "0 DESC"
            query_parameters: list[object] = [
                scan_run_id,
                preparation_run_id,
                str(project["project_id"]),
            ]
            if kind_weights:
                priority_expression = (
                    "CASE e.evidence_kind "
                    + " ".join("WHEN ? THEN ?" for _ in kind_weights)
                    + " ELSE 0 END DESC"
                )
                for evidence_kind, weight in sorted(kind_weights.items()):
                    query_parameters.extend((evidence_kind, weight))
            query_parameters.append(project_limit)
            evidence_rows = connection.execute(
                f"""
                SELECT e.evidence_id, e.project_id, e.project_snapshot_id, e.module_id,
                       e.source_revision_id, e.evidence_kind, e.locator, e.summary,
                       e.commit_state, e.content_equivalence_key, sa.worktree_id,
                       wt.canonical_root AS worktree_root, sa.relative_path,
                       sr.content_sha256, sr.analysis_fingerprint,
                       COALESCE(ev.validity, 'current') AS validity
                FROM preparation_run_projects AS prp
                JOIN project_snapshot_evidence AS pse
                  ON pse.project_snapshot_id = prp.project_snapshot_id
                JOIN evidence AS e ON e.evidence_id = pse.evidence_id
                LEFT JOIN source_revisions AS sr
                  ON sr.source_revision_id = e.source_revision_id
                LEFT JOIN source_artifacts AS sa ON sa.artifact_id = sr.artifact_id
                LEFT JOIN worktrees AS wt ON wt.worktree_id = sa.worktree_id
                LEFT JOIN evidence_validities AS ev
                  ON ev.scan_run_id = ? AND ev.evidence_id = e.evidence_id
                WHERE prp.preparation_run_id = ? AND prp.project_id = ?
                ORDER BY {priority_expression}, e.evidence_kind, e.evidence_id
                LIMIT ?
                """,
                tuple(query_parameters),
            ).fetchall()
            for evidence in evidence_rows:
                evidence_kind = str(evidence["evidence_kind"])
                worktree_relative_root: str | None = None
                workspace_relative_path: str | None = None
                if evidence["source_revision_id"] is not None:
                    if (
                        evidence["worktree_root"] is None
                        or evidence["relative_path"] is None
                        or evidence["worktree_id"] is None
                    ):
                        raise CapabilityError("source evidence has an incomplete worktree binding")
                    worktree_relative_root, workspace_relative_path = _workspace_source_locator(
                        workspace_root,
                        Path(str(evidence["worktree_root"])),
                        str(evidence["relative_path"]),
                    )
                matching_dimensions = sorted(
                    dimension_by_kind.get(evidence_kind, []),
                    key=lambda dimension: (-dimension.weight_bps, dimension.key),
                )
                evidence_items.append(
                    {
                        "evidence_id": str(evidence["evidence_id"]),
                        "project_id": str(evidence["project_id"]),
                        "project_snapshot_id": evidence["project_snapshot_id"],
                        "worktree_id": evidence["worktree_id"],
                        "worktree_relative_root": worktree_relative_root,
                        "module_id": evidence["module_id"],
                        "source_revision_id": evidence["source_revision_id"],
                        "relative_path": evidence["relative_path"],
                        "workspace_relative_path": workspace_relative_path,
                        "content_sha256": evidence["content_sha256"],
                        "analysis_fingerprint": evidence["analysis_fingerprint"],
                        "content_equivalence_key": evidence["content_equivalence_key"],
                        "evidence_kind": evidence_kind,
                        "locator": _stored_json(evidence["locator"], "Evidence locator"),
                        "summary": str(evidence["summary"]),
                        "commit_state": str(evidence["commit_state"]),
                        "validity": str(evidence["validity"]),
                        "priority_dimension_keys": [
                            dimension.key for dimension in matching_dimensions
                        ],
                        "priority_weight_bps": (
                            matching_dimensions[0].weight_bps if matching_dimensions else 0
                        ),
                    }
                )
        suggestions: list[dict[str, object]] = []
        seen_source_revisions: set[str] = set()
        for item in evidence_items:
            source_revision = item["source_revision_id"]
            if not isinstance(source_revision, str) or source_revision in seen_source_revisions:
                continue
            seen_source_revisions.add(source_revision)
            suggestions.append(
                {
                    "source_revision_id": source_revision,
                    "project_id": item["project_id"],
                    "worktree_id": item["worktree_id"],
                    "worktree_relative_root": item["worktree_relative_root"],
                    "module_id": item["module_id"],
                    "relative_path": item["relative_path"],
                    "workspace_relative_path": item["workspace_relative_path"],
                    "content_sha256": item["content_sha256"],
                    "priority_dimension_keys": item["priority_dimension_keys"],
                }
            )
            if len(suggestions) >= MAX_SOURCE_REVISION_BATCH:
                break
        issue_counts = [
            {
                "severity": str(issue["severity"]),
                "kind": str(issue["kind"]),
                "count": int(issue["count"]),
            }
            for issue in connection.execute(
                """
                SELECT severity, kind, COUNT(*) AS count
                FROM scan_issues WHERE scan_run_id = ?
                GROUP BY severity, kind ORDER BY severity, kind
                """,
                (scan_run_id,),
            ).fetchall()
        ]
        issues = [
            {
                "issue_id": str(issue["issue_id"]),
                "project_id": issue["project_id"],
                "artifact_id": issue["artifact_id"],
                "kind": str(issue["kind"]),
                "severity": str(issue["severity"]),
                "relative_path": issue["relative_path"],
                "message": str(issue["message"]),
                "remediation": str(issue["remediation"]),
            }
            for issue in connection.execute(
                """
                SELECT issue_id, project_id, artifact_id, kind, severity, relative_path,
                       message, remediation
                FROM scan_issues WHERE scan_run_id = ?
                ORDER BY CASE severity
                    WHEN 'error' THEN 0
                    WHEN 'warning' THEN 1
                    ELSE 2
                END, issue_id
                LIMIT ?
                """,
                (scan_run_id, MAX_BUNDLE_ISSUES),
            ).fetchall()
        ]
        total_issue_count = sum(cast(int, item["count"]) for item in issue_counts)
        total_evidence_row = connection.execute(
            """
            SELECT COUNT(*) AS count
            FROM preparation_run_projects AS prp
            JOIN project_snapshot_evidence AS pse
              ON pse.project_snapshot_id = prp.project_snapshot_id
            WHERE prp.preparation_run_id = ?
              AND prp.snapshot_disposition IN ('fresh', 'carried_forward')
            """,
            (preparation_run_id,),
        ).fetchone()
        assert total_evidence_row is not None
        total_evidence_count = int(total_evidence_row["count"])
        return {
            "contract_version": EVIDENCE_BUNDLE_CONTRACT_VERSION,
            "preparation_run_id": preparation_run_id,
            "scan_run_id": scan_run_id,
            "role_lens_id": role_lens_id,
            "coverage": coverage,
            "coverage_counts": {
                disposition: sum(item["snapshot_disposition"] == disposition for item in coverage)
                for disposition in (
                    "fresh",
                    "carried_forward",
                    "failed_no_baseline",
                    "excluded",
                )
            },
            "evidence_items": evidence_items,
            "deep_read_suggestions": suggestions,
            "scan_issue_summary": issue_counts,
            "scan_issues": issues,
            "limits": {
                "requested_evidence_per_project": requested_limit,
                "effective_evidence_per_project": effective_limit,
                "maximum_total_evidence": MAX_BUNDLE_EVIDENCE,
                "available_evidence": total_evidence_count,
                "evidence_truncated": total_evidence_count > len(evidence_items),
                "issues_truncated": total_issue_count > len(issues),
            },
        }

    def _storage_usage(self) -> dict[str, object]:
        paths = self._database.paths
        with self._database.read_connection() as connection:
            snapshot_row = connection.execute(
                "SELECT COUNT(*) AS count FROM project_snapshots"
            ).fetchone()
            preparation_row = connection.execute(
                "SELECT COUNT(*) AS count FROM preparation_runs"
            ).fetchone()
            assert snapshot_row is not None
            assert preparation_row is not None
            snapshot_count = int(snapshot_row["count"])
            preparation_count = int(preparation_row["count"])
        return {
            "data_dir": str(paths.root),
            "usage_bytes": {
                "sqlite": _directory_usage(paths.database_file),
                "artifacts": _directory_usage(paths.artifacts_dir),
                "exports": _directory_usage(paths.exports_dir),
                "drafts": _directory_usage(paths.drafts_dir),
            },
            "snapshot_count": snapshot_count,
            "preparation_run_count": preparation_count,
        }

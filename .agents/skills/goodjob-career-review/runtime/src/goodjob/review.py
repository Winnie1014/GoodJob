"""Stable review-target lineage and structured interview-review persistence."""

from __future__ import annotations

import hashlib
import hmac
import sqlite3
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import cast

from goodjob.db import Database
from goodjob.errors import InvalidInputError
from goodjob.preparation import (
    MAX_PRIVATE_PAYLOAD_BYTES,
    JsonObject,
    JsonValue,
    PreparationService,
    _canonical_json,
    _new_id,
    _now,
    _object,
    _reject_unknown_fields,
    _stored_json,
    _text,
)

INTERVIEW_INPUT_VERSION = "interview-input-v1"
MOCK_REVIEW_TARGETS_VERSION = "mock-review-targets-v1"
REVIEW_PROJECTION_VERSION = "review-projection-v1"
REVIEW_SUBJECT_PROJECTION_VERSION = "review-subject-projection-v1"
CLAIM_REVIEW_CONTRACT_VERSION = "claim-review-v1"
QUESTION_CONTRACT_VERSION = "interview-question-v1"
MASTERY_LEVELS = frozenset({"unfamiliar", "developing", "solid", "mastered"})
MAX_WEAK_POINTS = 20

type JSONObject = dict[str, object]


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _stable_id(kind: str, *parts: str) -> str:
    digest = _sha256_text("\0".join((kind, *parts)))
    return f"{kind}-{digest[:24]}"


def _question_id(subject_id: str) -> str:
    return _stable_id("question", subject_id)


def _json_list(raw: object, field_name: str) -> list[JsonValue]:
    value = _stored_json(raw, field_name)
    if not isinstance(value, list):
        raise InvalidInputError(f"stored {field_name} must be a JSON list")
    return value


def _json_object(raw: object, field_name: str) -> JsonObject:
    value = _stored_json(raw, field_name)
    if not isinstance(value, dict):
        raise InvalidInputError(f"stored {field_name} must be a JSON object")
    return value


def _weak_points(value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or len(value) > MAX_WEAK_POINTS:
        raise InvalidInputError("review.weak_points must be a bounded JSON string list")
    points = tuple(
        _text(item, f"review.weak_points[{index}]", maximum=1000)
        for index, item in enumerate(value)
    )
    if len(set(points)) != len(points):
        raise InvalidInputError("review.weak_points must not contain duplicates")
    return points


def _review_date(value: object) -> str | None:
    if value is None:
        return None
    raw = _text(value, "review.next_review_at", maximum=10)
    try:
        parsed = date.fromisoformat(raw)
    except ValueError as exc:
        raise InvalidInputError("review.next_review_at must be a valid ISO date") from exc
    if parsed.isoformat() != raw:
        raise InvalidInputError("review.next_review_at must use YYYY-MM-DD")
    return raw


@dataclass(frozen=True)
class MockReviewTargetRequest:
    preparation_run_id: str

    @classmethod
    def from_value(cls, value: object) -> MockReviewTargetRequest:
        request = _object(value, "interview_input")
        _reject_unknown_fields(
            request,
            frozenset({"contract_version", "mode", "action", "preparation_run_id"}),
            "interview_input",
        )
        _canonical_json(request, "interview_input", maximum_bytes=MAX_PRIVATE_PAYLOAD_BYTES)
        if _text(request.get("contract_version"), "contract_version", maximum=80) != (
            INTERVIEW_INPUT_VERSION
        ):
            raise InvalidInputError("unsupported InterviewInput version")
        if _text(request.get("mode"), "mode", maximum=40) != "mock_review":
            raise InvalidInputError("mock review requires mode=mock_review")
        if _text(request.get("action"), "action", maximum=40) != "list_targets":
            raise InvalidInputError("mock review target request requires action=list_targets")
        return cls(_text(request.get("preparation_run_id"), "preparation_run_id", maximum=200))


@dataclass(frozen=True)
class InterviewReviewRequest:
    request_id: str
    preparation_run_id: str
    review_target_binding_id: str
    question_id: str
    summary: str
    mastery_level: str
    weak_points: tuple[str, ...]
    next_review_at: str | None
    request_sha256: str

    @classmethod
    def from_value(cls, value: object) -> InterviewReviewRequest:
        request = _object(value, "interview_input")
        _reject_unknown_fields(
            request,
            frozenset(
                {
                    "contract_version",
                    "request_id",
                    "mode",
                    "action",
                    "preparation_run_id",
                    "review_target_binding_id",
                    "question_id",
                    "review",
                }
            ),
            "interview_input",
        )
        canonical = _canonical_json(
            request,
            "interview_input",
            maximum_bytes=MAX_PRIVATE_PAYLOAD_BYTES,
        )
        if _text(request.get("contract_version"), "contract_version", maximum=80) != (
            INTERVIEW_INPUT_VERSION
        ):
            raise InvalidInputError("unsupported InterviewInput version")
        if _text(request.get("mode"), "mode", maximum=40) != "mock_review":
            raise InvalidInputError("mock review requires mode=mock_review")
        if _text(request.get("action"), "action", maximum=40) != "record_review":
            raise InvalidInputError("mock review write requires action=record_review")
        review = _object(request.get("review"), "review")
        _reject_unknown_fields(
            review,
            frozenset({"summary", "mastery_level", "weak_points", "next_review_at"}),
            "review",
        )
        mastery_level = _text(review.get("mastery_level"), "review.mastery_level", maximum=40)
        if mastery_level not in MASTERY_LEVELS:
            raise InvalidInputError(
                "review.mastery_level must be unfamiliar, developing, solid, or mastered"
            )
        return cls(
            _text(request.get("request_id"), "request_id", maximum=200),
            _text(request.get("preparation_run_id"), "preparation_run_id", maximum=200),
            _text(
                request.get("review_target_binding_id"),
                "review_target_binding_id",
                maximum=200,
            ),
            _text(request.get("question_id"), "question_id", maximum=200),
            _text(review.get("summary"), "review.summary", maximum=4000),
            mastery_level,
            _weak_points(review.get("weak_points", [])),
            _review_date(review.get("next_review_at")),
            _sha256_text(canonical),
        )


class ReviewService:
    """Bind stable review subjects and append bounded review observations."""

    def __init__(self, database: Database) -> None:
        self._database = database

    @staticmethod
    def ensure_bindings_in_connection(
        connection: sqlite3.Connection,
        preparation_run_id: str,
        *,
        bound_at: str | None = None,
    ) -> None:
        run = connection.execute(
            """
            SELECT review_lineage_sequence
            FROM preparation_runs WHERE preparation_run_id = ?
            """,
            (preparation_run_id,),
        ).fetchone()
        if run is None:
            raise InvalidInputError("preparation_run_id does not exist")
        if int(run["review_lineage_sequence"]) <= 0:
            raise InvalidInputError("PreparationRun has no review lineage sequence")
        timestamp = bound_at or _now()
        claim_rows = connection.execute(
            """
            SELECT c.claim_id, cr.review_semantic_projection,
                   cr.review_semantic_sha256
            FROM preparation_claims AS pc
            JOIN claim_revisions AS cr
              ON cr.claim_revision_id = pc.claim_revision_id
            JOIN claims AS c ON c.claim_id = cr.claim_id
            WHERE pc.preparation_run_id = ?
            ORDER BY pc.rank, c.claim_id
            """,
            (preparation_run_id,),
        ).fetchall()
        for row in claim_rows:
            claim_id = str(row["claim_id"])
            semantic = _json_object(
                row["review_semantic_projection"], "Claim review semantic projection"
            )
            equivalence_status = semantic.get("equivalence_status")
            if equivalence_status not in {"verified", "unverified"}:
                equivalence_status = "unverified"
            atom: JsonObject = {
                "claim_id": claim_id,
                "review_semantic_sha256": str(row["review_semantic_sha256"]),
                "equivalence_status": cast(JsonValue, equivalence_status),
            }
            if equivalence_status == "unverified":
                fallback = semantic.get("fallback_semantic_sha256")
                if not isinstance(fallback, str) or not fallback:
                    fallback = _sha256_text(f"legacy-unverified\0{row['review_semantic_sha256']}")
                atom["fallback_semantic_sha256"] = fallback
            ReviewService._ensure_binding(
                connection,
                preparation_run_id=preparation_run_id,
                target_kind="claim",
                stable_subject_id=claim_id,
                topic_contract_version=CLAIM_REVIEW_CONTRACT_VERSION,
                projection={
                    "contract_version": REVIEW_SUBJECT_PROJECTION_VERSION,
                    "target_kind": "claim",
                    "topic_contract_version": CLAIM_REVIEW_CONTRACT_VERSION,
                    "question_contract_version": QUESTION_CONTRACT_VERSION,
                    "claim_atoms": [atom],
                    "gap_atoms": [],
                },
                bound_at=timestamp,
            )
        gap_rows = connection.execute(
            """
            SELECT gap_key, dimension, severity, resolution_kind, status,
                   gap_contract_version
            FROM knowledge_gaps
            WHERE preparation_run_id = ?
            ORDER BY scope_kind, scope_id, dimension, gap_key
            """,
            (preparation_run_id,),
        ).fetchall()
        for row in gap_rows:
            gap_key = str(row["gap_key"])
            topic_contract_version = str(row["gap_contract_version"])
            ReviewService._ensure_binding(
                connection,
                preparation_run_id=preparation_run_id,
                target_kind="topic",
                stable_subject_id=f"gap:{gap_key}",
                topic_contract_version=topic_contract_version,
                projection={
                    "contract_version": REVIEW_SUBJECT_PROJECTION_VERSION,
                    "target_kind": "topic",
                    "topic_contract_version": topic_contract_version,
                    "question_contract_version": QUESTION_CONTRACT_VERSION,
                    "claim_atoms": [],
                    "gap_atoms": [
                        {
                            "gap_key": gap_key,
                            "dimension": str(row["dimension"]),
                            "severity": str(row["severity"]),
                            "resolution_kind": str(row["resolution_kind"]),
                            "status": str(row["status"]),
                        }
                    ],
                },
                bound_at=timestamp,
            )

    @staticmethod
    def _ensure_binding(
        connection: sqlite3.Connection,
        *,
        preparation_run_id: str,
        target_kind: str,
        stable_subject_id: str,
        topic_contract_version: str,
        projection: JsonObject,
        bound_at: str,
    ) -> None:
        target_id = _stable_id(
            "review-target", target_kind, stable_subject_id, topic_contract_version
        )
        connection.execute(
            """
            INSERT INTO review_targets(
                review_target_id, target_kind, stable_subject_id,
                topic_contract_version, created_at
            ) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT DO NOTHING
            """,
            (
                target_id,
                target_kind,
                stable_subject_id,
                topic_contract_version,
                bound_at,
            ),
        )
        target = connection.execute(
            """
            SELECT target_kind, stable_subject_id, topic_contract_version
            FROM review_targets WHERE review_target_id = ?
            """,
            (target_id,),
        ).fetchone()
        if target is None or (
            str(target["target_kind"]),
            str(target["stable_subject_id"]),
            str(target["topic_contract_version"]),
        ) != (target_kind, stable_subject_id, topic_contract_version):
            raise InvalidInputError("stable ReviewTarget identity collision")
        canonical_projection = _canonical_json(
            cast(JsonValue, projection),
            "ReviewSubjectProjection",
            maximum_bytes=MAX_PRIVATE_PAYLOAD_BYTES,
        )
        projection_sha256 = _sha256_text(canonical_projection)
        fingerprint = _sha256_text(target_id + projection_sha256 + topic_contract_version)
        binding_id = _stable_id("review-binding", preparation_run_id, target_id)
        existing = connection.execute(
            """
            SELECT subject_projection, subject_projection_sha256, subject_fingerprint
            FROM review_target_bindings WHERE review_target_binding_id = ?
            """,
            (binding_id,),
        ).fetchone()
        if existing is not None:
            if (
                str(existing["subject_projection"]) != canonical_projection
                or not hmac.compare_digest(
                    str(existing["subject_projection_sha256"]), projection_sha256
                )
                or not hmac.compare_digest(str(existing["subject_fingerprint"]), fingerprint)
            ):
                raise InvalidInputError("ReviewTargetBinding is not reproducible")
            return
        prior_same = connection.execute(
            """
            SELECT 1
            FROM review_target_bindings AS rtb
            JOIN preparation_runs AS pr
              ON pr.preparation_run_id = rtb.preparation_run_id
            JOIN preparation_runs AS current_run
              ON current_run.preparation_run_id = ?
            WHERE rtb.review_target_id = ?
              AND rtb.subject_fingerprint = ?
              AND pr.review_lineage_sequence < current_run.review_lineage_sequence
            LIMIT 1
            """,
            (preparation_run_id, target_id, fingerprint),
        ).fetchone()
        prior_target = connection.execute(
            """
            SELECT 1
            FROM review_target_bindings AS rtb
            JOIN preparation_runs AS pr
              ON pr.preparation_run_id = rtb.preparation_run_id
            JOIN preparation_runs AS current_run
              ON current_run.preparation_run_id = ?
            WHERE rtb.review_target_id = ?
              AND pr.review_lineage_sequence < current_run.review_lineage_sequence
            LIMIT 1
            """,
            (preparation_run_id, target_id),
        ).fetchone()
        continuity = (
            "continued"
            if prior_same is not None
            else "reassess_required"
            if prior_target is not None
            else "new"
        )
        connection.execute(
            """
            INSERT INTO review_target_bindings(
                review_target_binding_id, review_target_id, preparation_run_id,
                subject_projection, subject_projection_sha256, subject_fingerprint,
                continuity_status, bound_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                binding_id,
                target_id,
                preparation_run_id,
                canonical_projection,
                projection_sha256,
                fingerprint,
                continuity,
                bound_at,
            ),
        )

    @staticmethod
    def report_projection_from_connection(
        connection: sqlite3.Connection,
        preparation_run_id: str,
        *,
        cutoff_at: str,
    ) -> JSONObject:
        run_clock = connection.execute(
            """
            SELECT started_at, review_cutoff_sequence
            FROM preparation_runs WHERE preparation_run_id = ?
            """,
            (preparation_run_id,),
        ).fetchone()
        if run_clock is None or str(run_clock["started_at"]) != cutoff_at:
            raise InvalidInputError("review projection cutoff does not match PreparationRun")
        review_cutoff_sequence = int(run_clock["review_cutoff_sequence"])
        rows = connection.execute(
            """
            SELECT rtb.review_target_binding_id, rtb.review_target_id,
                   rtb.subject_projection_sha256, rtb.subject_fingerprint,
                   rtb.continuity_status, rtb.bound_at,
                   rt.target_kind, rt.stable_subject_id,
                   rt.topic_contract_version
            FROM review_target_bindings AS rtb
            JOIN review_targets AS rt
              ON rt.review_target_id = rtb.review_target_id
            WHERE rtb.preparation_run_id = ?
            ORDER BY rt.target_kind, rt.stable_subject_id, rt.review_target_id
            """,
            (preparation_run_id,),
        ).fetchall()
        bindings: list[JSONObject] = []
        projected_reviews = 0
        requires_reassessment = False
        for row in rows:
            continuity = str(row["continuity_status"])
            review = None
            historical_review = None
            if continuity == "continued":
                review = connection.execute(
                    """
                    SELECT ir.review_id, ir.preparation_run_id AS source_preparation_run_id,
                           ir.review_target_binding_id, ir.question_id, ir.summary,
                           ir.mastery_level, ir.weak_points, ir.next_review_at,
                           ir.created_at, ir.review_sequence,
                           source_binding.subject_fingerprint
                    FROM interview_reviews AS ir
                    JOIN review_target_bindings AS source_binding
                      ON source_binding.review_target_binding_id =
                         ir.review_target_binding_id
                    WHERE source_binding.review_target_id = ?
                      AND source_binding.subject_fingerprint = ?
                      AND ir.review_sequence <= ?
                    ORDER BY ir.review_sequence DESC
                    LIMIT 1
                    """,
                    (
                        str(row["review_target_id"]),
                        str(row["subject_fingerprint"]),
                        review_cutoff_sequence,
                    ),
                ).fetchone()
            elif continuity == "reassess_required":
                historical_review = connection.execute(
                    """
                    SELECT ir.review_id, ir.preparation_run_id AS source_preparation_run_id,
                           ir.review_target_binding_id, ir.question_id, ir.summary,
                           ir.mastery_level, ir.weak_points, ir.next_review_at,
                           ir.created_at, ir.review_sequence,
                           source_binding.subject_fingerprint
                    FROM interview_reviews AS ir
                    JOIN review_target_bindings AS source_binding
                      ON source_binding.review_target_binding_id =
                         ir.review_target_binding_id
                    WHERE source_binding.review_target_id = ?
                      AND ir.review_sequence <= ?
                    ORDER BY ir.review_sequence DESC
                    LIMIT 1
                    """,
                    (str(row["review_target_id"]), review_cutoff_sequence),
                ).fetchone()
            item: JSONObject = {
                "review_target_binding_id": str(row["review_target_binding_id"]),
                "review_target_id": str(row["review_target_id"]),
                "target_kind": str(row["target_kind"]),
                "stable_subject_id": str(row["stable_subject_id"]),
                "topic_contract_version": str(row["topic_contract_version"]),
                "subject_projection_sha256": str(row["subject_projection_sha256"]),
                "subject_fingerprint": str(row["subject_fingerprint"]),
                "continuity_status": continuity,
                "bound_at": str(row["bound_at"]),
                "review_id": None,
                "source_preparation_run_id": None,
                "question_id": None,
                "summary": None,
                "mastery_level": None,
                "weak_points": [],
                "next_review_at": None,
                "reviewed_at": None,
                "historical_review": None,
            }
            if review is not None:
                projected_reviews += 1
                item.update(
                    {
                        "review_id": str(review["review_id"]),
                        "source_preparation_run_id": str(review["source_preparation_run_id"]),
                        "question_id": str(review["question_id"]),
                        "summary": str(review["summary"]),
                        "mastery_level": str(review["mastery_level"]),
                        "weak_points": _json_list(
                            review["weak_points"], "InterviewReview weak points"
                        ),
                        "next_review_at": review["next_review_at"],
                        "reviewed_at": str(review["created_at"]),
                    }
                )
            if historical_review is not None:
                item["historical_review"] = ReviewService._historical_review_projection(
                    historical_review
                )
            if continuity == "reassess_required":
                requires_reassessment = True
            bindings.append(item)
        status = (
            "reassessment_required"
            if requires_reassessment
            else "reviews_projected"
            if projected_reviews
            else "no_reviews_recorded"
        )
        return {
            "contract_version": REVIEW_PROJECTION_VERSION,
            "cutoff_at": cutoff_at,
            "bindings": bindings,
            "status": status,
            "skill_invocation": (
                "$goodjob-career-review 记录本次模拟面试复盘；"
                "复盘将在显式创建的新 PreparationRun 中呈现"
            ),
        }

    @staticmethod
    def _historical_review_projection(review: sqlite3.Row) -> JSONObject:
        return {
            "review_id": str(review["review_id"]),
            "source_preparation_run_id": str(review["source_preparation_run_id"]),
            "review_target_binding_id": str(review["review_target_binding_id"]),
            "question_id": str(review["question_id"]),
            "summary": str(review["summary"]),
            "mastery_level": str(review["mastery_level"]),
            "weak_points": _json_list(
                review["weak_points"], "historical InterviewReview weak points"
            ),
            "next_review_at": review["next_review_at"],
            "reviewed_at": str(review["created_at"]),
            "subject_fingerprint": str(review["subject_fingerprint"]),
        }

    def interview(
        self,
        *,
        workspace_path: Path,
        authorization_receipt_id: str,
        request_value: object,
    ) -> JSONObject:
        request = _object(request_value, "interview_input")
        action = _text(request.get("action"), "action", maximum=40)
        if action == "list_targets":
            parsed = MockReviewTargetRequest.from_value(request)
            return self._list_targets(
                workspace_path=workspace_path,
                authorization_receipt_id=authorization_receipt_id,
                request=parsed,
            )
        if action == "record_review":
            parsed_review = InterviewReviewRequest.from_value(request)
            return self._record_review(
                workspace_path=workspace_path,
                authorization_receipt_id=authorization_receipt_id,
                request=parsed_review,
            )
        raise InvalidInputError("mock review action must be list_targets or record_review")

    def _list_targets(
        self,
        *,
        workspace_path: Path,
        authorization_receipt_id: str,
        request: MockReviewTargetRequest,
    ) -> JSONObject:
        self._database.migrate()
        with self._database.write_transaction() as connection:
            snapshot = self._require_authorized_snapshot(
                connection,
                workspace_path,
                authorization_receipt_id,
                request.preparation_run_id,
            )
            self.ensure_bindings_in_connection(connection, request.preparation_run_id)
            projection = self.report_projection_from_connection(
                connection,
                request.preparation_run_id,
                cutoff_at=str(snapshot["started_at"]),
            )
            questions = self._questions(connection, request.preparation_run_id, projection)
            return {
                "status": "ok",
                "mock_review": {
                    "contract_version": MOCK_REVIEW_TARGETS_VERSION,
                    "preparation_run_id": request.preparation_run_id,
                    "artifact_snapshot_id": str(snapshot["artifact_snapshot_id"]),
                    "review_cutoff_at": str(snapshot["started_at"]),
                    "questions": questions,
                },
            }

    def _record_review(
        self,
        *,
        workspace_path: Path,
        authorization_receipt_id: str,
        request: InterviewReviewRequest,
    ) -> JSONObject:
        self._database.migrate()
        with self._database.write_transaction() as connection:
            self._require_authorized_snapshot(
                connection,
                workspace_path,
                authorization_receipt_id,
                request.preparation_run_id,
            )
            self.ensure_bindings_in_connection(connection, request.preparation_run_id)
            existing = connection.execute(
                """
                SELECT review_id, request_sha256, preparation_run_id
                FROM interview_reviews WHERE request_id = ?
                """,
                (request.request_id,),
            ).fetchone()
            if existing is not None:
                if (
                    not hmac.compare_digest(str(existing["request_sha256"]), request.request_sha256)
                    or str(existing["preparation_run_id"]) != request.preparation_run_id
                ):
                    raise InvalidInputError(
                        "request_id is already bound to another InterviewReview"
                    )
                return self._review_result(connection, str(existing["review_id"]))
            expected_question = self._expected_question_id(
                connection,
                request.preparation_run_id,
                request.review_target_binding_id,
            )
            if not hmac.compare_digest(expected_question, request.question_id):
                raise InvalidInputError(
                    "question_id does not belong to the selected ReviewTargetBinding"
                )
            review_id = _new_id()
            review_sequence = int(
                connection.execute(
                    """
                    SELECT COALESCE(MAX(review_sequence), 0) + 1
                    FROM interview_reviews
                    """
                ).fetchone()[0]
            )
            connection.execute(
                """
                INSERT INTO interview_reviews(
                    review_id, request_id, request_sha256, preparation_run_id,
                    review_target_binding_id, question_id, summary, mastery_level,
                    weak_points, next_review_at, created_at, review_sequence
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    review_id,
                    request.request_id,
                    request.request_sha256,
                    request.preparation_run_id,
                    request.review_target_binding_id,
                    request.question_id,
                    request.summary,
                    request.mastery_level,
                    _canonical_json(
                        cast(JsonValue, list(request.weak_points)), "review weak points"
                    ),
                    request.next_review_at,
                    _now(),
                    review_sequence,
                ),
            )
            return self._review_result(connection, review_id)

    def _require_authorized_snapshot(
        self,
        connection: sqlite3.Connection,
        workspace_path: Path,
        authorization_receipt_id: str,
        preparation_run_id: str,
    ) -> sqlite3.Row:
        canonical_workspace = workspace_path.expanduser().resolve(strict=False)
        PreparationService._require_source_receipt(
            connection, authorization_receipt_id, canonical_workspace
        )
        row = connection.execute(
            """
            SELECT pr.started_at, w.canonical_root, a.artifact_snapshot_id
            FROM preparation_runs AS pr
            JOIN workspaces AS w ON w.workspace_id = pr.workspace_id
            JOIN artifact_snapshots AS a
              ON a.preparation_run_id = pr.preparation_run_id
            WHERE pr.preparation_run_id = ?
            """,
            (preparation_run_id,),
        ).fetchone()
        if row is None:
            raise InvalidInputError(
                "mock review requires a successfully published ArtifactSnapshot"
            )
        stored_workspace = Path(str(row["canonical_root"])).resolve(strict=False)
        if stored_workspace != canonical_workspace:
            raise InvalidInputError("PreparationRun belongs to another workspace")
        # Import locally because reporting builds review projections and imports this service.
        from goodjob.reporting import ArtifactSnapshotService

        ArtifactSnapshotService(self._database).require_valid_snapshot_from_connection(
            connection, preparation_run_id
        )
        return cast(sqlite3.Row, row)

    @staticmethod
    def _expected_question_id(
        connection: sqlite3.Connection,
        preparation_run_id: str,
        review_target_binding_id: str,
    ) -> str:
        binding = connection.execute(
            """
            SELECT rt.target_kind, rt.stable_subject_id
            FROM review_target_bindings AS rtb
            JOIN review_targets AS rt
              ON rt.review_target_id = rtb.review_target_id
            WHERE rtb.review_target_binding_id = ?
              AND rtb.preparation_run_id = ?
            """,
            (review_target_binding_id, preparation_run_id),
        ).fetchone()
        if binding is None:
            raise InvalidInputError(
                "ReviewTargetBinding does not belong to the source PreparationRun"
            )
        stable_subject_id = str(binding["stable_subject_id"])
        if str(binding["target_kind"]) == "claim":
            exists = connection.execute(
                """
                SELECT 1
                FROM preparation_claims AS pc
                JOIN claim_revisions AS cr
                  ON cr.claim_revision_id = pc.claim_revision_id
                WHERE pc.preparation_run_id = ? AND cr.claim_id = ?
                """,
                (preparation_run_id, stable_subject_id),
            ).fetchone()
            subject_id = stable_subject_id
        else:
            if not stable_subject_id.startswith("gap:"):
                raise InvalidInputError("topic ReviewTarget has an invalid stable subject")
            gap_key = stable_subject_id.removeprefix("gap:")
            gap = connection.execute(
                """
                SELECT gap_id FROM knowledge_gaps
                WHERE preparation_run_id = ? AND gap_key = ? AND status = 'open'
                """,
                (preparation_run_id, gap_key),
            ).fetchone()
            exists = gap
            subject_id = str(gap["gap_id"]) if gap is not None else ""
        if exists is None:
            raise InvalidInputError("ReviewTarget has no mock-review question in this run")
        return _question_id(subject_id)

    @staticmethod
    def _questions(
        connection: sqlite3.Connection,
        preparation_run_id: str,
        projection: JSONObject,
    ) -> list[JSONObject]:
        raw_bindings = projection.get("bindings")
        if not isinstance(raw_bindings, list):
            raise InvalidInputError("review projection bindings are invalid")
        binding_by_subject = {
            str(binding["stable_subject_id"]): cast(JSONObject, binding)
            for binding in raw_bindings
            if isinstance(binding, dict) and "stable_subject_id" in binding
        }
        questions: list[JSONObject] = []
        claim_rows = connection.execute(
            """
            SELECT c.claim_id, pc.project_id, pc.module_id, pc.rank,
                   cr.statement_tokens
            FROM preparation_claims AS pc
            JOIN claim_revisions AS cr
              ON cr.claim_revision_id = pc.claim_revision_id
            JOIN claims AS c ON c.claim_id = cr.claim_id
            WHERE pc.preparation_run_id = ? ORDER BY pc.rank
            """,
            (preparation_run_id,),
        ).fetchall()
        for row in claim_rows:
            claim_id = str(row["claim_id"])
            binding = binding_by_subject.get(claim_id)
            if binding is None:
                raise InvalidInputError("Claim is missing its ReviewTargetBinding")
            questions.append(
                {
                    "question_id": _question_id(claim_id),
                    "level": "project_deep_dive",
                    "project_id": str(row["project_id"]),
                    "module_id": row["module_id"],
                    "claim_id": claim_id,
                    "prompt_tokens": [
                        {"kind": "text", "value": "请解释这个要点的实现机制、证据与取舍："},
                        *_json_list(row["statement_tokens"], "Claim statement tokens"),
                    ],
                    "follow_up_tokens": [
                        {
                            "kind": "text",
                            "value": "哪些证据支持当前状态？边界、失败路径和替代方案是什么？",
                        }
                    ],
                    **ReviewService._question_binding_projection(binding),
                }
            )
        gap_rows = connection.execute(
            """
            SELECT gap_id, gap_key, project_id, module_id, description_tokens,
                   resolution_kind
            FROM knowledge_gaps
            WHERE preparation_run_id = ? AND status = 'open'
            ORDER BY scope_kind, scope_id, dimension, gap_key
            """,
            (preparation_run_id,),
        ).fetchall()
        for row in gap_rows:
            binding = binding_by_subject.get(f"gap:{row['gap_key']}")
            if binding is None:
                raise InvalidInputError("KnowledgeGap is missing its ReviewTargetBinding")
            questions.append(
                {
                    "question_id": _question_id(str(row["gap_id"])),
                    "level": "knowledge_gap",
                    "project_id": row["project_id"],
                    "module_id": row["module_id"],
                    "gap_id": str(row["gap_id"]),
                    "prompt_tokens": [
                        {"kind": "text", "value": "请补全或明确以下知识缺口："},
                        *_json_list(row["description_tokens"], "KnowledgeGap description tokens"),
                    ],
                    "follow_up_tokens": [
                        {
                            "kind": "text",
                            "value": (
                                f"需要哪类证据或上下文才能按 {row['resolution_kind']} 解决？"
                            ),
                        }
                    ],
                    **ReviewService._question_binding_projection(binding),
                }
            )
        return questions

    @staticmethod
    def _question_binding_projection(binding: JSONObject) -> JSONObject:
        return {
            "review_target_binding_id": binding["review_target_binding_id"],
            "review_target_id": binding["review_target_id"],
            "continuity_status": binding["continuity_status"],
            "mastery_level": binding["mastery_level"],
            "next_review_at": binding["next_review_at"],
        }

    @staticmethod
    def _review_result(connection: sqlite3.Connection, review_id: str) -> JSONObject:
        row = connection.execute(
            """
            SELECT review_id, preparation_run_id, review_target_binding_id,
                   question_id, summary, mastery_level, weak_points,
                   next_review_at, created_at
            FROM interview_reviews WHERE review_id = ?
            """,
            (review_id,),
        ).fetchone()
        if row is None:
            raise InvalidInputError("InterviewReview does not exist")
        return {
            "status": "ok",
            "interview_review": {
                "review_id": str(row["review_id"]),
                "preparation_run_id": str(row["preparation_run_id"]),
                "review_target_binding_id": str(row["review_target_binding_id"]),
                "question_id": str(row["question_id"]),
                "summary": str(row["summary"]),
                "mastery_level": str(row["mastery_level"]),
                "weak_points": _json_list(row["weak_points"], "InterviewReview weak points"),
                "next_review_at": row["next_review_at"],
                "created_at": str(row["created_at"]),
            },
        }

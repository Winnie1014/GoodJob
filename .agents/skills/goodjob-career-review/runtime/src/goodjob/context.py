"""Task-bound project context interviews and reusable owner-provided facts."""

from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3
from dataclasses import dataclass
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
    _structured,
    _text,
)

CONTEXT_INTERVIEW_REQUEST_VERSION = "context-interview-request-v1"
INTERVIEW_INPUT_VERSION = "interview-input-v1"
CONTEXT_EVIDENCE_PAGE_REQUEST_VERSION = "context-evidence-page-request-v1"
CONTEXT_EVIDENCE_PAGE_VERSION = "context-evidence-page-v1"
MAX_CONTEXT_PROJECTS = 200
MAX_QUESTIONS_PER_PROJECT = 20
MAX_FACTS_PER_PROJECT = 40
MAX_CONTEXT_EVIDENCE_PAGE = 200
FACT_KINDS = frozenset(
    {
        "business_goal",
        "target_user",
        "role",
        "ownership",
        "metric",
        "outcome",
        "tradeoff",
        "learning",
    }
)


def _stable_key(value: object, field_name: str, *, maximum: int = 160) -> str:
    key = _text(value, field_name, maximum=maximum)
    if any(character not in "abcdefghijklmnopqrstuvwxyz0123456789_.-" for character in key):
        raise InvalidInputError(f"{field_name} must use lowercase stable-key characters")
    return key


def _list(value: object, field_name: str, *, maximum: int) -> list[object]:
    if not isinstance(value, list) or not 1 <= len(value) <= maximum:
        raise InvalidInputError(f"{field_name} must be a bounded non-empty JSON list")
    return cast(list[object], value)


@dataclass(frozen=True)
class ContextQuestion:
    question_id: str
    fact_kinds: tuple[str, ...]
    prompt: str

    @classmethod
    def from_value(cls, value: object, index: int) -> ContextQuestion:
        question = _object(value, f"questions[{index}]")
        _reject_unknown_fields(
            question,
            frozenset({"question_id", "fact_kinds", "prompt"}),
            f"questions[{index}]",
        )
        question_id = _stable_key(question.get("question_id"), "question_id")
        raw_fact_kinds = question.get("fact_kinds")
        if not isinstance(raw_fact_kinds, list) or not raw_fact_kinds:
            raise InvalidInputError("question.fact_kinds must be a non-empty JSON string list")
        fact_kinds = tuple(
            _text(item, "question.fact_kinds", maximum=40) for item in raw_fact_kinds
        )
        if len(set(fact_kinds)) != len(fact_kinds) or not set(fact_kinds) <= FACT_KINDS:
            raise InvalidInputError("question.fact_kinds contains duplicates or unsupported kinds")
        prompt = _text(question.get("prompt"), "question.prompt", maximum=1000)
        return cls(question_id, fact_kinds, prompt)

    def as_json(self) -> JsonObject:
        return {
            "question_id": self.question_id,
            "fact_kinds": list(self.fact_kinds),
            "prompt": self.prompt,
        }


@dataclass(frozen=True)
class ContextQuestionCard:
    project_id: str
    questions: tuple[ContextQuestion, ...]

    @classmethod
    def from_value(cls, value: object, index: int) -> ContextQuestionCard:
        card = _object(value, f"cards[{index}]")
        _reject_unknown_fields(
            card,
            frozenset({"project_id", "questions"}),
            f"cards[{index}]",
        )
        project_id = _text(card.get("project_id"), "card.project_id", maximum=200)
        questions = tuple(
            ContextQuestion.from_value(question, question_index)
            for question_index, question in enumerate(
                _list(
                    card.get("questions"),
                    "card.questions",
                    maximum=MAX_QUESTIONS_PER_PROJECT,
                )
            )
        )
        question_ids = [question.question_id for question in questions]
        if len(set(question_ids)) != len(question_ids):
            raise InvalidInputError("question IDs must be unique within one project card")
        return cls(project_id, questions)

    def as_json(self) -> JsonObject:
        return {
            "project_id": self.project_id,
            "questions": [question.as_json() for question in self.questions],
        }


@dataclass(frozen=True)
class ContextInterviewRequest:
    request_id: str
    preparation_run_id: str
    question_set_version: str
    cards: tuple[ContextQuestionCard, ...]

    @classmethod
    def from_value(cls, value: object) -> ContextInterviewRequest:
        request = _object(value, "context_interview_request")
        _reject_unknown_fields(
            request,
            frozenset(
                {
                    "contract_version",
                    "request_id",
                    "preparation_run_id",
                    "question_set_version",
                    "cards",
                }
            ),
            "context_interview_request",
        )
        _canonical_json(
            request,
            "context_interview_request",
            maximum_bytes=MAX_PRIVATE_PAYLOAD_BYTES,
        )
        version = _text(request.get("contract_version"), "contract_version", maximum=80)
        if version != CONTEXT_INTERVIEW_REQUEST_VERSION:
            raise InvalidInputError("unsupported context interview request version")
        cards = tuple(
            ContextQuestionCard.from_value(card, index)
            for index, card in enumerate(
                _list(request.get("cards"), "cards", maximum=MAX_CONTEXT_PROJECTS)
            )
        )
        project_ids = [card.project_id for card in cards]
        if len(set(project_ids)) != len(project_ids):
            raise InvalidInputError("each project may have at most one context question card")
        return cls(
            _text(request.get("request_id"), "request_id", maximum=200),
            _text(
                request.get("preparation_run_id"),
                "preparation_run_id",
                maximum=200,
            ),
            _text(
                request.get("question_set_version"),
                "question_set_version",
                maximum=200,
            ),
            cards,
        )

    @property
    def sha256(self) -> str:
        return hashlib.sha256(
            _canonical_json(
                {
                    "contract_version": CONTEXT_INTERVIEW_REQUEST_VERSION,
                    "request_id": self.request_id,
                    "preparation_run_id": self.preparation_run_id,
                    "question_set_version": self.question_set_version,
                    "cards": [card.as_json() for card in self.cards],
                },
                "context_interview_request",
                maximum_bytes=MAX_PRIVATE_PAYLOAD_BYTES,
            ).encode()
        ).hexdigest()


@dataclass(frozen=True)
class ContextFactDraft:
    fact_key: str
    fact_kind: str
    statement: str

    @classmethod
    def from_value(cls, value: object, index: int) -> ContextFactDraft:
        fact = _object(value, f"facts[{index}]")
        _reject_unknown_fields(
            fact,
            frozenset({"fact_key", "fact_kind", "statement"}),
            f"facts[{index}]",
        )
        fact_kind = _text(fact.get("fact_kind"), "fact_kind", maximum=40)
        if fact_kind not in FACT_KINDS:
            raise InvalidInputError("fact_kind is not supported")
        return cls(
            _stable_key(fact.get("fact_key"), "fact_key"),
            fact_kind,
            _text(fact.get("statement"), "fact.statement", maximum=2000),
        )


@dataclass(frozen=True)
class ContextProjectAnswer:
    project_id: str
    answer_status: str
    structured_answer: JsonValue
    facts: tuple[ContextFactDraft, ...]

    @classmethod
    def from_value(cls, value: object, index: int) -> ContextProjectAnswer:
        answer = _object(value, f"answers[{index}]")
        _reject_unknown_fields(
            answer,
            frozenset({"project_id", "status", "structured_answer", "facts"}),
            f"answers[{index}]",
        )
        status = _text(answer.get("status"), "answer.status", maximum=20)
        if status not in {"answered", "partial", "skipped"}:
            raise InvalidInputError("answer.status must be answered, partial, or skipped")
        raw_structured = answer.get("structured_answer", {})
        structured = _structured(
            raw_structured,
            "answer.structured_answer",
            allow_empty=status == "skipped",
        )
        raw_facts = answer.get("facts", [])
        if not isinstance(raw_facts, list) or len(raw_facts) > MAX_FACTS_PER_PROJECT:
            raise InvalidInputError("answer.facts must be a bounded JSON list")
        facts = tuple(
            ContextFactDraft.from_value(fact, fact_index)
            for fact_index, fact in enumerate(raw_facts)
        )
        fact_keys = [fact.fact_key for fact in facts]
        if len(set(fact_keys)) != len(fact_keys):
            raise InvalidInputError("fact keys must be unique within one project answer")
        if status == "answered" and not facts:
            raise InvalidInputError("an answered context card must yield at least one fact")
        if status == "skipped" and (facts or structured):
            raise InvalidInputError("a skipped context answer cannot contain facts or answer data")
        return cls(
            _text(answer.get("project_id"), "answer.project_id", maximum=200),
            status,
            structured,
            facts,
        )


@dataclass(frozen=True)
class ContextAnswerBatch:
    request_id: str
    preparation_run_id: str
    context_interview_id: str
    answers: tuple[ContextProjectAnswer, ...]

    @classmethod
    def from_value(cls, value: object) -> ContextAnswerBatch:
        request = _object(value, "interview_input")
        _reject_unknown_fields(
            request,
            frozenset(
                {
                    "contract_version",
                    "request_id",
                    "mode",
                    "preparation_run_id",
                    "context_interview_id",
                    "answers",
                }
            ),
            "interview_input",
        )
        _canonical_json(request, "interview_input", maximum_bytes=MAX_PRIVATE_PAYLOAD_BYTES)
        version = _text(request.get("contract_version"), "contract_version", maximum=80)
        if version != INTERVIEW_INPUT_VERSION:
            raise InvalidInputError("unsupported InterviewInput version")
        if _text(request.get("mode"), "mode", maximum=40) != "context":
            raise InvalidInputError("this interview operation currently supports mode=context")
        answers = tuple(
            ContextProjectAnswer.from_value(answer, index)
            for index, answer in enumerate(
                _list(request.get("answers"), "answers", maximum=MAX_CONTEXT_PROJECTS)
            )
        )
        project_ids = [answer.project_id for answer in answers]
        if len(set(project_ids)) != len(project_ids):
            raise InvalidInputError("context answers must contain each project once")
        return cls(
            _text(request.get("request_id"), "request_id", maximum=200),
            _text(
                request.get("preparation_run_id"),
                "preparation_run_id",
                maximum=200,
            ),
            _text(
                request.get("context_interview_id"),
                "context_interview_id",
                maximum=200,
            ),
            answers,
        )

    @property
    def sha256(self) -> str:
        value: JsonObject = {
            "contract_version": INTERVIEW_INPUT_VERSION,
            "request_id": self.request_id,
            "mode": "context",
            "preparation_run_id": self.preparation_run_id,
            "context_interview_id": self.context_interview_id,
            "answers": [
                {
                    "project_id": answer.project_id,
                    "status": answer.answer_status,
                    "structured_answer": answer.structured_answer,
                    "facts": [
                        {
                            "fact_key": fact.fact_key,
                            "fact_kind": fact.fact_kind,
                            "statement": fact.statement,
                        }
                        for fact in answer.facts
                    ],
                }
                for answer in self.answers
            ],
        }
        return hashlib.sha256(
            _canonical_json(
                value,
                "interview_input",
                maximum_bytes=MAX_PRIVATE_PAYLOAD_BYTES,
            ).encode()
        ).hexdigest()


@dataclass(frozen=True)
class ContextEvidencePageRequest:
    preparation_run_id: str
    project_id: str | None
    cursor: str | None
    limit: int

    @classmethod
    def from_value(cls, value: object) -> ContextEvidencePageRequest:
        request = _object(value, "context_evidence_page_request")
        _reject_unknown_fields(
            request,
            frozenset(
                {
                    "contract_version",
                    "preparation_run_id",
                    "project_id",
                    "cursor",
                    "limit",
                }
            ),
            "context_evidence_page_request",
        )
        version = _text(request.get("contract_version"), "contract_version", maximum=80)
        if version != CONTEXT_EVIDENCE_PAGE_REQUEST_VERSION:
            raise InvalidInputError("unsupported context Evidence page request version")
        project_value = request.get("project_id")
        cursor_value = request.get("cursor")
        limit = request.get("limit", MAX_CONTEXT_EVIDENCE_PAGE)
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 200:
            raise InvalidInputError("context Evidence page limit must be between 1 and 200")
        return cls(
            _text(
                request.get("preparation_run_id"),
                "preparation_run_id",
                maximum=200,
            ),
            (
                _text(project_value, "project_id", maximum=200)
                if project_value is not None
                else None
            ),
            _text(cursor_value, "cursor", maximum=200) if cursor_value is not None else None,
            limit,
        )


class ContextInterviewService:
    """Persist one compact context card batch and append its structured answers."""

    def __init__(self, database: Database) -> None:
        self._database = database

    def request_context(
        self,
        *,
        authorization_receipt_id: str,
        request_value: object,
    ) -> dict[str, object]:
        request = ContextInterviewRequest.from_value(request_value)
        request_sha256 = request.sha256
        self._database.migrate()
        with self._database.write_transaction() as connection:
            existing = connection.execute(
                """
                SELECT context_interview_id, request_sha256, preparation_run_id
                FROM context_interviews WHERE request_id = ?
                """,
                (request.request_id,),
            ).fetchone()
            if existing is not None:
                if (
                    not hmac.compare_digest(str(existing["request_sha256"]), request_sha256)
                    or str(existing["preparation_run_id"]) != request.preparation_run_id
                ):
                    raise InvalidInputError(
                        "request_id is already bound to another context request"
                    )
                self._require_session(
                    connection, request.preparation_run_id, authorization_receipt_id
                )
                interview_id = str(existing["context_interview_id"])
            else:
                status = self._require_session(
                    connection,
                    request.preparation_run_id,
                    authorization_receipt_id,
                )
                if status != "analyzing":
                    raise InvalidInputError("context can only be requested for an analyzing run")
                if (
                    connection.execute(
                        """
                        SELECT 1 FROM context_interviews
                        WHERE preparation_run_id = ?
                        """,
                        (request.preparation_run_id,),
                    ).fetchone()
                    is not None
                ):
                    raise InvalidInputError(
                        "PreparationRun already has one immutable context interview"
                    )
                eligible = self._eligible_projects(connection, request.preparation_run_id)
                requested = {card.project_id for card in request.cards}
                if not requested <= eligible:
                    raise InvalidInputError(
                        "a context card targets a project outside the frozen run"
                    )
                interview_id = _new_id()
                timestamp = _now()
                connection.execute(
                    """
                    INSERT INTO context_interviews(
                        context_interview_id, request_id, request_sha256,
                        preparation_run_id, question_set_version, status, created_at
                    ) VALUES (?, ?, ?, ?, ?, 'open', ?)
                    """,
                    (
                        interview_id,
                        request.request_id,
                        request_sha256,
                        request.preparation_run_id,
                        request.question_set_version,
                        timestamp,
                    ),
                )
                connection.executemany(
                    """
                    INSERT INTO context_question_cards(
                        context_interview_id, project_id, questions_json
                    ) VALUES (?, ?, ?)
                    """,
                    (
                        (
                            interview_id,
                            card.project_id,
                            _canonical_json(
                                [question.as_json() for question in card.questions],
                                "context questions",
                            ),
                        )
                        for card in request.cards
                    ),
                )
                connection.execute(
                    """
                    UPDATE preparation_runs
                    SET status = 'awaiting_context', status_reason = 'context_requested',
                        last_transition_at = ?
                    WHERE preparation_run_id = ?
                    """,
                    (timestamp, request.preparation_run_id),
                )
        return self._interview_result(interview_id)

    def record_context(
        self,
        *,
        authorization_receipt_id: str,
        request_value: object,
    ) -> dict[str, object]:
        request = ContextAnswerBatch.from_value(request_value)
        request_sha256 = request.sha256
        self._database.migrate()
        with self._database.write_transaction() as connection:
            existing = connection.execute(
                """
                SELECT context_answer_batch_id, request_sha256, preparation_run_id,
                       context_interview_id
                FROM context_answer_batches WHERE request_id = ?
                """,
                (request.request_id,),
            ).fetchone()
            if existing is not None:
                if (
                    not hmac.compare_digest(str(existing["request_sha256"]), request_sha256)
                    or str(existing["preparation_run_id"]) != request.preparation_run_id
                    or str(existing["context_interview_id"]) != request.context_interview_id
                ):
                    raise InvalidInputError("request_id is already bound to another answer batch")
                self._require_session(
                    connection, request.preparation_run_id, authorization_receipt_id
                )
                batch_id = str(existing["context_answer_batch_id"])
            else:
                status = self._require_session(
                    connection,
                    request.preparation_run_id,
                    authorization_receipt_id,
                )
                if status != "awaiting_context":
                    raise InvalidInputError("context answers require an awaiting_context run")
                interview = connection.execute(
                    """
                    SELECT question_set_version, status
                    FROM context_interviews
                    WHERE context_interview_id = ? AND preparation_run_id = ?
                    """,
                    (request.context_interview_id, request.preparation_run_id),
                ).fetchone()
                if interview is None or str(interview["status"]) != "open":
                    raise InvalidInputError("context interview is not open for this run")
                card_rows = connection.execute(
                    """
                    SELECT project_id, questions_json FROM context_question_cards
                    WHERE context_interview_id = ? ORDER BY project_id
                    """,
                    (request.context_interview_id,),
                ).fetchall()
                card_projects = {str(row["project_id"]) for row in card_rows}
                if {answer.project_id for answer in request.answers} != card_projects:
                    raise InvalidInputError(
                        "context answers must cover every requested project exactly once"
                    )
                requested_fact_kind_groups: dict[str, tuple[frozenset[str], ...]] = {}
                for row in card_rows:
                    project_id = str(row["project_id"])
                    raw_questions = json.loads(str(row["questions_json"]))
                    if not isinstance(raw_questions, list):
                        raise InvalidInputError("stored context questions are invalid")
                    requested_fact_kind_groups[project_id] = tuple(
                        frozenset(
                            str(fact_kind)
                            for fact_kind in question.get("fact_kinds", [])
                            if isinstance(fact_kind, str)
                        )
                        for question in raw_questions
                        if isinstance(question, dict)
                    )
                if any(
                    fact.fact_kind
                    not in set().union(*requested_fact_kind_groups[answer.project_id])
                    for answer in request.answers
                    for fact in answer.facts
                ):
                    raise InvalidInputError(
                        "context facts must use a fact kind requested by that project card"
                    )
                if any(
                    answer.answer_status == "answered"
                    and any(
                        not group.intersection(fact.fact_kind for fact in answer.facts)
                        for group in requested_fact_kind_groups[answer.project_id]
                    )
                    for answer in request.answers
                ):
                    raise InvalidInputError(
                        "an answered context card must cover every question; use partial otherwise"
                    )
                timestamp = _now()
                batch_id = _new_id()
                connection.execute(
                    """
                    INSERT INTO context_answer_batches(
                        context_answer_batch_id, request_id, request_sha256,
                        context_interview_id, preparation_run_id, committed_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        batch_id,
                        request.request_id,
                        request_sha256,
                        request.context_interview_id,
                        request.preparation_run_id,
                        timestamp,
                    ),
                )
                question_set_version = str(interview["question_set_version"])
                for answer in request.answers:
                    answer_id = _new_id()
                    connection.execute(
                        """
                        INSERT INTO context_answers(
                            answer_id, context_answer_batch_id, project_id,
                            question_set_version, answer_status, structured_answer, answered_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            answer_id,
                            batch_id,
                            answer.project_id,
                            question_set_version,
                            answer.answer_status,
                            _canonical_json(
                                answer.structured_answer,
                                "structured context answer",
                                maximum_bytes=MAX_PRIVATE_PAYLOAD_BYTES,
                            ),
                            timestamp,
                        ),
                    )
                    for fact in answer.facts:
                        self._insert_fact(
                            connection,
                            preparation_run_id=request.preparation_run_id,
                            answer_id=answer_id,
                            project_id=answer.project_id,
                            fact=fact,
                            timestamp=timestamp,
                        )
                connection.execute(
                    """
                    UPDATE context_interviews
                    SET status = 'completed', completed_at = ?
                    WHERE context_interview_id = ?
                    """,
                    (timestamp, request.context_interview_id),
                )
                connection.execute(
                    """
                    UPDATE preparation_runs
                    SET status = 'analyzing', status_reason = NULL, last_transition_at = ?
                    WHERE preparation_run_id = ?
                    """,
                    (timestamp, request.preparation_run_id),
                )
        return self._answer_result(batch_id)

    def list_context_evidence(
        self,
        *,
        authorization_receipt_id: str,
        request_value: object,
    ) -> dict[str, object]:
        request = ContextEvidencePageRequest.from_value(request_value)
        self._database.migrate()
        with self._database.read_connection() as connection:
            self._require_session(
                connection,
                request.preparation_run_id,
                authorization_receipt_id,
            )
            if request.project_id is not None and request.project_id not in self._eligible_projects(
                connection, request.preparation_run_id
            ):
                raise InvalidInputError(
                    "context Evidence project is outside the frozen PreparationRun"
                )
            cursor_key: tuple[str, str, str] | None = None
            if request.cursor is not None:
                cursor_row = connection.execute(
                    """
                    SELECT project_id, fact_key, context_fact_id
                    FROM preparation_context_facts
                    WHERE preparation_run_id = ? AND context_fact_id = ?
                      AND bound_status = 'current'
                      AND (? IS NULL OR project_id = ?)
                    """,
                    (
                        request.preparation_run_id,
                        request.cursor,
                        request.project_id,
                        request.project_id,
                    ),
                ).fetchone()
                if cursor_row is None:
                    raise InvalidInputError(
                        "context Evidence cursor is outside the requested run or project"
                    )
                cursor_key = (
                    str(cursor_row["project_id"]),
                    str(cursor_row["fact_key"]),
                    str(cursor_row["context_fact_id"]),
                )
            filters = [
                "pcf_bound.preparation_run_id = ?",
                "pcf_bound.bound_status = 'current'",
            ]
            parameters: list[object] = [request.preparation_run_id]
            if request.project_id is not None:
                filters.append("pcf_bound.project_id = ?")
                parameters.append(request.project_id)
            if cursor_key is not None:
                filters.append(
                    "(pcf_bound.project_id, pcf_bound.fact_key, "
                    "pcf_bound.context_fact_id) > (?, ?, ?)"
                )
                parameters.extend(cursor_key)
            parameters.append(request.limit + 1)
            rows = connection.execute(
                f"""
                SELECT pcf_bound.project_id, pcf.context_fact_id, pcf.fact_key,
                       pcf.fact_kind, pcf.statement, pcf.source_kind,
                       pcf_bound.bound_status, ec.evidence_id
                FROM preparation_context_facts AS pcf_bound
                JOIN project_context_facts AS pcf
                  ON pcf.context_fact_id = pcf_bound.context_fact_id
                JOIN evidence_contexts AS ec
                  ON ec.context_fact_id = pcf.context_fact_id
                WHERE {" AND ".join(filters)}
                ORDER BY pcf_bound.project_id, pcf_bound.fact_key,
                         pcf_bound.context_fact_id
                LIMIT ?
                """,
                parameters,
            ).fetchall()
            total_row = connection.execute(
                """
                SELECT COUNT(*) AS count
                FROM preparation_context_facts
                WHERE preparation_run_id = ? AND bound_status = 'current'
                  AND (? IS NULL OR project_id = ?)
                """,
                (
                    request.preparation_run_id,
                    request.project_id,
                    request.project_id,
                ),
            ).fetchone()
            assert total_row is not None
        has_more = len(rows) > request.limit
        visible_rows = rows[: request.limit]
        items = [
            {
                "evidence_id": str(row["evidence_id"]),
                "context_fact_id": str(row["context_fact_id"]),
                "project_id": str(row["project_id"]),
                "fact_key": str(row["fact_key"]),
                "fact_kind": str(row["fact_kind"]),
                "statement": str(row["statement"]),
                "source_kind": str(row["source_kind"]),
                "bound_status": str(row["bound_status"]),
            }
            for row in visible_rows
        ]
        return {
            "status": "ok",
            "context_evidence_page": {
                "contract_version": CONTEXT_EVIDENCE_PAGE_VERSION,
                "preparation_run_id": request.preparation_run_id,
                "project_id": request.project_id,
                "items": items,
                "item_count": len(items),
                "total_items": int(total_row["count"]),
                "has_more": has_more,
                "next_cursor": (
                    str(visible_rows[-1]["context_fact_id"]) if has_more and visible_rows else None
                ),
            },
        }

    @staticmethod
    def _insert_fact(
        connection: sqlite3.Connection,
        *,
        preparation_run_id: str,
        answer_id: str,
        project_id: str,
        fact: ContextFactDraft,
        timestamp: str,
    ) -> None:
        connection.execute(
            """
            UPDATE project_context_facts
            SET status = 'superseded'
            WHERE project_id = ? AND fact_key = ? AND status = 'current'
            """,
            (project_id, fact.fact_key),
        )
        context_fact_id = _new_id()
        connection.execute(
            """
            INSERT INTO project_context_facts(
                context_fact_id, project_id, fact_key, fact_kind, statement,
                source_kind, source_answer_id, config_revision, status, created_at
            ) VALUES (?, ?, ?, ?, ?, 'context_answer', ?, NULL, 'current', ?)
            """,
            (
                context_fact_id,
                project_id,
                fact.fact_key,
                fact.fact_kind,
                fact.statement,
                answer_id,
                timestamp,
            ),
        )
        evidence_id = _new_id()
        locator = _canonical_json(
            {"context_fact_id": context_fact_id},
            "context evidence locator",
        )
        connection.execute(
            """
            INSERT INTO evidence(
                evidence_id, project_id, acquisition_scope, project_snapshot_id,
                module_id, source_revision_id, content_equivalence_key, origin_kind,
                evidence_kind, locator, summary, commit_state, created_at,
                preparation_run_id, query_reason
            ) VALUES (?, ?, 'context', NULL, NULL, NULL, NULL, 'context_fact',
                      'user_statement', ?, ?, 'not_applicable', ?, NULL, NULL)
            """,
            (
                evidence_id,
                project_id,
                locator,
                f"Owner provided current {fact.fact_kind} context for this project.",
                timestamp,
            ),
        )
        connection.execute(
            """
            INSERT INTO evidence_contexts(evidence_id, context_fact_id)
            VALUES (?, ?)
            """,
            (evidence_id, context_fact_id),
        )
        connection.execute(
            """
            DELETE FROM preparation_context_facts
            WHERE preparation_run_id = ? AND project_id = ? AND fact_key = ?
            """,
            (preparation_run_id, project_id, fact.fact_key),
        )
        connection.execute(
            """
            INSERT INTO preparation_context_facts(
                preparation_run_id, context_fact_id, project_id, fact_key,
                bound_status, bound_at
            ) VALUES (?, ?, ?, ?, 'current', ?)
            """,
            (preparation_run_id, context_fact_id, project_id, fact.fact_key, timestamp),
        )

    @staticmethod
    def _eligible_projects(
        connection: sqlite3.Connection,
        preparation_run_id: str,
    ) -> set[str]:
        return {
            str(row["project_id"])
            for row in connection.execute(
                """
                SELECT project_id FROM preparation_run_projects
                WHERE preparation_run_id = ?
                  AND snapshot_disposition IN ('fresh', 'carried_forward')
                """,
                (preparation_run_id,),
            ).fetchall()
        }

    @staticmethod
    def _require_session(
        connection: sqlite3.Connection,
        preparation_run_id: str,
        authorization_receipt_id: str,
    ) -> str:
        status, _ = PreparationService._require_active_run_and_session(
            connection,
            preparation_run_id,
            authorization_receipt_id,
        )
        return status

    def _interview_result(self, context_interview_id: str) -> dict[str, object]:
        with self._database.read_connection() as connection:
            row = connection.execute(
                """
                SELECT context_interview_id, preparation_run_id, question_set_version,
                       status, created_at, completed_at
                FROM context_interviews WHERE context_interview_id = ?
                """,
                (context_interview_id,),
            ).fetchone()
            if row is None:
                raise InvalidInputError("context interview does not exist")
            cards = [
                {
                    "project_id": str(card["project_id"]),
                    "questions": json.loads(str(card["questions_json"])),
                }
                for card in connection.execute(
                    """
                    SELECT project_id, questions_json FROM context_question_cards
                    WHERE context_interview_id = ? ORDER BY project_id
                    """,
                    (context_interview_id,),
                ).fetchall()
            ]
        return {
            "status": "ok",
            "context_interview": {
                "context_interview_id": str(row["context_interview_id"]),
                "preparation_run_id": str(row["preparation_run_id"]),
                "question_set_version": str(row["question_set_version"]),
                "status": str(row["status"]),
                "created_at": str(row["created_at"]),
                "completed_at": row["completed_at"],
                "cards": cards,
            },
        }

    def _answer_result(self, batch_id: str) -> dict[str, object]:
        with self._database.read_connection() as connection:
            batch = connection.execute(
                """
                SELECT cab.context_answer_batch_id, cab.preparation_run_id,
                       cab.context_interview_id, cab.committed_at, pr.status AS run_status
                FROM context_answer_batches AS cab
                JOIN preparation_runs AS pr
                  ON pr.preparation_run_id = cab.preparation_run_id
                WHERE cab.context_answer_batch_id = ?
                """,
                (batch_id,),
            ).fetchone()
            if batch is None:
                raise InvalidInputError("context answer batch does not exist")
            answer_rows = connection.execute(
                """
                SELECT ca.answer_id, ca.project_id, ca.answer_status,
                       pcf.context_fact_id, pcf.fact_key, pcf.fact_kind,
                       pcf.statement, ec.evidence_id
                FROM context_answers AS ca
                LEFT JOIN project_context_facts AS pcf
                  ON pcf.source_answer_id = ca.answer_id
                LEFT JOIN evidence_contexts AS ec
                  ON ec.context_fact_id = pcf.context_fact_id
                WHERE ca.context_answer_batch_id = ?
                ORDER BY ca.project_id, pcf.fact_key
                """,
                (batch_id,),
            ).fetchall()
            answers_by_id: dict[str, dict[str, object]] = {}
            for row in answer_rows:
                answer_id = str(row["answer_id"])
                answer = answers_by_id.setdefault(
                    answer_id,
                    {
                        "answer_id": answer_id,
                        "project_id": str(row["project_id"]),
                        "status": str(row["answer_status"]),
                        "facts": [],
                    },
                )
                if row["context_fact_id"] is not None:
                    facts = cast(list[object], answer["facts"])
                    facts.append(
                        {
                            "context_fact_id": str(row["context_fact_id"]),
                            "fact_key": str(row["fact_key"]),
                            "fact_kind": str(row["fact_kind"]),
                            "statement": str(row["statement"]),
                            "evidence_id": str(row["evidence_id"]),
                        }
                    )
            answers = list(answers_by_id.values())
            for answer in answers:
                answer["fact_count"] = len(cast(list[object], answer["facts"]))
        return {
            "status": "ok",
            "context_answer_batch": {
                "context_answer_batch_id": str(batch["context_answer_batch_id"]),
                "preparation_run_id": str(batch["preparation_run_id"]),
                "context_interview_id": str(batch["context_interview_id"]),
                "committed_at": str(batch["committed_at"]),
                "answers": answers,
            },
            "run_status": str(batch["run_status"]),
        }

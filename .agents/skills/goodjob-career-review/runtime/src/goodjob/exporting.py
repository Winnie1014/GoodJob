"""Verified English projections derived from one immutable Chinese snapshot."""

from __future__ import annotations

import hashlib
import html
import json
import os
import re
import sqlite3
import stat
import unicodedata
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath
from typing import cast

from goodjob import __version__
from goodjob.analysis import InlineToken
from goodjob.db import Database
from goodjob.errors import InvalidInputError
from goodjob.preparation import PreparationService, _now
from goodjob.process_identity import owner_process_stopped, process_identity
from goodjob.reporting import (
    ArtifactSnapshotService,
    canonical_report_bundle,
    report_bundle_sha256,
)

TRANSLATION_EXPORT_REQUEST_CONTRACT_VERSION = "translation-export-request-v1"
TRANSLATION_EXPORT_SOURCE_CONTRACT_VERSION = "translation-export-source-v1"
EXPORT_PROJECTION_CONTRACT_VERSION = "export-projection-v1"
DERIVED_EXPORT_MANIFEST_CONTRACT_VERSION = "derived-export-manifest-v1"
EXPORT_KINDS = ("resume", "interview_qa")
GENERATOR_VERSION = f"goodjob-runtime-{__version__}"
RESUME_FILENAME = "resume.en.md"
INTERVIEW_FILENAME = "interview.en.md"
MANIFEST_FILENAME = "manifest.json"
MAX_SOURCE_ITEMS = 4_000
MAX_SOURCE_TEXT_CHARS = 20_000
MAX_TARGET_TEXT_CHARS = 20_000

_NUMBER = r"\d+(?:\.\d+)?"
_UNIT = (
    r"milliseconds?|millisecond|ms|seconds?|second|secs?|sec|s|minutes?|minute|mins?|min|"
    r"hours?|hour|hrs?|hr|days?|day|times?|items?|occurrences?|entries|entry|people|persons?|"
    r"percent(?:age)?|%|秒|分钟|小时|天|倍|个|次|条|人|mb|gb"
)
_NUMBER_ANCHOR = re.compile(
    rf"(?<![\w.])(?P<number>{_NUMBER})(?:\s*(?P<unit>{_UNIT}))?(?![\w.])",
    re.IGNORECASE,
)
_NUMBER_ANCHOR_FULL = re.compile(
    rf"^(?P<number>{_NUMBER})(?:\s*(?P<unit>{_UNIT}))?$",
    re.IGNORECASE,
)
_UNIT_ALIASES = {
    "%": "%",
    "percent": "%",
    "percentage": "%",
    "ms": "ms",
    "millisecond": "ms",
    "milliseconds": "ms",
    "s": "s",
    "sec": "s",
    "secs": "s",
    "second": "s",
    "seconds": "s",
    "秒": "s",
    "min": "min",
    "mins": "min",
    "minute": "min",
    "minutes": "min",
    "分钟": "min",
    "hr": "h",
    "hrs": "h",
    "hour": "h",
    "hours": "h",
    "小时": "h",
    "day": "d",
    "days": "d",
    "天": "d",
    "time": "x",
    "times": "x",
    "倍": "x",
    "item": "items",
    "items": "items",
    "个": "items",
    "occurrence": "occurrences",
    "occurrences": "occurrences",
    "次": "occurrences",
    "entry": "entries",
    "entries": "entries",
    "条": "entries",
    "person": "people",
    "persons": "people",
    "people": "people",
    "人": "people",
    "mb": "MB",
    "gb": "GB",
}
_TECHNOLOGY_ALIASES = {
    "js": "javascript",
    "javascript": "javascript",
    "ts": "typescript",
    "typescript": "typescript",
    "node": "node.js",
    "nodejs": "node.js",
    "node.js": "node.js",
    "postgres": "postgresql",
    "postgresql": "postgresql",
    "golang": "go",
    "go": "go",
    "k8s": "kubernetes",
    "kubernetes": "kubernetes",
}
_KNOWN_TECHNOLOGIES = {
    "python",
    "java",
    "kotlin",
    "swift",
    "rust",
    "go",
    "c++",
    "c#",
    "javascript",
    "typescript",
    "react",
    "vue",
    "angular",
    "node.js",
    "spring",
    "django",
    "flask",
    "fastapi",
    "sqlite",
    "mysql",
    "postgresql",
    "redis",
    "kafka",
    "docker",
    "kubernetes",
    "aws",
    "azure",
    "gcp",
    "grpc",
    "graphql",
}

type JSONObject = dict[str, object]


def _reject_json_constant(value: str) -> object:
    raise ValueError(f"unsupported JSON constant: {value}")


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
        raise InvalidInputError("translation export data is not canonical JSON") from exc


def _sha256_value(value: object) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _object(value: object, field_name: str) -> JSONObject:
    if not isinstance(value, dict):
        raise InvalidInputError(f"{field_name} must be a JSON object")
    return cast(JSONObject, value)


def _exact_fields(value: JSONObject, expected: set[str], field_name: str) -> None:
    missing = expected - set(value)
    extra = set(value) - expected
    if missing:
        raise InvalidInputError(f"{field_name} is missing fields: {', '.join(sorted(missing))}")
    if extra:
        raise InvalidInputError(f"{field_name} has unsupported fields: {', '.join(sorted(extra))}")


def _text(value: object, field_name: str, *, maximum: int = 512) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum or "\x00" in value:
        raise InvalidInputError(f"{field_name} must be a bounded non-empty string")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise InvalidInputError(f"{field_name} must be valid UTF-8 text") from exc
    return value


def _string_list(value: object, field_name: str, *, maximum: int = 2_000) -> list[str]:
    if not isinstance(value, list) or len(value) > maximum:
        raise InvalidInputError(f"{field_name} must be a bounded string list")
    result = [_text(item, f"{field_name}[]") for item in value]
    if len(result) != len(set(result)):
        raise InvalidInputError(f"{field_name} must not contain duplicates")
    return result


class _ReportDataParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self._inside = False
        self._seen = 0
        self._parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.casefold() != "script":
            return
        attributes = {key.casefold(): value for key, value in attrs}
        if attributes.get("id") != "report-data":
            return
        if self._inside or self._seen:
            raise InvalidInputError("snapshot HTML contains duplicate report data")
        if attributes.get("type") != "application/json":
            raise InvalidInputError("snapshot report data has an unexpected media type")
        self._inside = True
        self._seen += 1

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() == "script" and self._inside:
            self._inside = False

    def handle_data(self, data: str) -> None:
        if self._inside:
            self._parts.append(data)

    def value(self) -> str:
        if self._seen != 1 or self._inside or not self._parts:
            raise InvalidInputError("snapshot HTML does not contain one complete report data block")
        return "".join(self._parts)


def _report_bundle_from_html(html_bytes: bytes) -> JSONObject:
    try:
        html = html_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise InvalidInputError("snapshot HTML is not valid UTF-8") from exc
    parser = _ReportDataParser()
    try:
        parser.feed(html)
        parser.close()
        raw = parser.value()
        value: object = json.loads(raw, parse_constant=_reject_json_constant)
    except (UnicodeError, json.JSONDecodeError, RecursionError, ValueError) as exc:
        raise InvalidInputError("snapshot report data is not valid JSON") from exc
    bundle = _object(value, "snapshot ReportBundle")
    canonical_report_bundle(bundle)
    return bundle


def _source_text(value: object, field_name: str) -> str:
    if not isinstance(value, list) or not value:
        raise InvalidInputError(f"{field_name} must contain source tokens")
    parts: list[str] = []
    for index, item in enumerate(value):
        token = InlineToken.from_value(item, f"{field_name}[{index}]")
        parts.append(token.value)
    return _text("".join(parts), field_name, maximum=MAX_SOURCE_TEXT_CHARS)


def _anchors(value: object, field_name: str) -> JSONObject:
    anchors = _object(value, field_name)
    expected = {
        "numbers_and_units",
        "technology_identifiers",
        "implementation_status",
        "personal_attribution",
        "role_anchor_ids",
        "outcome_anchor_ids",
    }
    _exact_fields(anchors, expected, field_name)
    for key in expected - {"personal_attribution"}:
        _string_list(anchors[key], f"{field_name}.{key}")
    _text(anchors["personal_attribution"], f"{field_name}.personal_attribution")
    return anchors


def _projection_items(bundle: JSONObject) -> tuple[str, list[JSONObject]]:
    projection = _object(bundle.get("export_projection"), "ReportBundle export_projection")
    _exact_fields(
        projection,
        {"contract_version", "items", "projection_sha256"},
        "ReportBundle export_projection",
    )
    if projection["contract_version"] != EXPORT_PROJECTION_CONTRACT_VERSION:
        raise InvalidInputError("unsupported export projection contract version")
    raw_items = projection["items"]
    if not isinstance(raw_items, list) or len(raw_items) > MAX_SOURCE_ITEMS:
        raise InvalidInputError("export projection items must be a bounded list")
    projection_sha256 = _text(
        projection["projection_sha256"], "source_projection_sha256", maximum=64
    )
    if len(projection_sha256) != 64 or projection_sha256 != _sha256_value(raw_items):
        raise InvalidInputError("export projection digest does not match its frozen items")
    projected: list[JSONObject] = []
    seen_ids: set[str] = set()
    for index, raw_item in enumerate(raw_items):
        item = _object(raw_item, f"export_projection.items[{index}]")
        _exact_fields(
            item,
            {
                "source_item_id",
                "export_kind",
                "source_tokens",
                "claim_refs",
                "evidence_refs",
                "role_lens_refs",
                "anchors",
                "project_id",
                "module_id",
            },
            f"export_projection.items[{index}]",
        )
        source_item_id = _text(item["source_item_id"], "source_item_id")
        if source_item_id in seen_ids:
            raise InvalidInputError("export projection source_item_id values must be unique")
        seen_ids.add(source_item_id)
        export_kind = _text(item["export_kind"], "export_kind")
        if export_kind not in EXPORT_KINDS:
            raise InvalidInputError("export projection contains an unsupported export kind")
        module_id = item["module_id"]
        if module_id is not None:
            _text(module_id, "module_id")
        projected.append(
            {
                "source_item_id": source_item_id,
                "export_kind": export_kind,
                "source_text": _source_text(item["source_tokens"], "source_tokens"),
                "claim_refs": _string_list(item["claim_refs"], "claim_refs"),
                "evidence_refs": _string_list(item["evidence_refs"], "evidence_refs"),
                "role_lens_refs": _string_list(item["role_lens_refs"], "role_lens_refs"),
                "anchors": _anchors(item["anchors"], "anchors"),
                "project_id": _text(item["project_id"], "project_id"),
                "module_id": module_id,
            }
        )
    if [str(item["source_item_id"]) for item in projected] != sorted(seen_ids):
        raise InvalidInputError("export projection source items are not in canonical order")
    return projection_sha256, projected


def _normalized_text(value: str) -> str:
    return unicodedata.normalize("NFKC", value).casefold().strip()


def _normalized_string_set(value: object, field_name: str) -> tuple[str, ...]:
    return tuple(sorted({_normalized_text(item) for item in _string_list(value, field_name)}))


def _normalized_decimal(value: str, field_name: str) -> str:
    try:
        number = Decimal(value)
    except InvalidOperation as exc:
        raise InvalidInputError(f"{field_name} contains an invalid number") from exc
    normalized = format(number.normalize(), "f")
    if "." in normalized:
        normalized = normalized.rstrip("0").rstrip(".")
    return normalized or "0"


def _normalized_number_match(match: re.Match[str], field_name: str) -> str:
    number = _normalized_decimal(match.group("number"), field_name)
    raw_unit = match.group("unit")
    if raw_unit is None:
        return number
    unit = _UNIT_ALIASES.get(_normalized_text(raw_unit))
    if unit is None:
        raise InvalidInputError(f"{field_name} contains an unsupported unit")
    return f"{number}{unit}"


def _normalized_number_anchors(value: object, field_name: str) -> tuple[str, ...]:
    anchors = _string_list(value, field_name)
    normalized: set[str] = set()
    for anchor in anchors:
        match = _NUMBER_ANCHOR_FULL.fullmatch(unicodedata.normalize("NFKC", anchor).strip())
        if match is None:
            raise InvalidInputError(f"{field_name} contains a malformed numeric anchor")
        normalized.add(_normalized_number_match(match, field_name))
    return tuple(sorted(normalized))


def _numbers_in_target(value: str) -> tuple[str, ...]:
    return tuple(
        sorted(
            {
                _normalized_number_match(match, "translation target")
                for match in _NUMBER_ANCHOR.finditer(unicodedata.normalize("NFKC", value))
            }
        )
    )


def _technology_key(value: str) -> str:
    normalized = _normalized_text(value)
    return _TECHNOLOGY_ALIASES.get(normalized, normalized)


def _identifier_present(text: str, identifier: str) -> bool:
    normalized_text = _normalized_text(text)
    normalized_identifier = _normalized_text(identifier)
    escaped = re.escape(normalized_identifier)
    prefix = r"(?<![\w])" if normalized_identifier[:1].isalnum() else ""
    suffix = r"(?![\w])" if normalized_identifier[-1:].isalnum() else ""
    return re.search(f"{prefix}{escaped}{suffix}", normalized_text) is not None


def _known_technologies_in_target(text: str) -> set[str]:
    aliases = set(_TECHNOLOGY_ALIASES) | _KNOWN_TECHNOLOGIES
    return {_technology_key(value) for value in aliases if _identifier_present(text, value)}


def _normalized_anchor_projection(value: object, field_name: str) -> JSONObject:
    anchors = _anchors(value, field_name)
    return {
        "numbers_and_units": _normalized_number_anchors(
            anchors["numbers_and_units"], f"{field_name}.numbers_and_units"
        ),
        "technology_identifiers": tuple(
            sorted(
                {
                    _technology_key(item)
                    for item in _string_list(
                        anchors["technology_identifiers"],
                        f"{field_name}.technology_identifiers",
                    )
                }
            )
        ),
        "implementation_status": _normalized_string_set(
            anchors["implementation_status"], f"{field_name}.implementation_status"
        ),
        "personal_attribution": _normalized_text(
            _text(anchors["personal_attribution"], f"{field_name}.personal_attribution")
        ),
        "role_anchor_ids": _normalized_string_set(
            anchors["role_anchor_ids"], f"{field_name}.role_anchor_ids"
        ),
        "outcome_anchor_ids": _normalized_string_set(
            anchors["outcome_anchor_ids"], f"{field_name}.outcome_anchor_ids"
        ),
    }


def _target_text(value: object, field_name: str) -> str:
    text = _text(value, field_name, maximum=MAX_TARGET_TEXT_CHARS)
    if any(
        (unicodedata.category(character) == "Cc" and character not in {"\n", "\t"})
        or character
        in {
            "\u202a",
            "\u202b",
            "\u202c",
            "\u202d",
            "\u202e",
            "\u2066",
            "\u2067",
            "\u2068",
            "\u2069",
        }
        for character in text
    ):
        raise InvalidInputError(f"{field_name} contains unsafe control text")
    return text


def _candidate_target(value: object, export_kind: str, field_name: str) -> JSONObject:
    target = _object(value, field_name)
    expected = {"text"} if export_kind == "resume" else {"question", "answer"}
    _exact_fields(target, expected, field_name)
    return {key: _target_text(target[key], f"{field_name}.{key}") for key in sorted(expected)}


def _combined_target_text(target: JSONObject) -> str:
    return "\n".join(str(value) for value in target.values())


def _validate_target_facts(target: JSONObject, anchors: JSONObject) -> None:
    text = _combined_target_text(target)
    expected_numbers = cast(tuple[str, ...], anchors["numbers_and_units"])
    if _numbers_in_target(text) != expected_numbers:
        raise InvalidInputError("translation target changes numeric or unit anchors")
    expected_technologies = set(cast(tuple[str, ...], anchors["technology_identifiers"]))
    if any(not _identifier_present(text, technology) for technology in expected_technologies):
        raise InvalidInputError("translation target omits a technology anchor")
    extras = _known_technologies_in_target(text) - expected_technologies
    if extras:
        raise InvalidInputError("translation target introduces a technology anchor")


def _validated_candidates(
    raw_value: object,
    source_items: list[JSONObject],
) -> list[JSONObject]:
    if not isinstance(raw_value, list) or len(raw_value) > MAX_SOURCE_ITEMS:
        raise InvalidInputError("translation candidate items must be a bounded list")
    source_by_id = {str(item["source_item_id"]): item for item in source_items}
    candidates: dict[str, JSONObject] = {}
    expected_fields = {
        "source_item_id",
        "export_kind",
        "claim_refs",
        "evidence_refs",
        "role_lens_refs",
        "anchors",
        "project_id",
        "module_id",
        "target",
    }
    for index, raw_candidate in enumerate(raw_value):
        candidate = _object(raw_candidate, f"TranslationExportRequest.items[{index}]")
        _exact_fields(candidate, expected_fields, f"TranslationExportRequest.items[{index}]")
        source_item_id = _text(candidate["source_item_id"], "candidate source_item_id")
        if source_item_id in candidates:
            raise InvalidInputError("translation candidate source_item_id values must be unique")
        source = source_by_id.get(source_item_id)
        if source is None:
            raise InvalidInputError("translation candidates contain an unknown source item")
        for key in (
            "export_kind",
            "claim_refs",
            "evidence_refs",
            "role_lens_refs",
            "project_id",
            "module_id",
        ):
            if candidate[key] != source[key]:
                raise InvalidInputError(f"translation candidate changes source mapping field {key}")
        source_anchors = _normalized_anchor_projection(source["anchors"], "source anchors")
        candidate_anchors = _normalized_anchor_projection(candidate["anchors"], "candidate anchors")
        if candidate_anchors != source_anchors:
            raise InvalidInputError("translation candidate changes structured fact anchors")
        target = _candidate_target(
            candidate["target"], str(source["export_kind"]), "translation target"
        )
        _validate_target_facts(target, source_anchors)
        candidates[source_item_id] = {
            "source_item_id": source_item_id,
            "export_kind": source["export_kind"],
            "claim_refs": source["claim_refs"],
            "evidence_refs": source["evidence_refs"],
            "role_lens_refs": source["role_lens_refs"],
            "anchors": source["anchors"],
            "project_id": source["project_id"],
            "module_id": source["module_id"],
            "target": target,
        }
    if set(candidates) != set(source_by_id):
        raise InvalidInputError("translation source and target item sets differ")
    return [candidates[source_item_id] for source_item_id in sorted(candidates)]


def _markdown_text(value: str) -> str:
    visible = " ".join(value.split())
    escaped = html.escape(visible, quote=False)
    return re.sub(r"([\\`*_{}\[\]()#+.!|>-])", r"\\\1", escaped)


def _render_resume(candidates: list[JSONObject], source_snapshot_id: str) -> bytes:
    lines = ["# English Resume Evidence Draft", "", f"Frozen source: `{source_snapshot_id}`", ""]
    for candidate in candidates:
        if candidate["export_kind"] != "resume":
            continue
        target = cast(JSONObject, candidate["target"])
        lines.append(f"- {_markdown_text(str(target['text']))}")
    if len(lines) == 4:
        lines.extend(["No resume source items were present in the frozen projection.", ""])
    return ("\n".join(lines) + "\n").encode("utf-8")


def _render_interview(candidates: list[JSONObject], source_snapshot_id: str) -> bytes:
    lines = ["# English Interview Q&A", "", f"Frozen source: `{source_snapshot_id}`", ""]
    number = 0
    for candidate in candidates:
        if candidate["export_kind"] != "interview_qa":
            continue
        number += 1
        target = cast(JSONObject, candidate["target"])
        lines.extend(
            [
                f"## Q{number}. {_markdown_text(str(target['question']))}",
                "",
                _markdown_text(str(target["answer"])),
                "",
            ]
        )
    if number == 0:
        lines.extend(["No interview source items were present in the frozen projection.", ""])
    return ("\n".join(lines) + "\n").encode("utf-8")


@dataclass(frozen=True)
class _ExportAttempt:
    export_attempt_id: str
    derived_export_id: str
    source_artifact_snapshot_id: str
    source_report_bundle_sha256: str
    source_projection_sha256: str
    started_at: str
    temp_relative_path: str
    final_relative_path: str


class _InjectedExportInterruption(RuntimeError):
    pass


@contextmanager
def _connection_transaction(connection: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    connection.execute("BEGIN IMMEDIATE")
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise


def _write_new_file_at(directory_fd: int, name: str, content: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    descriptor = os.open(name, flags, 0o600, dir_fd=directory_fd)
    try:
        view = memoryview(content)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("export write made no progress")
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class ExportService:
    """Prepare and publish one fact-preserving English export."""

    def __init__(self, database: Database) -> None:
        self._database = database

    def translate_export(
        self,
        *,
        workspace_path: Path,
        authorization_receipt_id: str,
        request_value: object,
        _fault_at: str | None = None,
    ) -> JSONObject:
        request = _object(request_value, "TranslationExportRequest")
        action = _text(request.get("action"), "TranslationExportRequest.action")
        if action not in {"prepare", "publish"}:
            raise InvalidInputError("translation export action must be prepare or publish")
        expected_fields = {
            "contract_version",
            "action",
            "source_artifact_snapshot_id",
            "target_language",
            "export_kinds",
        }
        if action == "publish":
            expected_fields |= {"source_projection_sha256", "items"}
        _exact_fields(
            request,
            expected_fields,
            "TranslationExportRequest",
        )
        if request["contract_version"] != TRANSLATION_EXPORT_REQUEST_CONTRACT_VERSION:
            raise InvalidInputError("unsupported TranslationExportRequest contract version")
        if request["target_language"] != "en":
            raise InvalidInputError("the first translation export version only supports English")
        if request["export_kinds"] != list(EXPORT_KINDS):
            raise InvalidInputError("the first translation export requires resume and interview_qa")
        snapshot_id = _text(request["source_artifact_snapshot_id"], "source_artifact_snapshot_id")
        canonical_workspace = workspace_path.expanduser().resolve(strict=False)
        if action == "prepare":
            if _fault_at is not None:
                raise InvalidInputError("fault injection is only valid for export publication")
            with self._database.read_connection() as connection:
                source = self._load_source(
                    connection,
                    canonical_workspace,
                    authorization_receipt_id,
                    snapshot_id,
                )
            return {"status": "ok", "translation_source": source}
        if _fault_at not in {
            None,
            "after_temp",
            "after_publish",
            "before_database_commit",
            "fail_after_temp",
        }:
            raise InvalidInputError("unsupported export publication fault point")
        with self._database.exclusive_writer_connection() as connection:
            source = self._load_source(
                connection,
                canonical_workspace,
                authorization_receipt_id,
                snapshot_id,
            )
            requested_projection = _text(
                request["source_projection_sha256"], "source_projection_sha256", maximum=64
            )
            if requested_projection != source["source_projection_sha256"]:
                raise InvalidInputError(
                    "translation request no longer matches its frozen projection"
                )
            candidates = _validated_candidates(
                request["items"], cast(list[JSONObject], source["items"])
            )
            self._recover_interrupted_attempts(connection)
            return self._publish(
                connection,
                source,
                candidates,
                _fault_at=_fault_at,
            )

    def _load_source(
        self,
        connection: sqlite3.Connection,
        canonical_workspace: Path,
        authorization_receipt_id: str,
        snapshot_id: str,
    ) -> JSONObject:
        PreparationService._require_source_receipt(
            connection,
            authorization_receipt_id,
            canonical_workspace,
        )
        metadata, html_bytes = ArtifactSnapshotService(
            self._database
        ).read_verified_html_from_connection(connection, snapshot_id)
        if metadata["canonical_workspace_root"] != str(canonical_workspace):
            raise InvalidInputError("source ArtifactSnapshot belongs to another workspace")
        bundle = _report_bundle_from_html(html_bytes)
        bundle_sha256 = _text(bundle.get("bundle_sha256"), "ReportBundle bundle_sha256", maximum=64)
        if (
            bundle_sha256 != metadata["report_bundle_sha256"]
            or report_bundle_sha256(bundle) != bundle_sha256
        ):
            raise InvalidInputError("source ArtifactSnapshot and ReportBundle identity differ")
        projection_sha256, items = _projection_items(bundle)
        return {
            "contract_version": TRANSLATION_EXPORT_SOURCE_CONTRACT_VERSION,
            "source_artifact_snapshot_id": snapshot_id,
            "source_report_bundle_sha256": bundle_sha256,
            "source_projection_sha256": projection_sha256,
            "target_language": "en",
            "export_kinds": list(EXPORT_KINDS),
            "items": items,
        }

    def _publish(
        self,
        connection: sqlite3.Connection,
        source: JSONObject,
        candidates: list[JSONObject],
        *,
        _fault_at: str | None,
    ) -> JSONObject:
        attempt = self._start_attempt(connection, source)
        try:
            rendered = {
                RESUME_FILENAME: _render_resume(candidates, attempt.source_artifact_snapshot_id),
                INTERVIEW_FILENAME: _render_interview(
                    candidates, attempt.source_artifact_snapshot_id
                ),
            }
            file_hashes = {
                name: hashlib.sha256(content).hexdigest() for name, content in rendered.items()
            }
            manifest = self._manifest(attempt, candidates, file_hashes)
            manifest_bytes = _canonical_bytes(manifest) + b"\n"
            manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
            self._publish_directory(
                attempt,
                {**rendered, MANIFEST_FILENAME: manifest_bytes},
                manifest,
                _fault_at=_fault_at,
            )
            if _fault_at in {"after_publish", "before_database_commit"}:
                raise _InjectedExportInterruption(
                    "injected export interruption after directory publication"
                )
            self._record_success(connection, attempt, manifest_sha256)
        except _InjectedExportInterruption:
            raise
        except Exception as exc:
            self._record_failure_if_running(connection, attempt, exc)
            self._cleanup_failed_attempt(connection, attempt)
            if isinstance(exc, InvalidInputError):
                raise
            raise InvalidInputError(
                "English export publication failed; inspect the ExportAttempt diagnostic"
            ) from exc
        output = self._safe_export_path(attempt.final_relative_path)
        return {
            "status": "ok",
            "derived_export": {
                "derived_export_id": attempt.derived_export_id,
                "export_attempt_id": attempt.export_attempt_id,
                "source_artifact_snapshot_id": attempt.source_artifact_snapshot_id,
                "source_report_bundle_sha256": attempt.source_report_bundle_sha256,
                "source_projection_sha256": attempt.source_projection_sha256,
                "language": "en",
                "export_kinds": list(EXPORT_KINDS),
                "manifest_sha256": manifest_sha256,
                "output_path": str(output),
                "resume_markdown_path": str(output / RESUME_FILENAME),
                "interview_qa_markdown_path": str(output / INTERVIEW_FILENAME),
                "manifest_path": str(output / MANIFEST_FILENAME),
                "created_at": attempt.started_at,
            },
        }

    def _start_attempt(
        self,
        connection: sqlite3.Connection,
        source: JSONObject,
    ) -> _ExportAttempt:
        export_attempt_id = str(uuid.uuid4())
        derived_export_id = str(uuid.uuid4())
        attempt = _ExportAttempt(
            export_attempt_id=export_attempt_id,
            derived_export_id=derived_export_id,
            source_artifact_snapshot_id=str(source["source_artifact_snapshot_id"]),
            source_report_bundle_sha256=str(source["source_report_bundle_sha256"]),
            source_projection_sha256=str(source["source_projection_sha256"]),
            started_at=_now(),
            temp_relative_path=f"exports/.tmp/{export_attempt_id}",
            final_relative_path=f"exports/{derived_export_id}",
        )
        self._attempt_relative(attempt, "temp")
        self._attempt_relative(attempt, "final")
        with _connection_transaction(connection):
            connection.execute(
                """
                INSERT INTO export_attempts(
                    export_attempt_id, derived_export_id,
                    source_artifact_snapshot_id, source_projection_sha256,
                    generator_version, owner_process_identity,
                    temp_relative_path, final_relative_path, started_at, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'running')
                """,
                (
                    attempt.export_attempt_id,
                    attempt.derived_export_id,
                    attempt.source_artifact_snapshot_id,
                    attempt.source_projection_sha256,
                    GENERATOR_VERSION,
                    process_identity(),
                    attempt.temp_relative_path,
                    attempt.final_relative_path,
                    attempt.started_at,
                ),
            )
        return attempt

    @staticmethod
    def _manifest(
        attempt: _ExportAttempt,
        candidates: list[JSONObject],
        file_hashes: dict[str, str],
    ) -> JSONObject:
        return {
            "contract_version": DERIVED_EXPORT_MANIFEST_CONTRACT_VERSION,
            "derived_export_id": attempt.derived_export_id,
            "export_attempt_id": attempt.export_attempt_id,
            "source_artifact_snapshot_id": attempt.source_artifact_snapshot_id,
            "source_report_bundle_sha256": attempt.source_report_bundle_sha256,
            "source_projection_sha256": attempt.source_projection_sha256,
            "language": "en",
            "export_kinds": list(EXPORT_KINDS),
            "generator_version": GENERATOR_VERSION,
            "created_at": attempt.started_at,
            "items": [
                {
                    "source_item_id": candidate["source_item_id"],
                    "export_kind": candidate["export_kind"],
                    "claim_refs": candidate["claim_refs"],
                    "evidence_refs": candidate["evidence_refs"],
                    "role_lens_refs": candidate["role_lens_refs"],
                    "project_id": candidate["project_id"],
                    "module_id": candidate["module_id"],
                    "anchors_sha256": _sha256_value(candidate["anchors"]),
                    "target_sha256": _sha256_value(candidate["target"]),
                    "output_file": (
                        RESUME_FILENAME
                        if candidate["export_kind"] == "resume"
                        else INTERVIEW_FILENAME
                    ),
                }
                for candidate in candidates
            ],
            "files": [
                {"path": name, "sha256": file_hashes[name]}
                for name in (RESUME_FILENAME, INTERVIEW_FILENAME)
            ],
        }

    def _record_success(
        self,
        connection: sqlite3.Connection,
        attempt: _ExportAttempt,
        manifest_sha256: str,
    ) -> None:
        timestamp = _now()
        with _connection_transaction(connection):
            row = connection.execute(
                """
                SELECT status, source_artifact_snapshot_id, source_projection_sha256
                FROM export_attempts WHERE export_attempt_id = ?
                """,
                (attempt.export_attempt_id,),
            ).fetchone()
            if (
                row is None
                or str(row["status"]) != "running"
                or str(row["source_artifact_snapshot_id"]) != attempt.source_artifact_snapshot_id
                or str(row["source_projection_sha256"]) != attempt.source_projection_sha256
            ):
                raise InvalidInputError("ExportAttempt is no longer publishable")
            updated = connection.execute(
                """
                UPDATE export_attempts
                SET status = 'succeeded', finished_at = ?, error_summary = NULL
                WHERE export_attempt_id = ? AND status = 'running'
                """,
                (timestamp, attempt.export_attempt_id),
            )
            if updated.rowcount != 1:
                raise InvalidInputError("ExportAttempt status changed during publication")
            connection.execute(
                """
                INSERT INTO derived_exports(
                    derived_export_id, export_attempt_id,
                    source_artifact_snapshot_id, source_report_bundle_sha256,
                    source_projection_sha256, language, export_kinds,
                    manifest_sha256, output_path, created_at
                ) VALUES (?, ?, ?, ?, ?, 'en', ?, ?, ?, ?)
                """,
                (
                    attempt.derived_export_id,
                    attempt.export_attempt_id,
                    attempt.source_artifact_snapshot_id,
                    attempt.source_report_bundle_sha256,
                    attempt.source_projection_sha256,
                    _canonical_bytes(list(EXPORT_KINDS)).decode("utf-8"),
                    manifest_sha256,
                    attempt.final_relative_path,
                    timestamp,
                ),
            )

    def _record_failure_if_running(
        self,
        connection: sqlite3.Connection,
        attempt: _ExportAttempt,
        exc: Exception,
    ) -> None:
        summary = f"{type(exc).__name__}: {str(exc)}"[:500]
        with _connection_transaction(connection):
            if (
                connection.execute(
                    "SELECT 1 FROM derived_exports WHERE export_attempt_id = ?",
                    (attempt.export_attempt_id,),
                ).fetchone()
                is not None
            ):
                return
            connection.execute(
                """
                UPDATE export_attempts
                SET status = 'failed', finished_at = ?, error_summary = ?
                WHERE export_attempt_id = ? AND status = 'running'
                """,
                (_now(), summary, attempt.export_attempt_id),
            )

    def _recover_interrupted_attempts(self, connection: sqlite3.Connection) -> None:
        recovered: list[_ExportAttempt] = []
        with _connection_transaction(connection):
            rows = connection.execute(
                """
                SELECT ea.export_attempt_id, ea.derived_export_id,
                       ea.source_artifact_snapshot_id, a.report_bundle_sha256,
                       ea.source_projection_sha256, ea.started_at,
                       ea.temp_relative_path, ea.final_relative_path,
                       ea.owner_process_identity, ea.status
                FROM export_attempts AS ea
                JOIN artifact_snapshots AS a
                  ON a.artifact_snapshot_id = ea.source_artifact_snapshot_id
                LEFT JOIN derived_exports AS de
                  ON de.export_attempt_id = ea.export_attempt_id
                WHERE ea.status IN ('running', 'failed', 'interrupted')
                  AND de.derived_export_id IS NULL
                ORDER BY ea.started_at, ea.export_attempt_id
                """
            ).fetchall()
            for row in rows:
                status = str(row["status"])
                if status == "running" and not owner_process_stopped(
                    str(row["owner_process_identity"])
                ):
                    continue
                attempt = _ExportAttempt(
                    export_attempt_id=str(row["export_attempt_id"]),
                    derived_export_id=str(row["derived_export_id"]),
                    source_artifact_snapshot_id=str(row["source_artifact_snapshot_id"]),
                    source_report_bundle_sha256=str(row["report_bundle_sha256"]),
                    source_projection_sha256=str(row["source_projection_sha256"]),
                    started_at=str(row["started_at"]),
                    temp_relative_path=str(row["temp_relative_path"]),
                    final_relative_path=str(row["final_relative_path"]),
                )
                self._attempt_relative(attempt, "temp")
                self._attempt_relative(attempt, "final")
                recovered.append(attempt)
                if status == "running":
                    connection.execute(
                        """
                        UPDATE export_attempts
                        SET status = 'interrupted', finished_at = ?,
                            error_summary = 'owner process stopped before publication completed'
                        WHERE export_attempt_id = ? AND status = 'running'
                        """,
                        (_now(), attempt.export_attempt_id),
                    )
        for attempt in recovered:
            self._cleanup_failed_attempt(connection, attempt)

    def _cleanup_failed_attempt(
        self,
        connection: sqlite3.Connection,
        attempt: _ExportAttempt,
    ) -> None:
        self._remove_owned_relative(self._attempt_relative(attempt, "temp"))
        if (
            connection.execute(
                "SELECT 1 FROM derived_exports WHERE export_attempt_id = ?",
                (attempt.export_attempt_id,),
            ).fetchone()
            is None
        ):
            self._remove_owned_relative(self._attempt_relative(attempt, "final"))

    def _publish_directory(
        self,
        attempt: _ExportAttempt,
        rendered_files: dict[str, bytes],
        manifest: JSONObject,
        *,
        _fault_at: str | None,
    ) -> None:
        temp_relative = self._attempt_relative(attempt, "temp")
        final_relative = self._attempt_relative(attempt, "final")
        with (
            self._open_export_parent(temp_relative) as (temp_parent_fd, temp_name),
            self._open_export_parent(final_relative) as (final_parent_fd, final_name),
        ):
            for directory_fd, name in (
                (temp_parent_fd, temp_name),
                (final_parent_fd, final_name),
            ):
                try:
                    os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                except FileNotFoundError:
                    continue
                raise InvalidInputError("export attempt path already exists")
            try:
                os.mkdir(temp_name, mode=0o700, dir_fd=temp_parent_fd)
                directory_flags = (
                    os.O_RDONLY
                    | getattr(os, "O_DIRECTORY", 0)
                    | getattr(os, "O_NOFOLLOW", 0)
                    | getattr(os, "O_CLOEXEC", 0)
                )
                temp_fd = os.open(temp_name, directory_flags, dir_fd=temp_parent_fd)
                try:
                    for name in (RESUME_FILENAME, INTERVIEW_FILENAME, MANIFEST_FILENAME):
                        _write_new_file_at(temp_fd, name, rendered_files[name])
                    os.fsync(temp_fd)
                finally:
                    os.close(temp_fd)
                self._verify_rendered_files(temp_relative, manifest)
                if _fault_at == "after_temp":
                    raise _InjectedExportInterruption(
                        "injected export interruption after temporary files"
                    )
                if _fault_at == "fail_after_temp":
                    raise RuntimeError("injected normal export failure after temporary files")
                os.rename(
                    temp_name,
                    final_name,
                    src_dir_fd=temp_parent_fd,
                    dst_dir_fd=final_parent_fd,
                )
                os.fsync(temp_parent_fd)
                os.fsync(final_parent_fd)
                final_fd = os.open(final_name, directory_flags, dir_fd=final_parent_fd)
                try:
                    for name in rendered_files:
                        flags = (
                            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
                        )
                        file_fd = os.open(name, flags, dir_fd=final_fd)
                        try:
                            if not stat.S_ISREG(os.fstat(file_fd).st_mode):
                                raise InvalidInputError("published export is not a regular file")
                            os.fchmod(file_fd, stat.S_IRUSR)
                            os.fsync(file_fd)
                        finally:
                            os.close(file_fd)
                    os.fchmod(final_fd, stat.S_IRUSR | stat.S_IXUSR)
                    os.fsync(final_fd)
                finally:
                    os.close(final_fd)
            except OSError as exc:
                raise InvalidInputError("export directory publication failed") from exc

    def _verify_rendered_files(self, relative: str, manifest: JSONObject) -> None:
        expected_set = {RESUME_FILENAME, INTERVIEW_FILENAME, MANIFEST_FILENAME}
        if self._list_directory_relative(relative) != expected_set:
            raise InvalidInputError("export directory contains an unexpected file set")
        expected_hashes = {
            str(item["path"]): str(item["sha256"])
            for item in cast(list[JSONObject], manifest["files"])
        }
        if set(expected_hashes) != {RESUME_FILENAME, INTERVIEW_FILENAME}:
            raise InvalidInputError("export manifest file set is incomplete")
        for name, expected_hash in expected_hashes.items():
            if (
                hashlib.sha256(self._read_regular_relative(f"{relative}/{name}")).hexdigest()
                != expected_hash
            ):
                raise InvalidInputError("rendered export hash does not match manifest")
        manifest_bytes = self._read_regular_relative(f"{relative}/{MANIFEST_FILENAME}")
        try:
            parsed = json.loads(manifest_bytes, parse_constant=_reject_json_constant)
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError) as exc:
            raise InvalidInputError("rendered export manifest is invalid") from exc
        if parsed != manifest or manifest_bytes != _canonical_bytes(manifest) + b"\n":
            raise InvalidInputError("export manifest is not canonical to rendered state")

    @staticmethod
    def _attempt_relative(attempt: _ExportAttempt, kind: str) -> str:
        relative = {
            "temp": attempt.temp_relative_path,
            "final": attempt.final_relative_path,
        }.get(kind)
        if relative is None:
            raise AssertionError(f"unknown export attempt path kind: {kind}")
        expected = {
            "temp": ("exports", ".tmp", attempt.export_attempt_id),
            "final": ("exports", attempt.derived_export_id),
        }[kind]
        pure = PurePosixPath(relative)
        if pure.is_absolute() or pure.parts != expected:
            raise InvalidInputError("ExportAttempt path is outside its registered ownership")
        return relative

    @staticmethod
    def _relative_parts(relative: str) -> tuple[str, ...]:
        pure = PurePosixPath(relative)
        if (
            pure.is_absolute()
            or not pure.parts
            or pure.parts[0] != "exports"
            or any(part in {"", ".", ".."} for part in pure.parts)
        ):
            raise InvalidInputError("export path is outside the personal data directory")
        return pure.parts

    def _safe_export_path(self, relative: str) -> Path:
        return self._database.paths.root.joinpath(*self._relative_parts(relative))

    @contextmanager
    def _open_export_parent(self, relative: str) -> Iterator[tuple[int, str]]:
        parts = self._relative_parts(relative)
        flags = (
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0)
        )
        descriptor = -1
        try:
            descriptor = os.open(self._database.paths.root, flags)
            for component in parts[:-1]:
                next_descriptor = os.open(component, flags, dir_fd=descriptor)
                os.close(descriptor)
                descriptor = next_descriptor
            yield descriptor, parts[-1]
        except OSError as exc:
            raise InvalidInputError(
                "export path contains an unavailable or linked directory"
            ) from exc
        finally:
            if descriptor >= 0:
                os.close(descriptor)

    def _read_regular_relative(self, relative: str) -> bytes:
        with self._open_export_parent(relative) as (directory_fd, name):
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
            try:
                descriptor = os.open(name, flags, dir_fd=directory_fd)
            except OSError as exc:
                raise InvalidInputError("export file is unavailable or linked") from exc
            try:
                if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                    raise InvalidInputError("export file is not a regular file")
                chunks: list[bytes] = []
                while chunk := os.read(descriptor, 1024 * 1024):
                    chunks.append(chunk)
                return b"".join(chunks)
            finally:
                os.close(descriptor)

    def _list_directory_relative(self, relative: str) -> set[str]:
        with self._open_export_parent(relative) as (directory_fd, name):
            flags = (
                os.O_RDONLY
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_CLOEXEC", 0)
            )
            try:
                descriptor = os.open(name, flags, dir_fd=directory_fd)
            except OSError as exc:
                raise InvalidInputError("export directory is unavailable or linked") from exc
            try:
                return set(os.listdir(descriptor))
            finally:
                os.close(descriptor)

    def _remove_owned_relative(self, relative: str) -> None:
        parts = self._relative_parts(relative)
        if parts in {("exports",), ("exports", ".tmp")}:
            raise InvalidInputError("refusing to remove a protected export ancestor")
        with self._open_export_parent(relative) as (directory_fd, name):
            self._remove_entry_at(directory_fd, name)
            os.fsync(directory_fd)

    @staticmethod
    def _remove_entry_at(directory_fd: int, name: str) -> None:
        try:
            entry = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            return
        if not stat.S_ISDIR(entry.st_mode):
            os.unlink(name, dir_fd=directory_fd)
            return
        flags = (
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0)
        )
        child_fd = os.open(name, flags, dir_fd=directory_fd)
        try:
            os.fchmod(child_fd, stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
            for child_name in os.listdir(child_fd):
                ExportService._remove_entry_at(child_fd, child_name)
        finally:
            os.close(child_fd)
        os.rmdir(name, dir_fd=directory_fd)

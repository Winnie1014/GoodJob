"""Read owner-local scan configuration without mutating it."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal, cast

from goodjob.source_io import open_absolute_regular_file, read_open_file

ProjectMatchKind = Literal["identity_key", "relative_location"]
MAX_CONFIG_FILE_BYTES = 256 * 1024


@dataclass(frozen=True)
class ConfigIssue:
    """A recoverable problem in the personal configuration file."""

    kind: str
    message: str
    remediation: str


@dataclass(frozen=True)
class ExcludedProjectRule:
    """One validated, ordered project exclusion rule."""

    index: int
    match: ProjectMatchKind
    value: str
    reason: str
    normalized_value: str

    def matches(self, *, identity_key: str, relative_location: str) -> bool:
        candidate = (
            identity_key
            if self.match == "identity_key"
            else normalize_relative_location(relative_location)
        )
        return candidate == self.normalized_value


@dataclass(frozen=True)
class ProjectExclusionConfig:
    """Validated rules plus recoverable issues from one file read."""

    rules: tuple[ExcludedProjectRule, ...]
    issues: tuple[ConfigIssue, ...]


def normalize_relative_location(value: str) -> str:
    """Normalize lexical dot components while retaining exact path spelling otherwise."""
    return PurePosixPath(value).as_posix()


def load_project_exclusions(config_file: Path) -> ProjectExclusionConfig:
    """Read project exclusions once; malformed entries do not discard valid siblings."""
    try:
        file_fd, _ = open_absolute_regular_file(config_file)
        try:
            content = read_open_file(file_fd, maximum_bytes=MAX_CONFIG_FILE_BYTES)
        finally:
            os.close(file_fd)
        text = content.decode("utf-8")
    except (OSError, UnicodeError):
        return ProjectExclusionConfig(
            (),
            (
                ConfigIssue(
                    "project_exclusion_config_unreadable",
                    "The owner-local configuration is not a bounded regular UTF-8 file.",
                    (
                        "Restore a readable regular config.toml without symbolic links and run "
                        "scan or refresh again."
                    ),
                ),
            ),
        )
    try:
        document = tomllib.loads(text)
    except (tomllib.TOMLDecodeError, RecursionError):
        return ProjectExclusionConfig(
            (),
            (
                ConfigIssue(
                    "project_exclusion_config_invalid",
                    "The owner-local configuration is not valid TOML.",
                    "Fix config.toml syntax; project scanning continued without exclusion rules.",
                ),
            ),
        )

    goodjob = document.get("goodjob", {})
    if not isinstance(goodjob, dict):
        return _invalid_collection("goodjob must be a TOML table")
    raw_rules = goodjob.get("excluded_projects", [])
    if not isinstance(raw_rules, list):
        return _invalid_collection("goodjob.excluded_projects must be an array of tables")

    rules: list[ExcludedProjectRule] = []
    issues: list[ConfigIssue] = []
    for index, value in enumerate(raw_rules, start=1):
        if not isinstance(value, dict):
            issues.append(_invalid_rule(index, "entry must be a TOML table"))
            continue
        match = value.get("match")
        rule_value = value.get("value")
        reason = value.get("reason")
        invalid_fields: list[str] = []
        if match not in {"identity_key", "relative_location"}:
            invalid_fields.append("match")
        if not isinstance(rule_value, str) or not rule_value.strip():
            invalid_fields.append("value")
        if not isinstance(reason, str) or not reason.strip():
            invalid_fields.append("reason")
        if invalid_fields:
            issues.append(_invalid_rule(index, f"invalid fields: {', '.join(invalid_fields)}"))
            continue
        assert isinstance(match, str)
        assert isinstance(rule_value, str)
        assert isinstance(reason, str)
        normalized = (
            normalize_relative_location(rule_value) if match == "relative_location" else rule_value
        )
        rules.append(
            ExcludedProjectRule(
                index,
                cast(ProjectMatchKind, match),
                rule_value,
                reason,
                normalized,
            )
        )
    return ProjectExclusionConfig(tuple(rules), tuple(issues))


def _invalid_collection(detail: str) -> ProjectExclusionConfig:
    return ProjectExclusionConfig(
        (),
        (
            ConfigIssue(
                "project_exclusion_config_invalid",
                f"The owner-local project exclusion configuration is invalid: {detail}.",
                "Use [[goodjob.excluded_projects]] tables and run scan or refresh again.",
            ),
        ),
    )


def _invalid_rule(index: int, detail: str) -> ConfigIssue:
    return ConfigIssue(
        "project_exclusion_rule_invalid",
        f"Project exclusion rule {index} is invalid: {detail}.",
        "Fix or remove that rule; other valid project exclusions still apply.",
    )

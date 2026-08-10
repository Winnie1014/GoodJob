from __future__ import annotations

import pytest

from goodjob.scanner import WorkspaceScanner

# Frozen by hand from scanner.py at 3a81d9d; keep independent of production constants.
BASELINE_SENSITIVE_FILENAMES = (
    ".env",
    ".envrc",
    ".netrc",
    ".npmrc",
    ".pypirc",
    "auth.json",
    "credentials",
    "credentials.json",
    "credentials.yaml",
    "credentials.yml",
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
    "id_rsa",
    "secret.json",
    "secret.yaml",
    "secret.yml",
    "secrets.json",
    "secrets.yaml",
    "secrets.yml",
    "tokens.json",
    "server.key",
    "server.p12",
    "server.pem",
    "server.pfx",
)


@pytest.mark.parametrize("filename", ("production.env", "local.env", "secrets.env"))
def test_env_suffix_names_are_sensitive(filename: str) -> None:
    assert WorkspaceScanner._is_sensitive(filename) is True


@pytest.mark.parametrize("filename", BASELINE_SENSITIVE_FILENAMES)
def test_every_baseline_sensitive_filename_remains_sensitive(filename: str) -> None:
    assert WorkspaceScanner._is_sensitive(filename) is True


@pytest.mark.parametrize(
    "filename",
    (
        pytest.param(".env", id="exact-lower"),
        pytest.param(".ENV", id="exact-case-variant"),
        pytest.param(".env.production", id="prefix-lower"),
        pytest.param(".ENV.PRODUCTION", id="prefix-case-variant"),
        pytest.param("id_rsa", id="name-set-lower"),
        pytest.param("ID_RSA", id="name-set-case-variant"),
        pytest.param("server.pem", id="suffix-lower"),
        pytest.param("SERVER.PEM", id="suffix-case-variant"),
    ),
)
def test_each_sensitive_rule_is_case_insensitive(filename: str) -> None:
    assert WorkspaceScanner._is_sensitive(filename) is True


@pytest.mark.parametrize(
    "filename",
    ("secretary.md", ".envoy", "environment.md", "env.example", "README.env.md"),
)
def test_similar_non_sensitive_names_remain_allowed(filename: str) -> None:
    assert WorkspaceScanner._is_sensitive(filename) is False

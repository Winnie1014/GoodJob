from __future__ import annotations

from pathlib import Path

import pytest

from goodjob.errors import InvalidInputError
from goodjob.safe_fs import SafeDataTree


def test_safe_data_tree_always_protects_prefix_and_declared_ancestors(
    tmp_path: Path,
) -> None:
    artifacts = tmp_path / "artifacts"
    temporary = artifacts / ".tmp"
    temporary.mkdir(parents=True)
    tree = SafeDataTree(
        tmp_path,
        "artifacts",
        "artifact",
        frozenset({("artifacts", ".tmp")}),
    )

    with pytest.raises(InvalidInputError, match="protected artifact ancestor"):
        tree.remove("artifacts")
    with pytest.raises(InvalidInputError, match="protected artifact ancestor"):
        tree.remove("artifacts/.tmp")

    assert artifacts.is_dir()
    assert temporary.is_dir()


def test_safe_data_tree_rejects_an_out_of_prefix_protection_policy(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="protected data-tree ancestors"):
        SafeDataTree(
            tmp_path,
            "artifacts",
            "artifact",
            frozenset({("exports",)}),
        )

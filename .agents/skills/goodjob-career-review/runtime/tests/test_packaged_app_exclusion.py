from __future__ import annotations

import sqlite3
from pathlib import Path

from goodjob.auth import AuthorizationRepository, AuthorizationRequest, generate_capability
from goodjob.db import Database
from goodjob.paths import DataPaths
from goodjob.scanner import (
    WorkspaceScanner,
    _is_packaged_app_root,
    _safe_history_path,
)

NOTICE_VERSION = "goodjob-source-analysis-v1"


def _scope(workspace: Path) -> dict[str, object]:
    return {
        "workspace_path": str(workspace.resolve()),
        "allowed_categories": ["source_analysis"],
    }


def _direct_scanner(data_dir: Path, workspace: Path) -> tuple[WorkspaceScanner, str]:
    database = Database(DataPaths(data_dir))
    capability = generate_capability()
    request = AuthorizationRequest.from_values(
        receipt_kind="source_analysis",
        scope=_scope(workspace),
        notice_version=NOTICE_VERSION,
    )
    receipt = AuthorizationRepository(database).issue(
        capability=capability,
        request=request,
    )
    return WorkspaceScanner(database), receipt.authorization_receipt_id


# ---------------------------------------------------------------------------
# Unit tests for _is_packaged_app_root
# ---------------------------------------------------------------------------


class TestIsPackagedAppRoot:
    def test_app_suffix(self) -> None:
        assert _is_packaged_app_root("MyApp.app")
        assert _is_packaged_app_root("CodeRoute.app")
        assert _is_packaged_app_root("some-app.app")

    def test_framework_suffix(self) -> None:
        assert _is_packaged_app_root("MyFramework.framework")
        assert _is_packaged_app_root("AnotherThing.framework")

    def test_xcarchive_suffix(self) -> None:
        assert _is_packaged_app_root("MyArchive.xcarchive")
        assert _is_packaged_app_root("Build.xcarchive")

    def test_case_insensitive(self) -> None:
        assert _is_packaged_app_root("myapp.APP")
        assert _is_packaged_app_root("MyFramework.Framework")
        assert _is_packaged_app_root("MyArchive.XCARCHIVE")

    def test_negative_no_suffix(self) -> None:
        assert not _is_packaged_app_root("app")
        assert not _is_packaged_app_root("framework")
        assert not _is_packaged_app_root("xcarchive")

    def test_negative_wrong_suffix(self) -> None:
        assert not _is_packaged_app_root("myapp.app.bak")
        assert not _is_packaged_app_root("myapp.zip")
        assert not _is_packaged_app_root("regular_dir")

    def test_negative_existing_hard_excluded(self) -> None:
        assert not _is_packaged_app_root("node_modules")
        assert not _is_packaged_app_root("dist")
        assert not _is_packaged_app_root("target")
        assert not _is_packaged_app_root("build")


# ---------------------------------------------------------------------------
# D5a: _safe_history_path rejects paths through packaged app roots
# ---------------------------------------------------------------------------


class TestSafeHistoryPathPackagedApp:
    def test_app_bundle_path_rejected(self) -> None:
        assert not _safe_history_path("FooBar.app/Contents/Resources/deep/nested/file.py")

    def test_framework_path_rejected(self) -> None:
        assert not _safe_history_path("AnotherThing.framework/Versions/A/Headers/Types.h")

    def test_xcarchive_path_rejected(self) -> None:
        assert not _safe_history_path("MyArchive.xcarchive/Products/Applications/MyApp.app")

    def test_nested_app_bundle_rejected(self) -> None:
        assert not _safe_history_path("projects/SomeApp.app/Contents/Resources/config.json")

    def test_normal_path_still_accepted(self) -> None:
        assert _safe_history_path("src/main.py")
        assert _safe_history_path("projects/app/src/utils.py")

    def test_existing_hard_excluded_still_rejected(self) -> None:
        assert not _safe_history_path("node_modules/pkg/index.js")
        assert not _safe_history_path("dist/bundle.js")
        assert not _safe_history_path("target/release/binary")

    def test_file_inside_app_as_last_segment_still_checked(self) -> None:
        assert not _safe_history_path("FooBar.app/Info.plist")


# ---------------------------------------------------------------------------
# D1 + D4 + D6: Working tree scan excludes packaged app directories
# ---------------------------------------------------------------------------


def _create_workspace_with_app_bundle(
    workspace: Path,
    app_name: str,
    suffix: str,
) -> Path:
    project = workspace / "project"
    project.mkdir(parents=True)
    (project / "pyproject.toml").write_text("[project]\nname='test-project'\n", encoding="utf-8")
    (project / "main.py").write_text("print('hello')\n", encoding="utf-8")

    app_dir = project / f"{app_name}{suffix}"
    contents = app_dir / "Contents" / "Resources" / "deep" / "nested"
    contents.mkdir(parents=True)
    (contents / "file.py").write_text("# this should never be indexed\n", encoding="utf-8")
    (contents / "config.json").write_text('{"secret": "should_not_persist"}\n', encoding="utf-8")

    if suffix == ".framework":
        versions = app_dir / "Versions" / "A"
        versions.mkdir(parents=True)
        (versions / "module.swift").write_text("public struct Module {}\n", encoding="utf-8")

    if suffix == ".xcarchive":
        products = app_dir / "Products" / "Applications"
        products.mkdir(parents=True)
        (products / "inner.txt").write_text("archive content\n", encoding="utf-8")

    return project


def _scan_and_collect(
    tmp_path: Path,
    workspace: Path,
) -> tuple[dict[str, object], set[str]]:
    data_dir = tmp_path / "data"
    scanner, receipt_id = _direct_scanner(data_dir, workspace)
    result = scanner.scan(
        workspace_path=str(workspace),
        config_revision="test-v1",
        authorization_receipt_id=receipt_id,
    )
    connection = sqlite3.connect(data_dir / "goodjob.sqlite3")
    stored_paths = {
        str(row[0]) for row in connection.execute("SELECT relative_path FROM source_artifacts")
    }
    connection.close()
    return result.coverage, stored_paths


class TestScanExcludesAppBundle:
    def test_app_bundle_excluded_from_scan(self, tmp_path: Path) -> None:
        workspace = tmp_path / "workspace"
        _create_workspace_with_app_bundle(workspace, "SomeApp", ".app")

        coverage, stored_paths = _scan_and_collect(tmp_path, workspace)

        excluded = coverage["excluded_by_category"]
        assert isinstance(excluded, dict)
        assert excluded["hard_excluded"] >= 1

        app_files = {p for p in stored_paths if "SomeApp.app" in p}
        assert app_files == set(), f"app bundle files leaked: {app_files}"

    def test_framework_excluded_from_scan(self, tmp_path: Path) -> None:
        workspace = tmp_path / "workspace"
        _create_workspace_with_app_bundle(workspace, "AnotherThing", ".framework")

        coverage, stored_paths = _scan_and_collect(tmp_path, workspace)

        excluded = coverage["excluded_by_category"]
        assert isinstance(excluded, dict)
        assert excluded["hard_excluded"] >= 1

        fw_files = {p for p in stored_paths if "AnotherThing.framework" in p}
        assert fw_files == set(), f"framework files leaked: {fw_files}"

    def test_xcarchive_excluded_from_scan(self, tmp_path: Path) -> None:
        workspace = tmp_path / "workspace"
        _create_workspace_with_app_bundle(workspace, "MyArchive", ".xcarchive")

        coverage, stored_paths = _scan_and_collect(tmp_path, workspace)

        excluded = coverage["excluded_by_category"]
        assert isinstance(excluded, dict)
        assert excluded["hard_excluded"] >= 1

        archive_files = {p for p in stored_paths if "MyArchive.xcarchive" in p}
        assert archive_files == set(), f"xcarchive files leaked: {archive_files}"


# ---------------------------------------------------------------------------
# D4: Generality proof - two different fictional project names
# ---------------------------------------------------------------------------


class TestGeneralityNoHardcoding:
    def test_two_different_app_names(self, tmp_path: Path) -> None:
        workspace = tmp_path / "workspace"
        project = workspace / "project"
        project.mkdir(parents=True)
        (project / "pyproject.toml").write_text(
            "[project]\nname='generality-test'\n", encoding="utf-8"
        )
        (project / "main.py").write_text("print('hello')\n", encoding="utf-8")

        foo = project / "FooBar.app" / "Contents" / "Resources"
        foo.mkdir(parents=True)
        (foo / "foo.py").write_text("print('foo')\n", encoding="utf-8")

        bar = project / "BazQux.framework" / "Versions" / "A"
        bar.mkdir(parents=True)
        (bar / "bar.swift").write_text("public struct Bar {}\n", encoding="utf-8")

        coverage, stored_paths = _scan_and_collect(tmp_path, workspace)

        excluded = coverage["excluded_by_category"]
        assert isinstance(excluded, dict)
        assert excluded["hard_excluded"] >= 2

        leaked = {p for p in stored_paths if "FooBar.app" in p or "BazQux.framework" in p}
        assert leaked == set(), f"packaged app files leaked: {leaked}"


# ---------------------------------------------------------------------------
# D5b: Working tree traversal (_iter_project_files) excludes packaged apps
# (covered by TestScanExcludesAppBundle above)
# D6: Pruning proof - directory not entered during traversal
# ---------------------------------------------------------------------------


class TestPruningProof:
    def test_app_directory_pruned_not_traversed(self, tmp_path: Path) -> None:
        workspace = tmp_path / "workspace"
        project = workspace / "project"
        project.mkdir(parents=True)
        (project / "pyproject.toml").write_text("[project]\nname='prune-test'\n", encoding="utf-8")
        (project / "main.py").write_text("print('hello')\n", encoding="utf-8")

        app_dir = project / "TestPrune.app" / "Contents" / "Resources" / "deep"
        app_dir.mkdir(parents=True)
        (app_dir / "nested.py").write_text("# should never be read\n", encoding="utf-8")
        (app_dir / "secret.json").write_text('{"key": "value"}\n', encoding="utf-8")

        coverage, stored_paths = _scan_and_collect(tmp_path, workspace)

        excluded = coverage["excluded_by_category"]
        assert isinstance(excluded, dict)
        hard_excluded_count = excluded["hard_excluded"]
        assert isinstance(hard_excluded_count, int)
        assert hard_excluded_count >= 1

        app_leaked = {p for p in stored_paths if "TestPrune.app" in p}
        assert app_leaked == set(), f"traversal entered .app directory: {app_leaked}"

    def test_multiple_app_bundles_all_pruned(self, tmp_path: Path) -> None:
        workspace = tmp_path / "workspace"
        project = workspace / "project"
        project.mkdir(parents=True)
        (project / "pyproject.toml").write_text("[project]\nname='multi-prune'\n", encoding="utf-8")
        (project / "main.py").write_text("print('hello')\n", encoding="utf-8")

        for name in ("Alpha", "Beta", "Gamma"):
            d = project / f"{name}.app" / "Contents" / "Resources"
            d.mkdir(parents=True)
            (d / f"{name.lower()}.py").write_text(f"# {name}\n", encoding="utf-8")

        coverage, stored_paths = _scan_and_collect(tmp_path, workspace)

        excluded = coverage["excluded_by_category"]
        assert isinstance(excluded, dict)
        assert excluded["hard_excluded"] >= 3

        leaked = {p for p in stored_paths if ".app/" in p}
        assert leaked == set(), f"files inside .app bundles leaked: {leaked}"


# ---------------------------------------------------------------------------
# D2: Zero regression - existing hard-excluded directories still work
# ---------------------------------------------------------------------------


class TestZeroRegression:
    def test_node_modules_still_excluded(self, tmp_path: Path) -> None:
        workspace = tmp_path / "workspace"
        project = workspace / "project"
        project.mkdir(parents=True)
        (project / "pyproject.toml").write_text(
            "[project]\nname='regression-test'\n", encoding="utf-8"
        )
        (project / "main.py").write_text("print('hello')\n", encoding="utf-8")
        nm = project / "node_modules" / "pkg"
        nm.mkdir(parents=True)
        (nm / "index.js").write_text("module.exports = {};\n", encoding="utf-8")

        coverage, stored_paths = _scan_and_collect(tmp_path, workspace)

        excluded = coverage["excluded_by_category"]
        assert isinstance(excluded, dict)
        assert excluded["hard_excluded"] >= 1

        nm_files = {p for p in stored_paths if "node_modules" in p}
        assert nm_files == set()

    def test_dist_and_target_still_excluded(self, tmp_path: Path) -> None:
        workspace = tmp_path / "workspace"
        project = workspace / "project"
        project.mkdir(parents=True)
        (project / "pyproject.toml").write_text(
            "[project]\nname='regression-dist'\n", encoding="utf-8"
        )
        (project / "main.py").write_text("print('hello')\n", encoding="utf-8")
        dist = project / "dist"
        dist.mkdir()
        (dist / "bundle.js").write_text("console.log('hi');\n", encoding="utf-8")
        target = project / "target" / "release"
        target.mkdir(parents=True)
        (target / "binary").write_bytes(b"\x00\x01\x02")

        coverage, stored_paths = _scan_and_collect(tmp_path, workspace)

        excluded = coverage["excluded_by_category"]
        assert isinstance(excluded, dict)
        assert excluded["hard_excluded"] >= 2

        leaked = {p for p in stored_paths if "dist/" in p or "target/" in p}
        assert leaked == set()

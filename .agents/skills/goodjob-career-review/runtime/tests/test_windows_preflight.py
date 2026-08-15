from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from goodjob.platform.preflight_windows import evaluate_windows_preflight


@dataclass
class FakeWindowsProbes:
    git_executable: Path | None = Path(r"C:\Program Files\Git\mingw64\bin\git.exe")
    filesystem: str = "NTFS"
    bfe: bool = True
    elevated: bool = True
    wfp_api: bool = True
    wfp_write: bool = True
    wfp_error: OSError | None = None
    runtime_importable: bool = True

    def trusted_git_executable(self) -> Path | None:
        return self.git_executable

    def workspace_filesystem(self, workspace: Path) -> str:
        del workspace
        return self.filesystem

    def bfe_is_running(self) -> bool:
        return self.bfe

    def is_elevated(self) -> bool:
        return self.elevated

    def wfp_api_is_available(self) -> bool:
        return self.wfp_api

    def wfp_policy_write_access(self) -> bool:
        if self.wfp_error is not None:
            raise self.wfp_error
        return self.wfp_write

    def runtime_modules_importable(self) -> bool:
        return self.runtime_importable


def _runtime_tree(tmp_path: Path) -> Path:
    runtime = tmp_path / "runtime"
    (runtime / "scripts").mkdir(parents=True)
    (runtime / "src" / "goodjob").mkdir(parents=True)
    (runtime / "scripts" / "launch_broker.py").write_text("# launcher\n", encoding="utf-8")
    (runtime / "scripts" / "session.py").write_text("# broker\n", encoding="utf-8")
    (runtime / "scripts" / "windows_preflight.py").write_text("# preflight\n", encoding="utf-8")
    (runtime / "src" / "goodjob" / "__init__.py").write_text("", encoding="utf-8")
    return runtime


def test_windows_preflight_classifies_every_failed_prerequisite(tmp_path: Path) -> None:
    runtime = _runtime_tree(tmp_path)
    probes = FakeWindowsProbes(
        git_executable=None,
        filesystem="ReFS",
        bfe=False,
        elevated=False,
        wfp_api=False,
    )

    report = evaluate_windows_preflight(
        workspace=tmp_path / "workspace",
        runtime_dir=runtime,
        python_version=None,
        launcher_kind=None,
        uv_available=False,
        release_enabled=True,
        probes=probes,
    ).as_dict()

    assert report["contract_version"] == "windows-prerequisite-preflight-v1"
    assert report["status"] == "error"
    assert report["can_start_broker"] is False
    failed = {
        check["id"]: check["code"] for check in report["checks"] if check["status"] == "failed"
    }
    assert failed == {
        "python_runtime": "missing_dependency",
        "trusted_git": "missing_dependency",
        "workspace_filesystem": "unsupported_capability",
        "bfe_service": "unsupported_capability",
        "administrator": "permission_required",
        "wfp_api": "unsupported_capability",
        "wfp_permission": "permission_required",
    }
    python_check = next(check for check in report["checks"] if check["id"] == "python_runtime")
    assert python_check["remediation"] == {
        "action": "request_installation",
        "purpose": "run the isolated GoodJob broker with Python 3.12 or newer",
        "source_url": "https://www.python.org/downloads/windows/",
        "requires_explicit_consent": True,
    }


def test_windows_preflight_retries_after_installation_and_elevation(tmp_path: Path) -> None:
    runtime = _runtime_tree(tmp_path)
    workspace = tmp_path / "workspace"
    probes = FakeWindowsProbes(git_executable=None, bfe=False, elevated=False)

    refused = evaluate_windows_preflight(
        workspace=workspace,
        runtime_dir=runtime,
        python_version=(3, 12, 8),
        launcher_kind="direct_python",
        uv_available=False,
        release_enabled=True,
        probes=probes,
    ).as_dict()

    assert refused["can_start_broker"] is False
    assert all(
        check.get("remediation", {}).get("requires_explicit_consent", False)
        for check in refused["checks"]
        if check["status"] == "failed"
    )

    probes.git_executable = Path(r"C:\Program Files\Git\mingw64\bin\git.exe")
    probes.bfe = True
    probes.elevated = True
    retried = evaluate_windows_preflight(
        workspace=workspace,
        runtime_dir=runtime,
        python_version=(3, 12, 8),
        launcher_kind="direct_python",
        uv_available=False,
        release_enabled=True,
        probes=probes,
    ).as_dict()

    assert retried["status"] == "ok"
    assert retried["can_start_broker"] is True
    assert {check["id"] for check in retried["checks"]} == {
        "python_runtime",
        "runtime_installation",
        "trusted_git",
        "workspace_filesystem",
        "bfe_service",
        "administrator",
        "wfp_api",
        "wfp_permission",
        "native_windows_release",
    }


def test_windows_preflight_requires_a_successful_wfp_policy_write_probe(tmp_path: Path) -> None:
    report = evaluate_windows_preflight(
        workspace=tmp_path / "workspace",
        runtime_dir=_runtime_tree(tmp_path),
        python_version=(3, 12, 8),
        launcher_kind="direct_python",
        uv_available=False,
        release_enabled=True,
        probes=FakeWindowsProbes(wfp_write=False),
    ).as_dict()

    permission = next(check for check in report["checks"] if check["id"] == "wfp_permission")
    assert permission["status"] == "failed"
    assert permission["code"] == "permission_required"
    assert permission["remediation"]["action"] == "request_elevation"


def test_windows_preflight_classifies_non_permission_wfp_failure_as_unsupported(
    tmp_path: Path,
) -> None:
    report = evaluate_windows_preflight(
        workspace=tmp_path / "workspace",
        runtime_dir=_runtime_tree(tmp_path),
        python_version=(3, 12, 8),
        launcher_kind="direct_python",
        uv_available=False,
        release_enabled=True,
        probes=FakeWindowsProbes(wfp_error=OSError("WFP policy store is unavailable")),
    ).as_dict()

    permission = next(check for check in report["checks"] if check["id"] == "wfp_permission")
    assert permission["status"] == "failed"
    assert permission["code"] == "unsupported_capability"
    assert permission["remediation"]["action"] == "repair_windows_or_use_wsl2"


def test_windows_preflight_rejects_an_unimportable_runtime(tmp_path: Path) -> None:
    report = evaluate_windows_preflight(
        workspace=tmp_path / "workspace",
        runtime_dir=_runtime_tree(tmp_path),
        python_version=(3, 12, 8),
        launcher_kind="direct_python",
        uv_available=False,
        release_enabled=True,
        probes=FakeWindowsProbes(runtime_importable=False),
    ).as_dict()

    runtime = next(check for check in report["checks"] if check["id"] == "runtime_installation")
    assert runtime["status"] == "failed"
    assert runtime["code"] == "missing_dependency"
    assert runtime["remediation"]["action"] == "request_reinstallation"


def test_windows_preflight_keeps_unreleased_runtime_closed_without_ipv6_requirement(
    tmp_path: Path,
) -> None:
    report = evaluate_windows_preflight(
        workspace=tmp_path / "workspace",
        runtime_dir=_runtime_tree(tmp_path),
        python_version=(3, 13, 1),
        launcher_kind="uv",
        uv_available=True,
        release_enabled=False,
        probes=FakeWindowsProbes(),
    ).as_dict()

    failed = [check for check in report["checks"] if check["status"] == "failed"]
    assert [(check["id"], check["code"]) for check in failed] == [
        ("native_windows_release", "unsupported_capability")
    ]
    assert all("ipv6" not in check["id"].lower() for check in report["checks"])
    assert report["can_start_broker"] is False

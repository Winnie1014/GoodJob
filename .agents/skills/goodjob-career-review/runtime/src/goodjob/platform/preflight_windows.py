"""Structured, fail-closed prerequisites for the native Windows runtime."""

from __future__ import annotations

import ctypes
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol, TypedDict

from goodjob.platform.handles_windows import load_windows_dll

PreflightCode = Literal["missing_dependency", "permission_required", "unsupported_capability"]
CheckStatus = Literal["passed", "failed"]


class PreflightRemediationDict(TypedDict, total=False):
    action: str
    purpose: str
    source_url: str
    requires_explicit_consent: bool


class PreflightCheckDict(TypedDict, total=False):
    id: str
    status: CheckStatus
    message: str
    code: PreflightCode
    remediation: PreflightRemediationDict


class WindowsPreflightReportDict(TypedDict):
    contract_version: str
    status: Literal["ok", "error"]
    can_start_broker: bool
    checks: list[PreflightCheckDict]
    notices: list[str]


class WindowsPrerequisiteProbes(Protocol):
    """Read-only host probes used before authorization or protected execution."""

    def trusted_git_executable(self) -> Path | None: ...

    def workspace_filesystem(self, workspace: Path) -> str: ...

    def bfe_is_running(self) -> bool: ...

    def is_elevated(self) -> bool: ...

    def wfp_api_is_available(self) -> bool: ...


class SERVICE_STATUS_PROCESS(ctypes.Structure):
    _fields_ = [
        ("dwServiceType", ctypes.c_uint32),
        ("dwCurrentState", ctypes.c_uint32),
        ("dwControlsAccepted", ctypes.c_uint32),
        ("dwWin32ExitCode", ctypes.c_uint32),
        ("dwServiceSpecificExitCode", ctypes.c_uint32),
        ("dwCheckPoint", ctypes.c_uint32),
        ("dwWaitHint", ctypes.c_uint32),
        ("dwProcessId", ctypes.c_uint32),
        ("dwServiceFlags", ctypes.c_uint32),
    ]


class SystemWindowsPrerequisiteProbes:
    """Read native Windows capability state without installing or enabling anything."""

    def trusted_git_executable(self) -> Path | None:
        from goodjob.platform.sandbox_windows import windows_git_candidates

        for candidate in windows_git_candidates():
            try:
                if not candidate.is_file():
                    continue
                resolved = candidate.resolve(strict=True)
            except OSError:
                continue
            tail = tuple(part.lower() for part in resolved.parts[-3:])
            if tail == ("mingw64", "bin", "git.exe"):
                return resolved
        return None

    def workspace_filesystem(self, workspace: Path) -> str:
        kernel32 = load_windows_dll("kernel32.dll")
        kernel32.GetVolumePathNameW.argtypes = [
            ctypes.c_wchar_p,
            ctypes.c_wchar_p,
            ctypes.c_uint32,
        ]
        kernel32.GetVolumePathNameW.restype = ctypes.c_int
        kernel32.GetVolumeInformationW.argtypes = [
            ctypes.c_wchar_p,
            ctypes.c_wchar_p,
            ctypes.c_uint32,
            ctypes.POINTER(ctypes.c_uint32),
            ctypes.POINTER(ctypes.c_uint32),
            ctypes.POINTER(ctypes.c_uint32),
            ctypes.c_wchar_p,
            ctypes.c_uint32,
        ]
        kernel32.GetVolumeInformationW.restype = ctypes.c_int
        volume = ctypes.create_unicode_buffer(32768)
        if not kernel32.GetVolumePathNameW(str(workspace), volume, len(volume)):
            raise OSError("unable to resolve the Windows workspace volume")
        filesystem = ctypes.create_unicode_buffer(32)
        if not kernel32.GetVolumeInformationW(
            volume.value,
            None,
            0,
            None,
            None,
            None,
            filesystem,
            len(filesystem),
        ):
            raise OSError("unable to identify the Windows workspace filesystem")
        return filesystem.value

    def bfe_is_running(self) -> bool:
        advapi32 = load_windows_dll("advapi32.dll")
        advapi32.OpenSCManagerW.argtypes = [
            ctypes.c_wchar_p,
            ctypes.c_wchar_p,
            ctypes.c_uint32,
        ]
        advapi32.OpenSCManagerW.restype = ctypes.c_void_p
        advapi32.OpenServiceW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_uint32]
        advapi32.OpenServiceW.restype = ctypes.c_void_p
        advapi32.QueryServiceStatusEx.argtypes = [
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.POINTER(ctypes.c_ubyte),
            ctypes.c_uint32,
            ctypes.POINTER(ctypes.c_uint32),
        ]
        advapi32.QueryServiceStatusEx.restype = ctypes.c_int
        advapi32.CloseServiceHandle.argtypes = [ctypes.c_void_p]
        advapi32.CloseServiceHandle.restype = ctypes.c_int
        manager = advapi32.OpenSCManagerW(None, None, 0x0001)
        if not manager:
            return False
        service = None
        try:
            service = advapi32.OpenServiceW(manager, "BFE", 0x0004)
            if not service:
                return False
            status = SERVICE_STATUS_PROCESS()
            needed = ctypes.c_uint32()
            ok = advapi32.QueryServiceStatusEx(
                service,
                0,
                ctypes.cast(ctypes.byref(status), ctypes.POINTER(ctypes.c_ubyte)),
                ctypes.sizeof(status),
                ctypes.byref(needed),
            )
            return bool(ok) and int(status.dwCurrentState) == 4
        finally:
            if service:
                advapi32.CloseServiceHandle(service)
            advapi32.CloseServiceHandle(manager)

    def is_elevated(self) -> bool:
        shell32 = load_windows_dll("shell32.dll")
        shell32.IsUserAnAdmin.argtypes = []
        shell32.IsUserAnAdmin.restype = ctypes.c_int
        return bool(shell32.IsUserAnAdmin())

    def wfp_api_is_available(self) -> bool:
        try:
            api = load_windows_dll("fwpuclnt.dll")
        except OSError:
            return False
        return all(
            hasattr(api, name) for name in ("FwpmEngineOpen0", "FwpmEngineClose0", "FwpmFilterAdd0")
        )


@dataclass(frozen=True)
class PreflightRemediation:
    action: str
    purpose: str
    source_url: str | None = None
    requires_explicit_consent: bool = False

    def as_dict(self) -> PreflightRemediationDict:
        result = PreflightRemediationDict(
            action=self.action,
            purpose=self.purpose,
            requires_explicit_consent=self.requires_explicit_consent,
        )
        if self.source_url is not None:
            result["source_url"] = self.source_url
        return result


@dataclass(frozen=True)
class PreflightCheck:
    id: str
    status: CheckStatus
    message: str
    code: PreflightCode | None = None
    remediation: PreflightRemediation | None = None

    def as_dict(self) -> PreflightCheckDict:
        result = PreflightCheckDict(id=self.id, status=self.status, message=self.message)
        if self.code is not None:
            result["code"] = self.code
        if self.remediation is not None:
            result["remediation"] = self.remediation.as_dict()
        return result


@dataclass(frozen=True)
class WindowsPreflightReport:
    checks: tuple[PreflightCheck, ...]

    @property
    def can_start_broker(self) -> bool:
        return all(check.status == "passed" for check in self.checks)

    def as_dict(self) -> WindowsPreflightReportDict:
        can_start = self.can_start_broker
        return WindowsPreflightReportDict(
            contract_version="windows-prerequisite-preflight-v1",
            status="ok" if can_start else "error",
            can_start_broker=can_start,
            checks=[check.as_dict() for check in self.checks],
            notices=[
                "Native Windows Git subprocesses do not have filesystem read isolation; "
                "use WSL2 when complete Git filesystem isolation is required."
            ],
        )


def missing_python_runtime_report() -> WindowsPreflightReport:
    """Return the report used when no child can run the full Windows probes."""
    return WindowsPreflightReport(
        (
            _failed(
                "python_runtime",
                "missing_dependency",
                "Python 3.12 or newer is unavailable; uv is optional and no usable fallback exists",
                PreflightRemediation(
                    action="request_installation",
                    purpose="run the isolated GoodJob broker with Python 3.12 or newer",
                    source_url="https://www.python.org/downloads/windows/",
                    requires_explicit_consent=True,
                ),
            ),
        )
    )


def _passed(check_id: str, message: str) -> PreflightCheck:
    return PreflightCheck(id=check_id, status="passed", message=message)


def _failed(
    check_id: str,
    code: PreflightCode,
    message: str,
    remediation: PreflightRemediation,
) -> PreflightCheck:
    return PreflightCheck(
        id=check_id,
        status="failed",
        code=code,
        message=message,
        remediation=remediation,
    )


def evaluate_windows_preflight(
    *,
    workspace: Path,
    runtime_dir: Path,
    python_version: tuple[int, int, int] | None,
    launcher_kind: str | None,
    uv_available: bool,
    release_enabled: bool,
    probes: WindowsPrerequisiteProbes,
) -> WindowsPreflightReport:
    """Evaluate all Windows prerequisites without starting the broker."""
    checks: list[PreflightCheck] = []
    if python_version is None or python_version < (3, 12, 0):
        checks.append(
            _failed(
                "python_runtime",
                "missing_dependency",
                "Python 3.12 or newer is unavailable; uv is optional and no usable fallback exists",
                PreflightRemediation(
                    action="request_installation",
                    purpose="run the isolated GoodJob broker with Python 3.12 or newer",
                    source_url="https://www.python.org/downloads/windows/",
                    requires_explicit_consent=True,
                ),
            )
        )
    else:
        method = launcher_kind or ("uv" if uv_available else "direct_python")
        checks.append(
            _passed(
                "python_runtime",
                f"Python {'.'.join(str(part) for part in python_version)} via {method}",
            )
        )

    runtime_files = (
        runtime_dir / "scripts" / "session.py",
        runtime_dir / "src" / "goodjob" / "__init__.py",
    )
    if all(path.is_file() for path in runtime_files):
        checks.append(_passed("runtime_installation", "GoodJob runtime files are complete"))
    else:
        checks.append(
            _failed(
                "runtime_installation",
                "missing_dependency",
                "the installed GoodJob Skill runtime is incomplete",
                PreflightRemediation(
                    action="request_reinstallation",
                    purpose="restore the trusted GoodJob runtime files",
                    requires_explicit_consent=True,
                ),
            )
        )

    try:
        git_executable = probes.trusted_git_executable()
    except OSError:
        git_executable = None
    if git_executable is None:
        checks.append(
            _failed(
                "trusted_git",
                "missing_dependency",
                r"Git for Windows is missing at a trusted mingw64\bin\git.exe path",
                PreflightRemediation(
                    action="request_installation",
                    purpose="read local Git metadata through the fixed trusted entry point",
                    source_url="https://git-scm.com/download/win",
                    requires_explicit_consent=True,
                ),
            )
        )
    else:
        checks.append(_passed("trusted_git", "trusted Git for Windows entry point is available"))

    try:
        filesystem = probes.workspace_filesystem(workspace)
    except OSError:
        filesystem = "unavailable"
    if filesystem.upper() != "NTFS":
        checks.append(
            _failed(
                "workspace_filesystem",
                "unsupported_capability",
                f"native Windows scanning requires NTFS; the workspace is on {filesystem}",
                PreflightRemediation(
                    action="use_ntfs_or_wsl2",
                    purpose="preserve handle-relative filesystem authorization",
                ),
            )
        )
    else:
        checks.append(_passed("workspace_filesystem", "workspace is on NTFS"))

    try:
        bfe_running = probes.bfe_is_running()
    except OSError:
        bfe_running = False
    if bfe_running:
        checks.append(_passed("bfe_service", "Base Filtering Engine is running"))
    else:
        checks.append(
            _failed(
                "bfe_service",
                "unsupported_capability",
                "Base Filtering Engine is not running",
                PreflightRemediation(
                    action="request_service_enablement",
                    purpose="establish the mandatory WFP network boundary",
                    requires_explicit_consent=True,
                ),
            )
        )

    try:
        elevated = probes.is_elevated()
    except OSError:
        elevated = False
    if elevated:
        checks.append(_passed("administrator", "the current token is elevated"))
    else:
        checks.append(
            _failed(
                "administrator",
                "permission_required",
                "an elevated administrator token is required to install WFP filters",
                PreflightRemediation(
                    action="request_elevation",
                    purpose="install and verify request-scoped WFP filters",
                    requires_explicit_consent=True,
                ),
            )
        )

    try:
        wfp_available = probes.wfp_api_is_available()
    except OSError:
        wfp_available = False
    if wfp_available:
        checks.append(_passed("wfp_api", "Windows Filtering Platform API is available"))
    else:
        checks.append(
            _failed(
                "wfp_api",
                "unsupported_capability",
                "Windows Filtering Platform API is unavailable",
                PreflightRemediation(
                    action="repair_windows_or_use_wsl2",
                    purpose="preserve mandatory Git network isolation",
                ),
            )
        )

    if elevated and bfe_running and wfp_available:
        checks.append(
            _passed(
                "wfp_permission",
                "WFP permission prerequisites are present; each protected launch still "
                "installs and reads back its filters before resuming the child",
            )
        )
    elif not elevated:
        checks.append(
            _failed(
                "wfp_permission",
                "permission_required",
                "WFP filter installation requires an elevated token",
                PreflightRemediation(
                    action="request_elevation",
                    purpose="install and verify request-scoped WFP filters",
                    requires_explicit_consent=True,
                ),
            )
        )
    else:
        checks.append(
            _failed(
                "wfp_permission",
                "unsupported_capability",
                "WFP filter permission cannot be established while BFE or the WFP API "
                "is unavailable",
                PreflightRemediation(
                    action="repair_windows_or_use_wsl2",
                    purpose="preserve mandatory Git network isolation",
                ),
            )
        )

    if release_enabled:
        checks.append(_passed("native_windows_release", "native Windows release gate is enabled"))
    else:
        checks.append(
            _failed(
                "native_windows_release",
                "unsupported_capability",
                "native Windows remains unsupported until IMP-31A-G pass on one release candidate",
                PreflightRemediation(
                    action="use_wsl2",
                    purpose="keep all protected execution fail-closed until release acceptance",
                ),
            )
        )
    return WindowsPreflightReport(tuple(checks))

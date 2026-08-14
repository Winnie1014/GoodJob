"""Windows Git containment: trusted Git for Windows plus dynamic WFP filters."""

from __future__ import annotations

import ctypes
import importlib
import os
import threading
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any, Self

from goodjob.platform.detect import GitSandboxUnavailableError
from goodjob.platform.handles_windows import load_windows_dll

if TYPE_CHECKING:
    from goodjob.git_metadata import InternalGitBinding

RPC_C_AUTHN_WINNT = 10
FWPM_SESSION_FLAG_DYNAMIC = 0x00000001
FWP_EMPTY = 0
FWP_BYTE_BLOB_TYPE = 12
FWP_MATCH_EQUAL = 0
FWP_ACTION_BLOCK = 0x00001001

LAYER_KEYS = {
    "connect_v4": "c38d57d1-05a7-4c33-904f-7fbceee60e82",
    "connect_v6": "4a72393b-319f-44bc-84c3-ba54dcb3b6b4",
    "recv_accept_v4": "e1cd9fe7-f4b5-4273-96c0-592e487b8650",
    "recv_accept_v6": "a3b42c97-9f04-4672-b87e-cee9c483257f",
}

_RETAINED_WFP_ENGINES: list[tuple[Any, int]] = []
_RETAINED_WFP_ENGINES_LOCK = threading.Lock()


class GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", ctypes.c_uint32),
        ("Data2", ctypes.c_uint16),
        ("Data3", ctypes.c_uint16),
        ("Data4", ctypes.c_ubyte * 8),
    ]

    @classmethod
    def parse(cls, value: str) -> Self:
        parsed = uuid.UUID(value)
        return cls(
            parsed.time_low,
            parsed.time_mid,
            parsed.time_hi_version,
            (ctypes.c_ubyte * 8).from_buffer_copy(parsed.bytes[8:]),
        )


class FWP_BYTE_BLOB(ctypes.Structure):
    _fields_ = [("size", ctypes.c_uint32), ("data", ctypes.POINTER(ctypes.c_ubyte))]


class FWP_VALUE_UNION(ctypes.Union):
    _fields_ = [
        ("uint8", ctypes.c_uint8),
        ("uint16", ctypes.c_uint16),
        ("uint32", ctypes.c_uint32),
        ("uint64", ctypes.POINTER(ctypes.c_uint64)),
        ("byteBlob", ctypes.POINTER(FWP_BYTE_BLOB)),
        ("pointer", ctypes.c_void_p),
    ]


class FWP_VALUE0(ctypes.Structure):
    _fields_ = [("type", ctypes.c_uint32), ("value", FWP_VALUE_UNION)]


class FWPM_DISPLAY_DATA0(ctypes.Structure):
    _fields_ = [("name", ctypes.c_wchar_p), ("description", ctypes.c_wchar_p)]


class FWPM_SESSION0(ctypes.Structure):
    _fields_ = [
        ("sessionKey", GUID),
        ("displayData", FWPM_DISPLAY_DATA0),
        ("flags", ctypes.c_uint32),
        ("txnWaitTimeoutInMSec", ctypes.c_uint32),
        ("processId", ctypes.c_uint32),
        ("sid", ctypes.c_void_p),
        ("username", ctypes.c_wchar_p),
        ("kernelMode", ctypes.c_int),
    ]


class FWPM_SUBLAYER0(ctypes.Structure):
    _fields_ = [
        ("subLayerKey", GUID),
        ("displayData", FWPM_DISPLAY_DATA0),
        ("flags", ctypes.c_uint16),
        ("providerKey", ctypes.POINTER(GUID)),
        ("providerData", FWP_BYTE_BLOB),
        ("weight", ctypes.c_uint16),
    ]


class FWPM_FILTER_CONDITION0(ctypes.Structure):
    _fields_ = [
        ("fieldKey", GUID),
        ("matchType", ctypes.c_uint32),
        ("conditionValue", FWP_VALUE0),
    ]


class FWPM_ACTION0(ctypes.Structure):
    _fields_ = [("type", ctypes.c_uint32), ("filterType", GUID)]


class FWPM_FILTER_CONTEXT0(ctypes.Union):
    _fields_ = [("rawContext", ctypes.c_uint64), ("providerContextKey", GUID)]


class FWPM_FILTER0(ctypes.Structure):
    _fields_ = [
        ("filterKey", GUID),
        ("displayData", FWPM_DISPLAY_DATA0),
        ("flags", ctypes.c_uint32),
        ("providerKey", ctypes.POINTER(GUID)),
        ("providerData", FWP_BYTE_BLOB),
        ("layerKey", GUID),
        ("subLayerKey", GUID),
        ("weight", FWP_VALUE0),
        ("numFilterConditions", ctypes.c_uint32),
        ("filterCondition", ctypes.POINTER(FWPM_FILTER_CONDITION0)),
        ("action", FWPM_ACTION0),
        ("context", FWPM_FILTER_CONTEXT0),
        ("reserved", ctypes.POINTER(GUID)),
        ("filterId", ctypes.c_uint64),
        ("effectiveWeight", FWP_VALUE0),
    ]


APP_ID_KEY = GUID.parse("d78e1e87-8644-4ea5-9437-d809ecefc971")


def _wfp_api() -> Any:
    dll = load_windows_dll("fwpuclnt.dll")
    dll.FwpmEngineOpen0.argtypes = [
        ctypes.c_wchar_p,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.POINTER(FWPM_SESSION0),
        ctypes.POINTER(ctypes.c_void_p),
    ]
    dll.FwpmEngineOpen0.restype = ctypes.c_uint32
    dll.FwpmEngineClose0.argtypes = [ctypes.c_void_p]
    dll.FwpmEngineClose0.restype = ctypes.c_uint32
    dll.FwpmSubLayerAdd0.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(FWPM_SUBLAYER0),
        ctypes.c_void_p,
    ]
    dll.FwpmSubLayerAdd0.restype = ctypes.c_uint32
    dll.FwpmFilterAdd0.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(FWPM_FILTER0),
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_uint64),
    ]
    dll.FwpmFilterAdd0.restype = ctypes.c_uint32
    dll.FwpmFilterGetById0.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint64,
        ctypes.POINTER(ctypes.POINTER(FWPM_FILTER0)),
    ]
    dll.FwpmFilterGetById0.restype = ctypes.c_uint32
    dll.FwpmGetAppIdFromFileName0.argtypes = [
        ctypes.c_wchar_p,
        ctypes.POINTER(ctypes.POINTER(FWP_BYTE_BLOB)),
    ]
    dll.FwpmGetAppIdFromFileName0.restype = ctypes.c_uint32
    dll.FwpmFreeMemory0.argtypes = [ctypes.POINTER(ctypes.c_void_p)]
    dll.FwpmFreeMemory0.restype = None
    return dll


def _raise_wfp(operation: str, status: int) -> None:
    raise GitSandboxUnavailableError(
        f"Windows WFP could not establish the Git network boundary ({operation}: 0x{status:08X}); "
        "run elevated with BFE enabled or use WSL2"
    )


def _retry_retained_wfp_engines() -> None:
    """Retry construction cleanup without losing ownership of a live WFP engine."""
    with _RETAINED_WFP_ENGINES_LOCK:
        if not _RETAINED_WFP_ENGINES:
            return
        remaining: list[tuple[Any, int]] = []
        failure_status = 0
        for api, engine in _RETAINED_WFP_ENGINES:
            status = int(api.FwpmEngineClose0(ctypes.c_void_p(engine)))
            if status != 0:
                remaining.append((api, engine))
                failure_status = status
        _RETAINED_WFP_ENGINES[:] = remaining
    if remaining:
        _raise_wfp("FwpmEngineClose0(retained construction cleanup)", failure_status)


def _retain_wfp_engine(api: Any, engine: int) -> None:
    with _RETAINED_WFP_ENGINES_LOCK:
        _RETAINED_WFP_ENGINES.append((api, engine))


def _make_filter(
    sublayer_key: GUID,
    layer_key: GUID,
    app_blob: Any,
    name: str,
) -> tuple[FWPM_FILTER0, FWPM_FILTER_CONDITION0]:
    condition = FWPM_FILTER_CONDITION0()
    condition.fieldKey = APP_ID_KEY
    condition.matchType = FWP_MATCH_EQUAL
    condition.conditionValue.type = FWP_BYTE_BLOB_TYPE
    condition.conditionValue.value.byteBlob = app_blob
    filter_object = FWPM_FILTER0()
    filter_object.displayData.name = name
    filter_object.displayData.description = "GoodJob dynamic application-scoped block"
    filter_object.layerKey = layer_key
    filter_object.subLayerKey = sublayer_key
    filter_object.weight.type = FWP_EMPTY
    filter_object.numFilterConditions = 1
    filter_object.filterCondition = ctypes.pointer(condition)
    filter_object.action.type = FWP_ACTION_BLOCK
    return filter_object, condition


def _guid_bytes(value: GUID) -> bytes:
    return ctypes.string_at(ctypes.byref(value), ctypes.sizeof(value))


def _blob_bytes(value: Any) -> bytes:
    if not value:
        raise GitSandboxUnavailableError("Windows WFP filter readback omitted its application ID")
    blob = value.contents
    if blob.size == 0 or not blob.data:
        raise GitSandboxUnavailableError(
            "Windows WFP filter readback returned an empty application ID"
        )
    return ctypes.string_at(blob.data, blob.size)


def _verify_filter_readback(
    retrieved: Any,
    *,
    expected_id: int,
    expected_layer: GUID,
    expected_app_id: Any,
) -> None:
    value = retrieved.contents
    if (
        int(value.filterId) != expected_id
        or _guid_bytes(value.layerKey) != _guid_bytes(expected_layer)
        or int(value.action.type) != FWP_ACTION_BLOCK
        or int(value.numFilterConditions) != 1
        or not value.filterCondition
    ):
        raise GitSandboxUnavailableError(
            "Windows WFP filter readback did not match its block policy"
        )
    condition = value.filterCondition.contents
    if (
        _guid_bytes(condition.fieldKey) != _guid_bytes(APP_ID_KEY)
        or int(condition.matchType) != FWP_MATCH_EQUAL
        or int(condition.conditionValue.type) != FWP_BYTE_BLOB_TYPE
        or _blob_bytes(condition.conditionValue.value.byteBlob) != _blob_bytes(expected_app_id)
    ):
        raise GitSandboxUnavailableError(
            "Windows WFP filter readback did not match the scoped Git application ID"
        )


class WfpSession:
    """Own a verified dynamic session; close removes all filters atomically."""

    def __init__(
        self,
        api: Any,
        engine: int,
        app_blob: Any,
        filter_ids: tuple[int, ...],
    ) -> None:
        self._api = api
        self._engine = engine
        self._app_blob = app_blob
        self.filter_ids = filter_ids
        self.verified = len(filter_ids) == len(LAYER_KEYS)

    @classmethod
    def create(cls, executable: str) -> WfpSession:
        _retry_retained_wfp_engines()
        api = _wfp_api()
        session = FWPM_SESSION0()
        session.displayData.name = "GoodJob Git network boundary"
        session.displayData.description = "Dynamic WFP filters owned by one Git launch"
        session.flags = FWPM_SESSION_FLAG_DYNAMIC
        engine = ctypes.c_void_p()
        status = int(
            api.FwpmEngineOpen0(
                None, RPC_C_AUTHN_WINNT, None, ctypes.byref(session), ctypes.byref(engine)
            )
        )
        if status != 0 or not engine.value:
            _raise_wfp("FwpmEngineOpen0", status)
        assert engine.value is not None
        engine_value = int(engine.value)
        app_blob = ctypes.POINTER(FWP_BYTE_BLOB)()
        filter_ids: list[int] = []
        try:
            sublayer_key = GUID.parse(str(uuid.uuid4()))
            sublayer = FWPM_SUBLAYER0()
            sublayer.subLayerKey = sublayer_key
            sublayer.displayData.name = "GoodJob Git dynamic sublayer"
            sublayer.displayData.description = "Per-process launch network boundary"
            sublayer.weight = 0xFFFF
            status = int(api.FwpmSubLayerAdd0(engine, ctypes.byref(sublayer), None))
            if status != 0:
                _raise_wfp("FwpmSubLayerAdd0", status)
            status = int(api.FwpmGetAppIdFromFileName0(executable, ctypes.byref(app_blob)))
            if status != 0 or not app_blob:
                _raise_wfp("FwpmGetAppIdFromFileName0", status)
            for label, raw_key in LAYER_KEYS.items():
                layer_key = GUID.parse(raw_key)
                filter_object, condition = _make_filter(
                    sublayer_key,
                    layer_key,
                    app_blob,
                    f"GoodJob Git block {label}",
                )
                filter_id = ctypes.c_uint64()
                status = int(
                    api.FwpmFilterAdd0(
                        engine, ctypes.byref(filter_object), None, ctypes.byref(filter_id)
                    )
                )
                if status != 0:
                    _raise_wfp(f"FwpmFilterAdd0({label})", status)
                retrieved = ctypes.POINTER(FWPM_FILTER0)()
                status = int(
                    api.FwpmFilterGetById0(engine, filter_id.value, ctypes.byref(retrieved))
                )
                if status != 0 or not retrieved:
                    _raise_wfp(f"FwpmFilterGetById0({label})", status)
                free_pointer = ctypes.cast(retrieved, ctypes.c_void_p)
                try:
                    _verify_filter_readback(
                        retrieved,
                        expected_id=int(filter_id.value),
                        expected_layer=layer_key,
                        expected_app_id=app_blob,
                    )
                finally:
                    api.FwpmFreeMemory0(ctypes.byref(free_pointer))
                filter_ids.append(int(filter_id.value))
                del condition
            return cls(api, engine_value, app_blob, tuple(filter_ids))
        except BaseException as primary_error:
            if app_blob:
                free_blob = ctypes.cast(app_blob, ctypes.c_void_p)
                api.FwpmFreeMemory0(ctypes.byref(free_blob))
            close_status = int(api.FwpmEngineClose0(engine))
            if close_status != 0:
                _retain_wfp_engine(api, engine_value)
                try:
                    _raise_wfp("FwpmEngineClose0(construction cleanup)", close_status)
                except GitSandboxUnavailableError as cleanup_error:
                    raise cleanup_error from primary_error
            raise

    def close(self) -> None:
        if self._engine == 0:
            return
        engine = self._engine
        if self._app_blob:
            free_blob = ctypes.cast(self._app_blob, ctypes.c_void_p)
            self._api.FwpmFreeMemory0(ctypes.byref(free_blob))
            self._app_blob = ctypes.POINTER(FWP_BYTE_BLOB)()
        status = int(self._api.FwpmEngineClose0(ctypes.c_void_p(engine)))
        if status != 0:
            _raise_wfp("FwpmEngineClose0", status)
        self._engine = 0

    def __enter__(self) -> Self:
        if not self.verified:
            raise GitSandboxUnavailableError("Windows WFP filter readback is incomplete")
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


def _registry_install_roots() -> tuple[Path, ...]:
    try:
        winreg = importlib.import_module("winreg")
    except ImportError:
        return ()
    roots: list[Path] = []
    access_values = (
        int(winreg.KEY_READ),
        int(winreg.KEY_READ) | int(getattr(winreg, "KEY_WOW64_64KEY", 0)),
        int(winreg.KEY_READ) | int(getattr(winreg, "KEY_WOW64_32KEY", 0)),
    )
    for hive_name in ("HKEY_LOCAL_MACHINE", "HKEY_CURRENT_USER"):
        hive = getattr(winreg, hive_name)
        for access in access_values:
            try:
                with winreg.OpenKey(hive, r"SOFTWARE\GitForWindows", 0, access) as key:
                    value, _kind = winreg.QueryValueEx(key, "InstallPath")
            except OSError:
                continue
            if isinstance(value, str) and value:
                roots.append(Path(value))
    return tuple(roots)


def windows_git_candidates() -> tuple[Path, ...]:
    roots = list(_registry_install_roots())
    for variable in ("ProgramW6432", "ProgramFiles", "ProgramFiles(x86)", "LocalAppData"):
        value = os.environ.get(variable)
        if value:
            roots.append(Path(value) / ("Programs/Git" if variable == "LocalAppData" else "Git"))
    unique: list[Path] = []
    seen: set[str] = set()
    for root in roots:
        candidate = root / "mingw64" / "bin" / "git.exe"
        key = os.path.normcase(str(candidate))
        if key not in seen:
            unique.append(candidate)
            seen.add(key)
    return tuple(unique)


def resolve_windows_git_executable() -> str:
    for candidate in windows_git_candidates():
        if not candidate.is_file():
            continue
        resolved = candidate.resolve(strict=True)
        tail = tuple(part.lower() for part in resolved.parts[-3:])
        if tail == ("mingw64", "bin", "git.exe"):
            return str(resolved)
    raise GitSandboxUnavailableError(
        r"Git for Windows is unavailable at a trusted mingw64\bin\git.exe path; "
        r"cmd\git.exe is not an allowed entry point"
    )


class WfpGitSandbox:
    """Validate the exact Git entry point and create its per-launch WFP guard."""

    def __init__(self, git_executable: str) -> None:
        self._git_executable = str(Path(git_executable).resolve(strict=True))
        tail = tuple(part.lower() for part in Path(self._git_executable).parts[-3:])
        if tail != ("mingw64", "bin", "git.exe"):
            raise GitSandboxUnavailableError(r"Windows Git entry point must be mingw64\bin\git.exe")

    def build_command(
        self,
        git_executable: str,
        binding: InternalGitBinding,
        git_command: list[str],
    ) -> list[str]:
        del binding
        if os.path.normcase(str(Path(git_executable).resolve(strict=True))) != os.path.normcase(
            self._git_executable
        ):
            raise GitSandboxUnavailableError("Windows Git application ID and entry point differ")
        return git_command

    def open_network_guard(self) -> WfpSession:
        return WfpSession.create(self._git_executable)

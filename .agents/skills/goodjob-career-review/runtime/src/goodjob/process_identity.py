"""Process identity helpers used by crash-recoverable local attempts."""

from __future__ import annotations

import os
import subprocess
import sys

_PROCESS_ENV = {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "LC_ALL": "C"}


def process_identity() -> str:
    """Return a PID plus start marker so PID reuse cannot impersonate an owner."""
    pid = os.getpid()
    started = process_start_marker(pid)
    return f"pid:{pid};started:{started}" if started is not None else f"pid:{pid};started:unknown"


def is_recoverable_process_identity(identity: str) -> bool:
    """Return whether an identity can later prove PID death or reuse."""
    prefix, separator, started = identity.partition(";started:")
    if not separator or not prefix.startswith("pid:") or not started or started == "unknown":
        return False
    try:
        return int(prefix.removeprefix("pid:")) > 0
    except ValueError:
        return False


def process_start_marker(pid: int) -> str | None:
    if sys.platform == "darwin":
        return _macos_start_marker(pid)
    if sys.platform.startswith("linux"):
        return _linux_start_marker(pid)
    if sys.platform == "win32":
        from goodjob.platform.process_windows import process_start_marker as windows_start_marker

        return windows_start_marker(pid)
    raise OSError(f"process identity is not supported on platform: {sys.platform}")


def _macos_start_marker(pid: int) -> str | None:
    result = subprocess.run(
        ["/bin/ps", "-o", "lstart=", "-p", str(pid)],
        check=False,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        env=_PROCESS_ENV,
        text=True,
    )
    marker = result.stdout.strip()
    return marker if result.returncode == 0 and marker else None


def _linux_start_marker(pid: int) -> str | None:
    try:
        raw = (f"/proc/{pid}/stat").encode("ascii")
        fd = os.open(raw, os.O_RDONLY)
    except FileNotFoundError:
        return None
    except OSError:
        return None
    try:
        content = b""
        while True:
            chunk = os.read(fd, 4096)
            if not chunk:
                break
            content += chunk
            if len(content) > 65536:
                return None
    finally:
        os.close(fd)
    try:
        decoded = content.decode("utf-8", errors="replace")
    except UnicodeDecodeError:
        return None
    right_paren = decoded.rfind(")")
    if right_paren == -1:
        return None
    fields = decoded[right_paren + 2 :].split()
    if len(fields) < 20:
        return None
    starttime = fields[19]
    try:
        int(starttime)
    except ValueError:
        return None
    return starttime


def owner_process_stopped(identity: str) -> bool:
    """Return true only when both PID existence and its start marker prove death."""
    if not is_recoverable_process_identity(identity):
        return False
    prefix, _, started = identity.partition(";started:")
    try:
        pid = int(prefix.removeprefix("pid:"))
    except ValueError:
        return False
    if pid <= 0:
        return False
    if sys.platform == "win32":
        from goodjob.platform.process_windows import process_exists

        if not process_exists(pid):
            return True
    else:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        except PermissionError:
            return False
    current_start = process_start_marker(pid)
    return current_start is not None and current_start != started

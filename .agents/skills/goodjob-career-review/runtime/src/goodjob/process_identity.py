"""Process identity helpers used by crash-recoverable local attempts."""

from __future__ import annotations

import os
import subprocess

_PROCESS_ENV = {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "LC_ALL": "C"}


def process_identity() -> str:
    """Return a PID plus start marker so PID reuse cannot impersonate an owner."""
    pid = os.getpid()
    started = process_start_marker(pid)
    return f"pid:{pid};started:{started}" if started is not None else f"pid:{pid};started:unknown"


def process_start_marker(pid: int) -> str | None:
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


def owner_process_stopped(identity: str) -> bool:
    """Return true only when both PID existence and its start marker prove death."""
    prefix, separator, started = identity.partition(";started:")
    if not separator or not prefix.startswith("pid:") or started == "unknown":
        return False
    try:
        pid = int(prefix.removeprefix("pid:"))
    except ValueError:
        return False
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    current_start = process_start_marker(pid)
    return current_start is not None and current_start != started

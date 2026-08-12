"""Platform backend selection for sandbox and process identity primitives."""

from __future__ import annotations

from goodjob.platform.detect import (
    GitSandboxUnavailableError,
    Platform,
    detect_platform,
    select_git_sandbox,
)

__all__ = [
    "GitSandboxUnavailableError",
    "Platform",
    "detect_platform",
    "select_git_sandbox",
]

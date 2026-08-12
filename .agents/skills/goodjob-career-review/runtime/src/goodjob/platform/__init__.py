"""Platform backend selection for sandbox and process identity primitives."""

from __future__ import annotations

from goodjob.platform.detect import Platform, detect_platform, select_git_sandbox

__all__ = ["Platform", "detect_platform", "select_git_sandbox"]

"""macOS Seatbelt sandbox backend for Git subprocesses."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from goodjob.git_metadata import InternalGitBinding

SANDBOX_EXECUTABLE = Path("/usr/bin/sandbox-exec")

GIT_SANDBOX_PROFILE = " ".join(
    (
        "(version 1)",
        '(import "system.sb")',
        "(deny default)",
        "(deny network*)",
        '(allow process-exec (literal (param "GIT_EXECUTABLE")))',
        "(allow process-fork)",
        "("
        "allow file-read* file-test-existence file-map-executable "
        '(subpath (param "AUTHORIZED_ROOT")) '
        '(literal (param "GIT_EXECUTABLE"))'
        ")",
        '(allow file-read-metadata file-test-existence (path-ancestors (param "AUTHORIZED_ROOT")))',
        '(allow file-write-data (literal "/dev/null"))',
    )
)


class SeatbeltSandbox:
    """Build the sandbox-exec wrapped Git command for macOS."""

    def is_available(self) -> bool:
        return SANDBOX_EXECUTABLE.is_file()

    def build_command(
        self,
        git_executable: str,
        binding: InternalGitBinding,
        git_args: list[str],
    ) -> list[str]:
        if not self.is_available():
            raise OSError("macOS sandbox-exec is not available at /usr/bin/sandbox-exec")
        git_command = [
            git_executable,
            "--no-lazy-fetch",
            "--no-replace-objects",
            f"--git-dir={binding.git_dir}",
            f"--work-tree={binding.worktree_root}",
            "-c",
            "core.fsmonitor=false",
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "core.pager=cat",
            "--no-pager",
            *git_args,
        ]
        return [
            str(SANDBOX_EXECUTABLE),
            "-p",
            GIT_SANDBOX_PROFILE,
            "-D",
            f"AUTHORIZED_ROOT={binding.workspace_root}",
            "-D",
            f"GIT_EXECUTABLE={git_executable}",
            *git_command,
        ]

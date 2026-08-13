"""Linux bubblewrap (bwrap) sandbox backend for Git subprocesses."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from goodjob.git_metadata import InternalGitBinding

BWRAP_EXECUTABLE_CANDIDATES = ("/usr/bin/bwrap",)


def _find_bwrap() -> str | None:
    for candidate in BWRAP_EXECUTABLE_CANDIDATES:
        if Path(candidate).is_file():
            return candidate
    return None


class BwrapSandbox:
    """Build the bwrap-wrapped Git command for Linux.

    The bwrap profile is equivalent to the macOS Seatbelt profile:
    - ``--unshare-net``: deny network (equivalent to ``(deny network*)``)
    - ``--unshare-pid``: private PID namespace (prevent sandboxed process from
      seeing host process list)
    - ``--ro-bind <authorized_root> <authorized_root>``: read-only authorized root
    - ``--ro-bind /usr /usr`` + ``--ro-bind /lib /lib`` + ``--ro-bind-try /lib64``:
      system libraries + git binary readable
    - ``--ro-bind-try /etc /etc``: git needs /etc/passwd, /etc/group for identity
      resolution, /etc/resolv.conf, /etc/nsswitch.conf etc.
    - ``--dev /dev``: fresh devtmpfs (null/zero/random etc.)
    - ``--proc /proc``: procfs (MUST be mounted after --unshare-pid, otherwise
      host process cmdline is exposed)
    - ``--tmpfs /tmp``: empty tmpfs (avoid host /tmp leakage)
    - ``--chdir <authorized_root>``: replace the descriptor-bound host cwd with
      the read-only namespace mount before Git starts
    - ``--die-with-parent``: kill child when parent exits

    Security trade-off: bwrap makes /usr and /etc readable, which is wider than
    macOS Seatbelt's "AUTHORIZED_ROOT + GIT_EXECUTABLE only". But /usr and /etc
    are system files containing no user data; user home directory and other
    workspaces remain unreachable, which is the key security property.
    """

    def is_available(self) -> bool:
        return _find_bwrap() is not None

    def build_command(
        self,
        git_executable: str,
        binding: InternalGitBinding,
        git_command: list[str],
    ) -> list[str]:
        from goodjob.platform.detect import GitSandboxUnavailableError

        bwrap = _find_bwrap()
        if bwrap is None:
            raise GitSandboxUnavailableError(
                "bubblewrap (bwrap) is not installed; install it to enable the "
                "Linux Git sandbox, or use WSL2/macOS"
            )
        authorized_root = str(binding.workspace_root)
        return [
            bwrap,
            "--die-with-parent",
            "--unshare-net",
            "--unshare-pid",
            "--tmpfs",
            "/tmp",
            "--ro-bind",
            authorized_root,
            authorized_root,
            "--ro-bind",
            "/usr",
            "/usr",
            "--ro-bind",
            "/lib",
            "/lib",
            "--ro-bind-try",
            "/lib64",
            "/lib64",
            "--ro-bind-try",
            "/etc",
            "/etc",
            "--dev",
            "/dev",
            "--proc",
            "/proc",
            "--chdir",
            authorized_root,
            *git_command,
        ]

"""Bounded, symlink-resistant reads below an already authorized source root."""

from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path, PurePosixPath

MAX_SOURCE_FILE_BYTES = 2 * 1024 * 1024


def _regular_file_flags() -> int:
    return os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)


def _require_regular_file(file_fd: int) -> os.stat_result:
    file_stat = os.fstat(file_fd)
    if not stat.S_ISREG(file_stat.st_mode):
        os.close(file_fd)
        raise OSError("path is not a regular file")
    return file_stat


def open_regular_file(root: Path, relative_path: str) -> tuple[int, os.stat_result]:
    """Open a regular file below root without following any path component symlink."""
    path = PurePosixPath(relative_path)
    parts = path.parts
    if path.is_absolute() or not parts or any(part in {"", ".", ".."} for part in parts):
        raise OSError("relative path is not safe to open")
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | nofollow
    directory_fd = os.open(root, directory_flags)
    try:
        for part in parts[:-1]:
            next_fd = os.open(part, directory_flags, dir_fd=directory_fd)
            os.close(directory_fd)
            directory_fd = next_fd
        file_fd = os.open(parts[-1], _regular_file_flags(), dir_fd=directory_fd)
        return file_fd, _require_regular_file(file_fd)
    finally:
        os.close(directory_fd)


def open_absolute_regular_file(path: Path) -> tuple[int, os.stat_result]:
    """Open one canonical absolute file without following a replaced path component."""
    if not path.is_absolute():
        raise OSError("absolute file path is required")
    parts = path.parts
    if len(parts) < 2 or any(part in {"", ".", ".."} for part in parts[1:]):
        raise OSError("absolute file path is not safe to open")
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | nofollow
    directory_fd = os.open(parts[0], directory_flags)
    try:
        for part in parts[1:-1]:
            next_fd = os.open(part, directory_flags, dir_fd=directory_fd)
            os.close(directory_fd)
            directory_fd = next_fd
        file_fd = os.open(parts[-1], _regular_file_flags(), dir_fd=directory_fd)
        return file_fd, _require_regular_file(file_fd)
    finally:
        os.close(directory_fd)


def read_open_file(file_fd: int, *, maximum_bytes: int = MAX_SOURCE_FILE_BYTES) -> bytes:
    """Read an already-open file with a hard byte ceiling."""
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = os.read(file_fd, 1024 * 1024)
        if not chunk:
            return b"".join(chunks)
        total += len(chunk)
        if total > maximum_bytes:
            raise OSError("file exceeded the indexing limit while it was open")
        chunks.append(chunk)


def hash_regular_file(root: Path, relative_path: str) -> tuple[str, int]:
    """Return the bounded source bytes' SHA-256 and observed size."""
    file_fd, file_stat = open_regular_file(root, relative_path)
    try:
        content = read_open_file(file_fd)
    finally:
        os.close(file_fd)
    return hashlib.sha256(content).hexdigest(), file_stat.st_size

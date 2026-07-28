"""Root-confined filesystem operations for immutable runtime publications."""

from __future__ import annotations

import os
import stat
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from goodjob.errors import InvalidInputError


def write_new_file_at(
    directory_fd: int,
    name: str,
    content: bytes,
    *,
    mode: int = 0o600,
) -> None:
    """Create and fsync one new regular file relative to a trusted directory fd."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    descriptor = os.open(name, flags, mode, dir_fd=directory_fd)
    try:
        view = memoryview(content)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("file write made no progress")
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


@dataclass(frozen=True)
class SafeDataTree:
    """Operate below one fixed data-directory prefix without following directory links."""

    root: Path
    prefix: str
    label: str
    protected_ancestors: frozenset[tuple[str, ...]] = frozenset()

    def __post_init__(self) -> None:
        if PurePosixPath(self.prefix).parts != (self.prefix,) or self.prefix in {
            "",
            ".",
            "..",
        }:
            raise ValueError("data-tree prefix must be one safe path component")
        protected = set(self.protected_ancestors)
        protected.add((self.prefix,))
        if any(
            not parts or parts[0] != self.prefix or any(part in {"", ".", ".."} for part in parts)
            for parts in protected
        ):
            raise ValueError("protected data-tree ancestors must stay below its prefix")
        object.__setattr__(self, "protected_ancestors", frozenset(protected))

    def relative_parts(self, relative: str) -> tuple[str, ...]:
        pure = PurePosixPath(relative)
        if (
            pure.is_absolute()
            or not pure.parts
            or pure.parts[0] != self.prefix
            or any(part in {"", ".", ".."} for part in pure.parts)
        ):
            raise InvalidInputError(f"{self.label} path is outside the personal data directory")
        return pure.parts

    def path(self, relative: str) -> Path:
        return self.root.joinpath(*self.relative_parts(relative))

    @contextmanager
    def open_parent(self, relative: str) -> Iterator[tuple[int, str]]:
        parts = self.relative_parts(relative)
        flags = (
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0)
        )
        descriptor = -1
        try:
            descriptor = os.open(self.root, flags)
            for component in parts[:-1]:
                next_descriptor = os.open(component, flags, dir_fd=descriptor)
                os.close(descriptor)
                descriptor = next_descriptor
            yield descriptor, parts[-1]
        except OSError as exc:
            raise InvalidInputError(
                f"{self.label} path contains an unavailable or linked directory"
            ) from exc
        finally:
            if descriptor >= 0:
                os.close(descriptor)

    def read_regular(self, relative: str) -> bytes:
        with self.open_parent(relative) as (directory_fd, name):
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
            try:
                descriptor = os.open(name, flags, dir_fd=directory_fd)
            except OSError as exc:
                raise InvalidInputError(f"{self.label} file is unavailable or linked") from exc
            try:
                if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                    raise InvalidInputError(f"{self.label} file is not a regular file")
                chunks: list[bytes] = []
                while chunk := os.read(descriptor, 1024 * 1024):
                    chunks.append(chunk)
                return b"".join(chunks)
            finally:
                os.close(descriptor)

    def list_directory(self, relative: str) -> set[str]:
        with self.open_parent(relative) as (directory_fd, name):
            flags = (
                os.O_RDONLY
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_CLOEXEC", 0)
            )
            try:
                descriptor = os.open(name, flags, dir_fd=directory_fd)
            except OSError as exc:
                raise InvalidInputError(f"{self.label} directory is unavailable or linked") from exc
            try:
                return set(os.listdir(descriptor))
            finally:
                os.close(descriptor)

    def write_new(self, relative: str, content: bytes) -> None:
        with self.open_parent(relative) as (directory_fd, name):
            try:
                write_new_file_at(directory_fd, name, content)
                os.fsync(directory_fd)
            except OSError as exc:
                raise InvalidInputError(
                    f"{self.label} temporary file could not be created"
                ) from exc

    def replace_file(self, source: str, destination: str, *, mode: int) -> None:
        with (
            self.open_parent(source) as (source_fd, source_name),
            self.open_parent(destination) as (destination_fd, destination_name),
        ):
            try:
                destination_mode = os.stat(
                    destination_name,
                    dir_fd=destination_fd,
                    follow_symlinks=False,
                ).st_mode
            except FileNotFoundError:
                destination_mode = None
            if destination_mode is not None and stat.S_ISDIR(destination_mode):
                raise InvalidInputError(f"{self.label} destination is an unexpected directory")
            try:
                os.replace(
                    source_name,
                    destination_name,
                    src_dir_fd=source_fd,
                    dst_dir_fd=destination_fd,
                )
                flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
                descriptor = os.open(destination_name, flags, dir_fd=destination_fd)
                try:
                    if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                        raise InvalidInputError(f"{self.label} destination is not a regular file")
                    os.fchmod(descriptor, mode)
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
                os.fsync(source_fd)
                if destination_fd != source_fd:
                    os.fsync(destination_fd)
            except OSError as exc:
                raise InvalidInputError(f"{self.label} file replacement failed") from exc

    def publish_directory(
        self,
        temp_relative: str,
        final_relative: str,
        files: dict[str, bytes],
        *,
        verify: Callable[[str], None],
        before_rename: Callable[[], None] | None = None,
    ) -> None:
        """Durably publish a verified temporary directory under the configured tree."""
        if not files or any(
            PurePosixPath(name).parts != (name,) or name in {"", ".", ".."} for name in files
        ):
            raise InvalidInputError(f"{self.label} publication file set is invalid")
        with (
            self.open_parent(temp_relative) as (temp_parent_fd, temp_name),
            self.open_parent(final_relative) as (final_parent_fd, final_name),
        ):
            for directory_fd, name in (
                (temp_parent_fd, temp_name),
                (final_parent_fd, final_name),
            ):
                try:
                    os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                except FileNotFoundError:
                    continue
                raise InvalidInputError(f"{self.label} publication path already exists")
            try:
                os.mkdir(temp_name, mode=0o700, dir_fd=temp_parent_fd)
                directory_flags = (
                    os.O_RDONLY
                    | getattr(os, "O_DIRECTORY", 0)
                    | getattr(os, "O_NOFOLLOW", 0)
                    | getattr(os, "O_CLOEXEC", 0)
                )
                temp_fd = os.open(temp_name, directory_flags, dir_fd=temp_parent_fd)
                try:
                    for name, content in files.items():
                        write_new_file_at(temp_fd, name, content)
                    os.fsync(temp_fd)
                finally:
                    os.close(temp_fd)
                verify(temp_relative)
                if before_rename is not None:
                    before_rename()
                os.rename(
                    temp_name,
                    final_name,
                    src_dir_fd=temp_parent_fd,
                    dst_dir_fd=final_parent_fd,
                )
                os.fsync(temp_parent_fd)
                os.fsync(final_parent_fd)
                final_fd = os.open(final_name, directory_flags, dir_fd=final_parent_fd)
                try:
                    for name in files:
                        flags = (
                            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
                        )
                        file_fd = os.open(name, flags, dir_fd=final_fd)
                        try:
                            if not stat.S_ISREG(os.fstat(file_fd).st_mode):
                                raise InvalidInputError(
                                    f"published {self.label} is not a regular file"
                                )
                            os.fchmod(file_fd, stat.S_IRUSR)
                            os.fsync(file_fd)
                        finally:
                            os.close(file_fd)
                    os.fchmod(final_fd, stat.S_IRUSR | stat.S_IXUSR)
                    os.fsync(final_fd)
                finally:
                    os.close(final_fd)
            except OSError as exc:
                raise InvalidInputError(f"{self.label} directory publication failed") from exc

    def remove(self, relative: str) -> None:
        parts = self.relative_parts(relative)
        if parts in self.protected_ancestors:
            raise InvalidInputError(f"refusing to remove a protected {self.label} ancestor")
        with self.open_parent(relative) as (directory_fd, name):
            self._remove_entry_at(directory_fd, name)
            os.fsync(directory_fd)

    @classmethod
    def _remove_entry_at(cls, directory_fd: int, name: str) -> None:
        try:
            entry = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            return
        if not stat.S_ISDIR(entry.st_mode):
            os.unlink(name, dir_fd=directory_fd)
            return
        flags = (
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0)
        )
        child_fd = os.open(name, flags, dir_fd=directory_fd)
        try:
            os.fchmod(child_fd, stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
            for child_name in os.listdir(child_fd):
                cls._remove_entry_at(child_fd, child_name)
        finally:
            os.close(child_fd)
        os.rmdir(name, dir_fd=directory_fd)

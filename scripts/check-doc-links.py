#!/usr/bin/env python3
"""Check repository-local targets of Markdown inline links.
Use only Python 3.9+ syntax because gate-invoked python3 varies across machines."""

from __future__ import annotations

import os
import re
import stat
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path, PurePath, PurePosixPath
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parent.parent
BACKTICK_RUN_RE = re.compile(r"`+")
BLOCKQUOTE_PREFIX_RE = re.compile(r"^ {0,3}>[ \t]?")
LIST_ITEM_RE = re.compile(r"^ {0,3}(?:[-+*]|\d{1,9}[.)])[ \t]+")
SKIPPED_SCHEMES = {"http", "https", "mailto"}
IS_WINDOWS = sys.platform == "win32"
WINDOWS_DIRECTORY_ATTRIBUTE = getattr(stat, "FILE_ATTRIBUTE_DIRECTORY", 0x10)
WINDOWS_DEVICE_ATTRIBUTE = getattr(stat, "FILE_ATTRIBUTE_DEVICE", 0x40)
WINDOWS_REPARSE_ATTRIBUTE = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
WINDOWS_INVALID_ATTRIBUTES = 0xFFFFFFFF


def directory_flags() -> int:
    return os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


def file_flags() -> int:
    return os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC


def windows_file_attributes(path: Path) -> int | None:
    import ctypes

    query = ctypes.windll.kernel32.GetFileAttributesW
    query.argtypes = [ctypes.c_wchar_p]
    query.restype = ctypes.c_uint32
    attributes = int(query(str(path)))
    return None if attributes == WINDOWS_INVALID_ATTRIBUTES else attributes


def windows_repository_path(
    parts: tuple[str, ...], *, regular_file: bool = False
) -> Path | None:
    if any(
        part in {"", ".", ".."} or "/" in part or "\\" in part or ":" in part
        for part in parts
    ):
        return None

    current = ROOT
    for index, part in enumerate(parts):
        current = current / part
        attributes = windows_file_attributes(current)
        if attributes is None or attributes & WINDOWS_REPARSE_ATTRIBUTE:
            return None
        if index < len(parts) - 1 and not attributes & WINDOWS_DIRECTORY_ATTRIBUTE:
            return None

    if regular_file and (
        not parts
        or attributes & (WINDOWS_DIRECTORY_ATTRIBUTE | WINDOWS_DEVICE_ATTRIBUTE)
    ):
        return None
    return current


def repository_markdown_files() -> list[Path]:
    """Return tracked and unignored untracked Markdown files."""
    result = subprocess.run(
        [
            "git",
            "-C",
            str(ROOT),
            "ls-files",
            "-z",
            "--cached",
            "--others",
            "--exclude-standard",
            "--",
            "*.md",
        ],
        check=True,
        capture_output=True,
    )
    paths = (ROOT / os.fsdecode(item) for item in result.stdout.split(b"\0") if item)
    regular_files: list[Path] = []
    for path in paths:
        try:
            parts = path.relative_to(ROOT).parts
            if IS_WINDOWS:
                if windows_repository_path(parts, regular_file=True) is not None:
                    regular_files.append(path)
            elif stat.S_ISREG(path.lstat().st_mode):
                regular_files.append(path)
        except FileNotFoundError:
            continue
    return sorted(regular_files)


def open_parent_directory(parts: tuple[str, ...]) -> int:
    descriptor = os.open(ROOT, directory_flags())
    try:
        for part in parts[:-1]:
            next_descriptor = os.open(part, directory_flags(), dir_fd=descriptor)
            os.close(descriptor)
            descriptor = next_descriptor
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def read_repository_text(path: Path) -> str:
    parts = path.relative_to(ROOT).parts
    if IS_WINDOWS:
        checked_path = windows_repository_path(parts, regular_file=True)
        if checked_path is None:
            raise OSError(f"not a regular repository file: {path.relative_to(ROOT)}")
        return checked_path.read_text(encoding="utf-8")

    parent_descriptor = open_parent_directory(parts)
    try:
        descriptor = os.open(parts[-1], file_flags(), dir_fd=parent_descriptor)
    finally:
        os.close(parent_descriptor)
    with os.fdopen(descriptor, encoding="utf-8") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise OSError(f"not a regular file: {path.relative_to(ROOT)}")
        return handle.read()


def repository_path_exists(parts: tuple[str, ...]) -> bool:
    if not parts:
        return True
    if IS_WINDOWS:
        return windows_repository_path(parts) is not None
    try:
        parent_descriptor = open_parent_directory(parts)
        try:
            mode = os.stat(parts[-1], dir_fd=parent_descriptor, follow_symlinks=False).st_mode
        finally:
            os.close(parent_descriptor)
    except OSError:
        return False
    return not stat.S_ISLNK(mode)


def fence_marker(line: str) -> tuple[str, int, str] | None:
    indent = len(line) - len(line.lstrip(" "))
    if indent > 3:
        return None
    stripped = line[indent:]
    if not stripped or stripped[0] not in {"`", "~"}:
        return None
    marker = stripped[0]
    length = len(stripped) - len(stripped.lstrip(marker))
    if length < 3:
        return None
    return marker, length, stripped[length:]


def strip_blockquote_prefixes(line: str) -> str:
    content = line
    while match := BLOCKQUOTE_PREFIX_RE.match(content):
        content = content[match.end() :]
    return content


def container_content(line: str, list_indent: int | None) -> tuple[str, int | None]:
    content = strip_blockquote_prefixes(line)
    if not content.strip():
        return content, list_indent

    indentation = len(content) - len(content.lstrip(" "))
    base_indent = 0
    if list_indent is not None and indentation >= list_indent:
        content = content[list_indent:]
        base_indent = list_indent
    elif list_indent is not None:
        list_indent = None

    if match := LIST_ITEM_RE.match(content):
        base_indent += match.end()
        content = content[match.end() :]
        list_indent = base_indent
    return content, list_indent


def without_fenced_code(text: str) -> str:
    kept: list[str] = []
    fence: tuple[str, int] | None = None
    list_indent: int | None = None
    for line in text.splitlines(keepends=True):
        if fence is None:
            content, list_indent = container_content(line, list_indent)
            marker = fence_marker(content)
            if marker is None:
                kept.append(line)
            else:
                fence = marker[0], marker[1]
                kept.append("\n" if line.endswith("\n") else "")
            continue
        content = strip_blockquote_prefixes(line)
        if list_indent is not None and len(content) - len(content.lstrip(" ")) >= list_indent:
            content = content[list_indent:]
        marker = fence_marker(content)
        if marker and marker[0] == fence[0] and marker[1] >= fence[1] and not marker[2].strip():
            fence = None
        kept.append("\n" if line.endswith("\n") else "")
    return "".join(kept)


def is_escaped(text: str, position: int) -> bool:
    backslashes = 0
    position -= 1
    while position >= 0 and text[position] == "\\":
        backslashes += 1
        position -= 1
    return backslashes % 2 == 1


def code_mask(text: str) -> str:
    return "".join(character if character in "\r\n" else " " for character in text)


def without_inline_code(text: str) -> str:
    kept: list[str] = []
    cursor = 0
    runs = [
        match for match in BACKTICK_RUN_RE.finditer(text) if not is_escaped(text, match.start())
    ]
    index = 0
    while index < len(runs):
        opening = runs[index]
        closing_index = index + 1
        while closing_index < len(runs) and len(runs[closing_index].group()) != len(
            opening.group()
        ):
            closing_index += 1
        if closing_index == len(runs):
            index += 1
            continue
        kept.append(text[cursor : opening.start()])
        kept.append(code_mask(text[opening.start() : runs[closing_index].end()]))
        cursor = runs[closing_index].end()
        index = closing_index + 1
    kept.append(text[cursor:])
    return "".join(kept)


def matching_bracket(text: str, start: int) -> int | None:
    depth = 0
    for position in range(start, len(text)):
        if is_escaped(text, position):
            continue
        if text[position] == "[":
            depth += 1
        elif text[position] == "]":
            depth -= 1
            if depth == 0:
                return position
    return None


def link_close_after_title(text: str, start: int) -> int | None:
    cursor = start
    while cursor < len(text) and text[cursor].isspace():
        cursor += 1
    if cursor >= len(text):
        return None
    if text[cursor] == ")":
        return cursor + 1

    delimiter = text[cursor]
    if delimiter not in {'"', "'", "("}:
        return None
    closing = ")" if delimiter == "(" else delimiter
    cursor += 1
    depth = 1
    while cursor < len(text):
        if is_escaped(text, cursor):
            cursor += 1
        elif text[cursor] == delimiter and delimiter == "(":
            depth += 1
        elif text[cursor] == closing:
            depth -= 1
            if depth == 0:
                cursor += 1
                break
        cursor += 1
    else:
        return None

    while cursor < len(text) and text[cursor].isspace():
        cursor += 1
    if cursor < len(text) and text[cursor] == ")":
        return cursor + 1
    return None


def link_destination(text: str, start: int) -> tuple[str, int] | None:
    cursor = start
    while cursor < len(text) and text[cursor].isspace():
        cursor += 1
    if cursor >= len(text):
        return None

    if text[cursor] == "<":
        destination_start = cursor + 1
        cursor = destination_start
        while cursor < len(text) and text[cursor] not in "\r\n":
            if text[cursor] == ">" and not is_escaped(text, cursor):
                link_end = link_close_after_title(text, cursor + 1)
                if link_end is not None:
                    return text[destination_start:cursor], link_end
                return None
            cursor += 1
        return None

    destination_start = cursor
    depth = 0
    while cursor < len(text):
        if is_escaped(text, cursor):
            cursor += 2
            continue
        character = text[cursor]
        if character == "(":
            depth += 1
        elif character == ")":
            if depth == 0:
                return text[destination_start:cursor], cursor + 1
            depth -= 1
        elif character.isspace() and depth == 0:
            link_end = link_close_after_title(text, cursor)
            if link_end is not None:
                return text[destination_start:cursor], link_end
            return None
        cursor += 1
    return None


def inline_link_targets(text: str) -> Iterator[str]:
    cursor = 0
    while cursor < len(text):
        if text[cursor] != "[":
            cursor += 1
            continue
        label_end = matching_bracket(text, cursor)
        if label_end is None or label_end + 1 >= len(text) or text[label_end + 1] != "(":
            cursor += 1
            continue
        parsed = link_destination(text, label_end + 2)
        if parsed is None:
            cursor += 1
            continue
        yield parsed[0]
        cursor = parsed[1]


def normalized_target_parts(source: Path, target_path: str) -> tuple[str, ...] | None:
    markdown_path = PurePosixPath(target_path)
    if markdown_path.is_absolute():
        return None

    parts = list(source.parent.relative_to(ROOT).parts)
    for part in markdown_path.parts:
        if part in {"", "."}:
            continue
        if part == "..":
            if not parts:
                return None
            parts.pop()
        else:
            parts.append(part)
    return tuple(parts)


def broken_links(path: Path) -> Iterator[tuple[Path, str]]:
    text = read_repository_text(path)
    visible = without_inline_code(without_fenced_code(text))
    for raw_target in inline_link_targets(visible):
        target = raw_target.strip()
        try:
            parsed = urlsplit(target)
        except ValueError:
            yield path, target
            continue
        if parsed.scheme.lower() in SKIPPED_SCHEMES:
            continue
        if parsed.scheme or parsed.netloc:
            yield path, target
            continue
        if not parsed.path:
            continue
        parts = normalized_target_parts(path, unquote(parsed.path))
        if parts is None or not repository_path_exists(parts):
            yield path, target


def format_broken_link(source: PurePath, target: str) -> str:
    return f"{source.as_posix()}:{target}"


def main() -> int:
    files = repository_markdown_files()
    failures = [failure for path in files for failure in broken_links(path)]
    if failures:
        for source, target in failures:
            print(format_broken_link(source.relative_to(ROOT), target))
        return 1
    print(f"Markdown relative links OK: {len(files)} files")
    return 0


if __name__ == "__main__":
    sys.exit(main())

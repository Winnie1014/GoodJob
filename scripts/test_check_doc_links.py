from __future__ import annotations

import importlib.util
import io
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path, PureWindowsPath
from types import ModuleType
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
CHECKER = ROOT / "scripts" / "check-doc-links.py"


class CheckerCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.repo = Path(self.temporary_directory.name)
        (self.repo / "scripts").mkdir()
        shutil.copy2(CHECKER, self.repo / "scripts" / CHECKER.name)
        subprocess.run(
            ["git", "init", "-q", str(self.repo)],
            check=True,
            capture_output=True,
            text=True,
        )

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def write(self, relative_path: str, content: str = "") -> Path:
        path = self.repo / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def run_checker(self) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "scripts/check-doc-links.py"],
            cwd=self.repo,
            check=False,
            capture_output=True,
            text=True,
        )

    def load_checker(self) -> ModuleType:
        checker_path = self.repo / "scripts" / CHECKER.name
        spec = importlib.util.spec_from_file_location("check_doc_links", checker_path)
        assert spec is not None and spec.loader is not None
        checker = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(checker)
        assert isinstance(checker, ModuleType)
        return checker

    def run_checker_as_windows(
        self, *, reparse_path: Path | None = None
    ) -> subprocess.CompletedProcess[str]:
        checker_path = self.repo / "scripts" / CHECKER.name

        missing = object()
        saved_flags: dict[str, object] = {}
        for name in ("O_DIRECTORY", "O_NOFOLLOW", "O_CLOEXEC"):
            saved_flags[name] = getattr(os, name, missing)
            if saved_flags[name] is not missing:
                delattr(os, name)

        original_open = os.open
        original_stat = os.stat
        original_lstat = os.lstat

        def reject_dir_fd_open(*args: object, **kwargs: object) -> int:
            if kwargs.get("dir_fd") is not None:
                raise AssertionError("Windows branch used os.open(dir_fd=...)")
            return original_open(*args, **kwargs)

        def reject_dir_fd_stat(*args: object, **kwargs: object) -> os.stat_result:
            if kwargs.get("dir_fd") is not None:
                raise AssertionError("Windows branch used os.stat(dir_fd=...)")
            return original_stat(*args, **kwargs)

        def reject_lstat(*args: object, **kwargs: object) -> os.stat_result:
            del args, kwargs
            raise AssertionError("Windows branch used os.lstat()")

        def windows_file_attributes(path: Path) -> int | None:
            try:
                info = original_lstat(path)
            except OSError:
                return None
            attributes = 0x10 if stat.S_ISDIR(info.st_mode) else 0
            if reparse_path is not None and path == reparse_path:
                attributes |= 0x400
            return attributes

        try:
            checker = self.load_checker()
            checker.ROOT = self.repo
            checker.IS_WINDOWS = True
            checker.windows_file_attributes = windows_file_attributes
            stdout = io.StringIO()
            with (
                mock.patch.object(os, "open", reject_dir_fd_open),
                mock.patch.object(os, "stat", reject_dir_fd_stat),
                mock.patch.object(os, "lstat", reject_lstat),
                redirect_stdout(stdout),
            ):
                returncode = checker.main()
        finally:
            for name, value in saved_flags.items():
                if value is not missing:
                    setattr(os, name, value)

        return subprocess.CompletedProcess(
            [sys.executable, str(checker_path)],
            returncode,
            stdout.getvalue(),
            "",
        )

    def require_symlink_creation(self) -> None:
        target = self.write(".symlink-capability-target", "probe\n")
        link = self.repo / ".symlink-capability-link"
        try:
            link.symlink_to(target.name)
        except OSError as error:
            if sys.platform == "win32" and getattr(error, "winerror", None) in {
                5,
                1314,
            }:
                self.skipTest("Windows token cannot create symbolic links")
            raise
        finally:
            link.unlink(missing_ok=True)
            target.unlink(missing_ok=True)

    def assert_clean(self, result: subprocess.CompletedProcess[str], files: int) -> None:
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, f"Markdown relative links OK: {files} files\n")

    def test_broken_relative_link_reports_source_and_target(self) -> None:
        self.write("docs/source.md", "[missing](missing.md)\n")

        result = self.run_checker()

        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "docs/source.md:missing.md\n")

    def test_broken_link_diagnostic_normalizes_windows_separators(self) -> None:
        checker = self.load_checker()

        diagnostic = checker.format_broken_link(
            PureWindowsPath(r"docs\source.md"), "missing.md"
        )

        self.assertEqual(diagnostic, "docs/source.md:missing.md")

    def test_fenced_and_unfenced_links_are_distinguished(self) -> None:
        self.write(
            "docs/source.md",
            """```markdown
[missing](missing.md)
```
~~~markdown
[missing](missing.md)
~~~
[missing](missing.md)
""",
        )

        result = self.run_checker()

        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "docs/source.md:missing.md\n")

    def test_inline_code_and_visible_links_are_distinguished(self) -> None:
        self.write(
            "docs/source.md",
            "`[missing](missing.md)`\n[missing](missing.md)\n",
        )

        result = self.run_checker()

        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "docs/source.md:missing.md\n")

    def test_escaped_opening_bracket_is_not_a_link(self) -> None:
        self.write(
            "docs/source.md",
            "\\[escaped](missing-escaped.md)\n[visible](missing-visible.md)\n",
        )

        result = self.run_checker()

        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "docs/source.md:missing-visible.md\n")

    def test_fragment_query_and_percent_encoded_targets_resolve(self) -> None:
        self.write("docs/target#hash.md", "# Encoded target\n")
        self.write(
            "docs/source.md",
            """[fragment](missing-fragment.md#section)
[query](missing-query.md?view=1)
[encoded existing](target%23hash.md)
[encoded missing](missing%23hash.md)
""",
        )

        result = self.run_checker()

        self.assertEqual(result.returncode, 1)
        self.assertEqual(
            result.stdout.splitlines(),
            [
                "docs/source.md:missing-fragment.md#section",
                "docs/source.md:missing-query.md?view=1",
                "docs/source.md:missing%23hash.md",
            ],
        )

    def test_external_schemes_are_skipped(self) -> None:
        self.write(
            "docs/source.md",
            """[http](http://example.com/missing.md)
[https](https://example.com/missing.md)
[mail](mailto:person@example.com)
""",
        )

        self.assert_clean(self.run_checker(), files=1)

    def test_pure_anchor_is_skipped(self) -> None:
        self.write("docs/source.md", "[section](#details)\n")

        self.assert_clean(self.run_checker(), files=1)

    def test_malformed_links_and_code_boundaries_do_not_create_links(self) -> None:
        self.write(
            "docs/source.md",
            """[label]`code`(missing-concatenated.md)
[unclosed](missing-unclosed.md "title"
\\](missing-no-opening.md)
""",
        )

        self.assert_clean(self.run_checker(), files=1)

    def test_escaped_backticks_do_not_hide_a_real_link(self) -> None:
        self.write("docs/source.md", "\\`[missing](missing.md)\\`\n")

        result = self.run_checker()

        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "docs/source.md:missing.md\n")

    def test_multibacktick_code_span_hides_inner_backticks_and_link(self) -> None:
        self.write(
            "docs/source.md",
            "`` `[hidden](missing-hidden.md)` ``\n[visible](missing-visible.md)\n",
        )

        result = self.run_checker()

        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "docs/source.md:missing-visible.md\n")

    def test_git_enumeration_includes_untracked_and_excludes_ignored_markdown(self) -> None:
        self.write("included.md", "# Included\n")
        self.write("ignored/hidden.md", "# Ignored\n")
        self.write(".gitignore", "ignored/\n")

        self.assert_clean(self.run_checker(), files=1)

    def test_tracked_markdown_is_scanned_while_missing_worktree_file_is_skipped(self) -> None:
        missing = self.write("removed.md", "# Removed\n")
        self.write("tracked.md", "[missing](missing.md)\n")
        subprocess.run(
            ["git", "add", "removed.md", "tracked.md"],
            cwd=self.repo,
            check=True,
            capture_output=True,
            text=True,
        )
        missing.unlink()

        result = self.run_checker()

        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "tracked.md:missing.md\n")

    def test_blockquote_and_list_fences_hide_links(self) -> None:
        self.write(
            "docs/source.md",
            """> ~~~markdown
> [blockquote](missing-blockquote.md)
> ~~~

10. list item

    ~~~markdown
    [list](missing-list.md)
    ~~~
""",
        )

        self.assert_clean(self.run_checker(), files=1)

    def test_complete_inline_link_forms_report_their_targets(self) -> None:
        self.write(
            "docs/source.md",
            """[nested [label]](missing-one.md "title")
[single title](missing-two.md 'title')
[parenthesized title](missing-three.md (title))
[angle target](<missing four.md> "title")
[balanced target](missing(five).md)
[escaped target](missing\\)six.md)
""",
        )

        result = self.run_checker()

        self.assertEqual(result.returncode, 1)
        self.assertEqual(
            result.stdout.splitlines(),
            [
                "docs/source.md:missing-one.md",
                "docs/source.md:missing-two.md",
                "docs/source.md:missing-three.md",
                "docs/source.md:missing four.md",
                "docs/source.md:missing(five).md",
                "docs/source.md:missing\\)six.md",
            ],
        )

    def test_absolute_escaping_and_unsupported_scheme_targets_are_rejected(self) -> None:
        self.write(
            "docs/source.md",
            """[absolute](/etc/passwd)
[escape](../../etc/passwd)
[ftp](ftp://example.com/file.md)
""",
        )

        result = self.run_checker()

        self.assertEqual(result.returncode, 1)
        self.assertEqual(
            result.stdout.splitlines(),
            [
                "docs/source.md:/etc/passwd",
                "docs/source.md:../../etc/passwd",
                "docs/source.md:ftp://example.com/file.md",
            ],
        )

    def test_invalid_url_syntax_is_reported_as_broken(self) -> None:
        self.write("docs/source.md", "[invalid](http://[broken)\n")

        result = self.run_checker()

        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "docs/source.md:http://[broken\n")

    def test_repository_directories_are_valid_targets(self) -> None:
        self.write("docs/source.md", "[root](..)\n[folder](folder/)\n")
        (self.repo / "docs" / "folder").mkdir()

        self.assert_clean(self.run_checker(), files=1)

    def test_windows_branch_runs_without_posix_flags_or_dir_fd(self) -> None:
        self.write("docs/target.txt", "target\n")
        self.write("docs/source.md", "[target](target.txt)\n")

        self.assert_clean(self.run_checker_as_windows(), files=1)

    def test_windows_reparse_component_is_rejected_without_symlink_privilege(
        self,
    ) -> None:
        reparse_directory = self.repo / "docs" / "reparse"
        self.write("docs/reparse/target.txt", "target\n")
        self.write("docs/source.md", "[target](reparse/target.txt)\n")

        result = self.run_checker_as_windows(reparse_path=reparse_directory)

        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "docs/source.md:reparse/target.txt\n")

    def test_markdown_source_symlink_is_not_scanned(self) -> None:
        self.require_symlink_creation()
        self.write("outside.txt", "[missing](missing.md)\n")
        (self.repo / "docs").mkdir()
        (self.repo / "docs" / "external.md").symlink_to("../outside.txt")

        self.assert_clean(self.run_checker(), files=0)

    def test_symlink_targets_are_rejected(self) -> None:
        self.require_symlink_creation()
        self.write("docs/real-file.txt", "target\n")
        self.write("docs/real-dir/target.txt", "target\n")
        (self.repo / "docs" / "file-link.md").symlink_to("real-file.txt")
        (self.repo / "docs" / "dir-link").symlink_to("real-dir", target_is_directory=True)
        self.write(
            "docs/source.md",
            "[file](file-link.md)\n[directory](dir-link/target.txt)\n",
        )

        result = self.run_checker()

        self.assertEqual(result.returncode, 1)
        self.assertEqual(
            result.stdout.splitlines(),
            ["docs/source.md:file-link.md", "docs/source.md:dir-link/target.txt"],
        )


class GateIntegrationTests(unittest.TestCase):
    def test_gate_docs_runs_tests_before_checker(self) -> None:
        result = subprocess.run(
            ["make", "--no-print-directory", "-n", "gate-docs"],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        )

        self.assertEqual(
            result.stdout.splitlines(),
            [
                "python3 -m unittest scripts/test_check_doc_links.py",
                "python3 scripts/check-doc-links.py",
            ],
        )


if __name__ == "__main__":
    unittest.main()

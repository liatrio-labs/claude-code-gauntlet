"""Tests for scripts/ensure_output_dir.py (Issue #86).

Containment gate: resolve absolute output dir, establish ignore via
``git rev-parse --git-path info/exclude``, mkdir only after the gate passes.

Property on every in-repo success: exit 0 implies ``git check-ignore -q`` passes
for the resolved path.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import ensure_output_dir as eod  # noqa: E402


def _git(cwd: str, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=check,
        text=True,
        capture_output=True,
    )


def _init_repo(path: str) -> None:
    _git(path, "init")
    _git(path, "config", "user.email", "test@example.com")
    _git(path, "config", "user.name", "Test")
    # Need a commit before worktree add in some git versions.
    (Path(path) / "README").write_text("x\n", encoding="utf-8")
    _git(path, "add", "README")
    _git(path, "commit", "-m", "init")


def _check_ignore(cwd: str, path: str) -> int:
    return subprocess.run(
        ["git", "check-ignore", "-q", "--", path],
        cwd=cwd,
        capture_output=True,
    ).returncode


def _run(
    cwd: str,
    env: dict[str, str] | None = None,
    argv: list[str] | None = None,
) -> tuple[int, str, str]:
    environ = {**os.environ, **(env or {})}
    # Drop inherited override unless the test sets it.
    if env is None or "CODE_GAUNTLET_OUTPUT_DIR" not in env:
        environ.pop("CODE_GAUNTLET_OUTPUT_DIR", None)
    return eod.run(argv=argv or ["--cwd", cwd], environ=environ)


class TestEnsureOutputDir(unittest.TestCase):
    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory(prefix="cg-ensure-")
        self.repo = self._td.name
        _init_repo(self.repo)

    def tearDown(self) -> None:
        # Un-chmod anything we locked so cleanup can remove it.
        for root, dirs, files in os.walk(self.repo):
            for name in dirs + files:
                p = os.path.join(root, name)
                try:
                    os.chmod(p, stat.S_IRWXU)
                except OSError:
                    pass
        self._td.cleanup()

    def _assert_in_repo_success_ignored(self, code: int, stdout: str) -> str:
        self.assertEqual(code, 0, f"stdout={stdout!r}")
        abs_path = stdout.strip()
        self.assertTrue(abs_path, "stdout must carry the absolute path")
        self.assertTrue(os.path.isabs(abs_path))
        self.assertTrue(os.path.isdir(abs_path))
        self.assertEqual(
            _check_ignore(self.repo, abs_path),
            0,
            f"exit 0 must imply check-ignore passes for {abs_path}",
        )
        return abs_path

    def test_default_creates_ignored_dir(self) -> None:
        code, out, err = _run(self.repo)
        abs_path = self._assert_in_repo_success_ignored(code, out)
        self.assertTrue(abs_path.endswith(".code-gauntlet") or abs_path.endswith("/.code-gauntlet"))
        exclude = _git(self.repo, "rev-parse", "--git-path", "info/exclude").stdout.strip()
        if not os.path.isabs(exclude):
            exclude = os.path.join(self.repo, exclude)
        self.assertIn("/.code-gauntlet/", Path(exclude).read_text(encoding="utf-8"))

    def test_rerun_idempotent_exclude_unchanged(self) -> None:
        code1, out1, _ = _run(self.repo)
        abs1 = self._assert_in_repo_success_ignored(code1, out1)
        exclude = _git(self.repo, "rev-parse", "--git-path", "info/exclude").stdout.strip()
        if not os.path.isabs(exclude):
            exclude = os.path.join(self.repo, exclude)
        before = Path(exclude).read_bytes()
        code2, out2, _ = _run(self.repo)
        abs2 = self._assert_in_repo_success_ignored(code2, out2)
        self.assertEqual(abs1, abs2)
        self.assertEqual(before, Path(exclude).read_bytes())

    def test_already_gitignore_short_circuits_exclude_write(self) -> None:
        gi = Path(self.repo) / ".gitignore"
        gi.write_text("/.code-gauntlet/\n", encoding="utf-8")
        _git(self.repo, "add", ".gitignore")
        _git(self.repo, "commit", "-m", "ignore")
        exclude = _git(self.repo, "rev-parse", "--git-path", "info/exclude").stdout.strip()
        if not os.path.isabs(exclude):
            exclude = os.path.join(self.repo, exclude)
        # Ensure exclude file exists but stays empty of our pattern.
        Path(exclude).parent.mkdir(parents=True, exist_ok=True)
        Path(exclude).write_text("", encoding="utf-8")
        before = Path(exclude).read_bytes()
        code, out, _ = _run(self.repo)
        self._assert_in_repo_success_ignored(code, out)
        self.assertEqual(before, Path(exclude).read_bytes())

    def test_nested_in_repo_override_pattern(self) -> None:
        code, out, _ = _run(
            self.repo, env={"CODE_GAUNTLET_OUTPUT_DIR": "artifacts/out"}
        )
        abs_path = self._assert_in_repo_success_ignored(code, out)
        self.assertTrue(abs_path.endswith("/artifacts/out") or abs_path.endswith("/artifacts/out/"))
        exclude = _git(self.repo, "rev-parse", "--git-path", "info/exclude").stdout.strip()
        if not os.path.isabs(exclude):
            exclude = os.path.join(self.repo, exclude)
        self.assertIn("/artifacts/out/", Path(exclude).read_text(encoding="utf-8"))

    def test_out_of_repo_skips_exclude(self) -> None:
        outside = tempfile.mkdtemp(prefix="cg-outside-")
        self.addCleanup(shutil.rmtree, outside, ignore_errors=True)
        target = os.path.join(outside, "gauntlet-out")
        code, out, err = _run(self.repo, env={"CODE_GAUNTLET_OUTPUT_DIR": target})
        self.assertEqual(code, 0)
        abs_path = out.strip()
        self.assertEqual(os.path.realpath(abs_path), os.path.realpath(target))
        self.assertTrue(os.path.isdir(abs_path))
        self.assertIn("outside-repo", err)
        # Not in the reviewed tree — check-ignore from repo may not apply; property exempt.
        exclude = _git(self.repo, "rev-parse", "--git-path", "info/exclude").stdout.strip()
        if not os.path.isabs(exclude):
            exclude = os.path.join(self.repo, exclude)
        if os.path.isfile(exclude):
            self.assertNotIn("gauntlet-out", Path(exclude).read_text(encoding="utf-8"))

    def test_unwritable_exclude_hard_stop_no_mkdir(self) -> None:
        exclude = _git(self.repo, "rev-parse", "--git-path", "info/exclude").stdout.strip()
        if not os.path.isabs(exclude):
            exclude = os.path.join(self.repo, exclude)
        info_dir = os.path.dirname(exclude)
        os.makedirs(info_dir, exist_ok=True)
        # Ensure the exclude file exists, then lock file + dir so append cannot write.
        Path(exclude).write_text("# locked\n", encoding="utf-8")
        os.chmod(exclude, 0o444)
        os.chmod(info_dir, 0o555)
        out_dir = os.path.join(self.repo, ".code-gauntlet")
        self.assertFalse(os.path.exists(out_dir))
        code, out, err = _run(self.repo)
        self.assertEqual(code, 1)
        self.assertEqual(out.strip(), "")
        self.assertFalse(os.path.exists(out_dir))
        self.assertIn("CODE_GAUNTLET_OUTPUT_DIR", err)
        os.chmod(info_dir, 0o755)
        os.chmod(exclude, 0o644)

    def test_verify_after_append_failure_no_mkdir(self) -> None:
        """If append does not make check-ignore pass, exit 1 and do not mkdir."""

        def _bad_append(exclude_path: str, pattern: str) -> None:
            Path(exclude_path).parent.mkdir(parents=True, exist_ok=True)
            with open(exclude_path, "a", encoding="utf-8") as fh:
                fh.write("/definitely-not-the-right-pattern/\n")

        out_dir = os.path.join(self.repo, ".code-gauntlet")
        with unittest.mock.patch.object(eod, "append_exclude_pattern", _bad_append):
            code, out, err = _run(self.repo)
        self.assertEqual(code, 1)
        self.assertEqual(out.strip(), "")
        self.assertFalse(os.path.exists(out_dir))

    def test_abs_equals_repo_root_is_usage_error(self) -> None:
        code, out, err = _run(
            self.repo, env={"CODE_GAUNTLET_OUTPUT_DIR": self.repo}
        )
        self.assertEqual(code, 2)
        self.assertEqual(out.strip(), "")
        self.assertIn("repo root", err.lower())

    def test_check_ignore_128_is_usage_error(self) -> None:
        def boom(cwd: str, path: str) -> int:
            return 128

        with unittest.mock.patch.object(eod, "git_check_ignore", boom):
            code, out, err = _run(self.repo)
        self.assertEqual(code, 2)
        self.assertEqual(out.strip(), "")
        self.assertIn("check-ignore", err.lower())
        # Ensure we didn't leave a partial dir from a bad path (gate before mkdir).
        self.assertFalse(os.path.exists(os.path.join(self.repo, ".code-gauntlet")))

    def test_empty_env_is_usage_error(self) -> None:
        code, out, err = _run(self.repo, env={"CODE_GAUNTLET_OUTPUT_DIR": "  "})
        self.assertEqual(code, 2)
        self.assertEqual(out.strip(), "")

    def test_mkdir_failure_exit_1_empty_stdout(self) -> None:
        # Place a file where the directory should be created.
        blocker = Path(self.repo) / ".code-gauntlet"
        blocker.write_text("not a dir\n", encoding="utf-8")
        # Pre-ignore so we get past the ignore gate to mkdir.
        exclude = _git(self.repo, "rev-parse", "--git-path", "info/exclude").stdout.strip()
        if not os.path.isabs(exclude):
            exclude = os.path.join(self.repo, exclude)
        Path(exclude).parent.mkdir(parents=True, exist_ok=True)
        Path(exclude).write_text("/.code-gauntlet/\n", encoding="utf-8")
        code, out, err = _run(self.repo)
        self.assertEqual(code, 1)
        self.assertEqual(out.strip(), "")
        self.assertIn("mkdir", err.lower())

    def test_worktree_uses_git_path_exclude(self) -> None:
        wt = tempfile.mkdtemp(prefix="cg-wt-")
        self.addCleanup(shutil.rmtree, wt, ignore_errors=True)
        # Remove empty dir so worktree add can create it.
        os.rmdir(wt)
        _git(self.repo, "worktree", "add", wt, "HEAD")
        code, out, err = _run(wt)
        abs_path = out.strip()
        self.assertEqual(code, 0, err)
        self.assertTrue(os.path.isdir(abs_path))
        self.assertEqual(_check_ignore(wt, abs_path), 0)
        # Exclude lives under the common git dir, not wt/.git/info as a real dir.
        exclude = _git(wt, "rev-parse", "--git-path", "info/exclude").stdout.strip()
        if not os.path.isabs(exclude):
            exclude = os.path.join(wt, exclude)
        self.assertTrue(os.path.isfile(exclude))
        self.assertIn("/.code-gauntlet/", Path(exclude).read_text(encoding="utf-8"))

    def test_symlink_into_repo_classified_in_repo(self) -> None:
        outside = tempfile.mkdtemp(prefix="cg-sym-")
        self.addCleanup(shutil.rmtree, outside, ignore_errors=True)
        inner = Path(self.repo) / "nested"
        inner.mkdir()
        link = Path(outside) / "into-repo"
        link.symlink_to(inner)
        override = str(link / "out")
        code, out, err = _run(self.repo, env={"CODE_GAUNTLET_OUTPUT_DIR": override})
        abs_path = self._assert_in_repo_success_ignored(code, out)
        self.assertTrue(os.path.realpath(abs_path).startswith(os.path.realpath(self.repo)))
        self.assertNotIn("outside-repo", err)

    def test_exclude_without_trailing_newline(self) -> None:
        exclude = _git(self.repo, "rev-parse", "--git-path", "info/exclude").stdout.strip()
        if not os.path.isabs(exclude):
            exclude = os.path.join(self.repo, exclude)
        Path(exclude).parent.mkdir(parents=True, exist_ok=True)
        Path(exclude).write_bytes(b"# prior line without newline")
        code, out, _ = _run(self.repo)
        self._assert_in_repo_success_ignored(code, out)
        text = Path(exclude).read_text(encoding="utf-8")
        self.assertIn("/.code-gauntlet/", text)
        # Pattern must be on its own line, not concatenated.
        self.assertNotIn("newline/.code-gauntlet", text)


if __name__ == "__main__":
    unittest.main()

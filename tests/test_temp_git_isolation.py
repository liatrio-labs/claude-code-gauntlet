"""Reproduce the hostile-TMPDIR-inside-a-work-tree environment end to end and
confirm the root conftest.py's isolation keeps the suite green and the
enclosing repository untouched.

The scenario: a pytest run whose temp directory sits inside a git work tree,
and whose environment already carries git's repository-local variables (as a
git hook running pytest at pre-push does). Without the isolation, git walks
up from a test's own "not a git repository" fixture and discovers the
enclosing repository instead, both breaking the "not a repo" assumption and
risking a write against that repository's HEAD, config or index.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

CHILD_TIMEOUT_SECONDS = 300

NODE_IDS = (
    "tests/test_ensure_output_dir.py::TestEnsureOutputDir::"
    "test_not_a_git_repository_is_usage_error",
    "tests/test_render_fix_tasks.py::RenderFixTasksTest::"
    "test_root_level_siblings_and_non_git_root",
    "tests/test_resolve_config.py::TestResolverCli::"
    "test_usage_and_setup_fail_without_process_stream_noise",
    "bench/tests/test_report.py::TestCli::test_git_sha_tolerates_non_repo",
    "tests/test_await_workflow.py::TestResolveTarget::"
    "test_missing_getuid_uses_system_temp_root",
)


class TestSuiteIsHermeticToATempRootInsideAWorkTree(unittest.TestCase):
    def test_suite_is_hermetic_to_a_temp_root_inside_a_work_tree(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            outer = Path(td) / "claude-502" / "outer"
            outer.mkdir(parents=True)
            hostile_tmp = outer / "tmp"
            hostile_tmp.mkdir()

            git_env = {
                "GIT_CONFIG_GLOBAL": "/dev/null",
                "GIT_CONFIG_SYSTEM": "/dev/null",
                "GIT_AUTHOR_NAME": "cg-hermetic-test",
                "GIT_AUTHOR_EMAIL": "cg-hermetic-test@example.invalid",
                "GIT_COMMITTER_NAME": "cg-hermetic-test",
                "GIT_COMMITTER_EMAIL": "cg-hermetic-test@example.invalid",
            }

            def run_git(*args: str) -> None:
                subprocess.run(
                    ["git", *args],
                    cwd=outer,
                    env=git_env,
                    check=True,
                    text=True,
                    encoding="utf-8",
                    capture_output=True,
                )

            run_git("init", "-q")
            (outer / "committed.txt").write_text("hello\n", encoding="utf-8")
            run_git("add", "committed.txt")
            run_git(
                "-c",
                "commit.gpgsign=false",
                "commit",
                "-q",
                "-m",
                "seed commit",
            )

            head_before = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=outer,
                env=git_env,
                check=True,
                text=True,
                encoding="utf-8",
                capture_output=True,
            ).stdout.strip()

            child_env = dict(os.environ)
            for name in ("TMPDIR", "TEMP", "TMP"):
                child_env[name] = str(hostile_tmp)
            child_env["GIT_DIR"] = str(outer / ".git")
            child_env["GIT_WORK_TREE"] = str(outer)
            child_env.pop("GIT_CEILING_DIRECTORIES", None)

            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "pytest",
                    "-q",
                    "-p",
                    "no:cacheprovider",
                    *NODE_IDS,
                ],
                cwd=REPO_ROOT,
                env=child_env,
                text=True,
                encoding="utf-8",
                capture_output=True,
                timeout=CHILD_TIMEOUT_SECONDS,
            )

            self.assertEqual(
                result.returncode,
                0,
                "hermetic child run failed:\n"
                f"stdout:\n{result.stdout}\n"
                f"stderr:\n{result.stderr}",
            )

            head_after = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=outer,
                env=git_env,
                check=True,
                text=True,
                encoding="utf-8",
                capture_output=True,
            ).stdout.strip()
            self.assertEqual(head_before, head_after)

            is_bare = subprocess.run(
                ["git", "rev-parse", "--is-bare-repository"],
                cwd=outer,
                env=git_env,
                check=True,
                text=True,
                encoding="utf-8",
                capture_output=True,
            ).stdout.strip()
            self.assertNotEqual(is_bare, "true")


if __name__ == "__main__":
    unittest.main()

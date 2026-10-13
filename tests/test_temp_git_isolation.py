from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
CHILD_TIMEOUT_SECONDS = 300
NODE_IDS = (
    "tests/test_output_dir.py::test_not_a_git_repository_is_usage_error",
    "tests/test_fix_tasks.py::test_root_level_siblings_and_non_git_root",
    "tests/test_config.py::test_usage_and_setup_fail_without_process_stream_noise[nonrepo]",
    "bench/tests/test_report.py::TestCli::test_git_sha_tolerates_non_repo",
    "tests/test_tasks.py::"
    "test_real_default_discovery_preserves_order_and_deduplicates[no-getuid-system-temp]",
)


def _git_env() -> dict[str, str]:
    # A seed commit must not depend on the invoking machine's identity or config.
    return {
        **os.environ,
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_AUTHOR_NAME": "cg-hermetic-test",
        "GIT_AUTHOR_EMAIL": "cg-hermetic-test@example.invalid",
        "GIT_COMMITTER_NAME": "cg-hermetic-test",
        "GIT_COMMITTER_EMAIL": "cg-hermetic-test@example.invalid",
    }


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        env=_git_env(),
        check=True,
        text=True,
        encoding="utf-8",
        capture_output=True,
        timeout=60,
    )
    output: str = result.stdout
    return output


@pytest.fixture
def outer_repository(tmp_path: Path) -> Path:
    outer = tmp_path / "claude-502" / "outer"
    outer.mkdir(parents=True)
    (outer / "tmp").mkdir()
    _git(outer, "init", "-q")
    (outer / "committed.txt").write_text("hello\n", encoding="utf-8")
    _git(outer, "add", "committed.txt")
    _git(outer, "-c", "commit.gpgsign=false", "commit", "-q", "-m", "seed commit")
    return outer


def _repository_facts(outer: Path) -> dict[str, str]:
    return {
        "HEAD": _git(outer, "rev-parse", "HEAD").strip(),
        "is-bare": _git(outer, "rev-parse", "--is-bare-repository").strip(),
        "core.bare": _git(outer, "config", "--local", "--get", "core.bare").strip(),
        "local-config": _git(outer, "config", "--local", "--list"),
        "index": hashlib.sha256((outer / ".git" / "index").read_bytes()).hexdigest(),
    }


def test_suite_is_hermetic_to_a_temp_root_inside_a_work_tree(
    outer_repository: Path,
) -> None:
    # A hook-inherited GIT_DIR plus a temp root inside a work tree can redirect writes.
    outer = outer_repository
    before = _repository_facts(outer)
    child_env = dict(os.environ)
    for name in ("TMPDIR", "TEMP", "TMP"):
        child_env[name] = str(outer / "tmp")
    child_env["GIT_DIR"] = str(outer / ".git")
    child_env["GIT_WORK_TREE"] = str(outer)
    child_env.pop("GIT_CEILING_DIRECTORIES", None)
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", *NODE_IDS],
        cwd=REPO_ROOT,
        env=child_env,
        text=True,
        encoding="utf-8",
        capture_output=True,
        timeout=CHILD_TIMEOUT_SECONDS,
    )
    after = _repository_facts(outer)
    assert after == before
    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"

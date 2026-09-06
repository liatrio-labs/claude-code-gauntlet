"""Structural guard for the repository tooling boundary (#176).

The tooling boundary in AGENTS.md was convention-only before this test. The
vendored scorer's own ``bench/vendor/code-review-benchmark/uv.lock`` is the one
exact exemption, documented at bench/vendor/VENDORED.md:85-87; that vendored
third-party tree is exempt from the stdlib rule and runs under uv against its
own lockfile.
"""

import subprocess
import unittest
from collections.abc import Iterable
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
FORBIDDEN_BASENAMES = (
    "package.json",
    "package-lock.json",
    "npm-shrinkwrap.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "bun.lock",
    "bun.lockb",
    "uv.lock",
    "poetry.lock",
    "Pipfile",
    "Pipfile.lock",
)
EXEMPT_PATH = "bench/vendor/code-review-benchmark/uv.lock"


def violations(paths: Iterable[str]) -> list[str]:
    """Return tracked paths that cross the local tooling boundary."""
    offenders = []
    for path in paths:
        if path == EXEMPT_PATH:
            continue
        path_parts = Path(path).parts
        if Path(path).name in FORBIDDEN_BASENAMES or "node_modules" in path_parts:
            offenders.append(path)
    return offenders


class TestToolingBoundary(unittest.TestCase):
    def test_tracked_tooling_files_stay_outside_the_vendored_scorer(self) -> None:
        result = subprocess.run(
            ["git", "ls-files", "-z"],
            cwd=REPO,
            check=True,
            capture_output=True,
        )
        paths = [path for path in result.stdout.decode().split("\0") if path]
        self.assertEqual(violations(paths), [])
        self.assertEqual(paths.count(EXEMPT_PATH), 1)

    def test_violations_identifies_forbidden_synthetic_paths(self) -> None:
        paths = [
            "a/package.json",
            "x/y/node_modules/z.js",
            "bun.lockb",
            EXEMPT_PATH,
            "docs/uv.lock",
        ]
        self.assertEqual(
            violations(paths),
            ["a/package.json", "x/y/node_modules/z.js", "bun.lockb", "docs/uv.lock"],
        )

    def test_uv_lock_is_ignored_for_local_ci_tooling(self) -> None:
        result = subprocess.run(
            ["git", "check-ignore", "-q", "--", "uv.lock"],
            cwd=REPO,
        )
        self.assertEqual(result.returncode, 0)


if __name__ == "__main__":
    unittest.main()

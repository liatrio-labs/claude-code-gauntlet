import subprocess
from collections.abc import Iterable
from pathlib import Path

import pytest

from tests.support.repository import tracked_paths

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
# Only the vendored scorer's own lock belongs to its independent dependency tree.
EXEMPT_PATH = "bench/vendor/code-review-benchmark/uv.lock"


def violations(paths: Iterable[str]) -> list[str]:
    offenders = []
    for path in paths:
        if path == EXEMPT_PATH:
            continue
        candidate = Path(path)
        if candidate.name in FORBIDDEN_BASENAMES or "node_modules" in candidate.parts:
            offenders.append(path)
    return offenders


def test_tracked_tooling_files_stay_outside_the_vendored_scorer():
    paths = tracked_paths(REPO)
    assert violations(paths) == []
    assert paths.count(EXEMPT_PATH) == 1


@pytest.mark.parametrize(
    "rejected, exempt",
    [
        pytest.param(
            [
                "dir/package.json",
                "dir/package-lock.json",
                "dir/npm-shrinkwrap.json",
                "dir/yarn.lock",
                "dir/pnpm-lock.yaml",
                "dir/bun.lock",
                "dir/bun.lockb",
                "dir/uv.lock",
                "dir/poetry.lock",
                "dir/Pipfile",
                "dir/Pipfile.lock",
                "nested/node_modules/package.json",
            ],
            [],
            id="every-forbidden-basename-and-nested-manifest",
        ),
        pytest.param(
            [
                "x/y/node_modules/z.js",
                "bench/vendor/other/package.json",
                "bench/vendor/code-review-benchmark/package-lock.json",
            ],
            ["bench/vendor/code-review-benchmark/uv.lock"],
            id="source-and-vendor-rejections-with-exact-exemption",
        ),
    ],
)
def test_every_forbidden_basename_is_detected(rejected, exempt):
    # A table derived from the policy would hide a deleted policy arm.
    assert set(FORBIDDEN_BASENAMES) == {
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
    }
    assert violations([*rejected, *exempt]) == rejected


def test_uv_lock_is_ignored_for_local_ci_tooling():
    result = subprocess.run(["git", "check-ignore", "-q", "--", "uv.lock"], cwd=REPO)
    assert result.returncode == 0

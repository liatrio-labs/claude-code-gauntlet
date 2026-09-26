"""Make the pytest process hermetic to an ambient TMPDIR and to git's own
repository-local environment variables.

Two things outside a checkout can make its suites lie about what they cover:

* A hostile ambient temp directory. If ``TMPDIR`` (or the platform's ``TEMP``/
  ``TMP``) already points somewhere inside a git work tree — as it does inside
  a linked worktree at ``pre-commit``/``pre-push`` hook time, where git itself
  sets the temp root — anything this process writes under
  ``tempfile.gettempdir()`` lands inside that other repository instead of a
  scratch area, and a git command run from there discovers and can mutate it.
* Leaked git environment variables. Git exports ``GIT_DIR``,
  ``GIT_WORK_TREE`` and the rest of its repository-local variables
  (``git rev-parse --local-env-vars``) into any process it spawns, including a
  hook. A pytest run started that way inherits them, so a git subprocess a
  test issues resolves against the enclosing repository instead of the one
  the test built, unless those variables are cleared first.

This module fixes both for the lifetime of the pytest process: it points the
temp environment at a private, single-use directory and pops every
git-local variable, restoring everything at teardown.

Covers: git repository discovery and git environment leakage into
subprocesses spawned during the run, and the shape of temp-root names it
manufactures. Does not cover: a test that runs git with its own scrubbed
environment (nothing here re-injects variables a test itself removed), or a
run started outside pytest (e.g. ``python -m unittest`` never loads this
file). ``pytest --basetemp`` still lands under the private root created here,
since ``tempfile.tempdir`` is what basetemp resolution is relative to.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from typing import Any


class _SessionState:
    """Everything pytest_unconfigure needs to put the environment back."""

    def __init__(self) -> None:
        self.root: str | None = None
        self.saved_tempdir: str | None = None
        self.saved_env: dict[str, str | None] = {}


_STATE = _SessionState()

_TEMP_VARS = ("TMPDIR", "TEMP", "TMP")


def _git_local_env_vars() -> list[str]:
    result = subprocess.run(
        ["git", "rev-parse", "--local-env-vars"],
        check=True,
        text=True,
        encoding="utf-8",
        capture_output=True,
    )
    return [name for name in result.stdout.splitlines() if name]


def pytest_configure(config: Any) -> None:
    root = tempfile.mkdtemp(prefix="cg-pytest-")
    _STATE.root = root

    for name in (*_TEMP_VARS, "GIT_CEILING_DIRECTORIES", *_git_local_env_vars()):
        _STATE.saved_env[name] = os.environ.pop(name, None)

    for name in _TEMP_VARS:
        os.environ[name] = root
    os.environ["GIT_CEILING_DIRECTORIES"] = os.path.realpath(root)

    _STATE.saved_tempdir = tempfile.tempdir
    tempfile.tempdir = root


def pytest_unconfigure(config: Any) -> None:
    for name, value in _STATE.saved_env.items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value

    tempfile.tempdir = _STATE.saved_tempdir

    if _STATE.root is not None:
        shutil.rmtree(_STATE.root, ignore_errors=True)

"""Isolate pytest temp files, git environment, and task discovery.
Use private temp and task roots and guard default discovery.
Restore the saved environment at teardown."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from collections.abc import Iterator
from typing import Any

import pytest


class _SessionState:
    """Everything pytest_unconfigure needs to put the environment back."""

    def __init__(self) -> None:
        self.root: str | None = None
        self.saved_tempdir: str | None = None
        self.saved_env: dict[str, str | None] = {}


_STATE = _SessionState()

_TEMP_VARS = ("TMPDIR", "TEMP", "TMP")
_TASK_ROOTS_VAR = "CODE_GAUNTLET_TASK_ROOTS"
_TASKS_DIR_VAR = "CODE_GAUNTLET_TASKS_DIR"


def _git_local_env_vars() -> list[str]:
    result = subprocess.run(
        ["git", "rev-parse", "--local-env-vars"],
        check=True,
        text=True,
        encoding="utf-8",
        capture_output=True,
        timeout=30,
    )
    return [name for name in result.stdout.splitlines() if name]


def pytest_configure(config: Any) -> None:
    # Snapshot everything BEFORE making any change: if the git call below
    # fails, nothing has been touched yet, so nothing leaks; and teardown
    # must only ever restore what was actually saved here.
    _STATE.saved_tempdir = tempfile.tempdir
    names_to_clear = (
        *_TEMP_VARS,
        _TASK_ROOTS_VAR,
        _TASKS_DIR_VAR,
        "GIT_CEILING_DIRECTORIES",
        *_git_local_env_vars(),
    )
    for name in names_to_clear:
        _STATE.saved_env[name] = os.environ.pop(name, None)

    # Temp-derived paths prove literal handling in every test that builds from them.
    # Use [g]: Windows forbids * and ?, and glob treats an unclosed [ literally.
    root = tempfile.mkdtemp(prefix="cg-pytest-[g]-")
    _STATE.root = root

    for name in _TEMP_VARS:
        os.environ[name] = root
    os.environ["GIT_CEILING_DIRECTORIES"] = os.path.realpath(root)

    task_root = os.path.join(root, "task-roots")
    os.mkdir(task_root)
    os.environ[_TASK_ROOTS_VAR] = task_root

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


@pytest.fixture(autouse=True)
def _guard_default_task_discovery(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    from gauntlet import tasks

    calls: list[str | None] = []

    def forbidden(tmpdir: str | None) -> tuple[str, ...]:
        calls.append(tmpdir)
        raise AssertionError("default task-root discovery is forbidden in tests")

    monkeypatch.setattr(tasks, "_default_roots", forbidden)
    yield
    # Production may have converted the exception into a receipt; still fail this test.
    if calls:
        pytest.fail("default task-root discovery was reached during the test")

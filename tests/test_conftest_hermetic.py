from __future__ import annotations

import glob
import os
import shutil
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import conftest as root_conftest  # noqa: E402


@pytest.fixture
def prior_hook_state() -> Iterator[str]:
    saved_environ = dict(os.environ)
    saved_tempdir = tempfile.tempdir
    saved_state = root_conftest._STATE
    prior_tempdir = tempfile.mkdtemp(prefix="cg-prior-sentinel-")
    root_conftest._STATE = root_conftest._SessionState()
    try:
        yield prior_tempdir
    finally:
        # Hook calls must not disturb the pytest session running this test.
        if root_conftest._STATE.root is not None:
            shutil.rmtree(root_conftest._STATE.root, ignore_errors=True)
        os.environ.clear()
        os.environ.update(saved_environ)
        tempfile.tempdir = saved_tempdir
        root_conftest._STATE = saved_state
        shutil.rmtree(prior_tempdir, ignore_errors=True)


def _process_facts() -> dict[str, str | bytes | None]:
    return {
        "tempdir": tempfile.tempdir,
        **{
            name: os.environ.get(name)
            for name in (
                "TMPDIR",
                "GIT_DIR",
                "GIT_CEILING_DIRECTORIES",
                "CODE_GAUNTLET_TASK_ROOTS",
                "CODE_GAUNTLET_TASKS_DIR",
            )
        },
    }


def test_temp_root_name_carries_a_glob_guard() -> None:
    temp_root = tempfile.gettempdir()
    assert glob.has_magic(os.path.basename(temp_root))
    assert glob.glob(temp_root) == []


@pytest.mark.parametrize(
    ("prior_roots", "prior_tasks_dir"),
    [
        pytest.param(None, None, id="both-absent"),
        pytest.param("/sentinel/prior/task-roots", None, id="roots-only"),
        pytest.param(None, "/sentinel/prior/tasks-dir", id="tasks-dir-only"),
        pytest.param(
            "/sentinel/prior/task-roots", "/sentinel/prior/tasks-dir", id="both-present"
        ),
    ],
)
def test_configure_then_unconfigure_restores_exact_prior_state(
    prior_roots: str | None,
    prior_tasks_dir: str | None,
    prior_hook_state: str,
) -> None:
    os.environ["TMPDIR"] = "/sentinel/prior/tmpdir"
    os.environ["GIT_DIR"] = "/sentinel/prior/git-dir"
    os.environ.pop("GIT_CEILING_DIRECTORIES", None)
    for name, value in (
        ("CODE_GAUNTLET_TASK_ROOTS", prior_roots),
        ("CODE_GAUNTLET_TASKS_DIR", prior_tasks_dir),
    ):
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value
    # mkdtemp needs a real directory even though TMPDIR is a literal sentinel.
    tempfile.tempdir = prior_hook_state
    before = _process_facts()
    root_conftest.pytest_configure(None)
    created_root = root_conftest._STATE.root
    assert created_root is not None
    assert os.path.isdir(created_root)
    assert os.environ["TMPDIR"] == created_root
    assert tempfile.tempdir == created_root
    assert os.environ["CODE_GAUNTLET_TASK_ROOTS"] == os.path.join(
        created_root, "task-roots"
    )
    assert os.path.isdir(os.environ["CODE_GAUNTLET_TASK_ROOTS"])
    assert "CODE_GAUNTLET_TASKS_DIR" not in os.environ
    os.environ["CODE_GAUNTLET_TASKS_DIR"] = "/sentinel/session/tasks-dir"
    root_conftest.pytest_unconfigure(None)
    assert _process_facts() == before
    assert not os.path.exists(created_root)

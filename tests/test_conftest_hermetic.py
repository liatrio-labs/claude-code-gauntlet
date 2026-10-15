"""The root hooks isolate task discovery and restore process-global state."""

from __future__ import annotations

import glob
import os
import sys
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import conftest as root_conftest  # noqa: E402


def test_temp_root_name_carries_a_glob_guard():
    temp_root = tempfile.gettempdir()
    assert glob.has_magic(os.path.basename(temp_root))
    assert glob.glob(temp_root) == []


@pytest.mark.parametrize("present", [False, True])
def test_configure_then_unconfigure_restores_exact_prior_state(
    present, monkeypatch, tmp_path
):
    prior_roots = "/sentinel/prior/task-roots" if present else None
    prior_tasks_dir = "/sentinel/prior/tasks-dir" if present else None
    prior_tempdir = str(tmp_path)
    monkeypatch.setattr(os, "environ", dict(os.environ))
    monkeypatch.setattr(tempfile, "tempdir", prior_tempdir)
    monkeypatch.setattr(root_conftest, "_STATE", root_conftest._SessionState())
    try:
        os.environ["TMPDIR"] = "/sentinel/prior/tmpdir"
        os.environ["GIT_DIR"] = "/sentinel/prior/git-dir"
        os.environ.pop("GIT_CEILING_DIRECTORIES", None)
        if prior_roots is None:
            os.environ.pop("CODE_GAUNTLET_TASK_ROOTS", None)
        else:
            os.environ["CODE_GAUNTLET_TASK_ROOTS"] = prior_roots
        if prior_tasks_dir is None:
            os.environ.pop("CODE_GAUNTLET_TASKS_DIR", None)
        else:
            os.environ["CODE_GAUNTLET_TASKS_DIR"] = prior_tasks_dir
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
        assert os.environ["TMPDIR"] == "/sentinel/prior/tmpdir"
        assert os.environ["GIT_DIR"] == "/sentinel/prior/git-dir"
        assert "GIT_CEILING_DIRECTORIES" not in os.environ
        assert os.environ.get("CODE_GAUNTLET_TASK_ROOTS") == prior_roots
        assert os.environ.get("CODE_GAUNTLET_TASKS_DIR") == prior_tasks_dir
        assert tempfile.tempdir == prior_tempdir
        assert not os.path.exists(created_root)
    finally:
        # Cleanup also runs when a configure assertion fails.
        root_conftest.pytest_unconfigure(None)

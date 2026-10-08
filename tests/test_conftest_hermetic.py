"""The root hooks isolate task discovery and restore process-global state."""

from __future__ import annotations

import copy
import glob
import os
import shutil
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


@pytest.mark.parametrize("prior_roots", [None, "/sentinel/prior/task-roots"])
def test_configure_then_unconfigure_restores_exact_prior_state(prior_roots):
    saved_environ = dict(os.environ)
    saved_tempdir = tempfile.tempdir
    saved_state = copy.deepcopy(root_conftest._STATE.__dict__)
    prior_tempdir = tempfile.mkdtemp(prefix="cg-prior-sentinel-")
    try:
        os.environ["TMPDIR"] = "/sentinel/prior/tmpdir"
        os.environ["GIT_DIR"] = "/sentinel/prior/git-dir"
        os.environ.pop("GIT_CEILING_DIRECTORIES", None)
        if prior_roots is None:
            os.environ.pop("CODE_GAUNTLET_TASK_ROOTS", None)
        else:
            os.environ["CODE_GAUNTLET_TASK_ROOTS"] = prior_roots
        # mkdtemp needs a real prior directory even though TMPDIR is a literal sentinel.
        tempfile.tempdir = prior_tempdir
        root_conftest._STATE.__dict__.clear()
        root_conftest._STATE.__dict__.update(root_conftest._SessionState().__dict__)
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

        root_conftest.pytest_unconfigure(None)
        assert os.environ["TMPDIR"] == "/sentinel/prior/tmpdir"
        assert os.environ["GIT_DIR"] == "/sentinel/prior/git-dir"
        assert "GIT_CEILING_DIRECTORIES" not in os.environ
        assert os.environ.get("CODE_GAUNTLET_TASK_ROOTS") == prior_roots
        assert tempfile.tempdir == prior_tempdir
        assert not os.path.exists(created_root)
    finally:
        # Calls to these hooks must not disturb the session running this test.
        os.environ.clear()
        os.environ.update(saved_environ)
        tempfile.tempdir = saved_tempdir
        root_conftest._STATE.__dict__.clear()
        root_conftest._STATE.__dict__.update(saved_state)
        shutil.rmtree(prior_tempdir, ignore_errors=True)

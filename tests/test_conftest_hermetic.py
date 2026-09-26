"""Direct coverage for the root conftest.py's pytest_configure/pytest_unconfigure
pair, specifically the teardown side (pytest_unconfigure) that no other test
exercises directly.

This drives the two hooks on an isolated snapshot of process-global state
(os.environ and tempfile.tempdir), asserting the exact restore behavior:
a variable that had a prior value is restored to that exact value, a
variable that was absent before stays absent, and tempfile.tempdir and the
private root directory are cleaned up. It restores the live session's own
conftest state (the module-level _STATE object) afterward so this test
cannot disturb the pytest session running it.
"""

from __future__ import annotations

import copy
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import conftest as root_conftest  # noqa: E402


class TestConftestHermeticTeardown(unittest.TestCase):
    def setUp(self) -> None:
        # Snapshot the real environment and tempfile.tempdir so any mutation
        # this test makes (directly, or via pytest_configure/pytest_unconfigure)
        # is undone regardless of test outcome.
        self._saved_environ = dict(os.environ)
        self._saved_tempdir = tempfile.tempdir
        self.addCleanup(self._restore_environ_and_tempdir)

        # Snapshot the live session's own conftest state, since pytest_configure
        # ran it for real when this session started; pytest_unconfigure must not
        # run against that live state during this test.
        self._saved_live_state = copy.deepcopy(root_conftest._STATE.__dict__)
        self.addCleanup(self._restore_live_state)

    def _restore_environ_and_tempdir(self) -> None:
        os.environ.clear()
        os.environ.update(self._saved_environ)
        tempfile.tempdir = self._saved_tempdir

    def _restore_live_state(self) -> None:
        root_conftest._STATE.__dict__.clear()
        root_conftest._STATE.__dict__.update(self._saved_live_state)

    def test_configure_then_unconfigure_restores_exact_prior_state(self) -> None:
        # Arrange: a variable with a prior value to be restored exactly, and a
        # variable absent before that pytest_configure sets, to confirm it is
        # popped back to absent rather than restored to "".
        os.environ["TMPDIR"] = "/sentinel/prior/tmpdir"
        os.environ["GIT_DIR"] = "/sentinel/prior/git-dir"
        os.environ.pop("GIT_CEILING_DIRECTORIES", None)

        # tempfile.mkdtemp (called inside pytest_configure) uses the current
        # tempfile.tempdir as its base directory, so the "prior" sentinel must
        # be a real, existing directory rather than a synthetic path.
        prior_tempdir = tempfile.mkdtemp(prefix="cg-prior-sentinel-")
        self.addCleanup(shutil.rmtree, prior_tempdir, ignore_errors=True)
        tempfile.tempdir = prior_tempdir

        # Use a fresh state object so this test's configure/unconfigure pass
        # never touches the live session's _STATE (restored by addCleanup
        # regardless, but keeping the fresh instance makes the intent explicit).
        fresh_state = root_conftest._SessionState()
        root_conftest._STATE.__dict__.clear()
        root_conftest._STATE.__dict__.update(fresh_state.__dict__)

        root_conftest.pytest_configure(None)

        created_root = root_conftest._STATE.root
        assert created_root is not None
        self.assertTrue(os.path.isdir(created_root))
        self.assertEqual(os.environ["TMPDIR"], created_root)
        self.assertEqual(tempfile.tempdir, created_root)

        root_conftest.pytest_unconfigure(None)

        with self.subTest("prior value restored exactly"):
            self.assertEqual(os.environ["TMPDIR"], "/sentinel/prior/tmpdir")
            self.assertEqual(os.environ["GIT_DIR"], "/sentinel/prior/git-dir")
        with self.subTest("previously absent variable stays absent"):
            self.assertNotIn("GIT_CEILING_DIRECTORIES", os.environ)
        with self.subTest("tempfile.tempdir restored"):
            self.assertEqual(tempfile.tempdir, prior_tempdir)
        with self.subTest("private root directory removed"):
            self.assertFalse(os.path.exists(created_root))


if __name__ == "__main__":
    unittest.main()

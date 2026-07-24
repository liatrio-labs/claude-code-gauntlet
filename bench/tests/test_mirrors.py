"""Tests for bench/runner/mirrors.py — bare mirror + worktree lifecycle.

No network anywhere: a local bare repo stands in for the GitHub remote, with a
`refs/pull/7/head` ref set via `git update-ref` to mimic a GitHub pull ref.
"""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

# Import the module under test via the intended package path. `python -m pytest`
# from the repo root puts the root on sys.path; make that explicit so the test
# imports the same way regardless of how pytest is invoked.
REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from bench.runner import mirrors  # noqa: E402
from bench.runner.mirrors import (  # noqa: E402
    DriftError,
    ensure_mirror,
    make_worktree,
    remove_worktree,
)

_GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "bench",
    "GIT_AUTHOR_EMAIL": "bench@example.invalid",
    "GIT_COMMITTER_NAME": "bench",
    "GIT_COMMITTER_EMAIL": "bench@example.invalid",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_SYSTEM": os.devnull,
}


class MirrorsTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="mirrors-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.src = self.tmp / "src"
        self.remote = self.tmp / "remote.git"
        self.mirrors_dir = self.tmp / "mirrors"
        self.base_sha, self.feat_sha = self._build_remote()

    # --- git helpers ------------------------------------------------------
    def _git(self, *args, cwd):
        return subprocess.run(
            ["git", *args], cwd=str(cwd), env=_GIT_ENV, capture_output=True, text=True
        )

    def _git_ok(self, *args, cwd):
        result = self._git(*args, cwd=cwd)
        if result.returncode != 0:
            raise RuntimeError(f"git {args} failed: {result.stderr}")
        return result.stdout

    def _worktree_count(self, mirror):
        out = self._git_ok("worktree", "list", cwd=mirror)
        return len([ln for ln in out.splitlines() if ln.strip()])

    # --- fixture ----------------------------------------------------------
    def _build_remote(self):
        """A bare 'remote' with `main` at BASE and `refs/pull/7/head` at FEAT.

        FEAT is a child of BASE that is never merged into main, so
        merge-base(FEAT, main) == BASE — the exact shape the drift guard checks.
        """
        self.src.mkdir(parents=True)
        self._git_ok("init", "-q", str(self.src), cwd=self.tmp)
        self._git_ok("symbolic-ref", "HEAD", "refs/heads/main", cwd=self.src)
        (self.src / "f.txt").write_text("base\n")
        self._git_ok("add", "-A", cwd=self.src)
        self._git_ok("commit", "-qm", "base", cwd=self.src)
        base_sha = self._git_ok("rev-parse", "HEAD", cwd=self.src).strip()

        self._git_ok("checkout", "-q", "-b", "feature", cwd=self.src)
        (self.src / "f.txt").write_text("base\nfeat\n")
        self._git_ok("add", "-A", cwd=self.src)
        self._git_ok("commit", "-qm", "feat", cwd=self.src)
        feat_sha = self._git_ok("rev-parse", "HEAD", cwd=self.src).strip()
        self._git_ok("checkout", "-q", "main", cwd=self.src)

        self._git_ok("init", "-q", "--bare", str(self.remote), cwd=self.tmp)
        self._git_ok("remote", "add", "origin", str(self.remote), cwd=self.src)
        self._git_ok("push", "-q", "origin", "main", cwd=self.src)
        self._git_ok("push", "-q", "origin", "feature", cwd=self.src)
        # Simulate a GitHub pull ref, then drop the branch so only main + the
        # pull ref remain (as a real GitHub mirror would present them).
        self._git_ok("update-ref", "refs/pull/7/head", feat_sha, cwd=self.remote)
        self._git_ok("branch", "-D", "feature", cwd=self.remote)
        return base_sha, feat_sha

    def _add_pull_ref(self, pr_number):
        """Add a brand-new commit + `refs/pull/{n}/head` to the remote."""
        branch = f"pr{pr_number}"
        self._git_ok("checkout", "-q", "-b", branch, "main", cwd=self.src)
        (self.src / f"file{pr_number}.txt").write_text("x\n")
        self._git_ok("add", "-A", cwd=self.src)
        self._git_ok("commit", "-qm", f"pr{pr_number}", cwd=self.src)
        sha = self._git_ok("rev-parse", "HEAD", cwd=self.src).strip()
        self._git_ok("push", "-q", "origin", branch, cwd=self.src)
        self._git_ok("update-ref", f"refs/pull/{pr_number}/head", sha, cwd=self.remote)
        self._git_ok("checkout", "-q", "main", cwd=self.src)
        return sha


class EnsureMirrorTest(MirrorsTestBase):
    def test_mirror_created_once_and_reused(self):
        mirror = ensure_mirror(str(self.remote), self.mirrors_dir)
        self.assertTrue(mirror.is_dir())
        # A marker inside the mirror must survive a second call: a re-clone
        # would wipe the directory.
        marker = mirror / "REUSE_MARKER"
        marker.write_text("kept")
        again = ensure_mirror(str(self.remote), self.mirrors_dir)
        self.assertEqual(again, mirror)  # deterministic dir name -> same path
        self.assertTrue(marker.exists())
        self.assertEqual("kept", marker.read_text())

    def test_empty_dir_is_torn_down_and_recloned(self):
        # Interrupted clone / poisoned cache: directory exists but is not a bare repo.
        dirname = mirrors._mirror_dirname(str(self.remote))
        poison = self.mirrors_dir / dirname
        poison.mkdir(parents=True)
        (poison / "junk").write_text("incomplete")
        mirror = ensure_mirror(str(self.remote), self.mirrors_dir)
        self.assertEqual(mirror, poison)
        self.assertFalse((poison / "junk").exists())
        self.assertTrue(mirrors._mirror_is_usable(mirror))
        refs = self._git_ok("show-ref", cwd=mirror)
        self.assertIn("refs/heads/main", refs)

    def test_bare_repo_with_no_refs_is_torn_down_and_recloned(self):
        # Partial mirror: bare init succeeded but fetch never landed refs.
        dirname = mirrors._mirror_dirname(str(self.remote))
        poison = self.mirrors_dir / dirname
        poison.mkdir(parents=True)
        self._git_ok("init", "-q", "--bare", str(poison), cwd=self.tmp)
        self.assertFalse(mirrors._mirror_is_usable(poison))
        mirror = ensure_mirror(str(self.remote), self.mirrors_dir)
        self.assertTrue(mirrors._mirror_is_usable(mirror))
        refs = self._git_ok("show-ref", cwd=mirror)
        self.assertIn("refs/heads/main", refs)

    def test_non_bare_git_dir_is_torn_down_and_recloned(self):
        dirname = mirrors._mirror_dirname(str(self.remote))
        poison = self.mirrors_dir / dirname
        poison.mkdir(parents=True)
        self._git_ok("init", "-q", str(poison), cwd=self.tmp)  # non-bare
        mirror = ensure_mirror(str(self.remote), self.mirrors_dir)
        self.assertTrue(mirrors._mirror_is_usable(mirror))
        bare = self._git_ok("rev-parse", "--is-bare-repository", cwd=mirror).strip()
        self.assertEqual(bare, "true")

    def test_no_remote_update_without_refresh(self):
        mirror = ensure_mirror(str(self.remote), self.mirrors_dir)
        self._add_pull_ref(pr_number=8)  # appears in remote only, after mirroring
        again = ensure_mirror(str(self.remote), self.mirrors_dir)
        self.assertEqual(again, mirror)
        # No network touched => the mirror cannot know about pull/8 yet.
        probe = self._git("rev-parse", "-q", "--verify", "refs/pull/8/head", cwd=mirror)
        self.assertNotEqual(0, probe.returncode)

    def test_refresh_fetches_new_refs(self):
        mirror = ensure_mirror(str(self.remote), self.mirrors_dir)
        new_sha = self._add_pull_ref(pr_number=8)
        ensure_mirror(str(self.remote), self.mirrors_dir, refresh=True)
        probe = self._git("rev-parse", "-q", "--verify", "refs/pull/8/head", cwd=mirror)
        self.assertEqual(0, probe.returncode)
        self.assertEqual(new_sha, probe.stdout.strip())

    def test_mirror_retains_clone_url(self):
        mirror = ensure_mirror(str(self.remote), self.mirrors_dir)
        url = self._git_ok("config", "remote.origin.url", cwd=mirror).strip()
        self.assertEqual(str(self.remote), url)


class MakeWorktreeTest(MirrorsTestBase):
    def test_stale_registered_worktree_is_self_healed(self):
        # An unclean stop leaves the worktree behind; a resume must re-add, not fail.
        mirror = ensure_mirror(str(self.remote), self.mirrors_dir)
        dest = self.tmp / "wt-stale-reg"
        make_worktree(mirror, self.feat_sha, self.base_sha, "main", dest, pr_number=7)
        # No remove_worktree: simulate the crash, then resume.
        result = make_worktree(
            mirror, self.feat_sha, self.base_sha, "main", dest, pr_number=7
        )
        self.assertEqual(result, dest)
        self.assertTrue((dest / ".git").exists())

    def test_stale_unregistered_dir_is_self_healed(self):
        mirror = ensure_mirror(str(self.remote), self.mirrors_dir)
        dest = self.tmp / "wt-stale-dir"
        dest.mkdir(parents=True)
        (dest / "leftover.txt").write_text("stale")
        result = make_worktree(
            mirror, self.feat_sha, self.base_sha, "main", dest, pr_number=7
        )
        self.assertEqual(result, dest)
        self.assertFalse((dest / "leftover.txt").exists())

    def test_worktree_detached_at_head(self):
        mirror = ensure_mirror(str(self.remote), self.mirrors_dir)
        dest = self.tmp / "wt"
        result = make_worktree(
            mirror, self.feat_sha, self.base_sha, "main", dest, pr_number=7
        )
        self.assertEqual(dest, result)
        self.assertTrue(dest.is_dir())
        head = self._git_ok("rev-parse", "HEAD", cwd=dest).strip()
        self.assertEqual(self.feat_sha, head)
        # Detached HEAD => symbolic-ref fails (no branch attached).
        symref = self._git("symbolic-ref", "-q", "HEAD", cwd=dest)
        self.assertNotEqual(0, symref.returncode)

    def test_drift_error_when_head_mismatch(self):
        mirror = ensure_mirror(str(self.remote), self.mirrors_dir)
        dest = self.tmp / "wt"
        # Pin the wrong head (base != the pull ref's FEAT).
        with self.assertRaises(DriftError) as ctx:
            make_worktree(
                mirror, self.base_sha, self.base_sha, "main", dest, pr_number=7
            )
        self.assertIn("7", str(ctx.exception))
        self.assertFalse(dest.exists())

    def test_drift_error_when_base_mismatch(self):
        mirror = ensure_mirror(str(self.remote), self.mirrors_dir)
        dest = self.tmp / "wt"
        # Correct head, but pin the wrong base (feat != merge-base == BASE).
        with self.assertRaises(DriftError) as ctx:
            make_worktree(
                mirror, self.feat_sha, self.feat_sha, "main", dest, pr_number=7
            )
        self.assertIn("7", str(ctx.exception))
        self.assertFalse(dest.exists())


class RemoveWorktreeTest(MirrorsTestBase):
    def test_remove_cleans_up_and_is_tolerant(self):
        mirror = ensure_mirror(str(self.remote), self.mirrors_dir)
        dest = self.tmp / "wt"
        make_worktree(mirror, self.feat_sha, self.base_sha, "main", dest, pr_number=7)
        before = self._worktree_count(mirror)

        remove_worktree(mirror, dest)
        after = self._worktree_count(mirror)
        self.assertLess(after, before)  # `git worktree list` shrank
        self.assertFalse(dest.exists())

        # Second call on an already-absent worktree must not raise.
        remove_worktree(mirror, dest)
        self.assertEqual(after, self._worktree_count(mirror))


if __name__ == "__main__":
    unittest.main()

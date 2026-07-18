"""Tests for bench/run.py -- the one-command runner CLI.

No network, no real claude: mirrors.ensure_mirror/make_worktree/remove_worktree and
invoke.invoke_review are replaced with in-process fakes, and the workspace globals are
repointed at a tempdir. The golden data (subsets.json / shas.json / benchmark_data.min.json)
is the real committed data -- tier resolution and fixture-PR selection are exercised against
it. Prereq failures are provoked by stripping PATH and pointing ENV_PATH at a missing file.
"""

import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from bench import run  # noqa: E402

FIXTURE_URL = "https://github.com/getsentry/sentry/pull/77754"  # in review_md_fixtures
PLAIN_URL = "https://github.com/keycloak/keycloak/pull/37634"  # gate PR, not a fixture


# ------------------------------------------------------------------------------- fakes


def fake_ensure_mirror(clone_url, mirrors_dir, refresh=False):
    mirror = Path(mirrors_dir) / "mirror.git"
    mirror.mkdir(parents=True, exist_ok=True)
    return mirror


def fake_make_worktree(mirror, head_sha, base_sha, base_ref, dest, pr_number):
    Path(dest).mkdir(parents=True, exist_ok=True)
    return Path(dest)


def drift_on(target_pr_number):
    def _make(mirror, head_sha, base_sha, base_ref, dest, pr_number):
        if pr_number == target_pr_number:
            raise run.mirrors.DriftError("PR #{}: simulated input drift".format(pr_number))
        Path(dest).mkdir(parents=True, exist_ok=True)
        return Path(dest)

    return _make


def fake_invoke_ok(worktree, pr, run_dir, timeout_s=1800):
    outdir = Path(run_dir) / "output"
    outdir.mkdir(parents=True, exist_ok=True)
    payload = outdir / "post-review-payload.json"
    payload.write_text(json.dumps({"payload": {"comments": []}, "skipped": []}))
    (outdir / "deep-review-report.md").write_text("# report\n")
    return run.invoke.InvokeResult("ok", cost_usd=0.42, payload_path=str(payload))


def fake_invoke_boom(*args, **kwargs):
    raise AssertionError("invoke_review must not run for an already-completed PR")


def make_invoke_first_fails():
    """An invoke fake that fails the first PR and oks the rest."""
    state = {"n": 0}

    def _inv(worktree, pr, run_dir, timeout_s=1800):
        state["n"] += 1
        if state["n"] == 1:
            return run.invoke.InvokeResult("failed", reason="boom")
        return fake_invoke_ok(worktree, pr, run_dir, timeout_s)

    return _inv


def make_invoke_raises_first(exc):
    """An invoke fake that raises *exc* on the first PR and oks the rest."""
    state = {"n": 0}

    def _inv(worktree, pr, run_dir, timeout_s=1800):
        state["n"] += 1
        if state["n"] == 1:
            raise exc
        return fake_invoke_ok(worktree, pr, run_dir, timeout_s)

    return _inv


# -------------------------------------------------------------------------------- base


class RunTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="bench-run-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.runs_root = self.tmp / "runs"
        self.mirrors_dir = self.tmp / "mirrors"
        self._patch_global("RUNS_ROOT", self.runs_root)
        self._patch_global("MIRRORS_DIR", self.mirrors_dir)
        self._patch_global("WORKSPACE", self.tmp)

        self.shas = run._load_json(run.GOLDEN_DIR / "shas.json")
        self.subsets = run._load_json(run.GOLDEN_DIR / "subsets.json")
        self.bench_data = run._load_json(run.GOLDEN_DIR / "benchmark_data.min.json")

    def _patch_global(self, name, value):
        patcher = patch.object(run, name, value)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _install_runner_fakes(self, invoke_fn=fake_invoke_ok, make_worktree_fn=None,
                              remove_fn=None, ensure_fn=None):
        pairs = [
            (run.mirrors, "ensure_mirror", ensure_fn or fake_ensure_mirror),
            (run.mirrors, "make_worktree", make_worktree_fn or fake_make_worktree),
            (run.mirrors, "remove_worktree", remove_fn or (lambda mirror, dest: None)),
            (run.invoke, "invoke_review", invoke_fn),
        ]
        for target, name, fn in pairs:
            patcher = patch.object(target, name, fn)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _detail(self, cp, url):
        """Return the persisted checkpoint detail dict for *url*."""
        return json.loads(Path(cp._path(url)).read_text())["detail"]


# ----------------------------------------------------------------------------- prereqs


class PrereqTest(RunTestBase):
    @contextlib.contextmanager
    def _stripped_path(self, env_file):
        empty_bin = self.tmp / "empty-bin"
        empty_bin.mkdir(exist_ok=True)
        self._patch_global("ENV_PATH", env_file)
        with patch.dict(os.environ, {"PATH": str(empty_bin)}):
            yield

    def test_all_missing_prereqs_listed_and_exit_2(self):
        missing_env = self.tmp / "nope.env"  # does not exist
        stderr = io.StringIO()
        with self._stripped_path(missing_env), contextlib.redirect_stderr(stderr):
            rc = run.main(["--tier", "smoke"])
        self.assertEqual(rc, 2)
        text = stderr.getvalue()
        self.assertIn("claude CLI not found", text)
        self.assertIn("gh CLI not found", text)
        self.assertIn("ANTHROPIC_API_KEY missing", text)
        self.assertIn("uv not found", text)
        # One actionable bullet per failure (claude, gh, env, uv -- disk has headroom).
        bullets = [ln for ln in text.splitlines() if ln.startswith("  - ")]
        self.assertEqual(len(bullets), 4)

    def test_present_env_key_removes_that_failure(self):
        env_file = self.tmp / "bench.env"
        env_file.write_text("ANTHROPIC_API_KEY=sk-test-123\n")
        failures = None
        empty_bin = self.tmp / "empty-bin2"
        empty_bin.mkdir()
        with patch.dict(os.environ, {"PATH": str(empty_bin)}):
            failures = run.check_prereqs(env_path=env_file, workspace_dir=self.tmp)
        joined = "\n".join(failures)
        self.assertNotIn("ANTHROPIC_API_KEY", joined)  # key present -> not a failure
        self.assertIn("claude CLI not found", joined)
        self.assertIn("uv not found", joined)

    def test_manifest_invocation_labels_naive_and_skill(self):
        import types

        for anchor, expected in (
            ("naive", "naive:single-pass max-turns={}".format(run.NAIVE_MAX_TURNS)),
            (None, "headless:/deep-review"),
        ):
            run_dir = self.tmp / "manifest-{}".format(anchor or "skill")
            run_dir.mkdir(parents=True)
            args = types.SimpleNamespace(anchor=anchor, fidelity="dry-run")
            run._write_manifest(run_dir, "rid", "smoke", [], 60, args)
            manifest = json.loads((run_dir / "run.json").read_text())
            self.assertEqual(manifest["invocation"], expected)

    def test_quoted_empty_key_is_a_failure(self):
        # A quoted-empty value must fail prereqs the same way build_env would treat
        # it: _read_env_key delegates to invoke._load_dotenv_key, so the two parsers
        # can never disagree on this again.
        env_file = self.tmp / "bench.env"
        env_file.write_text('ANTHROPIC_API_KEY=""\n')
        empty_bin = self.tmp / "empty-bin3"
        empty_bin.mkdir()
        with patch.dict(os.environ, {"PATH": str(empty_bin)}):
            failures = run.check_prereqs(env_path=env_file, workspace_dir=self.tmp)
        self.assertTrue(any("ANTHROPIC_API_KEY missing" in f for f in failures))

    def test_low_disk_is_a_failure(self):
        env_file = self.tmp / "bench.env"
        env_file.write_text("ANTHROPIC_API_KEY=sk-test\n")
        # An absurd threshold forces the disk check to fail regardless of the partition.
        failures = run.check_prereqs(
            env_path=env_file, workspace_dir=self.tmp, min_free_gb=10 ** 9
        )
        self.assertTrue(any("free on the workspace partition" in f for f in failures))


# ---------------------------------------------------------------------- tier resolution


class TierResolutionTest(RunTestBase):
    def test_counts(self):
        self.assertEqual(len(run._resolve_tier("smoke", self.subsets, self.shas)), 3)
        self.assertEqual(len(run._resolve_tier("subset", self.subsets, self.shas)), 15)
        self.assertEqual(len(run._resolve_tier("full", self.subsets, self.shas)), 50)

    def test_subset_maps_to_gate(self):
        self.assertEqual(
            run._resolve_tier("subset", self.subsets, self.shas), self.subsets["gate"]
        )

    def test_full_is_all_sha_keys(self):
        self.assertEqual(
            run._resolve_tier("full", self.subsets, self.shas), list(self.shas.keys())
        )


# ------------------------------------------------------------------------ fixture write


class FixtureWriteTest(RunTestBase):
    def test_fixture_pr_gets_review_md_others_dont(self):
        self._install_runner_fakes()  # remove_worktree is a no-op -> worktree persists
        run_dir = self.runs_root / "fixture-run"
        run_dir.mkdir(parents=True)
        cp = run.checkpoint.Checkpoint(run_dir)
        urls = [FIXTURE_URL, PLAIN_URL]
        run._run_prs(
            run_dir, urls, cp, self.shas, {FIXTURE_URL}, 60, None, self.bench_data
        )

        fixture_key = run.invoke.pr_dir_name({"url": FIXTURE_URL, **self.shas[FIXTURE_URL]})
        plain_key = run.invoke.pr_dir_name({"url": PLAIN_URL, **self.shas[PLAIN_URL]})
        fixture_review = run_dir / fixture_key / "worktree" / "REVIEW.md"
        plain_review = run_dir / plain_key / "worktree" / "REVIEW.md"

        self.assertTrue(fixture_review.exists())
        self.assertEqual(fixture_review.read_text(), run.FIXTURE_PATH.read_text())
        self.assertFalse(plain_review.exists())


# --------------------------------------------------------------------- artifact capture


class ArtifactCaptureTest(RunTestBase):
    def test_artifacts_moved_and_output_left_empty(self):
        self._install_runner_fakes()
        run_dir = self.runs_root / "collect-run"
        run_dir.mkdir(parents=True)
        cp = run.checkpoint.Checkpoint(run_dir)
        run._run_prs(run_dir, [PLAIN_URL], cp, self.shas, set(), 60, None, self.bench_data)

        pr_dir = run_dir / run.invoke.pr_dir_name({"url": PLAIN_URL, **self.shas[PLAIN_URL]})
        self.assertTrue((pr_dir / "post-review-payload.json").is_file())
        self.assertTrue((pr_dir / "deep-review-report.md").is_file())
        self.assertTrue((pr_dir / "diff.patch").exists())
        # The shared output dir must be left empty for the next PR.
        output_dir = run_dir / "output"
        self.assertTrue(output_dir.is_dir())
        self.assertEqual(list(output_dir.iterdir()), [])
        self.assertEqual(cp.status(PLAIN_URL), "ok")


# ---------------------------------------------------------------- checkpoint payload path


class CheckpointPayloadPathTest(RunTestBase):
    def test_detail_payload_path_points_at_moved_pr_dir_file(self):
        # invoke returns payload_path in the shared output dir; _collect_artifacts then
        # moves it into pr-dir. The persisted checkpoint detail must record the pr-dir
        # location (the file that still exists), not the stale pre-move path.
        self._install_runner_fakes(invoke_fn=fake_invoke_ok)
        run_dir = self.runs_root / "payload-path-run"
        run_dir.mkdir(parents=True)
        cp = run.checkpoint.Checkpoint(run_dir)
        run._run_prs(run_dir, [PLAIN_URL], cp, self.shas, set(), 60, None, self.bench_data)

        pr_key = run.invoke.pr_dir_name({"url": PLAIN_URL, **self.shas[PLAIN_URL]})
        expected = run_dir / pr_key / "post-review-payload.json"
        detail = self._detail(cp, PLAIN_URL)
        self.assertEqual(detail["payload_path"], str(expected))
        self.assertTrue(Path(detail["payload_path"]).is_file())
        # The stale shared-output location must not be what was recorded.
        self.assertNotEqual(
            detail["payload_path"], str(run_dir / "output" / "post-review-payload.json")
        )


# --------------------------------------------------------------------------- drift guard


class DriftTest(RunTestBase):
    def test_drift_marks_drifted_and_run_continues(self):
        gate = self.subsets["gate"]
        urls = gate[:3]
        target_pr = self.shas[urls[0]]["pr_number"]
        self._install_runner_fakes(make_worktree_fn=drift_on(target_pr))

        run_dir = self.runs_root / "drift-run"
        run_dir.mkdir(parents=True)
        cp = run.checkpoint.Checkpoint(run_dir)
        summary = run._run_prs(
            run_dir, urls, cp, self.shas, set(), 60, None, self.bench_data
        )

        self.assertEqual(cp.status(urls[0]), "drifted")
        # The run did not abort: the remaining PRs were still processed to completion.
        self.assertEqual(cp.status(urls[1]), "ok")
        self.assertEqual(cp.status(urls[2]), "ok")
        self.assertEqual(summary["counts"].get("drifted"), 1)
        self.assertEqual(len(summary["drifted"]), 1)
        self.assertEqual(summary["drifted"][0][0], urls[0])


# ---------------------------------------------------------------------- loop hardening


class LoopHardeningTest(RunTestBase):
    def test_missing_sentinel_head_sha_failed_without_mirror(self):
        url = "https://github.com/o/r/pull/1"
        meta = {"owner": "o", "repo": "r", "head_sha": "missing",
                "base_sha": "b", "base_ref": "main", "pr_number": 1}
        called = []

        def boom(clone_url, mirrors_dir, refresh=False):
            called.append(clone_url)
            raise AssertionError("ensure_mirror must not run for an incomplete entry")

        self._install_runner_fakes(ensure_fn=boom)
        run_dir = self.runs_root / "run-missing-sha"
        run_dir.mkdir(parents=True)
        cp = run.checkpoint.Checkpoint(run_dir)
        summary = run._run_prs(run_dir, [url], cp, {url: meta}, set(), 60, None, {})

        self.assertEqual(cp.status(url), "failed")
        self.assertEqual(self._detail(cp, url)["reason"], "incomplete_sha_entry")
        self.assertEqual(summary["counts"].get("failed"), 1)
        self.assertEqual(called, [])

    def test_missing_owner_failed_without_mirror(self):
        url = "https://github.com/o/r/pull/2"
        meta = {"repo": "r", "head_sha": "h", "base_sha": "b",
                "base_ref": "main", "pr_number": 2}  # no owner

        def boom(clone_url, mirrors_dir, refresh=False):
            raise AssertionError("ensure_mirror must not run for an incomplete entry")

        self._install_runner_fakes(ensure_fn=boom)
        run_dir = self.runs_root / "run-missing-owner"
        run_dir.mkdir(parents=True)
        cp = run.checkpoint.Checkpoint(run_dir)
        summary = run._run_prs(run_dir, [url], cp, {url: meta}, set(), 60, None, {})

        self.assertEqual(cp.status(url), "failed")
        self.assertEqual(self._detail(cp, url)["reason"], "incomplete_sha_entry")
        self.assertEqual(summary["counts"].get("failed"), 1)

    def test_mirror_error_marks_failed_and_run_continues(self):
        urls = ["https://github.com/o/r/pull/10", "https://github.com/o/r/pull/11"]
        metas = {
            urls[0]: {"owner": "o", "repo": "r", "head_sha": "h0", "base_sha": "b0",
                      "base_ref": "main", "pr_number": 10},
            urls[1]: {"owner": "o", "repo": "r", "head_sha": "h1", "base_sha": "b1",
                      "base_ref": "main", "pr_number": 11},
        }
        state = {"n": 0}

        def ensure(clone_url, mirrors_dir, refresh=False):
            state["n"] += 1
            if state["n"] == 1:
                raise run.subprocess.CalledProcessError(128, ["git", "clone", "--mirror"])
            return fake_ensure_mirror(clone_url, mirrors_dir, refresh)

        self._install_runner_fakes(ensure_fn=ensure)
        run_dir = self.runs_root / "run-mirror-error"
        run_dir.mkdir(parents=True)
        cp = run.checkpoint.Checkpoint(run_dir)
        summary = run._run_prs(run_dir, urls, cp, metas, set(), 60, None, {})

        self.assertEqual(cp.status(urls[0]), "failed")
        self.assertEqual(self._detail(cp, urls[0])["reason"], "mirror_error")
        # The run did not abort: the next PR still ran to completion.
        self.assertEqual(cp.status(urls[1]), "ok")
        self.assertEqual(summary["counts"].get("failed"), 1)
        self.assertEqual(summary["counts"].get("ok"), 1)

    def test_oserror_during_mirror_block_marks_failed_and_continues(self):
        urls = ["https://github.com/o/r/pull/20", "https://github.com/o/r/pull/21"]
        metas = {
            urls[0]: {"owner": "o", "repo": "r", "head_sha": "h0", "base_sha": "b0",
                      "base_ref": "main", "pr_number": 20},
            urls[1]: {"owner": "o", "repo": "r", "head_sha": "h1", "base_sha": "b1",
                      "base_ref": "main", "pr_number": 21},
        }
        state = {"n": 0}

        def worktree_oserror(mirror, head_sha, base_sha, base_ref, dest, pr_number):
            state["n"] += 1
            if state["n"] == 1:
                raise OSError("rmtree failed on stale worktree")
            return fake_make_worktree(mirror, head_sha, base_sha, base_ref, dest, pr_number)

        self._install_runner_fakes(make_worktree_fn=worktree_oserror)
        run_dir = self.runs_root / "run-oserror"
        run_dir.mkdir(parents=True)
        cp = run.checkpoint.Checkpoint(run_dir)
        summary = run._run_prs(run_dir, urls, cp, metas, set(), 60, None, {})

        self.assertEqual(cp.status(urls[0]), "failed")
        detail = self._detail(cp, urls[0])
        self.assertEqual(detail["reason"], "mirror_error")
        self.assertIn("OSError", detail["error"])
        self.assertEqual(cp.status(urls[1]), "ok")

    def test_cleanup_failure_in_finally_does_not_abort(self):
        urls = ["https://github.com/o/r/pull/30", "https://github.com/o/r/pull/31"]
        metas = {
            urls[0]: {"owner": "o", "repo": "r", "head_sha": "h0", "base_sha": "b0",
                      "base_ref": "main", "pr_number": 30},
            urls[1]: {"owner": "o", "repo": "r", "head_sha": "h1", "base_sha": "b1",
                      "base_ref": "main", "pr_number": 31},
        }
        state = {"n": 0}

        def remove_boom(mirror, dest):
            state["n"] += 1
            if state["n"] == 1:
                raise run.subprocess.CalledProcessError(1, ["git", "worktree", "remove"])

        self._install_runner_fakes(remove_fn=remove_boom)
        run_dir = self.runs_root / "run-cleanup-fail"
        run_dir.mkdir(parents=True)
        cp = run.checkpoint.Checkpoint(run_dir)
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            summary = run._run_prs(run_dir, urls, cp, metas, set(), 60, None, {})

        # The failing cleanup neither aborted the tier nor changed the PR outcome.
        self.assertEqual(cp.status(urls[0]), "ok")
        self.assertEqual(cp.status(urls[1]), "ok")
        self.assertEqual(summary["counts"].get("ok"), 2)
        self.assertIn("worktree cleanup failed", stderr.getvalue())


# --------------------------------------------------------------------- unexpected error


class UnexpectedErrorTest(RunTestBase):
    """An unexpected per-PR exception fails only that PR and the tier continues."""

    def test_unexpected_exception_fails_pr_and_run_continues(self):
        gate = self.subsets["gate"]
        urls = gate[:2]
        removed = []
        self._install_runner_fakes(
            invoke_fn=make_invoke_raises_first(ValueError("kaboom")),
            remove_fn=lambda mirror, dest: removed.append(dest),
        )

        run_dir = self.runs_root / "unexpected-run"
        run_dir.mkdir(parents=True)
        cp = run.checkpoint.Checkpoint(run_dir)
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            summary = run._run_prs(
                run_dir, urls, cp, self.shas, set(), 60, None, self.bench_data
            )

        # The failed PR is marked with the unexpected_error reason + a full traceback.
        self.assertEqual(cp.status(urls[0]), "failed")
        detail = self._detail(cp, urls[0])
        self.assertTrue(detail["reason"].startswith("unexpected_error:ValueError:"))
        self.assertIn("kaboom", detail["reason"])
        self.assertIn("ValueError", detail["traceback"])
        self.assertIn("kaboom", detail["traceback"])
        # A single loud stderr breadcrumb names the PR.
        self.assertIn(urls[0], stderr.getvalue())

        # The run did not abort: the next PR still ran to completion.
        self.assertEqual(cp.status(urls[1]), "ok")
        self.assertEqual(summary["counts"].get("failed"), 1)
        self.assertEqual(summary["counts"].get("ok"), 1)
        # finally-removal still fired for both the failed and the ok PR.
        self.assertEqual(len(removed), 2)

        # A failed PR keeps the run's exit code at 1.
        final = {}
        for u in urls:
            final[cp.status(u)] = final.get(cp.status(u), 0) + 1
        self.assertEqual(run._exit_code(final, urls), 1)

    def test_keyboardinterrupt_is_not_swallowed(self):
        gate = self.subsets["gate"]
        urls = gate[:2]
        removed = []
        self._install_runner_fakes(
            invoke_fn=make_invoke_raises_first(KeyboardInterrupt()),
            remove_fn=lambda mirror, dest: removed.append(dest),
        )
        run_dir = self.runs_root / "kbint-run"
        run_dir.mkdir(parents=True)
        cp = run.checkpoint.Checkpoint(run_dir)
        with self.assertRaises(KeyboardInterrupt):
            run._run_prs(run_dir, urls, cp, self.shas, set(), 60, None, self.bench_data)
        # The worktree of the interrupted PR was still cleaned up (finally ran).
        self.assertEqual(len(removed), 1)


# ------------------------------------------------------------------------- exit codes


class ExitCodeTest(RunTestBase):
    def test_new_run_all_ok_exits_0(self):
        self._install_runner_fakes(invoke_fn=fake_invoke_ok)
        with patch.object(run, "check_prereqs", lambda *a, **k: []):
            rc = run.main(["--tier", "smoke"])
        self.assertEqual(rc, 0)

    def test_new_run_one_failed_exits_1(self):
        self._install_runner_fakes(invoke_fn=make_invoke_first_fails())
        with patch.object(run, "check_prereqs", lambda *a, **k: []):
            rc = run.main(["--tier", "smoke"])
        self.assertEqual(rc, 1)

    def test_resume_with_terminal_failure_exits_1(self):
        # A run where the first PR failed. Plain resume treats failed as terminal,
        # so nothing re-runs and the still-failed PR keeps the exit code at 1.
        self._install_runner_fakes(invoke_fn=make_invoke_first_fails())
        args = run.parse_args(["--tier", "smoke"])
        run._new_run(args)
        run_dir = next(p for p in self.runs_root.iterdir() if p.is_dir())
        run_id = run_dir.name
        with patch.object(run.invoke, "invoke_review", fake_invoke_boom):
            rc = run._resume(run_id, args, retry=False)
        self.assertEqual(rc, 1)


# ------------------------------------------------------------------------- runs / resume


class MultiRunTest(RunTestBase):
    def test_runs_three_creates_three_distinct_dirs(self):
        self._install_runner_fakes()
        with patch.object(run, "check_prereqs", lambda *a, **k: []):
            rc = run.main(["--tier", "smoke", "--runs", "3"])
        self.assertEqual(rc, 0)
        run_dirs = sorted(p for p in self.runs_root.iterdir() if p.is_dir())
        self.assertEqual(len(run_dirs), 3)
        for run_dir in run_dirs:
            self.assertTrue((run_dir / "run.json").is_file())

    def test_manifest_records_env_fingerprint(self):
        self._install_runner_fakes()
        args = run.parse_args(["--tier", "smoke"])
        run._new_run(args)
        run_dir = next(p for p in self.runs_root.iterdir() if p.is_dir())
        manifest = json.loads((run_dir / "run.json").read_text())
        self.assertEqual(manifest["tier"], "smoke")
        fingerprint = manifest["env_fingerprint"]
        self.assertEqual(fingerprint["DEEP_REVIEW_HEADLESS"], "1")
        self.assertEqual(fingerprint["DEEP_REVIEW_MODEL_TIER"], "optimized")
        self.assertEqual(fingerprint["timeout_s"], 45 * 60)


class ResumeTest(RunTestBase):
    def test_resume_skips_completed_prs(self):
        # First pass: everything succeeds.
        self._install_runner_fakes(invoke_fn=fake_invoke_ok)
        args = run.parse_args(["--tier", "smoke"])
        run._new_run(args)
        run_dir = next(p for p in self.runs_root.iterdir() if p.is_dir())
        run_id = run_dir.name

        # Resume with an invoke that explodes if reached: all PRs are ok, so pending() is
        # empty and nothing is re-invoked.
        with patch.object(run.invoke, "invoke_review", fake_invoke_boom):
            rc = run._resume(run_id, args, retry=False)
        self.assertEqual(rc, 0)
        cp = run.checkpoint.Checkpoint(run_dir)
        for url in run._resolve_tier("smoke", self.subsets, self.shas):
            self.assertEqual(cp.status(url), "ok")


# ----------------------------------------------------------------------------- cli guard


class CliGuardTest(RunTestBase):
    def test_score_only_missing_run_dir_errors_cleanly(self):
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            rc = run.main(["--score-only", "smoke-20260101-000000-abc1234"])
        self.assertEqual(rc, 2)
        self.assertIn("No run dir", stderr.getvalue())

    def test_score_only_invokes_score_run(self):
        run_id = "smoke-20260101-000000-abc1234"
        (run.RUNS_ROOT / run_id).mkdir(parents=True, exist_ok=True)
        with patch("bench.runner.score.score_run", return_value={"ok": True}) as scored:
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                rc = run.main(["--score-only", run_id])
        self.assertEqual(rc, 0)
        scored.assert_called_once_with(str(run.RUNS_ROOT / run_id))
        self.assertIn("Scored", stdout.getvalue())

    def test_score_only_refusal_is_actionable(self):
        run_id = "smoke-20260101-000000-abc1235"
        (run.RUNS_ROOT / run_id).mkdir(parents=True, exist_ok=True)
        with patch(
            "bench.runner.score.score_run",
            side_effect=RuntimeError("judge_pin is null"),
        ):
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                rc = run.main(["--score-only", run_id])
        self.assertEqual(rc, 2)
        self.assertIn("judge_pin is null", stderr.getvalue())

    def test_mutually_exclusive_modes_rejected(self):
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            rc = run.main(["--resume", "a", "--retry-failed", "b"])
        self.assertEqual(rc, 2)

    def test_resume_missing_run_dir_errors(self):
        with patch.object(run, "check_prereqs", lambda *a, **k: []):
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                rc = run.main(["--resume", "does-not-exist"])
        self.assertEqual(rc, 2)
        self.assertIn("nothing to resume", stderr.getvalue())


# --------------------------------------------------------------- naive anchor output


NAIVE_FAKE_CLAUDE = '''#!/usr/bin/env python3
import os, sys, json
try:
    sys.stdin.read()
except Exception:
    pass
result = os.environ.get("FAKE_NAIVE_RESULT", "")
envelope = {
    "type": "result", "subtype": "success", "is_error": False,
    "result": result, "total_cost_usd": 0.33,
    "modelUsage": {"claude-opus-4-8": {"inputTokens": 10, "outputTokens": 5, "costUSD": 0.33}},
    "usage": {"input_tokens": 10, "output_tokens": 5},
    "permission_denials": [],
}
sys.stdout.write(json.dumps(envelope) + "\\n")
'''

NAIVE_PR = {
    "url": "https://github.com/octo/widget/pull/7",
    "owner": "octo",
    "repo": "widget",
    "pr_number": 7,
    "head_sha": "a" * 40,
    "base_sha": "b" * 40,
    "base_ref": "main",
}


class NaivePayloadParseTest(RunTestBase):
    def test_fenced_block_written_verbatim_in_github_shape(self):
        pr_dir = self.tmp / "pr-7"
        pr_dir.mkdir()
        text = (
            "Here are my findings.\n```json\n"
            '{"comments": [{"path": "a.py", "line": 3, "body": "Null deref [HIGH]"}]}\n```\n'
        )
        dest = run._naive_payload_from_result(text, pr_dir)
        self.assertIsNotNone(dest)
        payload = json.loads(Path(dest).read_text())
        self.assertEqual(payload["platform"], "github")
        self.assertIsNone(payload["endpoint"])
        self.assertIsNone(payload["method"])
        self.assertEqual(payload["payload"]["event"], "COMMENT")
        self.assertEqual(
            payload["payload"]["comments"],
            [{"path": "a.py", "line": 3, "body": "Null deref [HIGH]"}],
        )
        self.assertEqual(payload["skipped"], [])

    def test_body_with_triple_backticks_parses_fully(self):
        # A comment body that itself contains ``` must not truncate the contract block:
        # the last fence runs to end-of-text, so backticks inside the JSON string reach
        # json.loads intact and the body is preserved verbatim.
        pr_dir = self.tmp / "pr-7"
        pr_dir.mkdir()
        body = "Use ```python\nx = 1\n``` instead of the raw call"
        text = (
            "Here are my findings.\n```json\n"
            + json.dumps({"comments": [{"path": "a.py", "line": 3, "body": body}]})
            + "\n```\n"
        )
        dest = run._naive_payload_from_result(text, pr_dir)
        self.assertIsNotNone(dest)
        payload = json.loads(Path(dest).read_text())
        self.assertEqual(payload["payload"]["comments"], [{"path": "a.py", "line": 3, "body": body}])

    def test_last_block_with_comments_wins(self):
        pr_dir = self.tmp / "pr-7"
        pr_dir.mkdir()
        text = (
            '```json\n{"comments": [{"path": "x", "line": 1, "body": "first"}]}\n```\n'
            'more prose\n```json\n{"comments": [{"path": "y", "line": 2, "body": "second"}]}\n```'
        )
        dest = run._naive_payload_from_result(text, pr_dir)
        payload = json.loads(Path(dest).read_text())
        self.assertEqual([c["body"] for c in payload["payload"]["comments"]], ["second"])

    def test_empty_comments_list_is_written(self):
        pr_dir = self.tmp / "pr-7"
        pr_dir.mkdir()
        text = "No issues found.\n```json\n{\"comments\": []}\n```"
        dest = run._naive_payload_from_result(text, pr_dir)
        self.assertIsNotNone(dest)
        payload = json.loads(Path(dest).read_text())
        self.assertEqual(payload["payload"]["comments"], [])

    def test_no_parseable_block_returns_none(self):
        pr_dir = self.tmp / "pr-7"
        pr_dir.mkdir()
        self.assertIsNone(run._naive_payload_from_result("just prose, no json block", pr_dir))
        self.assertFalse((pr_dir / "post-review-payload.json").exists())

    def test_prompt_appends_output_contract(self):
        prompt = run._naive_prompt(NAIVE_PR, "some diff", {"pr_title": "T"})
        self.assertIn(run._NAIVE_OUTPUT_CONTRACT, prompt)
        self.assertIn("```json", prompt)


class NaiveInvokeTest(RunTestBase):
    def _install_naive_fake(self, result_text):
        bindir = self.tmp / "naive-bin"
        bindir.mkdir(exist_ok=True)
        claude = bindir / "claude"
        claude.write_text(NAIVE_FAKE_CLAUDE)
        claude.chmod(0o755)
        return {
            "PATH": str(bindir) + os.pathsep + os.environ.get("PATH", ""),
            "FAKE_NAIVE_RESULT": result_text,
        }

    def _run_naive(self, result_text):
        run_dir = self.runs_root / "naive-run"
        run_dir.mkdir(parents=True, exist_ok=True)
        worktree = self.tmp / "wt"
        worktree.mkdir(exist_ok=True)
        with patch.dict(os.environ, self._install_naive_fake(result_text)):
            result = run._invoke_naive(
                worktree, NAIVE_PR, run_dir, "diff text", {"pr_title": "T"}, 30
            )
        return result

    def test_ok_with_parseable_block_sets_payload(self):
        text = (
            "Findings:\n```json\n"
            '{"comments": [{"path": "a.py", "line": 3, "body": "Bug"}]}\n```'
        )
        result = self._run_naive(text)
        self.assertEqual(result.status, "ok")
        self.assertIsNotNone(result.payload_path)
        payload = json.loads(Path(result.payload_path).read_text())
        self.assertEqual(payload["payload"]["comments"][0]["body"], "Bug")

    def test_unparseable_output_is_failed_and_retryable(self):
        result = self._run_naive("no structured output here at all")
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.reason, "naive_output_unparseable")

    def test_empty_comments_is_ok_with_empty_payload(self):
        result = self._run_naive("```json\n{\"comments\": []}\n```")
        self.assertEqual(result.status, "ok")
        payload = json.loads(Path(result.payload_path).read_text())
        self.assertEqual(payload["payload"]["comments"], [])

    def test_delegates_to_canonical_envelope_parser(self):
        # run.py must not re-implement the tolerant envelope parser; it delegates to the
        # shared invoke.parse_result_envelope. Spy-wrap the canonical parser and confirm
        # the real _invoke_naive path routes through it.
        real = run.invoke.parse_result_envelope
        calls = []

        def spy(text):
            calls.append(text)
            return real(text)

        with patch.object(run.invoke, "parse_result_envelope", spy):
            result = self._run_naive("```json\n{\"comments\": []}\n```")
        self.assertEqual(result.status, "ok")
        self.assertTrue(calls)


# --------------------------------------------------------------- naive failure reason


# A parametrizable naive fake: FAKE_NAIVE_IS_ERROR / _SUBTYPE / _EXIT / _RESULT let a
# test drive the exact failure envelope + exit code the reason logic must classify.
NAIVE_FAKE_CLAUDE_PARAM = '''#!/usr/bin/env python3
import os, sys, json
try:
    sys.stdin.read()
except Exception:
    pass
envelope = {
    "type": "result",
    "subtype": os.environ.get("FAKE_NAIVE_SUBTYPE", "success"),
    "is_error": os.environ.get("FAKE_NAIVE_IS_ERROR") == "1",
    "result": os.environ.get("FAKE_NAIVE_RESULT", ""),
    "total_cost_usd": 0.33,
    "modelUsage": {"claude-opus-4-8": {"inputTokens": 10, "outputTokens": 5, "costUSD": 0.33}},
    "usage": {"input_tokens": 10, "output_tokens": 5},
    "permission_denials": [],
}
sys.stdout.write(json.dumps(envelope) + "\\n")
sys.exit(int(os.environ.get("FAKE_NAIVE_EXIT", "0")))
'''


class NaiveFailureReasonTest(RunTestBase):
    def _run_param(self, **fake_env):
        bindir = self.tmp / "naive-param-bin"
        bindir.mkdir(exist_ok=True)
        claude = bindir / "claude"
        claude.write_text(NAIVE_FAKE_CLAUDE_PARAM)
        claude.chmod(0o755)
        overrides = {"PATH": str(bindir) + os.pathsep + os.environ.get("PATH", "")}
        overrides.update({k: str(v) for k, v in fake_env.items()})
        run_dir = self.runs_root / "naive-reason-run"
        run_dir.mkdir(parents=True, exist_ok=True)
        worktree = self.tmp / "wt-reason"
        worktree.mkdir(exist_ok=True)
        with patch.dict(os.environ, overrides):
            return run._invoke_naive(
                worktree, NAIVE_PR, run_dir, "diff text", {"pr_title": "T"}, 30
            )

    def test_is_error_with_success_subtype_keeps_both(self):
        # The precedence bug computed reason "success" for an is_error envelope whose
        # subtype was "success"; _fail_reason must report is_error(success) instead.
        res = self._run_param(
            FAKE_NAIVE_IS_ERROR="1", FAKE_NAIVE_SUBTYPE="success", FAKE_NAIVE_RESULT="x"
        )
        self.assertEqual(res.status, "failed")
        self.assertEqual(res.reason, "is_error(success)")

    def test_nonzero_exit_without_is_error_reports_exit_code(self):
        res = self._run_param(FAKE_NAIVE_EXIT="7", FAKE_NAIVE_RESULT="x")
        self.assertEqual(res.status, "failed")
        self.assertEqual(res.reason, "exit_7")


# ------------------------------------------------------------ naive comment validation


class NaiveCommentValidationTest(RunTestBase):
    """_extract_comments rejects a malformed comments block so the run fails cleanly."""

    def _extract(self, comments_obj):
        text = "```json\n" + json.dumps(comments_obj) + "\n```"
        return run._extract_comments(text)

    def test_string_element_rejected(self):
        self.assertIsNone(self._extract({"comments": ["a string"]}))

    def test_missing_body_key_rejected(self):
        self.assertIsNone(self._extract({"comments": [{"path": "a.py", "line": 1}]}))

    def test_empty_body_rejected(self):
        self.assertIsNone(self._extract({"comments": [{"body": "   "}]}))

    def test_nonstring_path_rejected(self):
        self.assertIsNone(self._extract({"comments": [{"body": "b", "path": 5}]}))

    def test_nonint_line_rejected(self):
        self.assertIsNone(self._extract({"comments": [{"body": "b", "line": "3"}]}))

    def test_bool_line_rejected(self):
        # bool is an int subclass; a JSON true/false is still not a line number.
        self.assertIsNone(self._extract({"comments": [{"body": "b", "line": True}]}))

    def test_minimal_valid_dicts_with_null_path_line_ok(self):
        out = self._extract({"comments": [{"body": "b", "path": None, "line": None}]})
        self.assertEqual(out, [{"body": "b", "path": None, "line": None}])

    def test_body_only_dict_ok(self):
        self.assertEqual(self._extract({"comments": [{"body": "b"}]}), [{"body": "b"}])

    def test_invalid_block_does_not_override_earlier_valid_block(self):
        text = (
            '```json\n{"comments": [{"body": "good", "path": "a.py", "line": 1}]}\n```\n'
            '```json\n{"comments": ["bad element"]}\n```'
        )
        self.assertEqual(
            run._extract_comments(text),
            [{"body": "good", "path": "a.py", "line": 1}],
        )

    def test_unparseable_shape_yields_none_payload(self):
        pr_dir = self.tmp / "pr-val"
        pr_dir.mkdir()
        text = '```json\n{"comments": ["just a string"]}\n```'
        self.assertIsNone(run._naive_payload_from_result(text, pr_dir))
        self.assertFalse((pr_dir / "post-review-payload.json").exists())


if __name__ == "__main__":
    unittest.main()

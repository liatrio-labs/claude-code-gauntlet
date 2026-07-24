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


def fake_invoke_ok(worktree, pr, run_dir, timeout_s=1800, tool="deep-review-v3",
                   child_model="inherit", child_auth="api"):
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

    def _inv(worktree, pr, run_dir, timeout_s=1800, tool="deep-review-v3",
             child_model="inherit", child_auth="api"):
        state["n"] += 1
        if state["n"] == 1:
            return run.invoke.InvokeResult("failed", reason="boom")
        return fake_invoke_ok(worktree, pr, run_dir, timeout_s)

    return _inv


def make_invoke_raises_first(exc):
    """An invoke fake that raises *exc* on the first PR and oks the rest."""
    state = {"n": 0}

    def _inv(worktree, pr, run_dir, timeout_s=1800, tool="deep-review-v3",
             child_model="inherit", child_auth="api"):
        state["n"] += 1
        if state["n"] == 1:
            raise exc
        return fake_invoke_ok(worktree, pr, run_dir, timeout_s)

    return _inv


def make_invoke_expecting_auth(expected):
    """An invoke fake that refuses any child_auth other than *expected*.

    The default-argument fakes above would silently accept "api" if _run_prs stopped
    forwarding the mode, so at least one fake has to assert on what it was handed --
    an unforwarded mode is a run billed against the wrong credential.
    """
    def _inv(worktree, pr, run_dir, timeout_s=1800, tool="deep-review-v3",
             child_model="inherit", child_auth="api"):
        assert child_auth == expected, "invoke_review got child_auth={!r}, want {!r}".format(
            child_auth, expected
        )
        return fake_invoke_ok(worktree, pr, run_dir, timeout_s)

    return _inv


def make_naive_expecting_auth(expected):
    """A _invoke_naive fake that refuses any child_auth other than *expected*."""
    def _naive(worktree, pr, run_dir, diff_text, bench_entry, timeout_s, child_auth="api"):
        assert child_auth == expected, "_invoke_naive got child_auth={!r}, want {!r}".format(
            child_auth, expected
        )
        payload = Path(run_dir) / run.invoke.pr_dir_name(pr) / "post-review-payload.json"
        payload.parent.mkdir(parents=True, exist_ok=True)
        payload.write_text(json.dumps({"payload": {"comments": []}, "skipped": []}))
        return run.invoke.InvokeResult("ok", cost_usd=0.11, payload_path=str(payload))

    return _naive


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
            (None, "headless:/code-gauntlet"),
        ):
            run_dir = self.tmp / "manifest-{}".format(anchor or "skill")
            run_dir.mkdir(parents=True)
            args = types.SimpleNamespace(
                anchor=anchor, fidelity="dry-run", tool="deep-review-v3", child_model=None
            )
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
        self.assertEqual(len(run._resolve_tier("mini", self.subsets, self.shas)), 6)
        self.assertEqual(len(run._resolve_tier("subset", self.subsets, self.shas)), 15)
        self.assertEqual(len(run._resolve_tier("holdout", self.subsets, self.shas)), 10)
        self.assertEqual(len(run._resolve_tier("full", self.subsets, self.shas)), 50)

    def test_subset_maps_to_gate(self):
        self.assertEqual(
            run._resolve_tier("subset", self.subsets, self.shas), self.subsets["gate"]
        )

    def test_mini_maps_to_mini_subset(self):
        self.assertEqual(
            run._resolve_tier("mini", self.subsets, self.shas), self.subsets["mini"]
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

    def test_workflow_records_collected_into_pr_dir(self):
        """_run_prs snapshots claude-home and copies new wf_*.json into pr_dir/workflows/.

        This is the glue feeding check.py G4 (plugin-identity). The fake invoke plants a
        workflow record under the workspace claude-home during the child call.
        """
        pipeline = str(run.REPO_ROOT / "workflows" / "pipeline.js")
        claude_home = self.tmp / "claude-home"

        def invoke_plants_wf(worktree, pr, run_dir, timeout_s=1800, tool="deep-review-v3",
                             child_model="inherit", child_auth="api"):
            # Mimic a child Workflow record written under CLAUDE_CONFIG_DIR during the run.
            wf = (
                claude_home / "config" / "projects" / "slug" / "session" / "workflows"
                / "wf_live-0001.json"
            )
            wf.parent.mkdir(parents=True, exist_ok=True)
            wf.write_text(
                json.dumps({"runId": "wf_live-0001", "scriptPath": pipeline}),
                encoding="utf-8",
            )
            return fake_invoke_ok(
                worktree, pr, run_dir, timeout_s=timeout_s, tool=tool, child_model=child_model
            )

        self._install_runner_fakes(invoke_fn=invoke_plants_wf)
        # Pre-existing unchanged record must not be copied (baseline filter).
        stale = (
            claude_home / "config" / "projects" / "slug" / "session" / "workflows"
            / "wf_stale.json"
        )
        stale.parent.mkdir(parents=True, exist_ok=True)
        stale.write_text(
            json.dumps({"runId": "wf_stale", "scriptPath": "/stale/workflows/pipeline.js"}),
            encoding="utf-8",
        )

        run_dir = self.runs_root / "wf-collect-run"
        run_dir.mkdir(parents=True)
        cp = run.checkpoint.Checkpoint(run_dir)
        run._run_prs(run_dir, [PLAIN_URL], cp, self.shas, set(), 60, None, self.bench_data)

        pr_dir = run_dir / run.invoke.pr_dir_name({"url": PLAIN_URL, **self.shas[PLAIN_URL]})
        collected = pr_dir / "workflows" / "wf_live-0001.json"
        self.assertTrue(collected.is_file(), "new wf record must land in pr_dir/workflows/")
        self.assertEqual(json.loads(collected.read_text())["scriptPath"], pipeline)
        self.assertFalse(
            (pr_dir / "workflows" / "wf_stale.json").exists(),
            "unchanged baseline wf records must not be copied",
        )

    def test_naive_anchor_skips_workflow_record_collection(self):
        self._install_runner_fakes()
        run_dir = self.runs_root / "naive-no-wf"
        run_dir.mkdir(parents=True)
        cp = run.checkpoint.Checkpoint(run_dir)
        # Plant a record that would be copied if the skill path ran.
        claude_home = self.tmp / "claude-home"
        wf = (
            claude_home / "config" / "projects" / "slug" / "session" / "workflows"
            / "wf_should_not_copy.json"
        )
        wf.parent.mkdir(parents=True, exist_ok=True)
        wf.write_text("{}", encoding="utf-8")

        run._run_prs(
            run_dir, [PLAIN_URL], cp, self.shas, set(), 60, "naive", self.bench_data
        )
        pr_dir = run_dir / run.invoke.pr_dir_name({"url": PLAIN_URL, **self.shas[PLAIN_URL]})
        self.assertFalse((pr_dir / "workflows").exists())


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
        self.assertEqual(fingerprint["CODE_GAUNTLET_HEADLESS"], "1")
        self.assertEqual(fingerprint["CODE_GAUNTLET_MODEL_TIER"], "optimized")
        self.assertEqual(fingerprint["timeout_s"], 45 * 60)


class ToolWiringTest(RunTestBase):
    """--tool threads into the manifest and forwards to invoke_review (v3 default)."""

    def _manifest_for(self, argv):
        args = run.parse_args(argv)
        run_dir = self.tmp / "tool-manifest-{}".format(len(list(self.tmp.iterdir())))
        run_dir.mkdir(parents=True)
        run._write_manifest(run_dir, "rid", "smoke", [], 60, args)
        return json.loads((run_dir / "run.json").read_text())

    def test_default_tool_is_v3_in_manifest(self):
        self.assertEqual(self._manifest_for(["--tier", "smoke"])["tool"], "deep-review-v3")

    def test_v2_label_still_selectable(self):
        manifest = self._manifest_for(["--tier", "smoke", "--tool", "deep-review-v2"])
        self.assertEqual(manifest["tool"], "deep-review-v2")

    def test_naive_anchor_leaves_tool_unset(self):
        # score._tool_label gives an explicit tool precedence over anchor, so a naive run
        # must not carry a tool label or its "naive-anchor" ledger label would be masked.
        manifest = self._manifest_for(["--tier", "smoke", "--anchor", "naive"])
        self.assertIsNone(manifest["tool"])

    def test_run_prs_forwards_tool_to_invoke_review(self):
        captured = {}

        def spy(worktree, pr, run_dir, timeout_s=1800, tool="deep-review-v3",
                child_model="inherit", child_auth="api"):
            captured["tool"] = tool
            return fake_invoke_ok(worktree, pr, run_dir, timeout_s)

        self._install_runner_fakes(invoke_fn=spy)
        run_dir = self.runs_root / "tool-forward"
        run_dir.mkdir(parents=True)
        cp = run.checkpoint.Checkpoint(run_dir)
        run._run_prs(
            run_dir, [PLAIN_URL], cp, self.shas, set(), 60, None, self.bench_data,
            tool="deep-review-v2",
        )
        self.assertEqual(captured["tool"], "deep-review-v2")

    def test_child_model_default_inherit_for_v3(self):
        # Measured 2026-07-21: sonnet and sonnet[1m] children both burn MORE tokens than
        # the inherited opus[1m] child (context churn), so v3 defaults to inherit too.
        self.assertEqual(self._manifest_for(["--tier", "smoke"])["child_model"], "inherit")

    def test_child_model_default_inherit_for_v2(self):
        # v2 inherits so its historical baseline model behavior is preserved.
        manifest = self._manifest_for(["--tier", "smoke", "--tool", "deep-review-v2"])
        self.assertEqual(manifest["child_model"], "inherit")

    def test_explicit_child_model_wins_over_per_tool_default(self):
        manifest = self._manifest_for(["--tier", "smoke", "--child-model", "opus"])
        self.assertEqual(manifest["child_model"], "opus")

    def test_naive_anchor_leaves_child_model_unset(self):
        manifest = self._manifest_for(["--tier", "smoke", "--anchor", "naive"])
        self.assertIsNone(manifest["child_model"])

    def test_run_prs_forwards_child_model_to_invoke_review(self):
        captured = {}

        def spy(worktree, pr, run_dir, timeout_s=1800, tool="deep-review-v3",
                child_model="inherit", child_auth="api"):
            captured["child_model"] = child_model
            return fake_invoke_ok(worktree, pr, run_dir, timeout_s)

        self._install_runner_fakes(invoke_fn=spy)
        run_dir = self.runs_root / "child-model-forward"
        run_dir.mkdir(parents=True)
        cp = run.checkpoint.Checkpoint(run_dir)
        run._run_prs(
            run_dir, [PLAIN_URL], cp, self.shas, set(), 60, None, self.bench_data,
            tool="deep-review-v3", child_model="opus",
        )
        self.assertEqual(captured["child_model"], "opus")


# ---------------------------------------------------------------------------- child auth


class ChildAuthCliTest(RunTestBase):
    """--child-auth parses to a None "not specified" sentinel that resolves to api.

    The sentinel is what lets a resumed run keep the mode its manifest recorded instead
    of silently adopting the flag's default half-way through.
    """

    def test_default_is_the_unspecified_sentinel(self):
        self.assertIsNone(run.parse_args(["--tier", "smoke"]).child_auth)

    def test_unspecified_resolves_to_api(self):
        self.assertEqual(run._resolve_child_auth(None), "api")

    def test_explicit_mode_wins(self):
        self.assertEqual(run._resolve_child_auth("subscription"), "subscription")

    def test_subscription_parses(self):
        args = run.parse_args(["--tier", "smoke", "--child-auth", "subscription"])
        self.assertEqual(args.child_auth, "subscription")

    def test_api_parses_explicitly(self):
        args = run.parse_args(["--tier", "smoke", "--child-auth", "api"])
        self.assertEqual(args.child_auth, "api")

    def test_bogus_mode_is_an_argparse_error(self):
        with contextlib.redirect_stderr(io.StringIO()) as err:
            with self.assertRaises(SystemExit) as cm:
                run.parse_args(["--tier", "smoke", "--child-auth", "oauth"])
        self.assertEqual(cm.exception.code, 2)
        self.assertIn("--child-auth", err.getvalue())

    def test_main_hands_the_resolved_mode_to_check_prereqs(self):
        captured = {}

        def spy(*args, **kwargs):
            captured.update(kwargs)
            return ["stop before the run starts"]

        with patch.object(run, "check_prereqs", spy):
            with contextlib.redirect_stderr(io.StringIO()):
                rc = run.main(["--tier", "smoke", "--child-auth", "subscription"])
        self.assertEqual(rc, 2)
        self.assertEqual(captured.get("child_auth"), "subscription")

    def _spy_prereq_mode(self, argv):
        """Return the child_auth main() preflighted with, without starting a run."""
        captured = {}

        def spy(*args, **kwargs):
            captured.update(kwargs)
            return ["stop before the run starts"]

        with patch.object(run, "check_prereqs", spy):
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(run.main(argv), 2)
        return captured.get("child_auth")

    def _write_resumable(self, run_id, child_auth):
        run_dir = self.runs_root / run_id
        run_dir.mkdir(parents=True)
        manifest = {"tier": "smoke", "pr_urls": [PLAIN_URL], "anchor": None}
        if child_auth is not None:
            manifest["child_auth"] = child_auth
        (run_dir / "run.json").write_text(json.dumps(manifest))
        return run_dir

    def test_resume_preflights_the_manifest_mode_not_the_flag_default(self):
        # main() must preflight the mode the run will ACTUALLY use. _resume takes the
        # manifest's mode, so preflighting the flag default would demand the metered key
        # a subscription resume does not need -- and skip the token/apiKeyHelper checks
        # that resume does need, deferring the failure to a mid-run build_env raise.
        self._write_resumable("smoke-resume-sub", "subscription")
        self.assertEqual(
            self._spy_prereq_mode(["--resume", "smoke-resume-sub"]), "subscription"
        )

    def test_retry_failed_preflights_the_manifest_mode_too(self):
        self._write_resumable("smoke-retry-sub", "subscription")
        self.assertEqual(
            self._spy_prereq_mode(["--retry-failed", "smoke-retry-sub"]), "subscription"
        )

    def test_resume_of_a_pre_child_auth_manifest_preflights_api(self):
        self._write_resumable("smoke-resume-legacy", None)
        self.assertEqual(self._spy_prereq_mode(["--resume", "smoke-resume-legacy"]), "api")

    def test_resume_of_a_missing_run_still_preflights_the_flag(self):
        # No manifest to consult (--resume of a bogus id) -- the flag is all there is.
        self.assertEqual(
            self._spy_prereq_mode(["--resume", "no-such-run", "--child-auth", "subscription"]),
            "subscription",
        )

    def test_legacy_resume_cannot_be_switched_to_subscription_by_the_flag(self):
        # A run dir is one auth mode: its PRs' envelope costs are summed into a single
        # ledger row carrying a single auth_mode. A pre-child_auth manifest ran on the
        # metered key, so honouring the flag would bill the remaining PRs the other way
        # and label the mixed total "api". Refuse instead of quietly mixing.
        self._write_resumable("smoke-legacy-switch", None)
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            rc = run.main(["--resume", "smoke-legacy-switch", "--child-auth", "subscription"])
        self.assertEqual(rc, 2)
        message = stderr.getvalue()
        self.assertIn("contradicts", message)
        self.assertIn("api", message)

    def test_resume_flag_agreeing_with_the_recorded_mode_is_accepted(self):
        self._write_resumable("smoke-agree-sub", "subscription")
        self.assertEqual(
            self._spy_prereq_mode(["--resume", "smoke-agree-sub", "--child-auth", "subscription"]),
            "subscription",
        )

    def test_resume_of_a_subscription_run_refuses_an_api_flag(self):
        # The guard is symmetric: downgrading mid-run corrupts the row just as much.
        self._write_resumable("smoke-sub-to-api", "subscription")
        with contextlib.redirect_stderr(io.StringIO()) as err:
            rc = run.main(["--resume", "smoke-sub-to-api", "--child-auth", "api"])
        self.assertEqual(rc, 2)
        message = err.getvalue()
        self.assertIn("contradicts", message)
        self.assertIn("subscription", message)

    def test_new_run_never_trips_the_conflict_guard(self):
        # The guard compares the flag against the resolved mode, which for a new run IS
        # the flag -- so it can only ever fire on a resume.
        self.assertEqual(
            self._spy_prereq_mode(["--tier", "smoke", "--child-auth", "subscription"]),
            "subscription",
        )

    def test_legacy_resume_without_a_flag_stays_on_api(self):
        self._write_resumable("smoke-legacy-noflag", None)
        self.assertEqual(self._spy_prereq_mode(["--resume", "smoke-legacy-noflag"]), "api")

    def test_default_child_auth_is_a_valid_mode(self):
        self.assertIn(run.DEFAULT_CHILD_AUTH, run.invoke.CHILD_AUTH_MODES)

    def _resume_modes(self, run_id, argv):
        """The mode main() preflights and the mode _resume resolves, for one argv.

        They must be equal: main validates the credential the run then spends, so any
        disagreement preflights one credential and charges the other.
        """
        args = run.parse_args(argv)
        return (self._spy_prereq_mode(argv), run._child_auth_for(args, run_id))

    def test_preflight_and_resume_agree_for_an_orphan_run_dir(self):
        # A run dir with no run.json: _make_run_dir created it and the process died
        # before _write_manifest, so no PR ran and no credential was spent. Whatever the
        # rule is, both halves must reach the same answer -- preflighting subscription
        # and then spending the metered key is the silent swap the guard exists to stop.
        (self.runs_root / "smoke-orphan").mkdir(parents=True)
        preflighted, resumed = self._resume_modes(
            "smoke-orphan", ["--resume", "smoke-orphan", "--child-auth", "subscription"]
        )
        self.assertEqual(preflighted, resumed)

    def test_preflight_and_resume_agree_for_a_corrupt_manifest(self):
        run_dir = self.runs_root / "smoke-corrupt"
        run_dir.mkdir(parents=True)
        (run_dir / "run.json").write_text("{not json")
        preflighted, resumed = self._resume_modes(
            "smoke-corrupt", ["--resume", "smoke-corrupt", "--child-auth", "subscription"]
        )
        self.assertEqual(preflighted, resumed)

    def test_preflight_and_resume_agree_for_a_recorded_mode(self):
        self._write_resumable("smoke-recorded", "subscription")
        preflighted, resumed = self._resume_modes(
            "smoke-recorded", ["--resume", "smoke-recorded"]
        )
        self.assertEqual(preflighted, resumed)
        self.assertEqual(resumed, "subscription")

    def test_preflight_and_resume_agree_for_a_legacy_manifest(self):
        self._write_resumable("smoke-legacy-agree", None)
        preflighted, resumed = self._resume_modes(
            "smoke-legacy-agree", ["--resume", "smoke-legacy-agree"]
        )
        self.assertEqual(preflighted, resumed)
        self.assertEqual(resumed, "api")

    def test_env_fingerprint_only_manifest_is_honoured_like_score_reads_it(self):
        # score._auth_mode falls back to env_fingerprint.child_auth, so the resume path
        # must read the same chain: otherwise a manifest carrying only the fingerprint
        # copy would be resumed on the metered key while its ledger row says subscription
        # and the cost-honesty gate drops real spend.
        run_dir = self.runs_root / "smoke-fingerprint-only"
        run_dir.mkdir(parents=True)
        (run_dir / "run.json").write_text(
            json.dumps({
                "tier": "smoke", "pr_urls": [PLAIN_URL], "anchor": None,
                "env_fingerprint": {"child_auth": "subscription"},
            })
        )
        args = run.parse_args(["--resume", "smoke-fingerprint-only"])
        self.assertEqual(run._child_auth_for(args, "smoke-fingerprint-only"), "subscription")


class ChildAuthPrereqTest(RunTestBase):
    """Subscription mode swaps the credential prereq -- and only that.

    It drops the metered-key requirement (that key is deliberately not in play) and
    replaces it with the OAuth token plus a refusal when the isolated config carries an
    apiKeyHelper: the helper outranks the token and, being a file rather than an env var,
    is the one over-ranking source build_env cannot strip. No judge key is demanded here;
    scoring resolves its own and fails loud separately.
    """

    def setUp(self):
        super().setUp()
        self.missing_env = self.tmp / "nope.env"  # does not exist

    def _failures(self, child_auth, env, env_path=None):
        return run.check_prereqs(
            env_path=self.missing_env if env_path is None else env_path,
            workspace_dir=self.tmp,
            child_auth=child_auth,
            env=env,
        )

    def _write_helper(self, claude_home=None, name="settings.json", value="/bin/echo key"):
        config = (claude_home or (self.tmp / "claude-home")) / "config"
        config.mkdir(parents=True, exist_ok=True)
        path = config / name
        path.write_text(json.dumps({"apiKeyHelper": value}))
        return path

    def test_subscription_drops_the_api_key_requirement(self):
        joined = "\n".join(self._failures("subscription", {"CLAUDE_CODE_OAUTH_TOKEN": "oat"}))
        self.assertNotIn("ANTHROPIC_API_KEY", joined)

    def test_subscription_without_a_token_is_one_actionable_failure(self):
        failures = [f for f in self._failures("subscription", {}) if "OAUTH_TOKEN" in f]
        self.assertEqual(len(failures), 1)
        self.assertIn("claude setup-token", failures[0])

    def test_subscription_token_in_dotenv_satisfies_the_check(self):
        env_file = self.tmp / "bench.env"
        env_file.write_text("CLAUDE_CODE_OAUTH_TOKEN=oat-dotenv\n")
        joined = "\n".join(self._failures("subscription", {}, env_path=env_file))
        self.assertNotIn("OAUTH_TOKEN", joined)

    def test_subscription_token_in_env_satisfies_the_check(self):
        joined = "\n".join(self._failures("subscription", {"CLAUDE_CODE_OAUTH_TOKEN": "oat"}))
        self.assertNotIn("OAUTH_TOKEN", joined)

    def test_env_defaults_to_os_environ(self):
        with patch.dict(os.environ, {"CLAUDE_CODE_OAUTH_TOKEN": "oat-ambient"}):
            failures = run.check_prereqs(
                env_path=self.missing_env, workspace_dir=self.tmp, child_auth="subscription"
            )
        self.assertNotIn("OAUTH_TOKEN", "\n".join(failures))

    def test_api_mode_still_requires_the_metered_key(self):
        joined = "\n".join(self._failures("api", {"CLAUDE_CODE_OAUTH_TOKEN": "oat"}))
        self.assertIn("ANTHROPIC_API_KEY missing", joined)
        self.assertNotIn("OAUTH_TOKEN", joined)

    def test_api_key_helper_fails_subscription_and_names_the_file(self):
        path = self._write_helper()
        failures = [
            f for f in self._failures("subscription", {"CLAUDE_CODE_OAUTH_TOKEN": "oat"})
            if "apiKeyHelper" in f
        ]
        self.assertEqual(len(failures), 1)
        self.assertIn(str(path), failures[0])
        self.assertIn("CLAUDE_CODE_OAUTH_TOKEN", failures[0])

    def test_api_key_helper_failure_carries_no_credential(self):
        self._write_helper(value="/bin/echo sk-helper-secret")
        joined = "\n".join(
            self._failures("subscription", {"CLAUDE_CODE_OAUTH_TOKEN": "oat-token-secret"})
        )
        self.assertIn("apiKeyHelper", joined)
        self.assertNotIn("sk-helper-secret", joined)
        self.assertNotIn("oat-token-secret", joined)

    def test_api_key_helper_check_honours_bench_claude_home(self):
        home = self.tmp / "elsewhere-home"
        path = self._write_helper(claude_home=home)
        env = {"CLAUDE_CODE_OAUTH_TOKEN": "oat", "BENCH_CLAUDE_HOME": str(home)}
        self.assertIn(str(path), "\n".join(self._failures("subscription", env)))

    def test_api_key_helper_in_settings_local_is_also_caught(self):
        path = self._write_helper(name="settings.local.json")
        joined = "\n".join(self._failures("subscription", {"CLAUDE_CODE_OAUTH_TOKEN": "oat"}))
        self.assertIn(str(path), joined)

    def test_api_mode_ignores_the_helper_entirely(self):
        self._write_helper()
        joined = "\n".join(self._failures("api", {}))
        self.assertNotIn("apiKeyHelper", joined)


class ChildAuthJudgeKeyNoteTest(RunTestBase):
    """Subscription mode warns -- never fails -- when no judge key is in sight.

    Dropping the ANTHROPIC_API_KEY requirement is the point of the mode, and --check needs
    no judge at all. But scoring does, and it happens hours later, so silence lets an
    operator finish a long leg and only then find it unscoreable. Making it fatal would
    re-impose the very requirement the mode relaxes.
    """

    def setUp(self):
        super().setUp()
        self.missing_env = self.tmp / "nope.env"

    def _note(self, child_auth, env, env_path=None):
        return run._judge_key_note(
            self.missing_env if env_path is None else env_path, child_auth, env=env
        )

    def test_subscription_with_no_key_anywhere_gets_a_note(self):
        note = self._note("subscription", {})
        self.assertIsNotNone(note)
        self.assertIn("--score-only", note)
        self.assertIn("--check", note)

    def test_ambient_judge_key_silences_it(self):
        self.assertIsNone(self._note("subscription", {"BENCH_JUDGE_API_KEY": "k"}))

    def test_ambient_api_key_silences_it(self):
        self.assertIsNone(self._note("subscription", {"ANTHROPIC_API_KEY": "k"}))

    def test_dotenv_key_silences_it(self):
        env_file = self.tmp / "bench.env"
        env_file.write_text("ANTHROPIC_API_KEY=sk-dotenv\n")
        self.assertIsNone(self._note("subscription", {}, env_path=env_file))

    def test_api_mode_is_silent(self):
        # api mode already hard-fails on a missing key; a note would be noise.
        self.assertIsNone(self._note("api", {}))

    def test_main_prints_the_note_once_prereqs_pass(self):
        stderr = io.StringIO()
        with patch.object(run, "check_prereqs", lambda **kwargs: []):
            with patch.object(run, "_new_run", lambda args: 0):
                with patch.dict(os.environ, {}, clear=True):
                    self._patch_global("ENV_PATH", self.missing_env)
                    with contextlib.redirect_stderr(stderr):
                        rc = run.main(["--tier", "smoke", "--child-auth", "subscription"])
        self.assertEqual(rc, 0)
        self.assertIn("judge", stderr.getvalue().lower())


class ChildAuthWiringTest(RunTestBase):
    """--child-auth is recorded for every run and reaches BOTH invocation paths."""

    def _manifest_for(self, argv):
        args = run.parse_args(argv)
        run_dir = self.tmp / "auth-manifest-{}".format(len(list(self.tmp.iterdir())))
        run_dir.mkdir(parents=True)
        run._write_manifest(run_dir, "rid", "smoke", [], 60, args)
        return json.loads((run_dir / "run.json").read_text())

    def test_manifest_defaults_to_api(self):
        self.assertEqual(self._manifest_for(["--tier", "smoke"])["child_auth"], "api")

    def test_manifest_records_explicit_subscription(self):
        manifest = self._manifest_for(["--tier", "smoke", "--child-auth", "subscription"])
        self.assertEqual(manifest["child_auth"], "subscription")

    def test_naive_anchor_still_records_child_auth(self):
        # Deliberately unlike tool/child_model, which are null for a naive run: the anchor
        # authenticates too (_invoke_naive builds its env through build_env), so dropping
        # the label would lose the auth provenance the ledger's cost honesty reads.
        manifest = self._manifest_for(
            ["--tier", "smoke", "--anchor", "naive", "--child-auth", "subscription"]
        )
        self.assertEqual(manifest["child_auth"], "subscription")
        self.assertIsNone(manifest["tool"])
        self.assertIsNone(manifest["child_model"])

    def test_env_fingerprint_carries_the_same_value(self):
        manifest = self._manifest_for(["--tier", "smoke", "--child-auth", "subscription"])
        self.assertEqual(manifest["env_fingerprint"]["child_auth"], "subscription")
        self.assertEqual(manifest["env_fingerprint"]["child_auth"], manifest["child_auth"])

    def test_run_prs_forwards_the_mode_to_invoke_review(self):
        self._install_runner_fakes(invoke_fn=make_invoke_expecting_auth("subscription"))
        run_dir = self.runs_root / "auth-forward-skill"
        run_dir.mkdir(parents=True)
        cp = run.checkpoint.Checkpoint(run_dir)
        run._run_prs(
            run_dir, [PLAIN_URL], cp, self.shas, set(), 60, None, self.bench_data,
            child_auth="subscription",
        )
        self.assertEqual(cp.status(PLAIN_URL), "ok", self._detail(cp, PLAIN_URL).get("reason"))

    def test_run_prs_forwards_the_mode_to_the_naive_anchor(self):
        self._install_runner_fakes()
        run_dir = self.runs_root / "auth-forward-naive"
        run_dir.mkdir(parents=True)
        cp = run.checkpoint.Checkpoint(run_dir)
        with patch.object(run, "_invoke_naive", make_naive_expecting_auth("subscription")):
            run._run_prs(
                run_dir, [PLAIN_URL], cp, self.shas, set(), 60, "naive", self.bench_data,
                child_auth="subscription",
            )
        self.assertEqual(cp.status(PLAIN_URL), "ok", self._detail(cp, PLAIN_URL).get("reason"))

    def test_invoke_naive_forwards_the_mode_to_build_env(self):
        # build_env owns the whole credential decision, so the only thing to pin here is
        # that the anchor hands it the mode rather than defaulting to the metered key.
        captured = {}
        empty_bin = self.tmp / "no-claude-bin"
        empty_bin.mkdir()

        def spy(pr, run_dir, base_env, child_auth="api"):
            captured["child_auth"] = child_auth
            return {"PATH": str(empty_bin)}

        with patch.object(run.invoke, "build_env", spy):
            result = run._invoke_naive(
                self.tmp, NAIVE_PR, self.runs_root / "naive-auth", "diff", {}, 30,
                child_auth="subscription",
            )
        self.assertEqual(captured["child_auth"], "subscription")
        self.assertEqual(result.reason, "claude_not_found")

    def test_new_run_threads_the_resolved_mode(self):
        self._install_runner_fakes(invoke_fn=make_invoke_expecting_auth("subscription"))
        rc = run._new_run(run.parse_args(["--tier", "smoke", "--child-auth", "subscription"]))
        self.assertEqual(rc, 0)

    def test_resume_prefers_the_manifest_mode_over_a_conflicting_flag(self):
        # A resumed run must not switch credentials mid-flight: half its PRs would be
        # billed one way and half the other, and the single ledger row could only label
        # one of them. Same precedence as tool/child_model.
        self._install_runner_fakes(invoke_fn=make_invoke_raises_first(KeyboardInterrupt()))
        with self.assertRaises(KeyboardInterrupt):
            run._new_run(run.parse_args(["--tier", "smoke", "--child-auth", "subscription"]))
        run_dir = next(p for p in self.runs_root.iterdir() if p.is_dir())
        run_id = run_dir.name
        self.assertEqual(
            json.loads((run_dir / "run.json").read_text())["child_auth"], "subscription"
        )

        api_args = run.parse_args(["--resume", run_id, "--child-auth", "api"])
        with patch.object(
            run.invoke, "invoke_review", make_invoke_expecting_auth("subscription")
        ):
            run._resume(run_id, api_args, retry=False)
        cp = run.checkpoint.Checkpoint(run_dir)
        for url in run._resolve_tier("smoke", self.subsets, self.shas):
            self.assertEqual(cp.status(url), "ok", self._detail(cp, url).get("reason"))

    def test_resume_of_a_pre_child_auth_manifest_falls_back_to_api(self):
        # run.json files written before the field existed all ran on the metered key.
        run_dir = self.runs_root / "legacy-run"
        run_dir.mkdir(parents=True)
        (run_dir / "run.json").write_text(
            json.dumps({"tier": "smoke", "pr_urls": [PLAIN_URL], "anchor": None})
        )
        self._install_runner_fakes(invoke_fn=make_invoke_expecting_auth("api"))
        run._resume(run_dir.name, run.parse_args(["--resume", run_dir.name]), retry=False)
        cp = run.checkpoint.Checkpoint(run_dir)
        self.assertEqual(cp.status(PLAIN_URL), "ok", self._detail(cp, PLAIN_URL).get("reason"))


class PrsListTest(RunTestBase):
    """--prs overrides --tier with an explicit, shas.json-validated golden PR list."""

    def test_parses_comma_list_and_strips_whitespace(self):
        args = run.parse_args(["--prs", "{},{}".format(FIXTURE_URL, PLAIN_URL)])
        self.assertEqual(args.prs, [FIXTURE_URL, PLAIN_URL])
        # Surrounding whitespace around each URL is trimmed.
        spaced = run.parse_args(["--prs", " {} , {} ".format(FIXTURE_URL, PLAIN_URL)])
        self.assertEqual(spaced.prs, [FIXTURE_URL, PLAIN_URL])

    def test_prs_mini_alias_expands_to_mini_subset(self):
        args = run.parse_args(["--prs", "mini"])
        self.assertEqual(args.prs, self.subsets["mini"])

    def test_unknown_url_is_argparse_error(self):
        with contextlib.redirect_stderr(io.StringIO()) as err:
            with self.assertRaises(SystemExit) as cm:
                run.parse_args(["--prs", "https://github.com/nope/nope/pull/999999"])
        self.assertEqual(cm.exception.code, 2)
        self.assertIn("not in shas.json", err.getvalue())

    def test_manifest_round_trip_labels_custom(self):
        self._install_runner_fakes()
        urls = [FIXTURE_URL, PLAIN_URL]
        args = run.parse_args(["--prs", ",".join(urls)])
        run._new_run(args)
        run_dir = next(p for p in self.runs_root.iterdir() if p.is_dir())
        manifest = json.loads((run_dir / "run.json").read_text())
        self.assertEqual(manifest["tier"], "custom")
        self.assertEqual(manifest["prs"], urls)
        self.assertEqual(manifest["pr_urls"], urls)
        # run_id is prefixed with the custom tier label.
        self.assertTrue(run_dir.name.startswith("custom-"))

    def test_tier_run_records_null_prs(self):
        self._install_runner_fakes()
        args = run.parse_args(["--tier", "smoke"])
        run._new_run(args)
        run_dir = next(p for p in self.runs_root.iterdir() if p.is_dir())
        manifest = json.loads((run_dir / "run.json").read_text())
        self.assertIsNone(manifest["prs"])
        self.assertEqual(manifest["tier"], "smoke")

    def test_resume_reruns_manifest_urls(self):
        # A --prs run interrupted (all PRs left pending) resumes from the manifest's
        # recorded URLs -- tier is "custom" so there is no subset to re-resolve; resume
        # reads pr_urls exactly like a tier run does.
        self._install_runner_fakes(invoke_fn=make_invoke_raises_first(KeyboardInterrupt()))
        urls = [FIXTURE_URL, PLAIN_URL]
        with self.assertRaises(KeyboardInterrupt):
            run._new_run(run.parse_args(["--prs", ",".join(urls)]))
        run_dir = next(p for p in self.runs_root.iterdir() if p.is_dir())
        run_id = run_dir.name

        seen = []

        def spy(worktree, pr, run_dir, timeout_s=1800, tool="deep-review-v3",
                child_model="inherit", child_auth="api"):
            seen.append(pr["url"])
            return fake_invoke_ok(worktree, pr, run_dir, timeout_s)

        with patch.object(run.invoke, "invoke_review", spy):
            run._resume(run_id, run.parse_args(["--resume", run_id]), retry=False)
        self.assertEqual(set(seen), set(urls))


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

    def test_resume_prefers_manifest_tool_over_cli_default(self):
        # A run started with --tool deep-review-v2 then interrupted (all PRs left pending)
        # must, on resume with default args (tool defaults to deep-review-v3), re-invoke
        # with the manifest's recorded tool -- the run.py:705 precedence
        # `manifest.get("tool") or args.tool`. The manifest wins over the v3 default.
        self._install_runner_fakes(invoke_fn=make_invoke_raises_first(KeyboardInterrupt()))
        v2_args = run.parse_args(["--tier", "smoke", "--tool", "deep-review-v2"])
        with self.assertRaises(KeyboardInterrupt):
            run._new_run(v2_args)  # writes the v2 manifest, then interrupts on PR 1
        run_dir = next(p for p in self.runs_root.iterdir() if p.is_dir())
        run_id = run_dir.name
        self.assertEqual(
            json.loads((run_dir / "run.json").read_text())["tool"], "deep-review-v2"
        )

        captured = []

        def spy(worktree, pr, run_dir, timeout_s=1800, tool="deep-review-v3",
                child_model="inherit", child_auth="api"):
            captured.append(tool)
            return fake_invoke_ok(worktree, pr, run_dir, timeout_s)

        default_args = run.parse_args(["--tier", "smoke"])  # no --tool -> v3 default
        with patch.object(run.invoke, "invoke_review", spy):
            run._resume(run_id, default_args, retry=False)
        # Every re-invoked (pending) PR used the manifest's v2 tool, never the v3 default.
        self.assertTrue(captured)
        self.assertTrue(all(t == "deep-review-v2" for t in captured))

    def test_resume_prefers_manifest_child_model_over_default(self):
        # A run started with --child-model opus then interrupted (all PRs left pending) must,
        # on resume with default args (child_model resolves to the v3 default sonnet),
        # re-invoke with the manifest's recorded opus -- the same precedence as tool
        # (`manifest.get("child_model") or _resolve_child_model(...)`).
        self._install_runner_fakes(invoke_fn=make_invoke_raises_first(KeyboardInterrupt()))
        opus_args = run.parse_args(["--tier", "smoke", "--child-model", "opus"])
        with self.assertRaises(KeyboardInterrupt):
            run._new_run(opus_args)  # writes the opus manifest, then interrupts on PR 1
        run_dir = next(p for p in self.runs_root.iterdir() if p.is_dir())
        run_id = run_dir.name
        self.assertEqual(
            json.loads((run_dir / "run.json").read_text())["child_model"], "opus"
        )

        captured = []

        def spy(worktree, pr, run_dir, timeout_s=1800, tool="deep-review-v3",
                child_model="inherit", child_auth="api"):
            captured.append(child_model)
            return fake_invoke_ok(worktree, pr, run_dir, timeout_s)

        default_args = run.parse_args(["--tier", "smoke"])  # no --child-model -> sonnet
        with patch.object(run.invoke, "invoke_review", spy):
            run._resume(run_id, default_args, retry=False)
        # Every re-invoked PR used the manifest's opus pin, never the resolved v3 sonnet.
        self.assertTrue(captured)
        self.assertTrue(all(m == "opus" for m in captured))


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

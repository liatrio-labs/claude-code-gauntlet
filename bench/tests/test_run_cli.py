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

        fixture_pr = self.shas[FIXTURE_URL]["pr_number"]
        plain_pr = self.shas[PLAIN_URL]["pr_number"]
        fixture_review = run_dir / "pr-{}".format(fixture_pr) / "worktree" / "REVIEW.md"
        plain_review = run_dir / "pr-{}".format(plain_pr) / "worktree" / "REVIEW.md"

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

        pr_number = self.shas[PLAIN_URL]["pr_number"]
        pr_dir = run_dir / "pr-{}".format(pr_number)
        self.assertTrue((pr_dir / "post-review-payload.json").is_file())
        self.assertTrue((pr_dir / "deep-review-report.md").is_file())
        self.assertTrue((pr_dir / "diff.patch").exists())
        # The shared output dir must be left empty for the next PR.
        output_dir = run_dir / "output"
        self.assertTrue(output_dir.is_dir())
        self.assertEqual(list(output_dir.iterdir()), [])
        self.assertEqual(cp.status(PLAIN_URL), "ok")


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


if __name__ == "__main__":
    unittest.main()

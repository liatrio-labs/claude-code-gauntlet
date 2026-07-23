"""Tests for the mechanical functional-smoke checker (Issue #28).

stdlib only, no network, no judge. Builds synthetic run trees under a tempdir and
asserts each gate (G1–G4) independently, plus the ``--check`` CLI path.
"""

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from bench import run  # noqa: E402
from bench.runner import check  # noqa: E402

PIPELINE = str(REPO_ROOT / "workflows" / "pipeline.js")


def _write_json(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2), encoding="utf-8")


def _ok_payload(n_comments=1):
    comments = [
        {"path": "a.py", "line": 10 + i, "body": "finding {}".format(i)}
        for i in range(n_comments)
    ]
    return {
        "platform": "github",
        "endpoint": "repos/o/r/pulls/1/reviews",
        "method": "POST",
        "payload": {
            "event": "COMMENT",
            "body": "deep-review (dry-run)",
            "comments": comments,
        },
        "skipped": [],
    }


def _ok_finding(origin="new"):
    return {
        "id": "f1",
        "title": "Null deref",
        "description": "Possible null dereference.",
        "body": "Possible null dereference.",
        "dimension": "bug",
        "severity": "high",
        "confidence": 0.9,
        "file": "a.py",
        "line_start": 10,
        "line_end": 10,
        "line": 10,
        "end_line": 10,
        "origin": origin,
        "cross_file_refs": [],
        "report_destination": "inline",
    }


def _raw_with_script_path(script_path=PIPELINE):
    return {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": "Review complete.",
        "total_cost_usd": 1.0,
        "tool_uses": [
            {
                "name": "Workflow",
                "input": {"scriptPath": script_path, "args": {}},
            }
        ],
    }


def _build_ok_run(run_dir, *, n_prs=1, n_comments=1, origin="new", script_path=PIPELINE):
    """Populate ``run_dir`` with a checker-passing synthetic run."""
    _write_json(
        run_dir / "run.json",
        {"run_id": run_dir.name, "tier": "smoke", "pr_urls": []},
    )
    state = run_dir / "state"
    state.mkdir(parents=True, exist_ok=True)
    for i in range(n_prs):
        url = "https://github.com/example/repo/pull/{}".format(i + 1)
        _write_json(
            state / "pr-{}.json".format(i + 1),
            {"url": url, "status": "ok", "detail": {}, "ts": "2026-07-23T00:00:00Z"},
        )
        pr_dir = run_dir / "pr-example-repo-{}".format(i + 1)
        pr_dir.mkdir(parents=True, exist_ok=True)
        _write_json(pr_dir / "post-review-payload.json", _ok_payload(n_comments))
        _write_json(
            pr_dir / "code-gauntlet-findings-deadbeef.json",
            {"findings": [_ok_finding(origin=origin)]},
        )
        _write_json(pr_dir / "raw.json", _raw_with_script_path(script_path))
        (pr_dir / "deep-review-report.md").write_text(
            "# Report\n\nAll good.\n", encoding="utf-8"
        )


class CheckRunTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="bench-check-")
        self.run_dir = Path(self.tmp) / "smoke-20260723-000000-abc1234"
        self.run_dir.mkdir()

    def tearDown(self):
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_happy_path_passes(self):
        _build_ok_run(self.run_dir)
        result = check.check_run(self.run_dir, repo_root=REPO_ROOT)
        self.assertTrue(result["ok"], result["failures"])
        self.assertEqual(result["failures"], [])
        self.assertGreaterEqual(result["stats"]["delivered_comments"], 1)

    def test_missing_payload_fails_g1(self):
        _build_ok_run(self.run_dir)
        (self.run_dir / "pr-example-repo-1" / "post-review-payload.json").unlink()
        result = check.check_run(self.run_dir, repo_root=REPO_ROOT)
        self.assertFalse(result["ok"])
        self.assertTrue(any("missing post-review-payload" in f for f in result["failures"]))

    def test_bad_platform_fails_g1(self):
        _build_ok_run(self.run_dir)
        _write_json(
            self.run_dir / "pr-example-repo-1" / "post-review-payload.json",
            {"platform": "bitbucket", "payload": {"comments": []}},
        )
        result = check.check_run(self.run_dir, repo_root=REPO_ROOT)
        self.assertFalse(result["ok"])
        self.assertTrue(any("unrecognized payload platform" in f for f in result["failures"]))

    def test_unknown_origin_fails_g2(self):
        _build_ok_run(self.run_dir, origin="unknown")
        result = check.check_run(self.run_dir, repo_root=REPO_ROOT)
        self.assertFalse(result["ok"])
        self.assertTrue(any("origin=unknown" in f for f in result["failures"]))
        self.assertEqual(result["stats"]["unknown_origin"], 1)

    def test_no_write_proof_gap_fails_g2(self):
        _build_ok_run(self.run_dir)
        report = self.run_dir / "pr-example-repo-1" / "deep-review-report.md"
        report.write_text(
            "# Report\n\ngaps: writeArtifacts: writer echo did not account "
            "for all four planned artifact paths (no write proof) — "
            "artifacts not persisted (partial-artifacts)\n",
            encoding="utf-8",
        )
        result = check.check_run(self.run_dir, repo_root=REPO_ROOT)
        self.assertFalse(result["ok"])
        self.assertTrue(
            any("no-write-proof" in f or "partial-artifacts" in f for f in result["failures"])
        )

    def test_stale_marketplace_script_path_fails_g3(self):
        # Same filename, wrong root — the contamination class that voided runs.
        _build_ok_run(
            self.run_dir,
            script_path="/home/user/.claude/plugins/cache/stale/workflows/pipeline.js",
        )
        result = check.check_run(self.run_dir, repo_root=REPO_ROOT)
        self.assertFalse(result["ok"])
        self.assertTrue(any("scriptPath" in f for f in result["failures"]))

    def test_missing_script_path_fails_g3(self):
        _build_ok_run(self.run_dir)
        _write_json(
            self.run_dir / "pr-example-repo-1" / "raw.json",
            {"type": "result", "result": "no workflow record here"},
        )
        result = check.check_run(self.run_dir, repo_root=REPO_ROOT)
        self.assertFalse(result["ok"])
        self.assertTrue(any("no scriptPath found" in f for f in result["failures"]))

    def test_zero_comments_fails_g4(self):
        _build_ok_run(self.run_dir, n_comments=0)
        result = check.check_run(self.run_dir, repo_root=REPO_ROOT)
        self.assertFalse(result["ok"])
        self.assertTrue(any("delivered comments" in f for f in result["failures"]))

    def test_non_ok_checkpoint_fails_precondition(self):
        _build_ok_run(self.run_dir)
        _write_json(
            self.run_dir / "state" / "pr-1.json",
            {
                "url": "https://github.com/example/repo/pull/1",
                "status": "failed",
                "detail": {"reason": "boom"},
                "ts": "2026-07-23T00:00:00Z",
            },
        )
        result = check.check_run(self.run_dir, repo_root=REPO_ROOT)
        self.assertFalse(result["ok"])
        self.assertTrue(any("precondition" in f for f in result["failures"]))

    def test_union_schema_missing_description_fails_g1(self):
        _build_ok_run(self.run_dir)
        bad = _ok_finding()
        del bad["description"]
        del bad["body"]
        _write_json(
            self.run_dir / "pr-example-repo-1" / "code-gauntlet-findings-deadbeef.json",
            {"findings": [bad]},
        )
        result = check.check_run(self.run_dir, repo_root=REPO_ROOT)
        self.assertFalse(result["ok"])
        self.assertTrue(any("description/body" in f for f in result["failures"]))

    def test_relative_pipeline_script_path_accepted(self):
        _build_ok_run(self.run_dir, script_path="workflows/pipeline.js")
        result = check.check_run(self.run_dir, repo_root=REPO_ROOT)
        self.assertTrue(result["ok"], result["failures"])

    def test_does_not_import_score_module(self):
        """Checker must never pull in the judge/score path."""
        import importlib

        # Ensure a clean import of check does not load score.
        if "bench.runner.score" in sys.modules:
            del sys.modules["bench.runner.score"]
        importlib.reload(check)
        self.assertNotIn("bench.runner.score", sys.modules)
        _build_ok_run(self.run_dir)
        check.check_run(self.run_dir, repo_root=REPO_ROOT)
        self.assertNotIn("bench.runner.score", sys.modules)


class CheckCliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="bench-check-cli-")
        self.runs_root = Path(self.tmp) / "runs"
        self.runs_root.mkdir()
        self.run_dir = self.runs_root / "smoke-20260723-000000-abc1234"
        self.run_dir.mkdir()
        _build_ok_run(self.run_dir)
        self._runs_patch = patch.object(run, "RUNS_ROOT", self.runs_root)
        self._runs_patch.start()

    def tearDown(self):
        self._runs_patch.stop()
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_check_cli_passes(self):
        with contextlib.redirect_stdout(io.StringIO()):
            rc = run.main(["--check", self.run_dir.name])
        self.assertEqual(rc, 0)

    def test_check_cli_fails_on_gate(self):
        (self.run_dir / "pr-example-repo-1" / "post-review-payload.json").unlink()
        with contextlib.redirect_stdout(io.StringIO()):
            with contextlib.redirect_stderr(io.StringIO()):
                rc = run.main(["--check", self.run_dir.name])
        self.assertEqual(rc, 1)

    def test_check_missing_run_is_exit_2(self):
        with contextlib.redirect_stderr(io.StringIO()):
            rc = run.main(["--check", "does-not-exist"])
        self.assertEqual(rc, 2)

    def test_check_mutex_with_score_only(self):
        with contextlib.redirect_stderr(io.StringIO()) as err:
            rc = run.main(["--check", "x", "--score-only", "y"])
        self.assertEqual(rc, 2)
        self.assertIn("--check", err.getvalue())


class MiniResolutionTest(unittest.TestCase):
    def test_tier_mini_resolves_six(self):
        subsets = json.loads((REPO_ROOT / "bench/golden/subsets.json").read_text())
        shas = json.loads((REPO_ROOT / "bench/golden/shas.json").read_text())
        urls = run._resolve_tier("mini", subsets, shas)
        self.assertEqual(len(urls), 6)
        self.assertEqual(urls, subsets["mini"])

    def test_prs_mini_alias_expands(self):
        subsets = json.loads((REPO_ROOT / "bench/golden/subsets.json").read_text())
        args = run.parse_args(["--prs", "mini"])
        self.assertEqual(args.prs, subsets["mini"])


if __name__ == "__main__":
    unittest.main()

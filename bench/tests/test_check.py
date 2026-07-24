"""Tests for the mechanical functional-smoke checker (Issue #28).

stdlib only, no network, no judge. Fixtures mirror real harness artifact names:
bare findings lists, ``workflows/wf_*.json`` (not fabricated ``raw.json`` tool_uses),
``code-gauntlet-checkpoint-all-*.json``, and ``run.json`` ``pr_urls`` completeness.
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
from bench.runner import check, invoke  # noqa: E402

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


def _ok_gitlab_payload(n_discussions=1):
    """Minimal GitLab dry-run payload shape (platform + discussions)."""
    discussions = [
        {
            "body": "finding {}".format(i),
            "position": {
                "position_type": "text",
                "new_path": "a.py",
                "new_line": 10 + i,
                "old_path": "a.py",
            },
        }
        for i in range(n_discussions)
    ]
    return {
        "platform": "gitlab",
        "discussions": discussions,
        "skipped": [],
    }


def _ok_finding(origin="new"):
    # Real persist output is a bare list of union-schema findings.
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


def _wf_record(script_path=PIPELINE, *, include_verify=True):
    """Shape of a per-child Workflow record (the real scriptPath carrier).

    Real skill runs also persist ``args.verify.scriptPath`` → verify_findings.py;
    G4 must ignore that nested path and only grade the top-level Workflow path.
    """
    rec = {
        "runId": "wf_test-0001",
        "scriptPath": script_path,
        "status": "completed",
    }
    if include_verify:
        # Absolute form mirrors SKILL.md's ``{plugin_root}/scripts/verify_findings.py``.
        rec["args"] = {
            "verify": {
                "scriptPath": str(REPO_ROOT / "scripts" / "verify_findings.py"),
                "inputPathBase": "/tmp/in",
                "outputPathBase": "/tmp/out",
            }
        }
    return rec


def _build_ok_run(
    run_dir,
    *,
    n_prs=1,
    n_comments=1,
    origin="new",
    script_path=PIPELINE,
    pr_urls=None,
    completed_prs=None,
    include_findings=True,
    include_workflow=True,
):
    """Populate ``run_dir`` with a checker-passing synthetic run (realistic names).

    ``pr_urls`` is what ``run.json`` declares. ``completed_prs`` (default: all of
    ``pr_urls``) controls which PRs get state files + artifact dirs — use a shorter
    list to model a mid-run kill.
    """
    urls = pr_urls
    if urls is None:
        urls = [
            "https://github.com/example/repo/pull/{}".format(i + 1) for i in range(n_prs)
        ]
    done = list(urls if completed_prs is None else completed_prs)
    _write_json(
        run_dir / "run.json",
        {
            "run_id": run_dir.name,
            "tier": "smoke",
            "anchor": None,
            "pr_urls": list(urls),
        },
    )
    state = run_dir / "state"
    state.mkdir(parents=True, exist_ok=True)
    for i, url in enumerate(done):
        _write_json(
            state / "pr-{}.json".format(i + 1),
            {"url": url, "status": "ok", "detail": {}, "ts": "2026-07-23T00:00:00Z"},
        )
        pr_dir = run_dir / "pr-example-repo-{}".format(i + 1)
        pr_dir.mkdir(parents=True, exist_ok=True)
        _write_json(pr_dir / "post-review-payload.json", _ok_payload(n_comments))
        if include_findings:
            # Bare list — the real writeArtifacts shape.
            _write_json(
                pr_dir / "code-gauntlet-findings-deadbeef.json",
                [_ok_finding(origin=origin)],
            )
        if include_workflow:
            _write_json(pr_dir / "workflows" / "wf_test-0001.json", _wf_record(script_path))
        # Result envelope only — no tool_uses / scriptPath (matches production raw.json).
        _write_json(
            pr_dir / "raw.json",
            {
                "type": "result",
                "subtype": "success",
                "is_error": False,
                "result": "Review complete.",
                "total_cost_usd": 1.0,
                "session_id": "fake-session-0001",
            },
        )
        (pr_dir / "code-gauntlet-report-deadbeef.md").write_text(
            "# Report\n\nAll good.\n", encoding="utf-8"
        )
        _write_json(
            pr_dir / "code-gauntlet-checkpoint-all-deadbeef.json",
            {"phases": {}, "gaps": []},
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
        self.assertGreaterEqual(result["stats"]["workflow_records"], 1)

    def test_gitlab_payload_happy_path_passes(self):
        _build_ok_run(self.run_dir)
        _write_json(
            self.run_dir / "pr-example-repo-1" / "post-review-payload.json",
            _ok_gitlab_payload(n_discussions=2),
        )
        result = check.check_run(self.run_dir, repo_root=REPO_ROOT)
        self.assertTrue(result["ok"], result["failures"])
        self.assertEqual(result["stats"]["delivered_comments"], 2)

    def test_gitlab_payload_missing_position_fails_g2(self):
        _build_ok_run(self.run_dir)
        bad = _ok_gitlab_payload()
        del bad["discussions"][0]["position"]["new_path"]
        _write_json(
            self.run_dir / "pr-example-repo-1" / "post-review-payload.json",
            bad,
        )
        result = check.check_run(self.run_dir, repo_root=REPO_ROOT)
        self.assertFalse(result["ok"])
        self.assertTrue(
            any("new_path/new_line" in f for f in result["failures"])
        )

    def test_dict_wrapped_findings_accepted(self):
        _build_ok_run(self.run_dir)
        _write_json(
            self.run_dir / "pr-example-repo-1" / "code-gauntlet-findings-deadbeef.json",
            {"findings": [_ok_finding()]},
        )
        result = check.check_run(self.run_dir, repo_root=REPO_ROOT)
        self.assertTrue(result["ok"], result["failures"])

    def test_bare_list_unknown_origin_fails_g3(self):
        _build_ok_run(self.run_dir, origin="unknown")
        result = check.check_run(self.run_dir, repo_root=REPO_ROOT)
        self.assertFalse(result["ok"])
        self.assertTrue(any("origin=unknown" in f for f in result["failures"]))

    def test_missing_payload_fails_g2(self):
        _build_ok_run(self.run_dir)
        (self.run_dir / "pr-example-repo-1" / "post-review-payload.json").unlink()
        result = check.check_run(self.run_dir, repo_root=REPO_ROOT)
        self.assertFalse(result["ok"])
        self.assertTrue(any("missing post-review-payload" in f for f in result["failures"]))

    def test_missing_findings_fails_g2(self):
        _build_ok_run(self.run_dir, include_findings=False)
        result = check.check_run(self.run_dir, repo_root=REPO_ROOT)
        self.assertFalse(result["ok"])
        self.assertTrue(
            any("missing code-gauntlet-findings" in f for f in result["failures"])
        )
        self.assertEqual(result["stats"]["findings_files"], 0)

    def test_partial_run_missing_checkpoint_fails_g1(self):
        # run.json declares 3 PRs; only 1 has state + artifacts (mid-run kill).
        urls = [
            "https://github.com/example/repo/pull/1",
            "https://github.com/example/repo/pull/2",
            "https://github.com/example/repo/pull/3",
        ]
        _build_ok_run(self.run_dir, pr_urls=urls, completed_prs=urls[:1])
        result = check.check_run(self.run_dir, repo_root=REPO_ROOT)
        self.assertFalse(result["ok"])
        self.assertTrue(any("no checkpoint" in f for f in result["failures"]))
        self.assertTrue(any("declares 3 PR" in f for f in result["failures"]))

    def test_checkpoint_all_degrade_fails_g3(self):
        _build_ok_run(self.run_dir)
        _write_json(
            self.run_dir / "pr-example-repo-1" / "code-gauntlet-checkpoint-all-deadbeef.json",
            {
                "gaps": [
                    "writeArtifacts: writer echo did not account for all four "
                    "planned artifact paths (no write proof) — artifacts not "
                    "persisted (partial-artifacts)"
                ]
            },
        )
        result = check.check_run(self.run_dir, repo_root=REPO_ROOT)
        self.assertFalse(result["ok"])
        self.assertTrue(
            any("checkpoint-all" in f and "no-write-proof" in f for f in result["failures"])
        )

    def test_report_degrade_fails_g3(self):
        _build_ok_run(self.run_dir)
        report = (
            self.run_dir / "pr-example-repo-1" / "code-gauntlet-report-deadbeef.md"
        )
        report.write_text(
            "# Report\n\ngaps: writeArtifacts: no write proof — partial-artifacts\n",
            encoding="utf-8",
        )
        result = check.check_run(self.run_dir, repo_root=REPO_ROOT)
        self.assertFalse(result["ok"])
        self.assertTrue(any("code-gauntlet-report" in f for f in result["failures"]))

    def test_stale_marketplace_script_path_fails_g4(self):
        _build_ok_run(
            self.run_dir,
            script_path="/home/user/.claude/plugins/cache/stale/workflows/pipeline.js",
        )
        result = check.check_run(self.run_dir, repo_root=REPO_ROOT)
        self.assertFalse(result["ok"])
        self.assertTrue(any("scriptPath" in f for f in result["failures"]))

    def test_nested_verify_script_path_ignored_by_g4(self):
        """Healthy runs carry args.verify.scriptPath → verify_findings.py; must pass."""
        _build_ok_run(self.run_dir)
        # Explicitly assert the fixture planted a nested non-pipeline scriptPath.
        wf = self.run_dir / "pr-example-repo-1" / "workflows" / "wf_test-0001.json"
        data = json.loads(wf.read_text(encoding="utf-8"))
        nested = data["args"]["verify"]["scriptPath"]
        self.assertTrue(nested.endswith("verify_findings.py"), nested)
        self.assertNotEqual(nested, data["scriptPath"])
        result = check.check_run(self.run_dir, repo_root=REPO_ROOT)
        self.assertTrue(result["ok"], result["failures"])
        # Extractor returns only the Workflow invocation path.
        extracted = check._extract_script_paths(wf)
        self.assertEqual(extracted, [PIPELINE])

    def test_extract_script_paths_skips_nested_under_args(self):
        wf = self.run_dir / "wf_nested.json"
        _write_json(
            wf,
            {
                "scriptPath": PIPELINE,
                "args": {
                    "verify": {"scriptPath": "/plugin/scripts/verify_findings.py"},
                    "other": {"scriptPath": "/should/ignore.js"},
                },
            },
        )
        self.assertEqual(check._extract_script_paths(wf), [PIPELINE])

    def test_extract_script_paths_accepts_wrapped_tool_input(self):
        wf = self.run_dir / "wf_wrapped.json"
        _write_json(
            wf,
            {
                "runId": "wf_wrap",
                "input": {
                    "scriptPath": PIPELINE,
                    "args": {
                        "verify": {"scriptPath": "/plugin/scripts/verify_findings.py"},
                    },
                },
            },
        )
        self.assertEqual(check._extract_script_paths(wf), [PIPELINE])

    def test_missing_workflow_records_fails_g4(self):
        _build_ok_run(self.run_dir, include_workflow=False)
        result = check.check_run(self.run_dir, repo_root=REPO_ROOT)
        self.assertFalse(result["ok"])
        self.assertTrue(any("no workflows/wf_" in f for f in result["failures"]))
        # raw.json must NOT be treated as a scriptPath source.
        self.assertFalse(any("raw.json" in f and "scriptPath" in f for f in result["failures"]))

    def test_zero_comments_fails_g5(self):
        _build_ok_run(self.run_dir, n_comments=0)
        result = check.check_run(self.run_dir, repo_root=REPO_ROOT)
        self.assertFalse(result["ok"])
        self.assertTrue(any("delivered comments" in f for f in result["failures"]))

    def test_naive_anchor_refused(self):
        _build_ok_run(self.run_dir)
        manifest = json.loads((self.run_dir / "run.json").read_text())
        manifest["anchor"] = "naive"
        _write_json(self.run_dir / "run.json", manifest)
        result = check.check_run(self.run_dir, repo_root=REPO_ROOT)
        self.assertTrue(result.get("refused"))
        self.assertFalse(result["ok"])
        self.assertTrue(any("naive" in f for f in result["failures"]))

    def test_union_schema_missing_description_fails_g2(self):
        _build_ok_run(self.run_dir)
        bad = _ok_finding()
        del bad["description"]
        del bad["body"]
        _write_json(
            self.run_dir / "pr-example-repo-1" / "code-gauntlet-findings-deadbeef.json",
            [bad],
        )
        result = check.check_run(self.run_dir, repo_root=REPO_ROOT)
        self.assertFalse(result["ok"])
        self.assertTrue(any("description/body" in f for f in result["failures"]))

    def test_union_schema_missing_line_identity_fails_g2(self):
        _build_ok_run(self.run_dir)
        bad = _ok_finding()
        del bad["line_start"]
        del bad["line"]
        _write_json(
            self.run_dir / "pr-example-repo-1" / "code-gauntlet-findings-deadbeef.json",
            [bad],
        )
        result = check.check_run(self.run_dir, repo_root=REPO_ROOT)
        self.assertFalse(result["ok"])
        self.assertTrue(any("line identity" in f for f in result["failures"]))

    def test_union_schema_missing_file_fails_g2(self):
        _build_ok_run(self.run_dir)
        bad = _ok_finding()
        del bad["file"]
        _write_json(
            self.run_dir / "pr-example-repo-1" / "code-gauntlet-findings-deadbeef.json",
            [bad],
        )
        result = check.check_run(self.run_dir, repo_root=REPO_ROOT)
        self.assertFalse(result["ok"])
        self.assertTrue(any("missing required field group file" in f for f in result["failures"]))

    def test_union_schema_missing_origin_fails_g2(self):
        _build_ok_run(self.run_dir)
        bad = _ok_finding()
        del bad["origin"]
        _write_json(
            self.run_dir / "pr-example-repo-1" / "code-gauntlet-findings-deadbeef.json",
            [bad],
        )
        result = check.check_run(self.run_dir, repo_root=REPO_ROOT)
        self.assertFalse(result["ok"])
        self.assertTrue(
            any("missing required field group origin" in f for f in result["failures"])
        )

    def test_relative_pipeline_script_path_accepted(self):
        _build_ok_run(self.run_dir, script_path="workflows/pipeline.js")
        result = check.check_run(self.run_dir, repo_root=REPO_ROOT)
        self.assertTrue(result["ok"], result["failures"])

    def test_does_not_import_score_module(self):
        import importlib

        if "bench.runner.score" in sys.modules:
            del sys.modules["bench.runner.score"]
        importlib.reload(check)
        self.assertNotIn("bench.runner.score", sys.modules)
        _build_ok_run(self.run_dir)
        check.check_run(self.run_dir, repo_root=REPO_ROOT)
        self.assertNotIn("bench.runner.score", sys.modules)


class WorkflowRecordCollectionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="bench-wf-collect-")
        self.home = Path(self.tmp) / "claude-home"
        self.pr_dir = Path(self.tmp) / "pr-example-repo-1"
        self.pr_dir.mkdir()

    def tearDown(self):
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def _plant(self, rel, payload, mtime_ns=None):
        path = self.home / "config" / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload), encoding="utf-8")
        if mtime_ns is not None:
            import os

            os.utime(path, ns=(mtime_ns, mtime_ns))
        return path

    def test_collects_only_new_or_changed_records(self):
        old = self._plant(
            "projects/slug/session/workflows/wf_old.json",
            _wf_record("/old/workflows/pipeline.js"),
        )
        baseline = invoke.snapshot_workflow_records(self.home)
        self.assertIn(str(old.resolve()), baseline)

        self._plant(
            "projects/slug/session/workflows/wf_new.json",
            _wf_record(PIPELINE),
        )
        # Change the old record so it is re-copied.
        self._plant(
            "projects/slug/session/workflows/wf_old.json",
            _wf_record(PIPELINE),
        )

        copied = invoke.collect_workflow_records(self.home, self.pr_dir, baseline)
        self.assertEqual(set(copied), {"wf_new.json", "wf_old.json"})
        dest = self.pr_dir / "workflows"
        self.assertTrue((dest / "wf_new.json").is_file())
        self.assertTrue((dest / "wf_old.json").is_file())
        data = json.loads((dest / "wf_new.json").read_text())
        self.assertEqual(data["scriptPath"], PIPELINE)

    def test_unchanged_baseline_copies_nothing(self):
        self._plant(
            "projects/slug/session/workflows/wf_old.json",
            _wf_record(PIPELINE),
        )
        baseline = invoke.snapshot_workflow_records(self.home)
        copied = invoke.collect_workflow_records(self.home, self.pr_dir, baseline)
        self.assertEqual(copied, [])
        self.assertFalse((self.pr_dir / "workflows").exists())

    def test_filename_collision_gets_numeric_suffix(self):
        # Same basename from two project slugs — second copy must not overwrite.
        self._plant(
            "projects/slug-a/session/workflows/wf_same.json",
            _wf_record(PIPELINE),
        )
        self._plant(
            "projects/slug-b/session/workflows/wf_same.json",
            _wf_record("/other/workflows/pipeline.js"),
        )
        copied = invoke.collect_workflow_records(self.home, self.pr_dir, {})
        self.assertEqual(set(copied), {"wf_same.json", "wf_same-2.json"})
        dest = self.pr_dir / "workflows"
        self.assertTrue((dest / "wf_same.json").is_file())
        self.assertTrue((dest / "wf_same-2.json").is_file())
        paths = {
            json.loads((dest / name).read_text())["scriptPath"]
            for name in copied
        }
        self.assertEqual(paths, {PIPELINE, "/other/workflows/pipeline.js"})


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

    def test_check_rejects_tier_flag(self):
        with contextlib.redirect_stderr(io.StringIO()) as err:
            rc = run.main(["--check", self.run_dir.name, "--tier", "smoke"])
        self.assertEqual(rc, 2)
        self.assertIn("does not accept", err.getvalue())

    def test_check_naive_is_exit_2(self):
        manifest = json.loads((self.run_dir / "run.json").read_text())
        manifest["anchor"] = "naive"
        _write_json(self.run_dir / "run.json", manifest)
        with contextlib.redirect_stdout(io.StringIO()):
            with contextlib.redirect_stderr(io.StringIO()):
                rc = run.main(["--check", self.run_dir.name])
        self.assertEqual(rc, 2)


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

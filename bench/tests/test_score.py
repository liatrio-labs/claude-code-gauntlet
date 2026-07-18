"""Tests for bench/runner/score.py.

No network, no keys, no ``uv`` subprocess: the models API, the vendored scorer,
and the adjudicator are all injected or monkeypatched. Covers judge-pin
resolution + idempotency, the pin-mismatch/null refusals, the bucket-join
bijection (including its raise paths), metric arithmetic on a hand-built
evaluations fixture, and one fully wired ``score_run`` over a fabricated run dir.
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from bench.runner import score  # noqa: E402
from bench.runner.score import (  # noqa: E402
    bucket_join,
    compute_metrics,
    resolve_judge_pin,
    score_run,
)
from bench.runner.ledger import REQUIRED_KEYS  # noqa: E402


def write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def ev_result(tp, fp, fn, matched):
    """A minimal step3 per-(url,tool) evaluation result."""
    return {
        "skipped": False,
        "true_positives": [{"matched_candidate": t} for t in matched],
        "false_positives": [],
        "false_negatives": [],
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "errors_count": 0,
    }


# --------------------------------------------------------------- judge pin


class ResolveJudgePinTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.baselines = Path(self._tmp.name) / "baselines.json"

    def tearDown(self):
        self._tmp.cleanup()

    def test_idempotent_returns_existing_without_api_call(self):
        write_json(self.baselines, {"judge_pin": "claude-opus-4-8-20260101"})

        def boom(*a, **k):
            raise AssertionError("models API must not be called when pin exists")

        with mock.patch.object(score, "_http_get_json", boom):
            pin = resolve_judge_pin(env={}, baselines_path=self.baselines)
        self.assertEqual(pin, "claude-opus-4-8-20260101")

    def test_force_reresolves_and_writes_newest_dated_snapshot(self):
        write_json(self.baselines, {"judge_pin": "claude-opus-4-8-20250101"})
        models = {
            "data": [
                {"id": "claude-opus-4-8-20260101"},
                {"id": "claude-opus-4-8-20260315"},  # newest
                {"id": "claude-opus-4-8"},            # alias, no date -> ignored
                {"id": "claude-opus-4-8[1m]"},        # variant, no date -> ignored
                {"id": "claude-sonnet-5-20260101"},   # wrong family -> ignored
            ]
        }
        with mock.patch.object(score, "_http_get_json", lambda url, headers: models):
            pin = resolve_judge_pin(
                env={"ANTHROPIC_API_KEY": "k"}, force=True, baselines_path=self.baselines
            )
        self.assertEqual(pin, "claude-opus-4-8-20260315")
        # Persisted back into baselines.json.
        self.assertEqual(json.loads(self.baselines.read_text())["judge_pin"], pin)

    def test_no_key_raises(self):
        write_json(self.baselines, {"judge_pin": None})
        with self.assertRaises(RuntimeError):
            resolve_judge_pin(env={}, baselines_path=self.baselines)

    def test_no_dated_snapshot_raises(self):
        write_json(self.baselines, {"judge_pin": None})
        models = {"data": [{"id": "claude-opus-4-8"}, {"id": "claude-opus-4-8[1m]"}]}
        with mock.patch.object(score, "_http_get_json", lambda url, headers: models):
            with self.assertRaises(RuntimeError):
                resolve_judge_pin(env={"ANTHROPIC_API_KEY": "k"}, baselines_path=self.baselines)

    def test_pick_snapshot_helper_selects_newest(self):
        self.assertEqual(
            score._pick_opus_48_snapshot(
                {"data": [{"id": "claude-opus-4-8-20260101"}, {"id": "claude-opus-4-8-20260901"}]}
            ),
            "claude-opus-4-8-20260901",
        )
        self.assertIsNone(score._pick_opus_48_snapshot({"data": []}))


# ------------------------------------------------------------- bucket join


class BucketJoinTests(unittest.TestCase):
    def test_partitions_matched_and_unmatched(self):
        candidates = {"u": {"deep-review": [
            {"text": "m1"}, {"text": "m2"}, {"text": "extra"},
        ]}}
        evaluations = {"u": {"deep-review": ev_result(2, 1, 0, ["m1", "m2"])}}
        out = bucket_join(candidates, evaluations)
        self.assertEqual(out["u"]["golden_matched"], ["m1", "m2"])
        self.assertEqual(out["u"]["adjudicator"], ["extra"])

    def test_matched_text_not_in_candidates_raises(self):
        candidates = {"u": {"deep-review": [{"text": "a"}, {"text": "b"}]}}
        # TP references a candidate we never submitted -> bijection violation.
        evaluations = {"u": {"deep-review": ev_result(1, 1, 0, ["ghost"])}}
        with self.assertRaises(ValueError) as ctx:
            bucket_join(candidates, evaluations)
        self.assertIn("bijection", str(ctx.exception))

    def test_ambiguous_duplicate_candidate_text_raises(self):
        candidates = {"u": {"deep-review": [{"text": "dup"}, {"text": "dup"}]}}
        evaluations = {"u": {"deep-review": ev_result(1, 1, 0, ["dup"])}}
        with self.assertRaises(ValueError) as ctx:
            bucket_join(candidates, evaluations)
        self.assertIn("ambiguous", str(ctx.exception))

    def test_candidates_without_evaluation_raises(self):
        candidates = {"u": {"deep-review": [{"text": "a"}]}}
        with self.assertRaises(ValueError):
            bucket_join(candidates, {})

    def test_empty_candidates_without_evaluation_is_empty_buckets(self):
        candidates = {"u": {"deep-review": []}}
        out = bucket_join(candidates, {})
        self.assertEqual(out["u"], {"golden_matched": [], "adjudicator": []})


# ---------------------------------------------------------------- metrics


class ComputeMetricsTests(unittest.TestCase):
    def test_two_tp_one_valid_extra_one_noise(self):
        # 4 candidates: m1,m2 matched (2 TP), plus 2 extras (fp=2). One golden
        # unmatched (fn=1). Adjudicator splits the extras 1 valid_extra / 1 noise.
        candidates = {"u": {"deep-review": [
            {"text": "[HIGH] m1"}, {"text": "[MEDIUM] m2"},
            {"text": "[LOW] e1"}, {"text": "[HIGH] e2"},
        ]}}
        evaluations = {"u": {"deep-review": ev_result(2, 2, 1, ["[HIGH] m1", "[MEDIUM] m2"])}}
        buckets = bucket_join(candidates, evaluations)
        adjudications = [
            {"bucket": "valid_extra", "candidate": "[LOW] e1"},
            {"bucket": "noise", "candidate": "[HIGH] e2"},
        ]
        m = compute_metrics(evaluations, candidates, buckets, adjudications)

        self.assertAlmostEqual(m["golden_recall"], 2 / 3)
        self.assertAlmostEqual(m["precision_strict"], 0.5)
        self.assertAlmostEqual(m["valid_extra_rate"], 0.25)
        self.assertAlmostEqual(m["noise_rate"], 0.25)
        self.assertAlmostEqual(m["f1_strict"], 2 * 0.5 * (2 / 3) / (0.5 + 2 / 3))
        self.assertEqual(m["per_bucket"], {"golden_matched": 2, "valid_extra": 1, "noise": 1})
        self.assertEqual(m["total_candidates"], 4)
        self.assertEqual(m["n_prs"], 1)
        # Buckets partition the tool's total candidates.
        self.assertEqual(
            m["per_bucket"]["golden_matched"]
            + m["per_bucket"]["valid_extra"]
            + m["per_bucket"]["noise"],
            m["total_candidates"],
        )

    def test_severity_dist_and_desc_len(self):
        candidates = {"u": {"deep-review": [
            {"text": "[HIGH] aaaa"}, {"text": "[HIGH] bbbb"}, {"text": "no tag"},
        ]}}
        evaluations = {"u": {"deep-review": ev_result(0, 3, 0, [])}}
        buckets = bucket_join(candidates, evaluations)
        adjudications = [{"bucket": "noise", "candidate": c["text"]}
                         for c in candidates["u"]["deep-review"]]
        m = compute_metrics(evaluations, candidates, buckets, adjudications)
        self.assertEqual(m["drift"]["severity_dist"], {"HIGH": 2, "UNKNOWN": 1})
        self.assertAlmostEqual(m["drift"]["desc_len_mean"], (11 + 11 + 6) / 3)


# --------------------------------------------------------------- score_run


class ScoreRunRefusalTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.run_dir = self.tmp / "run"
        self.run_dir.mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    def test_null_pin_refuses(self):
        baselines = self.tmp / "baselines.json"
        write_json(baselines, {"judge_pin": None})
        with self.assertRaises(RuntimeError) as ctx:
            score_run(self.run_dir, env={}, baselines_path=baselines)
        self.assertIn("judge_pin", str(ctx.exception))

    def test_martian_model_mismatch_refuses(self):
        baselines = self.tmp / "baselines.json"
        write_json(baselines, {"judge_pin": "claude-opus-4-8-20260101"})
        with self.assertRaises(RuntimeError) as ctx:
            score_run(
                self.run_dir,
                env={"MARTIAN_MODEL": "claude-opus-4-8-20259999"},
                baselines_path=baselines,
            )
        self.assertIn("MARTIAN_MODEL", str(ctx.exception))

    def test_no_ok_prs_raises_actionable(self):
        # A run whose only PR failed -> zero candidates -> the scorer would write no
        # evaluations.json (FileNotFoundError downstream). score_run must refuse first.
        state = self.run_dir / "state"
        state.mkdir()
        write_json(state / "a.json",
                   {"url": "https://github.com/o/r/pull/1", "status": "failed"})
        baselines = self.tmp / "baselines.json"
        write_json(baselines, {"judge_pin": "claude-opus-4-8-20260101"})
        with self.assertRaises(RuntimeError) as ctx:
            score_run(self.run_dir, env={}, baselines_path=baselines)
        self.assertIn("no scorable PRs", str(ctx.exception))


# ----------------------------------------------------- scorer-stage failure surface


class ScorerStageFailureTests(unittest.TestCase):
    def test_stage_nonzero_exit_raises_runtimeerror_naming_stage(self):
        pin = "claude-opus-4-8-20260101"
        fake = SimpleNamespace(returncode=1, stdout="", stderr="Traceback: boom in dedup")
        with mock.patch.object(score.subprocess, "run", return_value=fake):
            with self.assertRaises(RuntimeError) as ctx:
                score._run_scorer_stages(pin, "api-key", Path("/tmp/model-dir"))
        msg = str(ctx.exception)
        self.assertIn("dedup", msg)  # the failing stage is named
        self.assertIn("boom", msg)   # stderr tail is surfaced


# ------------------------------------------------------------- _read_run_costs


class ReadRunCostsTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_reads_raw_naive_envelope(self):
        # A naive-anchor PR dir carries only raw-naive.json (never raw.json).
        pr = self.tmp / "run" / "pr-9"
        pr.mkdir(parents=True)
        write_json(pr / "raw-naive.json", {
            "type": "result",
            "total_cost_usd": 0.7,
            "usage": {"input_tokens": 10, "output_tokens": 5},
        })
        costs = score._read_run_costs(self.tmp / "run")
        self.assertAlmostEqual(costs["cost_usd"], 0.7)
        self.assertEqual(costs["tokens_total"], 15)


# ------------------------------------------------------------- _assemble_candidates


class AssembleCandidatesTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_finds_nested_payload(self):
        # The flat post-review-payload.json is absent; a nested one under output/
        # must still be discovered (mirrors invoke._find_payload's rglob fallback).
        run_dir = self.tmp / "run"
        url = "https://github.com/o/r/pull/12"
        nested = run_dir / "pr-12" / "output"
        nested.mkdir(parents=True)
        write_json(nested / "post-review-payload.json", {
            "platform": "github",
            "payload": {"comments": [{"body": "c1", "path": "f.py", "line": 3}]},
            "skipped": [],
        })
        candidates, per_pr = score._assemble_candidates(run_dir, [(url, "ok")])
        self.assertIn(url, candidates)
        self.assertEqual(len(candidates[url]["deep-review"]), 1)
        self.assertEqual(per_pr[url]["candidates"][0]["text"], "c1")


class ToolLabelTests(unittest.TestCase):
    """The ledger row's tool label is derived from the run manifest's anchor field."""

    def test_naive_and_default_labels(self):
        self.assertEqual(score._tool_label({"anchor": "naive"}), "naive-anchor")
        self.assertEqual(score._tool_label({"anchor": None}), "deep-review-v2")
        self.assertEqual(score._tool_label({}), "deep-review-v2")

    def test_explicit_tool_overrides_anchor(self):
        self.assertEqual(
            score._tool_label({"tool": "deep-review-v3", "anchor": "naive"}),
            "deep-review-v3",
        )

    def test_label_read_from_run_json(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        run_dir = Path(tmp.name)

        write_json(run_dir / "run.json", {"run_id": "smoke-x", "anchor": "naive"})
        self.assertEqual(
            score._tool_label(score._read_run_manifest(run_dir)), "naive-anchor"
        )

        write_json(run_dir / "run.json", {"run_id": "smoke-x"})  # no anchor
        self.assertEqual(
            score._tool_label(score._read_run_manifest(run_dir)), "deep-review-v2"
        )


class ScoreRunEndToEndTests(unittest.TestCase):
    """One fully wired run: scorer + adjudicator injected, no network."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.pin = "claude-opus-4-8-20260101"

        # Two scored PRs + one failed PR (must be excluded from scoring).
        self.uA = "https://github.com/keycloak/keycloak/pull/101"
        self.uB = "https://github.com/grafana/grafana/pull/202"
        self.uC = "https://github.com/discourse/discourse/pull/303"

        self.run_dir = self.tmp / "subset-20260717T000000Z-abc1234"
        self.run_dir.mkdir()

        # checkpoint state.
        state = self.run_dir / "state"
        state.mkdir()
        write_json(state / "a.json", {"url": self.uA, "status": "ok"})
        write_json(state / "b.json", {"url": self.uB, "status": "ok"})
        write_json(state / "c.json", {"url": self.uC, "status": "failed"})

        # PR A: 3 comments (2 will match goldens, 1 is an extra).
        self._pr(101, [
            ("cA1 body", "src/a.py", 11),
            ("cA2 body", "src/a.py", 12),
            ("cA3 body", "src/a.py", 13),
        ], cost=0.5, tokens={"input_tokens": 100, "output_tokens": 50})
        # PR B: 1 comment, unmatched (an extra to adjudicate).
        self._pr(202, [
            ("cB1 body", "src/b.py", 5),
        ], cost=0.5, tokens={"input_tokens": 60, "output_tokens": 40})

        # golden data the scorer would read (2 goldens per PR).
        self.golden = self.tmp / "golden.json"
        write_json(self.golden, {
            self.uA: {"source_repo": "keycloak", "golden_comments": [
                {"comment": "gA1", "severity": "High"},
                {"comment": "gA2", "severity": "Medium"},
            ], "reviews": []},
            self.uB: {"source_repo": "grafana", "golden_comments": [
                {"comment": "gB1", "severity": "High"},
                {"comment": "gB2", "severity": "Low"},
            ], "reviews": []},
            self.uC: {"source_repo": "discourse", "golden_comments": [
                {"comment": "gC1", "severity": "Low"},
            ], "reviews": []},
        })

        self.baselines = self.tmp / "baselines.json"
        write_json(self.baselines, {
            "judge_pin": self.pin,
            "adjudicator_pin": self.pin,
            "scorer_sha": "dfc6cb42",
        })
        self.ledger = self.tmp / "experiments.jsonl"

        self.vendor = self.tmp / "vendor"
        (self.vendor / "results").mkdir(parents=True)

    def tearDown(self):
        self._tmp.cleanup()

    def _pr(self, number, comments, cost, tokens):
        pr_dir = self.run_dir / "pr-{}".format(number)
        pr_dir.mkdir()
        payload = {
            "platform": "github",
            "payload": {"comments": [
                {"body": body, "path": path, "line": line}
                for body, path, line in comments
            ]},
            "skipped": [],
        }
        write_json(pr_dir / "post-review-payload.json", payload)
        # a diff touching each commented path/line so slice_hunk succeeds.
        paths = {}
        for _body, path, line in comments:
            paths.setdefault(path, []).append(line)
        diff = []
        for path, lines in paths.items():
            start = min(lines)
            span = max(lines) - start + 1 + 2
            diff.append("diff --git a/{p} b/{p}\n--- a/{p}\n+++ b/{p}\n".format(p=path))
            diff.append("@@ -{s},{c} +{s},{c} @@\n".format(s=start, c=span))
            for ln in range(start, start + span):
                diff.append("+code line {}\n".format(ln))
        (pr_dir / "diff.patch").write_text("".join(diff), encoding="utf-8")
        # cost/token envelope the runner would have written.
        write_json(pr_dir / "raw.json", {
            "type": "result",
            "total_cost_usd": cost,
            "usage": tokens,
        })

    def _fake_scorer(self, model_dir):
        """Return a run_scorer that writes an evaluations.json into model_dir."""
        def run_scorer(pin, api_key, md):
            self.assertEqual(pin, self.pin)
            self.assertEqual(str(md), str(model_dir))
            evaluations = {
                self.uA: {"deep-review": ev_result(2, 1, 0, ["cA1 body", "cA2 body"])},
                self.uB: {"deep-review": ev_result(0, 1, 2, [])},
            }
            write_json(model_dir / "evaluations.json", evaluations)
        return run_scorer

    def _fake_adjudicator(self, seen):
        def adjudicator(comment_text, diff_hunk, file_ctx, pin, api_key):
            seen.append({"text": comment_text, "hunk": diff_hunk, "pin": pin})
            bucket = "valid_extra" if comment_text == "cA3 body" else "noise"
            return {"bucket": bucket, "failed_check": None, "reason": "test"}
        return adjudicator

    def test_full_pipeline(self):
        sanitized = self.pin.replace("/", "_")
        model_dir = self.vendor / "results" / sanitized
        seen = []

        with mock.patch.object(score, "VENDOR_DIR", self.vendor), \
             mock.patch.object(score, "GOLDEN_DATA", self.golden):
            scores = score_run(
                self.run_dir,
                env={},
                baselines_path=self.baselines,
                ledger_path=self.ledger,
                run_scorer=self._fake_scorer(model_dir),
                adjudicator=self._fake_adjudicator(seen),
            )

        m = scores["metrics"]
        # tp=2, fp=2, fn=2 across the two scored PRs.
        self.assertAlmostEqual(m["golden_recall"], 0.5)
        self.assertAlmostEqual(m["precision_strict"], 0.5)
        self.assertAlmostEqual(m["valid_extra_rate"], 0.25)  # cA3 -> valid_extra, /4
        self.assertAlmostEqual(m["noise_rate"], 0.25)         # cB1 -> noise, /4
        self.assertEqual(m["per_bucket"], {"golden_matched": 2, "valid_extra": 1, "noise": 1})
        self.assertEqual(m["n_prs"], 2)

        # Adjudicator saw exactly the two unmatched comments, with real hunks.
        adj_texts = sorted(s["text"] for s in seen)
        self.assertEqual(adj_texts, ["cA3 body", "cB1 body"])
        for s in seen:
            self.assertTrue(s["hunk"].startswith("@@ "))
            self.assertEqual(s["pin"], self.pin)

        # Costs summed from per-PR raw.json envelopes.
        row = scores["ledger_row"]
        self.assertAlmostEqual(row["cost_usd"], 1.0)
        self.assertEqual(row["tokens_total"], 250)  # 150 + 100
        self.assertEqual(row["tier"], "subset")
        self.assertEqual(row["tool"], "deep-review-v2")  # no anchor manifest -> v2 label
        self.assertEqual(row["judge_pin"], self.pin)
        self.assertEqual(row["scorer_sha"], "dfc6cb42")
        self.assertEqual(row["envelope"]["cap"], 25)

        # Ledger row carries every required key and was appended.
        for key in REQUIRED_KEYS:
            self.assertIn(key, row)
        appended = [json.loads(line) for line in self.ledger.read_text().splitlines() if line.strip()]
        self.assertEqual(len(appended), 1)
        self.assertEqual(appended[0]["run_id"], row["run_id"])

        # scores.json written next to the run.
        self.assertTrue((self.run_dir / "scores.json").is_file())

        # The failed PR (uC) was excluded from scoring.
        self.assertNotIn(self.uC, scores["buckets"])

        # candidates.json + injected benchmark_data.json staged for the scorer.
        staged = json.loads((model_dir / "candidates.json").read_text())
        self.assertEqual(set(staged), {self.uA, self.uB})
        bench_data = json.loads((self.vendor / "results" / "benchmark_data.json").read_text())
        self.assertEqual(
            bench_data[self.uA]["reviews"][0]["tool"], "deep-review"
        )
        self.assertEqual(bench_data[self.uC]["reviews"], [])  # not scored -> no stub


class PrepareScorerInputsStaleTests(unittest.TestCase):
    def test_stale_stage_outputs_removed_before_run(self):
        import shutil as _shutil

        tmp = Path(tempfile.mkdtemp(prefix="bench-score-stale-"))
        self.addCleanup(_shutil.rmtree, tmp, ignore_errors=True)
        results = tmp / "results"
        model = results / "pin"
        model.mkdir(parents=True)
        (model / "evaluations.json").write_text('{"stale": true}')
        (model / "dedup_groups.json").write_text('{"stale": true}')
        url = "https://github.com/grafana/grafana/pull/80329"
        score._prepare_scorer_inputs({url: {"deep-review": []}}, results, model)
        self.assertFalse((model / "evaluations.json").exists())
        self.assertFalse((model / "dedup_groups.json").exists())
        self.assertTrue((model / "candidates.json").exists())


if __name__ == "__main__":
    unittest.main()

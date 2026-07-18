"""Tests for the anchor candidates file and bench/runner/anchors.py.

Offline and keyless: the vendored anchor data is validated as committed; the
``spot_check`` scorer invocation and ``rejudge_anchors`` machinery run entirely
through injected fakes (no ``uv`` subprocess, no network, no keys). The real
plumbing spot-check that makes API spend lives outside the test suite.
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

from bench.runner import anchors, score  # noqa: E402
from bench.runner.anchors import (  # noqa: E402
    ANCHOR_TOOLS,
    rejudge_anchors,
    spot_check,
)

ANCHORS_FILE = REPO_ROOT / "bench" / "golden" / "anchors" / "candidates.json"
SUBSETS_FILE = REPO_ROOT / "bench" / "golden" / "subsets.json"


def write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


# ------------------------------------------------ Step 1: the committed data


class AnchorCandidatesFileTests(unittest.TestCase):
    """Validate bench/golden/anchors/candidates.json as committed (offline)."""

    @classmethod
    def setUpClass(cls):
        cls.anchors = json.loads(ANCHORS_FILE.read_text())
        subs = json.loads(SUBSETS_FILE.read_text())
        cls.subsets = subs
        cls.subset_urls = set(subs["gate"]) | set(subs["holdout"]) | set(subs["smoke"])

    def test_file_loads_and_is_nonempty(self):
        self.assertIsInstance(self.anchors, dict)
        self.assertTrue(self.anchors)

    def test_only_three_anchor_tools_present(self):
        seen = set()
        for tools in self.anchors.values():
            seen.update(tools)
        self.assertEqual(seen, set(ANCHOR_TOOLS))

    def test_only_subset_urls_present(self):
        extra = set(self.anchors) - self.subset_urls
        self.assertEqual(extra, set(), "non-subset URLs leaked into the anchor file")

    def test_every_gate_url_present_with_every_anchor_tool(self):
        # Upstream committed candidates for all three anchors on every gate PR;
        # the filtered file must preserve that (recall signal per gate PR).
        for url in self.subsets["gate"]:
            self.assertIn(url, self.anchors, "gate URL missing: {}".format(url))
            for tool in ANCHOR_TOOLS:
                self.assertIn(
                    tool, self.anchors[url],
                    "gate URL {} missing anchor tool {}".format(url, tool),
                )

    def test_candidate_entries_have_scorer_shape(self):
        # step3's get_candidates reads c["text"]; provenance is source="extracted".
        for url, tools in self.anchors.items():
            for tool, cands in tools.items():
                self.assertIsInstance(cands, list)
                for c in cands:
                    self.assertIn("text", c)
                    self.assertIsInstance(c["text"], str)
                    self.assertIn("source", c)

    def test_no_duplicate_candidate_text_within_a_tool(self):
        # bucket_join requires unambiguous candidate texts per (url, tool).
        for url, tools in self.anchors.items():
            for tool, cands in tools.items():
                texts = [c["text"] for c in cands]
                self.assertEqual(
                    len(texts), len(set(texts)),
                    "duplicate candidate text in {} / {}".format(url, tool),
                )


# ---------------------------------------------- Step 2: spot_check (mocked)


class SpotCheckTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.url = "https://github.com/o/r/pull/1"
        self.judge = "claude-opus-4-5-20251101"

        # anchor candidates fixture: claude (2), coderabbit (3).
        self.anchors_path = self.tmp / "anchors.json"
        write_json(self.anchors_path, {
            self.url: {
                "claude": [
                    {"text": "c-match", "path": None, "line": None, "source": "extracted"},
                    {"text": "c-extra", "path": None, "line": None, "source": "extracted"},
                ],
                "coderabbit": [
                    {"text": "r-match", "path": None, "line": None, "source": "extracted"},
                    {"text": "r-x1", "path": None, "line": None, "source": "extracted"},
                    {"text": "r-x2", "path": None, "line": None, "source": "extracted"},
                ],
            }
        })
        # golden fixture (3 goldens) so n_golden and staging resolve.
        self.golden_path = self.tmp / "golden.json"
        write_json(self.golden_path, {
            self.url: {
                "source_repo": "r",
                "golden_comments": [
                    {"comment": "g1", "severity": "High"},
                    {"comment": "g2", "severity": "Low"},
                    {"comment": "g3", "severity": "Medium"},
                ],
                "reviews": [],
            }
        })
        self.results_dir = self.tmp / "results"
        self.diff_out = self.tmp / "spot-check-diff.json"

    def tearDown(self):
        self._tmp.cleanup()

    def _fake_scorer(self, our_eval):
        model_dir = self.results_dir / self.judge.replace("/", "_")

        def run_scorer(model, key, base_url, dedup_rel, env=None):
            self.assertEqual(model, self.judge)
            model_dir.mkdir(parents=True, exist_ok=True)
            write_json(model_dir / "evaluations.json", our_eval)
        return run_scorer

    def _ev(self, tp, fp, fn, matched, fps):
        return {
            "tp": tp, "fp": fp, "fn": fn,
            "true_positives": [{"matched_candidate": t} for t in matched],
            "false_positives": [{"candidate": t} for t in fps],
        }

    def _upstream(self, path, claude, coderabbit):
        write_json(path, {self.url: {"claude": claude, "coderabbit": coderabbit}})

    def _run(self, our_eval, upstream_eval):
        up_path = self.tmp / "upstream_eval.json"
        write_json(up_path, upstream_eval)
        with mock.patch.object(anchors, "GOLDEN_DATA", self.golden_path):
            return spot_check(
                self.url,
                judge_model=self.judge,
                api_key="test-key",
                anchors_path=self.anchors_path,
                upstream_eval_path=up_path,
                results_dir=self.results_dir,
                run_scorer=self._fake_scorer(our_eval),
                diff_out=self.diff_out,
            )

    def test_exact_and_within_tolerance_passes(self):
        our = {self.url: {
            "claude": self._ev(1, 1, 2, ["c-match"], ["c-extra"]),
            "coderabbit": self._ev(2, 2, 1, ["r-match", "r-x1"], ["r-x2"]),
        }}
        upstream = {self.url: {
            "claude": self._ev(1, 1, 2, ["c-match"], ["c-extra"]),   # exact
            "coderabbit": self._ev(2, 1, 1, ["r-match", "r-x1"], ["r-x2"]),  # fp +1
        }}
        report = self._run(our, upstream)
        self.assertTrue(report["pass"])
        self.assertTrue(report["per_tool"]["claude"]["within_tolerance"])
        self.assertTrue(report["per_tool"]["coderabbit"]["within_tolerance"])
        self.assertEqual(report["per_tool"]["coderabbit"]["deltas"]["fp"], 1)
        # diff file always written.
        self.assertTrue(self.diff_out.is_file())
        self.assertEqual(json.loads(self.diff_out.read_text())["pr_url"], self.url)

    def test_drift_beyond_tolerance_fails_and_captures_diff(self):
        our = {self.url: {
            "claude": self._ev(1, 1, 2, ["c-match"], ["c-extra"]),
            "coderabbit": self._ev(2, 4, 1, ["r-match", "r-x1"], ["r-x2"]),  # fp +3
        }}
        upstream = {self.url: {
            "claude": self._ev(1, 1, 2, ["c-match"], ["c-extra"]),
            "coderabbit": self._ev(2, 1, 1, ["r-match", "r-x1"], ["r-x2"]),
        }}
        report = self._run(our, upstream)
        self.assertFalse(report["pass"])
        self.assertTrue(report["per_tool"]["claude"]["within_tolerance"])
        self.assertFalse(report["per_tool"]["coderabbit"]["within_tolerance"])
        self.assertEqual(report["per_tool"]["coderabbit"]["deltas"]["fp"], 3)
        # full per-comment comparison persisted for verbatim inspection.
        saved = json.loads(self.diff_out.read_text())
        self.assertIn("fp_ours", saved["per_tool"]["coderabbit"])
        self.assertIn("matched_upstream", saved["per_tool"]["coderabbit"])

    def test_unknown_pr_url_raises(self):
        with self.assertRaises(ValueError):
            spot_check(
                "https://github.com/o/r/pull/999",
                api_key="k",
                anchors_path=self.anchors_path,
                run_scorer=lambda *a, **k: None,
            )

    def test_spend_estimate_counts_calls(self):
        our = {self.url: {
            "claude": self._ev(0, 2, 3, [], ["c-match", "c-extra"]),
            "coderabbit": self._ev(0, 3, 3, [], ["r-match", "r-x1", "r-x2"]),
        }}
        report = self._run(our, our)
        # judge calls = golden(3) * (claude 2 + coderabbit 3) = 15; both tools >=2
        # candidates -> 2 dedup calls.
        self.assertEqual(report["spend_estimate"]["judge_calls"], 15)
        self.assertEqual(report["spend_estimate"]["dedup_calls"], 2)


# ------------------------------------------- Step 2: rejudge_anchors (mocked)


class RejudgeAnchorsTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.u1 = "https://github.com/o/r/pull/1"
        self.pin = "claude-opus-4-8-20260101"
        self.tools = ["claude", "coderabbit"]

        self.anchors_path = self.tmp / "anchors.json"
        write_json(self.anchors_path, {
            self.u1: {
                "claude": [
                    {"text": "c-match", "path": None, "line": None, "source": "extracted"},
                    {"text": "c-extra", "path": None, "line": None, "source": "extracted"},
                ],
                "coderabbit": [
                    {"text": "r-match", "path": None, "line": None, "source": "extracted"},
                    {"text": "r-x1", "path": None, "line": None, "source": "extracted"},
                    {"text": "r-x2", "path": None, "line": None, "source": "extracted"},
                ],
            }
        })
        self.subsets_path = self.tmp / "subsets.json"
        write_json(self.subsets_path, {"gate": [self.u1], "holdout": [], "smoke": []})
        self.golden_path = self.tmp / "golden.json"
        write_json(self.golden_path, {
            self.u1: {
                "source_repo": "r",
                "golden_comments": [
                    {"comment": "g1", "severity": "High"},
                    {"comment": "g2", "severity": "Low"},
                ],
                "reviews": [],
            }
        })
        self.results_dir = self.tmp / "results"
        self.cache_dir = self.tmp / "cache"

    def tearDown(self):
        self._tmp.cleanup()

    def _evaluations(self):
        def ev(tp, fp, fn, matched, fps):
            return {
                "tp": tp, "fp": fp, "fn": fn,
                "true_positives": [{"matched_candidate": t} for t in matched],
                "false_positives": [{"candidate": t} for t in fps],
            }
        return {self.u1: {
            "claude": ev(1, 1, 1, ["c-match"], ["c-extra"]),
            "coderabbit": ev(1, 2, 1, ["r-match"], ["r-x1", "r-x2"]),
        }}

    def _scorer(self, counter, model_pin=None):
        pin = model_pin or self.pin
        model_dir = self.results_dir / pin.replace("/", "_")

        def run_scorer(model, key, base_url, dedup_rel, env=None):
            counter.append(model)
            model_dir.mkdir(parents=True, exist_ok=True)
            write_json(model_dir / "evaluations.json", self._evaluations())
        return run_scorer

    def _adjudicator(self):
        def adj(text, hunk, ctx, pin, api_key):
            bucket = "valid_extra" if text == "c-extra" else "noise"
            return {"bucket": bucket, "failed_check": None, "reason": "test"}
        return adj

    def _call(self, counter, pin=None):
        pin = pin or self.pin
        with mock.patch.object(anchors, "GOLDEN_DATA", self.golden_path):
            return rejudge_anchors(
                pin,
                tools=self.tools,
                subset="gate",
                anchors_path=self.anchors_path,
                subsets_path=self.subsets_path,
                cache_dir=self.cache_dir,
                api_key="k",
                results_dir=self.results_dir,
                run_scorer=self._scorer(counter, pin),
                adjudicator=self._adjudicator(),
            )

    def test_per_tool_metrics_computed(self):
        counter = []
        result = self._call(counter)
        self.assertEqual(counter, [self.pin])
        self.assertFalse(result["cache_hit"])

        claude = result["per_tool"]["claude"]
        self.assertAlmostEqual(claude["recall"], 0.5)          # tp1/(tp1+fn1)
        self.assertAlmostEqual(claude["precision_strict"], 0.5)  # tp1/(tp1+fp1)
        self.assertAlmostEqual(claude["valid_extra_rate"], 0.5)  # c-extra valid / 2
        self.assertAlmostEqual(claude["noise_rate"], 0.0)
        self.assertEqual(claude["per_bucket"],
                         {"golden_matched": 1, "valid_extra": 1, "noise": 0})

        rabbit = result["per_tool"]["coderabbit"]
        self.assertAlmostEqual(rabbit["recall"], 0.5)
        self.assertAlmostEqual(rabbit["precision_strict"], 1 / 3)
        self.assertAlmostEqual(rabbit["noise_rate"], 2 / 3)     # r-x1,r-x2 noise / 3
        self.assertEqual(rabbit["per_bucket"],
                         {"golden_matched": 1, "valid_extra": 0, "noise": 2})

    def test_cache_hit_skips_scorer(self):
        counter = []
        first = self._call(counter)
        second = self._call(counter)
        # scorer ran exactly once; the second call was served from cache.
        self.assertEqual(counter, [self.pin])
        self.assertFalse(first["cache_hit"])
        self.assertTrue(second["cache_hit"])
        self.assertEqual(first["per_tool"], second["per_tool"])

    def test_cache_key_changes_with_pin(self):
        counter = []
        self._call(counter, pin=self.pin)
        other = "claude-opus-4-8-20260202"
        self._call(counter, pin=other)
        # a different pin is a different cache key -> scorer runs again.
        self.assertEqual(counter, [self.pin, other])
        files = sorted(p.name for p in self.cache_dir.glob("*.json"))
        self.assertEqual(len(files), 2)

    def test_unknown_subset_raises(self):
        with self.assertRaises(ValueError):
            rejudge_anchors(
                self.pin,
                tools=self.tools,
                subset="nope",
                anchors_path=self.anchors_path,
                subsets_path=self.subsets_path,
                cache_dir=self.cache_dir,
                api_key="k",
                run_scorer=lambda *a, **k: None,
                adjudicator=self._adjudicator(),
            )


class AnchorScorerStageFailureTests(unittest.TestCase):
    """The anchor path delegates to score.run_scorer_stages, so a non-zero scorer
    stage now surfaces a stage-named RuntimeError (score behavior) instead of the bare
    CalledProcessError the old duplicated runner raised."""

    def test_stage_failure_surfaces_stage_named_runtimeerror(self):
        fake = SimpleNamespace(returncode=1, stdout="", stderr="Traceback: boom in dedup")
        with mock.patch.object(score.subprocess, "run", return_value=fake):
            with self.assertRaises(RuntimeError) as ctx:
                anchors._run_scorer_stages(
                    "claude-opus-4-8-20260101",
                    "api-key",
                    anchors.MARTIAN_BASE_URL,
                    "results/claude-opus-4-8-20260101/dedup_groups.json",
                )
        msg = str(ctx.exception)
        self.assertIn("dedup", msg)  # the failing stage is named
        self.assertIn("boom", msg)   # stderr tail is surfaced


class AdjudicatorContextTests(unittest.TestCase):
    """Anchor comments (null path/line) get the whole capped PR diff as context."""

    def test_capped_diff_truncates_with_marker(self):
        short = "x" * 100
        self.assertEqual(anchors._capped_diff(short), short)
        long = "y" * (anchors._MAX_DIFF_CHARS + 500)
        out = anchors._capped_diff(long)
        self.assertTrue(out.startswith("y" * anchors._MAX_DIFF_CHARS))
        self.assertIn("truncated", out)

    def test_null_path_line_comment_gets_full_diff(self):
        buckets = {"u": {"golden_matched": [], "adjudicator": ["c-extra"]}}
        candidates = {"u": {"claude": [{"text": "c-extra", "path": None, "line": None}]}}
        diffs = {"u": "diff --git a/f b/f\n@@ -1 +1 @@\n+code"}
        seen = []

        def adj(text, hunk, ctx, pin, key):
            seen.append({"hunk": hunk, "ctx": ctx})
            return {"bucket": "valid_extra", "failed_check": None, "reason": "t"}

        anchors._adjudicate_anchor_bucket(buckets, candidates, "claude", "pin", "k", adj, diffs)
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0]["hunk"], diffs["u"])  # whole diff, not a slice
        self.assertEqual(seen[0]["ctx"], "")

    def test_located_candidate_gets_sliced_hunk(self):
        buckets = {"u": {"golden_matched": [], "adjudicator": ["c"]}}
        candidates = {"u": {"claude": [{"text": "c", "path": "f.py", "line": 2}]}}
        diff = ("diff --git a/f.py b/f.py\n--- a/f.py\n+++ b/f.py\n"
                "@@ -1,3 +1,3 @@\n line1\n+line2\n line3\n")
        seen = []

        def adj(text, hunk, ctx, pin, key):
            seen.append(hunk)
            return {"bucket": "noise", "failed_check": 1, "reason": "t"}

        anchors._adjudicate_anchor_bucket(buckets, candidates, "claude", "pin", "k", adj, {"u": diff})
        self.assertTrue(seen[0].startswith("@@ "))  # sliced hunk, not whole diff


if __name__ == "__main__":
    unittest.main()

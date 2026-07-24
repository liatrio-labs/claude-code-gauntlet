"""Tests for bench/report.py — the regenerable performance dashboard.

Offline: no network, no keys. A small synthetic ledger + minimal baselines dict
exercise the grouping/collapse, tile derivation, anchor bars, and self-contained
HTML invariants; a final pair of tests runs the generator against the real
committed data (bench/experiments.jsonl + bench/baselines.json).
"""

import json
import re
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from bench import report  # noqa: E402

BENCH_DIR = REPO_ROOT / "bench"
REAL_LEDGER = BENCH_DIR / "experiments.jsonl"
REAL_BASELINES = BENCH_DIR / "baselines.json"

# A subset run scored twice with an identical signature (the k=5 collapse case),
# plus one anchor row and one naive-anchor row.
FIXTURE_ROWS = [
    {
        "run_id": "subset-20260718-aaaaaaa", "ts": "2026-07-18T09:00:00Z",
        "tier": "subset", "tool": "deep-review-v2", "n_prs": 15,
        "golden_recall": 0.4915254237288136, "valid_extra_rate": 0.36363636363636365,
        "noise_rate": 0.16363636363636364, "precision_strict": 0.5272727272727272,
        "f1_strict": 0.5087719298245613,
        "per_bucket": {"golden_matched": 26, "valid_extra": 20, "noise": 9},
        "tokens_total": 41060587, "cost_usd": 190.71924090000007,
    },
    {
        "run_id": "subset-20260718-aaaaaaa", "ts": "2026-07-18T09:03:00Z",
        "tier": "subset", "tool": "deep-review-v2", "n_prs": 15,
        "golden_recall": 0.4915254237288136, "valid_extra_rate": 0.36363636363636365,
        "noise_rate": 0.16363636363636364, "precision_strict": 0.5272727272727272,
        "f1_strict": 0.5087719298245613,
        "per_bucket": {"golden_matched": 26, "valid_extra": 20, "noise": 9},
        "tokens_total": 41060587, "cost_usd": 190.71924090000007,
    },
    {
        "run_id": "anchors-gate-bbbbbbb", "ts": "2026-07-17T23:16:34Z",
        "tier": "subset", "tool": "anchor-claude", "n_prs": 15,
        "golden_recall": 0.3389830508474576, "valid_extra_rate": 0.14814814814814814,
        "noise_rate": 0.48148148148148145, "precision_strict": 0.39215686274509803,
        "tokens_total": 298304, "cost_usd": 1.8055,
    },
    {
        "run_id": "smoke-20260718-ccccccc", "ts": "2026-07-18T03:17:00Z",
        "tier": "smoke", "tool": "naive-anchor", "n_prs": 3,
        "golden_recall": 0.75, "valid_extra_rate": 0.5714285714285714,
        "noise_rate": 0.0, "precision_strict": 0.42857142857142855,
        "f1_strict": 0.5454545454545454,
        "per_bucket": {"golden_matched": 3, "valid_extra": 4, "noise": 0},
        "tokens_total": 527876, "cost_usd": 1.4069320000000003,
    },
]

FIXTURE_BASELINES = {
    "judge_pin": "claude-opus-4-5-20251101",
    "adjudicator_pin": "claude-opus-4-5-20251101",
    "anchors": {
        "rows": {
            "claude": {
                "recall": 0.3389830508474576, "noise_rate": 0.48148148148148145,
                "valid_extra_rate": 0.14814814814814814,
                "precision_strict": 0.39215686274509803,
            },
            "claude-code": {
                "recall": 0.2711864406779661, "noise_rate": 0.5416666666666666,
                "valid_extra_rate": 0.125, "precision_strict": 0.3333333333333333,
            },
            "coderabbit": {
                "recall": 0.6271186440677966, "noise_rate": 0.5658914728682171,
                "valid_extra_rate": 0.14728682170542637,
                "precision_strict": 0.29838709677419356,
            },
        }
    },
    "delta_noise_proposed": {"value": 0.24, "proposed": "2026-07-18"},
    "baseline_v2": {
        "run_id": "subset-20260718-aaaaaaa", "n_prs": 15, "n_goldens": 59, "runs": 1,
        "golden_recall": 0.4915, "valid_extra_rate": 0.3636, "noise_rate": 0.1636,
        "precision_strict": 0.5273, "f1_strict": 0.5088,
    },
}


def _render(rows=None, baselines=None):
    return report.render_html(
        rows if rows is not None else FIXTURE_ROWS,
        baselines if baselines is not None else FIXTURE_BASELINES,
        "abc1234",
        "2026-07-18",
    )


class TestGrouping(unittest.TestCase):
    def test_kfive_collapse_single_point(self):
        points = report.deep_review_points(FIXTURE_ROWS)
        self.assertEqual(len(points), 1)
        self.assertEqual(points[0]["count"], 2)
        self.assertTrue(points[0]["identical"])

    def test_latest_subset_row_selected(self):
        row = report.latest_subset_row(FIXTURE_ROWS)
        self.assertEqual(row["tool"], "deep-review-v2")
        self.assertEqual(row["tier"], "subset")
        # The later of the two identical re-scores wins the max-by-ts.
        self.assertEqual(row["ts"], "2026-07-18T09:03:00Z")

    def test_ledger_groups_collapse_and_order(self):
        groups = report.ledger_groups(FIXTURE_ROWS)
        # anchor + naive + collapsed v2 == 3 table rows, oldest first.
        self.assertEqual(len(groups), 3)
        self.assertEqual(groups[0]["row"]["tool"], "anchor-claude")
        v2 = [g for g in groups if g["row"]["tool"] == "deep-review-v2"][0]
        self.assertEqual(v2["count"], 2)
        self.assertTrue(v2["identical"])


class TestFormatting(unittest.TestCase):
    def test_fmt_pct(self):
        self.assertEqual(report.fmt_pct(0.4915254237288136), "49.2%")
        self.assertEqual(report.fmt_pct(0.16363636363636364), "16.4%")
        self.assertEqual(report.fmt_pct(None), "—")

    def test_fmt_money_and_int(self):
        self.assertEqual(report.fmt_money(190.71924090000007), "$190.72")
        self.assertEqual(report.fmt_int(41060587), "41,060,587")

    def test_fmt_date(self):
        self.assertEqual(report.fmt_date("2026-07-18T09:00:00Z"), "Jul 18")
        self.assertEqual(report.fmt_date(""), "—")

    def test_truncate_middle(self):
        # A 30-char id fits and is returned verbatim.
        short = "subset-20260718-031746-27875ca"
        self.assertEqual(report.truncate_middle(short), short)
        # A longer id is elided in the middle, keeping both ends.
        out = report.truncate_middle("subset-20260718-031746-extra-suffix-27875ca")
        self.assertIn("…", out)
        self.assertTrue(out.startswith("subset-"))
        self.assertTrue(out.endswith("27875ca"))


class TestSelfContained(unittest.TestCase):
    def test_no_external_resources(self):
        out = _render()
        self.assertIsNone(re.search(r'(?:src|href)\s*=\s*["\']https?:', out))
        self.assertNotIn("http://", out)
        self.assertNotIn("https://", out)

    def test_doctype_present(self):
        out = _render()
        self.assertTrue(out.lstrip().lower().startswith("<!doctype html"))

    def test_single_style_block(self):
        out = _render()
        self.assertEqual(out.lower().count("<style"), 1)
        self.assertEqual(out.lower().count("</style>"), 1)

    def test_embedded_data_present(self):
        out = _render()
        self.assertIn('<script type="application/json" id="bench-data">', out)


class TestTiles(unittest.TestCase):
    def test_stat_tiles_from_latest_subset_row(self):
        out = _render()
        # Values formatted from the latest deep-review-v2 subset row.
        self.assertIn("49.2%", out)   # golden_recall
        self.assertIn("16.4%", out)   # noise_rate
        self.assertIn("52.7%", out)   # precision_strict
        self.assertIn("50.9%", out)   # f1_strict
        self.assertIn("$190.72", out)  # cost_usd
        # Context caption pulls goldens/N from baseline_v2.
        self.assertIn("59 goldens", out)


class TestAnchors(unittest.TestCase):
    def test_all_four_anchor_tools_present(self):
        out = _render()
        self.assertIn("deep-review v2", out)
        self.assertIn("coderabbit", out)
        self.assertIn("claude-code", out)
        # "claude" as a standalone tool label (SVG text node).
        self.assertRegex(out, r">claude<")

    def test_anchor_metric_titles_present(self):
        out = _render()
        self.assertIn("Golden recall", out)
        self.assertIn("Noise rate", out)
        self.assertIn("Precision (strict)", out)


class TestFootnotes(unittest.TestCase):
    def test_footnotes_content(self):
        out = _render()
        self.assertIn("claude-opus-4-5-20251101", out)
        self.assertIn("judge_sd=0", out)
        self.assertIn("N=1", out)
        self.assertIn("bench/experiments.jsonl", out)


class TestCli(unittest.TestCase):
    def test_main_writes_out_file(self):
        with tempfile.TemporaryDirectory() as td:
            ledger = Path(td) / "ledger.jsonl"
            baselines = Path(td) / "baselines.json"
            out = Path(td) / "report.html"
            ledger.write_text(
                "\n".join(json.dumps(r) for r in FIXTURE_ROWS) + "\n",
                encoding="utf-8",
            )
            baselines.write_text(json.dumps(FIXTURE_BASELINES), encoding="utf-8")
            rc = report.main(
                ["--ledger", str(ledger), "--baselines", str(baselines),
                 "--out", str(out)]
            )
            self.assertEqual(rc, 0)
            self.assertTrue(out.exists())
            text = out.read_text(encoding="utf-8")
            self.assertTrue(text.lower().startswith("<!doctype html"))
            self.assertTrue(text.endswith("\n"))
            # Output stays clean for the trailing-whitespace hook.
            self.assertFalse(any(ln != ln.rstrip() for ln in text.split("\n")))

    def test_git_sha_tolerates_non_repo(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(report.git_short_sha(cwd=td), "uncommitted")


class TestRealData(unittest.TestCase):
    def test_real_data_renders(self):
        rows = report.load_ledger(REAL_LEDGER)
        baselines = report.load_baselines(REAL_BASELINES)
        out = report.render_html(rows, baselines, "abc1234", "2026-07-18")
        self.assertIn("subset-20260718", out)
        self.assertEqual(out.lower().count("<style"), 1)

    def test_real_data_cli(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "report.html"
            rc = report.main(
                ["--ledger", str(REAL_LEDGER), "--baselines", str(REAL_BASELINES),
                 "--out", str(out)]
            )
            self.assertEqual(rc, 0)
            self.assertTrue(out.exists())


class TestReleaseProgression(unittest.TestCase):
    """The release-progression headline section: RELEASES is data over the real
    ledger (frozen run_ids), so these tests exercise it against REAL_LEDGER rather
    than the small offline FIXTURE_ROWS."""

    @classmethod
    def setUpClass(cls):
        cls.rows = report.load_ledger(REAL_LEDGER)
        cls.baselines = report.load_baselines(REAL_BASELINES)

    def test_row_by_run_id_finds_exact_match(self):
        row = report.row_by_run_id(self.rows, "custom-20260723-102149-381e9ff")
        self.assertIsNotNone(row)
        self.assertEqual(row["golden_recall"], 0.6333333333333333)

    def test_row_by_run_id_missing_returns_none(self):
        self.assertIsNone(report.row_by_run_id(self.rows, "no-such-run-id"))

    def test_current_release_leg_picks_v31_on_real_ledger(self):
        rel, leg, row = report.current_release_leg(self.rows)
        self.assertEqual(rel["label"], "v3.1")
        self.assertEqual(leg["run_id"], "custom-20260723-102149-381e9ff")
        self.assertEqual(row["run_id"], "custom-20260723-102149-381e9ff")

    def test_current_release_leg_falls_back_on_fixture(self):
        # None of RELEASES' frozen run_ids exist in the small offline fixture —
        # the dashboard must degrade to (None, None, None), not raise.
        rel, leg, row = report.current_release_leg(FIXTURE_ROWS)
        self.assertIsNone(rel)
        self.assertIsNone(leg)
        self.assertIsNone(row)

    def test_releases_html_contains_every_release_and_run_id(self):
        top_anchor, v2_base, ceiling = report._thresholds(self.baselines)
        out = report.build_releases_html(self.rows, top_anchor, v2_base, ceiling)
        for label in ("v3.1", "v3.0", "v2"):
            self.assertIn(label, out)
        for run_id in (
            "custom-20260723-102149-381e9ff",
            "subset-20260721-015119-639e4bc",
            "holdout-20260721-085348-eec15be",
            "subset-20260718-031746-27875ca",
        ):
            self.assertIn(run_id, out)
        # v3.1 clears both hard bars on this ledger.
        self.assertIn("Clears both hard bars", out)

    def test_rendered_page_has_release_progression_ahead_of_history(self):
        out = report.render_html(self.rows, self.baselines, "abc1234", "2026-07-18")
        self.assertIn("Release progression", out)
        self.assertIn('<details class="history-section">', out)
        self.assertIn("History: the v2→v3 rewrite", out)
        # Progression heads the flow: it appears before the collapsed history.
        self.assertLess(
            out.index("Release progression"), out.index('class="history-section"')
        )

    def test_tiles_are_release_aware_on_real_ledger(self):
        out = report.render_html(self.rows, self.baselines, "abc1234", "2026-07-18")
        self.assertIn("current release", out)
        self.assertIn("custom-20260723-102149-381e9ff", out)
        # The v3.1 mini-subset's own numbers, not the stale Gate-1 subset tile.
        self.assertIn("63.3%", out)


class TestMiniTierGraded(unittest.TestCase):
    """``--tier mini`` must be a gate-graded tier in the dashboard (Issue #28)."""

    def test_mini_in_tier_info_and_gate_tiers(self):
        keys = [k for k, _label, _meaning in report.TIER_INFO]
        self.assertIn("mini", keys)
        self.assertIn("mini", report.GATE_TIERS)
        self.assertIn("mini", report._TIER_GLYPH_SVG)

    def test_mini_short_label_and_is_graded(self):
        pt = {
            "run_id": "mini-20260724-aaaaaaa",
            "tool": "deep-review-v3",
            "tier": "mini",
            "kind": "gate",
            "hypothesis": None,
            "recall": 0.6333,
            "noise": 0.2233,
        }
        self.assertEqual(report.short_label(pt), "mini")
        # classify grades GATE_TIERS against bars — mini clears both → gate.
        kind, _glyph, _desc = report.classify(
            {
                "run_id": pt["run_id"],
                "tool": "deep-review-v3",
                "tier": "mini",
                "golden_recall": 0.6333,
                "noise_rate": 0.2233,
            },
            top_anchor=0.6271,
            ceiling=0.24,
        )
        self.assertEqual(kind, "gate")

    def test_tier_info_and_explainer_have_no_double_backticks(self):
        for _key, _label, meaning in report.TIER_INFO:
            self.assertNotIn("``", meaning)
        # Explainer foot is assembled from plain strings — no reST leftovers.
        html_out = report.build_explainer_html(0.6271, 0.5, 0.24)
        self.assertNotIn("``", html_out)
        self.assertIn("pre-mini paired legs", html_out)

    def test_mini_run_marker_is_hexagon_not_subset_circle(self):
        mini = report._run_marker(10, 20, "mini", "--v3", False, False)
        subset = report._run_marker(10, 20, "subset", "--v3", False, False)
        self.assertIn("path", mini)
        self.assertIn("mk-fill", mini)
        self.assertNotIn("<circle", mini)
        self.assertIn("<circle", subset)
        self.assertNotEqual(mini, subset)


class TestVoidRunsAndMilestones(unittest.TestCase):
    """The two superseded runs (contaminated smoke, failed mini-subset attempt)
    fade like CONFOUNDED_RUNS; the three custom-tier runs get distinct labels."""

    @classmethod
    def setUpClass(cls):
        cls.rows = report.load_ledger(REAL_LEDGER)

    def _row(self, run_id):
        return report.row_by_run_id(self.rows, run_id)

    def test_void_runs_classify_as_reverted(self):
        for run_id in report.VOID_RUNS:
            row = self._row(run_id)
            self.assertIsNotNone(row, run_id)
            kind, glyph, desc = report.classify(row, top_anchor=0.6271, ceiling=0.24)
            self.assertEqual(kind, "reverted")
            self.assertEqual(desc, report.VOID_RUNS[run_id])

    def test_milestone_labels_disambiguate_the_three_custom_runs(self):
        labels = {
            run_id: report.short_label(
                {"run_id": run_id, "tool": "deep-review-v3", "tier": "custom",
                 "kind": "gate", "hypothesis": None}
            )
            for run_id in report.MILESTONE_LABELS
        }
        self.assertEqual(labels["custom-20260721-220637-5337f6c"], "Gate-2")
        self.assertEqual(labels["custom-20260723-070640-c1dd46f"], "mini-A (failed)")
        self.assertEqual(labels["custom-20260723-102149-381e9ff"], "v3.1")

    def test_gate2_row_pins_to_the_original_gate2_run(self):
        # Two newer custom-tier runs exist (both v3.1 mini-subset experiments);
        # gate2_row must not pick either of them up as "most recent custom".
        row = report.gate2_row(self.rows)
        self.assertEqual(row["run_id"], report.GATE2_RUN_ID)
        self.assertEqual(row["run_id"], "custom-20260721-220637-5337f6c")


if __name__ == "__main__":
    unittest.main()

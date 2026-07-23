"""Offline integrity tests for vendored golden data and subsets (Task 5).

stdlib only, no network. Validates that bench/golden/ was built from the pinned
upstream at the counts spec H4 requires and that subsets.json is internally
consistent with the golden data.
"""
import json
import unittest
from pathlib import Path

GOLDEN = Path(__file__).resolve().parent.parent / "golden"
GC = GOLDEN / "golden_comments"
GOLDEN_FILES = ["keycloak", "grafana", "discourse", "sentry", "cal_dot_com"]


def _load(name):
    with open(GOLDEN / name, encoding="utf-8") as fh:
        return json.load(fh)


class TestGoldenData(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.minf = _load("benchmark_data.min.json")
        cls.subsets = _load("subsets.json")
        cls.labels = _load("pr_labels.json")
        cls.raw = {
            f: json.load(open(GC / f"{f}.json", encoding="utf-8"))
            for f in GOLDEN_FILES
        }

    # --- totals -------------------------------------------------------------

    def test_pr_count_is_50(self):
        self.assertEqual(len(self.minf), 50)
        raw_prs = sum(len(prs) for prs in self.raw.values())
        self.assertEqual(raw_prs, 50)

    def test_golden_count_is_137(self):
        # Scorer-authoritative count: step3 reads goldens from this shape.
        total = sum(len(v["golden_comments"]) for v in self.minf.values())
        self.assertEqual(total, 137)

    def test_raw_golden_files_sum_to_136(self):
        # Documents the upstream inconsistency: the raw golden_comments/*.json
        # files omit one sentry-greptile#1 comment present in benchmark_data.json.
        total = sum(
            len(pr.get("comments", []))
            for prs in self.raw.values()
            for pr in prs
        )
        self.assertEqual(total, 136)

    # --- min-file shape -----------------------------------------------------

    def test_min_has_all_50_keys_with_empty_reviews(self):
        self.assertEqual(len(self.minf), 50)
        for url, v in self.minf.items():
            self.assertEqual(v["reviews"], [], f"{url} must have empty reviews")
            for field in ("pr_title", "original_url", "source_repo", "golden_comments"):
                self.assertIn(field, v, f"{url} missing {field}")

    def test_min_keys_match_raw_golden_urls(self):
        raw_urls = {pr["url"] for prs in self.raw.values() for pr in prs}
        self.assertEqual(set(self.minf), raw_urls)

    def test_pr_labels_stray_dropped(self):
        self.assertEqual(len(self.labels), 50)
        self.assertNotIn("https://example/pr", self.labels)

    # --- subsets ------------------------------------------------------------

    def test_subset_counts(self):
        self.assertEqual(len(self.subsets["gate"]), 15)
        self.assertEqual(len(self.subsets["holdout"]), 10)
        self.assertEqual(len(self.subsets["smoke"]), 3)
        self.assertEqual(len(self.subsets["mini"]), 6)
        self.assertEqual(len(self.subsets["review_md_fixtures"]), 2)

    def test_subsets_have_no_internal_duplicates(self):
        for name in ("gate", "holdout", "smoke", "mini", "review_md_fixtures"):
            urls = self.subsets[name]
            self.assertEqual(len(urls), len(set(urls)), f"{name} has duplicates")

    def test_gate_holdout_disjoint(self):
        self.assertEqual(set(self.subsets["gate"]) & set(self.subsets["holdout"]), set())

    def test_holdout_smoke_disjoint(self):
        self.assertEqual(set(self.subsets["holdout"]) & set(self.subsets["smoke"]), set())

    def test_gate_smoke_disjoint(self):
        self.assertEqual(set(self.subsets["gate"]) & set(self.subsets["smoke"]), set())

    def test_mini_is_subset_of_gate(self):
        # mini is a gate cut (highest-golden-density PRs), not a disjoint tier.
        self.assertTrue(set(self.subsets["mini"]) <= set(self.subsets["gate"]))

    def test_mini_urls_match_pre_registered_order(self):
        expected = [
            "https://github.com/ai-code-review-evaluation/discourse-graphite/pull/4",
            "https://github.com/calcom/cal.com/pull/11059",
            "https://github.com/calcom/cal.com/pull/14740",
            "https://github.com/getsentry/sentry/pull/93824",
            "https://github.com/grafana/grafana/pull/79265",
            "https://github.com/ai-code-review-evaluation/discourse-graphite/pull/10",
        ]
        self.assertEqual(self.subsets["mini"], expected)

    def test_review_md_fixtures_subset_of_gate(self):
        self.assertTrue(
            set(self.subsets["review_md_fixtures"]) <= set(self.subsets["gate"])
        )

    def test_every_subset_url_exists_in_golden_data(self):
        for name in ("gate", "holdout", "smoke", "mini", "review_md_fixtures"):
            for url in self.subsets[name]:
                self.assertIn(url, self.minf, f"{name} url not in golden data: {url}")


if __name__ == "__main__":
    unittest.main()

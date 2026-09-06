"""Tests for the citation absence-preamble measurement."""

import contextlib
import io
import json
import re
import tempfile
import unittest
from pathlib import Path

from bench.runner import citations

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_DIR = REPO_ROOT / "bench" / "tests" / "fixtures" / "citations"


def _read_jsonl(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


class CitationClassifierTest(unittest.TestCase):
    def test_corpus_rows_match_labels(self):
        """The 40 corpus rows are in-sample; probes below are the held-out set."""
        rows = _read_jsonl(FIXTURE_DIR / "corpus_labels.jsonl")
        self.assertEqual(len(rows), 40)
        for row in rows:
            with self.subTest(row=row["id"], value=row["value"]):
                self.assertEqual(
                    citations.has_absence_preamble(row["value"]),
                    row["absence_preamble"],
                )

    def test_held_out_probes_match_labels(self):
        """Probes are the held-out set, so they are checked independently of the in-sample corpus."""
        rows = _read_jsonl(FIXTURE_DIR / "probes.jsonl")
        self.assertGreaterEqual(len(rows), 13)
        for row in rows:
            with self.subTest(value=row["value"]):
                self.assertEqual(
                    citations.has_absence_preamble(row["value"]),
                    row["absence_preamble"],
                )

    def test_every_alternative_is_load_bearing(self):
        """Rebuilding the pattern without any one alternative flips at least one
        fixture row, so a silently redundant alternative cannot hide in the tuple."""
        rows = _read_jsonl(FIXTURE_DIR / "corpus_labels.jsonl") + _read_jsonl(
            FIXTURE_DIR / "probes.jsonl"
        )
        alternatives = citations.ABSENCE_PREAMBLE_ALTERNATIVES
        self.assertEqual(citations.ABSENCE_PREAMBLE_RE.pattern, "|".join(alternatives))
        self.assertFalse(citations.ABSENCE_PREAMBLE_RE.flags & re.VERBOSE)
        for index in range(len(alternatives)):
            without = citations.compile_absence_pattern(
                alternatives[:index] + alternatives[index + 1 :]
            )
            flipped = [
                row["value"]
                for row in rows
                if (without.search(row["value"]) is not None) != row["absence_preamble"]
            ]
            with self.subTest(alternative=index):
                self.assertTrue(flipped, "no fixture row depends on this alternative")

    def test_non_string_values_are_not_preambles(self):
        for value in (None, 0, False, [], {}):
            with self.subTest(value=value):
                self.assertFalse(citations.has_absence_preamble(value))


class CitationMeasurementTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="bench-citations-")
        self.run_dir = Path(self.tmp.name) / "mini-test"
        self.pr_dir = self.run_dir / "pr-example-repo-1"
        self.findings = [
            {
                "id": "pre",
                "dimension": "convention",
                "claude_md_rule": "No CLAUDE.md exists; use local precedent.",
            },
            {
                "id": "clean",
                "dimension": "convention",
                "claude_md_rule": "app/example.py:10 follows the sibling naming pattern.",
                "rule_source": "repo_precedent",
            },
            {
                "id": "comment",
                "dimension": "comment_accuracy",
                "claude_md_rule": "The comment at app/example.py:10 contradicts the code.",
            },
        ]
        _write_json(self.pr_dir / "code-gauntlet-findings-deadbeef.json", self.findings)
        _write_json(
            self.pr_dir / "code-gauntlet-post-review-deadbeef.json",
            {"findings": [self.findings[0], self.findings[2]]},
        )
        _write_json(
            self.pr_dir / "superseded" / "code-gauntlet-findings-old.json",
            [
                {
                    "id": "archived",
                    "dimension": "convention",
                    "claude_md_rule": "No CLAUDE.md exists; archived.",
                }
            ],
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_iter_citations_returns_required_tuple(self):
        self.assertEqual(
            list(citations.iter_citations(self.pr_dir)),
            [
                (
                    "pre",
                    "convention",
                    "No CLAUDE.md exists; use local precedent.",
                    None,
                ),
                (
                    "clean",
                    "convention",
                    "app/example.py:10 follows the sibling naming pattern.",
                    "repo_precedent",
                ),
                (
                    "comment",
                    "comment_accuracy",
                    "The comment at app/example.py:10 contradicts the code.",
                    None,
                ),
            ],
        )

    def test_measure_run_has_separate_exact_denominators(self):
        expected_block = {
            "populated": 3,
            "absence_preamble": 1,
            "rate": 1 / 3,
            "by_dimension": {
                "convention": {"populated": 2, "absence_preamble": 1},
                "comment_accuracy": {"populated": 1, "absence_preamble": 0},
            },
            "rule_source": {"absent": 2, "repo_precedent": 1},
            "per_pr": {"pr-example-repo-1": {"populated": 3, "absence_preamble": 1}},
        }
        expected_post_review_block = {
            "populated": 2,
            "absence_preamble": 1,
            "rate": 1 / 2,
            "by_dimension": {
                "convention": {"populated": 1, "absence_preamble": 1},
                "comment_accuracy": {"populated": 1, "absence_preamble": 0},
            },
            "rule_source": {"absent": 2},
            "per_pr": {"pr-example-repo-1": {"populated": 2, "absence_preamble": 1}},
        }
        self.assertEqual(
            citations.measure_run(self.run_dir),
            {"findings": expected_block, "post_review": expected_post_review_block},
        )

    def test_cli_prints_human_lines_and_json_round_trips(self):
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            self.assertEqual(citations.main([str(self.run_dir)]), 0)
        lines = stdout.getvalue().splitlines()
        self.assertEqual(
            lines[0],
            "mini-test findings=1/3 (0.333) post_review=1/2 (0.500) convention=1/2",
        )
        self.assertEqual(
            lines[1],
            "total findings=1/3 (0.333) post_review=1/2 (0.500) convention=1/2",
        )

        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            self.assertEqual(citations.main(["--json", str(self.run_dir)]), 0)
        records = json.loads(stdout.getvalue())
        self.assertEqual(records[0]["run_id"], "mini-test")
        self.assertEqual(records[0]["findings"]["populated"], 3)
        self.assertEqual(records[-1]["run_id"], "total")


if __name__ == "__main__":
    unittest.main()

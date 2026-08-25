"""Unit tests for scripts/generate_contract_requirements.py's pure functions (issue #238).

These exercise the generator in isolation from the live registry: fixture-shaped registry
dicts and small file fragments, so a regression in the splice/table-rewrite/sentence-render
logic fails here without needing `node` or the real agent files.
"""

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from scripts import generate_contract_requirements as gen  # noqa: E402


class TestDispatchRequiredSentence(unittest.TestCase):
    def test_single_field(self):
        sentence = gen.dispatch_required_sentence(["attack_vector"])
        self.assertIn("`attack_vector` is required by the dispatch schema", sentence)
        self.assertIn("it must always be present", sentence)

    def test_two_fields(self):
        sentence = gen.dispatch_required_sentence(["criticality", "failure_scenario"])
        self.assertIn(
            "`criticality` and `failure_scenario` are required by the dispatch schema",
            sentence,
        )
        self.assertIn("all must always be present", sentence)

    def test_three_fields_does_not_render_either_both_wording(self):
        sentence = gen.dispatch_required_sentence(["a", "b", "c"])
        self.assertIn("`a`, `b`, and `c` are required by the dispatch schema", sentence)
        self.assertIn("all must always be present", sentence)
        self.assertNotIn("either", sentence)
        self.assertNotIn("both", sentence)


class TestConditionalParagraphs(unittest.TestCase):
    def setUp(self):
        self.rows = [
            {"dimension": "convention", "requiredWhenDimension": ["claude_md_rule"]},
            {"dimension": "intent", "requiredWhenDimension": ["spec_text"]},
            {"dimension": "comment_accuracy", "requiredWhenDimension": []},
        ]

    def test_first_field_carries_full_explanation(self):
        paragraphs = gen.conditional_paragraphs(self.rows)
        self.assertEqual(len(paragraphs), 2)
        first = paragraphs[0]
        self.assertIn("For convention findings:", first)
        self.assertIn(
            "`claude_md_rule` is a dimension-conditional dispatch requirement.", first
        )
        self.assertIn(
            "sibling intent/comment_accuracy findings correctly omit it", first
        )
        self.assertIn(
            "convention, intent, and comment_accuracy findings in ONE schema", first
        )

    def test_second_field_refers_back_to_first(self):
        paragraphs = gen.conditional_paragraphs(self.rows)
        second = paragraphs[1]
        self.assertIn("For intent findings:", second)
        self.assertIn(
            "`spec_text` is a dimension-conditional dispatch requirement, on the same terms "
            "as claude_md_rule above",
            second,
        )
        # The unconditional phrase must never appear as a substring of the conditional one.
        self.assertNotIn("required by the dispatch schema", second)

    def test_no_conditional_fields_yields_no_paragraphs(self):
        rows = [{"dimension": "bug", "requiredWhenDimension": []}]
        self.assertEqual(gen.conditional_paragraphs(rows), [])


class TestSplice(unittest.TestCase):
    def test_first_run_replaces_anchor_text(self):
        text = (
            "before\n`x` is required by the dispatch schema — stale wording.\nafter\n"
        )
        out = gen.splice(
            text,
            gen._SINGLE_SENTENCE_ANCHOR,
            "`x` is required by the dispatch schema — new wording.",
        )
        self.assertIn(gen.MARKER_OPEN, out)
        self.assertIn(gen.MARKER_CLOSE, out)
        self.assertIn("new wording", out)
        self.assertNotIn("stale wording", out)
        self.assertTrue(out.startswith("before\n"))
        self.assertTrue(out.endswith("after\n"))

    def test_second_run_replaces_existing_block(self):
        first = gen.splice(
            "before\n`x` is required by the dispatch schema — old.\nafter\n",
            gen._SINGLE_SENTENCE_ANCHOR,
            "`x` is required by the dispatch schema — v1.",
        )
        second = gen.splice(
            first,
            gen._SINGLE_SENTENCE_ANCHOR,
            "`x` is required by the dispatch schema — v2.",
        )
        self.assertIn("v2", second)
        self.assertNotIn("v1", second)
        self.assertEqual(second.count(gen.MARKER_OPEN), 1)

    def test_anchor_matches_the_generators_own_output_for_every_field_count(self):
        # A stripped marker block must be recoverable from the sentence the generator
        # itself wrote — including the Oxford-comma join for three or more fields. Full
        # equality, not marker presence: an anchor that only matches from the final
        # backticked field would still splice, leaving the leading field list as debris.
        for fields in (["a"], ["a", "b"], ["a", "b", "c"], ["a", "b", "c", "d"]):
            sentence = gen.dispatch_required_sentence(fields)
            out = gen.splice(
                f"before\n{sentence}\nafter\n",
                gen._SINGLE_SENTENCE_ANCHOR,
                sentence,
            )
            self.assertEqual(
                out,
                f"before\n{gen.wrap_block(sentence)}\nafter\n",
                f"anchor mis-spanned the {len(fields)}-field sentence",
            )

    def test_no_anchor_and_no_marker_raises(self):
        with self.assertRaises(SystemExit):
            gen.splice(
                "nothing relevant here\n", gen._SINGLE_SENTENCE_ANCHOR, "irrelevant"
            )

    def test_two_marker_blocks_raises_instead_of_silently_leaving_one_stale(self):
        text = (
            f"{gen.MARKER_OPEN}\nfirst\n{gen.MARKER_CLOSE}\n\n"
            f"{gen.MARKER_OPEN}\nsecond\n{gen.MARKER_CLOSE}\n"
        )
        with self.assertRaises(SystemExit):
            gen.splice(text, gen._SINGLE_SENTENCE_ANCHOR, "new")

    def test_open_without_close_raises(self):
        text = f"before\n{gen.MARKER_OPEN}\nbody\nafter\n"
        with self.assertRaises(SystemExit):
            gen.splice(text, gen._SINGLE_SENTENCE_ANCHOR, "new")

    def test_close_without_open_raises(self):
        text = f"before\nbody\n{gen.MARKER_CLOSE}\nafter\n"
        with self.assertRaises(SystemExit):
            gen.splice(text, gen._SINGLE_SENTENCE_ANCHOR, "new")

    def test_typoed_open_marker_leaves_orphaned_close_and_raises(self):
        # A hand edit that corrupts the open marker's bytes (a typo) makes `MARKER_OPEN
        # in text` False, but the real MARKER_CLOSE debris is still there — exactly the
        # tamper case that used to make a second --check silently call the file current.
        typoed_open = gen.MARKER_OPEN.replace("do not edit", "do NOT edit")
        text = f"before\n{typoed_open}\nbody\n{gen.MARKER_CLOSE}\nafter\n"
        with self.assertRaises(SystemExit):
            gen.splice(text, gen._SINGLE_SENTENCE_ANCHOR, "new")


class TestFieldRequiredStatus(unittest.TestCase):
    def setUp(self):
        self.registry = {
            "required": ["id", "file"],
            "canonicalFields": ["id", "file", "claude_md_rule"],
            "dimensions": [
                {
                    "dimension": "security",
                    "requiredExtra": ["attack_vector"],
                    "requiredWhenDimension": [],
                    "extraFields": ["attack_vector"],
                },
                {
                    "dimension": "convention",
                    "requiredExtra": [],
                    "requiredWhenDimension": ["claude_md_rule"],
                    "extraFields": [],
                },
                {
                    "dimension": "bug",
                    "requiredExtra": [],
                    "requiredWhenDimension": [],
                    "extraFields": ["hidden_errors"],
                },
            ],
        }

    def test_canonical_required_field_is_yes(self):
        self.assertEqual(gen.field_required_status("id", self.registry), "yes")

    def test_required_extra_field_is_yes(self):
        self.assertEqual(
            gen.field_required_status("attack_vector", self.registry), "yes"
        )

    def test_required_when_dimension_field_is_conditional(self):
        self.assertEqual(
            gen.field_required_status("claude_md_rule", self.registry), "conditional"
        )

    def test_unlisted_field_is_no(self):
        self.assertEqual(
            gen.field_required_status("hidden_errors", self.registry), "no"
        )


class TestRewriteRequiredColumn(unittest.TestCase):
    def setUp(self):
        self.registry = {
            "required": ["id"],
            "canonicalFields": ["id", "claude_md_rule"],
            "dimensions": [
                {
                    "dimension": "security",
                    "requiredExtra": ["attack_vector"],
                    "requiredWhenDimension": [],
                    "extraFields": ["attack_vector"],
                },
                {
                    "dimension": "convention",
                    "requiredExtra": [],
                    "requiredWhenDimension": ["claude_md_rule"],
                    "extraFields": [],
                },
            ],
        }

    def test_flips_stale_canonical_cell(self):
        text = "| `claude_md_rule` | string | no | some description |\n"
        out = gen.rewrite_required_column(text, self.registry)
        self.assertIn(
            "| `claude_md_rule` | string | conditional | some description |", out
        )

    def test_flips_stale_perdim_cell(self):
        text = "| `attack_vector` | string | security | no | how it's exploited |\n"
        out = gen.rewrite_required_column(text, self.registry)
        self.assertIn(
            "| `attack_vector` | string | security | yes | how it's exploited |", out
        )

    def test_unknown_field_row_is_left_untouched(self):
        text = "| `not_a_real_field` | string | yes | made up |\n"
        out = gen.rewrite_required_column(text, self.registry)
        self.assertEqual(out, text)

    def test_non_table_lines_are_left_untouched(self):
        text = "Some prose that isn't a table row at all.\n"
        self.assertEqual(gen.rewrite_required_column(text, self.registry), text)

    def test_preserves_trailing_newline_absence(self):
        text = "| `claude_md_rule` | string | no | d |"
        out = gen.rewrite_required_column(text, self.registry)
        self.assertFalse(out.endswith("\n"))


class TestCliAgainstRealRegistry(unittest.TestCase):
    """Exercises apply_targets/main's write and --check paths against a throwaway copy."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        for rel in [
            *gen.compute_targets(str(REPO)).keys(),
            "workflows/src/registry.js",
        ]:
            src = REPO / rel
            dst = root / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
        self.root = root

    def test_check_is_clean_on_a_freshly_regenerated_copy(self):
        gen.apply_targets(str(self.root), check_only=False)
        self.assertEqual(gen.apply_targets(str(self.root), check_only=True), [])

    def test_write_mode_regenerates_a_hand_edited_sentence(self):
        target = self.root / "agents" / "security-reviewer.md"
        target.write_text(
            target.read_text(encoding="utf-8").replace(
                "required by the dispatch schema", "SOMETHING ELSE ENTIRELY"
            ),
            encoding="utf-8",
        )
        stale_before = gen.apply_targets(str(self.root), check_only=True)
        self.assertIn("agents/security-reviewer.md", stale_before)
        stale_after_write = gen.apply_targets(str(self.root), check_only=False)
        self.assertIn("agents/security-reviewer.md", stale_after_write)
        self.assertEqual(gen.apply_targets(str(self.root), check_only=True), [])

    def test_main_check_mode_exits_nonzero_on_drift(self):
        target = self.root / "agents" / "code-simplifier.md"
        target.write_text(
            target.read_text(encoding="utf-8").replace(
                "behavior_preserved", "renamed_field"
            ),
            encoding="utf-8",
        )
        result = subprocess.run(
            [
                sys.executable,
                str(REPO / "scripts" / "generate_contract_requirements.py"),
                "--repo-root",
                str(self.root),
                "--check",
            ],
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("stale generated contract requirements", result.stderr)

    def test_main_write_mode_reports_regeneration_and_then_is_clean(self):
        target = self.root / "agents" / "test-analyzer.md"
        target.write_text(
            target.read_text(encoding="utf-8").replace(
                "all must always be present", "x"
            ),
            encoding="utf-8",
        )
        write_result = subprocess.run(
            [
                sys.executable,
                str(REPO / "scripts" / "generate_contract_requirements.py"),
                "--repo-root",
                str(self.root),
            ],
            capture_output=True,
            text=True,
        )
        self.assertEqual(write_result.returncode, 0)
        self.assertIn("regenerated:", write_result.stdout)

        check_result = subprocess.run(
            [
                sys.executable,
                str(REPO / "scripts" / "generate_contract_requirements.py"),
                "--repo-root",
                str(self.root),
                "--check",
            ],
            capture_output=True,
            text=True,
        )
        self.assertEqual(check_result.returncode, 0)
        self.assertIn("current", check_result.stdout)


if __name__ == "__main__":
    unittest.main()

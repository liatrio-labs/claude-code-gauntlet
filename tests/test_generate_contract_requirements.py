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
from typing import ClassVar

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


class TestIdentityFenceGuards(unittest.TestCase):
    """One case per fail-loud guard on the identity-fence mechanism.

    None of these is reachable from the real tree — it is well formed — so without
    direct cases the whole guard block deletes green, and `--check` would then call a
    file carrying real marker debris current. Mirrors
    tests/test_generate_filter_patterns.py's TestMarkerPairs, the precedent this idiom
    is lifted from.
    """

    # Bodies are a pure function of this dict, so the guards can be driven without
    # `node` or the live registry.
    IDENTITY: ClassVar[dict] = {
        "brand": {"mark": "MARK", "name": "NAME"},
        "severityEmoji": {"critical": "C", "low": "L"},
        "severityEmojiFallback": "F",
    }

    # Every declared fence body, hand-typed against the placeholder IDENTITY above.
    # `identity_body` is the ONLY place these sentences exist in the tree, and both
    # --check and T-DOCFENCE compare a file against that function's own output — so
    # without a literal typed by hand, the generator can be edited to gut or invert a
    # live orchestrator instruction and every gate stays green. Keyed by the same
    # (rel_path, symbol) pair `identity_body` dispatches on, and the key set is
    # asserted equal to IDENTITY_FENCES below, so a new fence cannot ship unpinned.
    EXPECTED_BODIES: ClassVar[dict] = {
        ("scripts/post_review.py", "constants"): (
            'BRAND_MARK = "MARK"\n'
            'BRAND_NAME = "NAME"\n'
            "SEVERITY_EMOJI = {\n"
            '    "critical": "C",\n'
            '    "low": "L",\n'
            "}\n"
            'SEVERITY_EMOJI_FALLBACK = "F"'
        ),
        (gen.REPORT_FORMAT_REL, "severity_legend"): (
            "Product mark: MARK (NAME). Severity emoji: C critical, L low.\n"
            "Always use the Unicode characters, never GitHub shortcodes "
            "(`:red_circle:`) — shortcodes do\n"
            "not render in terminal/chat output."
        ),
        (gen.REPORT_FORMAT_REL, "inline_legend"): (
            "`{emoji}` is C critical / L low, `{SEVERITY}` is the severity uppercased."
        ),
        (gen.REPORT_FORMAT_REL, "summary_header"): "### MARK NAME",
        (gen.REPORT_FORMAT_REL, "inline_sample"): (
            "````markdown\n"
            "**{emoji} [SEVERITY] {finding.title}**\n"
            "\n"
            "{body}\n"
            "\n"
            "**Suggested fix:**\n"
            "{suggestion}\n"
            "\n"
            "**Cited rule:**\n"
            "> {claude_md_rule, falling back to spec_text — blockquoted, one `>` line per source line}\n"
            "\n"
            "```suggestion\n"
            "{suggested_fix_code}\n"
            "```\n"
            "\n"
            "MARK *NAME*\n"
            "````"
        ),
        (
            "skills/code-gauntlet/references/delivery-guide.md",
            "severity_legend",
        ): "Product mark: MARK (NAME). Severity emojis: C critical, L low.",
        (
            "skills/code-gauntlet/references/delivery-guide.md",
            "summary_header",
        ): "### MARK NAME",
        (
            "skills/code-gauntlet/references/delivery-guide.md",
            "inline_sample",
        ): (
            "````markdown\n"
            "**{emoji} [SEVERITY] {finding.title}**\n"
            "\n"
            "{body}\n"
            "\n"
            "**Suggested fix:**\n"
            "{suggestion}\n"
            "\n"
            "**Cited rule:**\n"
            "> {claude_md_rule, falling back to spec_text — blockquoted, one `>` line per source line}\n"
            "\n"
            "```suggestion\n"
            "{suggested_fix_code}\n"
            "```\n"
            "\n"
            "MARK *NAME*\n"
            "````"
        ),
        (
            "skills/code-gauntlet/references/delivery-guide.md",
            "delivery_identity",
        ): (
            "- **Identity:** prepends `### MARK NAME` to `review_body` and appends "
            "`MARK *NAME*` to every rendered comment body — one mark per delivered "
            "surface, never one per finding. Never hand-type either."
        ),
        ("skills/code-gauntlet/SKILL.md", "chat_identity"): (
            "The final delivery summary opens with `MARK NAME` on its first line and "
            "carries no other\n"
            "emoji, except severity emoji when listing findings."
        ),
    }

    def test_every_declared_body_matches_its_hand_typed_literal(self):
        """The second oracle `identity_body` otherwise has none.

        These bodies are live instructions to the orchestrator (D9's chat convention,
        the never-GitHub-shortcodes rule, the `{emoji}`/`{SEVERITY}` placeholder
        contract). Rewriting one in the generator and regenerating leaves the whole
        gate green unless the sentence is also typed out here.
        """
        declared = {
            (rel_path, symbol)
            for rel_path, symbols in gen.IDENTITY_FENCES.items()
            for symbol in symbols
        }
        renderer_owned = {(gen.REPORT_FORMAT_REL, "full_report_template")}
        self.assertEqual(set(self.EXPECTED_BODIES) | renderer_owned, declared)
        for (rel_path, symbol), expected in self.EXPECTED_BODIES.items():
            with self.subTest(path=rel_path, symbol=symbol):
                self.assertEqual(
                    "\n".join(gen.identity_body(rel_path, symbol, self.IDENTITY)),
                    expected,
                )

    def test_matched_pairs_are_returned_with_their_indices(self):
        open_a, close_a = gen.identity_marker_lines("alpha", "x.py")
        open_b, close_b = gen.identity_marker_lines("beta", "x.md")
        lines = [open_a, "body", close_a, "", open_b, close_b]
        self.assertEqual(
            gen.find_identity_pairs(lines, "x.py"), {"alpha": (0, 2), "beta": (4, 5)}
        )

    def test_duplicate_open_marker_fails(self):
        open_a, close_a = gen.identity_marker_lines("alpha", "x.py")
        with self.assertRaises(SystemExit) as ctx:
            gen.find_identity_pairs([open_a, open_a, close_a], "x.py")
        self.assertIn("duplicate open identity marker", str(ctx.exception))

    def test_duplicate_close_marker_fails(self):
        open_a, close_a = gen.identity_marker_lines("alpha", "x.py")
        with self.assertRaises(SystemExit) as ctx:
            gen.find_identity_pairs([open_a, close_a, close_a], "x.py")
        self.assertIn("duplicate close identity marker", str(ctx.exception))

    def test_close_before_open_fails(self):
        open_a, close_a = gen.identity_marker_lines("alpha", "x.py")
        with self.assertRaises(SystemExit) as ctx:
            gen.find_identity_pairs([close_a, open_a], "x.py")
        self.assertIn("precedes its open marker", str(ctx.exception))

    def test_nested_pairs_fail(self):
        open_a, close_a = gen.identity_marker_lines("alpha", "x.py")
        open_b, close_b = gen.identity_marker_lines("beta", "x.py")
        with self.assertRaises(SystemExit) as ctx:
            gen.find_identity_pairs([open_a, open_b, close_b, close_a], "x.py")
        self.assertIn("nested inside", str(ctx.exception))

    def test_an_undeclared_fence_fails_loud(self):
        """The direction `test_a_missing_identity_fence_fails_loud` does not cover: a
        pair naming a symbol IDENTITY_FENCES does not declare. Left unguarded it is
        debris the generator walks past and --check calls current.
        """
        rel = "skills/code-gauntlet/references/delivery-guide.md"
        lines = [
            line
            for symbol in [*gen.IDENTITY_FENCES[rel], "bogus"]
            for line in gen.identity_marker_lines(symbol, rel)
        ]
        with self.assertRaises(SystemExit) as ctx:
            gen.fill_identity_fences("\n".join(lines), rel, self.IDENTITY)
        self.assertIn("match no declared symbol", str(ctx.exception))
        self.assertIn("bogus", str(ctx.exception))

    def test_a_symbol_with_no_body_fails_loud(self):
        """A symbol added to IDENTITY_FENCES with no body must abort, never emit an
        empty fence that --check would then call current forever.
        """
        with self.assertRaises(SystemExit) as ctx:
            gen.identity_body("x.md", "nope", self.IDENTITY)
        self.assertIn("no identity body", str(ctx.exception))

    def test_declared_fences_are_filled_from_the_identity(self):
        rel = "skills/code-gauntlet/references/delivery-guide.md"
        lines = []
        for symbol in gen.IDENTITY_FENCES[rel]:
            open_line, close_line = gen.identity_marker_lines(symbol, rel)
            lines += [open_line, "STALE HAND COPY", close_line]
        filled = gen.fill_identity_fences("\n".join(lines), rel, self.IDENTITY)
        self.assertNotIn("STALE HAND COPY", filled)
        self.assertIn(
            "Product mark: MARK (NAME). Severity emojis: C critical, L low.", filled
        )
        self.assertIn("### MARK NAME", filled)


class TestCliAgainstRealRegistry(unittest.TestCase):
    """Exercises apply_targets/main's write and --check paths against a throwaway copy."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        for rel in [
            *gen.compute_targets(str(REPO)).keys(),
            "workflows/src/registry.js",
            "workflows/src/renderReport.js",
            "workflows/src/filterFindings.js",
            "workflows/src/applyChallenges.js",
            "workflows/src/applyValidations.js",
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
        self.assertIn("stale generated registry blocks", result.stderr)

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

    def test_an_orphaned_identity_fence_fails_loud(self):
        """A close marker deleted by hand must abort, not leave --check calling it current."""
        target = (
            self.root / "skills" / "code-gauntlet" / "references" / "delivery-guide.md"
        )
        _, close_line = gen.identity_marker_lines(
            "severity_legend", "skills/code-gauntlet/references/delivery-guide.md"
        )
        target.write_text(
            target.read_text(encoding="utf-8").replace(close_line + "\n", ""),
            encoding="utf-8",
        )
        with self.assertRaises(SystemExit) as raised:
            gen.apply_targets(str(self.root), check_only=True)
        self.assertIn("unmatched identity marker", str(raised.exception))
        self.assertIn("severity_legend", str(raised.exception))

    def test_a_missing_identity_fence_fails_loud(self):
        """A declared symbol with no fence must abort rather than ship a stale hand copy."""
        rel = "skills/code-gauntlet/references/delivery-guide.md"
        target = self.root / rel
        open_line, close_line = gen.identity_marker_lines("summary_header", rel)
        text = target.read_text(encoding="utf-8")
        start = text.index(open_line)
        end = text.index(close_line) + len(close_line) + 1
        target.write_text(text[:start] + text[end:], encoding="utf-8")
        with self.assertRaises(SystemExit) as raised:
            gen.apply_targets(str(self.root), check_only=True)
        self.assertIn("no marker pair for identity symbol", str(raised.exception))
        self.assertIn("summary_header", str(raised.exception))

    def test_report_format_carries_both_the_required_table_and_its_identity_fences(
        self,
    ):
        """One file, two generated regions — ONE apply_targets run must restore both.

        report-format.md is the only target that owns a Required-column rewrite AND
        identity fences. Under the former one-op-per-file mapping the second assignment
        silently destroyed the first, and it destroyed it INVISIBLY: whichever op
        survived kept the file current, so --check stayed green while the dropped
        region went stale. Corrupting both and asserting both come back is what makes
        that shape unrepresentable.
        """
        rel = gen.REPORT_FORMAT_REL
        target = self.root / rel
        text = target.read_text(encoding="utf-8")

        table_row = next(
            line
            for line in text.splitlines()
            if gen._CANONICAL_ROW.match(line)
            and gen._CANONICAL_ROW.match(line).group(3) == "yes"
        )
        match = gen._CANONICAL_ROW.match(table_row)
        broken_row = match.group(1) + "no" + match.group(4)

        legend_body = gen.identity_body(
            rel, "severity_legend", gen.load_registry(str(self.root))
        )
        broken_legend = "HAND-EDITED LEGEND"
        target.write_text(
            text.replace(table_row, broken_row, 1).replace(
                legend_body[0], broken_legend, 1
            ),
            encoding="utf-8",
        )
        self.assertIn(rel, gen.apply_targets(str(self.root), check_only=True))

        gen.apply_targets(str(self.root), check_only=False)
        restored = target.read_text(encoding="utf-8")
        self.assertIn(table_row, restored)
        self.assertNotIn(broken_row, restored)
        self.assertIn(legend_body[0], restored)
        self.assertNotIn(broken_legend, restored)
        self.assertEqual(gen.apply_targets(str(self.root), check_only=True), [])

        # And a hand-edit to EITHER region alone is reported stale.
        for label, mutated in (
            ("required table", restored.replace(table_row, broken_row, 1)),
            ("identity legend", restored.replace(legend_body[0], broken_legend, 1)),
        ):
            target.write_text(mutated, encoding="utf-8")
            with self.subTest(region=label):
                self.assertIn(rel, gen.apply_targets(str(self.root), check_only=True))
            target.write_text(restored, encoding="utf-8")


class TestApplyOne(unittest.TestCase):
    def test_an_unknown_target_kind_raises(self):
        """An unrecognised kind must abort — never fall through to another rewriter.

        The pre-#36 shape dispatched on `if kind == "splice": ... else: table`, so any
        new kind would have been silently rewritten as a Required-column table.
        """
        with self.assertRaises(SystemExit) as raised:
            gen._apply_one("text", "some/file.md", "bogus", None, {})
        self.assertIn("unknown target kind", str(raised.exception))


if __name__ == "__main__":
    unittest.main()

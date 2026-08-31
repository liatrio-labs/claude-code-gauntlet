"""Tests for the filter-pattern registry and its generator (issue #241).

Four things are pinned here, in rising order of how badly a regression would hurt:

1. **Registry row shapes.** A row that spells the union class literally instead of
   `{WS}`, or names a family that does not exist, would generate a twin that still
   passes every behavioural test while quietly reintroducing the hand-maintained
   duplication the registry exists to remove.
2. **Emission shapes the guard depends on.** `tests/test_filter_twins_unicode_guard.py`
   finds JS families with two strict line regexes. A one-line `compile_single` emission,
   or a marker line that happens to look like an array element, makes its lookup return
   None or capture debris -- and it fails as "not found in JS source", which reads like a
   missing family rather than a changed emission. Both are pinned against the guard's own
   finders, run over this generator's own output.
3. **`ruff format` fixed point.** The generator writes Python into a repo whose
   pre-commit hook rewrites Python. If the two disagree on one line, `--check` is stale
   from the moment anyone commits, forever.
4. **The CLI's write and `--check` paths**, against a throwaway copy of the real twins,
   plus a freshness check that the twins in the tree are actually current.
"""

import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import filter_patterns_registry as registry  # noqa: E402
import generate_filter_patterns as gen  # noqa: E402
import test_filter_twins_unicode_guard as guard  # noqa: E402

_KNOWN_KINDS = {
    "str_list",
    "compile_list",
    "compile_single",
    "word_split",
    "content_sets",
}


class TestRegistryRowShapes(unittest.TestCase):
    def test_every_row_declares_a_kind_the_generator_can_emit(self):
        for row in gen.blocks():
            with self.subTest(row.python_name):
                self.assertIn(row.kind, _KNOWN_KINDS)

    def test_pattern_family_names_follow_the_leading_underscore_convention(self):
        """The guard pairs twins by "Python name minus the leading underscore"; the
        registry must not be the place that quietly breaks it."""
        for row in registry.PATTERN_FAMILIES:
            with self.subTest(row.python_name):
                self.assertTrue(row.python_name.startswith("_"))
                self.assertEqual(row.js_name, row.python_name[1:])

    def test_only_content_sets_may_carry_no_patterns(self):
        for row in gen.blocks():
            with self.subTest(row.python_name):
                if row.kind == "content_sets":
                    self.assertEqual(row.patterns, ())
                else:
                    self.assertTrue(row.patterns)

    def test_single_pattern_kinds_carry_exactly_one_pattern(self):
        for row in gen.blocks():
            if row.kind in ("compile_single", "word_split"):
                with self.subTest(row.python_name):
                    self.assertEqual(len(row.patterns), 1)

    def test_no_pattern_spells_the_union_class_literally(self):
        """`{WS}` is the whole point: a literal respelling is a 135th hand-typed copy."""
        offenders = []
        for row in gen.blocks():
            for pattern in row.patterns:
                if registry.UNION_WS_INNER in pattern:
                    offenders.append(f"{row.python_name}: {pattern!r}")
        for site in registry.INLINE_SITES:
            if registry.UNION_WS_INNER in site.pattern:
                offenders.append(f"{site.name}: {site.pattern!r}")
        self.assertEqual(offenders, [], offenders)

    def test_no_pattern_uses_a_bare_whitespace_escape(self):
        """The same rule the guard enforces on the shipped twin, enforced one level up
        so a `\\s` cannot enter through the registry."""
        offenders = []
        for row in gen.blocks():
            for pattern in row.patterns:
                if "\\s" in pattern or "\\S" in pattern:
                    offenders.append(f"{row.python_name}: {pattern!r}")
        for site in registry.INLINE_SITES:
            if "\\s" in site.pattern or "\\S" in site.pattern:
                offenders.append(f"{site.name}: {site.pattern!r}")
        self.assertEqual(offenders, [], offenders)

    def test_every_flag_name_is_one_the_emitters_understand(self):
        for row in gen.blocks():
            for flag in row.flags:
                with self.subTest(f"{row.python_name}:{flag}"):
                    self.assertIn(flag, {"IGNORECASE", "ASCII", *gen._JS_FLAG_LETTERS})

    def test_content_set_order_references_real_families(self):
        js_names = {row.js_name for row in registry.PATTERN_FAMILIES}
        for content_set in registry.CONTENT_SET_ORDER:
            with self.subTest(content_set.phrase):
                self.assertIn(content_set.family, js_names)

    def test_content_set_phrases_are_js_single_quote_safe(self):
        """The JS emitter wraps a phrase in single quotes with no escaping."""
        for content_set in registry.CONTENT_SET_ORDER:
            with self.subTest(content_set.phrase):
                self.assertNotIn("'", content_set.phrase)
                self.assertNotIn("\\", content_set.phrase)

    def test_inline_site_anchors_are_prefixes_of_their_patterns(self):
        """An anchor that is not part of the pattern it locates would survive the
        rewrite it is supposed to move with, and silently stop finding the site."""
        for site in registry.INLINE_SITES:
            with self.subTest(site.name):
                self.assertIn(site.anchor, site.pattern)


class TestExpansion(unittest.TestCase):
    def test_ws_token_expands_to_the_inner_spelling(self):
        self.assertEqual(gen.expand("[{WS}]+"), f"[{registry.UNION_WS_INNER}]+")

    def test_expansion_leaves_regex_quantifiers_alone(self):
        """`str.format` would read `{0,40}` as a field; a plain replace must not."""
        self.assertEqual(gen.expand(r"[^\x00]{0,40}"), r"[^\x00]{0,40}")

    def test_dash_prefixed_class_expands_without_brackets(self):
        self.assertEqual(gen.expand("[-{WS}]?"), f"[-{registry.UNION_WS_INNER}]?")


class TestPythonEmission(unittest.TestCase):
    def test_raw_string_literal(self):
        self.assertEqual(gen.py_literal(r"\bfoo\b"), 'r"\\bfoo\\b"')

    def test_embedded_double_quote_is_backslash_escaped(self):
        """Dead today (#255 removed the last such pattern) and kept alive on purpose:
        without the escape the literal would end early and the twin would not parse.
        The guard's `_py_pattern_text` undoes exactly this escape before comparing."""
        self.assertEqual(gen.py_literal(r'[^)>"]'), 'r"[^)>\\"]"')
        self.assertEqual(guard._py_pattern_text(r"[^)>\"]"), r'[^)>"]')

    def test_flags_render_as_an_or_chain(self):
        self.assertEqual(
            gen.py_flags(("IGNORECASE", "ASCII")), "re.IGNORECASE | re.ASCII"
        )
        self.assertEqual(gen.py_flags(()), "")

    def test_compile_rows_are_always_exploded(self):
        """Never the collapsed one-line form: measured NOT a `ruff format` fixed point
        once a pattern carries the union class, which would leave `--check` stale
        forever. Emitting it uniformly also canonicalizes the three short rows HEAD
        happened to carry collapsed."""
        row = registry.PatternFamily(
            python_name="_X",
            js_name="X",
            kind="compile_list",
            flags=("IGNORECASE", "ASCII"),
            js_export=False,
            patterns=(r"\bdeadlock\b",),
        )
        self.assertEqual(
            gen.py_block(row),
            [
                "_X = [",
                "    re.compile(",
                '        r"\\bdeadlock\\b",',
                "        re.IGNORECASE | re.ASCII,",
                "    ),",
                "]",
            ],
        )

    def test_word_split_row_has_no_trailing_comma(self):
        """A magic trailing comma would explode the single argument onto its own
        permanently-reformatted line; the shipped 3-line form is the fixed point."""
        lines = gen.py_block(registry.WORD_SPLIT)
        self.assertEqual(lines[0], "_WORD_SPLIT_RE = re.compile(")
        self.assertFalse(lines[1].endswith(","))
        self.assertEqual(lines[2], ")")

    def test_content_sets_wrap_each_family_in_tuple(self):
        """The guard's `_content_set_family_names` asserts each element's patterns side
        is an `ast.Call` over a bare `ast.Name`; a plain tuple literal trips a bare
        `assert`, not a clean failure."""
        lines = gen.py_block(registry.CONTENT_SETS)
        self.assertEqual(lines[0], "_CONTENT_PATTERN_SETS = (")
        self.assertEqual(lines[-1], ")")
        for line in lines[1:-1]:
            self.assertRegex(line, r'^    \("[^"]+", tuple\(_INJECTION_\w+\)\),$')

    def test_unknown_kind_fails_loudly(self):
        row = registry.WORD_SPLIT._replace(kind="sparkle")
        with self.assertRaises(SystemExit):
            gen.py_block(row)

    def test_unknown_content_set_family_fails_loudly(self):
        with self.assertRaises(SystemExit):
            gen._family_by_js_name("NO_SUCH_FAMILY")


class TestJsEmission(unittest.TestCase):
    def test_every_literal_slash_is_escaped(self):
        """HEAD escapes `/` even inside a character class, where the syntax does not
        require it (`[A-Za-z0-9+\\/]` vs Python's `[A-Za-z0-9+/]`)."""
        self.assertEqual(
            gen.js_literal("[A-Za-z0-9+/]https?://", ("IGNORECASE",)),
            "/[A-Za-z0-9+\\/]https?:\\/\\//i",
        )

    def test_ascii_flag_has_no_js_letter(self):
        self.assertEqual(gen.js_flags(("IGNORECASE", "ASCII")), "i")
        self.assertEqual(gen.js_flags(()), "")

    def test_compile_single_is_the_two_line_shape(self):
        row = registry.PATTERN_FAMILIES[-2]
        self.assertEqual(row.kind, "compile_single")
        lines = gen.js_block(row)
        self.assertEqual(len(lines), 2)
        self.assertEqual(lines[0], f"const {row.js_name} =")
        self.assertTrue(lines[1].endswith(";"))

    def test_export_prefix_is_registry_driven(self):
        self.assertTrue(
            gen.js_block(registry.WORD_SPLIT)[0].startswith("export const ")
        )
        self.assertTrue(
            gen.js_block(registry.PATTERN_FAMILIES[0])[0].startswith("const ")
        )

    def test_suggestion_sets_rows_are_unwrapped(self):
        """The Python twin wraps each family in `tuple(...)`; the JS twin must not."""
        lines = gen.js_block(registry.CONTENT_SETS)
        self.assertEqual(lines[0], "export const SUGGESTION_SETS = [")
        for line in lines[1:-1]:
            self.assertRegex(line, r"^  \['[^']+', INJECTION_\w+\],$")

    def test_unknown_kind_fails_loudly(self):
        with self.assertRaises(SystemExit):
            gen.js_block(registry.WORD_SPLIT._replace(kind="sparkle"))


class TestEmissionAgainstTheGuardsOwnFinders(unittest.TestCase):
    """The guard finds JS families with two strict line regexes. Run them over this
    generator's output, not over the shipped file, so a shape change is caught at the
    emitter rather than as a confusing "not found in JS source"."""

    def _with_js_text(self, lines):
        self.addCleanup(setattr, guard, "_js_lines", guard._js_lines)
        guard._js_lines = lambda: lines

    def test_emitted_arrays_are_found_element_for_element(self):
        for row in gen.blocks():
            if row.kind not in ("str_list", "compile_list"):
                continue
            with self.subTest(row.js_name):
                self._with_js_text(gen.js_block(row))
                found = guard._find_js_list(row.js_name)
                self.assertIsNotNone(found)
                self.assertEqual(
                    [guard._js_literal_to_regex_text(e) for e in found],
                    [gen.expand(p) for p in row.patterns],
                )

    def test_emitted_compile_single_is_found_by_the_two_line_finder(self):
        for row in gen.blocks():
            if row.kind != "compile_single":
                continue
            with self.subTest(row.js_name):
                self._with_js_text(gen.js_block(row))
                found = guard._find_js_const_pattern(row.js_name)
                self.assertIsNotNone(
                    found, f"{row.js_name}: emitted shape is invisible to the guard"
                )
                self.assertEqual(
                    guard._js_literal_to_regex_text(found),
                    gen.expand(row.patterns[0]),
                )

    def test_marker_lines_are_invisible_to_the_js_element_scanners(self):
        """A fence line that matched `_JS_ELEMENT_RE` would be captured as a spurious
        array element and desync the byte-identity comparison; one matching
        `_JS_INLINE_TEST_RE` would be compared against a Python site that does not
        exist."""
        for row in gen.blocks():
            for line in gen.marker_lines(row.js_name, "//"):
                with self.subTest(line):
                    self.assertIsNone(guard._JS_ELEMENT_RE.match(line))
                    self.assertIsNone(guard._JS_CONST_BODY_RE.match(line))
                    self.assertIsNone(guard._JS_INLINE_TEST_RE.search(line))

    def test_python_marker_lines_are_comments(self):
        for row in gen.blocks():
            for line in gen.marker_lines(row.python_name, "#"):
                with self.subTest(line):
                    self.assertTrue(line.startswith("# "))


class TestMarkerPairs(unittest.TestCase):
    """`find_marker_pairs` is the per-symbol replacement for the precedent's
    single-open-marker SystemExit -- with a dozen pairs per file, "more than one open
    marker" is the normal case, so the malformed-marker rule had to be re-cut per
    symbol."""

    def test_matched_pairs_are_returned_with_their_indices(self):
        open_a, close_a = gen.marker_lines("A", "#")
        open_b, close_b = gen.marker_lines("B", "#")
        lines = [open_a, "body", close_a, "", open_b, close_b]
        self.assertEqual(
            gen.find_marker_pairs(lines, "#", "x.py"), {"A": (0, 2), "B": (4, 5)}
        )

    def test_indented_markers_are_recognized(self):
        open_a, close_a = gen.marker_lines("A", "//")
        lines = ["  " + open_a, "  " + close_a]
        self.assertEqual(gen.find_marker_pairs(lines, "//", "x.js"), {"A": (0, 1)})

    def test_duplicate_open_marker_fails(self):
        open_a, close_a = gen.marker_lines("A", "#")
        with self.assertRaises(SystemExit) as ctx:
            gen.find_marker_pairs([open_a, open_a, close_a], "#", "x.py")
        self.assertIn("duplicate open marker", str(ctx.exception))

    def test_duplicate_close_marker_fails(self):
        open_a, close_a = gen.marker_lines("A", "#")
        with self.assertRaises(SystemExit) as ctx:
            gen.find_marker_pairs([open_a, close_a, close_a], "#", "x.py")
        self.assertIn("duplicate close marker", str(ctx.exception))

    def test_orphan_open_marker_fails(self):
        open_a, _ = gen.marker_lines("A", "#")
        with self.assertRaises(SystemExit) as ctx:
            gen.find_marker_pairs([open_a], "#", "x.py")
        self.assertIn("unmatched", str(ctx.exception))

    def test_close_before_open_fails(self):
        open_a, close_a = gen.marker_lines("A", "#")
        with self.assertRaises(SystemExit) as ctx:
            gen.find_marker_pairs([close_a, open_a], "#", "x.py")
        self.assertIn("precedes its open marker", str(ctx.exception))

    def test_nested_pairs_fail(self):
        open_a, close_a = gen.marker_lines("A", "#")
        open_b, close_b = gen.marker_lines("B", "#")
        with self.assertRaises(SystemExit) as ctx:
            gen.find_marker_pairs([open_a, open_b, close_b, close_a], "#", "x.py")
        self.assertIn("nested inside", str(ctx.exception))


@unittest.skipUnless(
    shutil.which("ruff"), "ruff is not installed (it is a pre-commit-pinned tool)"
)
class TestRuffFormatFixedPoint(unittest.TestCase):
    """The generator emits the `ruff format` normal form, not a layout of its own.

    Belt-and-suspenders: the pre-commit `ruff-format` hook would rewrite a
    non-fixed-point line in the shipped twin and fail the commit. This catches the same
    thing one level earlier, and against the emitted text specifically, so the failure
    names the generator instead of the file it wrote."""

    def test_the_regenerated_python_twin_is_already_formatted(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "filter_findings.py"
            target.write_text(
                gen.expected_python(
                    (REPO / "scripts" / "filter_findings.py").read_text(
                        encoding="utf-8"
                    )
                ),
                encoding="utf-8",
            )
            result = subprocess.run(
                [
                    "ruff",
                    "format",
                    "--check",
                    "--config",
                    str(REPO / "pyproject.toml"),
                    str(target),
                ],
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


class TestCliAgainstRealRegistry(unittest.TestCase):
    """Exercises apply_targets/main's write and `--check` paths against a throwaway copy
    of the real twins."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        for rel, _ in gen.TARGETS:
            dst = root / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_text((REPO / rel).read_text(encoding="utf-8"), encoding="utf-8")
        self.root = root
        self.py = root / gen.PY_REL
        self.js = root / gen.JS_REL

    def _run(self, *args):
        return subprocess.run(
            [
                sys.executable,
                str(REPO / "scripts" / "generate_filter_patterns.py"),
                "--repo-root",
                str(self.root),
                *args,
            ],
            capture_output=True,
            text=True,
        )

    def test_a_pristine_copy_is_already_current(self):
        self.assertEqual(gen.apply_targets(str(self.root), check_only=True), [])

    def test_write_mode_regenerates_a_hand_edited_python_pattern(self):
        self.py.write_text(
            self.py.read_text(encoding="utf-8").replace(
                r'r"\bdeadlock\b"', r'r"\bHAND-EDITED\b"'
            ),
            encoding="utf-8",
        )
        self.assertEqual(
            gen.apply_targets(str(self.root), check_only=True), [gen.PY_REL]
        )
        self.assertEqual(gen.apply_targets(str(self.root)), [gen.PY_REL])
        self.assertEqual(gen.apply_targets(str(self.root), check_only=True), [])
        self.assertIn(r'r"\bdeadlock\b"', self.py.read_text(encoding="utf-8"))

    def test_write_mode_regenerates_a_hand_edited_js_pattern(self):
        self.js.write_text(
            self.js.read_text(encoding="utf-8").replace(
                r"/\bdeadlock\b/i,", r"/\bHAND-EDITED\b/i,"
            ),
            encoding="utf-8",
        )
        self.assertEqual(
            gen.apply_targets(str(self.root), check_only=True), [gen.JS_REL]
        )
        gen.apply_targets(str(self.root))
        self.assertEqual(gen.apply_targets(str(self.root), check_only=True), [])

    def test_check_mode_does_not_write(self):
        before = self.js.read_text(encoding="utf-8")
        self.js.write_text(
            before.replace(r"/\bdeadlock\b/i,", r"/\bHAND-EDITED\b/i,"),
            encoding="utf-8",
        )
        drifted = self.js.read_text(encoding="utf-8")
        gen.apply_targets(str(self.root), check_only=True)
        self.assertEqual(self.js.read_text(encoding="utf-8"), drifted)

    def test_an_inline_site_is_rewritten_in_place_mid_line(self):
        """The JS suppression rule sits inside an `if (...)`; the rewrite must replace
        the regex literal, not the line."""
        text = self.js.read_text(encoding="utf-8")
        self.js.write_text(
            text.replace(r"\bdeliberate\b/.test(", r"\bHAND\b/.test("), encoding="utf-8"
        )
        gen.apply_targets(str(self.root))
        rewritten = self.js.read_text(encoding="utf-8")
        self.assertEqual(rewritten, text)
        self.assertIn(".test(convText)) {", rewritten)

    def test_a_missing_marker_pair_fails_loudly(self):
        open_line, close_line = gen.marker_lines("INJECTION_SHELL_PATTERNS", "//")
        text = self.js.read_text(encoding="utf-8")
        self.js.write_text(
            text.replace(open_line + "\n", "").replace(close_line + "\n", ""),
            encoding="utf-8",
        )
        with self.assertRaises(SystemExit) as ctx:
            gen.apply_targets(str(self.root), check_only=True)
        self.assertIn("no marker pair for registry symbol", str(ctx.exception))
        self.assertIn("INJECTION_SHELL_PATTERNS", str(ctx.exception))

    def test_an_orphan_marker_pair_fails_loudly(self):
        open_line, close_line = gen.marker_lines("NO_SUCH_FAMILY", "#")
        self.py.write_text(
            self.py.read_text(encoding="utf-8") + f"\n{open_line}\n{close_line}\n",
            encoding="utf-8",
        )
        with self.assertRaises(SystemExit) as ctx:
            gen.apply_targets(str(self.root), check_only=True)
        self.assertIn("match no registry row", str(ctx.exception))

    def test_a_vanished_inline_anchor_fails_loudly(self):
        self.py.write_text(
            self.py.read_text(encoding="utf-8").replace(
                r"\bintentional\b", r"\bmoved-away\b"
            ),
            encoding="utf-8",
        )
        with self.assertRaises(SystemExit) as ctx:
            gen.apply_targets(str(self.root), check_only=True)
        self.assertIn("matched 0 times", str(ctx.exception))

    def test_a_duplicated_inline_anchor_fails_loudly(self):
        self.py.write_text(
            self.py.read_text(encoding="utf-8") + "\n# \\bintentional\\b\n",
            encoding="utf-8",
        )
        with self.assertRaises(SystemExit) as ctx:
            gen.apply_targets(str(self.root), check_only=True)
        self.assertIn("matched 2 times", str(ctx.exception))

    def test_an_unparseable_inline_line_fails_loudly(self):
        """The anchor is on a line the literal scanner cannot resolve to exactly one
        pattern literal -- a rewrite there would be a coin flip."""
        text = self.py.read_text(encoding="utf-8")
        line = next(ln for ln in text.splitlines() if r"\bintentional\b" in ln)
        self.py.write_text(
            text.replace(line, line.replace('r"', "").replace('",', ",")),
            encoding="utf-8",
        )
        with self.assertRaises(SystemExit) as ctx:
            gen.apply_targets(str(self.root), check_only=True)
        self.assertIn("pattern literals carrying", str(ctx.exception))

    def test_main_check_mode_exits_nonzero_on_drift(self):
        self.js.write_text(
            self.js.read_text(encoding="utf-8").replace(
                r"/\bdeadlock\b/i,", r"/\bHAND-EDITED\b/i,"
            ),
            encoding="utf-8",
        )
        result = self._run("--check")
        self.assertEqual(result.returncode, 1)
        self.assertIn("stale generated filter patterns", result.stderr)
        self.assertIn(gen.JS_REL, result.stderr)

    def test_main_write_mode_reports_regeneration_and_then_is_clean(self):
        self.py.write_text(
            self.py.read_text(encoding="utf-8").replace(
                r'r"\bdeadlock\b"', r'r"\bHAND-EDITED\b"'
            ),
            encoding="utf-8",
        )
        write_result = self._run()
        self.assertEqual(write_result.returncode, 0)
        self.assertIn("regenerated:", write_result.stdout)

        check_result = self._run("--check")
        self.assertEqual(check_result.returncode, 0)
        self.assertIn("current", check_result.stdout)

    def test_main_in_process_reports_current(self):
        self.assertEqual(gen.main(["--repo-root", str(self.root), "--check"]), 0)


class TestShippedTwinsAreFresh(unittest.TestCase):
    def test_generator_check_is_clean_against_the_real_repo(self):
        """The twins in the tree are what the registry says they are. Nothing else in
        the suite would notice a hand edit to a generated block that happened to stay
        cross-twin consistent."""
        result = subprocess.run(
            [
                sys.executable,
                str(REPO / "scripts" / "generate_filter_patterns.py"),
                "--check",
            ],
            capture_output=True,
            text=True,
            cwd=str(REPO),
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_every_registry_symbol_is_fenced_in_both_twins(self):
        for rel, comment, attr in (
            (gen.PY_REL, "#", "python_name"),
            (gen.JS_REL, "//", "js_name"),
        ):
            lines = (REPO / rel).read_text(encoding="utf-8").split("\n")
            pairs = gen.find_marker_pairs(lines, comment, rel)
            self.assertEqual(
                set(pairs),
                {getattr(row, attr) for row in gen.blocks()},
                rel,
            )

    def test_the_generator_is_stdlib_only(self):
        """scripts/AGENTS.md: nothing under scripts/ may import a non-stdlib module."""
        source = (REPO / "scripts" / "generate_filter_patterns.py").read_text(
            encoding="utf-8"
        )
        imported = set(re.findall(r"^(?:import|from) (\w+)", source, re.MULTILINE))
        self.assertEqual(
            imported - set(sys.stdlib_module_names), {"filter_patterns_registry"}
        )


if __name__ == "__main__":
    unittest.main()

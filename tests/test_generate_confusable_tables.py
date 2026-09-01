"""Tests for scripts/generate_confusable_tables.py (issue #272).

The sibling generator that emits the confusable-fold + invisible-strip packed tables
into both filter twins from scripts/filter_patterns_registry.py. Mirrors the shape of
tests/test_generate_filter_patterns.py: a shipped-twins-are-fresh check, a `ruff format`
fixed-point check for the emitted Python, write/`--check` CLI paths against a throwaway
copy, and stdlib-only enforcement.
"""

import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

import generate_confusable_tables as gen  # noqa: E402
from filter_patterns_registry import (  # noqa: E402
    CONFUSABLE_FOLD_PACKED,
    INVISIBLE_STRIP_PACKED,
)


class TestDecode(unittest.TestCase):
    def test_decode_fold_round_trips_the_registry(self):
        pairs = gen.decode_fold(CONFUSABLE_FOLD_PACKED)
        self.assertEqual(len(pairs), 1468)
        table = dict(pairs)
        # Precedence: U+017F LONG S resolves to s (casefold), never the confusables f.
        self.assertEqual(table[0x017F], "s")
        # Every target is one ASCII letter; every source is non-ASCII.
        for cp, letter in pairs:
            self.assertGreater(cp, 0x7F)
            self.assertEqual(len(letter), 1)
            self.assertTrue(letter.isascii() and letter.isalpha())

    def test_decode_strip_round_trips_the_registry(self):
        codepoints = gen.decode_strip(INVISIBLE_STRIP_PACKED)
        self.assertEqual(len(codepoints), 599)
        self.assertTrue(all(cp > 0x7F for cp in codepoints))

    def test_fold_and_strip_keys_are_disjoint(self):
        fold = {cp for cp, _ in gen.decode_fold(CONFUSABLE_FOLD_PACKED)}
        strip = set(gen.decode_strip(INVISIBLE_STRIP_PACKED))
        self.assertEqual(fold & strip, set())

    def test_astral_codepoints_survive_decoding(self):
        # 919 fold sources and 240 strip codepoints are astral; a UTF-16-unit decode
        # would split them. Assert some are present and decoded as single codepoints.
        fold = {cp for cp, _ in gen.decode_fold(CONFUSABLE_FOLD_PACKED)}
        strip = set(gen.decode_strip(INVISIBLE_STRIP_PACKED))
        self.assertIn(0x1D5CC, fold)  # MATH SANS-SERIF SMALL S
        self.assertIn(0xE0100, strip)  # VARIATION SELECTOR-17 (astral)


class TestEmission(unittest.TestCase):
    def test_python_and_js_escapes(self):
        self.assertEqual(gen._py_escape(0x017F), "\\u017f")
        self.assertEqual(gen._py_escape(0x1D5CC), "\\U0001d5cc")
        self.assertEqual(gen._js_escape(0x017F), "\\u017f")
        self.assertEqual(gen._js_escape(0x1D5CC), "\\u{1d5cc}")

    def test_py_block_is_a_parenthesized_string_concat(self):
        lines = gen.py_block("fold", "_CONFUSABLE_FOLD_PACKED")
        self.assertEqual(lines[0], "_CONFUSABLE_FOLD_PACKED = (")
        self.assertEqual(lines[-1], ")")
        self.assertTrue(all(seg.startswith('    "') for seg in lines[1:-1]))

    def test_js_block_is_a_plus_concatenated_string(self):
        lines = gen.js_block("strip", "INVISIBLE_STRIP_PACKED")
        self.assertEqual(lines[0], "const INVISIBLE_STRIP_PACKED =")
        self.assertTrue(lines[-1].endswith("';"))
        self.assertTrue(all(line.endswith(" +") for line in lines[1:-1]))


class TestShippedTwinsAreFresh(unittest.TestCase):
    def test_generator_check_is_clean_against_the_real_repo(self):
        result = subprocess.run(
            [
                sys.executable,
                str(REPO / "scripts" / "generate_confusable_tables.py"),
                "--check",
            ],
            capture_output=True,
            text=True,
            cwd=str(REPO),
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_both_twins_carry_both_marker_pairs(self):
        for rel in (gen.PY_REL, gen.JS_REL):
            lines = (REPO / rel).read_text(encoding="utf-8").split("\n")
            pairs = gen.find_marker_pairs(lines, rel)
            self.assertEqual(
                set(pairs),
                {
                    sym
                    for _, py, js in gen._TABLES
                    for sym in (py if rel == gen.PY_REL else js,)
                },
                rel,
            )


@unittest.skipUnless(
    shutil.which("ruff"), "ruff is not installed (it is a pre-commit-pinned tool)"
)
class TestRuffFormatFixedPoint(unittest.TestCase):
    def test_the_regenerated_python_twin_is_already_formatted(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "filter_findings.py"
            target.write_text(
                gen.expected_python((REPO / gen.PY_REL).read_text(encoding="utf-8")),
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

    def test_a_pristine_copy_is_already_current(self):
        self.assertEqual(gen.apply_targets(str(self.root), check_only=True), [])

    def test_a_hand_edit_inside_the_python_fence_is_detected_and_rewritten(self):
        text = self.py.read_text(encoding="utf-8")
        # Corrupt one packed segment inside the fold fence.
        marker = "_CONFUSABLE_FOLD_PACKED = (\n"
        idx = text.index(marker) + len(marker)
        corrupted = text[:idx] + '    "\\u0041x"\n' + text[idx:]
        self.py.write_text(corrupted, encoding="utf-8")
        self.assertEqual(
            gen.apply_targets(str(self.root), check_only=True), [gen.PY_REL]
        )
        self.assertEqual(gen.apply_targets(str(self.root)), [gen.PY_REL])
        self.assertEqual(gen.apply_targets(str(self.root), check_only=True), [])

    def test_a_hand_edit_inside_the_js_fence_is_detected(self):
        text = self.js.read_text(encoding="utf-8")
        marker = "const CONFUSABLE_FOLD_PACKED =\n"
        idx = text.index(marker) + len(marker)
        corrupted = text[:idx] + "  '\\u0041x' +\n" + text[idx:]
        self.js.write_text(corrupted, encoding="utf-8")
        self.assertEqual(
            gen.apply_targets(str(self.root), check_only=True), [gen.JS_REL]
        )
        gen.apply_targets(str(self.root))
        self.assertEqual(gen.apply_targets(str(self.root), check_only=True), [])

    def test_main_reports_current(self):
        self.assertEqual(gen.main(["--repo-root", str(self.root), "--check"]), 0)

    def test_main_check_reports_stale(self):
        marker = "_INVISIBLE_STRIP_PACKED = (\n"
        text = self.py.read_text(encoding="utf-8")
        idx = text.index(marker) + len(marker)
        self.py.write_text(
            text[:idx] + '    "\\u0041"\n' + text[idx:], encoding="utf-8"
        )
        self.assertEqual(gen.main(["--repo-root", str(self.root), "--check"]), 1)

    def test_main_write_regenerates(self):
        marker = "_INVISIBLE_STRIP_PACKED = (\n"
        text = self.py.read_text(encoding="utf-8")
        idx = text.index(marker) + len(marker)
        self.py.write_text(
            text[:idx] + '    "\\u0041"\n' + text[idx:], encoding="utf-8"
        )
        self.assertEqual(gen.main(["--repo-root", str(self.root)]), 0)
        self.assertEqual(gen.apply_targets(str(self.root), check_only=True), [])


class TestMarkerFailures(unittest.TestCase):
    def test_missing_pair_hard_fails(self):
        lines = ["nothing here"]
        with self.assertRaises(SystemExit):
            gen.fill_fences("\n".join(lines), "x", lambda k: "n", lambda k, n: ["x"])

    def test_duplicate_open_marker_hard_fails(self):
        lines = [
            "# generated-from-confusable-registry:_CONFUSABLE_FOLD_PACKED a",
            "# generated-from-confusable-registry:_CONFUSABLE_FOLD_PACKED a",
        ]
        with self.assertRaises(SystemExit):
            gen.find_marker_pairs(lines, "x")

    def test_unmatched_marker_hard_fails(self):
        lines = ["# generated-from-confusable-registry:_CONFUSABLE_FOLD_PACKED a"]
        with self.assertRaises(SystemExit):
            gen.find_marker_pairs(lines, "x")

    def test_close_before_open_hard_fails(self):
        lines = [
            "# /generated-from-confusable-registry:_CONFUSABLE_FOLD_PACKED",
            "# generated-from-confusable-registry:_CONFUSABLE_FOLD_PACKED a",
        ]
        with self.assertRaises(SystemExit):
            gen.find_marker_pairs(lines, "x")

    def test_orphan_pair_hard_fails(self):
        text = "\n".join(
            [
                "# generated-from-confusable-registry:BOGUS a",
                "# /generated-from-confusable-registry:BOGUS",
                "# generated-from-confusable-registry:_CONFUSABLE_FOLD_PACKED a",
                "# /generated-from-confusable-registry:_CONFUSABLE_FOLD_PACKED",
                "# generated-from-confusable-registry:_INVISIBLE_STRIP_PACKED a",
                "# /generated-from-confusable-registry:_INVISIBLE_STRIP_PACKED",
            ]
        )
        with self.assertRaises(SystemExit):
            gen.expected_python(text)


class TestStdlibOnly(unittest.TestCase):
    def test_the_generator_is_stdlib_only(self):
        import re

        source = (REPO / "scripts" / "generate_confusable_tables.py").read_text(
            encoding="utf-8"
        )
        imported = set(re.findall(r"^(?:import|from) (\w+)", source, re.MULTILINE))
        self.assertEqual(
            imported - set(sys.stdlib_module_names), {"filter_patterns_registry"}
        )


if __name__ == "__main__":
    unittest.main()

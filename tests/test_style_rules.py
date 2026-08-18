"""Guards for the session-output style mechanism.

docs/style/wording-rules.md and docs/style/cadence-rules.md are the canonical rule
sources, written for a maintainer. scripts/build_style_artifacts.py extracts every
`RULE: ` line verbatim into docs/style/session-context.md, the one carrier a SessionStart
hook injects whole via scripts/emit_style_context.py. This module proves: the carrier
stays fresh (delegating to the generator's own --check, mirroring
TestGeneratedTwins.test_no_twin_is_stale in test_agent_instruction_layout.py), the
sources obey their own rules, the carrier is a faithful and complete extraction, fenced
code blocks are skipped, and the emitter produces the exact hook payload shape.
"""

import json
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
BUILD_SCRIPT = REPO / "scripts" / "build_style_artifacts.py"
EMIT_SCRIPT = REPO / "scripts" / "emit_style_context.py"
WORDING_SOURCE = REPO / "docs" / "style" / "wording-rules.md"
CADENCE_SOURCE = REPO / "docs" / "style" / "cadence-rules.md"
CARRIER = REPO / "docs" / "style" / "session-context.md"

RULE_PREFIX = "RULE: "


def extract_rule_lines(text):
    """Every `RULE: ` line, verbatim (prefix stripped), skipping fenced blocks."""
    rules = []
    in_fence = False
    for line in text.splitlines():
        if line.strip().startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        if line.startswith(RULE_PREFIX):
            rules.append(line[len(RULE_PREFIX) :])
    return rules


def run_build(args, cwd=REPO):
    return subprocess.run(
        [sys.executable, str(BUILD_SCRIPT), *args],
        cwd=cwd,
        capture_output=True,
        text=True,
    )


class TestCarrierFreshness(unittest.TestCase):
    def test_check_passes_in_the_real_tree(self):
        result = run_build(["--check"])
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_check_fails_on_a_mutated_carrier(self):
        with _fixture_tree() as tmp:
            carrier = tmp / "docs" / "style" / "session-context.md"
            carrier.write_text(
                carrier.read_text(encoding="utf-8") + "stray\n", encoding="utf-8"
            )
            result = run_build(["--check", "--repo-root", str(tmp)])
            self.assertEqual(result.returncode, 1)
            self.assertIn("stale", result.stderr)

    def test_check_fails_on_a_missing_carrier(self):
        with _fixture_tree() as tmp:
            (tmp / "docs" / "style" / "session-context.md").unlink()
            result = run_build(["--check", "--repo-root", str(tmp)])
            self.assertEqual(result.returncode, 1)

    def test_write_regenerates_a_stale_carrier(self):
        with _fixture_tree() as tmp:
            carrier = tmp / "docs" / "style" / "session-context.md"
            original = carrier.read_text(encoding="utf-8")
            carrier.write_text("stale\n", encoding="utf-8")
            result = run_build(["--repo-root", str(tmp)])
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(carrier.read_text(encoding="utf-8"), original)

    def test_write_is_a_noop_message_when_already_current(self):
        with _fixture_tree() as tmp:
            result = run_build(["--repo-root", str(tmp)])
            self.assertEqual(result.returncode, 0)
            self.assertIn("current", result.stdout)


class TestSourceSelfCompliance(unittest.TestCase):
    """The sources must obey the rules they define, at least where a rule is checkable."""

    def _all_rule_lines(self):
        lines = []
        for source in (WORDING_SOURCE, CADENCE_SOURCE):
            lines.extend(extract_rule_lines(source.read_text(encoding="utf-8")))
        return lines

    def test_each_rule_line_is_25_words_or_fewer(self):
        for line in self._all_rule_lines():
            with self.subTest(line=line):
                word_count = len(line.split())
                self.assertLessEqual(word_count, 25, f"{word_count} words: {line!r}")

    def test_no_em_dash_in_any_rule_line(self):
        for line in self._all_rule_lines():
            with self.subTest(line=line):
                self.assertNotIn("—", line)

    def test_each_rule_is_one_physical_line(self):
        # A RULE: line whose sentence spans multiple physical lines would never match
        # the `line.startswith("RULE: ")` extraction test at all -- so the real risk is
        # a rule that got wrapped and silently lost everything past the first line.
        # Every extracted line must end with terminal punctuation to prove it is whole.
        for line in self._all_rule_lines():
            with self.subTest(line=line):
                self.assertTrue(
                    line.rstrip().endswith((".", '."')),
                    f"rule does not end in terminal punctuation, possibly wrapped: {line!r}",
                )

    def test_sources_yield_at_least_one_rule_each(self):
        self.assertTrue(extract_rule_lines(WORDING_SOURCE.read_text(encoding="utf-8")))
        self.assertTrue(extract_rule_lines(CADENCE_SOURCE.read_text(encoding="utf-8")))


class TestCarrierIntegrity(unittest.TestCase):
    def test_banner_is_line_one_and_an_html_comment(self):
        text = CARRIER.read_text(encoding="utf-8")
        first_line = text.splitlines()[0]
        self.assertTrue(first_line.startswith("<!--"))
        self.assertIn("GENERATED from docs/style/wording-rules.md", first_line)

    def test_every_source_rule_appears_verbatim_as_a_bullet(self):
        carrier_text = CARRIER.read_text(encoding="utf-8")
        for source in (WORDING_SOURCE, CADENCE_SOURCE):
            for rule in extract_rule_lines(source.read_text(encoding="utf-8")):
                with self.subTest(rule=rule):
                    self.assertIn(f"- {rule}", carrier_text)

    def test_bullet_count_matches_source_rule_count(self):
        carrier_text = CARRIER.read_text(encoding="utf-8")
        bullet_count = sum(
            1 for line in carrier_text.splitlines() if line.startswith("- ")
        )
        source_count = sum(
            len(extract_rule_lines(source.read_text(encoding="utf-8")))
            for source in (WORDING_SOURCE, CADENCE_SOURCE)
        )
        self.assertEqual(bullet_count, source_count)

    def test_carrier_contains_no_em_dash(self):
        self.assertNotIn("—", CARRIER.read_text(encoding="utf-8"))

    def test_carrier_ends_with_a_single_trailing_newline(self):
        raw = CARRIER.read_bytes()
        self.assertTrue(raw.endswith(b"\n"))
        self.assertFalse(raw.endswith(b"\n\n"))


class TestFencedBlockSafety(unittest.TestCase):
    def test_rule_line_inside_a_fence_is_not_extracted(self):
        with _fixture_tree() as tmp:
            wording = tmp / "docs" / "style" / "wording-rules.md"
            wording.write_text(
                wording.read_text(encoding="utf-8")
                + "\n```text\nRULE: this is inside a fence and must never be extracted.\n```\n",
                encoding="utf-8",
            )
            result = run_build(["--repo-root", str(tmp)])
            self.assertEqual(result.returncode, 0, result.stderr)
            carrier_text = (tmp / "docs" / "style" / "session-context.md").read_text(
                encoding="utf-8"
            )
            self.assertNotIn("this is inside a fence", carrier_text)


class TestGeneratorErrorPaths(unittest.TestCase):
    def test_missing_source_is_a_clear_error(self):
        with _fixture_tree() as tmp:
            (tmp / "docs" / "style" / "cadence-rules.md").unlink()
            result = run_build(["--repo-root", str(tmp)])
            self.assertEqual(result.returncode, 1)
            self.assertIn("missing", result.stderr)
            self.assertIn("cadence-rules.md", result.stderr)

    def test_zero_rules_is_a_clear_error(self):
        with _fixture_tree() as tmp:
            wording = tmp / "docs" / "style" / "wording-rules.md"
            wording.write_text("# no rules here\n\njust prose.\n", encoding="utf-8")
            result = run_build(["--repo-root", str(tmp)])
            self.assertEqual(result.returncode, 1)
            self.assertIn("zero", result.stderr)


class TestEmitter(unittest.TestCase):
    def test_stdout_is_the_expected_hook_payload(self):
        result = subprocess.run(
            [sys.executable, str(EMIT_SCRIPT)], capture_output=True, text=True
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["hookSpecificOutput"]["hookEventName"], "SessionStart")
        self.assertEqual(
            payload["hookSpecificOutput"]["additionalContext"],
            CARRIER.read_text(encoding="utf-8"),
        )

    def test_missing_carrier_exits_zero_with_empty_stdout(self):
        with _fixture_tree() as tmp:
            (tmp / "docs" / "style" / "session-context.md").unlink()
            script = tmp / "scripts" / "emit_style_context.py"
            result = subprocess.run(
                [sys.executable, str(script)], capture_output=True, text=True
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "")


class _fixture_tree:
    """A tmp dir laid out as docs/style/{wording,cadence,session-context} + scripts/.

    Never mutates the real tree. The emitter's repo root is derived from its own file
    location, so the fixture ships a copy of emit_style_context.py alongside the copied
    build script to exercise that path-resolution behavior too.
    """

    def __enter__(self):
        import tempfile

        self._tmpdir = tempfile.mkdtemp()
        tmp = Path(self._tmpdir)
        (tmp / "docs" / "style").mkdir(parents=True)
        (tmp / "scripts").mkdir(parents=True)
        shutil.copy(WORDING_SOURCE, tmp / "docs" / "style" / "wording-rules.md")
        shutil.copy(CADENCE_SOURCE, tmp / "docs" / "style" / "cadence-rules.md")
        shutil.copy(BUILD_SCRIPT, tmp / "scripts" / "build_style_artifacts.py")
        shutil.copy(EMIT_SCRIPT, tmp / "scripts" / "emit_style_context.py")
        result = subprocess.run(
            [sys.executable, str(tmp / "scripts" / "build_style_artifacts.py")],
            cwd=tmp,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr
        return tmp

    def __exit__(self, exc_type, exc, tb):
        shutil.rmtree(self._tmpdir, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()

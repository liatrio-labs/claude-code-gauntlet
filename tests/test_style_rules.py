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
import re
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
        # Every extracted line must end with terminal punctuation to prove it is whole,
        # and the physical line right after it must be blank -- a wrapped rule leaves
        # its continuation there instead.
        for source in (WORDING_SOURCE, CADENCE_SOURCE):
            lines = source.read_text(encoding="utf-8").splitlines()
            for i, line in enumerate(lines):
                if not line.startswith(RULE_PREFIX):
                    continue
                with self.subTest(source=source.name, line=line):
                    rule = line[len(RULE_PREFIX) :]
                    self.assertTrue(
                        rule.rstrip().endswith((".", '."')),
                        f"rule does not end in terminal punctuation, possibly wrapped: {rule!r}",
                    )
                    self.assertEqual(
                        lines[i + 1],
                        "",
                        f"line after RULE: is not blank, possibly a wrapped continuation: {lines[i + 1]!r}",
                    )

    def test_sources_yield_at_least_one_rule_each(self):
        self.assertTrue(extract_rule_lines(WORDING_SOURCE.read_text(encoding="utf-8")))
        self.assertTrue(extract_rule_lines(CADENCE_SOURCE.read_text(encoding="utf-8")))

    def test_each_section_has_exactly_one_rule_and_one_check_line(self):
        heading_re = re.compile(r"^## ")
        check_re = re.compile(r"^\*\*(Check|Self-check):\*\*")
        for source in (WORDING_SOURCE, CADENCE_SOURCE):
            lines = source.read_text(encoding="utf-8").splitlines()
            # Partition into sections starting at each `##` heading (source has no
            # fenced headings, so no fence-tracking is needed here).
            starts = [i for i, line in enumerate(lines) if heading_re.match(line)]
            bounds = list(zip(starts, [*starts[1:], len(lines)], strict=True))
            for start, end in bounds:
                section = lines[start:end]
                with self.subTest(source=source.name, heading=section[0]):
                    rule_count = sum(
                        1 for line in section if line.startswith(RULE_PREFIX)
                    )
                    check_count = sum(1 for line in section if check_re.match(line))
                    self.assertEqual(
                        rule_count, 1, f"{section[0]}: {rule_count} RULE: lines"
                    )
                    self.assertEqual(
                        check_count,
                        1,
                        f"{section[0]}: {check_count} Check/Self-check lines",
                    )


def raw_heading_count_outside_fences(text):
    """`^## ` headings outside fences, via a fresh scan -- independent of the module."""
    count = 0
    in_fence = False
    for line in text.splitlines():
        if line.strip().startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        if re.match(r"^## ", line):
            count += 1
    return count


def raw_rule_texts(text):
    """RULE: line texts, skipping fenced blocks -- its own parity toggle, independent of
    the generator's extract_rules. Without fence-awareness a fenced negative example that
    starts with `RULE: ` would poison this oracle into expecting carrier text that must
    never appear there.
    """
    texts = []
    in_fence = False
    for line in text.splitlines():
        if line.strip().startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        if line.startswith("RULE: "):
            texts.append(line[len("RULE: ") :])
    return texts


class TestCarrierIntegrity(unittest.TestCase):
    """Every check here is against an oracle independent of extract_rules.

    Heading count is the real invariant the generator enforces (one RULE: line per `##`
    section) -- checking bullet count against a reimplementation of the module's own
    extraction would prove only that two copies of the same logic agree.
    """

    def test_banner_is_line_one_and_an_html_comment(self):
        text = CARRIER.read_text(encoding="utf-8")
        first_line = text.splitlines()[0]
        self.assertTrue(first_line.startswith("<!--"))
        self.assertIn("GENERATED from docs/style/wording-rules.md", first_line)

    def test_heading_count_matches_bullet_count_per_source_section(self):
        carrier_text = CARRIER.read_text(encoding="utf-8")
        wording_section, cadence_section = _split_carrier_sections(carrier_text)
        cases = (
            (WORDING_SOURCE, wording_section),
            (CADENCE_SOURCE, cadence_section),
        )
        for source, section in cases:
            with self.subTest(source=source.name):
                headings = raw_heading_count_outside_fences(
                    source.read_text(encoding="utf-8")
                )
                bullets = sum(
                    1 for line in section.splitlines() if line.startswith("- ")
                )
                self.assertEqual(headings, bullets)

    def test_every_source_rule_text_appears_verbatim_in_the_carrier(self):
        carrier_text = CARRIER.read_text(encoding="utf-8")
        for source in (WORDING_SOURCE, CADENCE_SOURCE):
            for rule_text in raw_rule_texts(source.read_text(encoding="utf-8")):
                with self.subTest(rule=rule_text):
                    self.assertIn(f"- {rule_text}", carrier_text)

    def test_carrier_contains_no_em_dash(self):
        self.assertNotIn("—", CARRIER.read_text(encoding="utf-8"))

    def test_fenced_rule_line_is_neither_in_the_carrier_nor_fails_integrity(self):
        """A fenced negative example starting with `RULE: ` inside an existing section
        must not appear in the carrier, and must not trip the oracle above -- proving
        raw_rule_texts is fence-aware rather than trusting the generator's own skip.
        """
        with _fixture_tree() as tmp:
            wording = tmp / "docs" / "style" / "wording-rules.md"
            text = wording.read_text(encoding="utf-8")
            poison = (
                "\n```text\nRULE: this fenced line must never reach the carrier.\n```\n"
            )
            # Insert right after the first RULE: line's blank-line successor, still
            # inside that same `##` section, so heading parity is untouched.
            lines = text.splitlines()
            insert_at = next(
                i + 2 for i, line in enumerate(lines) if line.startswith("RULE: ")
            )
            lines[insert_at:insert_at] = poison.splitlines()
            wording.write_text("\n".join(lines) + "\n", encoding="utf-8")

            result = run_build(["--repo-root", str(tmp)])
            self.assertEqual(result.returncode, 0, result.stderr)

            carrier_text = (tmp / "docs" / "style" / "session-context.md").read_text(
                encoding="utf-8"
            )
            self.assertNotIn(
                "this fenced line must never reach the carrier", carrier_text
            )
            for rule_text in raw_rule_texts(text):
                self.assertIn(f"- {rule_text}", carrier_text)

    def test_carrier_ends_with_a_single_trailing_newline(self):
        raw = CARRIER.read_bytes()
        self.assertTrue(raw.endswith(b"\n"))
        self.assertFalse(raw.endswith(b"\n\n"))


def _split_carrier_sections(carrier_text):
    """The carrier's `## Wording` and `## Cadence` bullet blocks, as raw text."""
    wording_start = carrier_text.index("## Wording")
    cadence_start = carrier_text.index("## Cadence")
    return carrier_text[wording_start:cadence_start], carrier_text[cadence_start:]


class TestFencedBlockSafety(unittest.TestCase):
    def test_rule_line_inside_a_fence_is_not_extracted(self):
        with _fixture_tree() as tmp:
            wording = tmp / "docs" / "style" / "wording-rules.md"
            wording.write_text(
                wording.read_text(encoding="utf-8")
                + "\n## Fenced example\n\n"
                + "```text\nRULE: this is inside a fence and must never be extracted.\n```\n",
                encoding="utf-8",
            )
            # A heading with no live RULE: line (the real one is fenced) trips heading
            # parity, which is the correct failure -- proves the fence really hid it
            # from extraction rather than merely from some other check.
            result = run_build(["--repo-root", str(tmp)])
            self.assertEqual(result.returncode, 1)
            self.assertIn("sections", result.stderr)

    def test_unbalanced_fence_is_a_hard_error(self):
        with _fixture_tree() as tmp:
            wording = tmp / "docs" / "style" / "wording-rules.md"
            lines = wording.read_text(encoding="utf-8").split("\n")
            lines.insert(len(lines) // 2, "```")
            wording.write_text("\n".join(lines), encoding="utf-8")

            result = run_build(["--repo-root", str(tmp)])
            self.assertEqual(result.returncode, 1)
            self.assertIn("unbalanced", result.stderr)
            self.assertIn("wording-rules.md", result.stderr)

            check_result = run_build(["--check", "--repo-root", str(tmp)])
            self.assertEqual(check_result.returncode, 1)
            self.assertIn("unbalanced", check_result.stderr)

    def test_reproduction_stray_fence_errors_instead_of_dropping_rules(self):
        """The exact failure mode from review: a stray fence must not silently drop
        rules past it -- it must fail the build instead of returning a partial carrier.
        """
        with _fixture_tree() as tmp:
            wording = tmp / "docs" / "style" / "wording-rules.md"
            lines = wording.read_text(encoding="utf-8").split("\n")
            lines.insert(59, "```")
            wording.write_text("\n".join(lines), encoding="utf-8")

            result = run_build(["--repo-root", str(tmp)])
            self.assertEqual(result.returncode, 1)
            self.assertIn("unbalanced", result.stderr)


class TestHeadingParity(unittest.TestCase):
    def test_missing_rule_line_in_a_section_fails_the_build(self):
        with _fixture_tree() as tmp:
            wording = tmp / "docs" / "style" / "wording-rules.md"
            text = wording.read_text(encoding="utf-8")
            # Drop exactly one RULE: line, leaving its `##` section headerless of a rule.
            lines = text.splitlines()
            heading = None
            for i, line in enumerate(lines):
                if line.startswith("RULE: "):
                    for j in range(i, -1, -1):
                        if lines[j].startswith("## "):
                            heading = lines[j]
                            break
                    del lines[i]
                    break
            wording.write_text("\n".join(lines) + "\n", encoding="utf-8")

            result = run_build(["--repo-root", str(tmp)])
            self.assertEqual(result.returncode, 1)
            self.assertIn("sections", result.stderr)
            self.assertIn(heading, result.stderr)

    def test_zero_rule_lines_in_a_section_names_that_section(self):
        """A section with zero RULE: lines raises naming that specific heading, not an
        aggregate mismatch -- the per-section attribution the aggregate check missed.
        """
        with _fixture_tree() as tmp:
            wording = tmp / "docs" / "style" / "wording-rules.md"
            text = wording.read_text(encoding="utf-8")
            lines = text.splitlines()
            heading = None
            for i, line in enumerate(lines):
                if line.startswith("RULE: "):
                    for j in range(i, -1, -1):
                        if lines[j].startswith("## "):
                            heading = lines[j]
                            break
                    del lines[i]
                    break
            wording.write_text("\n".join(lines) + "\n", encoding="utf-8")

            result = run_build(["--repo-root", str(tmp)])
            self.assertEqual(result.returncode, 1)
            self.assertIn("0 RULE", result.stderr)
            self.assertIn(heading, result.stderr)

    def test_two_rule_lines_in_a_section_names_that_section(self):
        """A section with two RULE: lines raises naming that specific heading -- the
        aggregate heading-count vs rule-count check this replaced would pass this case
        outright whenever some other section happened to be short by one.
        """
        with _fixture_tree() as tmp:
            wording = tmp / "docs" / "style" / "wording-rules.md"
            text = wording.read_text(encoding="utf-8")
            lines = text.splitlines()
            heading = None
            for i, line in enumerate(lines):
                if line.startswith("RULE: "):
                    for j in range(i, -1, -1):
                        if lines[j].startswith("## "):
                            heading = lines[j]
                            break
                    lines.insert(i, line)
                    break
            wording.write_text("\n".join(lines) + "\n", encoding="utf-8")

            result = run_build(["--repo-root", str(tmp)])
            self.assertEqual(result.returncode, 1)
            self.assertIn("2 RULE", result.stderr)
            self.assertIn(heading, result.stderr)


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

        carrier_text = CARRIER.read_text(encoding="utf-8")
        banner, _, rest = carrier_text.partition("\n\n")
        self.assertEqual(payload["hookSpecificOutput"]["additionalContext"], rest)
        self.assertNotIn(
            "GENERATED", payload["hookSpecificOutput"]["additionalContext"]
        )

    def test_additional_context_starts_with_the_style_banner(self):
        """Pins the emitter's single-line banner strip: partition("\\n\\n") only removes
        the GENERATED comment because it is exactly one line. A future multi-line banner
        would leave stray banner text ahead of the real heading without this failing.
        """
        result = subprocess.run(
            [sys.executable, str(EMIT_SCRIPT)], capture_output=True, text=True
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertTrue(
            payload["hookSpecificOutput"]["additionalContext"].startswith(
                "# Session output style"
            )
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

"""Guards for the cross-tool agent-instruction layout.

`AGENTS.md` is the canonical file; `CLAUDE.md` is a pointer at it plus a Claude-only tail;
`.claude/rules/` and `.cursor/rules/` carry path scoping whose bodies are references, not
copied prose. Four readers, one source.

Every failure this module guards is SILENT — none of them raise, and all of them leave a
file that still looks correct:

* Codex truncates its concatenated instructions at 32 KiB without notice, so the repo can
  grow past the cap and simply stop delivering the tail.
* Claude Code's import parser skips code spans and fenced blocks, so one stray backtick
  around the pointer turns it into decoration and CLAUDE.md silently carries nothing.
* Prose migrates back into CLAUDE.md over time (it has, twice), which is the bloat the
  layout exists to prevent and the thing Anthropic's docs name as the cause of Claude
  ignoring instructions.
* A rule file can point at a target that was renamed or deleted, and the tool that reads it
  reports nothing.

Sizes here are budgets, not measurements of the current files — raising one is the
deliberate, reviewable act, exactly as with the byte ceiling this replaced.
"""

import os
import re
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

# Codex concatenates AGENTS.md from the repo root down to the working directory and stops
# adding files past 32 KiB (`project_doc_max_bytes`), with no notice. Keep the whole set well
# under it so a deep directory still receives its own rules.
CODEX_CAP_BYTES = 32_768
AGENTS_SET_BUDGET_BYTES = 24_000

# CLAUDE.md is a pointer, not a document. If this needs raising, the content almost
# certainly belongs in AGENTS.md or a path-scoped rule instead.
CLAUDE_MD_MAX_LINES = 40

RULE_BODY = re.compile(r"^---\n(?P<fm>.*?)\n---\n\n@(?P<target>\S+)\n?$", re.S)


def agents_files():
    return sorted(REPO.glob("AGENTS.md")) + sorted(
        p for p in REPO.glob("*/AGENTS.md") if ".git" not in p.parts
    )


class TestCanonicalSource(unittest.TestCase):
    def test_claude_md_imports_agents_md_outside_any_code_span(self):
        """The pointer must be a bare `@AGENTS.md` at line start.

        Claude Code's import parser skips code spans and fenced blocks, so backticks around
        the pointer leave a CLAUDE.md that reads correctly to a human and imports nothing.
        """
        text = (REPO / "CLAUDE.md").read_text(encoding="utf-8")
        fenced = re.sub(r"```.*?```", "", text, flags=re.S)
        uncoded = re.sub(r"`[^`\n]*`", "", fenced)
        self.assertRegex(
            uncoded,
            r"(?m)^@AGENTS\.md\s*$",
            "CLAUDE.md must carry a bare `@AGENTS.md` import on its own line, outside "
            "backticks and fences — otherwise the import silently does not expand.",
        )

    def test_claude_md_stays_a_pointer(self):
        lines = (REPO / "CLAUDE.md").read_text(encoding="utf-8").splitlines()
        self.assertLessEqual(
            len(lines),
            CLAUDE_MD_MAX_LINES,
            f"CLAUDE.md is {len(lines)} lines against a {CLAUDE_MD_MAX_LINES}-line budget. "
            "It is loaded on every turn and is meant to be a pointer plus a Claude-only "
            "tail. Portable content belongs in AGENTS.md; directory content belongs in that "
            "directory's AGENTS.md.",
        )

    def test_no_section_heading_lives_in_both_claude_md_and_agents_md(self):
        """Duplication is the failure mode the layout exists to remove."""
        def headings(p):
            return {
                line.strip().lstrip("#").strip().lower()
                for line in p.read_text(encoding="utf-8").splitlines()
                if line.startswith("##")
            }

        shared = headings(REPO / "CLAUDE.md") & headings(REPO / "AGENTS.md")
        self.assertEqual(
            shared, set(), f"headings present in both CLAUDE.md and AGENTS.md: {sorted(shared)}"
        )


class TestCodexSizeCap(unittest.TestCase):
    def test_agents_files_fit_under_the_codex_cap(self):
        files = agents_files()
        self.assertTrue(files, "expected at least a root AGENTS.md")
        total = sum(os.stat(p).st_size for p in files)
        detail = ", ".join(f"{p.relative_to(REPO)}={os.stat(p).st_size}" for p in files)
        self.assertLess(
            total,
            AGENTS_SET_BUDGET_BYTES,
            f"AGENTS.md set is {total} bytes against a {AGENTS_SET_BUDGET_BYTES}-byte budget "
            f"(Codex hard cap {CODEX_CAP_BYTES}, truncated silently). {detail}",
        )

    def test_root_agents_md_alone_is_well_under_the_cap(self):
        """A nested directory gets root + its own; the root must leave room for the tail."""
        size = os.stat(REPO / "AGENTS.md").st_size
        self.assertLess(size, CODEX_CAP_BYTES // 2, f"root AGENTS.md is {size} bytes")


class TestRulePointers(unittest.TestCase):
    def rule_files(self):
        return sorted((REPO / ".claude/rules").glob("*.md")) + sorted(
            (REPO / ".cursor/rules").glob("*.mdc")
        )

    def test_rule_files_are_pointers_carrying_no_prose(self):
        """A rule file is scoping metadata plus one reference. Prose in it is a second copy."""
        for path in self.rule_files():
            with self.subTest(rule=str(path.relative_to(REPO))):
                m = RULE_BODY.match(path.read_text(encoding="utf-8"))
                self.assertIsNotNone(
                    m,
                    "must be exactly YAML frontmatter followed by a single @reference — "
                    "content belongs in the AGENTS.md it points at",
                )

    def test_every_rule_target_exists(self):
        """The two tools resolve a reference from different bases, so the same target is
        spelled differently in each and neither spelling works in the other.

        Claude Code documents that a relative import resolves against the file containing
        it, hence `@../../workflows/AGENTS.md` from `.claude/rules/`. Cursor's `@file`
        references resolve from the project root, hence the bare `@workflows/AGENTS.md`.
        Getting this backwards yields a rule that loads with no content and says nothing.

        (The Cursor base is convention rather than something its docs state outright. If it
        turns out to resolve from the rule file, only the base below changes.)
        """
        bases = {".claude": lambda p: p.parent, ".cursor": lambda _p: REPO}
        for path in self.rule_files():
            with self.subTest(rule=str(path.relative_to(REPO))):
                m = RULE_BODY.match(path.read_text(encoding="utf-8"))
                assert m is not None
                tool = path.relative_to(REPO).parts[0]
                target = (bases[tool](path) / m.group("target")).resolve()
                self.assertTrue(
                    target.is_file(),
                    f"points at {m.group('target')}, which does not resolve from the "
                    f"{tool} base. Nothing reports a dangling reference at load time.",
                )

    def test_frontmatter_values_are_quoted(self):
        """`description: a: b` is invalid YAML; a glob starting with `*` is too."""
        for path in self.rule_files():
            with self.subTest(rule=str(path.relative_to(REPO))):
                m = RULE_BODY.match(path.read_text(encoding="utf-8"))
                assert m is not None
                for line in m.group("fm").splitlines():
                    key, _, value = line.partition(": ")
                    if value in ("true", "false"):
                        continue
                    self.assertTrue(
                        value.startswith('"') and value.endswith('"'),
                        f"{key} must be quoted; got {value!r}",
                    )

    def test_claude_and_cursor_scope_the_same_directories(self):
        """Scoping is duplicated per tool because no cross-tool mechanism exists. That is the
        one accepted duplication, so it must at least stay consistent."""
        def globs(paths, pattern, key):
            out = {}
            for p in sorted(paths.glob(pattern)):
                m = RULE_BODY.match(p.read_text(encoding="utf-8"))
                assert m is not None, p
                for line in m.group("fm").splitlines():
                    if line.startswith(f"{key}: "):
                        out[p.stem] = line.split(": ", 1)[1].strip('"')
            return out

        self.assertEqual(
            globs(REPO / ".claude/rules", "*.md", "paths"),
            globs(REPO / ".cursor/rules", "*.mdc", "globs"),
        )


if __name__ == "__main__":
    unittest.main()

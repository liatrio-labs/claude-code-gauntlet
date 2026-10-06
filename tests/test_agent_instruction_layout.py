"""Guards for the agent-instruction files.

`AGENTS.md` is the only instruction file, at the root and in the directories that need one.
Codex and Cursor read it natively. Claude Code reads it too, at launch for the root file and
on a Read for a nested one, but only where no `CLAUDE.md` or `CLAUDE.local.md` sits in that
directory or above it. Measured on Claude Code 2.1.292 with one scratch repository per layout,
each file carrying its own codeword: with no `CLAUDE.md` both the root and the nested
`AGENTS.md` loaded, and a `CLAUDE.md` beside an `AGENTS.md` loaded in its place.

The failures guarded here are silent: a shadowing file hides every rule from one tool, a rule
names code that no longer exists, and the files grow one reasonable line at a time.

This module stays stdlib `unittest`: the pre-commit hook that runs it lives in CI's lint job,
which installs no pytest.
"""

import re
import subprocess
import unittest
from pathlib import Path
from typing import ClassVar

REPO = Path(__file__).resolve().parents[1]

# A ratchet pinned at the measured size with no headroom, because a cushion is a smaller
# quantity of the thing being prevented. A cut or an equal-size correction passes; only net
# growth trips it, and raising the number on a line every reviewer sees is the mechanism.
# Before raising it, apply the test at the foot of the root AGENTS.md to the addition.
AGENTS_SET_BUDGET_BYTES = 4_131

SHADOWING_NAMES = ("CLAUDE.md", "CLAUDE.local.md", ".claude/CLAUDE.md")


def tracked(*patterns):
    out = subprocess.run(
        ["git", "ls-files", "-z", "--", *patterns],
        cwd=REPO,
        capture_output=True,
        check=True,
    ).stdout.decode()
    return [path for path in out.split("\0") if path]


def instruction_files():
    return tracked("AGENTS.md", "*/AGENTS.md")


class TestNothingShadowsAgentsMd(unittest.TestCase):
    def test_no_claude_md_exists(self):
        """Tracked anywhere, or on disk beside or above an `AGENTS.md`.

        The on-disk half matters because `CLAUDE.local.md` is normally personal and
        untracked, and a session started in any subdirectory counts files above it.
        """
        names = {Path(name).name for name in SHADOWING_NAMES}
        shadows = {path for path in tracked() if Path(path).name in names}
        for path in instruction_files():
            for directory in Path(path).parents:
                shadows.update(
                    (directory / name).as_posix()
                    for name in SHADOWING_NAMES
                    if (REPO / directory / name).exists()
                )
        self.assertEqual(
            sorted(shadows),
            [],
            "Claude Code stops reading AGENTS.md once one of these exists at or above "
            "the working directory. Put the content in AGENTS.md instead.",
        )


class TestSizeRatchet(unittest.TestCase):
    def test_instruction_files_do_not_grow(self):
        sizes = {path: (REPO / path).stat().st_size for path in instruction_files()}
        total = sum(sizes.values())
        self.assertLessEqual(
            total,
            AGENTS_SET_BUDGET_BYTES,
            f"AGENTS.md files grew to {total} bytes against a ratchet pinned at "
            f"{AGENTS_SET_BUDGET_BYTES}. Justify the addition at the constant, then raise "
            f"it in the same commit. {sizes}",
        )


class TestRulesFileQuotations(unittest.TestCase):
    """Quoted spans attributed to AGENTS.md/CLAUDE.md must exist in an instruction file."""

    QUOTE_NEAR_RULES: ClassVar[re.Pattern[str]] = re.compile(
        r"`?(?:[A-Za-z0-9_./-]+/)?(?:CLAUDE|AGENTS)\.md`?"
        r"\s+(?:says\s+|states\s+|already applies[^\n(]{0,100}\(\s*)?"
        r'["\u201c]([^"\u201d\n]{12,})["\u201d]',
        re.IGNORECASE,
    )
    QUOTE_EXEMPTIONS: ClassVar[set[tuple[str, str]]] = {
        (
            "skills/code-gauntlet/references/false-positive-exclusions.md",
            "all functions must have JSDoc",
        ),
    }

    def _rules_corpus(self):
        files = (
            subprocess.run(
                [
                    "git",
                    "ls-files",
                    "-z",
                    "--",
                    "AGENTS.md",
                    "*/AGENTS.md",
                ],
                cwd=REPO,
                capture_output=True,
                check=True,
            )
            .stdout.decode()
            .split("\0")
        )
        return "\n".join(
            (REPO / path).read_text(encoding="utf-8") for path in files if path
        )

    def _docs(self):
        paths = (
            subprocess.run(
                ["git", "ls-files", "-z", "--", "skills", "agents"],
                cwd=REPO,
                capture_output=True,
                check=True,
            )
            .stdout.decode()
            .split("\0")
        )
        return [
            path
            for path in paths
            if path.endswith(".md")
            and (
                path.startswith("skills/")
                or (path.startswith("agents/") and path.count("/") == 1)
            )
        ]

    def test_rules_file_quotations_resolve(self):
        corpus = self._rules_corpus()
        missing = []
        for doc in self._docs():
            text = (REPO / doc).read_text(encoding="utf-8")
            for quote in self.QUOTE_NEAR_RULES.findall(text):
                if (doc, quote) in self.QUOTE_EXEMPTIONS:
                    continue
                if quote not in corpus:
                    missing.append((doc, quote[:80]))
        self.assertEqual(
            missing,
            [],
            f"rules quotations not found in any AGENTS.md: {missing}",
        )


class TestClaimsResolve(unittest.TestCase):
    """Every file and symbol an instruction file names must exist in this tree.

    A rule naming code that does not exist sends an agent looking for it, and nothing
    reports the dangling reference. Hits inside the instruction files themselves do not
    count, so a missing symbol cannot vouch for itself.
    """

    FILES: ClassVar[list[str]] = [*instruction_files(), "REVIEW.md"]

    # Backticked prose terms that are English, schema field names, or host globals named
    # precisely because they are ABSENT — none of them are repo symbols.
    NOT_SYMBOLS: ClassVar[set[str]] = {
        "structuredClone",
        "setTimeout",
        "console",
        "process",
        "Buffer",
        "TextEncoder",
        "package.json",
        "node_modules",
    }

    def repo_files(self):
        out = subprocess.run(
            ["git", "ls-files"],
            cwd=REPO,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        return set(out.stdout.split())

    def test_referenced_paths_exist(self):
        tracked = self.repo_files()
        basenames = {Path(p).name for p in tracked}
        pattern = re.compile(r"`([A-Za-z0-9_./-]+\.(?:py|js|md|json|yaml|yml))`")
        for doc in self.FILES:
            text = (REPO / doc).read_text(encoding="utf-8")
            for ref in sorted(set(pattern.findall(text))):
                if ref in self.NOT_SYMBOLS or "*" in ref:
                    continue
                with self.subTest(doc=doc, ref=ref):
                    self.assertTrue(
                        ref in tracked
                        or Path(ref).name in basenames
                        or (REPO / Path(doc).parent / ref).exists(),
                        f"{doc} names {ref}, which is not in this tree",
                    )

    def test_referenced_symbols_exist(self):
        """A grep, deliberately — the claim is only that the name occurs in the code."""
        pattern = re.compile(r"`([a-z][a-zA-Z0-9_]{4,}|[A-Z][A-Z0-9_]{4,})`")
        docs = set(self.FILES)
        for doc in self.FILES:
            text = (REPO / doc).read_text(encoding="utf-8")
            for sym in sorted(set(pattern.findall(text))):
                if sym in self.NOT_SYMBOLS:
                    continue
                with self.subTest(doc=doc, symbol=sym):
                    found = subprocess.run(
                        ["git", "grep", "-l", "--", sym],
                        cwd=REPO,
                        capture_output=True,
                        text=True,
                        encoding="utf-8",
                    ).stdout.split()
                    # A hit in the instruction files themselves proves nothing.
                    real = [f for f in found if f not in docs]
                    self.assertTrue(
                        real,
                        f"{doc} names `{sym}`, which appears nowhere in the code. If it "
                        "lands with an unmerged branch, document it in that branch.",
                    )


if __name__ == "__main__":
    unittest.main()

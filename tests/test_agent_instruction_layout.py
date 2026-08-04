"""Guards for the cross-tool agent-instruction layout.

`AGENTS.md` is canonical — the file Codex and Cursor read natively. Root `CLAUDE.md` is a
pointer at it plus a Claude-only tail. Each directory's `CLAUDE.md` is a GENERATED twin of
its `AGENTS.md` (`scripts/sync_agent_rules.py`), because Claude Code's on-demand loader
injects a subdirectory memory file verbatim and does not expand `@imports` — measured, and
the reason two earlier pointer-based designs were thrown away.

Every failure guarded here is SILENT. None raise, and all leave a file that still looks
correct to a human:

* Codex truncates its concatenated instructions at 32 KiB with no notice.
* Claude's launch-time import parser skips code spans, so one backtick around the root
  pointer turns it into decoration and CLAUDE.md carries nothing.
* A generated twin drifts from its source, and the two tools then read different rules.
* An `AGENTS.md` is added with no twin, so Cursor and Codex see it and Claude never does.
* Prose migrates back into root CLAUDE.md, which is the bloat the layout removed.

Budgets here are budgets, not measurements of the current files. Raising one is the
deliberate, reviewable act.
"""

import os
import re
import subprocess
import sys
import unittest
from pathlib import Path
from typing import ClassVar

REPO = Path(__file__).resolve().parents[1]

# Codex concatenates AGENTS.md from the repo root down to the working directory and stops
# adding files past 32 KiB (`project_doc_max_bytes`), silently. That is the hard ceiling.
CODEX_CAP_BYTES = 32_768

# THESE TWO ARE RATCHETS, PINNED AT THE MEASURED SIZE WITH NO HEADROOM.
#
# This property was in the byte-budget test these guards replaced, and dropping it was a
# regression: the replacement checked shape and truth (symbols resolve, twins are fresh,
# the Codex cap is respected) but carried ~9 KB of slack, so the first PR after it landed
# grew the instruction files and nothing asked anyone to think about it.
#
# The reasoning from the original, which still holds: a cushion is just a smaller quantity
# of the exact thing being prevented. A reduction always passes. So does a correction that
# trades text of equal or smaller size. Only NET GROWTH trips it — and then someone must
# raise a number on a line every reviewer sees, which is the whole mechanism.
#
# Raising one is a deliberate act, not a formality. Before you do, apply the two-part test
# to the addition: (1) does it fail to be derivable from the code, and (2) is it NOT
# already stated by a comment at the site that owns it? If either answer is no, the content
# belongs in a code comment. If both are yes, raise the number in the same commit and say
# what the addition buys.
#
# Measured 2026-07-30. Update these ONLY alongside such a justification.
# Raised 14_627 -> 14_893 (2026-07-30, #55 audit): two corrections of false enforcement
# claims — which tests delete which sandbox-absent globals, and which duplicated copies are
# byte-checked vs presence-checked. Both name a cross-file guarantee boundary no single code
# site owns; the old, shorter text was wrong about what is enforced.
# Raised 14_893 -> 15_170 (2026-07-31, owner directive on PR #120): the docs-scratch
# policy line. Enforcement is structural (tests/test_docs_registry.py), but that test
# fires only after scratch is written; the line is what steers an agent at write time
# to the PR/issue thread instead. Not derivable from code, no single owning site.
# Raised 15_170 -> 16_273 (2026-08-03, #102): coverage-gate reproduction commands,
# threshold policy, and the stdlib carve-out for pinned CI tooling. All three are
# process/boundary statements no single code site owns (the gate lives in ci.yml;
# the carve-out is a cross-surface rule about what may import what; the floor policy
# is a merge convention). Not derivable from code.
# Raised 16_273 -> 17_291 (2026-08-03, #103): self-contained JS coverage gate,
# measured floors, and drift/presence diagnostics shared with CI. These are
# cross-surface process guarantees no single code site owns. Re-pinned from
# 17_343 after the floors-paragraph re-pin shortened the provisional wording.
# Raised 17_291 -> 17_485 (2026-08-03, #127): post_review.py must never grow a second
# parse of the marker it writes — its GitLab summary-note idempotency check delegates to
# detect_prior_review, the signal's only reader. The rule is a direction of dependency
# between two modules; neither file's code states it, and the import alone reads as a
# convenience rather than a boundary.
# Raised 17_485 -> 17_513 (2026-08-04, #135): the Python coverage floor's provenance
# note must name the measurement its value came from (raised to 91.7 against PR #135's
# 3.12 CI reading of 92.69, per the floor policy in the same paragraph) — a floor whose
# stated provenance no longer matches its value is exactly the unverifiable claim the
# repo's own rules prohibit.
# Raised 17_513 -> 18_916 (2026-08-04, #105): workflows/AGENTS.md JS lint
# section (Biome pin, formatter-off rationale) and deferred-rules table.
AGENTS_SET_BUDGET_BYTES = 18_916
CLAUDE_MD_MAX_BYTES = 856

# Root CLAUDE.md is a pointer, not a document. The line cap is a shape bound and keeps its
# slack deliberately — the byte ratchet above is what stops growth; this stops a wall of
# short lines that would satisfy the byte count only by being terse.
CLAUDE_MD_MAX_LINES = 40


def agents_dirs():
    """Directories carrying an AGENTS.md, excluding the repo root."""
    return [
        p.parent
        for p in sorted(REPO.glob("*/AGENTS.md"))
        if not p.parent.name.startswith(".")
    ]


class TestCanonicalPointer(unittest.TestCase):
    def test_root_claude_md_imports_agents_md_outside_any_code_span(self):
        """Must be a bare `@AGENTS.md` at line start.

        Claude Code's import parser skips code spans and fenced blocks, so backticks around
        the pointer leave a CLAUDE.md that reads correctly to a human and imports nothing.
        This is the one import in the layout that DOES expand — it is launch-time and
        root-level, unlike the on-demand subdirectory path.
        """
        text = (REPO / "CLAUDE.md").read_text(encoding="utf-8")
        uncoded = re.sub(r"`[^`\n]*`", "", re.sub(r"```.*?```", "", text, flags=re.S))
        self.assertRegex(
            uncoded,
            r"(?m)^@AGENTS\.md\s*$",
            "root CLAUDE.md must carry a bare `@AGENTS.md` on its own line, outside "
            "backticks and fences — otherwise the import silently does not expand.",
        )

    def test_root_claude_md_stays_a_pointer(self):
        text = (REPO / "CLAUDE.md").read_text(encoding="utf-8")
        lines, size = len(text.splitlines()), len(text.encode("utf-8"))
        self.assertLessEqual(
            lines, CLAUDE_MD_MAX_LINES, f"root CLAUDE.md is {lines} lines"
        )
        self.assertLessEqual(
            size,
            CLAUDE_MD_MAX_BYTES,
            f"root CLAUDE.md is {size} bytes against a {CLAUDE_MD_MAX_BYTES}-byte budget. "
            "It is loaded on every turn. Portable content belongs in AGENTS.md; "
            "directory content belongs in that directory's AGENTS.md.",
        )

    def test_no_section_heading_lives_in_both_root_files(self):
        def headings(p):
            return {
                line.strip().lstrip("#").strip().lower()
                for line in p.read_text(encoding="utf-8").splitlines()
                if line.startswith("##")
            }

        shared = headings(REPO / "CLAUDE.md") & headings(REPO / "AGENTS.md")
        self.assertEqual(shared, set(), f"headings in both files: {sorted(shared)}")


class TestCodexSizeCap(unittest.TestCase):
    def test_canonical_set_fits_under_the_codex_cap(self):
        """Only AGENTS.md files count — the generated twins are the same bytes again."""
        files = [REPO / "AGENTS.md"] + [d / "AGENTS.md" for d in agents_dirs()]
        total = sum(os.stat(p).st_size for p in files)
        detail = ", ".join(f"{p.relative_to(REPO)}={os.stat(p).st_size}" for p in files)
        # LessEqual, not Less: the ratchet is pinned AT the measured size, so the current
        # tree passes exactly and one added byte does not.
        self.assertLessEqual(
            total,
            AGENTS_SET_BUDGET_BYTES,
            f"AGENTS.md set grew to {total} bytes against a ratchet pinned at "
            f"{AGENTS_SET_BUDGET_BYTES} (Codex hard cap {CODEX_CAP_BYTES}, truncated "
            f"silently). This is the guard asking you to justify the addition, not a "
            f"limit you have reached — apply the two-part test at the constant, then "
            f"raise it in the same commit. {detail}",
        )

    def test_root_agents_md_leaves_room_for_a_nested_file(self):
        size = os.stat(REPO / "AGENTS.md").st_size
        self.assertLess(size, CODEX_CAP_BYTES // 2, f"root AGENTS.md is {size} bytes")


class TestGeneratedTwins(unittest.TestCase):
    """The twins are the ONLY way directory rules reach Claude. Drift is silent."""

    def test_every_agents_md_has_a_twin(self):
        for directory in agents_dirs():
            with self.subTest(directory=directory.name):
                self.assertTrue(
                    (directory / "CLAUDE.md").is_file(),
                    f"{directory.name}/AGENTS.md has no CLAUDE.md twin, so Cursor and "
                    "Codex read these rules and Claude Code never does. Run "
                    "scripts/sync_agent_rules.py.",
                )

    def test_no_twin_is_stale(self):
        """Delegates to the generator's own --check so the two cannot disagree."""
        result = subprocess.run(
            [sys.executable, str(REPO / "scripts" / "sync_agent_rules.py"), "--check"],
            capture_output=True,
            text=True,
        )
        self.assertEqual(
            result.returncode,
            0,
            f"generated twins are stale: {result.stderr.strip()}",
        )

    def test_twin_carries_the_do_not_edit_banner(self):
        """The banner is an HTML comment: stripped before injection, so it is free."""
        for directory in agents_dirs():
            with self.subTest(directory=directory.name):
                head = (directory / "CLAUDE.md").read_text(encoding="utf-8")[:400]
                self.assertIn("GENERATED from AGENTS.md", head)
                self.assertTrue(head.lstrip().startswith("<!--"))


class TestCollectorDedup(unittest.TestCase):
    """A repo shipping CLAUDE.md as a copy of AGENTS.md must not pay for both.

    This layout IS that shape, so the collector has to collapse it — otherwise every
    review of such a repo carries every rule twice. Exercises the real collector rather
    than its helper: an earlier version of this test asserted only that the twin and its
    source normalise alike, and stayed green with dedup disabled outright.
    """

    def collect(self):
        sys.path.insert(0, str(REPO))
        import json
        import tempfile

        changed = [str(d.name) + "/x" for d in agents_dirs()]
        with tempfile.TemporaryDirectory() as tmp:
            listing = Path(tmp) / "changed.json"
            listing.write_text(json.dumps(changed), encoding="utf-8")
            out = Path(tmp) / "rules.md"
            result = subprocess.run(
                [
                    sys.executable,
                    str(REPO / "scripts" / "collect_project_rules.py"),
                    "--repo-root",
                    str(REPO),
                    "--changed-files",
                    str(listing),
                    "--out",
                    str(out),
                ],
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            return json.loads(result.stdout)

    def test_each_rule_set_is_collected_exactly_once(self):
        from scripts.collect_project_rules import _effective

        report = self.collect()
        seen = {}
        for source in report["sources"]:
            text = (REPO / source["path"]).read_text(encoding="utf-8")
            key = _effective(text)
            self.assertNotIn(
                key,
                seen,
                f"{source['path']} duplicates {seen.get(key)} — the agents would read "
                "these rules twice and the payload pays for both",
            )
            seen[key] = source["path"]

    def test_the_twins_are_the_pairs_being_collapsed(self):
        """Precondition: without dedup this repo really would double-count."""
        report = self.collect()
        skipped = {
            s["path"] for s in report["skipped"] if s["reason"] == "duplicate_of"
        }
        for directory in agents_dirs():
            with self.subTest(directory=directory.name):
                pair = {f"{directory.name}/AGENTS.md", f"{directory.name}/CLAUDE.md"}
                self.assertTrue(
                    pair & skipped,
                    f"neither half of {sorted(pair)} was collapsed",
                )


class TestRulesFileQuotations(unittest.TestCase):
    """Quoted spans attributed to AGENTS.md/CLAUDE.md must exist in a rules file."""

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
                    "CLAUDE.md",
                    "*/AGENTS.md",
                    "*/CLAUDE.md",
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
            f"rules quotations not found in any AGENTS/CLAUDE: {missing}",
        )


class TestClaimsResolve(unittest.TestCase):
    """Every file and symbol an instruction file names must exist in THIS tree.

    Instruction files are read as fact by four different tools. A rule naming a function
    that does not exist sends an agent looking for it, and nothing reports the dangling
    reference — the same silent-failure shape as the rest of this module.

    This caught three real defects on the branch that introduced it: a cross-runtime
    constant, a proof-kind rule, and a whole section on health derivation, all describing
    code that lived only on an unmerged branch. Documenting code before it lands reads
    exactly like documenting code that shipped.

    Deliberately no exclusion list for this module: naming a missing symbol here would let
    it vouch for itself, so this docstring describes those defects without spelling them.
    """

    FILES: ClassVar[list[str]] = [
        "AGENTS.md",
        "CLAUDE.md",
        "REVIEW.md",
        "workflows/AGENTS.md",
        "scripts/AGENTS.md",
        "agents/AGENTS.md",
    ]

    # Backticked prose terms that are English, schema field names, or host globals named
    # precisely because they are ABSENT — none of them are repo symbols.
    NOT_SYMBOLS: ClassVar[set[str]] = {
        "description",
        "evidence",
        "suggestion",
        "severity",
        "confidence",
        "dimension",
        "origin",
        "criticality",
        "findings",
        "complete",
        "total_seen",
        "markdown",
        "optimized",
        "realpath",
        "structuredClone",
        "setTimeout",
        "queueMicrotask",
        "console",
        "process",
        "Buffer",
        "TextEncoder",
        "TextDecoder",
        "package.json",
        "node_modules",
        "noControlCharactersInRegex",  # Biome rule id in deferred-rules table
    }

    def repo_files(self):
        out = subprocess.run(
            ["git", "ls-files"], cwd=REPO, capture_output=True, text=True
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
                    ).stdout.split()
                    # Hits in the instruction files themselves prove nothing: the twins
                    # are copies, so a symbol could otherwise vouch for itself.
                    real = [
                        f
                        for f in found
                        if f not in docs and not f.endswith("/CLAUDE.md")
                    ]
                    self.assertTrue(
                        real,
                        f"{doc} names `{sym}`, which appears nowhere in the code. If it "
                        "lands with an unmerged branch, document it in that branch.",
                    )


if __name__ == "__main__":
    unittest.main()

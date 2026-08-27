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
# Raised 18_916 -> 19_079 (2026-08-04, #105): reword tooling-boundary bullet —
# shipped runtime is language globals + host-injected args only, not Node builtins.
# Raised 19_079 -> 19_282 (2026-08-04, #110): restore await_workflow stdout-payload
# rationale (Bash caller / model-context / distinct keys) wrongly compressed earlier in
# #110; add scripts CLI stdout/stderr contract pointing at script_io.write_result.
# Raised 19_282 -> 19_751 (2026-08-09, #137): build.js now fails on an import strip()
# cannot safely drop rather than silently stripping (or emitting) it. The mechanism is
# code, but the rule an author needs *before* writing the import is not — build.js's
# comment is only read after the failure, and neither the Biome gate nor the
# bundle-fresh check reports one, so nothing else in the tree tells them which imports
# are legal in src/. The rule takes two clauses, not one: the specifier must be a
# relative sibling AND the `from` clause must be on the same line, because a
# side-effect import satisfies the first and still ships into the bundle verbatim.
# Raised 19_751 -> 19_812 (2026-08-10, #171): floor ratchet to newly-measured CI
# headroom — Python 91.7 -> 91.8 (CI: 92.77), JS branches 82.3 -> 83.3 (CI: 84.23),
# JS functions 97 -> 97.2 (CI: 98.18); JS lines stayed at 98, headroom under 1.0 pp.
# The floor policy requires each value's provenance note name the measurement it
# came from, so a stale note is the unverifiable claim the repo's own rules prohibit.
# Raised 19_812 -> 20_118 (2026-08-11, #50b): workflows/AGENTS.md "The verify boundary"
# gained a bullet documenting the slice-input projection (VERIFY_SLICE_FIELDS /
# _SLICE_INPUT_FIELDS lockstep) — the rule an author needs before adding a field the
# script never reads is not otherwise stated anywhere in the tree.
# Raised 20_118 -> 20_357 (2026-08-11, #50b review fix): the same bullet was reworded to
# name the read-site scan mechanism (a lockstep test plus a source scan, not prose) and
# the three exemption lists it checks against — the rule now matches what actually
# enforces it, which is worth the extra bytes for the same reason the first raise was.
# Raised 20_357 -> 20_420 (2026-08-11, #50b review fix): the bullet undercounted the
# exemptions by one (omitted _NUMERIC_FIELDS, the line/end_line numeric-coercion loop);
# corrected to name all three.
# Raised 20_420 -> 20_436 (2026-08-18, #62): the coverage-floor ratchet (Python 92.1,
# JS branches 85.7 / functions 97.4) grew AGENTS.md's floors-and-provenance paragraph —
# the exact gate values Claude must pass are operational policy, not decoration.
# Raised 20_436 -> 20_685 (2026-08-18): the "Session output style" section points at the
# canonical rule sources and the generator that regenerates the SessionStart carrier —
# no code site states where those rules live or how the carrier gets refreshed.
# Lowered 20_685 -> 20_563 (2026-08-18, PR #216 review fix): "Session output style"
# shrunk to a heading plus a single pointer at scripts/build_style_artifacts.py, whose
# own docstring already states the mechanics the section had restated.
# Raised 20_563 -> 20_578 (2026-08-19, #63): `suggested_fix_code` added to the Canonical
# fields bullet. The bullet is the human index of registry.js FINDING_PROP_TYPES that
# tests/test_dimensions_registry.py pins to the set of keys there, so a newly-declared
# canonical field must appear on this line for that pin to keep holding true — no
# registry.js comment states the human-readable enumeration this line exists to give a
# reader who has not opened that file.
# Raised 20_578 -> 20_579 (2026-08-19, #63 round-1 F9): the Python coverage floor ratchet
# (92.1 -> 92.4) grew its provenance note by one digit ("93.1" -> "93.37", the #63 PR's
# actual measurement) — the floor policy requires the note name the measurement it came
# from, and a floor whose provenance doesn't match its value is the unverifiable claim
# the repo's own rules prohibit.
# Raised 20_579 -> 20_605 (2026-08-19, #219): the Python coverage floor ratchet (scripts
# 92.4 -> 92.5, bench 87 -> 87.5) rewrote the floors-and-provenance paragraph to name the
# #219 PR CI measurement (93.37 scripts, 88.29 bench) in place of the #63 one — the floor
# policy requires the note name the measurement it came from, and a stale note is the
# unverifiable claim the repo's own rules prohibit.
# Raised 20_605 -> 20_628 (2026-08-20, #226): scripts/AGENTS.md's "Always emit exactly
# one receipt line" bullet now names report_patches.py as the third script whose
# main() falls back to a hand-built minimal receipt if the real one will not
# serialize — a cross-script convention no single code site owns, so the rule an
# author needs before adding a fourth receipt-line script is not otherwise stated
# anywhere in the tree. (report_patches.py's own module docstring already states
# what the script itself does, so no second bullet restates it here.)
# Raised 20_628 -> 20_700 (2026-08-24, #231): the Python coverage floor ratchet (scripts
# 92.5 -> 92.6) rewrote the floors-and-provenance paragraph to name the #231 PR CI
# measurement (93.54) alongside the existing #219 note — the floor policy requires the
# note name the measurement it came from, and a stale note is the unverifiable claim the
# repo's own rules prohibit.
# Raised 20_700 -> 20_761 (2026-08-24, #236): the Python coverage floor ratchet (scripts
# 92.6 -> 92.7) extended the floors-and-provenance paragraph with the #236 PR CI
# measurement (93.67) — same policy, same shape as the #219/#231 raises above.
# Raised 20_761 -> 21_004 (2026-08-25, #218): agents/AGENTS.md's required-semantics bullet
# now names `requiredWhenDimension` — a field required only within its own dimension, on a
# first-party-direct dispatch, contract-enforced elsewhere. No single code site states this
# for an agent-contract author: registry.js's own comment is written for the workflows
# codebase, not for someone about to write an agent .md contract, which is this bullet's
# whole reason to exist (the same gap the requiredExtra half of the same bullet already
# closes for that mechanism).
# Raised 21_004 -> 21_061 (2026-08-26, #213/#249): the JS coverage floor ratchet (lines
# 98 -> 98.1, branches 85.7 -> 86.3, functions 97.4 -> 97.5) extended the
# floors-and-provenance paragraph to name the #249 PR CI measurement (99.01/87.25/98.48) —
# same policy, same shape as the #218/#231/#236 Python-floor raises above.
# Raised 21_061 -> 21_161 (2026-08-27, #251): the JS coverage floor ratchet (branches
# 86.3 -> 86.5, functions 97.5 -> 97.6; lines unchanged at 98.1) extended the
# floors-and-provenance paragraph to name the #251 PR CI measurement (99.02/87.47/98.51) —
# same policy, same shape as the #213/#249 raise above.
# Raised 21_161 -> 21_234 (2026-08-27, #255): the JS coverage floor ratchet (branches
# 86.5 -> 86.6; lines/functions unchanged) extended the floors-and-provenance paragraph
# to name the #255 PR CI measurement (87.60) — same policy, same shape as the raises
# above: the floor-history convention requires every prior floor bump be citable to the
# measurement that justified it, and an uncited bump is the unverifiable claim the repo's
# own rules prohibit.
AGENTS_SET_BUDGET_BYTES = 21_234
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

"""Byte budget for CLAUDE.md (issue #46).

CLAUDE.md loads into EVERY session unconditionally, so its size is a recurring
per-session cost, not a one-time one. The file grows by default: dense, well-written
prose matching its existing voice reads as belonging there, and nothing at the moment
of the edit forces the question of whether it earns its place.

Twice now — PR #71 and PR #76 — a block was added that duplicated comments already
sitting at the code they described, and was caught only by measuring it afterwards.
The second time was in the same session that wrote the governing rule down, one PR
later. So a written rule is not the mechanism; it asks an author mid-feature to
re-derive and re-apply a semantic test from memory, which is exactly what failed.

This test does not and cannot check that rule. "Is this derivable from the code, and
is it already adequately stated by a comment at the site?" is a judgment call, not a
computation — and per CLAUDE.md's own standing discipline, a guard must be a
structural property rather than a phrase count or a model prompt doing code's job.
What this buys is narrower and sufficient: the judgment call can no longer be skipped
SILENTLY. Growth stops the suite until someone raises the ceiling below, in the same
PR, on a line every reviewer sees.

The ceiling is pinned with NO headroom on purpose. A 2% cushion on a 24 KB file is
~500 bytes — a quarter of the block that prompted this — so any cushion is just a
smaller quantity of the exact thing being prevented. A reduction always passes, and so
does a correction that trades text of equal or smaller size; only NET GROWTH trips it.
"""

import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
CLAUDE_MD = REPO / "CLAUDE.md"

# Measured 2026-07-28 at 23,982 bytes / 115 lines. Raising this is the deliberate,
# reviewable act the guard exists to force — not a formality. Before you do:
#   1. Does the new content fail to be derivable from the code?
#   2. Is it NOT already adequately stated by a comment at the site that owns it?
# If either answer is no, the content belongs in a code comment, not here.
#
# Raised 2026-07-29 to 24,469 (+487 bytes) for issue #49: a new "Scripts" bullet
# documenting `scripts/collect_project_rules.py`'s existence and its `@import`
# repo-confinement contract. Neither part of the two-part test lets this sit in
# a code comment instead — the rule the byte ceiling exists to enforce is about
# a *behavioral* contract (realpath confinement, depth/size bounds before `open`)
# spanning a new script and several docs, not something derivable by reading the
# script alone, and CLAUDE.md is where every other cross-cutting script rule in
# this file already lives (stdlib-only, language-agnostic, repo-root-for-searches
# — the three bullets directly above this one).
#
# Raised 2026-07-29 to 26,209 (+1,740 bytes) for issue #25 PR2 (verify delta echo): a
# "Verify boundary" section carrying four rules, each of which SPANS two files and so
# cannot sit in a comment at any one site — which is exactly what the two-part test
# asks. (1) `_DELTA_FIELDS` in scripts/verify_findings.py and `DELTA_KEYS` in
# workflows/src/stages.js are one list in two runtimes, walked in the same order to
# build a checksummed canonical form. (2) `result.deltas` must stay the first key of
# the script's `result`, because of a platform property of the READER (a length-capped
# Read with no truncation notice) that the writing script cannot state alone.
# (3) `agent` is deleted at the join, with the measured recall evidence for why.
# (4) the checksum reuses assemble_artifacts.py's pair rather than growing a third copy.
# The first draft of this section ran to +4,292 bytes; it was cut by ~60% to exactly
# these cross-file rules, and everything derivable from one site — the incident history,
# the excluded audit fields, the not-authentication framing — was left at that site.
CLAUDE_MD_BYTE_CEILING = 26_209


class TestClaudeMdBudget(unittest.TestCase):
    def test_claude_md_stays_within_its_byte_budget(self):
        size = len(CLAUDE_MD.read_bytes())
        self.assertLessEqual(
            size,
            CLAUDE_MD_BYTE_CEILING,
            f"CLAUDE.md is {size} bytes, {size - CLAUDE_MD_BYTE_CEILING} over the "
            f"{CLAUDE_MD_BYTE_CEILING}-byte budget pinned in tests/test_claude_md_budget.py "
            "(issue #46). This file loads into every session, so the growth is a recurring "
            "cost. Before raising CLAUDE_MD_BYTE_CEILING, apply the two-part test: (1) does "
            "the new content fail to be derivable from the code, and (2) is it NOT already "
            "adequately stated by a comment at the site? If either answer is no, put it in a "
            "code comment instead. If both are yes, raise the ceiling in the same PR.",
        )

    def test_budget_is_pinned_at_or_below_the_measured_size(self):
        # A ceiling set far above the real size silently re-opens the gap it closes, which
        # is how this guard would rot: one generous bump and it never fires again. Keep the
        # slack at zero — every raise should be exactly as large as the content that earned
        # it, so the diff shows the true cost.
        size = len(CLAUDE_MD.read_bytes())
        self.assertLessEqual(
            CLAUDE_MD_BYTE_CEILING - size,
            0,
            f"CLAUDE_MD_BYTE_CEILING ({CLAUDE_MD_BYTE_CEILING}) sits "
            f"{CLAUDE_MD_BYTE_CEILING - size} bytes above CLAUDE.md's actual size ({size}). "
            "That slack is room for unexamined growth. Lower the ceiling to the measured size.",
        )

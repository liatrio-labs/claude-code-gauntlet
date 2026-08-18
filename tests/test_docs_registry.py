"""Guard: the tracked documentation surface is an allowlist, not a landing zone.

Session scratch — design memos, implementation plans, handoff notes — kept landing
as tracked ``docs/*.md`` files (PR #120 removed four across two PRs). The gitignore
already quarantines the *known* scratch homes (``docs/superpowers/``,
``deep-review-*.md``, ``.superpowers/``), but an ignore list only blocks names it
anticipated; scratch written one directory up, under a fresh name, lands silently.
The durable homes for that material are the PR description and the issue thread.
This guard closes the gap from the other side: every tracked doc is a deliberate,
reviewable act — add the path to the allowlist below in the same commit, with a
reason a reviewer can weigh.

Scope: git-tracked files only (``git ls-files``), so gitignored local artifacts
never fail the suite.
"""

import subprocess
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

# Root-level markdown: community-health and contract files only. CHANGELOG.md is
# semantic-release-generated. Review outputs (deep-review-*.md and kin) are
# gitignored run artifacts and must never be tracked.
ROOT_MD_ALLOW = {
    "AGENTS.md",
    "CHANGELOG.md",
    "CLAUDE.md",
    "CODE_OF_CONDUCT.md",
    "CONTRIBUTING.md",
    "PRIVACY.md",
    "README.md",
    "REVIEW.md",
    "SECURITY.md",
}

# Files directly under docs/: living registries, required audit artifacts, and the
# maintainer standard. Entries may predate their file landing (an open PR may add
# one); the guard is subset-only in that direction on purpose.
DOCS_ALLOW = {
    "docs/duplication-register.md",  # living register, required by #55
    "docs/engineering-audit-2026-07.md",  # point-in-time audit artifact, required by #55
    "docs/machine-parsed-strings.md",  # living registry, required by #37 (PR #119)
    "docs/maintainer-issues.md",  # maintainer work-queue standard
    "docs/v3-residue-audit-2026-07.md",  # point-in-time audit artifact, required by #37 (PR #119)
    "docs/style/wording-rules.md",  # canonical output-style rule source
    "docs/style/cadence-rules.md",  # canonical output-style rule source
    "docs/style/session-context.md",  # generated session-output carrier
}

# Subtrees under docs/ with their own curated index; markdown only inside.
DOCS_ALLOWED_SUBTREES = ("docs/research/",)

POLICY = (
    "Session design/plan/handoff scratch never lands in the tree — it belongs in the "
    "PR description or issue thread. A genuinely durable doc is added to the allowlist "
    "in tests/test_docs_registry.py in the same commit, with a reason."
)


def tracked(pathspec):
    out = subprocess.run(
        ["git", "ls-files", "--", pathspec],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return [line for line in out.splitlines() if line]


class TestDocsRegistry(unittest.TestCase):
    def test_every_tracked_docs_file_is_allowlisted(self):
        offenders = []
        for path in tracked("docs"):
            if path.startswith(DOCS_ALLOWED_SUBTREES):
                if not path.endswith(".md"):
                    offenders.append(f"{path} (non-markdown inside a docs subtree)")
            elif path not in DOCS_ALLOW:
                offenders.append(path)
        self.assertEqual(
            offenders,
            [],
            f"tracked under docs/ but not in the registry allowlist: {offenders}. {POLICY}",
        )

    def test_root_markdown_is_allowlisted(self):
        offenders = [
            path
            for path in tracked("*.md")
            if "/" not in path and path not in ROOT_MD_ALLOW
        ]
        self.assertEqual(
            offenders,
            [],
            f"tracked root-level markdown outside the allowlist: {offenders}. "
            f"Review-run outputs stay untracked (gitignored). {POLICY}",
        )


if __name__ == "__main__":
    unittest.main()

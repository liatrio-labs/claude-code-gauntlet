"""Guard the tracked documentation allowlist and generated config receipts."""

import json
import re
import subprocess
import unittest
from pathlib import Path

from gauntlet import contract_gen as contract_generator
from gauntlet.registry import KNOB_REGISTRY

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
        encoding="utf-8",
    ).stdout
    return [line for line in out.splitlines() if line]


class TestDocsRegistry(unittest.TestCase):
    def test_generated_config_receipt_matches_resolver_fixtures(self):
        """The generated receipts stay pinned to the resolver's empty-env fixtures."""
        registry = KNOB_REGISTRY
        resolver = contract_generator._load_resolver(str(REPO))
        path = REPO / "skills" / "code-gauntlet" / "SKILL.md"
        text = path.read_text(encoding="utf-8")
        open_marker, close_marker = contract_generator.identity_marker_lines(
            "config_receipt", "skills/code-gauntlet/SKILL.md"
        )
        start = text.index(open_marker) + len(open_marker)
        end = text.index(close_marker, start)
        body = text[start:end]
        for mode, label in (("interactive", "Interactive"), ("headless", "Headless")):
            match = re.search(
                rf"\*\*{label} receipt:\*\*\n\n```json\n(.*?)\n```",
                body,
                re.DOTALL,
            )
            self.assertIsNotNone(match, f"missing {mode} config receipt fence")
            actual = json.loads(match.group(1))
            expected = resolver.resolve(mode, {}, None, "pr", registry=registry)[
                "configEcho"
            ]
            self.assertEqual(
                actual, expected, f"{mode} receipt drifted from KNOB_REGISTRY"
            )
        interactive = json.loads(
            re.search(
                r"\*\*Interactive receipt:\*\*\n\n```json\n(.*?)\n```",
                body,
                re.DOTALL,
            ).group(1)
        )
        self.assertEqual(
            list(interactive), ["model_tier", "pr_comment_cap", "delivery_tier"]
        )

    def test_config_receipt_examples_follow_registry_order(self):
        """Every skill example must render knobs in the source registry's mode order."""
        registry = KNOB_REGISTRY
        expected = {
            mode: [
                row["key"]
                for row in registry
                if mode in row["modes"] and row.get("derivedFrom") is None
            ]
            for mode in ("interactive", "headless")
        }
        skill = (REPO / "skills/code-gauntlet/SKILL.md").read_text(encoding="utf-8")
        open_marker, close_marker = contract_generator.identity_marker_lines(
            "config_receipt", "skills/code-gauntlet/SKILL.md"
        )
        start = skill.index(open_marker)
        generated = skill[start : skill.index(close_marker, start)]
        for mode, label in (("interactive", "Interactive"), ("headless", "Headless")):
            match = re.search(
                rf"\*\*{label} block:\*\*\n\n```text\n(.*?)\n```",
                generated,
                re.DOTALL,
            )
            self.assertIsNotNone(match)
            keys = re.findall(r"^  ([A-Za-z_]+)=", match.group(1), re.MULTILINE)
            self.assertEqual(keys[:-2], expected[mode])

        report = (REPO / "skills/code-gauntlet/references/report-format.md").read_text(
            encoding="utf-8"
        )
        report_match = re.search(
            r"^Resolved config:\n((?:^  [^\n]+\n?)+)", report, re.MULTILINE
        )
        self.assertIsNotNone(report_match)
        report_keys = re.findall(
            r"^  ([A-Za-z_]+)=", report_match.group(1), re.MULTILINE
        )
        full_expected = [
            row["key"] for row in registry if "interactive" in row["modes"]
        ]
        self.assertEqual(report_keys[:-2], full_expected)

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

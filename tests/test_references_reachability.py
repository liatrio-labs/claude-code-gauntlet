"""References reachability guard (Issue #37).

Every file under skills/code-gauntlet/references/ must be reachable from the
live instruction graph: SKILL.md, agents/*.md, or another reference. A document
that nothing live points at is orphaned residue — the class that recurs after
v2→v3 moves.
"""

import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
REFS = REPO / "skills" / "code-gauntlet" / "references"
SKILL = REPO / "skills" / "code-gauntlet" / "SKILL.md"
AGENTS = REPO / "agents"


def reference_files() -> list[Path]:
    return sorted(p for p in REFS.glob("*.md") if p.is_file())


def live_sources(exclude: Path | None = None) -> list[Path]:
    sources = [SKILL, *sorted(AGENTS.glob("*.md")), *reference_files()]
    return [p for p in sources if p != exclude and p.is_file()]


def inbound_basenames() -> set[str]:
    found: set[str] = set()
    for ref in reference_files():
        name = ref.name
        for src in live_sources(exclude=ref):
            text = src.read_text(encoding="utf-8")
            if name in text:
                found.add(name)
                break
    return found


class TestReferencesReachability(unittest.TestCase):
    def test_every_reference_is_reachable(self):
        all_names = {p.name for p in reference_files()}
        self.assertTrue(all_names, "no reference files found")
        reachable = inbound_basenames()
        orphans = sorted(all_names - reachable)
        self.assertEqual(
            orphans,
            [],
            "orphaned references (no inbound from SKILL.md, agents/, or other refs): "
            + ", ".join(orphans),
        )

    def test_helper_treats_missing_basename_as_unreachable(self):
        # Sanity: a nonsense name is not in inbound_basenames
        self.assertNotIn("no-such-reference-zz.md", inbound_basenames())


if __name__ == "__main__":
    unittest.main()

"""References reachability guard (Issue #37).

Every file under skills/code-gauntlet/references/ must be reachable from the
live instruction graph: SKILL.md, agents/*.md, or another reference. A document
that nothing live points at is orphaned residue — the class that recurs after
v2→v3 moves.
"""

import tempfile
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


def inbound_basenames(refs=None, sources=None) -> set[str]:
    """Basenames among *refs* named by some source other than the file itself.

    *refs* and *sources* default to the live instruction graph; passing both
    lets the helper be exercised against a fixture whose answer is known, which
    is the only way to catch it over-reporting (a self-mention counted as
    inbound, say) rather than under-reporting.
    """
    refs = reference_files() if refs is None else list(refs)
    found: set[str] = set()
    for ref in refs:
        name = ref.name
        pool = (
            live_sources(exclude=ref) if sources is None
            else [p for p in sources if p != ref]
        )
        for src in pool:
            if name in src.read_text(encoding="utf-8"):
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

    def test_helper_separates_reachable_from_orphaned_on_a_known_graph(self):
        # The previous check asked whether a name that is in no file at all was
        # absent from the result — true for any helper, including one that
        # returns every reference unconditionally. Drive it with a fixture whose
        # answer is known instead, covering the two ways it can be wrong: a
        # pointed-at file must be found, and a file whose only mention of its own
        # name is inside itself must not be.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            hub = root / "hub.md"
            linked = root / "linked.md"
            orphan = root / "orphan.md"
            self_only = root / "self-only.md"
            hub.write_text("see linked.md for details\n", encoding="utf-8")
            linked.write_text("body\n", encoding="utf-8")
            orphan.write_text("nothing points here\n", encoding="utf-8")
            self_only.write_text("this is self-only.md\n", encoding="utf-8")

            refs = [linked, orphan, self_only]
            sources = [hub, linked, orphan, self_only]
            self.assertEqual(inbound_basenames(refs, sources), {"linked.md"})

    def test_a_live_reference_is_actually_reported_reachable(self):
        # A live positive: SKILL.md names phase1-preflight.md, so a helper that
        # silently stopped finding inbound links cannot pass.
        self.assertIn("phase1-preflight.md", inbound_basenames())


if __name__ == "__main__":
    unittest.main()

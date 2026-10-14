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
    refs = reference_files() if refs is None else list(refs)
    found: set[str] = set()
    for ref in refs:
        name = ref.name
        pool = (
            live_sources(exclude=ref)
            if sources is None
            else [p for p in sources if p != ref]
        )
        for src in pool:
            if name in src.read_text(encoding="utf-8"):
                found.add(name)
                break
    return found


def test_every_reference_is_reachable():
    all_names = {p.name for p in reference_files()}
    assert all_names, "no reference files found"
    orphans = sorted(all_names - inbound_basenames())
    assert not orphans, (
        f"orphaned references (no inbound from another live file): {orphans}"
    )


def test_helper_separates_reachable_from_orphaned_on_a_known_graph(tmp_path):
    # A self-mention cannot make an otherwise orphaned reference reachable.
    contents = {
        "hub.md": "see linked.md for details\n",
        "linked.md": "body\n",
        "orphan.md": "nothing points here\n",
        "self-only.md": "this is self-only.md\n",
    }
    for name, text in contents.items():
        (tmp_path / name).write_text(text, encoding="utf-8")
    refs = [tmp_path / name for name in ("linked.md", "orphan.md", "self-only.md")]
    sources = [tmp_path / name for name in contents]
    assert inbound_basenames(refs, sources) == {"linked.md"}

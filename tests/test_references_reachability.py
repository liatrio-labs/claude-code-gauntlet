import re
from pathlib import Path
from urllib.parse import unquote

REPO = Path(__file__).resolve().parents[1]
REFS = REPO / "skills" / "code-gauntlet" / "references"
SKILL = REPO / "skills" / "code-gauntlet" / "SKILL.md"
AGENTS = REPO / "agents"


def heading_slug(text: str) -> str:
    return re.sub(r"[^\w -]", "", text.lower()).replace(" ", "-")


def heading_anchors(path: Path) -> set[str]:
    anchors: set[str] = set()
    counts: dict[str, int] = {}
    fence = ""
    for line in path.read_text(encoding="utf-8").splitlines():
        marker = re.match(r"^\s*(`{3,}|~{3,})", line)
        if marker:
            run = marker[1]
            if not fence:
                fence = run
            elif (
                run[0] == fence[0]
                and len(run) >= len(fence)
                and not line.strip()[len(run) :]
            ):
                fence = ""
            continue
        if fence:
            continue
        heading = re.match(r"^#{1,6}\s+(.+?)\s*#*\s*$", line)
        if heading:
            slug = heading_slug(heading[1])
            count = counts.get(slug, 0)
            anchors.add(f"{slug}-{count}" if count else slug)
            counts[slug] = count + 1
    return anchors


def section_target(source: Path, name: str, files: list[Path]) -> Path | None:
    if not name:
        return source
    for base in (source.parent, source.parent.parent, REPO):
        candidate = (base / name).resolve()
        if candidate.is_file():
            return candidate
    matches = [path for path in files if path.name == Path(name).name]
    return matches[0] if len(matches) == 1 else None


def test_skill_section_references_resolve():
    files = sorted((REPO / "skills").rglob("*.md"))
    links = re.compile(r"\]\(([^\s()#]*\.md)?#([^\s()]+)\)")
    filename = r"[\w./-]+\.md"
    prose = re.compile(
        rf"(?P<file>{filename})`?"
        r"(?:(?:'s)?(?:\s+Phase\s+\d+)?\s*(?:\u2192|->|section)\s*"
        r"|\s*(?:'s|,|under)\s*(?=\"))"
        r'(?:"(?P<quoted>[^"\n]+)"|(?P<bare>[\w-]+))'
    )
    below = re.compile(r'(?:see|under)\s+"([^"\n]+)"\s+below')
    missing = []
    for source in files:
        text = source.read_text(encoding="utf-8")
        references = [
            (match.start(), match[1] or "", unquote(match[2]), True)
            for match in links.finditer(text)
        ]
        references.extend(
            (match.start(), match["file"], match["quoted"] or match["bare"], False)
            for match in prose.finditer(text)
        )
        references.extend(
            (match.start(), "", match[1], False) for match in below.finditer(text)
        )
        for start, name, section, anchor in references:
            target = section_target(source, name, files)
            headings = heading_anchors(target) if target else set()
            slug = section if anchor else heading_slug(section)
            # Prose often names just the numbered or short prefix of a full heading.
            found = slug in headings or (
                not anchor and any(h.startswith(slug + "-") for h in headings)
            )
            if not found:
                line = text.count("\n", 0, start) + 1
                missing.append(f"{source.relative_to(REPO)}:{line}: {name}#{slug}")
    assert not missing, "missing skill section targets:\n" + "\n".join(missing)


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

"""Guards for the agent-instruction files.

`AGENTS.md` is the only instruction file, at the root and in the directories that need one.
Codex and Cursor read it natively. Claude Code reads it too, at launch for the root file and
on a Read for a nested one, but only where no `CLAUDE.md` or `CLAUDE.local.md` sits in that
directory or above it. Measured on Claude Code 2.1.292 with one scratch repository per layout,
each file carrying its own codeword: with no `CLAUDE.md` both the root and the nested
`AGENTS.md` loaded, and a `CLAUDE.md` beside an `AGENTS.md` loaded in its place.

The failures guarded here are silent: a shadowing file hides every rule from one tool, a rule
names code that no longer exists, and the files grow one reasonable line at a time.
"""

import re
import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]

# A ratchet pinned at the measured size with no headroom, because a cushion is a smaller
# quantity of the thing being prevented. A cut or an equal-size correction passes; only net
# growth trips it, and raising the number on a line every reviewer sees is the mechanism.
# Before raising it, apply the test at the foot of the root AGENTS.md to the addition.
AGENTS_SET_BUDGET_BYTES = 4_131

SHADOWING_NAMES = ("CLAUDE.md", "CLAUDE.local.md", ".claude/CLAUDE.md")


def tracked(root: Path) -> list[str]:
    out = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=root,
        capture_output=True,
        check=True,
    ).stdout.decode()
    return [path for path in out.split("\0") if path]


def instruction_files(tracked_files: Sequence[str]) -> list[str]:
    return [path for path in tracked_files if Path(path).name == "AGENTS.md"]


def check_instruction_shadows(root: Path, tracked_files: Sequence[str]) -> None:
    """Tracked anywhere, or on disk beside or above an `AGENTS.md`.

    The on-disk half matters because `CLAUDE.local.md` is normally personal and
    untracked, and a session started in any subdirectory counts files above it.
    """
    names = {Path(name).name for name in SHADOWING_NAMES}
    shadows = {path for path in tracked_files if Path(path).name in names}
    for path in instruction_files(tracked_files):
        for directory in Path(path).parents:
            shadows.update(
                (directory / name).as_posix()
                for name in SHADOWING_NAMES
                if (root / directory / name).exists()
            )
    assert not shadows, (
        "Claude Code stops reading AGENTS.md once one of these exists at or above "
        "the working directory. Put the content in AGENTS.md instead. "
        f"{sorted(shadows)}"
    )


def check_instruction_size(root: Path, tracked_files: Sequence[str]) -> None:
    sizes = {
        path: (root / path).stat().st_size for path in instruction_files(tracked_files)
    }
    total = sum(sizes.values())
    assert total <= AGENTS_SET_BUDGET_BYTES, (
        f"AGENTS.md files grew to {total} bytes against a ratchet pinned at "
        f"{AGENTS_SET_BUDGET_BYTES}. Justify the addition at the constant, then raise "
        f"it in the same commit. {sizes}"
    )


QUOTE_NEAR_RULES = re.compile(
    r"`?(?:[A-Za-z0-9_./-]+/)?(?:CLAUDE|AGENTS)\.md`?"
    r"\s+(?:says\s+|states\s+|already applies[^\n(]{0,100}\(\s*)?"
    r'["\u201c]([^"\u201d\n]{12,})["\u201d]',
    re.IGNORECASE,
)
QUOTE_EXEMPTIONS = {
    (
        "skills/code-gauntlet/references/false-positive-exclusions.md",
        "all functions must have JSDoc",
    ),
}


# Backticked prose terms that are English, schema field names, or host globals named
# precisely because they are ABSENT - none of them are repo symbols.
NOT_SYMBOLS = {
    "structuredClone",
    "setTimeout",
    "console",
    "process",
    "Buffer",
    "TextEncoder",
    "package.json",
    "node_modules",
}


@pytest.fixture(scope="module")
def tracked_files() -> list[str]:
    return tracked(REPO)


def test_no_claude_md_exists(tracked_files: list[str]) -> None:
    check_instruction_shadows(REPO, tracked_files)


def test_instruction_files_do_not_grow(tracked_files: list[str]) -> None:
    check_instruction_size(REPO, tracked_files)


def test_rules_file_quotations_resolve(tracked_files: list[str]) -> None:
    corpus = "\n".join(
        (REPO / path).read_text(encoding="utf-8")
        for path in instruction_files(tracked_files)
    )
    docs = [
        path
        for path in tracked_files
        if path.endswith(".md")
        and (
            path.startswith("skills/")
            or (path.startswith("agents/") and path.count("/") == 1)
        )
    ]
    missing = []
    for doc in docs:
        text = (REPO / doc).read_text(encoding="utf-8")
        for quote in QUOTE_NEAR_RULES.findall(text):
            if (doc, quote) in QUOTE_EXEMPTIONS:
                continue
            if quote not in corpus:
                missing.append((doc, quote[:80]))
    assert not missing, f"rules quotations not found in any AGENTS.md: {missing}"


def test_referenced_paths_exist(tracked_files: list[str]) -> None:
    """A dangling reference sends an agent looking for code that does not exist."""
    files = set(tracked_files)
    basenames = {Path(path).name for path in files}
    pattern = re.compile(r"`([A-Za-z0-9_./-]+\.(?:py|js|md|json|yaml|yml))`")
    missing = []
    for doc in [*instruction_files(tracked_files), "REVIEW.md"]:
        text = (REPO / doc).read_text(encoding="utf-8")
        for ref in sorted(set(pattern.findall(text))):
            if ref in NOT_SYMBOLS or "*" in ref:
                continue
            if not (
                ref in files
                or Path(ref).name in basenames
                or (REPO / Path(doc).parent / ref).exists()
            ):
                missing.append((doc, ref))
    assert not missing, f"instruction files name paths outside this tree: {missing}"


def test_referenced_symbols_exist(tracked_files: list[str]) -> None:
    """A grep, deliberately - the claim is only that the name occurs in the code."""
    pattern = re.compile(r"`([a-z][a-zA-Z0-9_]{4,}|[A-Z][A-Z0-9_]{4,})`")
    docs = [*instruction_files(tracked_files), "REVIEW.md"]
    missing = []
    for doc in docs:
        text = (REPO / doc).read_text(encoding="utf-8")
        for sym in sorted(set(pattern.findall(text))):
            if sym in NOT_SYMBOLS:
                continue
            found = subprocess.run(
                ["git", "grep", "-l", "--", sym],
                cwd=REPO,
                capture_output=True,
                text=True,
                encoding="utf-8",
            ).stdout.split()
            # A hit in the instruction files themselves proves nothing.
            if not any(path not in docs for path in found):
                missing.append((doc, sym))
    assert not missing, (
        f"instruction files name symbols absent from the code: {missing}. "
        "If it lands with an unmerged branch, document it in that branch."
    )


@pytest.fixture
def instruction_repo(tmp_path: Path) -> Path:
    (tmp_path / "AGENTS.md").write_text("rules\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "add", "--", "AGENTS.md"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    return tmp_path


def test_grown_instruction_file_fails_size_check(instruction_repo: Path) -> None:
    files = tracked(instruction_repo)
    instructions = instruction_repo / "AGENTS.md"
    instructions.write_bytes(b"x" * 4_131)
    check_instruction_size(instruction_repo, files)
    instructions.write_bytes(b"x" * 4_132)
    with pytest.raises(AssertionError, match=r"AGENTS\.md files grew to 4132 bytes"):
        check_instruction_size(instruction_repo, files)


def test_added_claude_md_fails_shadow_check(instruction_repo: Path) -> None:
    files = tracked(instruction_repo)
    check_instruction_shadows(instruction_repo, files)
    # Personal instruction files can shadow rules without ever entering the git index.
    (instruction_repo / "CLAUDE.md").write_text("shadow\n", encoding="utf-8")
    with pytest.raises(AssertionError, match=r"CLAUDE\.md"):
        check_instruction_shadows(instruction_repo, files)

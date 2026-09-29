"""One offline freshness gate for the repository's local generators."""

import ast
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PARITY_RECORDER = "workflows/test/tools/record_parity.py"
COMMANDS = (
    (sys.executable, "scripts/generate_contract_requirements.py", "--check"),
    (sys.executable, "scripts/build_style_artifacts.py", "--check"),
    (sys.executable, "scripts/sync_agent_rules.py", "--check"),
    (sys.executable, PARITY_RECORDER, "--check"),
)


def _parses_check(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return any(
        (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "add_argument"
            and any(
                isinstance(arg, ast.Constant) and arg.value == "--check"
                for arg in node.args
            )
        )
        or (
            isinstance(node, ast.Compare)
            and any(
                isinstance(part, ast.Constant) and part.value == "--check"
                for part in (node.left, *node.comparators)
            )
        )
        for node in ast.walk(tree)
    )


def _check_generators():
    tracked = subprocess.run(
        ("git", "ls-files", "scripts/*.py", PARITY_RECORDER),
        cwd=REPO,
        capture_output=True,
        text=True,
        check=True,
        encoding="utf-8",
    ).stdout.splitlines()
    entries = set()
    for relative in tracked:
        if relative == PARITY_RECORDER:
            if _parses_check(REPO / relative):
                entries.add(relative)
            continue
        tree = ast.parse((REPO / relative).read_text(encoding="utf-8"))
        modules = [
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
            and node.module
            and node.module.startswith("gauntlet.")
            and any(alias.name == "CLI" for alias in node.names)
        ]
        for module in modules:
            source = REPO / "scripts" / Path(*module.split(".")).with_suffix(".py")
            if source.is_file() and _parses_check(source):
                entries.add(relative)
    return entries


def _worktree_state():
    commands = (
        ("git", "diff", "--binary"),
        ("git", "ls-files", "--others", "--exclude-standard"),
    )
    tracked = tuple(
        subprocess.run(command, cwd=REPO, capture_output=True, check=True).stdout
        for command in commands
    )
    registry = REPO / "scripts/gauntlet/registry.py"
    return tracked, registry.read_bytes() if registry.exists() else None


def test_all_offline_generators_current():
    assert {command[1] for command in COMMANDS} == _check_generators()
    before = _worktree_state()
    failures = []
    for command in COMMANDS:
        result = subprocess.run(
            command, cwd=REPO, capture_output=True, text=True, encoding="utf-8"
        )
        if result.returncode:
            failures.append(f"{' '.join(command)}: {result.stderr.strip()}")
    assert _worktree_state() == before, "generator --check changed the worktree"
    assert not failures, "\n".join(failures)

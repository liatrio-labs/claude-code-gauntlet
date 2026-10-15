"""One offline freshness gate for the repository's local generators."""

import importlib
import subprocess
import sys
from pathlib import Path

from tests.conftest import ENTRY_IMPORT
from tests.support.repository import tracked_paths

REPO = Path(__file__).resolve().parents[1]
PARITY_RECORDER = "workflows/test/tools/record_parity.py"
COMMANDS = (
    (sys.executable, "scripts/generate_contract_requirements.py", "--check"),
    (sys.executable, "scripts/build_style_artifacts.py", "--check"),
    (sys.executable, PARITY_RECORDER, "--check"),
)


def _check_generators():
    entries = {PARITY_RECORDER}
    for relative in tracked_paths(REPO, ("scripts/*.py",)):
        if Path(relative).parent != Path("scripts"):
            continue
        match = ENTRY_IMPORT.search((REPO / relative).read_text(encoding="utf-8"))
        assert match
        command = importlib.import_module(f"gauntlet.{match[1]}").CLI
        if any(
            "--check" in action.option_strings for action in command.parser._actions
        ):
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

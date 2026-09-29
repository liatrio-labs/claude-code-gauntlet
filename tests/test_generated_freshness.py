"""One offline freshness gate for the repository's local generators."""

import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
COMMANDS = (
    ("python3", "scripts/generate_contract_requirements.py", "--check"),
    ("python3", "scripts/build_style_artifacts.py", "--check"),
    ("python3", "scripts/sync_agent_rules.py", "--check"),
    ("python3", "workflows/test/tools/record_parity.py", "--check"),
)


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

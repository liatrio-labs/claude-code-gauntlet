"""One offline freshness gate for the repository's local generators."""

import importlib
import subprocess
import sys
from pathlib import Path

from gauntlet import contract_gen as gen

from tests.conftest import ENTRY_IMPORT
from tests.support.repository import copy_generator_tree, tracked_paths

REPO = Path(__file__).resolve().parents[1]
PARITY_RECORDER = "workflows/test/tools/record_parity.py"
COMMANDS = (
    (sys.executable, "scripts/generate_contract_requirements.py", "--check"),
    (sys.executable, "scripts/build_style_artifacts.py", "--check"),
    (sys.executable, PARITY_RECORDER, "--check"),
)


def test_registry_generation_is_ruff_format_stable(tmp_path, invoke):
    from tests.test_contract_gen import TARGETS

    copy_generator_tree(REPO, tmp_path, TARGETS)
    result = invoke(
        "generate_contract_requirements", ["--repo-root", str(tmp_path)], tmp_path
    )
    assert result.returncode == 0, result.stderr
    path = tmp_path / "scripts/gauntlet/registry.py"
    before = path.read_bytes()
    formatted = subprocess.run(
        [
            sys.executable,
            "-m",
            "ruff",
            "format",
            "--config",
            str(REPO / "pyproject.toml"),
            str(path),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    assert path.read_bytes() == before, formatted.stdout
    result = invoke(
        "generate_contract_requirements",
        ["--repo-root", str(tmp_path), "--check"],
        tmp_path,
    )
    assert result.returncode == 0, result.stderr
    result = invoke(
        "generate_contract_requirements", ["--repo-root", str(tmp_path)], tmp_path
    )
    assert result.returncode == 0, result.stderr
    assert (
        path.read_bytes()
        == before
        == gen.render_python_registry(gen.load_registry(str(tmp_path))).encode("utf-8")
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

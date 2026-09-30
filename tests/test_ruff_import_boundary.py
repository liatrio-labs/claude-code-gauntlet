"""The repository import ban is enforced by Ruff."""

import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _ruff(
    source: str, filename: str = "scripts/gauntlet/probe.py"
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "ruff",
            "check",
            "--no-cache",
            "--output-format",
            "concise",
            "--stdin-filename",
            filename,
            "-",
        ],
        input=source,
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=ROOT,
        check=False,
    )


@pytest.mark.parametrize(
    ("source", "banned"),
    [
        ("from scripts.gauntlet import cli\n\nprint(cli)\n", True),
        ("import scripts.gauntlet\n\nprint(scripts.gauntlet)\n", True),
        ("from gauntlet import cli\n\nprint(cli)\n", False),
    ],
)
def test_ruff_bans_scripts_imports(source, banned):
    result = _ruff(source)
    assert ("TID251" in result.stdout) is banned, result.stdout + result.stderr
    assert result.returncode == (1 if banned else 0), result.stderr


@pytest.mark.parametrize(
    ("filename", "source", "banned"),
    [
        ("scripts/gauntlet/probe.py", "import subprocess\n\nprint(subprocess)\n", True),
        (
            "scripts/gauntlet/probe.py",
            "from subprocess import run\n\nprint(run)\n",
            True,
        ),
        (
            "scripts/gauntlet/probe.py",
            "from scripts.gauntlet import cli\n\nprint(cli)\n",
            True,
        ),
        ("scripts/gauntlet/proc.py", "import subprocess\n\nprint(subprocess)\n", True),
        ("scripts/gauntlet/proc.py", "from scripts import cli\n\nprint(cli)\n", True),
        (
            "scripts/gauntlet/proc.py",
            "import subprocess  # noqa: TID251\n\nprint(subprocess)\n",
            False,
        ),
        ("tests/probe.py", "import subprocess\n\nprint(subprocess)\n", False),
    ],
)
def test_ruff_shipped_process_boundary(filename, source, banned):
    result = _ruff(source, filename)
    assert ("TID251" in result.stdout) is banned, result.stdout + result.stderr
    assert result.returncode == (1 if banned else 0), result.stdout + result.stderr


def test_ruff_pin_matches_pre_commit():
    dev = re.search(
        r'"ruff==([^"]+)"', (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    )
    hook = re.search(
        r"ruff-pre-commit\n\s+rev: v(\S+)",
        (ROOT / ".pre-commit-config.yaml").read_text(encoding="utf-8"),
    )
    assert dev and hook and dev.group(1) == hook.group(1)


def test_real_proc_passes_ruff():
    result = subprocess.run(
        [sys.executable, "-m", "ruff", "check", "scripts/gauntlet/proc.py"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert result.returncode == 0, result.stdout + result.stderr

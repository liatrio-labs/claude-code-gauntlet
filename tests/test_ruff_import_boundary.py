"""The repository import ban is enforced by Ruff."""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_ruff_bans_scripts_imports():
    config = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert re.search(
        r"\[tool\.ruff\.lint\.flake8-tidy-imports\.banned-api\]\s*"
        r'"scripts"\.msg\s*=\s*"[^"]+"',
        config,
    )

    ruff = shutil.which("ruff")
    if ruff is None:
        pytest.skip("Ruff is absent from PATH; configuration shape remains checked")
    result = subprocess.run(
        [
            ruff,
            "check",
            "--config",
            str(ROOT / "pyproject.toml"),
            "--stdin-filename",
            "scripts/probe.py",
            "-",
        ],
        input="from scripts.x import y\n",
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
        cwd=ROOT,
    )
    assert result.returncode != 0
    assert "TID251" in result.stdout

"""The repository import ban is enforced by Ruff."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_ruff_bans_scripts_imports():
    config = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert re.search(
        r"\[tool\.ruff\.lint\.flake8-tidy-imports\.banned-api\]\s*"
        r'"scripts"\.msg\s*=\s*"[^"]+"',
        config,
    )

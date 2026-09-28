"""The repository import ban is enforced by Ruff."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_ruff_bans_scripts_imports():
    # tomllib is 3.11+ and CI still runs 3.10, so read the two settings by pattern.
    config = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    select = re.search(
        r"(?m)^\[tool\.ruff\.lint\]\n(?:[^\[\n].*\n)*?select = \[([^\]]*)\]", config
    )
    assert select and '"TID251"' in select.group(1)
    assert re.search(
        r"\[tool\.ruff\.lint\.flake8-tidy-imports\.banned-api\]\s*"
        r'"scripts"\.msg\s*=\s*"[^"]+"',
        config,
    )

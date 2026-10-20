"""Pytest configuration and common fixtures.

Vendored subset (deep-review bench harness): only the sys.path bootstrap is kept.
Upstream's conftest also installed an openpyxl stub for the pruned xlsx/label
steps; those modules and their tests are not vendored, so the stub is dropped.
"""

from __future__ import annotations

from pathlib import Path
import sys

# Ensure the package root is importable for code under test.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

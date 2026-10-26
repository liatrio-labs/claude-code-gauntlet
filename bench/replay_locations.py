#!/usr/bin/env python3
"""Freeze evidence slices or replay saved locations without remote services."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "scripts")]

from bench.runner.location_replay import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())

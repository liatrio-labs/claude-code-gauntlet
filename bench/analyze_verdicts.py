#!/usr/bin/env python3
"""Report replay route, place, independent-reader agreement and stability tables."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bench.runner.verdict_analysis import main

if __name__ == "__main__":
    raise SystemExit(main())

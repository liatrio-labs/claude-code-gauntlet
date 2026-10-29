#!/usr/bin/env python3
"""Replay recorded delivery without review dispatches."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bench.runner.delivery_replay import main

if __name__ == "__main__":
    sys.exit(main())

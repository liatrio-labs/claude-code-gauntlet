#!/usr/bin/env python3
"""Replay validation against frozen discovery without running discovery or a judge."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from bench.runner.validation_replay import main

if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Stale truncate command entry."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from gauntlet.stale_truncate import CLI  # noqa: E402

CLI.run()

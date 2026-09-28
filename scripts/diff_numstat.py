#!/usr/bin/env python3
"""Diff numstat command entry."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from gauntlet.diff_numstat import CLI  # noqa: E402

CLI.run()

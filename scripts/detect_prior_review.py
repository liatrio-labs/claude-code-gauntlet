#!/usr/bin/env python3
"""Detect prior review command entry."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from gauntlet.detect_prior_review import CLI  # noqa: E402

CLI.run()

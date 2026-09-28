#!/usr/bin/env python3
"""Build style artifacts command entry."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from gauntlet.build_style_artifacts import CLI  # noqa: E402

CLI.run()

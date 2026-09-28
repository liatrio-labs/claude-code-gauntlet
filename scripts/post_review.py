#!/usr/bin/env python3
"""Post review command entry."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from gauntlet.post_review import CLI  # noqa: E402

CLI.run()

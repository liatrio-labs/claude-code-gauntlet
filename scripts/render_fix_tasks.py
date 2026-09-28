#!/usr/bin/env python3
"""Render fix tasks command entry."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from gauntlet.render_fix_tasks import CLI  # noqa: E402

CLI.run()

#!/usr/bin/env python3
"""Emit style context command entry."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from gauntlet.emit_style_context import CLI  # noqa: E402

CLI.run()

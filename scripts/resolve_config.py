#!/usr/bin/env python3
"""Resolve config command entry."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from gauntlet.resolve_config import CLI  # noqa: E402

CLI.run()

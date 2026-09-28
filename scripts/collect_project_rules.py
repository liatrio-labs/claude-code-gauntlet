#!/usr/bin/env python3
"""Collect project rules command entry."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from gauntlet.collect_project_rules import CLI  # noqa: E402

CLI.run()

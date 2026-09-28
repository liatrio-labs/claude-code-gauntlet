#!/usr/bin/env python3
"""Sync agent rules command entry."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from gauntlet.sync_agent_rules import CLI  # noqa: E402

CLI.run()

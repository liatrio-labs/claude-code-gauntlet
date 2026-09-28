#!/usr/bin/env python3
"""Collect project rules command entry."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from gauntlet.project_rules import CLI

CLI.run()

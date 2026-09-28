#!/usr/bin/env python3
"""Sync agent rules command entry."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from gauntlet.agent_rules import CLI

CLI.run()

#!/usr/bin/env python3
"""Resolve pr identity command entry."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from gauntlet.pr_identity import CLI

CLI.run()

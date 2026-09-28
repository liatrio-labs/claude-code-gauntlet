#!/usr/bin/env python3
"""Detect prior review command entry."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from gauntlet.prior_review import CLI

CLI.run()

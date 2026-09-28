#!/usr/bin/env python3
"""Ensure output dir command entry."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from gauntlet.output_dir import CLI

CLI.run()

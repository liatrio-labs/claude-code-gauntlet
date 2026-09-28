#!/usr/bin/env python3
"""Emit style context command entry."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from gauntlet.style_hook import CLI

CLI.run()

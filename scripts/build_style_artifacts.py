#!/usr/bin/env python3
"""Build style artifacts command entry."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from gauntlet.style import CLI

CLI.run()

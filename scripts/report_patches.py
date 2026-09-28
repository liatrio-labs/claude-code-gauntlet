#!/usr/bin/env python3
"""Report patches command entry."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from gauntlet.patches import CLI

CLI.run()

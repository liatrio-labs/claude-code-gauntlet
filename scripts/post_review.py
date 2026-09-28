#!/usr/bin/env python3
"""Post review command entry."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from gauntlet.delivery.post import CLI

CLI.run()

#!/usr/bin/env python3
"""Write shared context command entry."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from gauntlet.shared_context import CLI

CLI.run()

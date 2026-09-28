#!/usr/bin/env python3
"""Render fix tasks command entry."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from gauntlet.fix_tasks import CLI

CLI.run()

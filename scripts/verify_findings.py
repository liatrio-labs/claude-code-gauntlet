#!/usr/bin/env python3
"""Verify findings command entry."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from gauntlet.verify.decide import CLI

CLI.run()

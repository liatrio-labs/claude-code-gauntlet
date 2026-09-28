#!/usr/bin/env python3
"""Diff numstat command entry."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from gauntlet.numstat import CLI

CLI.run()

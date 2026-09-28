#!/usr/bin/env python3
"""Materialize artifacts command entry."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from gauntlet.materialize import CLI

CLI.run()

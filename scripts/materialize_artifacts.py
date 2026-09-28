#!/usr/bin/env python3
"""Materialize artifacts command entry."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from gauntlet.materialize_artifacts import CLI  # noqa: E402

CLI.run()

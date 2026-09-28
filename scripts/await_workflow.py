#!/usr/bin/env python3
"""Await workflow command entry."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from gauntlet.await_workflow import CLI  # noqa: E402

CLI.run()

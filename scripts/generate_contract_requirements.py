#!/usr/bin/env python3
"""Generate contract requirements command entry."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from gauntlet.generate_contract_requirements import CLI  # noqa: E402

CLI.run()

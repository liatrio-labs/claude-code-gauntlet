import sys
from pathlib import Path

_SCRIPTS_PATH = str(Path(__file__).resolve().parents[1] / "scripts")
if _SCRIPTS_PATH not in sys.path:
    # Bench modules import gauntlet outside pytest, so its scripts directory must be importable.
    sys.path.insert(0, _SCRIPTS_PATH)

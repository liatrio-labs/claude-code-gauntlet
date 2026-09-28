"""Read JavaScript-owned contract values once per pytest session."""

import json
import subprocess
from functools import lru_cache
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


@lru_cache(maxsize=1)
def js_values():
    script = (
        "import('./workflows/src/filterFindings.js').then(m => "
        "console.log(JSON.stringify({FIELD_RENAMES: m.FIELD_RENAMES, "
        "SEVERITY_ORDER: m.SEVERITY_ORDER})))"
    )
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=True,
        encoding="utf-8",
    )
    return json.loads(result.stdout)

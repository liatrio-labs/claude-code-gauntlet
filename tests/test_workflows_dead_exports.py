"""Dead-export guard for workflows/src (Issue #37).

Every export function must be referenced from workflows/src outside its export
declaration, or appear in EXPORT_ALLOWLIST with an inventory/issue citation.
Parity-only exports owned by #24 stay allowlisted, not deleted here.
"""

import re
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "workflows" / "src"

_EXPORT_FN = re.compile(r"^export\s+(?:async\s+)?function\s+(\w+)\s*\(", re.M)

# key: "filterFindings.js:parseReviewMd" -> inventory ID + citation
EXPORT_ALLOWLIST = {
    "filterFindings.js:parseReviewMd": "R-003 owned-elsewhere:#24",
    "filterFindings.js:loadExclusions": "R-003 owned-elsewhere:#24",
}


def export_functions() -> list[tuple[str, str, Path]]:
    out = []
    for path in sorted(SRC.glob("*.js")):
        text = path.read_text(encoding="utf-8")
        for m in _EXPORT_FN.finditer(text):
            out.append((path.name, m.group(1), path))
    return out


def is_referenced(name: str, defining: Path) -> bool:
    pat = re.compile(rf"\b{re.escape(name)}\b")
    for path in SRC.glob("*.js"):
        text = path.read_text(encoding="utf-8")
        for i, line in enumerate(text.splitlines(), 1):
            if not pat.search(line):
                continue
            if path == defining and _EXPORT_FN.search(line) and name in line:
                continue  # skip the export declaration line
            return True
    return False


class TestWorkflowsDeadExports(unittest.TestCase):
    def test_exports_are_live_or_allowlisted(self):
        offenders = {}
        for mod, name, path in export_functions():
            key = f"{mod}:{name}"
            if key in EXPORT_ALLOWLIST:
                continue
            if not is_referenced(name, path):
                offenders[key] = "no workflows/src reference outside export line"
        self.assertEqual(offenders, {}, f"dead exports: {offenders}")

    def test_allowlist_entries_exist(self):
        keys = {f"{m}:{n}" for m, n, _ in export_functions()}
        missing = sorted(set(EXPORT_ALLOWLIST) - keys)
        self.assertEqual(missing, [], f"allowlist names missing from src: {missing}")


if __name__ == "__main__":
    unittest.main()

"""Shared CLI result-write helpers for retained scripts (stdlib-only)."""

from __future__ import annotations

import json
import sys
from typing import Any

try:
    from assemble_artifacts import escape_lone_surrogates
except ImportError:  # imported as scripts.script_io by the test suite
    from scripts.assemble_artifacts import escape_lone_surrogates


def write_result(
    path: str | None, obj: Any, summary_lines: list[str] | None = None
) -> None:
    """Serialize *obj* as JSON.

    If *path* is set: write the file (indent=2, ensure_ascii=False, trailing
    newline) and print each summary line to stderr. If *path* is None: print
    the JSON payload to stdout only — no summary on stdout.

    Propagates OSError on write failure; callers keep their ``die(...)`` wrap.
    """
    output_text = escape_lone_surrogates(json.dumps(obj, indent=2, ensure_ascii=False))
    if path:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(output_text)
            fh.write("\n")
        for line in summary_lines or []:
            print(line, file=sys.stderr)
    else:
        print(output_text)

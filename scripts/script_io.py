"""Shared CLI result-write helpers for retained scripts (stdlib-only)."""

from __future__ import annotations

import json
import sys
from typing import Any


def escape_lone_surrogates(s):
    """Spell surrogate code points the way a well-formed JSON.stringify does.

    ES2019 made JSON.stringify "well-formed": a lone surrogate is emitted as a
    `\\uXXXX` escape rather than a raw code unit. `json.dumps(ensure_ascii=False)`
    emits it raw instead, which then (a) diverges from JS and (b) makes the
    result *unencodable* as UTF-8 — the crash that used to leave a zero-byte file
    at a planned path.

    Python's JSON decoder combines a well-formed pair into one astral character,
    so a surrogate reaching here is normally already lone; the pair branch below
    exists so the function is faithful to JS for any input, not just decoder
    output (JS sees the two code units as one astral character and emits it raw).
    """
    if not any(0xD800 <= ord(ch) <= 0xDFFF for ch in s):
        return s
    out = []
    i = 0
    n = len(s)
    while i < n:
        cp = ord(s[i])
        if 0xD800 <= cp <= 0xDBFF and i + 1 < n and 0xDC00 <= ord(s[i + 1]) <= 0xDFFF:
            low = ord(s[i + 1])
            out.append(chr(0x10000 + ((cp - 0xD800) << 10) + (low - 0xDC00)))
            i += 2
            continue
        if 0xD800 <= cp <= 0xDFFF:
            out.append(f"\\u{cp:04x}")
            i += 1
            continue
        out.append(s[i])
        i += 1
    return "".join(out)


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

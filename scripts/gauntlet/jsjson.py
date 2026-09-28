"""JavaScript-compatible JSON output."""

import json
from typing import Any


def dumps(obj: Any, *, ascii: bool = True, compact: bool = False) -> str:
    separators = (",", ":") if compact else None
    return escape_lone_surrogates(
        json.dumps(obj, ensure_ascii=ascii, allow_nan=False, separators=separators)
    )


def escape_lone_surrogates(s: str) -> str:
    """Match well-formed JSON.stringify while keeping UTF-8 output encodable.

    A valid surrogate pair remains one astral code point.
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


def write_result(obj: Any) -> None:
    """Escape lone surrogates so JSON output stays UTF-8 encodable."""
    print(escape_lone_surrogates(json.dumps(obj, indent=2, ensure_ascii=False)))

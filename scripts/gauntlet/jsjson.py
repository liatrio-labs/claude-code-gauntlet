"""JavaScript-compatible JSON output."""

import json
import struct
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


# The workflow sandbox hashes charCodeAt units and has no TextEncoder or Buffer.
# Masked arithmetic yields its 32-bit pattern.
FNV_OFFSET_BASIS = 0x811C9DC5
FNV_PRIME = 0x01000193


def utf16_code_units(s: str) -> tuple[int, ...]:
    raw = s.encode("utf-16-le", "surrogatepass")
    return struct.unpack(f"<{len(raw) // 2}H", raw)


def utf16_len(s: str) -> int:
    """JS's ``s.length``, not ``len(s)``."""
    return len(s.encode("utf-16-le", "surrogatepass")) // 2


def fnv1a32(s: str) -> str:
    h = FNV_OFFSET_BASIS
    for unit in utf16_code_units(s):
        h ^= unit
        h = (h * FNV_PRIME) & 0xFFFFFFFF
    return f"fnv1a32:0x{h:08x}"


def normalize_content(s: str) -> str:
    """Strip a BOM and at most one trailing newline before a content proof.

    The Write tool may add either, and the workflow normalizes its side the same
    way. Two trailing newlines remain a real difference.
    """
    if s.startswith("\ufeff"):
        s = s[1:]
    if s.endswith("\r\n"):
        return s[:-2]
    if s.endswith("\n"):
        return s[:-1]
    return s


class JsSerializationError(ValueError):
    """A value that cannot be rendered like JSON.stringify."""


# Python and JS spell floats differently (1e-7 against 1e-07, 90 against 90.0), so
# floats are refused rather than porting Number#toString. Integers beyond the safe
# range are refused because JS parses them lossily. persistDerivable in stages.js
# applies the same precondition.
JS_MAX_SAFE_INTEGER = 2**53 - 1


def assert_js_reproducible(obj: object, path: str = "$") -> None:
    """Reject floats, non-finite values, unsafe integers, non-string keys and non-JSON values.

    Iterative traversal leaves recursion to the encoder.
    """
    stack = [(obj, path)]
    while stack:
        node, where = stack.pop()
        if node is None or isinstance(node, (bool, str)):
            continue
        if isinstance(node, int):
            if not (-JS_MAX_SAFE_INTEGER <= node <= JS_MAX_SAFE_INTEGER):
                raise JsSerializationError(
                    f"integer at {where} is outside JS's safe integer range ({node!r})"
                )
            continue
        if isinstance(node, float):
            raise JsSerializationError(
                f"non-integer number at {where} ({node!r}): JS and Python spell "
                "such numbers differently, so the derived artifact would diverge"
            )
        if isinstance(node, list):
            for i, item in enumerate(node):
                stack.append((item, f"{where}[{i}]"))
            continue
        if isinstance(node, dict):
            for key, value in node.items():
                if not isinstance(key, str):
                    raise JsSerializationError(
                        f"non-string object key at {where} ({key!r})"
                    )
                stack.append((value, f"{where}.{key}"))
            continue
        raise JsSerializationError(
            f"value at {where} has no JSON representation ({type(node).__name__})"
        )


def _is_array_index(key: str) -> bool:
    # Bound length before int() to avoid Python's digit limit.
    return key == "0" or (
        key.isascii()
        and key.isdecimal()
        and len(key) <= 10
        and key[0] != "0"
        and int(key) <= 4294967294
    )


def _js_property_order(value: object) -> object:
    """Place array-index keys first, as JSON.stringify does."""
    root: list[Any] = [value]
    stack: list[tuple[Any, Any]] = [(root, 0)]
    while stack:
        parent, slot = stack.pop()
        node = parent[slot]
        copy: Any
        if isinstance(node, dict):
            keys = sorted(filter(_is_array_index, node), key=int)
            keys += [key for key in node if not _is_array_index(key)]
            copy = {key: node[key] for key in keys}
            stack.extend((copy, key) for key in keys)
        elif isinstance(node, list):
            copy = list(node)
            stack.extend((copy, index) for index in range(len(copy)))
        else:
            continue
        parent[slot] = copy
    return root[0]


def js_stringify_pretty(obj: object) -> str:
    """Render JSON.stringify(obj, null, 2) bytes.

    JS never escapes non-ASCII and spells non-finite numbers as null, hence
    ensure_ascii=False and allow_nan=False. A deep document is refused where the
    indent encoder recurses (Python before 3.14); 3.14 serializes it.
    """
    try:
        assert_js_reproducible(obj)
        return escape_lone_surrogates(
            json.dumps(
                _js_property_order(obj),
                indent=2,
                ensure_ascii=False,
                allow_nan=False,
            )
        )
    except RecursionError as exc:
        raise JsSerializationError("document exceeds JSON nesting limit") from exc


def checksum_or_none(obj: object) -> str | None:
    """Omit an unsupported consistency proof to preserve the envelope's failure shape.

    This detects a drifting executor, not an untrusted writer.
    """
    try:
        return fnv1a32(js_stringify_pretty(obj))
    except JsSerializationError:
        return None

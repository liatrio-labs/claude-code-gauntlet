"""Decode the Workflow's bounded, lossless measurement transport."""

from __future__ import annotations

import base64
import json
import struct
from typing import Literal, TypedDict

MeasurementEncoding = Literal["json-pool-lzw16-base64-v1"]
ENCODING: MeasurementEncoding = "json-pool-lzw16-base64-v1"


class PackedMeasurement(TypedDict):
    strings: list[str]
    root: object


# An untrusted task record must not expand without limit during collection.
MAX_TRACE_CHARS = 32 * 1024 * 1024


def measurement_text(text: str, encoding: str | None = None) -> str:
    if encoding is None:
        return text
    if encoding != ENCODING:
        raise ValueError("unknown measurement encoding")
    data = base64.b64decode(text, validate=True)
    if len(data) % 2:
        raise ValueError("truncated compressed measurement")
    dictionary = [bytes([i]) for i in range(256)]
    previous = b""
    parts = []
    size = 0
    for (code,) in struct.iter_unpack(">H", data):
        if code < len(dictionary):
            word = dictionary[code]
        elif code == len(dictionary) and previous:
            word = previous + previous[:1]
        else:
            raise ValueError("invalid compressed measurement code")
        size += len(word)
        if size > MAX_TRACE_CHARS:
            raise ValueError("decoded measurement exceeds safety bound")
        parts.append(word)
        if previous and len(dictionary) < 65536:
            dictionary.append(previous + word[:1])
        previous = word
    packet: PackedMeasurement = json.loads(b"".join(parts).decode("ascii"))
    if not isinstance(packet, dict) or set(packet) != {"strings", "root"}:
        raise ValueError("invalid compressed measurement tree")
    strings = packet["strings"]
    if not isinstance(strings, list) or any(not isinstance(s, str) for s in strings):
        raise ValueError("invalid compressed measurement strings")
    widths = [len(json.dumps(s)) for s in strings]
    expanded = 0

    def charge(amount: int) -> None:
        nonlocal expanded
        expanded += amount
        if expanded > MAX_TRACE_CHARS:
            raise ValueError("expanded measurement exceeds safety bound")

    def unpack(node: object) -> object:
        if node is None or type(node) in (bool, int, float):
            charge(len(json.dumps(node, allow_nan=False)))
            return node
        if not isinstance(node, list) or len(node) != 2 or type(node[0]) is not int:
            raise ValueError("invalid compressed measurement node")
        tag, value = node
        if tag == 0 and type(value) is int and 0 <= value < len(strings):
            charge(widths[value])
            return strings[value]
        if tag == 1 and isinstance(value, list):
            charge(2 + max(0, len(value) - 1))
            return [unpack(item) for item in value]
        if tag == 2 and isinstance(value, list):
            charge(2 + max(0, len(value) - 1))
            result = {}
            for entry in value:
                if (
                    not isinstance(entry, list)
                    or len(entry) != 2
                    or not isinstance(entry[0], str)
                    or entry[0] in result
                ):
                    raise ValueError("invalid compressed measurement property")
                charge(len(json.dumps(entry[0])) + 1)
                result[entry[0]] = unpack(entry[1])
            return result
        raise ValueError("invalid compressed measurement reference")

    return json.dumps(unpack(packet["root"]), separators=(",", ":"), allow_nan=False)

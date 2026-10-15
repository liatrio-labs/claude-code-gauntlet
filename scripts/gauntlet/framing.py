"""Keep reviewed content from impersonating prompt block boundaries."""

import re
import secrets
import sys
import unicodedata
from collections.abc import Mapping
from typing import AnyStr, Literal, get_args

BlockTag = Literal["untrusted-code-content", "project-rules", "review-rules"]

# unicodedata has no Default_Ignorable_Code_Point, so its non-Cf members are listed.
_IGNORABLE_NON_CF = (
    0x034F,
    0x115F,
    0x1160,
    0x17B4,
    0x17B5,
    *range(0x180B, 0x180E),
    0x3164,
    *range(0xFE00, 0xFE10),
    0xFFA0,
    *range(0xE0100, 0xE01F0),
)
_FORMAT = "".join(
    re.escape(chr(c))
    for c in range(sys.maxunicode + 1)
    if unicodedata.category(chr(c)) == "Cf" or c in _IGNORABLE_NON_CF
)
_GAP = rf"[\s{_FORMAT}]*"
_NAME_GAP = rf"[{_FORMAT}]*"
_NAMES = "|".join(
    _NAME_GAP.join(re.escape(ch) for ch in tag) for tag in get_args(BlockTag)
)
# Models can read past gaps, and forged blocks can impersonate other trust levels.
# The second gap only follows a literal slash, which keeps matching linear in gap length.
_BOUNDARY = re.compile(
    rf"<{_GAP}(?:/(?P<after>{_GAP}))?(?P<name>{_NAMES})(?![\w.:-])", re.IGNORECASE
)


def _neutralize(match: re.Match[str]) -> str:
    if match["after"] is not None:
        return rf"<\/{match['after']}{match['name']}"
    return "<\\" + match[0][1:]


def _escape_attribute(value: str) -> str:
    return (
        value.replace("&", "&amp;")
        .replace('"', "&quot;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def frame(
    tag: BlockTag, body: AnyStr, attrs: Mapping[str, str] | None = None
) -> AnyStr:
    nonce = secrets.token_hex(16)
    pairs = {**(attrs or {}), "id": nonce}
    rendered = "".join(f' {k}="{_escape_attribute(v)}"' for k, v in pairs.items())
    opening = f"<{tag}{rendered}>\n"
    closing = f'</{tag} id="{nonce}">'
    if isinstance(body, bytes):
        text = body.decode("utf-8", "surrogateescape")
        framed = _BOUNDARY.sub(_neutralize, text).encode("utf-8", "surrogateescape")
        return opening.encode() + framed + closing.encode()
    return opening + _BOUNDARY.sub(_neutralize, body) + closing

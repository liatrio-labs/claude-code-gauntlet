"""Keep reviewed content from closing its own prompt block."""

import re
import secrets
import sys
import unicodedata
from typing import AnyStr

# Models read past whitespace and default-ignorable characters, so neutralize
# closers containing either as a second layer.
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


def frame(tag: str, body: AnyStr, attrs: str = "") -> AnyStr:
    nonce = secrets.token_hex(16)
    opening = f'<{tag}{attrs} id="{nonce}">\n'
    closing = f'</{tag} id="{nonce}">'
    name = _NAME_GAP.join(re.escape(ch) for ch in tag)
    closer = re.compile(rf"<{_GAP}/(?={_GAP}{name}(?![\w.:-]))", re.IGNORECASE)
    if isinstance(body, bytes):
        text = body.decode("utf-8", "surrogateescape")
        framed = closer.sub(r"<\\/", text).encode("utf-8", "surrogateescape")
        return opening.encode() + framed + closing.encode()
    return opening + closer.sub(r"<\\/", body) + closing

"""Keep reviewed content from closing its own prompt block."""

import re
import sys
import unicodedata
from typing import AnyStr

# Models read past whitespace and invisible format characters, so a closer
# spelled with either inside it must still count as the block's closer.
_FORMAT = "".join(
    re.escape(chr(c))
    for c in range(sys.maxunicode + 1)
    if unicodedata.category(chr(c)) == "Cf"
)
_GAP = rf"[\s{_FORMAT}]*"
_NAME_GAP = rf"[{_FORMAT}]*"


def frame(tag: str, opening: AnyStr, body: AnyStr) -> AnyStr:
    name = _NAME_GAP.join(re.escape(ch) for ch in tag)
    closer = re.compile(rf"<{_GAP}/(?={_GAP}{name}(?![\w.:-]))", re.IGNORECASE)
    if isinstance(body, bytes):
        text = body.decode("utf-8", "surrogateescape")
        framed = closer.sub(r"<\\/", text).encode("utf-8", "surrogateescape")
        return opening + framed + f"</{tag}>".encode()
    return opening + closer.sub(r"<\\/", body) + f"</{tag}>"

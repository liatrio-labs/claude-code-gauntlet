"""Keep reviewed content from closing its own prompt block."""

import re
from typing import AnyStr


def frame(tag: str, opening: AnyStr, body: AnyStr) -> AnyStr:
    pattern = rf"<\s*/(?=\s*{re.escape(tag)}(?![\w.:-]))"
    if isinstance(body, bytes):
        return (
            opening
            + re.sub(pattern.encode("ascii"), rb"<\\/", body, flags=re.IGNORECASE)
            + f"</{tag}>".encode("ascii")
        )
    return opening + re.sub(pattern, r"<\\/", body, flags=re.IGNORECASE) + f"</{tag}>"

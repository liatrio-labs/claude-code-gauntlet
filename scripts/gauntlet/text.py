"""Outbound display text and JavaScript-compatible trim alphabet.

JS_TRIM_CHARS is the Python twin of JS String.prototype.trim.
"""

import re
from collections.abc import Mapping

from gauntlet.markdown import (
    code_span,
    escaped_tick,
    open_fence,
    span_close,
    tick_run,
)
from gauntlet.marker import FINDING_MARKER_TOKEN, MARKER_TOKENS
from gauntlet.registry import JS_TRIM_CHARS as JS_TRIM_CHARS


def normalize_report_severity(raw: object, labels: Mapping[str, object]) -> str:
    # Twin fallback: workflows/src/renderReport.js::normalizeReportSeverity.
    fallback = "low"
    if not isinstance(raw, str):
        return fallback
    normalized = raw.strip(JS_TRIM_CHARS).lower()
    return normalized if normalized in labels else fallback


def _rendered_text(value: object) -> str | None:
    """Strip LF edges because compose supplies separators; patch blanks stay separate."""
    if value is None:
        return None
    if not isinstance(value, str):
        value = str(value)
    if not value.strip():
        return None
    return value.strip("\n")


_RULE_TEXT_CAP = 500
_TRUNCATION_MARKER = "…[truncated]"

_GH_TOKEN_RE = re.compile(r"(?:ghp_|gho_|ghs_|ghr_|ghu_|github_pat_)[A-Za-z0-9_]{20,}")
_GL_TOKEN_RE = re.compile(r"(?:glpat-|glrt-)[A-Za-z0-9_\-]{20,}")

_ENTITY_DEC_RE = re.compile(r"&#([0-9]+);")
_ENTITY_HEX_RE = re.compile(r"&#x([0-9a-fA-F]+);", re.IGNORECASE)
_HTML_COMMENT_RE = re.compile(r"<!--[\s\S]*?-->")
_BACKTICK_RUN_RE = re.compile(r"`{3,}")

# A frozenset avoids CodeQL overly-large-range warnings while retaining TAB/LF.
_INVISIBLE_ORDS = frozenset(
    (
        *range(0x00, 0x09),
        0x0B,
        0x0C,
        0x0D,  # CR cannot terminate a quoted line outside its prefix.
        *range(0x0E, 0x20),
        0x7F,
        *range(0x80, 0xA0),
        0xAD,
        0x200B,
        0x200C,
        0x200D,
        0xFEFF,
        0x2060,
        *range(0x202A, 0x202F),
        *range(0x2066, 0x206A),
    )
)


def _strip_invisibles(text: str) -> str:
    return "".join(ch for ch in text if ord(ch) not in _INVISIBLE_ORDS)


def _decode_numeric_entities(text: str) -> str:
    """Decode ASCII and &commat; only, so decoding cannot reintroduce invisibles.

    Markdown parses fences before entities; encoded backticks cannot form a fence.
    """

    def _dec(match: re.Match[str]) -> str:
        num = int(match.group(1), 10)
        if 32 <= num <= 126:
            return chr(num)
        return ""

    def _hex(match: re.Match[str]) -> str:
        num = int(match.group(1), 16)
        if 32 <= num <= 126:
            return chr(num)
        return ""

    text = _ENTITY_DEC_RE.sub(_dec, text)
    text = _ENTITY_HEX_RE.sub(_hex, text)
    return text.replace("&commat;", "@")


_DANGEROUS_LT_RE = re.compile(r"<(?=[A-Za-z/!?])")
_MARKER_OPEN_RE = re.compile(
    r"<!--\s*(?:"
    + "|".join(re.escape(token) for token in (*MARKER_TOKENS, FINDING_MARKER_TOKEN))
    + r")\s*:"
)
_FENCE_SHAPE_RE = re.compile(r"^(?:[ \t>]|[-+*][ \t]|[0-9]{1,9}[.)][ \t])*([`~])\1{2,}")
_MULTILINE_QUOTE_RE = re.compile(r"^(?:[ \t>]|[-+*][ \t]|[0-9]{1,9}[.)][ \t])*?(>{3,})")


def _remove_comments(text: str) -> str:
    # The normalization fixed point also removes comment openers made by removal.
    return _HTML_COMMENT_RE.sub("", text)


def _normalize_outbound(text: str) -> str:
    while True:
        normalized = _strip_invisibles(_remove_comments(_decode_numeric_entities(text)))
        if normalized == text:
            return text
        text = normalized


def has_marker_opener(text: str) -> bool:
    return _MARKER_OPEN_RE.search(text) is not None


def _break_marker_openers(text: str) -> str:
    return _MARKER_OPEN_RE.sub(lambda match: "&lt;" + match.group()[1:], text)


def _escape_visible(text: str, *, code: bool = False) -> str:
    text = _DANGEROUS_LT_RE.sub("\uff1c" if code else "&lt;", text)
    return re.sub(
        r"@",
        lambda match: (
            "\uff20"
            if not match.start()
            or not re.match(r"[A-Za-z0-9]", text[match.start() - 1])
            else "@"
        ),
        text,
    )


def _escape_images(text: str) -> str:
    # Inline spans and indentation cannot certify renderer code.
    text = re.sub(
        r"(\\*)!\[",
        lambda match: (
            match.group(1) + ("\\" if len(match.group(1)) % 2 == 0 else "") + "!["
        ),
        text,
    )
    return re.sub(
        r"(\\*)\[\[(?=(?:\\[\s\S]|[^\\\[])*?\]\])",
        lambda match: (
            match.group(1) + ("\\" if len(match.group(1)) % 2 == 0 else "") + "[["
        ),
        text,
    )


_DEFINITION_RE = re.compile(
    r"(?m)^[^A-Za-z\\\[\n]*\["
    r"((?:\\[^\n]|[^\\\[\]\n]|\n(?![ \t>]*(?:\n|$))[ \t>]*)+)\]:"
)


def _escape_definitions(text: str) -> str:
    # Escape the colon because escaping the closing bracket can discard footnotes.
    # Twin: workflows/src/renderReport.js::outboundContain, plus LF continuation.
    return _DEFINITION_RE.sub(
        lambda match: (
            match.group()[:-1] + "\\:"
            if re.sub(r"\n[ \t>]*", "\n", match.group(1)).strip()
            else match.group()
        ),
        text,
    )


def _contain_line(line: str) -> str:
    line = re.sub(r"<(?=`+[A-Za-z/!?])", "\uff1c", line)
    out = []
    index = 0
    while index < len(line):
        if line[index] == "`":
            if escaped_tick(line, index):
                out.append("`")
                index += 1
                continue
            end = tick_run(line, index)
            width = end - index
            close = span_close(line, index, end)
            if close is not None:
                out.append(
                    line[index : close + width].replace(
                        line[end:close],
                        _escape_visible(line[end:close], code=True),
                        1,
                    )
                )
                index = close + width
                continue
            out.append("\\`")
            index += 1
            continue
        next_tick = line.find("`", index)
        if next_tick < 0:
            next_tick = len(line)
        out.append(_escape_visible(line[index:next_tick]))
        index = next_tick
    return "".join(out)


def _prepare_text(
    text: object,
    *,
    single_line: bool = False,
    collapse_ticks: bool = False,
    cap: int | None = None,
    trust_fences: bool = True,
) -> str:
    if text is None:
        return ""
    if not isinstance(text, str):
        text = str(text)
    if not text.strip():
        return ""
    text = _normalize_outbound(text)
    text = redact_secrets(text)
    if collapse_ticks:
        text = _BACKTICK_RUN_RE.sub("``", text)
    if single_line:
        text = re.sub(r"[\r\n]+", " ", text)
    if cap is not None:
        text = _cap_rule_text(text, cap)
    if not text.strip():
        return ""
    intervals: list[tuple[int, int]] = []
    fence = (
        open_fence(text, strict=True, intervals=intervals)
        if not single_line and trust_fences
        else None
    )
    lines = text.split("\n")
    protected_lines: list[bool] = []
    offset = 0
    for line in lines:
        protected_lines.append(any(start <= offset < end for start, end in intervals))
        offset += len(line) + 1
    start = 0
    for index in range(1, len(lines) + 1):
        if index == len(lines) or protected_lines[index] != protected_lines[start]:
            block = "\n".join(lines[start:index])
            lines[start:index] = (
                _break_marker_openers(block)
                if protected_lines[start]
                else _escape_definitions(_escape_images(block))
            ).split("\n")
            start = index
    prepared = []
    for line, protected in zip(lines, protected_lines, strict=True):
        shape = _FENCE_SHAPE_RE.match(line) if not single_line else None
        if shape and not protected:
            tick = shape.start(1)
            line = line[:tick] + "\\" + line[tick:]
        if not protected and not single_line:
            if line.startswith("/"):
                line = "\\" + line
            quote = _MULTILINE_QUOTE_RE.match(line)
            if quote:
                index = quote.start(1)
                line = line[:index] + "\\" + line[index:]
        prepared.append(line if protected else _contain_line(line))
    result = "\n".join(prepared)
    if fence is not None:
        result += "\n" + fence[0] * fence[1]
    return result if result.strip() else ""


def prepare_prose(text: object) -> str:
    """Prepare multiline, untrusted text for a posted Markdown field."""
    return _prepare_text(text)


def prepare_line(text: object) -> str:
    """Prepare a single-line, untrusted display field."""
    return _prepare_text(text, single_line=True)


def redact_secrets(text: str) -> str:
    """Only prefixed credentials with at least 20 body characters are redacted."""
    text = _GH_TOKEN_RE.sub("[REDACTED]", text)
    text = _GL_TOKEN_RE.sub("[REDACTED]", text)
    return text


def _cap_rule_text(text: str, limit: int = _RULE_TEXT_CAP) -> str:
    """Hard-cap cited-rule text; marker is appended outside ``limit``."""
    if len(text) <= limit:
        return text
    return text[:limit] + _TRUNCATION_MARKER


def prepared_prose(text: object, *, cap: bool = False) -> str | None:
    """Optional fields may disappear after normalization; capped rules distrust fences."""
    prepared = _prepare_text(
        text,
        collapse_ticks=True,
        cap=_RULE_TEXT_CAP if cap else None,
        trust_fences=not cap,
    )
    return _rendered_text(prepared)


def prepare_location(value: object) -> str:
    """Display locations must contain mentions and markup without losing space edges."""
    value = redact_secrets(_normalize_outbound(str(value)))
    value = re.sub(r"<(?=`+[A-Za-z/!?])", "\uff1c", value)
    value = _escape_visible(value, code=True).replace("\r", " ").replace("\n", " ")
    return code_span(value, pad_space_edges=True)

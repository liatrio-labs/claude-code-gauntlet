"""Outbound display text and JavaScript-compatible trim alphabet.

JS_TRIM_CHARS is the Python twin of JS String.prototype.trim.
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from gauntlet.markdown import (
    TickRunIndex,
    code_span,
    open_fence,
    paired_code_spans,
    tick_run_index,
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

_BACKTICK_RUN_RE = re.compile(r"`{3,}")
_EntityName = Literal[
    "commat",
    "excl",
    "lbrack",
    "lsqb",
    "rsqb",
    "rbrack",
    "colon",
    "lpar",
    "rpar",
    "bsol",
    "nbsp",
    "NonBreakingSpace",
]
_NAMED_ENTITIES: dict[_EntityName, str] = {
    "commat": "@",
    "excl": "!",
    "lbrack": "[",
    "lsqb": "[",
    "rsqb": "]",
    "rbrack": "]",
    "colon": ":",
    "lpar": "(",
    "rpar": ")",
    "bsol": "\\",
    "nbsp": " ",
    "NonBreakingSpace": " ",
}
_NAMED_ENTITY_PREFIXES = frozenset(
    name[:length] for name in _NAMED_ENTITIES for length in range(1, len(name) + 1)
)

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
_NORMALIZER_BOUNDARY_RE = re.compile(
    r"[&<>\-\x00-\x08\x0b-\x1f\x7f-\x9f\u00a0\u00ad\u200b-\u200d"
    r"\ufeff\u2060\u202a-\u202e\u2066-\u2069]"
)


@dataclass(frozen=True, slots=True)
class _EntityCandidate:
    start: int
    kind: Literal["start", "numeric", "decimal", "hex", "named"]
    value: int = 0
    has_digit: bool = False
    name: str = ""


@dataclass(frozen=True, slots=True)
class _NormalizerEntry:
    character: str
    entity: _EntityCandidate | None
    comment_start: int | None
    comment_end: int | None


_DANGEROUS_LT_RE = re.compile(r"<(?=[A-Za-z/!?])")
_MARKER_OPEN_RE = re.compile(
    r"<!--\s*(?:"
    + "|".join(re.escape(token) for token in (*MARKER_TOKENS, FINDING_MARKER_TOKEN))
    + r")\s*:"
)
_BACKTICK_FENCE_SHAPE_RE = re.compile(
    r"^(?:[ \t>]|[-+*][ \t]|[0-9]{1,9}[.)][ \t])*(`{3,})"
)
_MULTILINE_QUOTE_RE = re.compile(r"^(?:[ \t>]|[-+*][ \t]|[0-9]{1,9}[.)][ \t])*?(>{3,})")
_DEFINITION_LINK_TRIGGER_RE = re.compile(r"\]\\?\(")
_DEFINITION_CLOSE_COLON_RE = re.compile(r"\](\\*):")


def _normalize_outbound(text: str) -> str:
    """Decode bounded outbound entities and remove complete non-overlapping comments."""
    if _NORMALIZER_BOUNDARY_RE.search(text) is None:
        return text
    output: list[_NormalizerEntry] = []

    def push(character: str) -> str | None:
        if character == "\u00a0":
            character = " "
        if ord(character) in _INVISIBLE_ORDS:
            return None
        previous = output[-1] if output else None
        entity = previous.entity if previous is not None else None
        replacement: str | None = None
        if character == "&":
            entity = _EntityCandidate(len(output), "start")
        elif entity is not None:
            if entity.kind == "start":
                if character == "#":
                    entity = _EntityCandidate(entity.start, "numeric")
                elif "A" <= character <= "Z" or "a" <= character <= "z":
                    name = entity.name + character
                    entity = (
                        _EntityCandidate(entity.start, "named", name=name)
                        if name in _NAMED_ENTITY_PREFIXES
                        else None
                    )
                else:
                    entity = None
            elif entity.kind == "numeric":
                if character in "xX":
                    entity = _EntityCandidate(entity.start, "hex")
                elif "0" <= character <= "9":
                    entity = _EntityCandidate(
                        entity.start,
                        "decimal",
                        min(127, ord(character) - 48),
                        True,
                    )
                else:
                    entity = None
            elif entity.kind in ("decimal", "hex"):
                if character == ";" and entity.has_digit:
                    replacement = chr(entity.value) if 32 <= entity.value <= 126 else ""
                else:
                    digit = (
                        ord(character) - 48
                        if "0" <= character <= "9"
                        else ord(character.lower()) - 87
                        if entity.kind == "hex" and "a" <= character.lower() <= "f"
                        else -1
                    )
                    radix = 10 if entity.kind == "decimal" else 16
                    if digit >= 0 and digit < radix:
                        entity = _EntityCandidate(
                            entity.start,
                            entity.kind,
                            min(127, entity.value * radix + digit),
                            True,
                        )
                    else:
                        entity = None
            elif entity.kind == "named":
                if character == ";":
                    if entity.name in _NAMED_ENTITIES:
                        replacement = _NAMED_ENTITIES[entity.name]
                    else:
                        entity = None
                elif "A" <= character <= "Z" or "a" <= character <= "z":
                    name = entity.name + character
                    entity = (
                        _EntityCandidate(entity.start, "named", name=name)
                        if name in _NAMED_ENTITY_PREFIXES
                        else None
                    )
                else:
                    entity = None

        if replacement is not None and entity is not None:
            del output[entity.start :]
            return replacement or None

        comment_start = previous.comment_start if previous is not None else None
        comment_end = previous.comment_end if previous is not None else None
        output.append(_NormalizerEntry(character, entity, comment_start, comment_end))
        if (
            comment_start is None
            and len(output) >= 4
            and output[-4].character == "<"
            and output[-3].character == "!"
            and output[-2].character == "-"
            and output[-1].character == "-"
        ):
            output[-1] = _NormalizerEntry(
                character, entity, len(output) - 4, len(output)
            )
            return None
        if (
            comment_start is not None
            and len(output) >= 3
            and output[-3].character == "-"
            and output[-2].character == "-"
            and output[-1].character == ">"
            and comment_end is not None
            and len(output) - 3 >= comment_end
        ):
            del output[comment_start:]
        return None

    for original in text:
        character: str | None = original
        while character is not None:
            character = push(character)
    return "".join([entry.character for entry in output])


def has_marker_opener(text: str) -> bool:
    return _MARKER_OPEN_RE.search(text) is not None


def _break_marker_openers(text: str) -> str:
    return _MARKER_OPEN_RE.sub(lambda match: "&lt;" + match.group()[1:], text)


def _escape_visible(text: str, *, code: bool = False) -> str:
    # GitLab reparses text after consuming escapes; fullwidth brackets survive.
    # Original neighbours keep overlapping openers; the lookbehind anchors each run.
    text = re.sub(
        r"(?<=[!\[])\\*\[",
        lambda match: match.group()[:-1] + "\uff3b",
        text,
    )
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


_DEFINITION_RE = re.compile(
    r"(?m)^[^A-Za-z\\\[\n]*\[(?:(\^[^\]\n]*)|"
    # The character alternative consumes continuation prefixes without rescans.
    r"((?:\\[^\n]|[^\\\[\]\n]|\n(?![ \t>]*(?:\n|$)))+))\]:"
)


def _escape_triggered_definition_colons(line: str, *, force: bool = False) -> str:
    if not force and _DEFINITION_LINK_TRIGGER_RE.search(line) is None:
        return line
    spans = _containment_code_spans(line)
    parts: list[str] = []
    cursor = 0
    span_index = 0
    for match in _DEFINITION_CLOSE_COLON_RE.finditer(line):
        colon = match.end() - 1
        while span_index < len(spans) and spans[span_index][2] <= colon:
            span_index += 1
        if (
            span_index < len(spans)
            and spans[span_index][0] <= colon < spans[span_index][2]
        ):
            continue
        parts.extend((line[cursor:colon], "\uff1a"))
        cursor = colon + 1
    if cursor == 0:
        return line
    parts.append(line[cursor:])
    return "".join(parts)


def _escape_definitions(text: str) -> str:
    # Escape the colon because escaping the closing bracket can discard footnotes.
    # Twin: workflows/src/renderReport.js::outboundDefinitions, plus LF continuation.
    text = "\n".join(
        _escape_triggered_definition_colons(line) for line in text.split("\n")
    )

    def escape(match: re.Match[str]) -> str:
        colon = match.end() - 1
        start = text.rfind("\n", 0, colon) + 1
        end = text.find("\n", colon)
        line = text[start : end if end >= 0 else len(text)]
        # On triggered lines, remaining ASCII colons belong to selected code spans.
        if _DEFINITION_LINK_TRIGGER_RE.search(line) is not None:
            return match.group()
        if (
            match.group(1) is not None
            or re.sub(r"\n[ \t>]*", "\n", match.group(2)).strip()
        ):
            return match.group()[:-1] + "\\:"
        return match.group()

    return _DEFINITION_RE.sub(escape, text)


def _escape_tilde_runs(line: str) -> str:
    spans = _containment_code_spans(line)
    span_index = 0
    parts = []
    index = 0
    backslashes = 0
    while index < len(line):
        if span_index < len(spans) and index == spans[span_index][0]:
            end = spans[span_index][2]
            parts.append(line[index:end])
            index = end
            span_index += 1
            backslashes = 0
            continue
        if line[index] == "~":
            end = index + 1
            while end < len(line) and line[end] == "~":
                end += 1
            if end - index >= 3:
                if backslashes % 2 == 0:
                    parts.append("\\")
                parts.append("~" + "\\~" * (end - index - 1))
                index = end
                backslashes = 0
                continue
        character = line[index]
        parts.append(character)
        backslashes = backslashes + 1 if character == "\\" else 0
        index += 1
    return "".join(parts)


def _containment_code_spans(
    line: str, index: TickRunIndex | None = None
) -> list[tuple[int, int, int]]:
    return paired_code_spans(
        index if index is not None else tick_run_index(line), suffix_retry=True
    )


def _contain_line(line: str) -> str:
    line = re.sub(r"<(?=`+[A-Za-z/!?])", "\uff1c", line)
    index = tick_run_index(line)
    spans = _containment_code_spans(line, index)
    span_index = 0
    out: list[str] = []
    cursor = 0
    run_index = 0
    while run_index < len(index.runs):
        run = index.runs[run_index]
        out.append(_escape_visible(line[cursor : run.start]))
        opener = run.start
        if run.escaped:
            out.append("`")
            opener += 1
        if span_index < len(spans) and spans[span_index][0] < run.end:
            selected, close, close_end = spans[span_index]
            span_index += 1
            out.append("\\`" * (selected - opener))
            opener_end = selected + close_end - close
            out.append(line[selected:opener_end])
            out.append(_escape_visible(line[opener_end:close], code=True))
            out.append(line[close:close_end])
            cursor = close_end
            run_index += 1
            while run_index < len(index.runs) and index.runs[run_index].start < cursor:
                run_index += 1
        else:
            if not run.escaped:
                out.append("\\`" * run.width)
            else:
                out.append("\\`" * (run.width - 1))
            cursor = run.end
            run_index += 1
    out.append(_escape_visible(line[cursor:]))
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
    opener_suffixes: list[tuple[int, int]] = []
    fence = (
        open_fence(
            text,
            strict=True,
            intervals=intervals,
            opener_suffixes=opener_suffixes,
        )
        if not single_line and trust_fences
        else None
    )
    lines = text.split("\n")
    protected_lines: list[bool] = []
    offset = 0
    interval_index = 0
    for line in lines:
        while (
            interval_index < len(intervals) and intervals[interval_index][1] <= offset
        ):
            interval_index += 1
        protected_lines.append(
            interval_index < len(intervals)
            and intervals[interval_index][0] <= offset < intervals[interval_index][1]
        )
        offset += len(line) + 1
    suffix_index = 0
    offset = 0
    for index, line in enumerate(lines):
        while suffix_index < len(opener_suffixes) and opener_suffixes[suffix_index][
            0
        ] <= offset + len(line):
            start, _ = opener_suffixes[suffix_index]
            if offset <= start <= offset + len(line):
                lines[index] = line[: start - offset]
                suffix_index += 1
                break
            suffix_index += 1
        offset += len(line) + 1
    classified_lines: list[str] = []
    start = 0
    while start < len(lines):
        end = start + 1
        while end < len(lines) and protected_lines[end] == protected_lines[start]:
            end += 1
        block = "\n".join(lines[start:end])
        classified_lines.extend(
            (
                _break_marker_openers(block)
                if protected_lines[start]
                else _escape_definitions(block)
            ).split("\n")
        )
        start = end
    lines = classified_lines
    prepared = []
    for line, protected in zip(lines, protected_lines, strict=True):
        shape = _BACKTICK_FENCE_SHAPE_RE.match(line) if not single_line else None
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
        line = line if protected else _contain_line(line)
        if not protected and not single_line:
            line = _escape_tilde_runs(line)
        prepared.append(line)
    # Bracket replacements can complete definition-shaped labels.
    defined_lines: list[str] = []
    start = 0
    while start < len(prepared):
        end = start + 1
        while end < len(prepared) and protected_lines[end] == protected_lines[start]:
            end += 1
        block = "\n".join(prepared[start:end])
        defined_lines.extend(
            (_escape_definitions(block) if not protected_lines[start] else block).split(
                "\n"
            )
        )
        start = end
    prepared = defined_lines
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

"""Markdown block facts, spans, and safe delimiter selection."""

import re
from bisect import bisect_left
from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True, slots=True)
class _FenceLine:
    character: str
    width: int
    suffix_start: int
    indent_offset: int


@dataclass(frozen=True, slots=True)
class _OpenFence:
    character: str
    width: int
    opener_offset: int


def _fence_line(line: str) -> _FenceLine | None:
    indent = 0
    while indent < 3 and indent < len(line) and line[indent] == " ":
        indent += 1
    if indent >= len(line) or line[indent] not in "`~":
        return None
    char = line[indent]
    end = indent
    while end < len(line) and line[end] == char:
        end += 1
    return _FenceLine(char, end - indent, end, indent)


# Twin of ``openProseFence`` in ``workflows/src/outboundText.js``.
def open_fence(
    text: str,
    *,
    strict: bool = False,
    intervals: list[tuple[int, int]] | None = None,
    opener_suffixes: list[tuple[int, int]] | None = None,
) -> tuple[str, int, int] | None:
    """Return the final open fence as ``(char, run length, opener offset)``.

    Collect half-open string offsets; strict openers start at column zero.
    """
    state: _OpenFence | None = None
    opened_at: int | None = None
    line_start = 0
    index = 0

    def visit(line: str, offset: int, line_end: int) -> None:
        nonlocal state, opened_at
        run = _fence_line(line)
        if state is not None:
            if (
                run is not None
                and run.character == state.character
                and run.width >= state.width
                and all(character in " \t" for character in line[run.suffix_start :])
            ):
                state = None
                if intervals is not None and opened_at is not None:
                    intervals.append((opened_at, line_end))
            return
        if (
            run is not None
            and run.width >= 3
            and (not strict or run.indent_offset == 0)
            and not (run.character == "`" and "`" in line[run.suffix_start :])
        ):
            state = _OpenFence(run.character, run.width, offset + run.indent_offset)
            opened_at = offset
            if opener_suffixes is not None:
                opener_suffixes.append((offset + run.suffix_start, offset + len(line)))

    while index < len(text):
        if text[index] in "\r\n":
            visit(text[line_start:index], line_start, index + 1)
            if (
                text[index] == "\r"
                and index + 1 < len(text)
                and text[index + 1] == "\n"
            ):
                index += 2
            else:
                index += 1
            line_start = index
        else:
            index += 1
    visit(text[line_start:], line_start, len(text))
    if intervals is not None and state is not None and opened_at is not None:
        intervals.append((opened_at, len(text)))
    return (
        None if state is None else (state.character, state.width, state.opener_offset)
    )


# Twin of ``proseFenceCloser`` in ``workflows/src/outboundText.js``.
def fence_closer(prefix: str) -> str:
    state = open_fence(prefix)
    return "" if state is None else state[0] * state[1]


@dataclass(frozen=True, slots=True)
class TickRun:
    start: int
    end: int
    width: int
    escaped: bool


@dataclass(frozen=True, slots=True)
class TickRunIndex:
    runs: tuple[TickRun, ...]
    starts_by_width: dict[int, tuple[int, ...]]


def tick_run_index(line: str) -> TickRunIndex:
    runs: list[TickRun] = []
    starts: dict[int, list[int]] = {}
    index = 0
    slash_parity = 0
    while index < len(line):
        character = line[index]
        if character == "\\":
            slash_parity ^= 1
            index += 1
            continue
        if character == "`":
            start = index
            while index < len(line) and line[index] == "`":
                index += 1
            width = index - start
            runs.append(TickRun(start, index, width, bool(slash_parity)))
            starts.setdefault(width, []).append(start)
        else:
            index += 1
        slash_parity = 0
    return TickRunIndex(
        tuple(runs),
        {width: tuple(positions) for width, positions in starts.items()},
    )


def span_close(index: TickRunIndex, after: int, width: int) -> int | None:
    """Return the first complete equal-width run; span contents ignore slashes."""
    starts = index.starts_by_width.get(width)
    if starts is None:
        return None
    position = bisect_left(starts, after)
    return starts[position] if position < len(starts) else None


def _first_close(
    index: TickRunIndex, run: TickRun, suffix_retry: bool
) -> tuple[int, int, int] | None:
    opener = run.start + int(run.escaped)
    width = run.width - int(run.escaped)
    while width > 0:
        close = span_close(index, run.end, width)
        if close is not None:
            return opener, width, close
        if not suffix_retry:
            return None
        width -= 1
        opener += 1
    return None


def paired_code_spans(
    index: TickRunIndex,
    *,
    suffix_retry: bool = False,
    skip_openers: frozenset[int] = frozenset(),
) -> list[tuple[int, int, int]]:
    """Select paired spans with the requested unmatched-run policy."""
    spans: list[tuple[int, int, int]] = []
    run_index = 0
    while run_index < len(index.runs):
        run = index.runs[run_index]
        if run_index in skip_openers:
            run_index += 1
            continue
        found = _first_close(index, run, suffix_retry)
        run_index += 1
        if found is None:
            continue
        opener, width, close = found
        close_end = close + width
        spans.append((opener, close, close_end))
        while run_index < len(index.runs) and index.runs[run_index].start < close_end:
            run_index += 1
    return spans


def protected_line_flags(
    lines: list[str], intervals: list[tuple[int, int]]
) -> list[bool]:
    """Mark line starts inside sorted, half-open fence intervals in one pass."""
    protected: list[bool] = []
    offset = 0
    interval_index = 0
    for line in lines:
        while (
            interval_index < len(intervals) and intervals[interval_index][1] <= offset
        ):
            interval_index += 1
        protected.append(
            interval_index < len(intervals)
            and intervals[interval_index][0] <= offset < intervals[interval_index][1]
        )
        offset += len(line) + 1
    return protected


def code_spans(text: str) -> tuple[list[tuple[int, int]], list[tuple[int, int]]]:
    """Pair one-line spans outside top-level fences."""
    fences: list[tuple[int, int]] = []
    spans: list[tuple[int, int]] = []
    open_fence(text, intervals=fences)
    offset = 0
    lines = text.split("\n")
    for line, protected in zip(lines, protected_line_flags(lines, fences), strict=True):
        if protected:
            offset += len(line) + 1
            continue
        spans.extend(
            (offset + start, offset + close_end)
            for start, _close, close_end in paired_code_spans(tick_run_index(line))
        )
        offset += len(line) + 1
    return spans, fences


def fence_run(payload: str) -> str:
    """Longest inner run + 1, minimum 3, so the payload cannot close the fence early
    (CommonMark). Matches GitLab's suggestion UI, which documents four-backtick
    nesting; GitHub keeps Apply working at four or more backticks.
    """
    runs = re.findall(r"`+", payload)
    length = max(3, max((len(run) for run in runs), default=0) + 1)
    return "`" * length


def code_span(value: str, *, pad_space_edges: bool = False) -> str:
    """Pad backtick edges; optionally preserve space edges."""
    runs = re.findall(r"`+", value)
    delimiter = "`" * (max((len(run) for run in runs), default=0) + 1)
    edges = ("`", " ") if pad_space_edges else ("`",)
    padding = " " if value.startswith(edges) or value.endswith(edges) else ""
    return f"{delimiter}{padding}{value}{padding}{delimiter}"


BlockKind = Literal[
    "blank", "paragraph", "fence", "indented_code", "html", "heading", "rule"
]


@dataclass(frozen=True, slots=True)
class BlockLine:
    prefix_end: int
    contained: bool
    kind: BlockKind
    continues_paragraph: bool


@dataclass(frozen=True, slots=True)
class _Container:
    kind: Literal["quote", "list"]
    width: int = 0


_QUOTE = re.compile(r" {0,3}> ?")
_LIST = re.compile(r"( {0,3})([-+*]|[0-9]{1,9}[.)])( +|$)")
_HEADING = re.compile(r" {0,3}#{1,6}(?: |$)")
_RULE = re.compile(r" {0,3}(?:(?:\* *){3,}|(?:- *){3,}|(?:_ *){3,})$")
_SETEXT = re.compile(r" {0,3}(?:=+|-+) *$")
_TAG = r"[A-Za-z][A-Za-z0-9-]*"
_ATTRIBUTE = (
    r"""[A-Za-z_:][A-Za-z0-9_.:-]*(?:\s*=\s*(?:[^\s"'=<>`]+|'[^']*'|"[^"]*"))?"""
)
_BLOCK_TAGS = (
    "address|article|aside|base|basefont|blockquote|body|caption|center|col|colgroup|"
    "dd|details|dialog|dir|div|dl|dt|fieldset|figcaption|figure|footer|form|frame|"
    "frameset|h[1-6]|head|header|hr|html|iframe|legend|li|link|main|menu|menuitem|"
    "nav|noframes|ol|optgroup|option|p|param|search|section|source|summary|table|"
    "tbody|td|tfoot|th|thead|title|tr|track|ul"
)


@dataclass(frozen=True, slots=True)
class _HtmlBlock:
    start: re.Pattern[str]
    end: re.Pattern[str] | None
    interrupts: bool


_HTML_BLOCKS = (
    _HtmlBlock(
        re.compile(r"<(?:script|pre|style|textarea)(?:[ \t]|>|$)", re.I),
        re.compile(r"</(?:script|pre|style|textarea)>", re.I),
        True,
    ),
    _HtmlBlock(re.compile(r"<!--"), re.compile(r"-->"), True),
    _HtmlBlock(re.compile(r"<\?"), re.compile(r"\?>"), True),
    _HtmlBlock(re.compile(r"<![A-Za-z]"), re.compile(r">"), True),
    _HtmlBlock(re.compile(r"<!\[CDATA\["), re.compile(r"\]\]>"), True),
    _HtmlBlock(
        re.compile(r"</?(?:" + _BLOCK_TAGS + r")(?:[ \t]|/?>|$)", re.I), None, True
    ),
    _HtmlBlock(
        re.compile(
            r"(?:<(?!(?i:pre|script|style|textarea)(?:[ \t]|/?>|$))"
            + _TAG
            + r"(?:[ \t]+"
            + _ATTRIBUTE
            + r")*[ \t]*/?>|</"
            + _TAG
            + r"[ \t]*>)[ \t]*$"
        ),
        None,
        False,
    ),
)
_MAX_CONTAINERS = 64


def _html_start(content: str, paragraph: bool) -> int | None:
    for index, block in enumerate(_HTML_BLOCKS):
        if (block.interrupts or not paragraph) and block.start.match(content):
            return index
    return None


def _fence_opener(content: str) -> _FenceLine | None:
    run = _fence_line(content)
    if (
        run is None
        or run.width < 3
        or (run.character == "`" and "`" in content[run.suffix_start :])
    ):
        return None
    return run


def _list_start(content: str, paragraph: bool) -> re.Match[str] | None:
    match = _LIST.match(content)
    if match is not None and paragraph:
        marker = match[2]
        if (len(marker) > 1 and marker[:-1] != "1") or not content[match.end() :]:
            return None
    return match


def _interrupts(content: str, *, html: bool) -> bool:
    return bool(
        _HEADING.match(content)
        or _RULE.fullmatch(content)
        or _QUOTE.match(content)
        or _list_start(content, True)
        or _fence_opener(content)
        or (
            html
            and _html_start(content.lstrip(" "), True) is not None
            and len(content) - len(content.lstrip(" ")) < 4
        )
    )


def _prefix_index(line: str, column: int) -> int:
    if "\t" not in line:
        return column
    expanded = 0
    for index, character in enumerate(line):
        if expanded >= column:
            return index
        expanded += 4 - expanded % 4 if character == "\t" else 1
    return len(line)


def blocks(text: str, *, html: bool = True) -> tuple[BlockLine, ...]:
    """Classify LF-separated lines; prefix_end indexes the original, unexpanded line.

    Container depth is bounded. Beyond the bound, ambiguous lines are contained
    paragraph text until an unindented plain line establishes a fresh block.
    """
    result: list[BlockLine] = []
    stack: list[_Container] = []
    paragraph = False
    fence: _FenceLine | None = None
    html_block: int | None = None
    overflow = False
    for original in text.split("\n"):
        line = original.expandtabs(4)
        if overflow:
            if not line or line[0] in " >" or _LIST.match(line):
                result.append(
                    BlockLine(0, True, "paragraph" if line.strip() else "blank", False)
                )
                continue
            overflow = False
        position = 0
        matched = 0
        for container in stack:
            if container.kind == "quote":
                quote = _QUOTE.match(line, position)
                if quote is None:
                    break
                position = quote.end()
            else:
                end = position + container.width
                if line[position:end] != " " * container.width:
                    if line[position:].strip():
                        break
                    position = len(line)
                else:
                    position = end
            matched += 1
        content = line[position:]
        lazy = (
            matched < len(stack)
            and paragraph
            and bool(content.strip())
            and not _interrupts(content, html=html)
        )
        if matched < len(stack) and not lazy:
            del stack[matched:]
            paragraph = False
            fence = None
            html_block = None
        new_container = False
        if fence is None and html_block is None and not lazy:
            while True:
                quote = _QUOTE.match(line, position)
                item = (
                    None
                    if _RULE.fullmatch(line[position:])
                    else _list_start(line[position:], paragraph)
                )
                if quote is None and item is None:
                    break
                if len(stack) == _MAX_CONTAINERS:
                    overflow = True
                    stack.clear()
                    break
                if quote is not None:
                    stack.append(_Container("quote"))
                    position = quote.end()
                elif item is not None:
                    padding = len(item[3])
                    # An empty item's padding cannot move its content column.
                    width = (
                        item.end()
                        if padding <= 4 and line[position + item.end() :]
                        else item.end() - padding + 1
                    )
                    stack.append(_Container("list", width))
                    position += width
                new_container = True
                paragraph = False
        if overflow:
            result.append(BlockLine(0, True, "paragraph", False))
            paragraph = False
            continue
        content = line[position:]
        stripped = content.lstrip(" ")
        indent = len(content) - len(stripped)
        kind: BlockKind
        continues = False
        if fence is not None:
            kind = "fence"
            run = _fence_line(content)
            if (
                run is not None
                and run.character == fence.character
                and run.width >= fence.width
                and all(character in " \t" for character in content[run.suffix_start :])
            ):
                fence = None
        elif html_block is not None and (
            stripped or _HTML_BLOCKS[html_block].end is not None
        ):
            kind = "html"
        elif not stripped:
            kind = "blank"
            html_block = None
        elif lazy:
            kind = "paragraph"
            continues = True
        elif indent >= 4 and not paragraph:
            kind = "indented_code"
        elif indent < 4 and (opener := _fence_opener(content)) is not None:
            kind = "fence"
            fence = opener
        elif (
            indent < 4
            and html
            and (start := _html_start(stripped, paragraph)) is not None
        ):
            kind = "html"
            html_block = start
        elif _HEADING.match(content) or (paragraph and _SETEXT.fullmatch(content)):
            kind = "heading"
        elif _RULE.fullmatch(content):
            kind = "rule"
        else:
            kind = "paragraph"
            continues = paragraph and not new_container
        if kind == "html" and html_block is not None:
            closer = _HTML_BLOCKS[html_block].end
            if closer is not None and closer.search(content):
                html_block = None
        result.append(
            BlockLine(_prefix_index(original, position), bool(stack), kind, continues)
        )
        paragraph = kind == "paragraph"
    return tuple(result)

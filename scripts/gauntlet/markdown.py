"""Top-level Markdown fences, one-line spans, and safe delimiter selection."""

import re
from bisect import bisect_left
from dataclasses import dataclass


# Twin of ``openProseFence`` in ``workflows/src/renderReport.js``.
def open_fence(
    text: str,
    *,
    strict: bool = False,
    intervals: list[tuple[int, int]] | None = None,
    opener_suffixes: list[tuple[int, int]] | None = None,
) -> tuple[str, int, int] | None:
    """Return the final open fence as ``(char, run length, opener offset)``.

    Collected intervals are half-open Python string offsets. ``strict`` constrains openers.
    """
    state: tuple[str, int, int] | None = None
    opened_at: int | None = None
    line_start = 0
    index = 0

    def line_run(line: str) -> tuple[str, int, int, int] | None:
        indent = 0
        while indent < 3 and indent < len(line) and line[indent] == " ":
            indent += 1
        if indent >= len(line) or line[indent] not in "`~":
            return None
        char = line[indent]
        end = indent
        while end < len(line) and line[end] == char:
            end += 1
        return char, end - indent, end, indent

    def visit(line: str, offset: int, line_end: int) -> None:
        nonlocal state, opened_at
        run = line_run(line)
        if state is not None:
            if (
                run is not None
                and run[0] == state[0]
                and run[1] >= state[1]
                and all(character in " \t" for character in line[run[2] :])
            ):
                state = None
                if intervals is not None and opened_at is not None:
                    intervals.append((opened_at, line_end))
            return
        if (
            run is not None
            and run[1] >= 3
            and (not strict or run[3] == 0)
            and not (run[0] == "`" and "`" in line[run[2] :])
        ):
            state = (run[0], run[1], offset + run[3])
            opened_at = offset
            if opener_suffixes is not None:
                opener_suffixes.append((offset + run[2], offset + len(line)))

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
    return state


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


def paired_code_spans(
    index: TickRunIndex, *, suffix_retry: bool = False
) -> list[tuple[int, int, int]]:
    """Select paired spans with the requested unmatched-run policy."""
    spans: list[tuple[int, int, int]] = []
    run_index = 0
    while run_index < len(index.runs):
        run = index.runs[run_index]
        opener = run.start + int(run.escaped)
        width = run.width - int(run.escaped)
        if width <= 0:
            run_index += 1
            continue
        while width > 0:
            close = span_close(index, run.end, width)
            if close is not None:
                close_end = close + width
                spans.append((opener, close, close_end))
                run_index += 1
                while run_index < len(index.runs) and index.runs[run_index].start < close_end:
                    run_index += 1
                break
            if not suffix_retry:
                run_index += 1
                break
            width -= 1
            opener += 1
        else:
            run_index += 1
    return spans


def code_spans(text: str) -> tuple[list[tuple[int, int]], list[tuple[int, int]]]:
    """Pair equal-width one-line spans outside top-level fences; escaped openers skip."""
    fences: list[tuple[int, int]] = []
    spans: list[tuple[int, int]] = []
    open_fence(text, intervals=fences)
    offset = 0
    fence_index = 0
    for line in text.split("\n"):
        while fence_index < len(fences) and fences[fence_index][1] <= offset:
            fence_index += 1
        if (
            fence_index < len(fences)
            and fences[fence_index][0] <= offset < fences[fence_index][1]
        ):
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
    """Padding separates backtick edges; outbound locations also preserve space edges."""
    runs = re.findall(r"`+", value)
    delimiter = "`" * (max((len(run) for run in runs), default=0) + 1)
    edges = ("`", " ") if pad_space_edges else ("`",)
    padding = " " if value.startswith(edges) or value.endswith(edges) else ""
    return f"{delimiter}{padding}{value}{padding}{delimiter}"

"""Top-level Markdown fences, one-line spans, and safe delimiter selection."""

import re


# Twin of ``foldProse`` in ``workflows/src/renderReport.js``.
def open_fence(
    text: str,
    *,
    strict: bool = False,
    intervals: list[tuple[int, int]] | None = None,
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


def escaped_tick(text: str, index: int) -> bool:
    backslashes = 0
    index -= 1
    while index >= 0 and text[index] == "\\":
        backslashes += 1
        index -= 1
    return bool(backslashes % 2)


def span_close(line: str, start: int) -> int | None:
    """Find an equal-width closer; backslashes inside a CommonMark span are literal."""
    end = start
    while end < len(line) and line[end] == "`":
        end += 1
    width = end - start
    cursor = end
    while cursor < len(line):
        tick = line.find("`", cursor)
        if tick < 0:
            return None
        after = tick
        while after < len(line) and line[after] == "`":
            after += 1
        if after - tick == width:
            return tick
        cursor = after
    return None


def code_spans(text: str) -> tuple[list[tuple[int, int]], list[tuple[int, int]]]:
    """Pair equal-width one-line spans outside top-level fences; escaped openers skip."""
    fences: list[tuple[int, int]] = []
    spans: list[tuple[int, int]] = []
    open_fence(text, intervals=fences)
    offset = 0
    for line in text.split("\n"):
        if any(start <= offset < end for start, end in fences):
            offset += len(line) + 1
            continue
        cursor = 0
        while cursor < len(line):
            start = line.find("`", cursor)
            if start < 0:
                break
            end = start
            while end < len(line) and line[end] == "`":
                end += 1
            if escaped_tick(line, start):
                cursor = start + 1
                continue
            close = span_close(line, start)
            if close is None:
                cursor = end
                continue
            width = end - start
            spans.append((offset + start, offset + close + width))
            cursor = close + width
        offset += len(line) + 1
    return spans, fences


def fence_run(payload: str, minimum: int = 3) -> str:
    """GitLab nests suggestions; GitHub Apply accepts fences of four or more ticks."""
    runs = re.findall(r"`+", payload)
    length = max(minimum, max((len(run) for run in runs), default=0) + 1)
    return "`" * length


def code_span(value: str, *, pad_space_edges: bool = False) -> str:
    """Padding separates backtick edges; outbound locations also preserve space edges."""
    runs = re.findall(r"`+", value)
    delimiter = "`" * (max((len(run) for run in runs), default=0) + 1)
    edges = ("`", " ") if pad_space_edges else ("`",)
    padding = " " if value.startswith(edges) or value.endswith(edges) else ""
    return f"{delimiter}{padding}{value}{padding}{delimiter}"

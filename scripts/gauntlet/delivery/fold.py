"""UTF-8 delivery budgets and folding of prepared, assembled Markdown sections."""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal, TypedDict

from gauntlet.markdown import code_spans, fence_closer, open_fence

Platform = Literal["github", "gitlab"]
Surface = Literal["summary", "inline", "discussion", "note"]


class SurfaceLimit(TypedDict):
    surface: str
    bytes: int


class PlatformLimits(TypedDict):
    label: str
    surfaces: dict[Surface, SurfaceLimit]


@dataclass(frozen=True, slots=True)
class BodyLimit:
    label: str
    surface: str
    bytes: int


PLATFORM_BODY_LIMITS: dict[Platform, PlatformLimits] = {
    "github": {
        "label": "GitHub",
        "surfaces": {
            "summary": {"surface": "review body", "bytes": 65536},
            "inline": {"surface": "inline review comment", "bytes": 65536},
        },
    },
    "gitlab": {
        "label": "GitLab",
        "surfaces": {
            "summary": {"surface": "summary note", "bytes": 1000000},
            "discussion": {"surface": "inline discussion", "bytes": 1000000},
            "note": {"surface": "corroborator note", "bytes": 1000000},
        },
    },
}


def utf8_len(text: str) -> int:
    return len(text.encode("utf-8"))


def body_limit(platform: Platform, surface: Surface = "summary") -> BodyLimit:
    limits = PLATFORM_BODY_LIMITS[platform]
    row = limits["surfaces"][surface]
    return BodyLimit(limits["label"], row["surface"], row["bytes"])


def _codepoint_prefix(text: str, allowance: int) -> str:
    if allowance <= 0:
        return ""
    pieces = []
    used = 0
    for character in text:
        size = utf8_len(character)
        if used + size > allowance:
            break
        pieces.append(character)
        used += size
    return "".join(pieces)


def _retreat_inside_span(
    text: str, cut: int, intervals: Sequence[tuple[int, int]]
) -> str:
    for start, end in intervals:
        if start < cut < end:
            return text[:start]
    return text[:cut]


def _drop_last_line(prefix: str) -> str:
    """Drop the final logical line, accepting LF, CRLF, and lone CR."""

    ended_with_line_ending = prefix.endswith(("\r", "\n"))
    end = len(prefix)
    if prefix.endswith("\r\n"):
        end -= 2
    elif prefix.endswith(("\r", "\n")):
        end -= 1
    separators = [prefix.rfind("\n", 0, end), prefix.rfind("\r", 0, end)]
    line_start = max(separators)
    if line_start < 0:
        return ""
    if ended_with_line_ending:
        return prefix[: line_start + 1]
    if prefix[line_start] == "\n" and line_start and prefix[line_start - 1] == "\r":
        line_start -= 1
    return prefix[:line_start]


def _retreat_fold_prefix(
    prefix: str,
    *,
    cut_inside_line: bool,
    suggestion_start: int | None = None,
    intervals: Sequence[tuple[int, int]] = (),
) -> tuple[str, bool]:
    if suggestion_start is not None:
        prefix = prefix[:suggestion_start]
        cut_inside_line = False
    elif cut_inside_line and not prefix.endswith(("\n", "\r")):
        prefix = prefix[:-1]
    else:
        prefix = _drop_last_line(prefix)
        cut_inside_line = False
    prefix = _retreat_inside_span(prefix, len(prefix), intervals)
    return prefix, cut_inside_line


def _open_suggestion_line(
    prefix: str, state: tuple[str, int, int] | None
) -> int | None:
    if state is None:
        return None
    _char, length, offset = state
    line_start = max(prefix.rfind("\n", 0, offset), prefix.rfind("\r", 0, offset)) + 1
    line_end = len(prefix)
    for separator in ("\n", "\r"):
        candidate = prefix.find(separator, offset)
        if candidate >= 0:
            line_end = min(line_end, candidate)
    line = prefix[line_start:line_end]
    delimiter = offset - line_start
    if line[delimiter + length :].startswith("suggestion"):
        return line_start
    return None


def _fold(
    text: str,
    allowance: int,
    limit: BodyLimit,
    *,
    protect_suggestions: bool,
) -> tuple[str, int]:
    """Platform budgets count bytes; folds must preserve lines and code points."""
    total = utf8_len(text)
    intervals, _fences = code_spans(text)

    def fold_line_for(byte_count: int) -> str:
        return (
            f"_[folded: {byte_count} more bytes; this {limit.surface} reached the "
            f"{limit.bytes}-byte {limit.label} body limit]_"
        )

    # Reserve the fold line at the maximum digit count and the shortest possible
    # synthetic closer. The final assembly measures the actual fence and retreats
    # whole lines when the closer is longer.
    reserve = utf8_len(f"\n\n{fold_line_for(total)}\n{fence_closer('```')}")
    prefix = ""
    cut_inside_line = False
    if allowance >= reserve:
        prefix_allowance = allowance - reserve
        for index, line in enumerate(text.split("\n")):
            next_part = line if index == 0 else f"\n{line}"
            remaining = prefix_allowance - utf8_len(prefix)
            if utf8_len(next_part) <= remaining:
                prefix += next_part
                continue
            if utf8_len(line) > prefix_allowance:
                partial = _codepoint_prefix(next_part, remaining)
                prefix += partial
                cut_inside_line = bool(partial) and not partial.endswith(("\n", "\r"))
            break

    prefix = _retreat_inside_span(text, len(prefix), intervals)

    while True:
        state = open_fence(prefix)
        suggestion_start = (
            _open_suggestion_line(prefix, state) if protect_suggestions else None
        )
        if suggestion_start is not None:
            # A partial committable suggestion is worse than omitting its patch:
            # closing it would turn an incomplete patch into a valid wrong patch.
            prefix, cut_inside_line = _retreat_fold_prefix(
                prefix,
                cut_inside_line=cut_inside_line,
                suggestion_start=suggestion_start,
                intervals=intervals,
            )
            continue

        closer = "" if state is None else state[0] * state[1]
        dropped_bytes = total - utf8_len(prefix)
        fold_line = fold_line_for(dropped_bytes)
        separator = "" if not closer or prefix.endswith(("\n", "\r")) else "\n"
        folded = f"{prefix}{separator}{closer}\n\n{fold_line}"
        if utf8_len(folded) <= allowance or not prefix:
            return folded, dropped_bytes
        # After an overlong-line cut the prefix ends mid-line, so retreat one code
        # point at a time: dropping the line discards the partial opener run
        # pinned by the PARTIAL fixture row.
        prefix, cut_inside_line = _retreat_fold_prefix(
            prefix,
            cut_inside_line=cut_inside_line,
            intervals=intervals,
        )


def fold_review_body(text: str, allowance: int, platform: Platform) -> tuple[str, int]:
    return _fold(text, allowance, body_limit(platform), protect_suggestions=False)


def fold_inline_body(
    sections: str, allowance: int, platform: Platform, surface: Surface
) -> tuple[str, int]:
    return _fold(
        sections, allowance, body_limit(platform, surface), protect_suggestions=True
    )

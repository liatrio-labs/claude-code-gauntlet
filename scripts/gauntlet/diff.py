"""Walk unified diffs and collect typed facts under explicit path policies."""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from typing import Literal, cast


@dataclass(frozen=True, slots=True)
class HeaderEvent:
    kind: Literal["git_header", "old_path", "new_path"]
    value: str


@dataclass(frozen=True, slots=True)
class HunkEvent:
    old_line: int
    new_line: int
    old_count: int
    new_count: int


@dataclass(frozen=True, slots=True)
class RawHunk:
    new_path: str | None
    hunk: HunkEvent
    text: str


@dataclass(frozen=True, slots=True)
class LineEvent:
    old_line: int | None
    new_line: int | None
    text: str


DiffEvent = HeaderEvent | HunkEvent | LineEvent
LineKey = tuple[str, int]
PostingPathPolicy = Literal["git-prefixed", "glab-verbatim"]
DiffPathPolicy = Literal["git-prefixed", "glab-verbatim", "verify-both-spellings"]


@dataclass(frozen=True, slots=True)
class DiffFacts:
    valid_lines: Mapping[LineKey, int | None]
    new_files: frozenset[str]
    old_paths: Mapping[str, str]
    line_texts: Mapping[LineKey, str]


@dataclass(frozen=True, slots=True)
class DiffCounts:
    added: int
    removed: int
    binary_files: int


# Prefixes may be syntax or real directories; policies decide after decoding.
_OLD_HEADER_RE = re.compile(r"^--- (.+)$")
_NEW_HEADER_RE = re.compile(r"^\+\+\+ (.+)$")
_GIT_HEADER_PREFIX = "diff --git "
_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
_DIFF_PREFIX_RE = re.compile(r"^[ab]/")
# Match default a/ because git-prefixed strips that prefix; mnemonic prefixes
# deliberately fail closed rather than strip the wrong component. The quote
# admits a C-quoted first file. First-line anchoring prevents bare body markers
# from changing keying. Reports key git-shaped captures git-style; live GitLab
# instead requires per-block proof, so quoted or mismatched pairs can differ.
_GIT_SHAPED_RE = re.compile(r'\Adiff --git "?a/')

# C quoting encodes bytes, with letter escapes or one to three octal digits.
_C_ESCAPES = {
    ord("a"): 0x07,
    ord("b"): 0x08,
    ord("f"): 0x0C,
    ord("n"): 0x0A,
    ord("r"): 0x0D,
    ord("t"): 0x09,
    ord("v"): 0x0B,
    ord("\\"): 0x5C,
    ord('"'): 0x22,
}
_BACKSLASH = 0x5C
_OCTAL_DIGITS = range(0x30, 0x38)


def _decode_header_path(field: str) -> str:
    """Undo git's TAB terminator for spaces and byte-wise C quoting.

    Git quotes the whole field outside its prefix for controls, quotes,
    backslashes and, unless core.quotePath=false, non-ASCII bytes.
    Literal TAB or quote-wrapped glab names are mis-read, an accepted limitation
    that demotes findings on those names to summary-only.
    Malformed fields pass through verbatim to keep the walk lossless.
    """
    path = field.split("\t", 1)[0]
    if len(path) < 2 or not path.startswith('"') or not path.endswith('"'):
        return path

    # Octal escapes name bytes of UTF-8, so resolve them before decoding text.
    quoted = path[1:-1].encode("utf-8", "surrogateescape")
    decoded = bytearray()
    index = 0
    while index < len(quoted):
        byte = quoted[index]
        index += 1
        if byte != _BACKSLASH:
            decoded.append(byte)
            continue
        if index >= len(quoted):
            return field
        if quoted[index] in _C_ESCAPES:
            decoded.append(_C_ESCAPES[quoted[index]])
            index += 1
            continue
        end = index
        while end < len(quoted) and end - index < 3 and quoted[end] in _OCTAL_DIGITS:
            end += 1
        if end == index:
            return field
        value = int(quoted[index:end], 8)
        if value > 0xFF:
            return field
        decoded.append(value)
        index = end

    try:
        return bytes(decoded).decode("utf-8")
    except UnicodeDecodeError:
        return field


def _walk_diff_indexed(lines: list[str]) -> Iterator[tuple[int, DiffEvent]]:
    old_line = 0
    new_line = 0
    old_rem = 0
    new_rem = 0

    for line_index, raw_line in enumerate(lines):
        if old_rem <= 0 and new_rem <= 0:
            if raw_line.startswith(_GIT_HEADER_PREFIX):
                yield (
                    line_index,
                    HeaderEvent("git_header", raw_line[len(_GIT_HEADER_PREFIX) :]),
                )
                continue

            old_match = _OLD_HEADER_RE.match(raw_line)
            if old_match:
                yield (
                    line_index,
                    HeaderEvent("old_path", _decode_header_path(old_match.group(1))),
                )
                continue

            new_match = _NEW_HEADER_RE.match(raw_line)
            if new_match:
                yield (
                    line_index,
                    HeaderEvent("new_path", _decode_header_path(new_match.group(1))),
                )
                continue

            hunk_match = _HUNK_RE.match(raw_line)
            if hunk_match:
                old_start, old_count, new_start, new_count = hunk_match.groups()
                old_line = int(old_start)
                new_line = int(new_start)
                # Unified diffs omit count 1; a missing group never means an empty side.
                old_rem = 1 if old_count is None else int(old_count)
                new_rem = 1 if new_count is None else int(new_count)
                yield line_index, HunkEvent(old_line, new_line, old_rem, new_rem)
                continue

            # Header-zone noise must not become phantom lines in the preceding file.
            continue

        # ---/+++ body content is indistinguishable from headers without budgets.
        # Deleted bodies must drain too, or the next file's headers disappear here.
        if raw_line.startswith("\\"):
            # `\ No newline at end of file` belongs to neither side.
            continue

        if raw_line.startswith("+"):
            new_rem -= 1
            yield line_index, LineEvent(None, new_line, raw_line[1:])
            new_line += 1
        elif raw_line.startswith("-"):
            old_rem -= 1
            yield line_index, LineEvent(old_line, None, raw_line[1:])
            old_line += 1
        else:
            # Bare context has no marker; slicing it would eat a content character.
            old_rem -= 1
            new_rem -= 1
            yield (
                line_index,
                LineEvent(
                    old_line,
                    new_line,
                    raw_line[1:] if raw_line.startswith(" ") else raw_line,
                ),
            )
            new_line += 1
            old_line += 1


def walk_diff(diff_text: str) -> Iterator[DiffEvent]:
    """Yield events using trusted hunk budgets to separate headers from bodies."""
    lines, _ = _split_lines(diff_text)

    for _, event in _walk_diff_indexed(lines):
        yield event


def _split_lines(diff_text: str) -> tuple[list[str], bool]:
    # Git treats form feeds, vertical tabs, NEL and Unicode separators as content.
    # splitlines() would drain budgets early and shift the header/body boundary.
    lines = diff_text.split("\n")
    has_final_newline = lines[-1] == ""
    if has_final_newline:
        # A terminating split tail would mint a phantom line in a truncated hunk.
        lines.pop()
    return lines, has_final_newline


def _build_raw_hunk(
    lines: list[str],
    has_final_newline: bool,
    new_path: str | None,
    hunk: HunkEvent,
    start: int,
    last_body: int,
) -> RawHunk:
    end = last_body
    # The walker yields no event for a marker line, so a trailing one is found here.
    if end + 1 < len(lines) and lines[end + 1].startswith("\\"):
        end += 1
    text = "\n".join(lines[start : end + 1])
    if end < len(lines) - 1 or has_final_newline:
        text += "\n"
    return RawHunk(new_path, hunk, text)


def raw_hunks(diff_text: str) -> Iterator[RawHunk]:
    """Yield budgeted hunks with their decoded new path and original text."""
    lines, has_final_newline = _split_lines(diff_text)

    current_path: str | None = None
    hunk: HunkEvent | None = None
    hunk_path: str | None = None
    start = last_body = 0
    for line_index, event in _walk_diff_indexed(lines):
        if isinstance(event, HeaderEvent) and event.kind == "new_path":
            current_path = event.value
        elif isinstance(event, HunkEvent):
            if hunk is not None:
                yield _build_raw_hunk(
                    lines, has_final_newline, hunk_path, hunk, start, last_body
                )
            hunk = event
            hunk_path = current_path
            start = last_body = line_index
        elif isinstance(event, LineEvent):
            last_body = line_index

    if hunk is not None:
        yield _build_raw_hunk(
            lines, has_final_newline, hunk_path, hunk, start, last_body
        )


def _strip_ab_prefix(path: str) -> str:
    return _DIFF_PREFIX_RE.sub("", path)


def _verify_both_spellings(path: str) -> tuple[str, ...]:
    # Headers cannot distinguish a synthetic prefix from a real a/ or b/ directory.
    # Membership-only use permits the union without losing either spelling;
    # the accepted cost is a literal b/x finding cross-matching a diff touching x.
    stripped = _strip_ab_prefix(path)
    return (path,) if stripped == path else (path, stripped)


def _git_header_agrees(git_header: str | None, old_side: str, new_side: str) -> bool:
    """Compare the whole header: unquoted spaces make splitting undecidable.

    A null old side borrows the new name. Decoded TAB/quoted fields may fail
    reconstruction, so those glab pairs retain their unproven spelling.
    """
    old_name = old_side[2:] if old_side.startswith("a/") else None
    new_name = new_side[2:] if new_side.startswith("b/") else None
    if old_side == "/dev/null":
        old_name = new_name
    return (
        old_name is not None
        and new_name is not None
        and git_header == f"a/{old_name} b/{new_name}"
    )


def posting_policy(platform: Literal["github", "gitlab"]) -> PostingPathPolicy:
    return "git-prefixed" if platform == "github" else "glab-verbatim"


def patch_report_policy(diff_text: str) -> PostingPathPolicy:
    return "git-prefixed" if _GIT_SHAPED_RE.match(diff_text) else "glab-verbatim"


def parse_diff(diff_text: str, *, policy: DiffPathPolicy) -> DiffFacts:
    """Collect new-side addressability and text, retaining old context numbers.

    GitLab context positions need both numbers and the pre-rename old path.
    Added files must omit old_path to avoid HTTP 500. Plain empty-old hunks
    cannot distinguish additions from edits of empty files and guess added;
    proven git blocks instead use the null old side. Empty successful diffs
    stay present; failed retrieval stays with callers.
    """
    valid_lines: dict[LineKey, int | None] = {}
    line_texts: dict[LineKey, str] = {}
    new_files: set[str] = set()
    old_paths: dict[str, str] = {}
    git_header: str | None = None
    pending_old_side: str | None = None
    current_paths: tuple[str, ...] = ()
    zero_old_hunk_means_added = True

    for event in walk_diff(diff_text):
        if isinstance(event, HeaderEvent):
            if event.kind == "git_header":
                git_header = event.value
                # An unpaired old header cannot belong to the next git block.
                pending_old_side = None
            elif event.kind == "old_path":
                pending_old_side = event.value
            else:
                old_side, new_side = pending_old_side, event.value
                pending_old_side = None
                git_style = (
                    policy == "glab-verbatim"
                    and old_side is not None
                    and _git_header_agrees(git_header, old_side, new_side)
                )
                # Even a failed proof belongs to only this pair.
                git_header = None
                if policy == "verify-both-spellings":
                    # Null clears the previous aliases before body budgets drain.
                    current_paths = (
                        ()
                        if new_side == "/dev/null"
                        else _verify_both_spellings(new_side)
                    )
                    continue
                if policy == "git-prefixed" or git_style:
                    old_side = None if old_side is None else old_side.removeprefix("a/")
                    new_side = new_side.removeprefix("b/")
                zero_old_hunk_means_added = not git_style
                current_paths = () if new_side == "/dev/null" else (new_side,)
                if current_paths:
                    if old_side == "/dev/null":
                        new_files.add(new_side)
                    elif old_side is not None:
                        old_paths[new_side] = old_side
            continue
        if isinstance(event, HunkEvent):
            if (
                policy != "verify-both-spellings"
                and zero_old_hunk_means_added
                and event.old_line == 0
                and event.old_count == 0
            ):
                new_files.update(current_paths)
            continue
        if event.new_line is not None:
            for path in current_paths:
                key = (path, event.new_line)
                valid_lines[key] = event.old_line
                line_texts[key] = event.text
    return DiffFacts(valid_lines, frozenset(new_files), old_paths, line_texts)


def diff_counts(diff_text: str) -> DiffCounts:
    added = 0
    removed = 0
    for event in walk_diff(diff_text):
        if isinstance(event, LineEvent):
            if event.new_line is not None and event.old_line is None:
                added += 1
            elif event.old_line is not None and event.new_line is None:
                removed += 1
    # Binary prose is a raw-text statistic, including malformed or body-zone input.
    binary_files = sum(
        line.startswith("Binary files ") and line.rstrip("\r").endswith(" differ")
        for line in diff_text.split("\n")
    )
    return DiffCounts(added, removed, binary_files)


def _resolve_spelling(facts: DiffFacts, path: str, line: int | None) -> str | None:
    # Exact wins for real a/b directories. Missing/deleted siblings can still cross
    # resolve; the fence ambiguity check sees addressable keys only.
    if (path, line) in facts.valid_lines:
        return path
    stripped = _strip_ab_prefix(path)
    return stripped if (stripped, line) in facts.valid_lines else None


def is_line_valid(facts: DiffFacts | None, path: str, line: int | None) -> bool:
    return facts is None or _resolve_spelling(facts, path, line) is not None


def diff_path_spelling(facts: DiffFacts | None, path: str, line: int | None) -> str:
    spelling = None if facts is None else _resolve_spelling(facts, path, line)
    return path if spelling is None else spelling


def old_line_for(facts: DiffFacts | None, path: str, line: int | None) -> int | None:
    if facts is None:
        return None
    # Use the same resolution as validity, or accepted prefix aliases lose old_line.
    spelling = _resolve_spelling(facts, path, line)
    return (
        None if spelling is None else facts.valid_lines[cast(LineKey, (spelling, line))]
    )


def valid_lines_for_file(facts: DiffFacts | None, path: str) -> list[int] | None:
    if facts is None:
        return None
    # There is no query line to resolve; diagnostics need the union of both spellings.
    stripped = _strip_ab_prefix(path)
    lines = sorted({line for fp, line in facts.valid_lines if fp in (path, stripped)})
    return lines[:10]


def range_is_valid(facts: DiffFacts | None, path: str, start: int, end: int) -> bool:
    # A contiguous run of valid lines implies the single hunk GitHub requires
    # for a multi-line comment.
    # Short-circuit on a miss so a bogus huge end does not force a huge scan.
    return facts is None or all(
        is_line_valid(facts, path, line) for line in range(start, end + 1)
    )


def is_new_file(facts: DiffFacts | None, resolved_path: str) -> bool:
    # Already resolved: another strip could collide a modified real a/ file with a new one.
    return facts is not None and resolved_path in facts.new_files


def old_path_for(facts: DiffFacts | None, resolved_path: str) -> str:
    return (
        resolved_path
        if facts is None
        else facts.old_paths.get(resolved_path, resolved_path)
    )


def span_texts(
    facts: DiffFacts | None, resolved_path: str, start: int, end: int
) -> list[str] | None:
    if facts is None:
        return None
    # Partial content means unavailable, never "no difference". Keep one exact spelling.
    texts = []
    for line in range(start, end + 1):
        key = (resolved_path, line)
        if key not in facts.line_texts:
            return None
        texts.append(facts.line_texts[key])
    return texts


def path_is_ambiguous(facts: DiffFacts | None, raw_path: str) -> bool:
    if facts is None:
        return False
    stripped = _strip_ab_prefix(raw_path)
    if stripped == raw_path:
        return False
    # Deletions, header-only paths and pre-rename names are not addressable keys.
    paths = {path for path, _ in facts.valid_lines}
    return raw_path in paths and stripped in paths

"""Pure suggested-fix validation and platform apply sites."""

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Final, Literal, TypeGuard, cast

from gauntlet import diff, registry
from gauntlet.diff import DiffFacts
from gauntlet.text import has_marker_opener, redact_secrets

__all__ = [
    "GITLAB_SUGGESTION_OFFSET_CAP",
    "ApplyRange",
    "ApplySite",
    "FenceOffsets",
    "FixReason",
    "FixVerdict",
    "GitHubApplySite",
    "GitLabApplySite",
    "OverlapCandidate",
    "demote",
    "evaluate_fix",
    "fix_code_text",
    "format_fix_warning",
    "github_apply_range",
    "gitlab_apply_range",
    "is_plain_int",
    "overlap_losers",
    "ranges_overlap",
]

ApplyRange = tuple[int, int]
FenceOffsets = tuple[int, int]
FixReason = Literal[
    "non_string",
    "empty",
    "redacted",
    "marker_shaped",
    "missing_end_line",
    "invalid_range",
    "no_diff_oracle",
    "range_not_in_diff",
    "anchor_mismatch",
    "span_exceeds_platform_cap",
    "no_op_replacement",
    "indentation_mismatch",
    "replacement_too_large",
    "carriage_return",
    "overlaps_kept_fence",
]

# GitLab silently clamps larger offsets, so emitting one would apply another range.
# Suggestible::MAX_LINES_CONTEXT is independent of the fence payload bounds.
GITLAB_SUGGESTION_OFFSET_CAP: Final[int] = 100


def is_plain_int(value: object) -> TypeGuard[int]:
    """True only for a real ``int`` — ``True`` and ``2.0`` both hash equal to the
    integer key, so they survive every dict lookup and equality check; type is the
    only thing that separates them from the integer they impersonate."""
    return isinstance(value, int) and not isinstance(value, bool)


@dataclass(frozen=True, slots=True)
class FixVerdict:
    keep: bool
    reason: FixReason | None
    apply_range: ApplyRange | None

    @property
    def downgrade_reason(self) -> FixReason:
        assert not self.keep and self.reason is not None
        return self.reason


def demote(verdict: FixVerdict) -> FixVerdict:
    # Per-finding failures outrank overlap demotion.
    return (
        replace(verdict, keep=False, reason="overlaps_kept_fence")
        if verdict.keep
        else verdict
    )


@dataclass(frozen=True, slots=True)
class GitHubApplySite:
    apply_range: tuple[object, object]
    multiline: bool = False


@dataclass(frozen=True, slots=True)
class GitLabApplySite:
    apply_range: tuple[object, object]
    offsets: FenceOffsets | None = None
    cap_exceeded: bool = False


ApplySite = GitHubApplySite | GitLabApplySite


@dataclass(frozen=True, slots=True)
class OverlapCandidate:
    index: int
    path_lookup: str
    apply_range: ApplyRange


# The gate measures and the fence carries the same text. One final LF is a
# terminator; edge blank lines are content that a one-click patch must preserve.
def fix_code_text(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        value = str(value)
    if not value.strip():
        return None
    return value[:-1] if value.endswith("\n") else value


def _leading_whitespace_charset(lines: Sequence[str]) -> set[str]:
    # Unindented lines say nothing about the surrounding indentation style.
    charset: set[str] = set()
    for line in lines:
        charset.update(line[: len(line) - len(line.lstrip(" \t"))])
    return charset


def _check_fix(
    finding: Mapping[str, object],
    *,
    apply_range: tuple[object, object] | None,
    facts: DiffFacts | None,
    path_lookup: str,
) -> FixReason | None:
    # Anchors fail open without facts; a misplaced one-click patch corrupts the file.
    # Raw-path collisions therefore fail closed before range membership is checked.
    raw = finding["suggested_fix_code"]
    if not isinstance(raw, str):
        return "non_string"
    text = fix_code_text(raw)
    if text is None:
        return "empty"
    # CommonMark renders "foo\rbar\rbaz" as three lines, but split("\n") sees
    # one. Reject CR before no-op, indentation and size checks so their
    # measurements describe the document a one-click apply would commit.
    if "\r" in text:
        return "carriage_return"
    # Gate on the ORIGINAL bytes: a fence carrying a literal `[REDACTED]` would be
    # committed by one click. Passing here means the render-time redaction is a
    # guaranteed no-op, so the posted fence is byte-identical to what was checked.
    if redact_secrets(text) != text:
        return "redacted"
    if has_marker_opener(text):
        return "marker_shaped"

    line = finding.get("line")
    end_line = finding.get("end_line")
    if end_line is None:
        # A patch's stated range must be explicit. An absent end_line — including
        # one deleted for exceeding maxLineSpan — is exactly how a multi-line
        # replacement lands on a single-line anchor and corrupts the file.
        return "missing_end_line"
    if (
        not is_plain_int(line)
        or not is_plain_int(end_line)
        or line < 1
        or end_line < line
    ):
        return "invalid_range"
    if facts is None:
        return "no_diff_oracle"
    if diff.path_is_ambiguous(facts, cast(str, finding.get("file", "?"))):
        return "no_diff_oracle"
    if not diff.range_is_valid(facts, path_lookup, line, end_line):
        return "range_not_in_diff"
    if apply_range != (line, end_line):
        return "anchor_mismatch"

    replacement = text.split("\n")
    span = diff.span_texts(facts, path_lookup, line, end_line)
    if span is None:
        # Partial text is unavailable, never permission to skip content checks.
        # A span needs one exact spelling even when membership resolves per line.
        return "no_diff_oracle"
    # A trailing CR is transport (a CRLF diff carries one on every line), not
    # content, and `fix_code_text` already took the replacement's terminating
    # newline off — so neither side's line terminators decide this. An EDGE
    # BLANK LINE survives that normalization and is compared as content: a
    # patch that only adds one is a change, not a no-op.
    if replacement == [ln.rstrip("\r") for ln in span]:
        return "no_op_replacement"
    span_indent = _leading_whitespace_charset(span)
    fix_indent = _leading_whitespace_charset(replacement)
    # Deliberately weak, and language-agnostic: a legitimate re-indentation
    # passes, and only a tab/space charset conflict — the one that silently
    # corrupts a file whichever language it is written in — is caught.
    if (span_indent == {" "} and "\t" in fix_indent) or (
        span_indent == {"\t"} and " " in fix_indent
    ):
        return "indentation_mismatch"
    # Both runtimes bound normalized payloads by split-LF lines and code points.
    # Prose remains uncapped because a human reads it before acting.
    if len(replacement) > registry.FIX_MAX_LINES or len(text) > registry.FIX_MAX_CHARS:
        return "replacement_too_large"
    return None


def evaluate_fix(
    finding: Mapping[str, object],
    *,
    apply_range: tuple[object, object] | None,
    facts: DiffFacts | None,
    mismatch_reason: FixReason = "anchor_mismatch",
) -> FixVerdict:
    evaluated = None
    if apply_range is not None and all(is_plain_int(v) for v in apply_range):
        evaluated = cast(ApplyRange, apply_range)
    if "suggested_fix_code" not in finding:
        return FixVerdict(True, None, evaluated)
    # Resolve at the finding's own line so verdict and render agree; None means no fence can apply there.
    path_lookup = diff.diff_path_spelling(
        facts,
        cast(str, finding.get("file", "?")),
        cast(int | None, finding.get("line")),
    )
    reason = _check_fix(
        finding, apply_range=apply_range, facts=facts, path_lookup=path_lookup
    )
    keep = reason is None
    # Per-finding failures outrank set-level demotion; only anchor failure is renamed.
    if not keep and reason == "anchor_mismatch":
        reason = mismatch_reason
    return FixVerdict(keep, reason, evaluated)


def github_apply_range(
    facts: DiffFacts | None,
    filepath: str,
    line: object,
    end_line: object,
) -> GitHubApplySite:
    # Preserve isinstance's bool/float comparisons here; the content gate is stricter.
    start = cast(int, line)
    # Keep multiline ranges in one hunk because an end outside it rejects the whole review.
    multiline = (
        isinstance(end_line, int)
        and end_line > start
        and diff.range_is_valid(facts, filepath, start, end_line)
    )
    return GitHubApplySite(
        (line, end_line) if multiline else (line, line), multiline=multiline
    )


def gitlab_apply_range(
    finding: Mapping[str, object], anchor: object
) -> GitLabApplySite:
    line, end_line = finding.get("line"), finding.get("end_line")
    if not is_plain_int(anchor) or not is_plain_int(line) or not is_plain_int(end_line):
        return GitLabApplySite((anchor, anchor))
    above, below = anchor - line, end_line - anchor
    # Offsets extend outward from the anchor, so an outside anchor cannot realize a range.
    if above < 0 or below < 0:
        return GitLabApplySite((anchor, anchor))
    if above > GITLAB_SUGGESTION_OFFSET_CAP or below > GITLAB_SUGGESTION_OFFSET_CAP:
        return GitLabApplySite((anchor, anchor), cap_exceeded=True)
    return GitLabApplySite((anchor - above, anchor + below), offsets=(above, below))


def ranges_overlap(a: ApplyRange, b: ApplyRange) -> bool:
    # GitLab Range#overlaps? uses closed intervals, including identical single lines.
    return max(a[0], b[0]) <= min(a[1], b[1])


def overlap_losers(records: Iterable[OverlapCandidate]) -> set[int]:
    # Delivery priority beats maximum cardinality. Losers never claim an interval.
    losers = set()
    kept_by_path: dict[str, list[ApplyRange]] = {}
    for record in records:
        kept = kept_by_path.setdefault(record.path_lookup, [])
        if any(ranges_overlap(record.apply_range, k) for k in kept):
            losers.add(record.index)
        else:
            kept.append(record.apply_range)
    return losers


def format_fix_warning(
    finding: Mapping[str, object], reason: FixReason, *, label: str
) -> str:
    return f"{label} downgraded: {finding.get('file', '?')}:{finding.get('line')} ({reason})"

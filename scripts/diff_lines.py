"""
diff_lines.py — the one unified-diff walk the retained diff parsers share.

SCOPE SPLIT, and the whole reason this module is thin: THE WALK lives here —
header zone vs. hunk-body zone, the per-hunk budgets that separate them, and the
old/new line-number advance. HEADER SEMANTICS stay in the callers. What a path
spelling means (git's synthetic ``a/``/``b/`` prefixes are diff syntax under
``gh pr diff`` and a real top-level directory under ``glab mr diff``, which
writes paths verbatim), what ``/dev/null`` implies, and which lines are worth
recording at all are decisions the two callers answer differently — folding them
in here would need a platform flag and would put one caller's answer on the
other's path.

No external dependencies. stdlib only.

Usage:
    # Standalone / SKILL.md script invocation (scripts/ is on sys.path):
    from diff_lines import walk_diff
    # From pytest run at the repo root (repo root is on sys.path):
    from scripts.diff_lines import walk_diff
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from typing import NamedTuple


class DiffEvent(NamedTuple):
    """One meaningful line of a unified diff, interpreted as far as the walk can.

    ``kind`` is one of:

    * ``"old_path"`` / ``"new_path"`` — a ``---`` / ``+++`` header, matched only
      between hunks. ``path`` is the header's text VERBATIM: no prefix stripped and
      ``/dev/null`` passed through as itself, because both are caller semantics.
    * ``"hunk"`` — an ``@@`` header. ``old_line``/``new_line`` are the sides' start
      lines; ``old_count``/``new_count`` are the RESOLVED body-line budgets. A unified
      diff omits a count exactly when that side holds one line, so the omitted spelling
      resolves to 1 here rather than reaching a caller as ``None`` — a caller reading
      the raw group would compare an added-file signal against something that is not a
      number.
    * ``"line"`` — a hunk-body line. ``new_line`` is set when the line exists on the new
      side and ``old_line`` when it exists on the old side: an added line carries only
      the former, a removed line only the latter, a context line both. ``\\ No newline
      at end of file`` belongs to neither side and yields no event at all.
    """

    kind: str
    path: str | None = None
    old_line: int | None = None
    new_line: int | None = None
    old_count: int | None = None
    new_count: int | None = None


# Prefix-free on purpose: a header's `a/`/`b/` is diff syntax on one platform and a real
# directory on the other, so the walk hands back what it read and the caller decides.
_OLD_HEADER_RE = re.compile(r"^--- (.+)$")
_NEW_HEADER_RE = re.compile(r"^\+\+\+ (.+)$")
_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def walk_diff(diff_text: str) -> Iterator[DiffEvent]:
    """Yield a :class:`DiffEvent` for every meaningful line of *diff_text*.

    The parser tracks each hunk's DECLARED old/new line budgets and matches file and
    hunk headers only BETWEEN hunks. Well-formed git/gh/glab diffs always declare
    correct counts, so the counts are trusted.
    """
    old_line = 0
    new_line = 0
    # Lines of each side still owed by the hunk being read. Both at 0 means "between
    # hunks" — the only zone where a line may be read as a header.
    old_rem = 0
    new_rem = 0

    # Split on "\n" ONLY, never str.splitlines(): that also breaks on \x0c, \x0b, \x85
    # and U+2028/U+2029, which git treats as ordinary line CONTENT. A form feed inside a
    # hunk body would become two parsed lines, draining the declared budgets one line
    # early — flipping the header/body zone boundary and shifting every line number
    # after it. The trailing "" a newline-terminated stream yields lands in the header
    # zone and matches nothing.
    for raw_line in diff_text.split("\n"):
        if old_rem <= 0 and new_rem <= 0:
            # -- header zone -------------------------------------------------
            old_match = _OLD_HEADER_RE.match(raw_line)
            if old_match:
                yield DiffEvent("old_path", path=old_match.group(1))
                continue

            new_match = _NEW_HEADER_RE.match(raw_line)
            if new_match:
                yield DiffEvent("new_path", path=new_match.group(1))
                continue

            hunk_match = _HUNK_RE.match(raw_line)
            if hunk_match:
                old_start, old_count, new_start, new_count = hunk_match.groups()
                old_line = int(old_start)
                new_line = int(new_start)
                old_rem = 1 if old_count is None else int(old_count)
                new_rem = 1 if new_count is None else int(new_count)
                yield DiffEvent(
                    "hunk",
                    old_line=old_line,
                    new_line=new_line,
                    old_count=old_rem,
                    new_count=new_rem,
                )
                continue

            # Anything else between hunks (`diff --git …`, `index …`, mode lines,
            # `Binary files … differ`) is noise. Reading it as body content is what
            # attributes a phantom line to whichever file was parsed last.
            continue

        # -- hunk-body zone --------------------------------------------------
        # Headers are NOT matched here: `--- <text>` / `+++ <text>` are body content.
        # Removing the SQL comment `-- deprecated: drop me` renders as
        # `--- deprecated: drop me` and an added `++ x` renders as `+++ x`; by prefix
        # alone neither is distinguishable from a real header. Budgets drain for every
        # body line, including a deleted file's — a body left undrained puts the NEXT
        # file's headers inside this zone, where nothing matches them.
        if raw_line.startswith("\\"):
            # `\ No newline at end of file` belongs to neither side.
            continue

        if raw_line.startswith("+"):
            new_rem -= 1
            yield DiffEvent("line", new_line=new_line)
            new_line += 1
        elif raw_line.startswith("-"):
            old_rem -= 1
            yield DiffEvent("line", old_line=old_line)
            old_line += 1
        else:
            # Context line (space- or zero-prefixed) — present on BOTH sides.
            old_rem -= 1
            new_rem -= 1
            yield DiffEvent("line", old_line=old_line, new_line=new_line)
            new_line += 1
            old_line += 1

"""Build complete or deliberately partial diff inputs for consumer tests."""

from collections.abc import Iterable, Mapping

from gauntlet.diff import DiffFacts, LineKey


def diff_facts(
    valid_lines: Mapping[LineKey, int | None],
    *,
    new_files: Iterable[str] = (),
    old_paths: Mapping[str, str] | None = None,
    line_texts: Mapping[LineKey, str] | None = None,
) -> DiffFacts:
    return DiffFacts(
        dict(valid_lines),
        frozenset(new_files),
        dict(old_paths or {}),
        dict(line_texts or {}),
    )

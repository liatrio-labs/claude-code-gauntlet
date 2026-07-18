"""Adapter: post_review.py --dry-run payload -> vendored-scorer candidates.json.

Converts the ``post-review-payload.json`` emitted by ``scripts/post_review.py
--dry-run`` into the ``candidates.json`` shape the vendored MIT scorer consumes
at its dedup->judge entry point (step2 extraction is skipped — deep-review
comments are atomic by construction).

Return contract (both public functions return a 2-tuple):

    (result, stats)

  * ``result`` is a candidates.json-shaped dict::

        {golden_url: {tool: [{"text", "path", "line", "source"}, ...]}}

  * ``stats`` is ``{"n_candidates": int, "n_skipped": int}``.

``payload_to_candidates`` maps one posted comment to one candidate with order
preserved — candidate index ``i`` corresponds to payload comment ``i`` (the
Task 13 bucket join relies on this positional correspondence). ``text`` is the
comment body verbatim (no trimming or rewriting); ``path``/``line`` are copied
from the comment (extra info harmless to the judge, which reads only ``text``).
``skipped`` payload entries are excluded from candidates but counted in
``stats["n_skipped"]``.

``merge_candidates`` unions per-PR candidate files/dicts into one candidates.json
keyed by ``golden_url``. Its ``stats["n_candidates"]`` is the total across all
inputs; ``stats["n_skipped"]`` is always 0 — skip accounting is a payload-level
concern surfaced by ``payload_to_candidates``, not retained in candidate files.

stdlib-only (CLAUDE.md).
"""

import json
import os

DEFAULT_TOOL = "deep-review"


def _load(payload_or_path):
    """Return a payload/candidates dict from a dict or a JSON file path."""
    if isinstance(payload_or_path, dict):
        return payload_or_path
    if isinstance(payload_or_path, (str, os.PathLike)):
        with open(payload_or_path, encoding="utf-8") as f:
            return json.load(f)
    raise TypeError(
        f"expected a dict or filesystem path, got {type(payload_or_path).__name__}"
    )


def _iter_comments(payload):
    """Yield ``(text, path, line)`` per posted comment, in payload order.

    GitHub: one entry per ``payload.payload.comments[]`` (``body``/``path``/
    ``line``). GitLab: one entry per ``payload.discussions[]`` (``body`` +
    ``position.new_path``/``position.new_line``).
    """
    platform = payload.get("platform")
    if platform == "github":
        comments = (payload.get("payload") or {}).get("comments", [])
        for c in comments:
            yield c.get("body"), c.get("path"), c.get("line")
    elif platform == "gitlab":
        for d in payload.get("discussions", []):
            position = d.get("position") or {}
            yield d.get("body"), position.get("new_path"), position.get("new_line")
    else:
        raise ValueError(
            f"unrecognized payload platform: {platform!r} "
            "(expected 'github' or 'gitlab')"
        )


def payload_to_candidates(payload_json, golden_url, tool=DEFAULT_TOOL):
    """Convert one PR's dry-run payload into a candidates dict.

    ``payload_json`` may be a payload dict or a path to the payload JSON file.
    Returns ``({golden_url: {tool: [candidate, ...]}}, stats)``. The tool list is
    always present (empty when there are no posted comments), never a missing key.
    """
    payload = _load(payload_json)
    candidates = [
        {"text": text, "path": path, "line": line, "source": "extracted"}
        for text, path, line in _iter_comments(payload)
    ]
    result = {golden_url: {tool: candidates}}
    stats = {
        "n_candidates": len(candidates),
        "n_skipped": len(payload.get("skipped", [])),
    }
    return result, stats


def merge_candidates(files):
    """Union per-PR candidate files/dicts into one candidates.json-shaped dict.

    ``files`` is an iterable whose elements are each a candidates dict or a path
    to one. Returns ``(merged, stats)``. Distinct golden_urls keep their own
    bucket; a golden_url present in more than one input has its per-tool lists
    concatenated (order preserved) rather than overwritten.
    """
    merged = {}
    n_candidates = 0
    for entry in files:
        data = _load(entry)
        for golden_url, tools in data.items():
            dest = merged.setdefault(golden_url, {})
            for tool_name, cands in tools.items():
                dest.setdefault(tool_name, []).extend(cands)
                n_candidates += len(cands)
    stats = {"n_candidates": n_candidates, "n_skipped": 0}
    return merged, stats

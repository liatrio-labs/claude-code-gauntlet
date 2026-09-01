#!/usr/bin/env python3
r"""Emit the confusable-fold + invisible-strip tables into both filter twins from one registry.

WHY THIS EXISTS -- issue #272.

The injection filter folds cross-script/lookalike codepoints to ASCII and strips
zero-width/joiner/bidi/combining boundary-breakers before its heuristics scan
(`scripts/filter_findings.py`, `workflows/src/filterFindings.js`). The fold map is 1468
entries and the strip set 599 codepoints -- far too many to hand-respell in both twins.

`scripts/filter_patterns_registry.py` holds the two tables as canonical packed escaped-
codepoint string constants (`CONFUSABLE_FOLD_PACKED` / `INVISIBLE_STRIP_PACKED`); this
script decodes them and writes the packed form into a fenced block in each twin, the way
`scripts/generate_filter_patterns.py` writes the regex families -- with the same `--check`
freshness contract. It is a SIBLING of that generator rather than an extension of it: the
data is a packed STRING literal, not a regex `PatternFamily`, so it shares none of that
generator's `ruff format`/`kind`-dispatch/guard-discovery machinery, and it fences under a
DIFFERENT marker tag (`generated-from-confusable-registry`) so neither generator sees the
other's blocks as orphaned debris.

The emitted block is a single parenthesized/concatenated STRING constant, never an
`ast.List` of pattern strings, so `tests/test_filter_twins_unicode_guard.py`'s discovery
(which keys off `ast.List`-of-metachar-strings and `/regex/.test(` shapes) never mistakes
it for an injection family and the guard's re.*/`.test(`/union-class censuses are unmoved.

The two twins decode the packed string at module load into the `str.translate` table
(Python) / `Map`+`Set` (JS); the ASCII letter targets ride as literal characters, the
non-ASCII source codepoints as `\uXXXX` / `\U00xxxxxx` (Python) or `\uXXXX` / `\u{xxxxx}`
(JS) escapes -- 919 fold sources and 240 strip codepoints are ASTRAL, so both twins
iterate by CODE POINT (Python string iteration, JS `for...of`), never a regex char class.

Usage:
    python3 scripts/generate_confusable_tables.py           # write the generated blocks
    python3 scripts/generate_confusable_tables.py --check   # exit 1 if either twin is stale
"""

import argparse
import os
import re
import sys

sys.path.insert(0, os.path.dirname(__file__))

from filter_patterns_registry import CONFUSABLE_FOLD_PACKED, INVISIBLE_STRIP_PACKED

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

PY_REL = "scripts/filter_findings.py"
JS_REL = "workflows/src/filterFindings.js"

_MARKER_TAG = "generated-from-confusable-registry"

# `symbol` here is the Python constant name; the JS twin fences the same DATA under the
# same marker symbol, so one pair name locates the block in either file.
_MARKER_RE = re.compile(
    rf"^\s*(?:#|//) (?P<close>/)?{re.escape(_MARKER_TAG)}:(?P<symbol>\S+)"
)

# Fold entries per emitted source line, and strip codepoints per line. Both are measured
# `ruff format` fixed points for the parenthesized implicit-concatenation Python form
# (test_generate_confusable_tables.TestRuffFormatFixedPoint); the JS emitter reuses them
# for a symmetric `+`-concatenated literal (Biome's formatter is off for src/).
_FOLD_PER_LINE = 12
_STRIP_PER_LINE = 16

# The block symbol -> (source_kind, python_name, js_name). One row per fenced table.
_TABLES = (
    ("fold", "_CONFUSABLE_FOLD_PACKED", "CONFUSABLE_FOLD_PACKED"),
    ("strip", "_INVISIBLE_STRIP_PACKED", "INVISIBLE_STRIP_PACKED"),
)


def decode_fold(packed):
    """[(codepoint, ascii_letter), ...] from a packed fold string.

    Iterates by CODE POINT (Python string iteration yields one element per codepoint,
    astral included), consuming (source, letter) pairs -- byte-identical to the twins'
    own decoders."""
    out = []
    chars = iter(packed)
    for src in chars:
        out.append((ord(src), next(chars)))
    return out


def decode_strip(packed):
    """[codepoint, ...] from a packed strip string, one codepoint per element."""
    return [ord(c) for c in packed]


def _py_escape(cp):
    return f"\\u{cp:04x}" if cp <= 0xFFFF else f"\\U{cp:08x}"


def _js_escape(cp):
    return f"\\u{cp:04x}" if cp <= 0xFFFF else f"\\u{{{cp:x}}}"


def _fold_segments(pairs, escape):
    """One packed string segment per output line: escaped source codepoint + literal
    ASCII letter, `_FOLD_PER_LINE` pairs to a segment."""
    return [
        "".join(escape(cp) + letter for cp, letter in pairs[i : i + _FOLD_PER_LINE])
        for i in range(0, len(pairs), _FOLD_PER_LINE)
    ]


def _strip_segments(codepoints, escape):
    return [
        "".join(escape(cp) for cp in codepoints[i : i + _STRIP_PER_LINE])
        for i in range(0, len(codepoints), _STRIP_PER_LINE)
    ]


def _segments_for(kind, escape):
    if kind == "fold":
        return _fold_segments(decode_fold(CONFUSABLE_FOLD_PACKED), escape)
    return _strip_segments(decode_strip(INVISIBLE_STRIP_PACKED), escape)


def py_block(kind, name):
    """`NAME = (\\n    "seg"\\n    "seg"\\n)` -- the ruff-format normal form for a long
    parenthesized implicit string concatenation."""
    segments = _segments_for(kind, _py_escape)
    out = [f"{name} = ("]
    out += [f'    "{seg}"' for seg in segments]
    out.append(")")
    return out


def js_block(kind, name):
    """`const NAME =\\n  'seg' +\\n  'seg';` -- a `+`-concatenated string literal. Not a
    `const NAME =\\n  /re/;` shape, so the guard's compile-single finder never sees it."""
    segments = _segments_for(kind, _js_escape)
    out = [f"const {name} ="]
    for i, seg in enumerate(segments):
        terminator = ";" if i == len(segments) - 1 else " +"
        out.append(f"  '{seg}'{terminator}")
    return out


def find_marker_pairs(lines, rel_path):
    """{symbol: (open_index, close_index)} for every confusable-table marker pair."""
    opens = {}
    closes = {}
    for index, line in enumerate(lines):
        match = _MARKER_RE.match(line)
        if not match:
            continue
        bucket = closes if match.group("close") else opens
        symbol = match.group("symbol")
        if symbol in bucket:
            raise SystemExit(
                f"{rel_path}: duplicate marker for {symbol} "
                f"(lines {bucket[symbol] + 1} and {index + 1}); fix by hand"
            )
        bucket[symbol] = index
    unmatched = sorted(set(opens) ^ set(closes))
    if unmatched:
        raise SystemExit(
            f"{rel_path}: unmatched confusable-table marker(s) for "
            f"{', '.join(unmatched)}; fix by hand"
        )
    pairs = {}
    for symbol, open_index in opens.items():
        close_index = closes[symbol]
        if close_index <= open_index:
            raise SystemExit(
                f"{rel_path}: close marker for {symbol} precedes its open "
                f"(lines {close_index + 1} and {open_index + 1}); fix by hand"
            )
        pairs[symbol] = (open_index, close_index)
    return pairs


def fill_fences(text, rel_path, name_of, body_of):
    """Rewrite every fenced confusable-table block in `text` from the registry."""
    lines = text.split("\n")
    pairs = find_marker_pairs(lines, rel_path)
    expected = {name_of(kind): (kind, name_of(kind)) for kind, _, _ in _TABLES}
    missing = sorted(set(expected) - set(pairs))
    if missing:
        raise SystemExit(
            f"{rel_path}: no marker pair for confusable symbol(s) "
            f"{', '.join(missing)} -- place an empty pair where it belongs, then rerun"
        )
    orphans = sorted(set(pairs) - set(expected))
    if orphans:
        raise SystemExit(
            f"{rel_path}: marker pair(s) {', '.join(orphans)} match no confusable table "
            "-- remove the fence or add the table"
        )
    for symbol in sorted(pairs, key=lambda s: pairs[s][0], reverse=True):
        open_index, close_index = pairs[symbol]
        kind, name = expected[symbol]
        lines[open_index + 1 : close_index] = body_of(kind, name)
    return "\n".join(lines)


def expected_python(text):
    py_name = {kind: name for kind, name, _ in _TABLES}
    kind_of = {name: kind for kind, name, _ in _TABLES}
    return fill_fences(
        text,
        PY_REL,
        lambda kind: py_name[kind],
        lambda kind, name: py_block(kind_of[name], name),
    )


def expected_js(text):
    js_name = {kind: js for kind, _, js in _TABLES}
    kind_of = {js: kind for kind, _, js in _TABLES}
    return fill_fences(
        text,
        JS_REL,
        lambda kind: js_name[kind],
        lambda kind, name: js_block(kind_of[name], name),
    )


TARGETS = (
    (PY_REL, expected_python),
    (JS_REL, expected_js),
)


def apply_targets(repo_root=REPO_ROOT, check_only=False):
    stale = []
    for rel_path, render in TARGETS:
        abs_path = os.path.join(repo_root, rel_path)
        with open(abs_path, encoding="utf-8") as handle:
            current = handle.read()
        expected = render(current)
        if expected == current:
            continue
        stale.append(rel_path)
        if not check_only:
            with open(abs_path, "w", encoding="utf-8") as handle:
                handle.write(expected)
    return stale


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", default=REPO_ROOT)
    parser.add_argument(
        "--check",
        action="store_true",
        help="report stale generated confusable tables without writing",
    )
    args = parser.parse_args(argv)

    stale = apply_targets(args.repo_root, check_only=args.check)
    if not stale:
        print("confusable tables are current")
        return 0
    if args.check:
        sys.stderr.write(
            f"stale generated confusable tables: {', '.join(stale)}\n"
            "run: python3 scripts/generate_confusable_tables.py\n"
        )
        return 1
    print(f"regenerated: {', '.join(stale)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

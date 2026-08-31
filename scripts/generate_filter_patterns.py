#!/usr/bin/env python3
"""Emit the first-party pattern declarations into both filter twins from one registry.

WHY THIS EXISTS -- issue #241.

`scripts/filter_findings.py` and `workflows/src/filterFindings.js` carry the same
first-party injection/routing patterns, respelled by hand in each twin, including 135
hand-typed copies of an 87-character union whitespace class per file.
`tests/test_filter_twins_unicode_guard.py` proves the two spellings agree today, but a
proof of agreement is not a source of truth: adding a pattern still costs two edits and
widening the whitespace class costs 270.

`scripts/filter_patterns_registry.py` is the source; this script writes it into both
twins, the way `scripts/generate_contract_requirements.py` writes the registry's
dispatch sentences into the agent contracts, with the same `--check` freshness contract.

Two target kinds:

* **Fenced declarations.** Each generated declaration sits between a hand-placed
  marker pair naming the symbol it fences. Markers are PER SYMBOL, not one spelling per
  file -- the fenceable declarations are non-contiguous (hand-written prose comments,
  which are twin-asymmetric decision records, sit BETWEEN them and must stay OUTSIDE the
  fences), so the single-block splice this script's precedent uses would hard-fail on the
  second pair. Placing a NEW pair is a human act: this script only fills pairs that
  already exist, and `--check` fails on a registry symbol with no pair or a pair with no
  registry row.
* **Anchored line rewrites.** The inline (not-a-named-declaration) patterns are located
  by a skeleton that occurs exactly once per twin and rewritten in place, substring-
  scoped, because one of them sits mid-line inside an `if`.

Python emission is the `ruff format` normal form, not a layout of this script's choosing:
`tests/test_generate_filter_patterns.py` runs the emitted text through the repo's pinned
`ruff format` and asserts a fixed point, so the generator and the format hook can never
fight over a line.

Usage:
    python3 scripts/generate_filter_patterns.py           # write the generated blocks
    python3 scripts/generate_filter_patterns.py --check   # exit 1 if either twin is stale
"""

import argparse
import os
import re
import sys

sys.path.insert(0, os.path.dirname(__file__))

from filter_patterns_registry import (
    CONTENT_SET_ORDER,
    CONTENT_SETS,
    INLINE_SITES,
    PATTERN_FAMILIES,
    UNION_WS_INNER,
    WORD_SPLIT,
)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

PY_REL = "scripts/filter_findings.py"
JS_REL = "workflows/src/filterFindings.js"

_MARKER_TAG = "generated-from-filter-pattern-registry"
_MARKER_HINT = "do not edit; run scripts/generate_filter_patterns.py"

# Recognizes EITHER marker of EITHER comment syntax, so an orphan pair naming a symbol
# no registry row declares is reported rather than silently left to rot.
_MARKER_RE = re.compile(
    rf"^\s*(?:#|//) (?P<close>/)?{re.escape(_MARKER_TAG)}:(?P<symbol>\S+)"
)

# `re` flag name -> JS regex-literal flag letter. `ASCII` maps to nothing on purpose: a
# JS regex without `/u` already has the ASCII `\b`/`\w`/`\d` semantics `re.ASCII` buys.
_JS_FLAG_LETTERS = {
    "IGNORECASE": "i",
    "MULTILINE": "m",
    "DOTALL": "s",
    "GLOBAL": "g",
}


def blocks():
    """Every fenced declaration the registry owns, in registry order."""
    return (*PATTERN_FAMILIES, WORD_SPLIT, CONTENT_SETS)


def expand(pattern):
    """Substitute the union whitespace class for the `{WS}` token.

    A plain replace, never `str.format`: a pattern is full of `{0,40}`-style regex
    quantifiers that `str.format` would read as fields and reject.
    """
    return pattern.replace("{WS}", UNION_WS_INNER)


def _family_by_js_name(js_name):
    for row in PATTERN_FAMILIES:
        if row.js_name == js_name:
            return row
    raise SystemExit(
        f"CONTENT_SET_ORDER references unknown family {js_name!r} -- "
        "every row's `family` must name a PATTERN_FAMILIES js_name"
    )


# --- Python emission (the ruff-format normal form) ---------------------------


def py_literal(pattern):
    """A raw-string literal for `pattern`.

    A `"` inside the pattern is backslash-escaped even though a raw string does not
    strip that backslash from the value -- it is the only way to keep the literal from
    ending early, and it is exactly the source-syntax noise the guard's
    `_py_pattern_text` undoes before comparing twins. No shipped pattern needs it today
    (#255 removed the last one); the branch stays so a future one does not silently
    produce a syntax error.
    """
    return 'r"' + pattern.replace('"', '\\"') + '"'


def py_flags(flags):
    return " | ".join(f"re.{flag}" for flag in flags)


def py_block(row):
    """The Python declaration for one registry row, as a list of lines.

    Three pinned layouts, each a measured `ruff format` fixed point:

    1. a bare pattern string per line (ruff never splits a string literal);
    2. `re.compile(...)` ALWAYS exploded with a magic trailing comma -- the collapsed
       one-line form is NOT a fixed point once a pattern carries the 89-character union
       class, so emitting it would leave `--check` permanently stale;
    3. the word splitter's exploded, single-argument, NO-trailing-comma form.
    """
    name = row.python_name
    if row.kind == "str_list":
        out = [f"{name} = ["]
        out += [f"    {py_literal(expand(p))}," for p in row.patterns]
        out.append("]")
        return out
    if row.kind == "compile_list":
        out = [f"{name} = ["]
        for pattern in row.patterns:
            out.append("    re.compile(")
            out.append(f"        {py_literal(expand(pattern))},")
            out.append(f"        {py_flags(row.flags)},")
            out.append("    ),")
        out.append("]")
        return out
    if row.kind == "compile_single":
        return [
            f"{name} = re.compile(",
            f"    {py_literal(expand(row.patterns[0]))},",
            f"    {py_flags(row.flags)},",
            ")",
        ]
    if row.kind == "word_split":
        return [
            f"{name} = re.compile(",
            f"    {py_literal(expand(row.patterns[0]))}",
            ")",
        ]
    if row.kind == "content_sets":
        out = [f"{name} = ("]
        for content_set in CONTENT_SET_ORDER:
            family = _family_by_js_name(content_set.family)
            out.append(f'    ("{content_set.phrase}", tuple({family.python_name})),')
        out.append(")")
        return out
    raise SystemExit(f"unknown registry kind {row.kind!r} for {name}")


# --- JS emission -------------------------------------------------------------


def js_flags(flags):
    return "".join(_JS_FLAG_LETTERS[flag] for flag in flags if flag in _JS_FLAG_LETTERS)


def js_literal(pattern, flags):
    """A regex literal for `pattern`.

    EVERY literal `/` is escaped as `\\/`, including where the syntax does not require
    it (inside a character class), because that is the convention the hand-written twin
    already follows -- e.g. `[A-Za-z0-9+\\/]` against Python's bare `[A-Za-z0-9+/]`. The
    guard's `_js_literal_to_regex_text` unescapes it back, so semantics survive either
    way, but the twin's bytes do not.
    """
    return "/" + pattern.replace("/", "\\/") + "/" + js_flags(flags)


def js_block(row):
    """The JS declaration for one registry row, as a list of lines."""
    prefix = "export " if row.js_export else ""
    name = row.js_name
    if row.kind in ("str_list", "compile_list"):
        out = [f"{prefix}const {name} = ["]
        out += [f"  {js_literal(expand(p), row.flags)}," for p in row.patterns]
        out.append("];")
        return out
    if row.kind == "compile_single":
        # The two-line shape is load-bearing: the guard finds these families with a
        # strict `^const (\w+) =$` / `^\s*(/.*/[a-z]*);$` line pair, and a one-line
        # emission makes its lookup return None.
        return [
            f"{prefix}const {name} =",
            f"  {js_literal(expand(row.patterns[0]), row.flags)};",
        ]
    if row.kind == "word_split":
        return [
            f"{prefix}const {name} = {js_literal(expand(row.patterns[0]), row.flags)};"
        ]
    if row.kind == "content_sets":
        out = [f"{prefix}const {name} = ["]
        for content_set in CONTENT_SET_ORDER:
            out.append(f"  ['{content_set.phrase}', {content_set.family}],")
        out.append("];")
        return out
    raise SystemExit(f"unknown registry kind {row.kind!r} for {name}")


# --- marker fences -----------------------------------------------------------


def marker_lines(symbol, comment):
    return (
        f"{comment} {_MARKER_TAG}:{symbol} {_MARKER_HINT}",
        f"{comment} /{_MARKER_TAG}:{symbol}",
    )


def find_marker_pairs(lines, comment, rel_path):
    """{symbol: (open_index, close_index)} for every marker pair in `lines`.

    Hard-fails PER SYMBOL rather than on a whole-file open-marker count: this file holds
    a dozen independent pairs by design, so "more than one open marker" is the normal
    case, not the malformed one. What IS malformed: a symbol whose open and close
    markers do not appear exactly once each, in that order, without another pair opening
    in between.
    """
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
                f"{rel_path}: duplicate {'close' if match.group('close') else 'open'} "
                f"marker for {symbol} (lines {bucket[symbol] + 1} and {index + 1}) -- "
                "expected exactly one matched pair per symbol; fix by hand"
            )
        bucket[symbol] = index
    unmatched = sorted(set(opens) ^ set(closes))
    if unmatched:
        raise SystemExit(
            f"{rel_path}: unmatched generated-block marker(s) for "
            f"{', '.join(unmatched)} -- an orphaned marker would make --check call the "
            "file current while real debris sits in it; fix by hand"
        )
    pairs = {}
    for symbol, open_index in opens.items():
        close_index = closes[symbol]
        if close_index <= open_index:
            raise SystemExit(
                f"{rel_path}: close marker for {symbol} precedes its open marker "
                f"(lines {close_index + 1} and {open_index + 1}); fix by hand"
            )
        pairs[symbol] = (open_index, close_index)
    for symbol, (open_index, close_index) in pairs.items():
        for other, (other_open, _) in pairs.items():
            if other != symbol and open_index < other_open < close_index:
                raise SystemExit(
                    f"{rel_path}: {other}'s block is nested inside {symbol}'s "
                    f"(lines {open_index + 1}-{close_index + 1}); fix by hand"
                )
    return pairs


def fill_fences(text, rel_path, comment, symbol_of, body_of):
    """Rewrite every fenced block in `text` from the registry.

    `symbol_of(row)` names the symbol this file fences a row under; `body_of(row)`
    renders it. Both the "registry row with no fence" and the "fence with no registry
    row" directions fail loudly -- the first would silently ship an unmaintained
    hand-written declaration, the second stale debris.
    """
    lines = text.split("\n")
    pairs = find_marker_pairs(lines, comment, rel_path)
    expected = {symbol_of(row): row for row in blocks()}
    missing = sorted(set(expected) - set(pairs))
    if missing:
        raise SystemExit(
            f"{rel_path}: no marker pair for registry symbol(s) {', '.join(missing)} -- "
            "place an empty pair where the declaration belongs, then rerun"
        )
    orphans = sorted(set(pairs) - set(expected))
    if orphans:
        raise SystemExit(
            f"{rel_path}: marker pair(s) {', '.join(orphans)} match no registry row -- "
            "remove the fence or add the row"
        )
    for symbol in sorted(pairs, key=lambda s: pairs[s][0], reverse=True):
        open_index, close_index = pairs[symbol]
        lines[open_index + 1 : close_index] = body_of(expected[symbol])
    return "\n".join(lines)


# --- anchored inline rewrites ------------------------------------------------

_PY_INLINE_LITERAL_RE = re.compile(r'r"[^"]*"')
# A JS regex literal: escaped pairs, bracketed classes (which may legally hold a bare
# `/`), or any other non-delimiter character, then the closing `/` and flag letters.
_JS_INLINE_LITERAL_RE = re.compile(r"/(?:\\.|\[(?:\\.|[^\]\\])*\]|[^/\\\[\n])+/[a-z]*")


def rewrite_inline_sites(text, rel_path, literal_re, render):
    """Rewrite each INLINE_SITES pattern in place, located by its anchor skeleton.

    The anchor must hit exactly once in the file -- 0 hits means the call site moved or
    was deleted, >1 means the skeleton stopped being unique and the rewrite would be a
    coin flip. Both are hard failures, never a silent no-op. The replacement is scoped
    to the regex literal on the anchor's line, not the line, because the JS site sits
    mid-line inside an `if`.
    """
    for site in INLINE_SITES:
        hits = text.count(site.anchor)
        if hits != 1:
            raise SystemExit(
                f"{rel_path}: inline-site anchor for {site.name} matched {hits} times "
                "(expected exactly 1) -- the call site moved or the skeleton is no "
                "longer unique; fix the registry anchor by hand"
            )
        lines = text.split("\n")
        line_index = next(i for i, line in enumerate(lines) if site.anchor in line)
        line = lines[line_index]
        candidates = [m for m in literal_re.finditer(line) if site.anchor in m.group(0)]
        if len(candidates) != 1:
            raise SystemExit(
                f"{rel_path}:{line_index + 1}: found {len(candidates)} pattern literals "
                f"carrying {site.name}'s anchor (expected exactly 1); fix by hand"
            )
        match = candidates[0]
        lines[line_index] = line[: match.start()] + render(site) + line[match.end() :]
        text = "\n".join(lines)
    return text


def py_inline_literal(site):
    return py_literal(expand(site.pattern))


def js_inline_literal(site):
    return js_literal(expand(site.pattern), site.js_flags)


# --- targets -----------------------------------------------------------------


def expected_python(text):
    text = fill_fences(
        text,
        PY_REL,
        "#",
        lambda row: row.python_name,
        py_block,
    )
    return rewrite_inline_sites(text, PY_REL, _PY_INLINE_LITERAL_RE, py_inline_literal)


def expected_js(text):
    text = fill_fences(
        text,
        JS_REL,
        "//",
        lambda row: row.js_name,
        js_block,
    )
    return rewrite_inline_sites(text, JS_REL, _JS_INLINE_LITERAL_RE, js_inline_literal)


TARGETS = (
    (PY_REL, expected_python),
    (JS_REL, expected_js),
)


def apply_targets(repo_root=REPO_ROOT, check_only=False):
    """Return the relative paths whose generated content is stale, writing unless asked
    not to."""
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
        help="report stale generated pattern blocks without writing",
    )
    args = parser.parse_args(argv)

    stale = apply_targets(args.repo_root, check_only=args.check)
    if not stale:
        print("filter pattern declarations are current")
        return 0
    if args.check:
        sys.stderr.write(
            f"stale generated filter patterns: {', '.join(stale)}\n"
            "run: python3 scripts/generate_filter_patterns.py\n"
        )
        return 1
    print(f"regenerated: {', '.join(stale)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

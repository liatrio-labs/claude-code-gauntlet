"""Declarative registry of the first-party regex patterns shared by the filter twins.

WHY THIS EXISTS -- issue #241.

`scripts/filter_findings.py` and `workflows/src/filterFindings.js` each respell the
same first-party injection/routing patterns by hand, including 135 hand-typed copies
of an 87-character union whitespace class per twin. `tests/test_filter_twins_unicode_guard.py`
proves the two spellings agree, but proving agreement is not the same as having one
source: adding a pattern still costs two hand edits, and the class itself costs 135.

This module is that one source. It is DATA ONLY -- no logic, no imports beyond
`typing` -- so both the generator (`scripts/generate_filter_patterns.py`, which emits
the fenced declarations into both twins) and the tests can read it without side
effects.

`{WS}` inside a pattern expands to `UNION_WS_INNER`, the INNER spelling of the union
whitespace class (no brackets), so a pattern spells `[{WS}]` for the plain class and
`[-{WS}]` where a literal `-` leads the class. Nothing else in a pattern is templated:
`{0,40}`, `{40,}` and friends are regex quantifiers and are emitted verbatim (the
expansion is a plain string replace of the `{WS}` token, never `str.format`).
"""

from typing import NamedTuple

# The INNER spelling of the union whitespace class -- no surrounding brackets, so a
# row can spell either `[{WS}]` or `[-{WS}]`. Byte-identical in both twins; the
# guard's count-parity assertion counts every expansion of it.
UNION_WS_INNER = r"\t\n\x0b\x0c\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff"


class PatternFamily(NamedTuple):
    r"""One generated declaration, in both twins.

    `kind` selects the emission layout:

    - ``str_list``      -- Python list of raw pattern strings compiled at the call
                           site; JS array of regex literals.
    - ``compile_list``  -- Python list of `re.compile(...)` calls; JS array of literals.
    - ``compile_single``-- one `re.compile(...)`; JS two-line `const NAME =\n  /re/f;`.
    - ``word_split``    -- the word splitter: Python 3-line no-trailing-comma
                           `re.compile(...)`; JS one-line `export const NAME = /re/;`.
    - ``content_sets``  -- the (phrase, family) table; patterns come from
                           `CONTENT_SET_ORDER`, not from `patterns`.

    `flags` names `re` flags; the JS emitter maps them to regex-literal flag letters
    (ASCII has no JS counterpart -- a JS regex without `/u` already has the ASCII
    `\b`/`\w`/`\d` semantics `re.ASCII` buys in Python).
    """

    python_name: str
    js_name: str
    kind: str
    flags: tuple[str, ...]
    js_export: bool
    patterns: tuple[str, ...]


class ContentSet(NamedTuple):
    """One row of `_CONTENT_PATTERN_SETS` / `SUGGESTION_SETS`: a human-readable
    elimination phrase and the `PatternFamily.js_name` of the family it scans."""

    phrase: str
    family: str


class InlineSite(NamedTuple):
    """A first-party pattern that is NOT a named declaration -- it sits inline at its
    call site, so the generator rewrites it in place instead of filling a fence.

    `anchor` is a non-whitespace-class skeleton that occurs exactly ONCE in each twin;
    the generator hard-fails on 0 or >1 hits rather than guessing. The two twins carry
    different flags for the same pattern by design: the Python site passes `re.ASCII`
    while the JS site lower-cases its input instead of carrying `/i`.
    """

    name: str
    anchor: str
    pattern: str
    py_flags: tuple[str, ...]
    js_flags: tuple[str, ...]


# The eight injection families plus the three dimension-routing families, in
# source-declaration order. `_CONTENT_PATTERN_SETS` references seven of the
# eight injection families by `js_name`; see CONTENT_SET_ORDER below.
PATTERN_FAMILIES = (
    PatternFamily(
        python_name="_INJECTION_TITLE_PATTERNS",
        js_name="INJECTION_TITLE_PATTERNS",
        kind="str_list",
        flags=("IGNORECASE", "ASCII"),
        js_export=False,
        patterns=(
            r"\bExample[{WS}]+finding\b",
            r"\bSample[{WS}]+finding\b",
            r"\btest[{WS}]+finding\b",
            r"\bdemo[{WS}]+finding\b",
            r"\bPlaceholder[{WS}]+finding\b",
        ),
    ),
    PatternFamily(
        python_name="_INJECTION_BODY_PATTERNS",
        js_name="INJECTION_BODY_PATTERNS",
        kind="str_list",
        flags=("IGNORECASE", "ASCII"),
        js_export=False,
        patterns=(
            r"<finding(?:[{WS}][^>]*)?>",
            r"<example(?:[{WS}][^>]*)?>",
            r"\[[{WS}]*INSERT[{WS}]*\]",
            r"\[[{WS}]*INSERT\b[^\]]*\b(?:FINDING|TITLE|TEXT|PLACEHOLDER|HERE)\b[^\]]*\]",
            r"lorem[{WS}]+ipsum",
        ),
    ),
    PatternFamily(
        python_name="_INJECTION_SHELL_PATTERNS",
        js_name="INJECTION_SHELL_PATTERNS",
        kind="str_list",
        flags=("IGNORECASE", "ASCII"),
        js_export=False,
        patterns=(
            r"\brm[{WS}]+-[rf]",
            r"\bcurl[{WS}]+https?://",
            r"\bwget[{WS}]+https?://",
            r"\bgit[{WS}]+push\b",
            r"\bgh[{WS}]+api\b",
        ),
    ),
    PatternFamily(
        python_name="_INJECTION_URL_PATTERNS",
        js_name="INJECTION_URL_PATTERNS",
        kind="str_list",
        flags=("IGNORECASE", "ASCII"),
        js_export=False,
        patterns=(
            r"\bvisit[{WS}]+[a-z][a-z0-9+.\-]{1,15}://",
            r"\bdownload[{WS}]+from[{WS}]+[a-z][a-z0-9+.\-]{1,15}://",
        ),
    ),
    PatternFamily(
        python_name="_INJECTION_ENCODED_PATTERNS",
        js_name="INJECTION_ENCODED_PATTERNS",
        kind="str_list",
        flags=("IGNORECASE", "ASCII"),
        js_export=False,
        patterns=(
            r"\b(?:decode|base64|atob|b64decode)\b[^\x00]{0,40}[A-Za-z0-9+/]{40,}={0,2}\b",
            r"\b[A-Za-z0-9+/]{40,}={0,2}\b[^\x00]{0,40}(?:\|[{WS}]*(?:sh|bash|zsh)\b|base64[{WS}]+-d\b|(?:then|and)[{WS}]+(?:run|execute|eval)\b)",
            r"\b(?:decode|unhex|xxd|fromhex|unhexlify)\b[^\x00]{0,40}(?<!\w)(?:0x)?[0-9a-fA-F]{32,}(?!\w)",
            r"(?<!\w)(?:0x)?[0-9a-fA-F]{32,}(?!\w)[^\x00]{0,40}(?:\|[{WS}]*(?:xxd|sh|bash)\b|(?:then|and)[{WS}]+(?:run|execute|eval)\b|-r[{WS}]+-p\b)",
        ),
    ),
    PatternFamily(
        python_name="_INJECTION_BYPASS_PATTERNS",
        js_name="INJECTION_BYPASS_PATTERNS",
        kind="str_list",
        flags=("IGNORECASE", "ASCII"),
        js_export=False,
        patterns=(
            r"\bskip[{WS}]+review\b",
            r"\bauto[-{WS}]?approve[{WS}]+(?:this|these|the|it|my|your)[{WS}]+(?:pr|pull[{WS}]+request|mr|merge[{WS}]+request|changes?|commit)\b",
            r"\bauto[-{WS}]?approve[{WS}]+and[{WS}]+(?:merge|skip|bypass|push|deploy|proceed|continue)\b",
            r"\bbypass[{WS}]+(?:security[{WS}]+)?controls?\b",
            r"\bbypass[{WS}]+(?:the[{WS}]+)?(?:auth|authentication|authorization)\b",
            r"\bdisable[{WS}]+(?:auth|authentication|authorization)\b",
            r"\bmark[{WS}]+(?:this[{WS}]+)?(?:finding[{WS}]+)?as[{WS}]+safe\b",
            r"\bapprove[{WS}]+(?:this|the)[{WS}]+(?:PR|pull[{WS}]+request|change)\b",
        ),
    ),
    PatternFamily(
        python_name="_INJECTION_INSTRUCTIONAL_PATTERNS",
        js_name="INJECTION_INSTRUCTIONAL_PATTERNS",
        kind="str_list",
        flags=("IGNORECASE", "ASCII"),
        js_export=False,
        patterns=(
            r"\byou[{WS}]+should[{WS}]+run\b",
            r"\bexecute[{WS}]+the[{WS}]+following\b",
            r"\brun[{WS}]+this[{WS}]+command\b",
            r"\bplease[{WS}]+run\b",
            r"\bpaste[{WS}]+(?:this|the[{WS}]+following)[{WS}]+into[{WS}]+(?:your[{WS}]+)?terminal",
            r"\bcopy[{WS}]+and[{WS}]+paste[{WS}]+the[{WS}]+following\b",
        ),
    ),
    PatternFamily(
        python_name="_INJECTION_VULN_INTRO_PATTERNS",
        js_name="INJECTION_VULN_INTRO_PATTERNS",
        kind="str_list",
        flags=("IGNORECASE", "ASCII"),
        js_export=False,
        patterns=(
            r"\badd[{WS}]+eval[{WS}]*\(",
            r"\buse[{WS}]+eval[{WS}]*\(",
            r"\bdisable[{WS}]+(?:CORS|CSP|content[-{WS}]security[-{WS}]policy)\b",
            r"\bdisable[{WS}]+(?:CSRF|csrf)[{WS}]+(?:protection|check|token)\b",
            r"\ballow[{WS}]+all[{WS}]+origins\b",
            r"\bset[{WS}]+secure[{WS}]+to[{WS}]+false\b",
            r"\bdisable[{WS}]+(?:TLS|SSL|HTTPS)[{WS}]+(?:verification|validation)\b",
            r"\bskip[{WS}]+(?:certificate|cert)[{WS}]+(?:verification|validation)\b",
            r"\bdisable[{WS}]+security[{WS}]+(?:check|feature|control)\b",
        ),
    ),
    PatternFamily(
        python_name="_FUNCTIONAL_VIOLATION_KEYWORDS",
        js_name="FUNCTIONAL_VIOLATION_KEYWORDS",
        kind="compile_single",
        flags=("IGNORECASE", "ASCII"),
        js_export=False,
        patterns=(
            r"\bcrash\b|\bdata[{WS}]+loss\b|\bsilent(?:ly)?\b|\bincorrect\b|\bwrong\b|\bfail(?:s|ure)?\b|\bruntime[{WS}]+error\b|\bexception\b|\bpanic\b|\bundefined[{WS}]+behavio(?:u)?r\b",
        ),
    ),
    PatternFamily(
        python_name="_TYPE_SAFETY_BUG_KEYWORDS",
        js_name="TYPE_SAFETY_BUG_KEYWORDS",
        kind="compile_single",
        flags=("IGNORECASE", "ASCII"),
        js_export=False,
        patterns=(
            r"\bruntime\b|\bcastexception\b|\btype[{WS}]+error\b|\bclasscastexception\b|\bnull[{WS}]+pointer\b|\bnullpointer\b|\btype[{WS}]+mismatch\b",
        ),
    ),
    PatternFamily(
        python_name="_TEST_CORRECTNESS_PATTERNS",
        js_name="TEST_CORRECTNESS_PATTERNS",
        kind="compile_list",
        flags=("IGNORECASE", "ASCII"),
        js_export=False,
        patterns=(
            r"\brace[{WS}]+condition\b",
            r"\balways[{WS}]+pass(?:es)?\b",
            r"\balways[-{WS}]pass(?:es)?\b",
            r"\bnever[{WS}]+fail(?:s)?\b",
            r"\bvacuous(?:ly)?\b",
            r"\btautolog(?:y|ical)\b",
            r"\bassert(?:ion)?[{WS}]+(?:is[{WS}]+)?never[{WS}]+reached\b",
            r"\bdeadlock\b",
            r"\bdata[{WS}]+race\b",
            r"\bthread[{WS}]+(?:safety|unsafe|race)\b",
            r"\btest[{WS}]+(?:never[{WS}]+)?(?:actually[{WS}]+)?(?:verif|test|check)(?:s|ies)?[{WS}]+nothing\b",
            r"\bfalse[{WS}]+positive[{WS}]+(?:test|assertion)\b",
            r"\bincorrect(?:ly)?[{WS}]+(?:assert|verify|test)\b",
            r"\bwrong[{WS}]+(?:value|result|output)\b",
            r"\blocal[{WS}]+variable[{WS}]+(?:is[{WS}]+)?never[{WS}]+(?:used|read)\b",
            r"\bassert(?:s|ion)?[{WS}]+(?:on[{WS}]+)?(?:a[{WS}]+)?(?:local|copy|snapshot)\b",
            r"\bcompares?[{WS}]+(?:wrong|incorrect|different)[{WS}]+object\b",
            r"\btest[{WS}]+(?:does[{WS}]+not|doesn'?t)[{WS}]+(?:wait|join|block)\b",
            r"\breader[{WS}]+thread[{WS}]+not[{WS}]+waited\b",
            r"\bflaky[{WS}]+test\b",
            r"\bassertion[{WS}]+always[{WS}]+(?:true|passes?|succeed)\b",
            r"\bassert(?:s|ion)?[{WS}]+(?:is[{WS}]+)?always[{WS}]+(?:true|pass(?:es?)?|succeed)\b",
            r"\btest[{WS}]+(?:is[{WS}]+)?always[{WS}]+(?:true|pass(?:es?)?|succeed)\b",
            r"\blogic[{WS}]+error\b",
            r"\bincorrect[{WS}]+(?:logic|behavior|behaviour|result)\b",
        ),
    ),
    # --- Config-parser family (issue #243) --------------------------------
    # parse_review_md / load_exclusions read REVIEW.md and exclusions-md file
    # FORMAT. Historically these lived UNGENERATED and NAMED-EXEMPT from the
    # union/re.ASCII discipline, which let the two twins drift: Python `\s`,
    # `splitlines()` and unicode `\d`/IGNORECASE folding all diverged from the
    # JS twin's `[ \t]`/`split`/ASCII `/i`. #243 converges them and folds the
    # declarations into the registry so they are generated, not hand-typed.
    #
    # The block-marker list is a `str_list` (compiled at the call site with
    # re.IGNORECASE | re.ASCII) so it stays discoverable by the guard's
    # visit_Assign walk; the five directive regexes and the two exclusion
    # regexes are module-level `compile_single` constants. `.` under
    # DOTALL / `[\s\S]` becomes `[^\x00]` (cross-twin symmetric, NOT
    # behavior-preserving vs a NUL in the block body -- disclosed in the PR).
    PatternFamily(
        python_name="_REVIEW_BLOCK_PATTERNS",
        js_name="REVIEW_BLOCK_PATTERNS",
        kind="str_list",
        flags=("IGNORECASE", "ASCII"),
        js_export=False,
        patterns=(
            r"```(?:yaml|)[{WS}]*#?[{WS}]*code-gauntlet(?:[^\n]*)?\n([^\x00]*?)```",
            r"<!--[{WS}]*code-gauntlet-config[{WS}]*\n([^\x00]*?)-->",
            r"```(?:yaml|)[{WS}]*#?[{WS}]*deep-review(?:[^\n]*)?\n([^\x00]*?)```",
            r"<!--[{WS}]*deep-review-config[{WS}]*\n([^\x00]*?)-->",
        ),
    ),
    PatternFamily(
        python_name="_REVIEW_CONFIDENCE_RE",
        js_name="REVIEW_CONFIDENCE_RE",
        kind="compile_single",
        flags=("IGNORECASE", "ASCII"),
        js_export=False,
        patterns=(r"(?:^|\n)[ \t]*confidence_threshold[{WS}]*[:=][{WS}]*([0-9]{1,3})",),
    ),
    PatternFamily(
        python_name="_REVIEW_SECURITY_RE",
        js_name="REVIEW_SECURITY_RE",
        kind="compile_single",
        flags=("IGNORECASE", "ASCII"),
        js_export=False,
        patterns=(
            r"(?:^|\n)[ \t]*security_min_confidence[{WS}]*[:=][{WS}]*([0-9]{1,3})",
        ),
    ),
    PatternFamily(
        python_name="_REVIEW_SEVERITY_RE",
        js_name="REVIEW_SEVERITY_RE",
        kind="compile_single",
        flags=("IGNORECASE", "ASCII"),
        js_export=False,
        patterns=(
            r"(?:^|\n)[ \t]*severity_threshold[{WS}]*[:=][{WS}]*(critical|high|medium|low)",
        ),
    ),
    PatternFamily(
        python_name="_REVIEW_IGNORE_RE",
        js_name="REVIEW_IGNORE_RE",
        kind="compile_single",
        flags=("IGNORECASE", "ASCII"),
        js_export=False,
        patterns=(r"(?:^|\n)[ \t]*ignore[{WS}]*:[{WS}]*\n((?:[ \t]*-[^\n]*\n?)+)",),
    ),
    PatternFamily(
        python_name="_REVIEW_IGNORE_ITEM_RE",
        js_name="REVIEW_IGNORE_ITEM_RE",
        kind="compile_single",
        flags=("ASCII",),
        js_export=False,
        patterns=(r"^[{WS}]*-[{WS}]*",),
    ),
    PatternFamily(
        python_name="_REVIEW_EXCL_BLOCK_RE",
        js_name="REVIEW_EXCL_BLOCK_RE",
        kind="compile_single",
        flags=("ASCII",),
        js_export=False,
        patterns=(r"```[^\n]*\n([^\x00]*?)```",),
    ),
    PatternFamily(
        python_name="_REVIEW_EXCL_BULLET_RE",
        js_name="REVIEW_EXCL_BULLET_RE",
        kind="compile_single",
        flags=("ASCII",),
        js_export=False,
        patterns=(r"^[{WS}]*[-*][{WS}]+([^\n]+)$",),
    ),
)

# --- Casefold-reachable homoglyph map (issue #242) ---------------------------
# The four codepoints that fullmatch a plain ASCII letter under Python
# `re.IGNORECASE` -- an independently re-measured-complete set. They are NON-word
# characters under ASCII semantics, so a `\b`/`(?<!\w)`-anchored injection
# pattern currently treats e.g. `[U+017F]kip` as a word split and misses it; the filter
# folds them to ASCII in a UNION scan (raw first, folded only if a mapped
# codepoint is present) so the hardened heuristics see through the disguise
# without losing the boundaries the raw text already satisfies.
#
# DATA ONLY. The two fold helpers are HAND-WRITTEN per twin (`str.translate` in
# Python, a `String.prototype.replace` over a `/[...]/g` literal in JS) and each
# hard-codes the same pairs, pinned to this tuple by a test in both suites --
# NFKC/normalize at runtime is a twin hazard (CPython UCD and Node ICU ship
# different Unicode versions), so the map cannot be derived, only hand-pinned.
# NFKC lookalikes ([U+0455]kip, [U+FF53]kip, ...) and format-char/zero-width boundary breakers
# are a DIFFERENT, strictly-cheaper evasion class left to a separate mechanism.
ASCII_CASEFOLD_REACHABLE = (
    (0x017F, "s"),  # LATIN SMALL LETTER LONG S            [U+017F] -> s
    (0x0131, "i"),  # LATIN SMALL LETTER DOTLESS I         [U+0131] -> i
    (0x0130, "i"),  # LATIN CAPITAL LETTER I WITH DOT ABOVE [U+0130] -> i
    (0x212A, "k"),  # KELVIN SIGN                          K -> k
)


# The word splitter -- the one place the union class is captured as a reusable
# compiled object rather than respelled into a pattern.
WORD_SPLIT = PatternFamily(
    python_name="_WORD_SPLIT_RE",
    js_name="WORD_SPLIT_RE",
    kind="word_split",
    flags=(),
    js_export=True,
    patterns=(r"[{WS}]+",),
)

# The (phrase, family) table behind `_CONTENT_PATTERN_SETS` / `SUGGESTION_SETS`,
# in scan order.
CONTENT_SETS = PatternFamily(
    python_name="_CONTENT_PATTERN_SETS",
    js_name="SUGGESTION_SETS",
    kind="content_sets",
    flags=(),
    js_export=True,
    patterns=(),
)

CONTENT_SET_ORDER = (
    ContentSet(
        phrase="contains shell command pattern", family="INJECTION_SHELL_PATTERNS"
    ),
    ContentSet(phrase="contains visit-URL pattern", family="INJECTION_URL_PATTERNS"),
    ContentSet(
        phrase="contains encoded payload pattern", family="INJECTION_ENCODED_PATTERNS"
    ),
    ContentSet(
        phrase="contains bypass/auto-approve instruction",
        family="INJECTION_BYPASS_PATTERNS",
    ),
    ContentSet(
        phrase="uses instructional tone", family="INJECTION_INSTRUCTIONAL_PATTERNS"
    ),
    ContentSet(
        phrase="recommends introducing vulnerability",
        family="INJECTION_VULN_INTRO_PATTERNS",
    ),
    ContentSet(phrase="matches injection marker", family="INJECTION_BODY_PATTERNS"),
)

# Inline (not-a-named-declaration) first-party patterns, rewritten in place.
INLINE_SITES = (
    InlineSite(
        name="suppression_intentional",
        anchor=r"\bintentional\b",
        pattern=r"\bintentional\b|\bby[{WS}]+design\b|\bexpected[{WS}]+behavior\b|\bdeliberate\b",
        py_flags=("ASCII",),
        js_flags=(),
    ),
    InlineSite(
        name="suppression_generated",
        anchor=r"\bgenerated\b|\bscaffolding\b",
        pattern=r"\bgenerated\b|\bscaffolding\b|\bauto[-{WS}]?generated\b|\bboilerplate\b",
        py_flags=("ASCII",),
        js_flags=(),
    ),
)

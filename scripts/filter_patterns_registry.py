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

# --- Confusable fold + invisible strip tables (issue #272) -------------------
# CONFUSABLE_FOLD_PACKED and INVISIBLE_STRIP_PACKED are the SINGLE source of truth
# for the injection filter's homoglyph fold + zero-width/boundary strip; both twins
# are GENERATED from them by scripts/generate_confusable_tables.py and pinned back to
# this module by a parity test in each suite. DATA ONLY -- the decode is a per-twin
# port, and NFKC/normalize at runtime is a twin hazard (CPython UCD vs Node ICU ship
# different Unicode versions), so the tables are hand-pinned, never re-derived at load
# or in a test (the CI Python matrix 3.10/3.11/3.12 ships different UCD versions).
#
# Packed form: each fold entry is an escaped non-ASCII SOURCE codepoint (\uXXXX below
# U+10000, \U00xxxxxx above) followed by its literal single ASCII-letter TARGET; each
# strip entry is a bare escaped codepoint. 919 fold sources and 240 strip codepoints
# are ASTRAL, so both twins decode by CODE-POINT iteration, never a regex char class.
#
# CONFUSABLE_FOLD (1468 entries): single non-ASCII codepoint -> single ASCII letter,
# precedence casefold(4) > NFKC(898, CPython unicodedata 15.0.0, single-cp -> ASCII-
# letter, cp>127) > confusables(585, Unicode confusables.txt v17.0.0 single-source ->
# ASCII-letter complement not already NFKC-covered) already baked in: the 16 NFKC/
# confusables conflicts resolve to the Latin/casefold target (e.g. U+017F LONG S -> s,
# NOT the confusables f), and the 4 casefold-reachable rows (U+017F->s, U+0131->i,
# U+0130->i, U+212A->k) win. INVISIBLE_STRIP (599 codepoints): zero-width spaces,
# joiners, bidi controls, invisible operators, variation selectors (incl. astral
# U+E0100-U+E01EF) and combining marks -- stripped from the scanned copy. Fold-keys
# and strip-keys are DISJOINT (measured overlap 0).
CONFUSABLE_FOLD_PACKED = (
    "\u00aaa\u00bao\u00d7x\u00fep\u0130i\u0131i\u017fs\u0184b\u018dg\u0192f\u0196l\u01a6R"
    "\u01bds\u01bfp\u01c0l\u0251a\u0261g\u0263y\u0269i\u026ai\u026fw\u028bu\u028fy\u02b0h"
    "\u02b2j\u02b3r\u02b7w\u02b8y\u02dbi\u02e1l\u02e2s\u02e3x\u037ai\u037fJ\u0391A\u0392B"
    "\u0395E\u0396Z\u0397H\u0399l\u039aK\u039cM\u039dN\u039fO\u03a1P\u03a4T\u03a5Y\u03a7X"
    "\u03b1a\u03b3y\u03b9i\u03bdv\u03bfo\u03c1p\u03c3o\u03c5u\u03d2Y\u03dcF\u03edo\u03f1p"
    "\u03f2c\u03f3j\u03f8p\u03f9C\u03faM\u0405S\u0406l\u0408J\u0410A\u0412B\u0415E\u041aK"
    "\u041cM\u041dH\u041eO\u0420P\u0421C\u0422T\u0423Y\u0425X\u042cb\u0430a\u0433r\u0435e"
    "\u043eo\u0440p\u0441c\u0443y\u0445x\u0448w\u0455s\u0456i\u0458j\u0461w\u0474V\u0475v"
    "\u04aeY\u04afy\u04bbh\u04bde\u04c0l\u04cfl\u0501d\u050cG\u051bq\u051cW\u051dw\u054dU"
    "\u054fS\u0555O\u0561w\u0563q\u0566q\u0570h\u0578n\u057cn\u057du\u0581g\u0582i\u0584f"
    "\u0585o\u05c0l\u05d5l\u05d8v\u05dfl\u05e1o\u0627l\u0647o\u0661l\u0665o\u0667V\u06beo"
    "\u06c1o\u06d5o\u06f1l\u06f5o\u06f7V\u07c0O\u07cal\u0966o\u09e6o\u0a66o\u0ae6o\u0b20O"
    "\u0b66o\u0be6o\u0c02o\u0c66o\u0c82o\u0ce6O\u0d02o\u0d1fs\u0d20o\u0d66o\u0d82o\u0e50o"
    "\u0ed0o\u1004c\u101do\u1040o\u105ac\u10e7y\u10ffo\u1200U\u12d0O\u13a0D\u13a1R\u13a2T"
    "\u13a5i\u13a9Y\u13aaA\u13abJ\u13acE\u13b3W\u13b7M\u13bbH\u13bdY\u13c0G\u13c2h\u13c3Z"
    "\u13cfb\u13d2R\u13d4W\u13d5S\u13d9V\u13daS\u13deL\u13dfC\u13e2P\u13e6K\u13e7d\u13f3G"
    "\u13f4B\u142fV\u144cU\u146dP\u146fd\u1472b\u148dJ\u14aaL\u1541x\u157cH\u157dx\u1587R"
    "\u15afb\u15b4F\u15c5A\u15deD\u15eaD\u15f0M\u15f7B\u166dX\u166ex\u16b7X\u16c1l\u16d5K"
    "\u16d6M\u17e0o\u1d04c\u1d0fo\u1d11o\u1d1cu\u1d20v\u1d21w\u1d22z\u1d26r\u1d2cA\u1d2eB"
    "\u1d30D\u1d31E\u1d33G\u1d34H\u1d35I\u1d36J\u1d37K\u1d38L\u1d39M\u1d3aN\u1d3cO\u1d3eP"
    "\u1d3fR\u1d40T\u1d41U\u1d42W\u1d43a\u1d47b\u1d48d\u1d49e\u1d4dg\u1d4fk\u1d50m\u1d52o"
    "\u1d56p\u1d57t\u1d58u\u1d5bv\u1d62i\u1d63r\u1d64u\u1d65v\u1d83g\u1d8cy\u1d9cc\u1da0f"
    "\u1dbbz\u1e9df\u1effy\u1fbei\u2071i\u207fn\u2090a\u2091e\u2092o\u2093x\u2095h\u2096k"
    "\u2097l\u2098m\u2099n\u209ap\u209bs\u209ct\u2102C\u210ag\u210bH\u210cH\u210dH\u210eh"
    "\u2110I\u2111I\u2112L\u2113l\u2115N\u2119P\u211aQ\u211bR\u211cR\u211dR\u2124Z\u2128Z"
    "\u212ak\u212cB\u212dC\u212ee\u212fe\u2130E\u2131F\u2133M\u2134o\u2139i\u213dy\u2145D"
    "\u2146d\u2147e\u2148i\u2149j\u2160I\u2164V\u2169X\u216cL\u216dC\u216eD\u216fM\u2170i"
    "\u2174v\u2179x\u217cl\u217dc\u217ed\u217fm\u2223l\u2228v\u222aU\u22a4T\u22c1v\u22c3U"
    "\u22ffE\u2373i\u2374p\u237aa\u23fdl\u24b6A\u24b7B\u24b8C\u24b9D\u24baE\u24bbF\u24bcG"
    "\u24bdH\u24beI\u24bfJ\u24c0K\u24c1L\u24c2M\u24c3N\u24c4O\u24c5P\u24c6Q\u24c7R\u24c8S"
    "\u24c9T\u24caU\u24cbV\u24ccW\u24cdX\u24ceY\u24cfZ\u24d0a\u24d1b\u24d2c\u24d3d\u24d4e"
    "\u24d5f\u24d6g\u24d7h\u24d8i\u24d9j\u24dak\u24dbl\u24dcm\u24ddn\u24deo\u24dfp\u24e0q"
    "\u24e1r\u24e2s\u24e3t\u24e4u\u24e5v\u24e6w\u24e7x\u24e8y\u24e9z\u2573X\u27d9T\u292bx"
    "\u292cx\u2a2fx\u2c7cj\u2c7dV\u2c82B\u2c85r\u2c8eH\u2c92l\u2c93i\u2c94K\u2c98M\u2c9aN"
    "\u2c9eO\u2c9fo\u2ca2P\u2ca3p\u2ca4C\u2ca5c\u2ca6T\u2ca8Y\u2ca9y\u2cacX\u2cbdw\u2cceP"
    "\u2ccfp\u2cd0L\u2d38V\u2d39E\u2d4fl\u2d54O\u2d55Q\u2d5dX\u3007O\ua4d0B\ua4d1P\ua4d2d"
    "\ua4d3D\ua4d4T\ua4d6G\ua4d7K\ua4d9J\ua4daC\ua4dcZ\ua4ddF\ua4dfM\ua4e0N\ua4e1L\ua4e2S"
    "\ua4e3R\ua4e6V\ua4e7H\ua4eaW\ua4ebX\ua4ecY\ua4eeA\ua4f0E\ua4f2l\ua4f3O\ua4f4U\ua647i"
    "\ua6dfV\ua731s\ua798F\ua799f\ua79fu\ua7b2J\ua7b3X\ua7b4B\ua7f2C\ua7f3F\ua7f4Q\uab32e"
    "\uab35f\uab3do\uab47r\uab48r\uab4eu\uab52u\uab5ay\uab75i\uab81r\uab83w\uab93z\uaba9v"
    "\uabaas\uabafc\ufba6o\ufba7o\ufba8o\ufba9o\ufbaao\ufbabo\ufbaco\ufbado\ufe8dl\ufe8el"
    "\ufee9o\ufeeao\ufeebo\ufeeco\uff21A\uff22B\uff23C\uff24D\uff25E\uff26F\uff27G\uff28H"
    "\uff29I\uff2aJ\uff2bK\uff2cL\uff2dM\uff2eN\uff2fO\uff30P\uff31Q\uff32R\uff33S\uff34T"
    "\uff35U\uff36V\uff37W\uff38X\uff39Y\uff3aZ\uff41a\uff42b\uff43c\uff44d\uff45e\uff46f"
    "\uff47g\uff48h\uff49i\uff4aj\uff4bk\uff4cl\uff4dm\uff4en\uff4fo\uff50p\uff51q\uff52r"
    "\uff53s\uff54t\uff55u\uff56v\uff57w\uff58x\uff59y\uff5az\uffe8l\U00010282B\U00010286E\U00010287F"
    "\U0001028al\U00010290X\U00010292O\U00010295P\U00010296S\U00010297T\U000102a0A\U000102a1B\U000102a2C\U000102a5F\U000102abO\U000102b0M"
    "\U000102b1T\U000102b2Y\U000102b4X\U000102cfH\U000102f5Z\U00010301B\U00010302C\U00010309l\U00010311M\U00010315T\U00010317X\U00010320l"
    "\U00010322X\U00010404O\U00010415C\U0001041bL\U00010420S\U0001042co\U0001043dc\U00010448s\U000104b4R\U000104c2O\U000104ceU\U000104eao"
    "\U000104f6u\U00010513N\U00010516O\U00010518K\U0001051cC\U0001051dV\U00010525F\U00010526L\U00010527X\U000107a5q\U000114d0o\U00011706v"
    "\U0001170aw\U0001170ew\U0001170fw\U000118a0V\U000118a2F\U000118a3L\U000118a4Y\U000118a6E\U000118a9Z\U000118aeE\U000118b2L\U000118b5O"
    "\U000118b8U\U000118bcT\U000118c0v\U000118c1s\U000118c2F\U000118c3i\U000118c4z\U000118c8o\U000118d7o\U000118d8u\U000118dcy\U000118e0O"
    "\U000118e5Z\U000118e6W\U000118e9C\U000118ecX\U000118efW\U000118f2C\U00011ddal\U00011de0O\U00011de1l\U00016eaal\U00016eb6b\U00016f08V"
    "\U00016f0aT\U00016f16L\U00016f28l\U00016f35R\U00016f3aS\U00016f40A\U00016f42U\U00016f43Y\U0001ccdel\U0001ccf0O\U0001ccf1l\U0001d20dV"
    "\U0001d213F\U0001d216R\U0001d22aL\U0001d400A\U0001d401B\U0001d402C\U0001d403D\U0001d404E\U0001d405F\U0001d406G\U0001d407H\U0001d408I"
    "\U0001d409J\U0001d40aK\U0001d40bL\U0001d40cM\U0001d40dN\U0001d40eO\U0001d40fP\U0001d410Q\U0001d411R\U0001d412S\U0001d413T\U0001d414U"
    "\U0001d415V\U0001d416W\U0001d417X\U0001d418Y\U0001d419Z\U0001d41aa\U0001d41bb\U0001d41cc\U0001d41dd\U0001d41ee\U0001d41ff\U0001d420g"
    "\U0001d421h\U0001d422i\U0001d423j\U0001d424k\U0001d425l\U0001d426m\U0001d427n\U0001d428o\U0001d429p\U0001d42aq\U0001d42br\U0001d42cs"
    "\U0001d42dt\U0001d42eu\U0001d42fv\U0001d430w\U0001d431x\U0001d432y\U0001d433z\U0001d434A\U0001d435B\U0001d436C\U0001d437D\U0001d438E"
    "\U0001d439F\U0001d43aG\U0001d43bH\U0001d43cI\U0001d43dJ\U0001d43eK\U0001d43fL\U0001d440M\U0001d441N\U0001d442O\U0001d443P\U0001d444Q"
    "\U0001d445R\U0001d446S\U0001d447T\U0001d448U\U0001d449V\U0001d44aW\U0001d44bX\U0001d44cY\U0001d44dZ\U0001d44ea\U0001d44fb\U0001d450c"
    "\U0001d451d\U0001d452e\U0001d453f\U0001d454g\U0001d456i\U0001d457j\U0001d458k\U0001d459l\U0001d45am\U0001d45bn\U0001d45co\U0001d45dp"
    "\U0001d45eq\U0001d45fr\U0001d460s\U0001d461t\U0001d462u\U0001d463v\U0001d464w\U0001d465x\U0001d466y\U0001d467z\U0001d468A\U0001d469B"
    "\U0001d46aC\U0001d46bD\U0001d46cE\U0001d46dF\U0001d46eG\U0001d46fH\U0001d470I\U0001d471J\U0001d472K\U0001d473L\U0001d474M\U0001d475N"
    "\U0001d476O\U0001d477P\U0001d478Q\U0001d479R\U0001d47aS\U0001d47bT\U0001d47cU\U0001d47dV\U0001d47eW\U0001d47fX\U0001d480Y\U0001d481Z"
    "\U0001d482a\U0001d483b\U0001d484c\U0001d485d\U0001d486e\U0001d487f\U0001d488g\U0001d489h\U0001d48ai\U0001d48bj\U0001d48ck\U0001d48dl"
    "\U0001d48em\U0001d48fn\U0001d490o\U0001d491p\U0001d492q\U0001d493r\U0001d494s\U0001d495t\U0001d496u\U0001d497v\U0001d498w\U0001d499x"
    "\U0001d49ay\U0001d49bz\U0001d49cA\U0001d49eC\U0001d49fD\U0001d4a2G\U0001d4a5J\U0001d4a6K\U0001d4a9N\U0001d4aaO\U0001d4abP\U0001d4acQ"
    "\U0001d4aeS\U0001d4afT\U0001d4b0U\U0001d4b1V\U0001d4b2W\U0001d4b3X\U0001d4b4Y\U0001d4b5Z\U0001d4b6a\U0001d4b7b\U0001d4b8c\U0001d4b9d"
    "\U0001d4bbf\U0001d4bdh\U0001d4bei\U0001d4bfj\U0001d4c0k\U0001d4c1l\U0001d4c2m\U0001d4c3n\U0001d4c5p\U0001d4c6q\U0001d4c7r\U0001d4c8s"
    "\U0001d4c9t\U0001d4cau\U0001d4cbv\U0001d4ccw\U0001d4cdx\U0001d4cey\U0001d4cfz\U0001d4d0A\U0001d4d1B\U0001d4d2C\U0001d4d3D\U0001d4d4E"
    "\U0001d4d5F\U0001d4d6G\U0001d4d7H\U0001d4d8I\U0001d4d9J\U0001d4daK\U0001d4dbL\U0001d4dcM\U0001d4ddN\U0001d4deO\U0001d4dfP\U0001d4e0Q"
    "\U0001d4e1R\U0001d4e2S\U0001d4e3T\U0001d4e4U\U0001d4e5V\U0001d4e6W\U0001d4e7X\U0001d4e8Y\U0001d4e9Z\U0001d4eaa\U0001d4ebb\U0001d4ecc"
    "\U0001d4edd\U0001d4eee\U0001d4eff\U0001d4f0g\U0001d4f1h\U0001d4f2i\U0001d4f3j\U0001d4f4k\U0001d4f5l\U0001d4f6m\U0001d4f7n\U0001d4f8o"
    "\U0001d4f9p\U0001d4faq\U0001d4fbr\U0001d4fcs\U0001d4fdt\U0001d4feu\U0001d4ffv\U0001d500w\U0001d501x\U0001d502y\U0001d503z\U0001d504A"
    "\U0001d505B\U0001d507D\U0001d508E\U0001d509F\U0001d50aG\U0001d50dJ\U0001d50eK\U0001d50fL\U0001d510M\U0001d511N\U0001d512O\U0001d513P"
    "\U0001d514Q\U0001d516S\U0001d517T\U0001d518U\U0001d519V\U0001d51aW\U0001d51bX\U0001d51cY\U0001d51ea\U0001d51fb\U0001d520c\U0001d521d"
    "\U0001d522e\U0001d523f\U0001d524g\U0001d525h\U0001d526i\U0001d527j\U0001d528k\U0001d529l\U0001d52am\U0001d52bn\U0001d52co\U0001d52dp"
    "\U0001d52eq\U0001d52fr\U0001d530s\U0001d531t\U0001d532u\U0001d533v\U0001d534w\U0001d535x\U0001d536y\U0001d537z\U0001d538A\U0001d539B"
    "\U0001d53bD\U0001d53cE\U0001d53dF\U0001d53eG\U0001d540I\U0001d541J\U0001d542K\U0001d543L\U0001d544M\U0001d546O\U0001d54aS\U0001d54bT"
    "\U0001d54cU\U0001d54dV\U0001d54eW\U0001d54fX\U0001d550Y\U0001d552a\U0001d553b\U0001d554c\U0001d555d\U0001d556e\U0001d557f\U0001d558g"
    "\U0001d559h\U0001d55ai\U0001d55bj\U0001d55ck\U0001d55dl\U0001d55em\U0001d55fn\U0001d560o\U0001d561p\U0001d562q\U0001d563r\U0001d564s"
    "\U0001d565t\U0001d566u\U0001d567v\U0001d568w\U0001d569x\U0001d56ay\U0001d56bz\U0001d56cA\U0001d56dB\U0001d56eC\U0001d56fD\U0001d570E"
    "\U0001d571F\U0001d572G\U0001d573H\U0001d574I\U0001d575J\U0001d576K\U0001d577L\U0001d578M\U0001d579N\U0001d57aO\U0001d57bP\U0001d57cQ"
    "\U0001d57dR\U0001d57eS\U0001d57fT\U0001d580U\U0001d581V\U0001d582W\U0001d583X\U0001d584Y\U0001d585Z\U0001d586a\U0001d587b\U0001d588c"
    "\U0001d589d\U0001d58ae\U0001d58bf\U0001d58cg\U0001d58dh\U0001d58ei\U0001d58fj\U0001d590k\U0001d591l\U0001d592m\U0001d593n\U0001d594o"
    "\U0001d595p\U0001d596q\U0001d597r\U0001d598s\U0001d599t\U0001d59au\U0001d59bv\U0001d59cw\U0001d59dx\U0001d59ey\U0001d59fz\U0001d5a0A"
    "\U0001d5a1B\U0001d5a2C\U0001d5a3D\U0001d5a4E\U0001d5a5F\U0001d5a6G\U0001d5a7H\U0001d5a8I\U0001d5a9J\U0001d5aaK\U0001d5abL\U0001d5acM"
    "\U0001d5adN\U0001d5aeO\U0001d5afP\U0001d5b0Q\U0001d5b1R\U0001d5b2S\U0001d5b3T\U0001d5b4U\U0001d5b5V\U0001d5b6W\U0001d5b7X\U0001d5b8Y"
    "\U0001d5b9Z\U0001d5baa\U0001d5bbb\U0001d5bcc\U0001d5bdd\U0001d5bee\U0001d5bff\U0001d5c0g\U0001d5c1h\U0001d5c2i\U0001d5c3j\U0001d5c4k"
    "\U0001d5c5l\U0001d5c6m\U0001d5c7n\U0001d5c8o\U0001d5c9p\U0001d5caq\U0001d5cbr\U0001d5ccs\U0001d5cdt\U0001d5ceu\U0001d5cfv\U0001d5d0w"
    "\U0001d5d1x\U0001d5d2y\U0001d5d3z\U0001d5d4A\U0001d5d5B\U0001d5d6C\U0001d5d7D\U0001d5d8E\U0001d5d9F\U0001d5daG\U0001d5dbH\U0001d5dcI"
    "\U0001d5ddJ\U0001d5deK\U0001d5dfL\U0001d5e0M\U0001d5e1N\U0001d5e2O\U0001d5e3P\U0001d5e4Q\U0001d5e5R\U0001d5e6S\U0001d5e7T\U0001d5e8U"
    "\U0001d5e9V\U0001d5eaW\U0001d5ebX\U0001d5ecY\U0001d5edZ\U0001d5eea\U0001d5efb\U0001d5f0c\U0001d5f1d\U0001d5f2e\U0001d5f3f\U0001d5f4g"
    "\U0001d5f5h\U0001d5f6i\U0001d5f7j\U0001d5f8k\U0001d5f9l\U0001d5fam\U0001d5fbn\U0001d5fco\U0001d5fdp\U0001d5feq\U0001d5ffr\U0001d600s"
    "\U0001d601t\U0001d602u\U0001d603v\U0001d604w\U0001d605x\U0001d606y\U0001d607z\U0001d608A\U0001d609B\U0001d60aC\U0001d60bD\U0001d60cE"
    "\U0001d60dF\U0001d60eG\U0001d60fH\U0001d610I\U0001d611J\U0001d612K\U0001d613L\U0001d614M\U0001d615N\U0001d616O\U0001d617P\U0001d618Q"
    "\U0001d619R\U0001d61aS\U0001d61bT\U0001d61cU\U0001d61dV\U0001d61eW\U0001d61fX\U0001d620Y\U0001d621Z\U0001d622a\U0001d623b\U0001d624c"
    "\U0001d625d\U0001d626e\U0001d627f\U0001d628g\U0001d629h\U0001d62ai\U0001d62bj\U0001d62ck\U0001d62dl\U0001d62em\U0001d62fn\U0001d630o"
    "\U0001d631p\U0001d632q\U0001d633r\U0001d634s\U0001d635t\U0001d636u\U0001d637v\U0001d638w\U0001d639x\U0001d63ay\U0001d63bz\U0001d63cA"
    "\U0001d63dB\U0001d63eC\U0001d63fD\U0001d640E\U0001d641F\U0001d642G\U0001d643H\U0001d644I\U0001d645J\U0001d646K\U0001d647L\U0001d648M"
    "\U0001d649N\U0001d64aO\U0001d64bP\U0001d64cQ\U0001d64dR\U0001d64eS\U0001d64fT\U0001d650U\U0001d651V\U0001d652W\U0001d653X\U0001d654Y"
    "\U0001d655Z\U0001d656a\U0001d657b\U0001d658c\U0001d659d\U0001d65ae\U0001d65bf\U0001d65cg\U0001d65dh\U0001d65ei\U0001d65fj\U0001d660k"
    "\U0001d661l\U0001d662m\U0001d663n\U0001d664o\U0001d665p\U0001d666q\U0001d667r\U0001d668s\U0001d669t\U0001d66au\U0001d66bv\U0001d66cw"
    "\U0001d66dx\U0001d66ey\U0001d66fz\U0001d670A\U0001d671B\U0001d672C\U0001d673D\U0001d674E\U0001d675F\U0001d676G\U0001d677H\U0001d678I"
    "\U0001d679J\U0001d67aK\U0001d67bL\U0001d67cM\U0001d67dN\U0001d67eO\U0001d67fP\U0001d680Q\U0001d681R\U0001d682S\U0001d683T\U0001d684U"
    "\U0001d685V\U0001d686W\U0001d687X\U0001d688Y\U0001d689Z\U0001d68aa\U0001d68bb\U0001d68cc\U0001d68dd\U0001d68ee\U0001d68ff\U0001d690g"
    "\U0001d691h\U0001d692i\U0001d693j\U0001d694k\U0001d695l\U0001d696m\U0001d697n\U0001d698o\U0001d699p\U0001d69aq\U0001d69br\U0001d69cs"
    "\U0001d69dt\U0001d69eu\U0001d69fv\U0001d6a0w\U0001d6a1x\U0001d6a2y\U0001d6a3z\U0001d6a4i\U0001d6a8A\U0001d6a9B\U0001d6acE\U0001d6adZ"
    "\U0001d6aeH\U0001d6b0l\U0001d6b1K\U0001d6b3M\U0001d6b4N\U0001d6b6O\U0001d6b8P\U0001d6bbT\U0001d6bcY\U0001d6beX\U0001d6c2a\U0001d6c4y"
    "\U0001d6cai\U0001d6cev\U0001d6d0o\U0001d6d2p\U0001d6d4o\U0001d6d6u\U0001d6e0p\U0001d6e2A\U0001d6e3B\U0001d6e6E\U0001d6e7Z\U0001d6e8H"
    "\U0001d6eal\U0001d6ebK\U0001d6edM\U0001d6eeN\U0001d6f0O\U0001d6f2P\U0001d6f5T\U0001d6f6Y\U0001d6f8X\U0001d6fca\U0001d6fey\U0001d704i"
    "\U0001d708v\U0001d70ao\U0001d70cp\U0001d70eo\U0001d710u\U0001d71ap\U0001d71cA\U0001d71dB\U0001d720E\U0001d721Z\U0001d722H\U0001d724l"
    "\U0001d725K\U0001d727M\U0001d728N\U0001d72aO\U0001d72cP\U0001d72fT\U0001d730Y\U0001d732X\U0001d736a\U0001d738y\U0001d73ei\U0001d742v"
    "\U0001d744o\U0001d746p\U0001d748o\U0001d74au\U0001d754p\U0001d756A\U0001d757B\U0001d75aE\U0001d75bZ\U0001d75cH\U0001d75el\U0001d75fK"
    "\U0001d761M\U0001d762N\U0001d764O\U0001d766P\U0001d769T\U0001d76aY\U0001d76cX\U0001d770a\U0001d772y\U0001d778i\U0001d77cv\U0001d77eo"
    "\U0001d780p\U0001d782o\U0001d784u\U0001d78ep\U0001d790A\U0001d791B\U0001d794E\U0001d795Z\U0001d796H\U0001d798l\U0001d799K\U0001d79bM"
    "\U0001d79cN\U0001d79eO\U0001d7a0P\U0001d7a3T\U0001d7a4Y\U0001d7a6X\U0001d7aaa\U0001d7acy\U0001d7b2i\U0001d7b6v\U0001d7b8o\U0001d7bap"
    "\U0001d7bco\U0001d7beu\U0001d7c8p\U0001d7caF\U0001d7ceO\U0001d7cfl\U0001d7d8O\U0001d7d9l\U0001d7e2O\U0001d7e3l\U0001d7ecO\U0001d7edl"
    "\U0001d7f6O\U0001d7f7l\U0001e8c7l\U0001ee00l\U0001ee24o\U0001ee64o\U0001ee80l\U0001ee84o\U0001f12bC\U0001f12cR\U0001f130A\U0001f131B"
    "\U0001f132C\U0001f133D\U0001f134E\U0001f135F\U0001f136G\U0001f137H\U0001f138I\U0001f139J\U0001f13aK\U0001f13bL\U0001f13cM\U0001f13dN"
    "\U0001f13eO\U0001f13fP\U0001f140Q\U0001f141R\U0001f142S\U0001f143T\U0001f144U\U0001f145V\U0001f146W\U0001f147X\U0001f148Y\U0001f149Z"
    "\U0001f74cC\U0001f768T\U0001fbf0O\U0001fbf1l"
)

INVISIBLE_STRIP_PACKED = (
    "\u00ad\u0300\u0301\u0302\u0303\u0304\u0305\u0306\u0307\u0308\u0309\u030a\u030b\u030c\u030d\u030e"
    "\u030f\u0310\u0311\u0312\u0313\u0314\u0315\u0316\u0317\u0318\u0319\u031a\u031b\u031c\u031d\u031e"
    "\u031f\u0320\u0321\u0322\u0323\u0324\u0325\u0326\u0327\u0328\u0329\u032a\u032b\u032c\u032d\u032e"
    "\u032f\u0330\u0331\u0332\u0333\u0334\u0335\u0336\u0337\u0338\u0339\u033a\u033b\u033c\u033d\u033e"
    "\u033f\u0340\u0341\u0342\u0343\u0344\u0345\u0346\u0347\u0348\u0349\u034a\u034b\u034c\u034d\u034e"
    "\u034f\u0350\u0351\u0352\u0353\u0354\u0355\u0356\u0357\u0358\u0359\u035a\u035b\u035c\u035d\u035e"
    "\u035f\u0360\u0361\u0362\u0363\u0364\u0365\u0366\u0367\u0368\u0369\u036a\u036b\u036c\u036d\u036e"
    "\u036f\u061c\u180e\u1ab0\u1ab1\u1ab2\u1ab3\u1ab4\u1ab5\u1ab6\u1ab7\u1ab8\u1ab9\u1aba\u1abb\u1abc"
    "\u1abd\u1abe\u1abf\u1ac0\u1ac1\u1ac2\u1ac3\u1ac4\u1ac5\u1ac6\u1ac7\u1ac8\u1ac9\u1aca\u1acb\u1acc"
    "\u1acd\u1ace\u1acf\u1ad0\u1ad1\u1ad2\u1ad3\u1ad4\u1ad5\u1ad6\u1ad7\u1ad8\u1ad9\u1ada\u1adb\u1adc"
    "\u1add\u1ade\u1adf\u1ae0\u1ae1\u1ae2\u1ae3\u1ae4\u1ae5\u1ae6\u1ae7\u1ae8\u1ae9\u1aea\u1aeb\u1aec"
    "\u1aed\u1aee\u1aef\u1af0\u1af1\u1af2\u1af3\u1af4\u1af5\u1af6\u1af7\u1af8\u1af9\u1afa\u1afb\u1afc"
    "\u1afd\u1afe\u1aff\u1dc0\u1dc1\u1dc2\u1dc3\u1dc4\u1dc5\u1dc6\u1dc7\u1dc8\u1dc9\u1dca\u1dcb\u1dcc"
    "\u1dcd\u1dce\u1dcf\u1dd0\u1dd1\u1dd2\u1dd3\u1dd4\u1dd5\u1dd6\u1dd7\u1dd8\u1dd9\u1dda\u1ddb\u1ddc"
    "\u1ddd\u1dde\u1ddf\u1de0\u1de1\u1de2\u1de3\u1de4\u1de5\u1de6\u1de7\u1de8\u1de9\u1dea\u1deb\u1dec"
    "\u1ded\u1dee\u1def\u1df0\u1df1\u1df2\u1df3\u1df4\u1df5\u1df6\u1df7\u1df8\u1df9\u1dfa\u1dfb\u1dfc"
    "\u1dfd\u1dfe\u1dff\u200b\u200c\u200d\u200e\u200f\u202a\u202b\u202c\u202d\u202e\u2060\u2061\u2062"
    "\u2063\u2064\u2066\u2067\u2068\u2069\u20d0\u20d1\u20d2\u20d3\u20d4\u20d5\u20d6\u20d7\u20d8\u20d9"
    "\u20da\u20db\u20dc\u20dd\u20de\u20df\u20e0\u20e1\u20e2\u20e3\u20e4\u20e5\u20e6\u20e7\u20e8\u20e9"
    "\u20ea\u20eb\u20ec\u20ed\u20ee\u20ef\u20f0\u20f1\u20f2\u20f3\u20f4\u20f5\u20f6\u20f7\u20f8\u20f9"
    "\u20fa\u20fb\u20fc\u20fd\u20fe\u20ff\ufe00\ufe01\ufe02\ufe03\ufe04\ufe05\ufe06\ufe07\ufe08\ufe09"
    "\ufe0a\ufe0b\ufe0c\ufe0d\ufe0e\ufe0f\ufe20\ufe21\ufe22\ufe23\ufe24\ufe25\ufe26\ufe27\ufe28\ufe29"
    "\ufe2a\ufe2b\ufe2c\ufe2d\ufe2e\ufe2f\ufeff\U000e0100\U000e0101\U000e0102\U000e0103\U000e0104\U000e0105\U000e0106\U000e0107\U000e0108"
    "\U000e0109\U000e010a\U000e010b\U000e010c\U000e010d\U000e010e\U000e010f\U000e0110\U000e0111\U000e0112\U000e0113\U000e0114\U000e0115\U000e0116\U000e0117\U000e0118"
    "\U000e0119\U000e011a\U000e011b\U000e011c\U000e011d\U000e011e\U000e011f\U000e0120\U000e0121\U000e0122\U000e0123\U000e0124\U000e0125\U000e0126\U000e0127\U000e0128"
    "\U000e0129\U000e012a\U000e012b\U000e012c\U000e012d\U000e012e\U000e012f\U000e0130\U000e0131\U000e0132\U000e0133\U000e0134\U000e0135\U000e0136\U000e0137\U000e0138"
    "\U000e0139\U000e013a\U000e013b\U000e013c\U000e013d\U000e013e\U000e013f\U000e0140\U000e0141\U000e0142\U000e0143\U000e0144\U000e0145\U000e0146\U000e0147\U000e0148"
    "\U000e0149\U000e014a\U000e014b\U000e014c\U000e014d\U000e014e\U000e014f\U000e0150\U000e0151\U000e0152\U000e0153\U000e0154\U000e0155\U000e0156\U000e0157\U000e0158"
    "\U000e0159\U000e015a\U000e015b\U000e015c\U000e015d\U000e015e\U000e015f\U000e0160\U000e0161\U000e0162\U000e0163\U000e0164\U000e0165\U000e0166\U000e0167\U000e0168"
    "\U000e0169\U000e016a\U000e016b\U000e016c\U000e016d\U000e016e\U000e016f\U000e0170\U000e0171\U000e0172\U000e0173\U000e0174\U000e0175\U000e0176\U000e0177\U000e0178"
    "\U000e0179\U000e017a\U000e017b\U000e017c\U000e017d\U000e017e\U000e017f\U000e0180\U000e0181\U000e0182\U000e0183\U000e0184\U000e0185\U000e0186\U000e0187\U000e0188"
    "\U000e0189\U000e018a\U000e018b\U000e018c\U000e018d\U000e018e\U000e018f\U000e0190\U000e0191\U000e0192\U000e0193\U000e0194\U000e0195\U000e0196\U000e0197\U000e0198"
    "\U000e0199\U000e019a\U000e019b\U000e019c\U000e019d\U000e019e\U000e019f\U000e01a0\U000e01a1\U000e01a2\U000e01a3\U000e01a4\U000e01a5\U000e01a6\U000e01a7\U000e01a8"
    "\U000e01a9\U000e01aa\U000e01ab\U000e01ac\U000e01ad\U000e01ae\U000e01af\U000e01b0\U000e01b1\U000e01b2\U000e01b3\U000e01b4\U000e01b5\U000e01b6\U000e01b7\U000e01b8"
    "\U000e01b9\U000e01ba\U000e01bb\U000e01bc\U000e01bd\U000e01be\U000e01bf\U000e01c0\U000e01c1\U000e01c2\U000e01c3\U000e01c4\U000e01c5\U000e01c6\U000e01c7\U000e01c8"
    "\U000e01c9\U000e01ca\U000e01cb\U000e01cc\U000e01cd\U000e01ce\U000e01cf\U000e01d0\U000e01d1\U000e01d2\U000e01d3\U000e01d4\U000e01d5\U000e01d6\U000e01d7\U000e01d8"
    "\U000e01d9\U000e01da\U000e01db\U000e01dc\U000e01dd\U000e01de\U000e01df\U000e01e0\U000e01e1\U000e01e2\U000e01e3\U000e01e4\U000e01e5\U000e01e6\U000e01e7\U000e01e8"
    "\U000e01e9\U000e01ea\U000e01eb\U000e01ec\U000e01ed\U000e01ee\U000e01ef"
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
    # --- Non-regex whitespace/digit skew (issue #244) ------------------------
    # Two first-party sites whose host-language string primitives (Python
    # `str.strip()`/`int(str)` vs JS `trim()`/`parseInt`) disagreed on which
    # codepoints count as whitespace, and (int only) on which digits count. Both
    # are respelled onto the ONE union whitespace class + ASCII `[0-9]`, so the
    # class stays registry-sourced and the two engines converge.
    #
    # (a) The union-whitespace trim. ONE constant (`_WS_TRIM_RE`/`WS_TRIM_RE`)
    #     used at FOUR call sites that all trim the union whitespace class off
    #     both ends: the dedup-signature title strip AND the three review-line
    #     strips (load_exclusions' fenced-block + bullet-fallback, and
    #     parse_review_md's ignore-item) whose per-line `str.strip()`/`trim()`
    #     otherwise diverged on U+001C-U+001F/U+0085/U+FEFF -- the same six
    #     codepoints the signature strip did -- silently zeroing a user
    #     exclusion/ignore pattern carrying one of them. Python site is a
    #     module-level `_WS_TRIM_RE = re.compile(...)` used with `.sub("", ...)`
    #     (inherently global); the JS site is `.replace(/.../g, '')`, so it
    #     carries the GLOBAL flag the Python `.sub` does not need -- the per-twin
    #     flag split InlineSite exists for. The literal appears once per twin (the
    #     constant declaration); the other three sites reuse it BY NAME.
    InlineSite(
        name="ws_trim",
        anchor=r"]+|[",
        pattern=r"^[{WS}]+|[{WS}]+$",
        py_flags=(),
        js_flags=("GLOBAL",),
    ),
    # (b) The line_start string-coercion gate. Python site is a module-level
    #     `_INT_COERCE_RE = re.compile(...)` used with `.match`; the JS site is
    #     inline in `pyIntOrNull` (`.exec`). No flags either side -- the pattern
    #     is ASCII-explicit (`[0-9]`, the union class), so it is INERT and re.ASCII
    #     would be a no-op. int()/parseInt run on the CAPTURE, never the raw string.
    InlineSite(
        name="line_start_int_coerce",
        anchor=r"[+-]?[0-9]+",
        pattern=r"^[{WS}]*([+-]?[0-9]+)[{WS}]*$",
        py_flags=(),
        js_flags=(),
    ),
)

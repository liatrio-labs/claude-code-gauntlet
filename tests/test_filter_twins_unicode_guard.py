"""Structural guard for issue #211: the filter-twin unicode word-boundary /
whitespace / case-fold pin.

Discovery is by SHAPE, not by a name allowlist, so a brand-new pattern list
or call site is covered by default (AGENTS.md: "extending should cost one
edit" -- the guard must not need editing just because a pattern was added).

Python side: an ``ast`` walk of scripts/filter_findings.py finds (a) every
module/function-local ``Assign`` of a list of string constants that contains
a regex metacharacter, and (b) every ``re.compile``/``re.search``/``re.match``/
``re.sub`` call site, with its enclosing function (for scoping) and, where
resolvable, its literal pattern text and whether ``re.ASCII`` is present in
its flags.

JS side: a line-oriented scan of workflows/src/filterFindings.js for
``NAME = [ ... ]`` regex-literal arrays and ``NAME =\\n  /pattern/flags;``
single-pattern consts, keyed off the SAME family names the Python side
discovered (leading-underscore stripped) -- never a hardcoded pair list.
Array-bracket matching alone is not used to find element boundaries (a
bracket count is broken by ``]`` inside a character class); each array
element is instead recognized by the whole-line shape
``^\\s*(/.*/[a-z]*),?$``.

Scope ("first-party finding-text pattern") is structural, not a positive
allowlist of what to check:

- FORBIDDEN (must NOT carry re.ASCII): call sites enclosed by
  ``apply_exclusions`` -- user-authored REVIEW.md ignore patterns over
  arbitrary-script finding text; re.ASCII there breaks legitimate folding
  (e.g. "café" no longer matching "CAFÉ"), pinned separately by the
  exclusions/case_fold_unicode_cafe parity fixture.
- NAMED-EXEMPT (out of #211 scope entirely, adjacent issue): call sites
  enclosed by ``parse_review_md`` or ``load_exclusions`` -- these parse
  REVIEW.md / exclusions-md file FORMAT (config block markers, bullet
  lines), not attacker-controlled finding text.
- INERT-EXEMPT (re.ASCII would be a structural no-op): a call whose
  resolved pattern text contains none of ``\\b \\B \\w \\W \\d \\D \\s \\S`` AND
  whose flags carry no ``re.IGNORECASE`` -- re.ASCII changes the meaning of
  those eight escapes (not just the word-boundary four -- it also narrows
  ``\\d``/``\\D`` to ASCII digits and ``\\s``/``\\S`` to ASCII whitespace) and
  of IGNORECASE folding, so a call using none of the eight is unaffected by
  the flag either way. This is a content rule, not a name rule: it currently
  covers ``_WORD_SPLIT_RE`` (an explicit character class, no \\w/\\b/\\d/\\s at
  all) and the file-path template check (``<[^\\n]*?>|\\{[^\\n]*?\\}``,
  punctuation only). Everything else defaults to REQUIRED.

Everything not in one of those buckets is REQUIRED to carry re.ASCII by
default -- a 9th injection list added tomorrow with no flag fails this test
immediately, without anyone updating an allowlist.
"""

import ast
import re
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PY_SRC = REPO / "scripts" / "filter_findings.py"
JS_SRC = REPO / "workflows" / "src" / "filterFindings.js"

_METACHAR_RE = re.compile(r"[\\\[\]()|+*?{}.^$]")
_ASCII_SENSITIVE_TOKEN_RE = re.compile(r"\\[bBwWdDsS]")

_REGEX_CALL_METHODS = {
    "compile",
    "search",
    "match",
    "split",
    "sub",
    "subn",
    "finditer",
    "findall",
    "fullmatch",
}

# Call sites enclosed by these functions match USER-AUTHORED, arbitrary-script
# patterns over finding text (#211 decision item 1) -- MUST NOT carry re.ASCII.
_ASCII_FORBIDDEN_FUNCS = {
    "apply_exclusions": (
        "user-authored REVIEW.md ignore patterns over arbitrary-script "
        "finding text; re.ASCII breaks legitimate unicode folding"
    ),
}

# Call sites enclosed by these functions parse REVIEW.md / exclusions-md file
# FORMAT (config block markers, bullet-list lines), not attacker-controlled
# finding text -- out of #211's scope (tracked: "parse_review_md regex family
# audit" adjacent issue). Exempt from BOTH the re.ASCII and the \s-respell
# requirements below.
_NAMED_EXEMPT_FUNCS = {
    "parse_review_md": "parses REVIEW.md config block markers, not finding text",
    "load_exclusions": "parses exclusions-md file FORMAT, not finding text",
}


class _Discovery(ast.NodeVisitor):
    def __init__(self):
        self.list_families = {}  # name -> {"patterns": [...], "func": str|None}
        self.calls = []  # [{"func", "has_ascii", "has_ignorecase", "pattern_text"}]
        self._func_stack = []

    def _current_func(self):
        return self._func_stack[-1] if self._func_stack else None

    def visit_FunctionDef(self, node):
        self._func_stack.append(node.name)
        self.generic_visit(node)
        self._func_stack.pop()

    def visit_Assign(self, node):
        if (
            isinstance(node.value, ast.List)
            and node.value.elts
            and all(
                isinstance(e, ast.Constant) and isinstance(e.value, str)
                for e in node.value.elts
            )
        ):
            strs = [e.value for e in node.value.elts]
            if any(_METACHAR_RE.search(s) for s in strs):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        self.list_families[target.id] = {
                            "patterns": strs,
                            "func": self._current_func(),
                        }
        self.generic_visit(node)

    def visit_Call(self, node):
        if (
            isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "re"
            and node.func.attr in _REGEX_CALL_METHODS
        ):
            flag_exprs = list(node.args[1:]) + [kw.value for kw in node.keywords]
            has_ascii = any(self._expr_has_flag(e, "ASCII") for e in flag_exprs)
            has_ignorecase = any(
                self._expr_has_flag(e, "IGNORECASE") for e in flag_exprs
            )
            pattern_text = None
            if (
                node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
            ):
                pattern_text = node.args[0].value
            self.calls.append(
                {
                    "func": self._current_func(),
                    "has_ascii": has_ascii,
                    "has_ignorecase": has_ignorecase,
                    "pattern_text": pattern_text,
                    "lineno": node.lineno,
                }
            )
        self.generic_visit(node)

    def _expr_has_flag(self, expr, flag_name):
        if (
            isinstance(expr, ast.Attribute)
            and isinstance(expr.value, ast.Name)
            and expr.value.id == "re"
            and expr.attr == flag_name
        ):
            return True
        if isinstance(expr, ast.BinOp):
            return self._expr_has_flag(expr.left, flag_name) or self._expr_has_flag(
                expr.right, flag_name
            )
        return False


def _discover_python():
    tree = ast.parse(PY_SRC.read_text(encoding="utf-8"), filename=str(PY_SRC))
    d = _Discovery()
    d.visit(tree)
    return d.list_families, d.calls


def _is_inert(call):
    if call["has_ignorecase"]:
        return False
    text = call["pattern_text"]
    if text is None:
        return False  # unresolvable pattern (e.g. a comprehension var) -- not inert by construction
    return not _ASCII_SENSITIVE_TOKEN_RE.search(text)


# --- JS-side discovery: line-oriented, keyed off Python-discovered names ---

_JS_LIST_START_RE = re.compile(r"^const (\w+) = \[$")
_JS_LIST_END_RE = re.compile(r"^\];$")
# Greedy `.*` (not a hand-rolled escaped-char/non-slash alternation) so a
# BARE, legally-unescaped `/` inside a character class (e.g. `[A-Za-z0-9+/]`)
# does not end the match early -- .* backtracks to the rightmost
# `/flags,?$` on the line, which is always the real literal's closing
# delimiter since every element here is confirmed one-per-line (measured
# rt2 §5: naive bracket/slash matching breaks on exactly this shape).
_JS_ELEMENT_RE = re.compile(r"^\s*(/.*/[a-z]*),?$")
_JS_CONST_HEAD_RE = re.compile(r"^const (\w+) =$")
_JS_CONST_BODY_RE = re.compile(r"^\s*(/.*/[a-z]*);$")
# Any JS regex literal immediately followed by `.test(` -- catches an inline
# literal used at its call site (as opposed to a named const/array, which the
# other _JS_* finders above already cover). Deliberately not anchored to a
# fixed set of line numbers: a new inline `.test(` call anywhere in the file
# is picked up automatically.
_JS_INLINE_TEST_RE = re.compile(r"(/.*/[a-z]*)\.test\(")


def _js_lines():
    return JS_SRC.read_text(encoding="utf-8").splitlines()


def _find_js_list(name):
    """Return the ordered list of regex-literal sources for `const NAME = [...]`,
    or None if no such array is found."""
    lines = _js_lines()
    i = 0
    while i < len(lines):
        m = _JS_LIST_START_RE.match(lines[i])
        if m and m.group(1) == name:
            elements = []
            j = i + 1
            while j < len(lines) and not _JS_LIST_END_RE.match(lines[j]):
                em = _JS_ELEMENT_RE.match(lines[j])
                if em:
                    elements.append(em.group(1))
                j += 1
            return elements
        i += 1
    return None


def _find_js_inline_test_literals():
    """Return, in source order, the regex-literal source of every JS inline
    `/pattern/flags.test(...)` call site -- the inline suppression-rule
    regexes and the file-path template check, none of which is a named
    const/array the other _find_js_* helpers key off of."""
    out = []
    for line in _js_lines():
        m = _JS_INLINE_TEST_RE.search(line)
        if m:
            out.append(m.group(1))
    return out


def _find_js_const_pattern(name):
    """Return the single regex-literal source for `const NAME =\\n  /pat/flags;`,
    or None if not found."""
    lines = _js_lines()
    for i, line in enumerate(lines):
        m = _JS_CONST_HEAD_RE.match(line)
        if m and m.group(1) == name and i + 1 < len(lines):
            bm = _JS_CONST_BODY_RE.match(lines[i + 1])
            if bm:
                return bm.group(1)
    return None


def _py_pattern_text(value):
    """Undo Python-string-delimiter escaping that has nothing to do with the
    regex itself: `_INJECTION_URL_PATTERNS`' one pattern needs a literal `"`
    inside the class `[^...)>"']`, and since the raw string housing it is
    itself double-quoted, Python's raw-string syntax requires a backslash
    before that `"` to keep the string from ending early -- a raw string
    does NOT strip that backslash from the resulting value (verified: the
    parsed ast.Constant retains the literal 2-char `\\"` ). JS's regex
    literal, delimited by `/` not by a quote character, needs no such
    escape. Both compile to the same single literal `"`, so the backslash
    is source-syntax noise, not a pattern-content difference."""
    return value.replace('\\"', '"')


def _js_literal_to_regex_text(js_literal):
    """Strip a JS regex literal's /.../flags delimiters to bare regex source,
    and unescape `\\/` back to a bare `/` -- JS regex-literal syntax requires
    escaping a literal slash so it doesn't end the literal, but that escape
    is JS-delimiter syntax, not pattern content: Python's raw strings need
    no equivalent escape for the same literal `/`, so leaving `\\/` in would
    make an identical pattern compare unequal across twins."""
    last_slash = js_literal.rfind("/")
    assert js_literal.startswith("/") and last_slash > 0, js_literal
    return js_literal[1:last_slash].replace("\\/", "/")


class TestFilterTwinsUnicodeGuard(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.list_families, cls.calls = _discover_python()

    def _scope(self, func):
        if func in _ASCII_FORBIDDEN_FUNCS:
            return "forbidden"
        if func in _NAMED_EXEMPT_FUNCS:
            return "named_exempt"
        return "in_scope"

    def test_discovery_finds_the_known_shape(self):
        # Internal consistency check, doubling as documentation: if this
        # drifts, the guard's own discovery mechanism changed shape and every
        # assertion below needs a second look before trusting it.
        injection_lists = {
            name: fam
            for name, fam in self.list_families.items()
            if name.startswith("_INJECTION_")
        }
        self.assertEqual(len(injection_lists), 8, sorted(injection_lists))
        self.assertIn("block_patterns", self.list_families)
        self.assertEqual(
            self.list_families["block_patterns"]["func"], "parse_review_md"
        )
        # 25 TEST_CORRECTNESS compiles + 2 keyword-set compiles + 1 word-split
        # compile (func=None, module level) = 28, + 2 list-comp compiles (#215
        # round-1 parity-F4/F5 collapsed the seven _CONTENT_PATTERN_SETS
        # compiles into ONE re.compile call site inside apply_injection_filter's
        # generator expression -- one AST node compiling all seven sets, still
        # carrying the same literal re.IGNORECASE | re.ASCII flags this guard
        # checks per call site, so nothing is unprotected; title_re's own
        # list-comp compile stays separate) + 1 file-path search
        # (func=apply_injection_filter) = 3, + 2 suppression
        # (func=detect_disagreement), + parse_review_md's 6, + load_exclusions'
        # 2, + apply_exclusions' 1 = 42
        self.assertEqual(len(self.calls), 42, len(self.calls))

    def test_no_first_party_pattern_contains_bare_whitespace_class(self):
        """(i) No first-party finding-text pattern contains \\s or \\S -- they
        must use the explicit union class instead."""
        offenders = []
        for name, fam in self.list_families.items():
            if fam["func"] in _NAMED_EXEMPT_FUNCS:
                continue
            for p in fam["patterns"]:
                if "\\s" in p or "\\S" in p:
                    offenders.append(f"{name}: {p!r}")
        for call in self.calls:
            if call["func"] in _NAMED_EXEMPT_FUNCS:
                continue
            text = call["pattern_text"]
            if text is None:
                continue
            if "\\s" in text or "\\S" in text:
                offenders.append(f"call@{call['lineno']}: {text!r}")
        self.assertEqual(
            offenders, [], f"bare \\s/\\S found in first-party pattern(s): {offenders}"
        )

    def test_every_first_party_call_site_carries_re_ascii(self):
        """(ii) Every first-party Python call/compile site carries re.ASCII;
        the apply_exclusions site must NOT (forbidden, asserted separately
        below)."""
        missing = []
        for call in self.calls:
            scope = self._scope(call["func"])
            if scope in ("forbidden", "named_exempt"):
                continue
            if _is_inert(call):
                continue
            if not call["has_ascii"]:
                missing.append(
                    f"call@{call['lineno']} (func={call['func']}): missing re.ASCII"
                )
        self.assertEqual(
            missing, [], f"first-party call site(s) missing re.ASCII: {missing}"
        )

    def test_apply_exclusions_has_no_re_ascii(self):
        """(ii), negative half: apply_exclusions' re.search MUST NOT carry
        re.ASCII -- pinned here so a future edit that "fixes" this by adding
        the flag is caught immediately; the café/CAFÉ fixture pins the
        resulting BEHAVIOR, this pins the FLAG."""
        forbidden_calls = [c for c in self.calls if c["func"] in _ASCII_FORBIDDEN_FUNCS]
        self.assertTrue(
            forbidden_calls, "expected to find apply_exclusions' re.search call"
        )
        for call in forbidden_calls:
            self.assertFalse(
                call["has_ascii"],
                f"call@{call['lineno']} (func={call['func']}) must NOT carry re.ASCII: "
                f"{_ASCII_FORBIDDEN_FUNCS[call['func']]}",
            )

    def test_first_party_families_are_byte_identical_across_twins(self):
        """(iii) Each first-party pattern family is element-wise byte-identical
        between the Python and JS twins (after de-quoting each side's own
        string/regex-literal syntax)."""
        mismatches = []

        # The 8 _INJECTION_* lists -- Python name minus leading underscore is
        # the JS array name, by construction (both sides are hand-paired by
        # this exact naming convention throughout filterFindings.js).
        for py_name, fam in self.list_families.items():
            if not py_name.startswith("_INJECTION_"):
                continue
            js_name = py_name[1:]
            js_elements = _find_js_list(js_name)
            if js_elements is None:
                mismatches.append(f"{js_name}: not found in JS source")
                continue
            py_texts = [_py_pattern_text(p) for p in fam["patterns"]]
            js_texts = [_js_literal_to_regex_text(e) for e in js_elements]
            if py_texts != js_texts:
                mismatches.append(
                    f"{py_name}/{js_name}: element mismatch\n  py={py_texts}\n  js={js_texts}"
                )

        # FUNCTIONAL_VIOLATION_KEYWORDS / TYPE_SAFETY_BUG_KEYWORDS: single
        # compiled alternation patterns (Python: adjacent-string-literal
        # concatenation already merged into one ast.Constant by the parser).
        # Located directly by assignment target name (same technique as the
        # _TEST_CORRECTNESS_PATTERNS lookup below), not by pattern-text
        # sniffing, since these are `NAME = re.compile(...)` assignments, not
        # list elements this class's other discovery rules already capture.
        tree = ast.parse(PY_SRC.read_text(encoding="utf-8"), filename=str(PY_SRC))
        for py_name in ("_FUNCTIONAL_VIOLATION_KEYWORDS", "_TYPE_SAFETY_BUG_KEYWORDS"):
            js_name = py_name[1:]
            assign_node = None
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.Assign)
                    and len(node.targets) == 1
                    and isinstance(node.targets[0], ast.Name)
                    and node.targets[0].id == py_name
                ):
                    assign_node = node
                    break
            self.assertIsNotNone(
                assign_node, f"could not locate {py_name}'s assignment"
            )
            self.assertIsInstance(assign_node.value, ast.Call)
            self.assertIsInstance(assign_node.value.args[0], ast.Constant)
            py_text = assign_node.value.args[0].value

            js_pattern = _find_js_const_pattern(js_name)
            self.assertIsNotNone(js_pattern, f"{js_name}: not found in JS source")
            js_text = _js_literal_to_regex_text(js_pattern)
            if py_text != js_text:
                mismatches.append(
                    f"{py_name}/{js_name}: mismatch\n  py={py_text}\n  js={js_text}"
                )

        # _TEST_CORRECTNESS_PATTERNS: 25 module-level re.compile(...) calls,
        # in source order, vs the JS array of the same length in the same order.
        # Located by re-walking the AST list directly (module-level Call list,
        # not caught by visit_Assign's "list of Constants" rule since its
        # elements are Call nodes, not Constants), rather than filtering
        # self.calls -- module scope holds other compiles too (FUNCTIONAL_
        # VIOLATION_KEYWORDS, TYPE_SAFETY_BUG_KEYWORDS, WORD_SPLIT_RE), and
        # this list literal is the only structural anchor for "these 25, in
        # this order".
        tree = ast.parse(PY_SRC.read_text(encoding="utf-8"), filename=str(PY_SRC))
        tc_node = None
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Assign)
                and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and node.targets[0].id == "_TEST_CORRECTNESS_PATTERNS"
            ):
                tc_node = node
                break
        self.assertIsNotNone(
            tc_node, "could not locate _TEST_CORRECTNESS_PATTERNS assignment"
        )
        py_tc_texts = []
        for elt in tc_node.value.elts:
            self.assertIsInstance(elt, ast.Call)
            self.assertIsInstance(elt.args[0], ast.Constant)
            py_tc_texts.append(elt.args[0].value)

        js_tc_elements = _find_js_list("TEST_CORRECTNESS_PATTERNS")
        self.assertIsNotNone(
            js_tc_elements, "TEST_CORRECTNESS_PATTERNS: not found in JS source"
        )
        js_tc_texts = [_js_literal_to_regex_text(e) for e in js_tc_elements]
        if py_tc_texts != js_tc_texts:
            mismatches.append(
                f"_TEST_CORRECTNESS_PATTERNS/TEST_CORRECTNESS_PATTERNS: element mismatch\n"
                f"  py={py_tc_texts}\n  js={js_tc_texts}"
            )

        self.assertEqual(mismatches, [], "\n".join(mismatches))

    def test_inline_first_party_literals_are_byte_identical_across_twins(self):
        """(v) The inline (not-a-named-const) first-party call sites -- the
        file-path template check and the two detect_disagreement suppression
        regexes -- are element-wise byte-identical between the twins, same
        technique as test_first_party_families_are_byte_identical_across_twins
        above. #211 round-2 review R2A-F1/B2: these three sites are inline
        literals, not part of any named list/const family the other
        byte-identity assertions in this class walk, and a JS-only edit to
        one of them (a suppression word added/changed, or an alternative
        dropped from the file-path check) is INVISIBLE to every other
        assertion in this class as long as it doesn't change the union
        class's total occurrence count -- measured: `\\bdeliberate\\b` deleted
        from JS suppression rule 1, and the file-path check's `\\{...\\}`
        alternative dropped from JS, both leave the full pytest suite, the
        full node suite, and every other assertion in this class green.
        """
        py_texts = [
            _py_pattern_text(c["pattern_text"])
            for c in self.calls
            if c["func"] is not None
            and self._scope(c["func"]) == "in_scope"
            and c["pattern_text"] is not None
        ]
        js_texts = [
            _js_literal_to_regex_text(lit) for lit in _find_js_inline_test_literals()
        ]
        self.assertEqual(len(py_texts), 3, py_texts)
        self.assertEqual(py_texts, js_texts)

    def test_union_class_constant_is_byte_identical_across_twins(self):
        """(iv) The union whitespace class constant appears with the same
        byte spelling in both twins. Built once here from integers (never
        hand-typed) so this assertion cannot itself be the thing that's
        wrong."""
        contents = "".join(
            [
                "\\t",
                "\\n",
                "\\x0b",
                "\\x0c",
                "\\r",
                " ",
                "\\x1c-\\x1f",
                "\\x85",
                "\\xa0",
                "\\u1680",
                "\\u2000-\\u200a",
                "\\u2028",
                "\\u2029",
                "\\u202f",
                "\\u205f",
                "\\u3000",
                "\\ufeff",
            ]
        )
        full = "[" + contents + "]"
        py_text = PY_SRC.read_text(encoding="utf-8")
        js_text = JS_SRC.read_text(encoding="utf-8")
        self.assertIn(
            full,
            py_text,
            "union class spelling not found verbatim in scripts/filter_findings.py",
        )
        self.assertIn(
            full,
            js_text,
            "union class spelling not found verbatim in workflows/src/filterFindings.js",
        )
        self.assertGreaterEqual(
            py_text.count(full),
            8,
            "expected the union class to appear at multiple Python call sites",
        )
        self.assertGreaterEqual(
            js_text.count(full),
            8,
            "expected the union class to appear at multiple JS call sites",
        )

        # Cross-twin equality on the INNER spelling (contents + closing bracket),
        # so both the plain `[...]` form and the `[-...]` variant (which prefixes
        # a literal `-`, e.g. the auto[-<union>]?generated suppression rule) are
        # counted together. This is a cheap count-parity FLOOR over the union
        # class's total occurrences in each whole file -- it catches only a
        # change to how many times the class appears, not what else changed
        # around it. Element-wise byte identity for the two suppression-rule
        # regexes and the file-path template check is asserted precisely by
        # test_inline_first_party_literals_are_byte_identical_across_twins
        # above (#211 round-2 review R2A-F1/F2/B2); this count assertion is
        # kept alongside it as a belt-and-suspenders backstop, not the
        # mechanism that reaches those sites.
        inner = contents + "]"
        self.assertEqual(
            py_text.count(inner),
            js_text.count(inner),
            "union class inner spelling occurs a different number of times in "
            "each twin -- a JS-only or Python-only respelling of an inline "
            "site (e.g. a suppression rule) went undetected",
        )


if __name__ == "__main__":
    unittest.main()

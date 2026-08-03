"""Dead-export guard for workflows/src (Issue #37).

Every export function must be referenced from workflows/src outside its export
declaration, or appear in EXPORT_ALLOWLIST with an inventory/issue citation.
Parity-only exports owned by #24 stay allowlisted, not deleted here.

"Referenced" means referenced from *code*: comments, string literals, and regex
literals are blanked before the search. A raw-text search made the guard unable
to fail — every allowlisted name is discussed in a neighbouring comment, so
deleting its allowlist entry still passed, and two genuinely dead exports
(`stages.js:parseWriterPayload`, `args.js:normalizeArgs`) were invisible behind
their own comments.
"""

import re
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "workflows" / "src"

_EXPORT_FN = re.compile(r"^export\s+(?:async\s+)?function\s+(\w+)\s*\(", re.M)

# key: "filterFindings.js:parseReviewMd" -> inventory ID + citation
EXPORT_ALLOWLIST = {
    "filterFindings.js:parseReviewMd": "R-003 owned-elsewhere:#24",
    "filterFindings.js:loadExclusions": "R-003 owned-elsewhere:#24",
    "stages.js:parseWriterPayload": "R-044 intentional-and-documented (test-only)",
    "args.js:normalizeArgs": "R-045 intentional-and-documented (test-only)",
}


# A `/` opening a regex literal can only follow one of these, or a keyword, or
# nothing. After anything else (an identifier, `)`, `]`, a digit) it is division.
# Getting this wrong desynchronises the scan: `!/["\\]/.test(x)` in args.js would
# otherwise open a double-quoted string that swallows the rest of the file.
_REGEX_PRECEDERS = set("(,=:[!&|?{};+-*%~^<>")
_REGEX_KEYWORDS = {
    "return",
    "typeof",
    "case",
    "in",
    "of",
    "new",
    "delete",
    "void",
    "do",
    "else",
    "yield",
    "await",
    "instanceof",
}
_TRAILING_WORD = re.compile(r"(\w+)$")


def _opens_regex(code_tail: str) -> bool:
    """True when a `/` following *code_tail* (recent non-space code) starts a regex."""
    if not code_tail:
        return True
    if code_tail[-1] in _REGEX_PRECEDERS:
        return True
    word = _TRAILING_WORD.search(code_tail)
    return bool(word) and word.group(1) in _REGEX_KEYWORDS


def strip_comments_and_strings(text: str) -> str:
    """Blank out JS comments, string/template literals, and regex literals.

    A name that survives only inside a comment or a quoted string is not a
    reference — that is how `parseReviewMd` and `loadExclusions` looked live
    while nothing called them. Template-literal `${...}` interpolations are real
    code and are kept. Every blanked character becomes a space (newlines
    survive) so line numbers and the `^export` anchor still line up.
    """
    out: list[str] = []
    tail = ""  # recent non-whitespace code characters, for regex-vs-division
    interpolations: list[int] = []  # brace depth per open `${`
    state: str | None = None  # None | line | block | regex | ' | " | `
    in_class = False  # inside a regex `[...]`, where `/` is literal
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        nxt = text[i + 1] if i + 1 < n else ""
        if state is None:
            if ch == "/" and nxt == "/":
                state, i = "line", i + 2
                out.append("  ")
            elif ch == "/" and nxt == "*":
                state, i = "block", i + 2
                out.append("  ")
            elif ch == "/" and _opens_regex(tail):
                state, in_class, i = "regex", False, i + 1
                out.append(" ")
                tail = ""
            elif ch in "'\"`":
                state, i = ch, i + 1
                out.append(" ")
                tail = ""
            elif interpolations and ch in "{}":
                interpolations[-1] += 1 if ch == "{" else -1
                if interpolations[-1] == 0:
                    interpolations.pop()
                    state = "`"
                out.append(" " if state == "`" else ch)
                tail = "" if state == "`" else (tail + ch)[-24:]
                i += 1
            else:
                out.append(ch)
                if not ch.isspace():
                    tail = (tail + ch)[-24:]
                i += 1
        elif state == "line":
            out.append("\n" if ch == "\n" else " ")
            if ch == "\n":
                state = None
            i += 1
        elif state == "block":
            if ch == "*" and nxt == "/":
                state, i = None, i + 2
                out.append("  ")
            else:
                out.append("\n" if ch == "\n" else " ")
                i += 1
        elif state == "regex":
            if ch == "\\":
                out.append("  "[: min(2, n - i)])
                i += 2
                continue
            if ch == "[":
                in_class = True
            elif ch == "]":
                in_class = False
            elif ch == "/" and not in_class:
                state, tail = None, "_"  # a closed literal is a value, not an operator
            out.append("\n" if ch == "\n" else " ")
            i += 1
        elif state == "`" and ch == "$" and nxt == "{":
            interpolations.append(1)
            state, tail, i = None, "", i + 2
            out.append("  ")
        elif ch == "\\":
            out.append("  "[: min(2, n - i)])
            i += 2
        elif ch == state:
            state, tail, i = None, "_", i + 1
            out.append(" ")
        else:
            out.append("\n" if ch == "\n" else " ")
            i += 1
    return "".join(out)


def code_text(path: Path) -> str:
    return strip_comments_and_strings(path.read_text(encoding="utf-8"))


def export_functions() -> list[tuple[str, str, Path]]:
    out = []
    for path in sorted(SRC.glob("*.js")):
        for m in _EXPORT_FN.finditer(code_text(path)):
            out.append((path.name, m.group(1), path))
    return out


def is_referenced(name: str, defining: Path) -> bool:
    pat = re.compile(rf"\b{re.escape(name)}\b")
    for path in sorted(SRC.glob("*.js")):
        for line in code_text(path).splitlines():
            if not pat.search(line):
                continue
            declaration = _EXPORT_FN.search(line)
            if path == defining and declaration and declaration.group(1) == name:
                continue  # skip the export declaration line
            return True
    return False


class TestWorkflowsDeadExports(unittest.TestCase):
    def test_exports_are_live_or_allowlisted(self):
        offenders = {}
        for mod, name, path in export_functions():
            key = f"{mod}:{name}"
            if key in EXPORT_ALLOWLIST:
                continue
            if not is_referenced(name, path):
                offenders[key] = "no workflows/src reference outside export line"
        self.assertEqual(offenders, {}, f"dead exports: {offenders}")

    def test_allowlist_entries_exist(self):
        keys = {f"{m}:{n}" for m, n, _ in export_functions()}
        missing = sorted(set(EXPORT_ALLOWLIST) - keys)
        self.assertEqual(missing, [], f"allowlist names missing from src: {missing}")

    def test_allowlist_entries_are_still_dead(self):
        # The other direction: an entry that became live is a stale exemption,
        # and leaving it in means the next death at that name goes unreported.
        revived = {
            f"{mod}:{name}"
            for mod, name, path in export_functions()
            if f"{mod}:{name}" in EXPORT_ALLOWLIST and is_referenced(name, path)
        }
        self.assertEqual(revived, set(), f"allowlisted but now live: {revived}")

    def test_allowlist_entries_cite_an_inventory_id(self):
        uncited = sorted(
            key
            for key, why in EXPORT_ALLOWLIST.items()
            if not re.match(r"R-\d{3}\b", why)
        )
        self.assertEqual(uncited, [], f"allowlist entries without an R-id: {uncited}")

    def test_scrubber_blanks_comments_and_literals_but_keeps_code(self):
        source = "\n".join(
            [
                "// ghostName in a line comment",
                "/* ghostName in a block",
                "   comment */",
                "const s = 'ghostName in a string';",
                'const d = "ghostName again";',
                "const t = `prefix ${liveName} suffix ghostName`;",
                'if (!/["\\\\]/.test(x)) callName();',
                # Keyword-preceded regex branch of _opens_regex (return/case/typeof…):
                # without it, `/ghostName/` is misread as division and the token survives.
                "return /ghostName/.test(x);",
                "case /ghostName/:",
                "typeof /ghostName/;",
            ]
        )
        scrubbed = strip_comments_and_strings(source)
        self.assertNotIn("ghostName", scrubbed)
        self.assertIn("liveName", scrubbed)
        self.assertIn("callName", scrubbed)
        self.assertEqual(
            len(scrubbed.splitlines()),
            len(source.splitlines()),
            "blanking must preserve line structure",
        )


if __name__ == "__main__":
    unittest.main()

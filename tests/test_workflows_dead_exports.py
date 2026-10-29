import ast
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "workflows" / "src"

_EXPORT_FN = re.compile(r"^export\s+(?:async\s+)?function\s+(\w+)\s*\(", re.M)

EXPORT_ALLOWLIST = {
    "args.js:normalizeArgs": "R-045 intentional-and-documented (test-only)",
}


# A `/` opening a regex literal can only follow one of these, or a keyword, or
# nothing. After anything else (an identifier, `)`, `]`, a digit) it is division.
# Getting this wrong desynchronises the scan: `!/["\\]/.test(x)` in entry.js would
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
    if not code_tail:
        return True
    if code_tail[-1] in _REGEX_PRECEDERS:
        return True
    word = _TRAILING_WORD.search(code_tail)
    if word is None:
        return False
    return word.group(1) in _REGEX_KEYWORDS


def strip_comments_and_strings(text: str) -> str:
    # Only code references keep an export live. Template interpolations are code;
    # blanked literals retain newlines so export anchors and diagnostics stay aligned.
    out: list[str] = []
    tail = ""
    interpolations: list[int] = []
    state: str | None = None
    in_class = False
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
                continue
            return True
    # Bench probes execute JavaScript imports embedded in Python string literals.
    imported = re.compile(
        rf"import\s*\{{[^}}]*\b{re.escape(name)}\b[^}}]*\}}\s*from\s*"
        rf"['\"]\./workflows/src/{re.escape(defining.name)}['\"]"
    )
    for path in (REPO / "bench/runner").glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and imported.search(node.value)
            ):
                return True
    return False


def test_exports_are_live_or_allowlisted():
    offenders = {
        f"{mod}:{name}": "no workflows/src reference outside export line"
        for mod, name, path in export_functions()
        if f"{mod}:{name}" not in EXPORT_ALLOWLIST and not is_referenced(name, path)
    }
    assert offenders == {}, f"dead exports: {offenders}"


def test_allowlist_entries_exist():
    exports = {f"{mod}:{name}": path for mod, name, path in export_functions()}
    missing = sorted(set(EXPORT_ALLOWLIST) - exports.keys())
    assert missing == [], f"allowlist names missing from src: {missing}"
    # A revived exemption would hide the next death at the same name.
    revived = [
        key
        for key, path in exports.items()
        if key in EXPORT_ALLOWLIST and is_referenced(key.split(":")[1], path)
    ]
    assert revived == [], f"allowlisted but now live: {revived}"
    uncited = [
        key for key, why in EXPORT_ALLOWLIST.items() if not re.match(r"R-\d{3}\b", why)
    ]
    assert uncited == [], f"allowlist entries without an R-id: {uncited}"


def test_scrubber_blanks_comments_and_literals_but_keeps_code():
    source = "\n".join(
        [
            "// ghostName in a line comment",
            "/* ghostName in a block",
            "   comment */",
            "const s = 'ghostName in a string';",
            'const d = "ghostName again";',
            "const t = `prefix ${liveName} suffix ghostName`;",
            'if (!/["\\\\]/.test(x)) callName();',
            # Keyword-preceded regexes must not be misread as division.
            "return /ghostName/.test(x);",
            "case /ghostName/:",
            "typeof /ghostName/;",
        ]
    )
    scrubbed = strip_comments_and_strings(source)
    assert "ghostName" not in scrubbed
    assert "liveName" in scrubbed
    assert "callName" in scrubbed
    assert len(scrubbed.splitlines()) == 10, "blanking must preserve line structure"

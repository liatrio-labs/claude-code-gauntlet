"""Guard: the tracked documentation surface is an allowlist, not a landing zone.

Session scratch — design memos, implementation plans, handoff notes — kept landing
as tracked ``docs/*.md`` files (PR #120 removed four across two PRs). The gitignore
already quarantines the *known* scratch homes (``docs/superpowers/``,
``deep-review-*.md``, ``.superpowers/``), but an ignore list only blocks names it
anticipated; scratch written one directory up, under a fresh name, lands silently.
The durable homes for that material are the PR description and the issue thread.
This guard closes the gap from the other side: every tracked doc is a deliberate,
reviewable act — add the path to the allowlist below in the same commit, with a
reason a reviewer can weigh.

Scope: git-tracked files only (``git ls-files``), so gitignored local artifacts
never fail the suite.
"""

import fnmatch
import json
import re
import subprocess
import unittest
from pathlib import Path

from scripts import generate_contract_requirements as contract_generator

REPO = Path(__file__).resolve().parents[1]

# Spelled-out number words for the duplication register's row-count sentence
# ("Fifty-one rows: ..."). The table has never held fewer than thirty rows or
# more than sixty; extend the range here if it ever does.
_ONES = ["", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine"]
_TENS = {30: "thirty", 40: "forty", 50: "fifty", 60: "sixty"}
_NUMBER_WORDS = {60: _TENS[60]}
for _tens in (30, 40, 50):
    _NUMBER_WORDS[_tens] = _TENS[_tens]
    for _i in range(1, 10):
        _NUMBER_WORDS[_tens + _i] = f"{_TENS[_tens]}-{_ONES[_i]}"
_WORD_TO_NUMBER = {word: number for number, word in _NUMBER_WORDS.items()}

# Root-level markdown: community-health and contract files only. CHANGELOG.md is
# semantic-release-generated. Review outputs (deep-review-*.md and kin) are
# gitignored run artifacts and must never be tracked.
ROOT_MD_ALLOW = {
    "AGENTS.md",
    "CHANGELOG.md",
    "CLAUDE.md",
    "CODE_OF_CONDUCT.md",
    "CONTRIBUTING.md",
    "PRIVACY.md",
    "README.md",
    "REVIEW.md",
    "SECURITY.md",
}

# Files directly under docs/: living registries, required audit artifacts, and the
# maintainer standard. Entries may predate their file landing (an open PR may add
# one); the guard is subset-only in that direction on purpose.
DOCS_ALLOW = {
    "docs/duplication-register.md",  # living register, required by #55
    "docs/engineering-audit-2026-07.md",  # point-in-time audit artifact, required by #55
    "docs/machine-parsed-strings.md",  # living registry, required by #37 (PR #119)
    "docs/maintainer-issues.md",  # maintainer work-queue standard
    "docs/v3-residue-audit-2026-07.md",  # point-in-time audit artifact, required by #37 (PR #119)
    "docs/style/wording-rules.md",  # canonical output-style rule source
    "docs/style/cadence-rules.md",  # canonical output-style rule source
    "docs/style/session-context.md",  # generated session-output carrier
}

# Subtrees under docs/ with their own curated index; markdown only inside.
DOCS_ALLOWED_SUBTREES = ("docs/research/",)

POLICY = (
    "Session design/plan/handoff scratch never lands in the tree — it belongs in the "
    "PR description or issue thread. A genuinely durable doc is added to the allowlist "
    "in tests/test_docs_registry.py in the same commit, with a reason."
)


def tracked(pathspec):
    out = subprocess.run(
        ["git", "ls-files", "--", pathspec],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=True,
        encoding="utf-8",
    ).stdout
    return [line for line in out.splitlines() if line]


_TRACKED_FILES = None


def _register_sections(text):
    """Return ``(name, pre_table_sentence, data_rows)`` for every ``##`` section."""
    lines = text.splitlines()
    starts = [i for i, line in enumerate(lines) if line.startswith("## ")]
    sections = []
    separator = re.compile(r":?-{3,}:?")
    for position, start in enumerate(starts):
        end = starts[position + 1] if position + 1 < len(starts) else len(lines)
        name = lines[start][3:]
        table_start = None
        for i in range(start + 1, end - 1):
            if not lines[i].strip().startswith("|"):
                continue
            header = _split_table_cells(lines[i])
            separators = _split_table_cells(lines[i + 1])
            if (
                header
                and separators
                and all(separator.fullmatch(cell.strip()) for cell in separators)
            ):
                table_start = i
                break
        sentence = None
        if table_start is not None:
            for line in reversed(lines[start + 1 : table_start]):
                if line.strip():
                    sentence = line.strip()
                    break
        rows = []
        if table_start is not None:
            for i in range(table_start + 2, end):
                if not lines[i].strip().startswith("|"):
                    break
                rows.append((i + 1, _split_table_cells(lines[i])))
        sections.append((name, sentence, rows))
    return sections


def _split_table_cells(line):
    """Split one Markdown table line, preserving escaped and code-span pipes."""
    text = line.strip()
    if text.startswith("|"):
        text = text[1:]
    if text.endswith("|") and not text.endswith("\\|"):
        text = text[:-1]
    cells = []
    current = []
    delimiter = None
    i = 0
    while i < len(text):
        if text[i] == "\\" and i + 1 < len(text) and text[i + 1] == "|":
            current.extend(("\\", "|"))
            i += 2
            continue
        if text[i] == "`":
            end = i
            while end < len(text) and text[end] == "`":
                end += 1
            run = end - i
            current.append(text[i:end])
            if delimiter is None:
                delimiter = run
            elif delimiter == run:
                delimiter = None
            i = end
            continue
        if text[i] == "|" and delimiter is None:
            cells.append("".join(current).strip())
            current = []
        else:
            current.append(text[i])
        i += 1
    cells.append("".join(current).strip())
    return cells


def _code_spans(text):
    """Return ``(start, end, content)`` for matched single or multi-backtick spans."""
    spans = []
    i = 0
    while i < len(text):
        if text[i] != "`":
            i += 1
            continue
        end = i
        while end < len(text) and text[end] == "`":
            end += 1
        delimiter = text[i:end]
        close = text.find(delimiter, end)
        if close < 0:
            i = end
            continue
        spans.append((i, close + len(delimiter), text[end:close]))
        i = close + len(delimiter)
    return spans


_LOCATION_ATOM = re.compile(r"^/?[\w.*\-]+(?:/[\w.*\-]+)*/?$")


def _is_location_span(span, extensions):
    """True iff a test could be asked to resolve the span: an UNRESOLVED marker, a
    same-file shorthand (":12", "::Name", "#frag"), or a leading atom that is
    path-shaped and either crosses a directory or ends in an extension some tracked
    file has. Malformed locations (line specs, elisions, bare basenames, absolute
    paths, whitespace, shorthand) classify True so validation rejects them by name;
    expressions ("re.ASCII", "^[ab]/", "/i", "title.lower()", "{20,}") classify False."""
    if span.startswith("UNRESOLVED:") or re.match(r"^(?::\d|::|#)", span):
        return True
    leader = re.split(r"::|#", span, maxsplit=1)[0]
    atom = re.split(r"[\s:]", leader, maxsplit=1)[0]
    if not atom or not _LOCATION_ATOM.match(atom):
        return False
    body = atom.lstrip("/")
    return "/" in body or Path(body).suffix in extensions


TITLE_KIND = "node_title"

DEFINITION_PATTERNS = (
    ("python", r"^[ \t]*(?:async[ \t]+)?def[ \t]+{S}[ \t]*\("),
    ("python", r"^[ \t]*class[ \t]+{S}(?=[ \t]*[(:])"),
    ("python_constant", r"^{S}[ \t]*(?::[^=\n]+)?[ \t]*=(?!=)"),
    (
        "js",
        r"^[ \t]*(?:export[ \t]+)?(?:default[ \t]+)?(?:async[ \t]+)?function[ \t]*\*?[ \t]+{S}[ \t]*\(",
    ),
    ("js", r"^[ \t]*(?:export[ \t]+)?(?:default[ \t]+)?class[ \t]+{S}(?=[ \t{])"),
    ("js", r"^[ \t]*(?:export[ \t]+)?(?:const|let|var)[ \t]+{S}[ \t]*=(?!=)"),
    ("js", r"""(?:^|[{,])[ \t]*(?:{S}|"{S}"|'{S}')[ \t]*:"""),
    ("yaml", r"""^[ \t]*-[ \t]+id:[ \t]*(?:{S}|"{S}"|'{S}')[ \t]*(?:#.*)?$"""),
    (TITLE_KIND, r"^[ \t]*(?:test|describe|it)[ \t]*\([ \t]*{Q}{T}{Q}[ \t]*,"),
)
HEADING_PATTERN = r"^#{1,6}\s+{H}\s*$"
HEADING_SUFFIX = ".md"

DEFINITION_KINDS_BY_SUFFIX = {
    ".py": {"python", "python_constant"},
    ".js": {"js"},
    ".mjs": {"js"},
    ".yaml": {"yaml"},
    ".yml": {"yaml"},
}


def _tracked_files():
    global _TRACKED_FILES
    if _TRACKED_FILES is None:
        output = subprocess.run(
            ["git", "ls-files", "-z"], cwd=REPO, capture_output=True, check=True
        ).stdout.decode()
        _TRACKED_FILES = tuple(path for path in output.split("\0") if path)
    return _TRACKED_FILES


def _parse_citation(span):
    """Parse a path, symbol chain, quoted title, or heading citation."""
    if span.startswith("UNRESOLVED:"):
        return "unresolved", span, (), None
    separator = span.find("::")
    if separator >= 0 and span[separator + 2 :].startswith('"'):
        path = span[:separator]
        suffix = span[separator + 2 :]
        try:
            title = json.loads(suffix)
        except json.JSONDecodeError:
            return "invalid", path, (), None
        if not isinstance(title, str):
            return "invalid", path, (), None
        return "title", path, (), title
    if "#" in span:
        hash_at = span.find("#")
        before = span[:hash_at]
        if "::" in before:
            return "invalid", span, (), None
        return "heading", before, (), span[hash_at + 1 :]
    if separator >= 0:
        path = span[:separator]
        suffix = span[separator + 2 :]
        return "symbol", path, tuple(suffix.split("::")), None
    return "file", span, (), None


def _path_reason(path, tracked_files):
    if not path:
        return "same-file shorthand forbidden"
    if path.startswith("/"):
        return "absolute path forbidden"
    if re.search(r":\d", path):
        return "line-number citation forbidden"
    if "..." in path:
        return "elided path forbidden"
    if any(char.isspace() for char in path):
        return "whitespace in location"
    components = path.split("/")
    if any(component in (".", "..") for component in components):
        return "dot path component forbidden"
    if any(not component for component in components[:-1]):
        return "empty path component forbidden"
    if path.endswith("/"):
        prefix = path
        if not any(candidate.startswith(prefix) for candidate in tracked_files):
            return "untracked path"
        return None
    if any(char in path for char in "*?["):
        if not any(fnmatch.fnmatchcase(candidate, path) for candidate in tracked_files):
            return "glob matches no tracked files"
        return None
    if path in tracked_files:
        return None
    return "untracked path"


def _unfenced_lines(text):
    """Blank fenced lines, including delimiters, without changing line indices."""
    lines = text.splitlines()
    fence = ""
    for index, line in enumerate(lines):
        if fence:
            if re.fullmatch(
                r"[ \t]*" + fence[0] + "{" + str(len(fence)) + r",}[ \t]*", line
            ):
                fence = ""
            lines[index] = ""
            continue
        match = re.match(r"[ \t]*(`{3,}|~{3,})(.*)", line)
        if match:
            run, info = match.groups()
            if run[0] == "`" and "`" in info:
                continue
            fence = run
            lines[index] = ""
    return lines


def _kind_line_visible(text, kind, symbol):
    """True when an unfenced line of text is shaped like a kind definition of symbol."""
    patterns = [
        pattern.replace("{S}", re.escape(symbol))
        for pattern_kind, pattern in DEFINITION_PATTERNS
        if pattern_kind == kind
    ]
    lines = _unfenced_lines(text)
    return any(re.search(pattern, line) for pattern in patterns for line in lines)


def _definition_reason(path, symbols, tracked_files, *, witness=None):
    path_reason = _path_reason(path, tracked_files)
    if path_reason:
        return path_reason
    if path not in tracked_files:
        return "symbols require an exact tracked file"
    if not symbols:
        return None
    if any(
        not re.fullmatch(r"[A-Za-z_$][A-Za-z0-9_$-]*", segment) for segment in symbols
    ):
        return "invalid symbol chain"
    suffix = Path(path).suffix
    kinds = DEFINITION_KINDS_BY_SUFFIX.get(suffix, set())
    if not kinds:
        return f"no definition patterns for {suffix or 'this file type'}"
    lines = _unfenced_lines((REPO / path).read_text(encoding="utf-8"))
    cursor = 0
    for segment in symbols:
        S = re.escape(segment)
        found = None
        for line_number in range(cursor, len(lines)):
            for pattern_index, (kind, pattern) in enumerate(DEFINITION_PATTERNS):
                if kind not in kinds:
                    continue
                if kind == "python_constant" and not re.fullmatch(
                    r"[A-Z_][A-Z0-9_]*", segment
                ):
                    continue
                regex = re.compile(pattern.replace("{S}", S))
                if regex.search(lines[line_number]):
                    found = line_number
                    if witness is not None:
                        witness.append((kind, pattern_index))
                    break
            if found is not None:
                break
        if found is None:
            return f"unresolved symbol {segment!r} after line {cursor}"
        cursor = found
    return None


def _title_reason(path, title, tracked_files, *, witness=None):
    path_reason = _path_reason(path, tracked_files)
    if path_reason:
        return path_reason
    if path not in tracked_files:
        return "titles require an exact tracked file"
    if Path(path).suffix not in {".js", ".mjs"}:
        return "titles require a JS test source"
    text = _unfenced_lines((REPO / path).read_text(encoding="utf-8"))
    title_entry = next(
        (
            (index, pattern)
            for index, (kind, pattern) in enumerate(DEFINITION_PATTERNS)
            if kind == TITLE_KIND
        ),
        None,
    )
    if title_entry is None:
        return "title not found"
    pattern_index, title_pattern = title_entry
    for delimiter in ("'", '"', "`"):
        escaped_title = title.replace("\\", "\\\\").replace(delimiter, "\\" + delimiter)
        regex = re.compile(
            title_pattern.replace("{Q}", re.escape(delimiter)).replace(
                "{T}", re.escape(escaped_title)
            )
        )
        if delimiter == "`" and "${" in title:
            continue
        for line in text:
            if regex.search(line):
                if witness is not None:
                    witness.append((TITLE_KIND, pattern_index))
                return None
    return "title not found"


def _citation_reason(span, tracked_files, *, witness=None):
    if span.startswith("UNRESOLVED:"):
        return "UNRESOLVED requires orchestrator ruling"
    kind, path, symbols, anchor = _parse_citation(span)
    if kind == "invalid":
        return "invalid citation syntax"
    unquoted = (
        path
        if kind in {"file", "heading", "title"}
        else path + "::" + "::".join(symbols)
    )
    if re.search(r":\d", unquoted):
        return "line-number citation forbidden"
    if "..." in unquoted:
        return "elided path forbidden"
    if kind == "heading" and not anchor:
        return "empty heading forbidden"
    if kind == "heading":
        path_reason = _path_reason(path, tracked_files)
        if path_reason:
            return path_reason
        if path not in tracked_files:
            return "heading requires an exact tracked file"
        if Path(path).suffix != HEADING_SUFFIX:
            return "heading requires a Markdown file"
        heading_regex = re.compile(HEADING_PATTERN.replace("{H}", re.escape(anchor)))
        for line in _unfenced_lines((REPO / path).read_text(encoding="utf-8")):
            if heading_regex.search(line):
                if witness is not None:
                    witness.append(("heading", None))
                return None
        return "heading not found"
    if kind == "title":
        return _title_reason(path, anchor, tracked_files, witness=witness)
    if kind == "symbol":
        if any(char.isspace() for segment in symbols for char in segment):
            return "whitespace in location"
        return _definition_reason(path, symbols, tracked_files, witness=witness)
    return _path_reason(path, tracked_files)


def _looks_like_location(span, extensions, top_dirs):
    """A path-shaped token that could only be a repository location: it ends in an
    extension some tracked file has, or its first segment is a tracked top-level
    directory. A slash-joined word pair in prose ("JS/Python", "read/write") is
    neither and stays prose."""
    if span.startswith(("http://", "https://", "/")):
        return False
    atom = re.split(r"::|#|\s", span, maxsplit=1)[0]
    return bool(_LOCATION_ATOM.match(atom)) and (
        Path(atom).suffix in extensions
        or ("/" in atom and atom.split("/", 1)[0] in top_dirs)
    )


class TestDocsRegistry(unittest.TestCase):
    def test_generated_config_receipt_matches_resolver_fixtures(self):
        """The generated receipts stay pinned to the resolver's empty-env fixtures."""
        registry = contract_generator.load_registry(str(REPO))["knobs"]
        resolver = contract_generator._load_resolver(str(REPO))
        path = REPO / "skills" / "code-gauntlet" / "SKILL.md"
        text = path.read_text(encoding="utf-8")
        open_marker, close_marker = contract_generator.identity_marker_lines(
            "config_receipt", "skills/code-gauntlet/SKILL.md"
        )
        start = text.index(open_marker) + len(open_marker)
        end = text.index(close_marker, start)
        body = text[start:end]
        for mode, label in (("interactive", "Interactive"), ("headless", "Headless")):
            match = re.search(
                rf"\*\*{label} receipt:\*\*\n\n```json\n(.*?)\n```",
                body,
                re.DOTALL,
            )
            self.assertIsNotNone(match, f"missing {mode} config receipt fence")
            actual = json.loads(match.group(1))
            expected = resolver.resolve(mode, {}, None, "pr", registry=registry)[
                "configEcho"
            ]
            self.assertEqual(
                actual, expected, f"{mode} receipt drifted from KNOB_REGISTRY"
            )
        interactive = json.loads(
            re.search(
                r"\*\*Interactive receipt:\*\*\n\n```json\n(.*?)\n```",
                body,
                re.DOTALL,
            ).group(1)
        )
        self.assertEqual(
            list(interactive), ["model_tier", "pr_comment_cap", "delivery_tier"]
        )

    def test_config_receipt_examples_follow_registry_order(self):
        """Every skill example must render knobs in the source registry's mode order."""
        args_source = (REPO / "workflows" / "src" / "args.js").read_text(
            encoding="utf-8"
        )
        descriptors = re.findall(
            r"\{ key: '([^']+)', modes: \[([^\]]+)\]",
            args_source,
        )
        self.assertTrue(descriptors, "KNOB_REGISTRY descriptors not found")
        registry = contract_generator.load_registry(str(REPO))["knobs"]
        expected = {
            mode: [
                row["key"]
                for row in registry
                if mode in row["modes"] and row.get("derivedFrom") is None
            ]
            for mode in ("interactive", "headless")
        }
        skill = (REPO / "skills/code-gauntlet/SKILL.md").read_text(encoding="utf-8")
        open_marker, close_marker = contract_generator.identity_marker_lines(
            "config_receipt", "skills/code-gauntlet/SKILL.md"
        )
        start = skill.index(open_marker)
        generated = skill[start : skill.index(close_marker, start)]
        for mode, label in (("interactive", "Interactive"), ("headless", "Headless")):
            match = re.search(
                rf"\*\*{label} block:\*\*\n\n```text\n(.*?)\n```",
                generated,
                re.DOTALL,
            )
            self.assertIsNotNone(match)
            keys = re.findall(r"^  ([A-Za-z_]+)=", match.group(1), re.MULTILINE)
            self.assertEqual(keys[:-2], expected[mode])

        report = (REPO / "skills/code-gauntlet/references/report-format.md").read_text(
            encoding="utf-8"
        )
        report_match = re.search(
            r"^Resolved config:\n((?:^  [^\n]+\n?)+)", report, re.MULTILINE
        )
        self.assertIsNotNone(report_match)
        report_keys = re.findall(
            r"^  ([A-Za-z_]+)=", report_match.group(1), re.MULTILINE
        )
        full_expected = [
            row["key"] for row in registry if "interactive" in row["modes"]
        ]
        self.assertEqual(report_keys[:-2], full_expected)

    def test_every_tracked_docs_file_is_allowlisted(self):
        offenders = []
        for path in tracked("docs"):
            if path.startswith(DOCS_ALLOWED_SUBTREES):
                if not path.endswith(".md"):
                    offenders.append(f"{path} (non-markdown inside a docs subtree)")
            elif path not in DOCS_ALLOW:
                offenders.append(path)
        self.assertEqual(
            offenders,
            [],
            f"tracked under docs/ but not in the registry allowlist: {offenders}. {POLICY}",
        )

    def test_root_markdown_is_allowlisted(self):
        offenders = [
            path
            for path in tracked("*.md")
            if "/" not in path and path not in ROOT_MD_ALLOW
        ]
        self.assertEqual(
            offenders,
            [],
            f"tracked root-level markdown outside the allowlist: {offenders}. "
            f"Review-run outputs stay untracked (gitignored). {POLICY}",
        )

    def test_duplication_register_row_count_sentence_matches_table(self):
        """The sentence above the "Individually classified rows" table (e.g.
        "Forty rows: 30 `intentional-and-documented`, 10
        `intentional-but-undocumented`.") must state the table's real total and
        the real count for every classification the table actually contains — a
        row added without updating the sentence is exactly the kind of drift this
        guards against.

        Tolerant of the classification set: only the counts the sentence names are
        checked against the table, and the table's classifications must each be
        named — an unnamed classification, not a fixed vocabulary, is the failure.
        """
        sections = _register_sections(
            (REPO / "docs" / "duplication-register.md").read_text(encoding="utf-8")
        )
        sentence, table_rows = next(
            (sentence, rows)
            for name, sentence, rows in sections
            if name == "Individually classified rows"
        )
        self.assertIsNotNone(sentence, "no row-count sentence found above the table")
        self.assertTrue(table_rows, "no data rows found in the table")

        table_counts = {}
        for _, cells in table_rows:
            classification = cells[1]
            table_counts[classification] = table_counts.get(classification, 0) + 1
        table_total = len(table_rows)

        word_match = re.match(r"^([A-Za-z-]+) rows:", sentence)
        self.assertIsNotNone(
            word_match, f"sentence does not start with '<Word> rows:': {sentence!r}"
        )
        word = word_match.group(1).lower()
        self.assertIn(
            word,
            _WORD_TO_NUMBER,
            f"{word!r} is not a recognized spelled-out number 30-60: {sentence!r}",
        )
        sentence_total = _WORD_TO_NUMBER[word]

        sentence_counts = {
            classification: int(count)
            for count, classification in re.findall(r"(\d+) `([\w-]+)`", sentence)
        }

        self.assertEqual(
            sentence_total,
            table_total,
            f"sentence claims {sentence_total} rows ({word!r}) but the table has "
            f"{table_total}: {sentence!r}",
        )
        self.assertEqual(
            set(table_counts),
            set(sentence_counts) & set(table_counts),
            "sentence is missing a classification present in the table: "
            f"table has {sorted(table_counts)}, sentence names {sorted(sentence_counts)}",
        )
        for classification, count in table_counts.items():
            self.assertEqual(
                sentence_counts.get(classification),
                count,
                f"sentence says {sentence_counts.get(classification)!r} "
                f"`{classification}` rows but the table has {count}",
            )

    def test_duplication_register_citations_resolve(self):
        """Every location citation in the register is a durable tracked anchor."""
        register_path = "docs/duplication-register.md"
        text = (REPO / register_path).read_text(encoding="utf-8")
        tracked_files = _tracked_files()
        extensions = {Path(path).suffix for path in tracked_files if Path(path).suffix}
        top_dirs = {path.split("/", 1)[0] for path in tracked_files if "/" in path}
        sections = _register_sections(text)
        section_map = {name: (sentence, rows) for name, sentence, rows in sections}
        errors = []

        def add_error(line, span, reason):
            errors.append(f"{register_path}:{line}: {span!r}: {reason}")

        for name in ("Individually classified rows", "Grouped patterns"):
            if name not in section_map:
                add_error(0, name, "required table missing")
            elif not section_map[name][1]:
                add_error(0, name, "required table empty")

        classification_cases = (
            ("re.ASCII", False),
            ("re.IGNORECASE", False),
            ("/i", False),
            ("/", False),
            (".venv", False),
            ("math.trunc", False),
            ("_WS_TRIM_RE.sub", False),
            ("WS_TRIM_RE.replace", False),
            ("script_io.write_result", False),
            ("title.lower()", False),
            ("^[ab]/", False),
            ("{20,}", False),
            ("filter_findings.py", True),
            ("/tmp/file.py", True),
            ("::Name", True),
            ("#frag", True),
            (":12", True),
            ("scripts/a spaced file.py", True),
            ("tests/.../expected.json", True),
            ("UNRESOLVED:foo.py", True),
        )
        for span, expected in classification_cases:
            actual = _is_location_span(span, extensions)
            if actual != expected:
                add_error(
                    0, span, f"classifier returned {actual!r}, expected {expected!r}"
                )

        location_helper_cases = (
            ("stages.js", True),
            ("workflows/src/stages.js", True),
            ("workflows/", True),
            ("agents/*.md", True),
            ("http://x/y", False),
            ("/i", False),
            ("title.lower()", False),
            ("JS/Python", False),
            ("read/write", False),
            ("tests", False),
        )
        for span, expected in location_helper_cases:
            actual = _looks_like_location(span, extensions, top_dirs)
            if actual != expected:
                add_error(
                    0,
                    span,
                    f"location helper returned {actual!r}, expected {expected!r}",
                )

        helper_citations = (
            (
                "tests/test_docs_registry.py::TestDocsRegistry::test_duplication_register_row_count_sentence_matches_table",
                None,
            ),
            (".pre-commit-config.yaml::agent-instruction-layout", None),
            ("workflows/src/stages.js::dispatchVerifySlice", None),
            ("workflows/src/registry.js::SEVERITY_EMOJI::low", None),
            (
                "workflows/src/stages.js::normalizeFieldNames",
                "unresolved symbol 'normalizeFieldNames' after line 0",
            ),
            ("workflows/", None),
            ("agents/*.md", None),
            ("workflows/AGENTS.md#The verify boundary", None),
            (
                'workflows/test/filter_unit.test.js::"#244/coerce-table: pyIntOrNull/lineBucket shared cross-twin table"',
                None,
            ),
            (
                'workflows/test/args.test.js::"normalizeArgs treats a provided challengeCap:0 as a real value, not an absent one to default over"',
                None,
            ),
            (
                'workflows/test/filter_unit.test.js::"#211: template filepath with brace markers matches (the {...} alternative)"',
                None,
            ),
        )
        for span, expected_reason in helper_citations:
            actual_reason = _citation_reason(span, tracked_files)
            if actual_reason != expected_reason:
                add_error(
                    0,
                    span,
                    f"helper resolved as {actual_reason!r}, expected {expected_reason!r}",
                )

        ordered_helper_citations = (
            (
                "tests/test_docs_registry.py::TestDocsRegistry::_register_sections",
                "unresolved symbol '_register_sections'",
            ),
        )
        for span, expected_prefix in ordered_helper_citations:
            actual_reason = _citation_reason(span, tracked_files)
            if not actual_reason or not actual_reason.startswith(expected_prefix):
                add_error(
                    0,
                    span,
                    f"ordered helper resolved as {actual_reason!r}, expected prefix {expected_prefix!r}",
                )

        malformed_citations = (
            ("scripts/verify_findings.py:12", "line-number citation forbidden"),
            ("tests/.../test_docs_registry.py", "elided path forbidden"),
            ("/tmp/file.py", "absolute path forbidden"),
            ("::Name", "same-file shorthand forbidden"),
            ("#frag", "same-file shorthand forbidden"),
            ("scripts/missing.py", "untracked path"),
            ("scripts/a spaced file.py", "whitespace in location"),
            ("filter_findings.py", "untracked path"),
            ("UNRESOLVED:foo.py", "UNRESOLVED requires orchestrator ruling"),
            ("scripts/./verify_findings.py", "dot path component forbidden"),
            ("scripts//verify_findings.py", "empty path component forbidden"),
            ("scripts/*.nothing", "glob matches no tracked files"),
            ("nope/", "untracked path"),
            ("scripts/*.py::x", "symbols require an exact tracked file"),
            ("scripts/verify_findings.py::bad.symbol", "invalid symbol chain"),
            ("scripts/verify_findings.py::bad symbol", "whitespace in location"),
            ("README.md::Foo", "no definition patterns for .md"),
            (
                "scripts/verify_findings.py::no_such_definition",
                "unresolved symbol 'no_such_definition' after line 0",
            ),
            ('workflows/test/*.test.js::"x"', "titles require an exact tracked file"),
            ('scripts/verify_findings.py::"x"', "titles require a JS test source"),
            ('workflows/test/parity.test.js::"no such title"', "title not found"),
            ('scripts/verify_findings.py::"unterminated', "invalid citation syntax"),
            ("a::b#c", "invalid citation syntax"),
            ("CLAUDE.md#", "empty heading forbidden"),
            ("docs/*.md#X", "heading requires an exact tracked file"),
            ("scripts/verify_findings.py#X", "heading requires a Markdown file"),
            ("CLAUDE.md#Nonexistent Heading", "heading not found"),
        )
        for span, expected_reason in malformed_citations:
            actual_reason = _citation_reason(span, tracked_files)
            if actual_reason != expected_reason:
                add_error(
                    0,
                    span,
                    f"validator returned {actual_reason!r}, expected {expected_reason!r}",
                )

        table_citation_keys = set()
        for _, _, rows in sections:
            for line, cells in rows:
                for cell in cells:
                    for _, _, span in _code_spans(cell):
                        if not _is_location_span(span, extensions):
                            continue
                        table_citation_keys.add((line, span))
                        reason = _citation_reason(span, tracked_files)
                        if reason:
                            add_error(line, span, reason)

        residual_location_spans = set()
        for start, _, span in _code_spans(text):
            line = text.count("\n", 0, start) + 1
            if (line, span) in table_citation_keys:
                continue
            if span == "::":
                continue
            if _looks_like_location(span, extensions, top_dirs):
                residual_location_spans.add(span)
            if _is_location_span(span, extensions):
                reason = _citation_reason(span, tracked_files)
                if reason:
                    add_error(line, span, reason)
            elif _looks_like_location(span, extensions, top_dirs):
                add_error(line, span, "location-shaped span not classified")

        for line_number, line_text in enumerate(_unfenced_lines(text), 1):
            without_code = re.sub(r"`+[^`]*`+", "", line_text)
            without_links = re.sub(r"\[[^\]]*\]\([^)]*\)", "", without_code)
            for token in re.findall(r"(?<!\w)\S+", without_links):
                token = token.strip(".,;:()[]{}<>\"'")
                if _looks_like_location(token, extensions, top_dirs):
                    add_error(line_number, token, "uncited location in plain text")

        for span in {
            "tests/fixtures/parity/",
            "agents/",
            "workflows/src/stages.js",
            "tests/test_parity_fixtures.py",
            "workflows/test/tools/record_parity.py",
        }:
            if span not in residual_location_spans:
                add_error(0, span, "residual scan missed expected location-shaped span")

        for line_number, line_text in enumerate(text.splitlines(), 1):
            for match in re.finditer(r"\]\(([^)]+)\)", line_text):
                target = match.group(1)
                if target.startswith(("http://", "https://")):
                    continue
                if not (REPO / "docs" / target).is_file():
                    add_error(
                        line_number,
                        target,
                        "markdown link target not found relative to docs/",
                    )

        self.assertFalse(errors, "\n".join(errors))

    def test_duplication_resolver_matching_clauses(self):
        tracked_files = _tracked_files()
        base = "tests/fixtures/citation_resolver/"
        p = base + "definitions.py"
        j = base + "definitions.js"
        m = base + "definitions.mjs"
        y = base + "definitions.yaml"
        z = base + "definitions.yml"
        t = base + "titles.js"
        h = base + "headings.md"
        u = base + "headings_unclosed.md"
        fixture_paths = sorted(path for path in tracked_files if path.startswith(base))
        definition_paths = [
            path
            for path in fixture_paths
            if Path(path).suffix in DEFINITION_KINDS_BY_SUFFIX
        ]
        # These tracked files are resolver input, never executed as source.
        # Rows for the suffix map's values are derived from the map itself.
        # Each definition fixture declares the kinds its suffix grants on line one.
        # Each near-miss row flips when the clause it names is removed.
        # Witness assertions tie every pattern and suffix to a positive row.
        # Near misses isolate constraints.
        # Positive rows pin accepted forms.
        # Cursor diagnostics use exact offsets in this fixed fixture tree.
        # Fence rows pin masking and the boundaries that restore matching.
        citations = (
            (p + "::py_ok", None),  # def: accepts a definition
            (p + "::py_async", None),  # def: accepts async
            (
                p + "::py_inline",
                "unresolved symbol 'py_inline' after line 0",
            ),  # def: anchored at line start
            (
                p + "::py_prefix",
                "unresolved symbol 'py_prefix' after line 0",
            ),  # def: requires the opening parenthesis
            (
                p + "::joined",
                "unresolved symbol 'joined' after line 0",
            ),  # def: requires whitespace before the name
            (
                p + "::async_joined",
                "unresolved symbol 'async_joined' after line 0",
            ),  # def: requires nonempty token separation
            (
                p + "::async_punct",
                "unresolved symbol 'async_punct' after line 0",
            ),  # def: accepts only whitespace at gap 2
            (
                p + "::y_ok",
                "unresolved symbol 'y_ok' after line 0",
            ),  # def: whitespace class at gap 3
            (p + "::PyOk", None),  # class: accepts a definition
            (
                p + "::PyInline",
                "unresolved symbol 'PyInline' after line 0",
            ),  # class: anchored at line start
            (
                p + "::PyPrefix",
                "unresolved symbol 'PyPrefix' after line 0",
            ),  # class: requires a name boundary
            (p + "::WithBase", None),  # class: accepts a base-class parenthesis
            (p + "::tab_def", None),  # def: accepts tabs in every gap
            (p + "::TabClass", None),  # class: accepts tabs in every gap
            (p + "::TAB_CONST", None),  # constant: accepts tabs around the assignment
            (
                p + "::Joined",
                "unresolved symbol 'Joined' after line 0",
            ),  # class: requires whitespace before the name
            (
                p + "::yOk",
                "unresolved symbol 'yOk' after line 0",
            ),  # class: whitespace class at gap 2
            (p + "::UPPER_OK", None),  # constant: accepts uppercase assignment
            (p + "::ANNOTATED_OK", None),  # constant: accepts annotated assignment
            (
                p + "::UPPER",
                "unresolved symbol 'UPPER' after line 0",
            ),  # constant: requires a name boundary
            (
                p + "::ANNOTATION",
                "unresolved symbol 'ANNOTATION' after line 0",
            ),  # constant: annotation cannot consume equals
            (
                p + "::COMPARE",
                "unresolved symbol 'COMPARE' after line 0",
            ),  # constant: rejects equality comparisons
            (
                p + "::lower",
                "unresolved symbol 'lower' after line 0",
            ),  # constant: requires uppercase names
            (
                p + "::Mixed",
                "unresolved symbol 'Mixed' after line 0",
            ),  # constant: checks the whole name
            (
                p + "::INDENTED",
                "unresolved symbol 'INDENTED' after line 0",
            ),  # constant: requires column zero
            (
                p + "::EMPTY",
                "unresolved symbol 'EMPTY' after line 0",
            ),  # constant: annotation body must be nonempty
            (j + "::js_ok", None),  # function: accepts a definition
            (
                j + "::js_star",
                None,
            ),  # function: accepts export default async and generator
            (
                j + "::js_inline",
                "unresolved symbol 'js_inline' after line 0",
            ),  # function: anchored at line start
            (
                j + "::js_prefix",
                "unresolved symbol 'js_prefix' after line 0",
            ),  # function: requires the opening parenthesis
            (
                j + "::Joined",
                "unresolved symbol 'Joined' after line 0",
            ),  # function: requires whitespace before the name
            (
                j + "::export_joined",
                "unresolved symbol 'export_joined' after line 0",
            ),  # function: requires nonempty token separation
            (
                j + "::default_joined",
                "unresolved symbol 'default_joined' after line 0",
            ),  # function: requires nonempty token separation
            (
                j + "::async_joined",
                "unresolved symbol 'async_joined' after line 0",
            ),  # function: requires nonempty token separation
            (
                j + "::export_punct",
                "unresolved symbol 'export_punct' after line 0",
            ),  # function: accepts only whitespace at gap 2
            (
                j + "::default_punct",
                "unresolved symbol 'default_punct' after line 0",
            ),  # function: accepts only whitespace at gap 3
            (
                j + "::async_punct",
                "unresolved symbol 'async_punct' after line 0",
            ),  # function: accepts only whitespace at gap 4
            (
                j + "::fn_punct",
                "unresolved symbol 'fn_punct' after line 0",
            ),  # function: accepts only whitespace at gap 5
            (
                j + "::s_ok",
                "unresolved symbol 's_ok' after line 0",
            ),  # function: whitespace class at gap 6
            (j + "::JsOk", None),  # JS class: accepts a definition
            (j + "::JsExport", None),  # JS class: accepts export and default
            (
                j + "::JsInline",
                "unresolved symbol 'JsInline' after line 0",
            ),  # JS class: anchored at line start
            (
                j + "::JsPrefix",
                "unresolved symbol 'JsPrefix' after line 0",
            ),  # JS class: requires a name boundary
            (
                j + "::JsBrace",
                None,
            ),  # JS class: accepts a brace directly after the name
            (j + "::tab_fn", None),  # function: accepts tabs in every gap
            (j + "::TabClassJs", None),  # JS class: accepts tabs in every gap
            (j + "::TAB_VAR", None),  # variable: accepts tabs in every gap
            (j + "::tab_key", None),  # key: accepts tabs in every gap
            (j + "::DUAL", None),  # pattern order: the first matching pattern wins
            (
                j + "::JoinedClass",
                "unresolved symbol 'JoinedClass' after line 0",
            ),  # JS class: requires whitespace before the name
            (
                j + "::ExportJoined",
                "unresolved symbol 'ExportJoined' after line 0",
            ),  # JS class: requires nonempty token separation
            (
                j + "::DefaultJoined",
                "unresolved symbol 'DefaultJoined' after line 0",
            ),  # JS class: requires nonempty token separation
            (
                j + "::ExportPunct",
                "unresolved symbol 'ExportPunct' after line 0",
            ),  # JS class: accepts only whitespace at gap 2
            (
                j + "::DefaultPunct",
                "unresolved symbol 'DefaultPunct' after line 0",
            ),  # JS class: accepts only whitespace at gap 3
            (
                j + "::sOk",
                "unresolved symbol 'sOk' after line 0",
            ),  # JS class: whitespace class at gap 4
            (j + "::VAR_OK", None),  # variable: accepts const
            (j + "::LET_OK", None),  # variable: accepts let
            (j + "::OLD_OK", None),  # variable: accepts var
            (j + "::EXP_OK", None),  # variable: accepts export
            (
                j + "::VAR_INLINE",
                "unresolved symbol 'VAR_INLINE' after line 0",
            ),  # variable: anchored at line start
            (
                j + "::VAR",
                "unresolved symbol 'VAR' after line 0",
            ),  # variable: requires a name boundary
            (
                j + "::var_fake",
                "unresolved symbol 'var_fake' after line 0",
            ),  # variable: requires a declaration keyword
            (
                j + "::EQUALITY",
                "unresolved symbol 'EQUALITY' after line 0",
            ),  # variable: rejects equality comparisons
            (
                j + "::JoinedVar",
                "unresolved symbol 'JoinedVar' after line 0",
            ),  # variable: requires whitespace before the name
            (
                j + "::EXPORT_JOINED",
                "unresolved symbol 'EXPORT_JOINED' after line 0",
            ),  # variable: requires nonempty token separation
            (
                j + "::EXPORT_PUNCT",
                "unresolved symbol 'EXPORT_PUNCT' after line 0",
            ),  # variable: accepts only whitespace at gap 2
            (
                j + "::AR_OK",
                "unresolved symbol 'AR_OK' after line 0",
            ),  # variable: whitespace class at gap 3
            (j + "::key_ok", None),  # key: accepts an unquoted key
            (j + "::double_key", None),  # key: accepts double quotes
            (j + "::single_key", None),  # key: accepts single quotes
            (j + "::start_key", None),  # key: accepts start of line
            (
                j + "::key_inline",
                "unresolved symbol 'key_inline' after line 0",
            ),  # key: requires line start or a delimiter
            (
                j + "::key_call",
                "unresolved symbol 'key_call' after line 0",
            ),  # key: requires a colon
            (
                j + "::key",
                "unresolved symbol 'key' after line 0",
            ),  # key: requires a name boundary
            (
                j + "::ey_ok",
                "unresolved symbol 'ey_ok' after line 0",
            ),  # key: whitespace class at gap 1
            (y + "::yaml_ok", None),  # YAML: accepts a list id
            (y + "::double_id", None),  # YAML: accepts double quotes
            (y + "::single_id", None),  # YAML: accepts single quotes and a comment
            (y + "::wide_dash", None),  # YAML: accepts multiple spaces after dash
            (
                y + "::no_dash",
                "unresolved symbol 'no_dash' after line 0",
            ),  # YAML: requires a list dash
            (
                y + "::yaml_prefix",
                "unresolved symbol 'yaml_prefix' after line 0",
            ),  # YAML: requires a complete id
            (
                y + "::wrong_key",
                "unresolved symbol 'wrong_key' after line 0",
            ),  # YAML: requires the id key
            (
                y + "::spaced_colon",
                "unresolved symbol 'spaced_colon' after line 0",
            ),  # YAML: requires the colon directly after id
            (y + "::tab_id", None),  # YAML: accepts tabs in every gap
            (
                y + "::inline_id",
                "unresolved symbol 'inline_id' after line 0",
            ),  # YAML: anchored at line start
            (
                y + "::joined_dash",
                "unresolved symbol 'joined_dash' after line 0",
            ),  # YAML: requires nonempty token separation
            (
                y + "::punct_dash",
                "unresolved symbol 'punct_dash' after line 0",
            ),  # YAML: accepts only whitespace at gap 2
            (
                y + "::aml_ok",
                "unresolved symbol 'aml_ok' after line 0",
            ),  # YAML: whitespace class at gap 3
            (t + '::"single title"', None),  # title call: accepts test
            (
                t + '::"wrong callee"',
                "title not found",
            ),  # title call: requires a supported callee
            (
                t + '::"long title"',
                "title not found",
            ),  # title call: requires the closing quote
            (t + '::"joined title"', "title not found"),  # title call: requires a comma
            (
                t + '::"inline title"',
                "title not found",
            ),  # title call: anchored at line start
            (
                t + '::"callee gap"',
                "title not found",
            ),  # title call: accepts only whitespace at gap 2
            (
                t + '::"argument gap"',
                "title not found",
            ),  # title call: accepts only whitespace at gap 3
            (t + '::"double title"', None),  # title: accepts double quotes and describe
            (t + '::"plain template"', None),  # title: accepts plain backticks and it
            (t + '::"it\'s escaped"', None),  # title: escapes the delimiter
            (t + '::"path\\\\part"', None),  # title: escapes backslashes
            (t + '::"literal (dot.)"', None),  # title: escapes regex syntax
            (t + '::"say \\"yes\\""', None),  # title: escapes double quotes
            (t + '::"tick `value`"', None),  # title: escapes backticks
            (
                t + '::"mismatch"',
                "title not found",
            ),  # title: requires matching delimiters
            (t + '::"tab title"', None),  # title: accepts tabs in every gap
            (
                t + '::"literal ${name}"',
                None,
            ),  # title: accepts interpolation text in quotes
            (
                t + '::"dynamic ${name}"',
                "title not found",
            ),  # title: rejects dynamic templates
            (m + "::module_ok", None),  # kind: recognizes the mjs extension
            (m + '::"module title"', None),  # kind: titles accept the mjs extension
            (z + "::yml_ok", None),  # kind: recognizes the yml extension
            (m, None),  # path: a tracked fixture file resolves as a path
            (
                h + '::"Heading One"',
                "titles require a JS test source",
            ),  # kind: titles require a JS source
            (
                h + "::MD_SYMBOL",
                "no definition patterns for .md",
            ),  # kind: unknown suffix has no symbol patterns
            (
                p
                + "#Python resolver matching fixtures; data only. kinds: python, python_constant",
                "heading requires a Markdown file",
            ),  # kind: headings require the Markdown suffix
            (p + "::Parent::child", None),  # cursor: selects the first parent match
            (j + "::SAME::same_key", None),  # cursor: includes the parent line
            (j + "::$cash", None),  # escape: escapes symbol regex syntax
            (
                p + "::Parent::early",
                "unresolved symbol 'early' after line 19",
            ),  # cursor: updates and never searches backwards
            (h + "#Heading One", None),  # heading: accepts one hash
            (h + "#Heading Six", None),  # heading: accepts six hashes
            (h + "#Trailing Space", None),  # heading: accepts trailing whitespace
            (h + "#Trailing Tab", None),  # heading: accepts a trailing tab
            (h + "#Tabbed Heading", None),  # heading: accepts a tab after the hashes
            (h + "#Literal (dot.)", None),  # heading: treats regex syntax literally
            (h + "#Longer", "heading not found"),  # heading: requires the complete text
            (
                h + "#NoSpace",
                "heading not found",
            ),  # heading: requires whitespace after hashes
            (
                h + "#Indented prose",
                "heading not found",
            ),  # heading: requires at least one hash
            (
                h + "#Too Deep",
                "heading not found",
            ),  # heading: at most six hashes, anchored at line start
            (
                h + "#Embedded Heading",
                "heading not found",
            ),  # heading: rejects prose containing the text
            (
                h + "#Missing",
                "heading not found",
            ),  # heading: requires the requested text
            (h + "#Literal.*", "heading not found"),  # heading: rejects regex wildcards
            (
                h + "#Fenced Heading",
                "heading not found",
            ),  # fence: backticks mask a heading
            (h + "#After Backticks", None),  # fence: equal backticks close
            (
                h + "#Tilde Heading",
                "heading not found",
            ),  # fence: tildes allow backticks in info
            (h + "#After Tildes", None),  # fence: equal tildes close
            (h + "#Two Backticks", None),  # fence: two backticks do not open
            (h + "#Two Tildes", None),  # fence: two tildes do not open
            (h + "#Inline Fence", None),  # fence: openers start after indentation only
            (h + "#Mixed Fence", None),  # fence: opener runs use one character
            (
                h + "#Backtick Info",
                None,
            ),  # fence: backtick info cannot contain backticks
            (
                h + "#Long Backtick Body",
                "heading not found",
            ),  # fence: backtick runs may exceed three
            (
                h + "#Short Close",
                "heading not found",
            ),  # fence: short backticks do not close
            (
                h + "#Wrong Close",
                "heading not found",
            ),  # fence: another character does not close
            (
                h + "#Text Close",
                "heading not found",
            ),  # fence: closer suffix must be spaces or tabs
            (
                h + "#Inline Close",
                "heading not found",
            ),  # fence: closers start after indentation only
            (h + "#Longer Close", None),  # fence: longer backticks close
            (
                h + "#Space Open",
                "heading not found",
            ),  # fence: openers allow four spaces
            (h + "#Space Close", None),  # fence: closers allow four spaces
            (
                h + "#Tab Open",
                "heading not found",
            ),  # fence: openers allow repeated tabs
            (h + "#Tab Close", None),  # fence: closers allow repeated tabs
            (
                h + "#Space Tail Body",
                "heading not found",
            ),  # fence: fenced body is masked
            (h + "#Space Tail Close", None),  # fence: closers allow trailing spaces
            (h + "#Tab Tail Body", "heading not found"),  # fence: tilde body is masked
            (h + "#Tab Tail Close", None),  # fence: closers allow trailing tabs
            (
                h + "#Tilde Short Close",
                "heading not found",
            ),  # fence: short tildes do not close
            (h + "#Tilde Longer Close", None),  # fence: longer tildes close
            (
                h + "#Nested Fence",
                "heading not found",
            ),  # fence: a fence inside a fence remains masked
            (
                h + "#After Nested",
                None,
            ),  # fence: a nested fence cannot replace the opener
            (
                h + "#Mixed Open",
                "heading not found",
            ),  # fence: opener indentation may mix spaces and tabs
            (
                h + "#Mixed Close",
                None,
            ),  # fence: closer indentation and tail may mix spaces and tabs
            (
                u + "#Before Unclosed",
                None,
            ),  # fence: headings before an open fence resolve
            (
                u + "#Unclosed Heading",
                "heading not found",
            ),  # fence: unclosed fences mask to EOF
            (
                u + "#Fenced EOF",
                "heading not found",
            ),  # fence: the last fenced line is masked
            (
                p + "::fenced_py",
                "unresolved symbol 'fenced_py' after line 0",
            ),  # fence: definitions use the mask
            (
                p + "::TabClass::fenced_py",
                "unresolved symbol 'fenced_py' after line 30",
            ),  # fence: later chain segments use the mask
            (p + "::after_fence", None),  # fence: definitions resume after a closer
            (
                p + "::after_fence::missing",
                "unresolved symbol 'missing' after line 37",
            ),  # fence: masking preserves cursor indices
            (t + '::"fenced title"', "title not found"),  # fence: titles use the mask
            (t + '::"after fence"', None),  # fence: titles resume after a closer
            (
                t + "::opener_key",
                "unresolved symbol 'opener_key' after line 0",
            ),  # fence: the opener itself is masked
            (
                h + "#Spaced Backtick Info",
                None,
            ),  # fence: backtick info is checked past whitespace
            (
                h + "#Tilde Tilde Info",
                "heading not found",
            ),  # fence: tilde info may contain tildes
            (t + "::invalid_key", None),  # fence: a rejected opener stays visible
            (h + "#Nbsp Open", None),  # fence: openers reject non-ASCII indentation
            (
                h + "#Nbsp Close",
                "heading not found",
            ),  # fence: closers reject non-ASCII indentation
            (
                h + "#Nbsp Tail",
                "heading not found",
            ),  # fence: closer tails reject non-ASCII whitespace
            (h + "#Separator", None),  # fence: lines split as splitlines splits them
            (
                h + "#Long Backtick Info",
                None,
            ),  # fence: longer backtick openers reject backtick info
            (
                h + "#Much Longer Body",
                "heading not found",
            ),  # fence: a fence stays open until its closer
            (
                h + "#Much Longer Close",
                None,
            ),  # fence: closers may be longer than the opener
            (
                h + "#Backtick Tilde Info",
                "heading not found",
            ),  # fence: backtick info may contain tildes
            (
                h + "#Very Long Backtick Info",
                None,
            ),  # fence: any backtick opener rejects backtick info
            (
                h + "#Very Long Body",
                "heading not found",
            ),  # fence: openers have no maximum run
            (h + "#Very Long Close", None),  # fence: closers have no maximum run
            (
                t + "::deep_key",
                "unresolved symbol 'deep_key' after line 0",
            ),  # fence: deep-indented openers are masked
            (
                t + '::"deep fenced title"',
                "title not found",
            ),  # fence: openers allow docstring-depth indentation
            (
                t + '::"after deep fence"',
                None,
            ),  # fence: closers allow docstring-depth indentation
            (
                h + "#Trailing Backtick Info",
                None,
            ),  # fence: backtick info is checked to its last character
            (
                h + "#Blank Line Body",
                "heading not found",
            ),  # fence: blank lines do not close
            (
                h + "#Fence Tail Body",
                "heading not found",
            ),  # fence: closer tails reject fence characters
            (h + "#Quoted Fence", None),  # fence: openers reject blockquote markers
            (
                h + "#Backtick In Tilde Body",
                "heading not found",
            ),  # fence: backticks do not close a tilde fence
            (
                z + "::fenced_yaml",
                "unresolved symbol 'fenced_yaml' after line 0",
            ),  # fence: block-scalar fences are masked
        )
        errors = []
        witnessed = []
        positive_paths = set()

        def add_error(span, reason):
            errors.append(f"{span!r}: {reason}")

        for span, expected_reason in citations:
            witness = []
            actual_reason = _citation_reason(span, tracked_files, witness=witness)
            if actual_reason != expected_reason:
                add_error(
                    span,
                    f"resolver returned {actual_reason!r}, expected {expected_reason!r}",
                )
            if expected_reason is None:
                citation_kind, path, symbols, _ = _parse_citation(span)
                expected_count = {"file": 0, "symbol": len(symbols)}.get(
                    citation_kind, 1
                )
                if len(witness) != expected_count:
                    add_error(
                        span,
                        f"{len(witness)} witnesses recorded, expected {expected_count}",
                    )
                witnessed.extend(witness)
                positive_paths.add(path)
        foreign_symbols = {
            "python": "foreign_python",
            "python_constant": "FOREIGN_PYTHON_CONSTANT",
            "js": "foreign_js",
            "yaml": "foreign_yaml",
        }
        definition_kinds = {kind for kind, _ in DEFINITION_PATTERNS} - {TITLE_KIND}
        self.assertEqual(
            set(foreign_symbols),
            definition_kinds,
            "foreign_symbols must name every definition kind",
        )
        for path in definition_paths:
            text = (REPO / path).read_text(encoding="utf-8")
            granted = DEFINITION_KINDS_BY_SUFFIX.get(Path(path).suffix, set())
            declaration = re.search(r"kinds: ([\w, ]+)$", text.splitlines()[0])
            declared = set(declaration.group(1).split(", ")) if declaration else set()
            if declared != granted:
                add_error(
                    path,
                    f"declares kinds {sorted(declared)}, the map grants {sorted(granted)}",
                )
            for kind in sorted(definition_kinds):
                symbol = foreign_symbols[kind]
                span = f"{path}::{symbol}"
                if symbol not in text:
                    if kind not in granted:
                        add_error(
                            span,
                            f"fixture has no foreign {kind} line naming {symbol!r}",
                        )
                    continue
                if not _kind_line_visible(text, kind, symbol):
                    add_error(
                        span,
                        f"foreign {kind} line naming {symbol!r} is fenced or not {kind}-shaped",
                    )
                actual_reason = _citation_reason(span, tracked_files)
                expected_reason = f"unresolved symbol {symbol!r} after line 0"
                if actual_reason != expected_reason:
                    add_error(
                        span,
                        f"resolver returned {actual_reason!r}, expected {expected_reason!r}",
                    )
        self.assertFalse(errors, "\n".join(errors))
        indices = {index for _, index in witnessed if index is not None}
        expected_indices = set(range(len(DEFINITION_PATTERNS)))
        self.assertEqual(
            indices,
            expected_indices,
            f"missing pattern witnesses: {sorted(expected_indices - indices)}; unexpected: {sorted(indices - expected_indices)}",
        )
        heading_indices = {index for kind, index in witnessed if kind == "heading"}
        self.assertEqual(
            heading_indices, {None}, "heading witnesses must carry no pattern index"
        )
        kinds = {kind for kind, _ in witnessed}
        expected_kinds = set().union(*DEFINITION_KINDS_BY_SUFFIX.values()) | {
            TITLE_KIND,
            "heading",
        }
        self.assertEqual(
            kinds,
            expected_kinds,
            f"missing kind witnesses: {sorted(expected_kinds - kinds)}; unexpected: {sorted(kinds - expected_kinds)}",
        )
        suffixes = {Path(path).suffix for path in definition_paths}
        self.assertEqual(
            set(DEFINITION_KINDS_BY_SUFFIX),
            suffixes,
            f"missing fixture suffixes: {sorted(set(DEFINITION_KINDS_BY_SUFFIX) - suffixes)}; unmapped: {sorted(suffixes - set(DEFINITION_KINDS_BY_SUFFIX))}",
        )
        self.assertEqual(
            positive_paths,
            set(fixture_paths),
            f"fixture files with no positive row: {sorted(set(fixture_paths) - positive_paths)}; rows citing untracked files: {sorted(positive_paths - set(fixture_paths))}",
        )
        self.assertEqual(
            _unfenced_lines("~~~\n# First\n~~~\nkept\n  ```\nx\n  ```"),
            ["", "", "", "kept", "", "", ""],
            "fences open on the first line, blank both delimiters, and keep every slot",
        )
        marker_cases = (
            ("- ```\nx", ["- ```", "x"]),
            ("* ```\nx", ["* ```", "x"]),
            ("| ```\nx", ["| ```", "x"]),
            ("# ```\nx", ["# ```", "x"]),
            ("```\n~```\nx\n```", ["", "", "", ""]),
            ("```\n>```\nx\n```", ["", "", "", ""]),
            ("```\n#```\nx\n```", ["", "", "", ""]),
            ("```\n` ```\nx\n```", ["", "", "", ""]),
            ("```\n```~\nx\n```", ["", "", "", ""]),
            ("```\n```>\nx\n```", ["", "", "", ""]),
            ("```\n```#\nx\n```", ["", "", "", ""]),
            ("```\n```x\nx\n```", ["", "", "", ""]),
        )
        self.assertEqual(
            [_unfenced_lines(text) for text, _ in marker_cases],
            [expected for _, expected in marker_cases],
            "only spaces and tabs may surround a fence run",
        )
        edge_cases = (
            ("~~~\n```\nx\n~~~", ["", "", "", ""]),
            ("~~~\n~~~ x\nx\n~~~", ["", "", "", ""]),
            ("```\nx\n```\t\ny", ["", "", "", "y"]),
            ("```\nx\n\t```\ny", ["", "", "", "y"]),
            ("\t```\nx\n```\ny", ["", "", "", "y"]),
            ("  ~~~\nx\n~~~\ny", ["", "", "", "y"]),
            ("~~~\nx", ["", ""]),
            ("~~~\n\nx\n~~~", ["", "", "", ""]),
            ("```\n \t\nx\n```", ["", "", "", ""]),
            ("```\n```\ny", ["", "", "y"]),
            ("```\nx\n`````\ny", ["", "", "", "y"]),
            ("x\n~~~", ["x", ""]),
            ("x\n```", ["x", ""]),
            ("``` a``b\nx", ["``` a``b", "x"]),
            ("~~~~\nx\n~~~~", ["", "", ""]),
            ("\n```\nx\n```\ny", ["", "", "", "", "y"]),
        )
        self.assertEqual(
            [_unfenced_lines(text) for text, _ in edge_cases],
            [expected for _, expected in edge_cases],
            "backtick and tilde fences follow the same rules at every edge",
        )
        self.assertEqual(
            [
                _kind_line_visible(text, kind, symbol)
                for text, kind, symbol in (
                    ("const a = { foreign_js: 1 };", "js", "foreign_js"),
                    ("```\nconst a = { foreign_js: 1 };\n```", "js", "foreign_js"),
                    ("# foreign_js", "js", "foreign_js"),
                    ("def foreign_js(): pass", "js", "foreign_js"),
                    ("foreign_python = 1", "python", "foreign_python"),
                    ("const a = { FOREIGN_JS: 1 };", "js", "foreign_js"),
                    ("```\nconst a = { foreign_js: 1 };", "js", "foreign_js"),
                    ("const a = { foreign_jsx: 1 };", "js", "foreign_js"),
                    ("~~~\nconst a = { foreign_js: 1 };\n~~~", "js", "foreign_js"),
                    ("- id: foreign_js", "js", "foreign_js"),
                    ("  const a = { foreign_js: 1 };", "js", "foreign_js"),
                )
            ],
            [True, False, False, False, False, False, False, False, False, False, True],
            "a foreign line counts only unfenced and shaped like its kind",
        )


if __name__ == "__main__":
    unittest.main()

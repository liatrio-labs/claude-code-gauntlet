"""Question-surface guard (Issue #35).

The interactive skill asks the user nothing before the review runs and exactly two
blocking questions after it. That is a property of the shipped prose, and prose
regrows: every past attempt to hold it with an instruction ("never skip
AskUserQuestion") produced the opposite — a gate that forced a question to exist.

This test extracts every AskUserQuestion example block from skills/**/*.md and
asserts the schema each one must satisfy, pins the total number of sites, and
bans the gate patterns that keyed on prompt history rather than resolved state.
"""

import re
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SKILLS = REPO / "skills"

# Every AskUserQuestion example block in the shipped skills, counted per question
# (a block may hold more than one). Raise or lower this deliberately, in the PR
# that changes the surface, with the reason in the body.
EXPECTED_QUESTION_SITES = 9

MAX_HEADER_CHARS = 12
MIN_OPTIONS = 2
MAX_OPTIONS = 4

# Gate patterns that inspect whether a prompt happened instead of what was
# resolved. Banned repo-wide under skills/ (issue #35).
FORBIDDEN_PATTERNS = (
    "was presented",
    "Never skip AskUserQuestion",
    "Phase 2 checks that AskUserQuestion",
)

_STR = r'"((?:[^"\\]|\\.)*)"'
_QUESTION = re.compile(r"\bquestion:\s*" + _STR)
_HEADER = re.compile(r"\bheader:\s*" + _STR)
_MULTISELECT = re.compile(r"\bmultiSelect:\s*(true|false)\b")
_OPTION = re.compile(
    r"\{\s*label:\s*" + _STR + r"\s*,\s*description:\s*" + _STR + r"\s*\}"
)
_FENCE = re.compile(r"^(\s*)(`{3,})")


def fenced_blocks(text):
    """Yield the body of every fenced code block, indentation-tolerant.

    Reference docs indent example blocks inside list items, so the opening fence
    is not always column 0. A block closes on the first line whose stripped form
    is a backtick run at least as long as the opener's -- the same rule
    CommonMark uses, which keeps ````markdown blocks (used for REVIEW.md
    templates) from swallowing the ``` fences nested inside them.
    """
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        m = _FENCE.match(lines[i])
        if not m:
            i += 1
            continue
        opener = m.group(2)
        body = []
        i += 1
        while i < len(lines):
            close = _FENCE.match(lines[i])
            if (
                close
                and close.group(2) >= opener
                and not lines[i].strip()[len(close.group(2)) :]
            ):
                break
            body.append(lines[i])
            i += 1
        i += 1
        yield "\n".join(body)


def question_segments(block):
    """Split a block into one segment per question, in source order.

    Every template writes the fields in the order question, header, multiSelect,
    options, so a segment running from one `question:` to the next holds exactly
    that question's fields. The ordering is part of the contract this test pins.
    """
    starts = [m.start() for m in _QUESTION.finditer(block)]
    bounds = [*starts, len(block)]
    return [block[bounds[i] : bounds[i + 1]] for i in range(len(starts))]


def sites():
    """Every question site: (path, question, header, multiSelect, options)."""
    found = []
    for path in sorted(SKILLS.rglob("*.md")):
        text = path.read_text(encoding="utf-8")
        for block in fenced_blocks(text):
            if "AskUserQuestion" not in block:
                continue
            for seg in question_segments(block):
                q = _QUESTION.search(seg)
                h = _HEADER.search(seg)
                ms = _MULTISELECT.search(seg)
                found.append(
                    {
                        "path": str(path.relative_to(REPO)),
                        "question": q.group(1) if q else None,
                        "header": h.group(1) if h else None,
                        "multiSelect": ms.group(1) if ms else None,
                        "options": _OPTION.findall(seg),
                    }
                )
    return found


class TestQuestionSurface(unittest.TestCase):
    def test_extractor_finds_a_known_shape(self):
        # Fixture test: the live assertions below only exercise today's blocks.
        # A broken fence walker or field regex must fail here even when the real
        # files happen to still parse.
        fixture = "\n".join(
            [
                "prose before",
                "  ```",
                "  AskUserQuestion(",
                "    questions: [{",
                '      question: "Q one?",',
                '      header: "Head",',
                "      multiSelect: false,",
                "      options: [",
                '        { label: "A", description: "first" },',
                '        { label: "B", description: "second" }',
                "      ]",
                "    }]",
                "  )",
                "  ```",
                "````markdown",
                "```yaml",
                "# not a question block",
                "```",
                "````",
            ]
        )
        blocks = [b for b in fenced_blocks(fixture) if "AskUserQuestion" in b]
        self.assertEqual(len(blocks), 1)
        segs = question_segments(blocks[0])
        self.assertEqual(len(segs), 1)
        header_match = _HEADER.search(segs[0])
        self.assertIsNotNone(header_match)
        assert header_match is not None  # narrows for static analysis (pyright)
        self.assertEqual(header_match.group(1), "Head")
        self.assertEqual(_OPTION.findall(segs[0]), [("A", "first"), ("B", "second")])

    def test_site_count_is_pinned(self):
        found = sites()
        self.assertEqual(
            len(found),
            EXPECTED_QUESTION_SITES,
            "question surface changed: "
            + ", ".join(f"{s['path']}:{s['header']}" for s in found),
        )

    def test_every_site_conforms_to_the_schema(self):
        offenders = {}
        for s in sites():
            key = f"{s['path']}:{s['header']}"
            msgs = offenders.setdefault(key, [])
            if not s["question"]:
                msgs.append("no question string")
            if not s["header"]:
                msgs.append("no header")
            elif len(s["header"]) > MAX_HEADER_CHARS:
                msgs.append(f"header {len(s['header'])} chars > {MAX_HEADER_CHARS}")
            if s["multiSelect"] is None:
                msgs.append("multiSelect not stated explicitly")
            n = len(s["options"])
            if not MIN_OPTIONS <= n <= MAX_OPTIONS:
                msgs.append(f"{n} options")
            for label, description in s["options"]:
                if not label.strip() or not description.strip():
                    msgs.append("empty label or description")
            if not msgs:
                del offenders[key]
        self.assertEqual(offenders, {}, f"schema failures: {offenders}")

    def test_no_two_sites_share_a_question_string(self):
        seen = {}
        dupes = {}
        for s in sites():
            q = s["question"]
            if q in seen:
                dupes[q] = [seen[q], s["path"]]
            seen[q] = s["path"]
        self.assertEqual(
            dupes, {}, f"duplicated question text (single-source it): {dupes}"
        )

    def test_prompt_history_gates_are_absent(self):
        offenders = []
        for path in sorted(SKILLS.rglob("*.md")):
            text = path.read_text(encoding="utf-8")
            for pattern in FORBIDDEN_PATTERNS:
                if pattern in text:
                    offenders.append(f"{path.relative_to(REPO)}: {pattern!r}")
        self.assertEqual(
            offenders,
            [],
            "gates must inspect resolved configuration state, never whether a "
            f"prompt occurred (issue #35): {offenders}",
        )


if __name__ == "__main__":
    unittest.main()

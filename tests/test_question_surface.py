import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SKILLS = REPO / "skills"

# Changing the question surface requires an explicit count change.
EXPECTED_QUESTION_SITES = 9

MAX_HEADER_CHARS = 12
MIN_OPTIONS = 2
MAX_OPTIONS = 4

# Gate patterns that inspect whether a prompt happened instead of what was
# resolved.
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
    # CommonMark fence lengths prevent an inner ``` in a REVIEW.md template
    # from closing its outer ```` block; list examples may be indented.
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
    # Templates put question first, so each segment owns that question's fields.
    starts = [m.start() for m in _QUESTION.finditer(block)]
    bounds = [*starts, len(block)]
    return [block[bounds[i] : bounds[i + 1]] for i in range(len(starts))]


def sites():
    found = []
    for path in sorted(SKILLS.rglob("*.md")):
        text = path.read_text(encoding="utf-8")
        for block in fenced_blocks(text):
            if "AskUserQuestion" not in block:
                continue
            for seg in question_segments(block):
                h = _HEADER.search(seg)
                ms = _MULTISELECT.search(seg)
                found.append(
                    {
                        "path": path.relative_to(REPO).as_posix(),
                        "question": _QUESTION.findall(seg)[0],
                        "header": h.group(1) if h else None,
                        "multiSelect": ms.group(1) if ms else None,
                        "options": _OPTION.findall(seg),
                    }
                )
    return found


@pytest.fixture(scope="module")
def question_sites():
    return sites()


def test_extractor_finds_a_known_shape():
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
    assert len(blocks) == 1
    segs = question_segments(blocks[0])
    assert len(segs) == 1
    header_match = _HEADER.search(segs[0])
    assert header_match is not None
    assert header_match.group(1) == "Head"
    assert _OPTION.findall(segs[0]) == [("A", "first"), ("B", "second")]


def test_task_board_site_is_pinned_verbatim(question_sites):
    expected = {
        "path": "skills/code-gauntlet/SKILL.md",
        "question": "Create fix tasks on the task board from these findings?",
        "header": "Task Board",
        "multiSelect": "false",
        "options": [
            (
                "Yes — create tasks",
                "One FIX task per delivered finding via TaskCreate (FIX-bug-1, FIX-conv-2, ...)",
            ),
            ("No — done", "Finish the review without creating tasks"),
        ],
    }
    matches = [site for site in question_sites if site["header"] == "Task Board"]
    assert len(matches) == 1, "expected one Task Board site"
    assert matches[0] == expected


def test_task_board_gate_states_the_action_boundary():
    skill = (SKILLS / "code-gauntlet" / "SKILL.md").read_text(encoding="utf-8")
    gate_line = next(
        line
        for line in skill.splitlines()
        if "MANDATORY GATE" in line and "task-board question" in line
    )
    assert "only TaskCreate makes one task per delivered finding" in gate_line
    assert "never issues, PRs, or branches" in gate_line
    gate = skill.index("task-board question below")
    paragraph_end = skill.index("\n", gate)
    exception = skill.index(
        "> If `artifactPaths.postReview` is null, skip the offer; say no delivery "
        "set was persisted, so no tasks can be created."
    )
    block = skill.index(
        'question: "Create fix tasks on the task board from these findings?"'
    )
    next_section = skill.index("### Print methodology")
    assert gate < block, "offer must follow its gate"
    assert block < next_section, "offer must precede Print methodology"
    assert paragraph_end < exception, "exception must follow the gate paragraph"
    assert exception < block, "exception must precede the offer"


def test_every_site_conforms_to_the_schema(question_sites):
    assert len(question_sites) == EXPECTED_QUESTION_SITES, "question surface changed"
    questions = [site["question"] for site in question_sites]
    assert len(set(questions)) == len(questions), "duplicated question text"
    delivery_sites = [site for site in question_sites if site["header"] == "Delivery"]
    assert len(delivery_sites) == 1, "expected one Delivery site"
    assert (
        delivery_sites[0]["path"]
        == "skills/code-gauntlet/references/phase8-delivery.md"
    )
    for site in question_sites:
        assert site["question"], site
        assert site["header"] and len(site["header"]) <= MAX_HEADER_CHARS, site
        assert site["multiSelect"] is not None, site
        assert MIN_OPTIONS <= len(site["options"]) <= MAX_OPTIONS, site
        for label, description in site["options"]:
            assert label.strip() and description.strip(), site


def test_prompt_history_gates_are_absent():
    offenders = []
    for path in sorted(SKILLS.rglob("*.md")):
        text = path.read_text(encoding="utf-8")
        for pattern in FORBIDDEN_PATTERNS:
            if pattern in text:
                offenders.append(f"{path.relative_to(REPO)}: {pattern!r}")
    assert not offenders, (
        "gates must inspect resolved configuration state, never whether a "
        f"prompt occurred: {offenders}"
    )

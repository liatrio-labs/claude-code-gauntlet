"""Literal outbound vectors and input-only seeded corpus builders."""

import json
import random
import re
from pathlib import Path
from typing import Literal, TypedDict, cast, get_args

from gauntlet.delivery.fold import Platform, Surface
from gauntlet.markdown import open_fence
from gauntlet.marker import FINDING_MARKER_TOKEN, MARKER_TOKENS

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
TextOperation = Literal["prose", "line", "optional", "rule", "redact", "location"]


class TextOptions(TypedDict, total=False):
    rule_ids: list[str]
    kind: Literal["regression", "control"]


class TextVector(TextOptions):
    id: str
    operation: TextOperation
    input: object
    expected: str | None


class CommentCase(TypedDict):
    id: str
    field_class: Literal["prose", "rule", "single_line", "location"]
    input: str
    expected: str


def comment_cases() -> list[CommentCase]:
    payload = json.loads((FIXTURES / "outbound_comment_cases.json").read_text("utf-8"))
    return cast(list[CommentCase], payload["cases"])


def text_vectors() -> list[TextVector]:
    payload = json.loads((FIXTURES / "outbound_text.json").read_text("utf-8"))
    return cast(list[TextVector], payload["cases"])


def line_vectors() -> list[TextVector]:
    payload = json.loads(
        (FIXTURES / "cross_runtime" / "outbound_line.json").read_text("utf-8")
    )
    assert payload["algorithm"] == "outbound_line"
    return [
        cast(
            TextVector, {**row, "id": "outbound_line_" + row["id"], "operation": "line"}
        )
        for row in payload["cases"]
    ]


class FoldInput(TypedDict):
    prefix: str
    repeat: str
    count: int


FoldOperation = Literal[
    "fold",
    "closer",
    "fence_state",
    "fold_review_py",
    "fold_inline_py",
    "fold_prose_js",
    "fold_inline_js",
]


class FoldOptions(TypedDict, total=False):
    platform: Platform
    surface: Surface
    py_allowance: int
    expected_dropped_bytes: int
    limit_bytes: int
    fits_allowance: bool
    js_limit: int
    expected_py: str | list[str | int] | None
    expected_js: str | list[str | int] | None


class FoldVector(FoldOptions):
    id: str
    operation: FoldOperation
    input: str | FoldInput


FOLD_OPERATIONS: tuple[str, ...] = get_args(FoldOperation)


def fold_vectors(
    path: Path = FIXTURES / "cross_runtime" / "outbound_fold.json",
) -> list[FoldVector]:
    payload = json.loads(path.read_text("utf-8"))
    assert payload["algorithm"] == "outbound_fold"
    rows = cast(list[FoldVector], payload["cases"])
    for row in rows:
        if row["operation"] not in FOLD_OPERATIONS:
            raise ValueError(f"unknown fold operation: {row['operation']}")
    return rows


def attack_corpus(seed: Literal[1729, 917]) -> list[str]:
    generator = random.Random(seed)
    alphabet = (
        "@<&#;`!?/0123456789abcdefghijklmnopqrstuvwxyz \n\r"
        if seed == 1729
        else "@<&#!/?`\\0123456789abc\n\r "
    )
    generated = [
        "".join(generator.choice(alphabet) for _ in range(generator.randint(1, 64)))
        for _ in range(5000)
    ]
    if seed == 1729:
        generated.append("```\n<!--\n\ncode-gauntlet-findings: poisoned\n```")
    return generated


def markup_corpus() -> list[str]:
    generator = random.Random(414)
    atoms = (
        "[",
        "]",
        "(",
        ")",
        ":",
        "!",
        "\\",
        "`",
        " ",
        "\n",
        "a",
        ">",
        "-",
        "1.",
        "![a](u)",
    )
    return [
        "".join(generator.choices(atoms, k=generator.randint(1, 30)))
        for _ in range(5000)
    ]


_DANGEROUS_LT = re.compile(r"<(?=[A-Za-z/!?])")
_DANGEROUS_AT = re.compile(r"(?<![A-Za-z0-9])@")
_MULTILINE_QUOTE_OPENER = re.compile(
    r"^(?:[ \t>]|[-+*][ \t]|[0-9]{1,9}[.)][ \t])*?(>{3,})"
)
_MARKER_OPEN = re.compile(
    r"<!--\s*(?:"
    + "|".join(re.escape(token) for token in (*MARKER_TOKENS, FINDING_MARKER_TOKEN))
    + r")\s*:"
)
_DEFINITION = re.compile(
    r"(?m)^(?:[ \t>+*-]|[0-9]+[.)])*\["
    r"((?:\\[^\n]|[^\\\[\]\n]|\n(?![ \t>]*(?:\n|$))[ \t>]*)+)\]:"
)


def assert_outbound_string_invariant(
    output: str,
    *,
    check_prose_rules: bool = True,
    literal_locations: tuple[str, ...] = (),
) -> None:
    fences: list[tuple[int, int]] = []
    open_fence(output, strict=True, intervals=fences)
    cursor = 0
    outside = []
    for start, end in fences:
        outside.append(output[cursor:start])
        assert not _MARKER_OPEN.search(output[start:end]), output[start:end]
        cursor = end
    outside.append(output[cursor:])
    for fragment in outside:
        markup = fragment
        # Only caller-owned location wrappers bypass prose preparation.
        for location in literal_locations:
            markup = markup.replace(location, "")
        assert not re.search(r"(?<!\\)(?:\\\\)*!\[", markup), markup
        for definition in _DEFINITION.finditer(markup):
            label = re.sub(r"\n[ \t>]*", "\n", definition.group(1))
            assert len(label) > 999 or not label.strip(), markup
        for visible in (fragment, re.sub(r"`+", "", fragment)):
            assert not _DANGEROUS_LT.search(visible), visible
            assert not _DANGEROUS_AT.search(visible), visible
            assert not _MARKER_OPEN.search(visible), visible
        if check_prose_rules:
            for line in fragment.splitlines():
                assert not line.startswith("/"), line
                assert not _MULTILINE_QUOTE_OPENER.match(line), line

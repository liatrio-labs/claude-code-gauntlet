"""Typed literal outbound rows, fixture readers and seeded input corpora."""

import json
import random
from pathlib import Path
from typing import Literal, TypedDict, cast, get_args

from gauntlet.delivery.fold import Platform, Surface

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
TextOperation = Literal["prose", "line", "optional", "rule", "redact", "location"]


class TextOptions(TypedDict, total=False):
    js: Literal["n/a"]
    rule_ids: list[str]
    kind: Literal["regression", "control"]
    also: Literal["untrusted_fence"]
    note: str
    operations: list[TextOperation]


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


class SummaryFinding(TypedDict):
    id: str
    severity: str
    file: str
    line_start: int
    title: str


class SummaryIdentity(TypedDict):
    platform: str
    web_origin: str
    owner: str
    repo: str
    sha_full: str


class SummaryInput(TypedDict):
    findings: list[SummaryFinding]


class LinkedSummaryInput(SummaryInput):
    prIdentity: SummaryIdentity


class SummaryCase(TypedDict):
    id: str
    input: SummaryInput | LinkedSummaryInput
    expected_py: str
    expected_js: str


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
            TextVector,
            {**row, "id": f"outbound_{operation}_" + row["id"], "operation": operation},
        )
        for row in payload["cases"]
        for operation in row.get("operations", ["line"])
    ]


def summary_cases() -> list[SummaryCase]:
    payload = json.loads(
        (FIXTURES / "cross_runtime" / "outbound_line.json").read_text("utf-8")
    )
    assert payload["algorithm"] == "outbound_line"
    return cast(list[SummaryCase], payload["summary_cases"])


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
    also: Literal["prepared_fixpoint", "string_invariant"]
    note: str


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
        "&",
        ";",
        "&excl;",
        "&lbrack;",
        "&lsqb;",
        ">",
        "-",
        "1.",
        "![a](u)",
        ": ",
        "~ ",
        "[[",
        "]]",
        "|",
        "a" * 1000,
        "[critical]: u",
        "[" + "a" * 1000 + "]: u",
        "[[https://example.test/p.png]]",
        "<!--",
        "-->",
        "<!",
        "--",
        "&#",
        "&#x",
        "58;",
        "x3a;",
        "93;",
        "40;",
        "x5c;",
        "&colon;",
        "&rsqb;",
        "&lpar;",
        "&bsol;",
        "&nbsp;",
        "&NonBreakingSpace;",
        "\u00a0",
        "\u200b",
        "~~~",
        "](",
        "]:",
        "[^",
    )
    return [
        "".join(generator.choices(atoms, k=generator.randint(1, 30)))
        for _ in range(5000)
    ]

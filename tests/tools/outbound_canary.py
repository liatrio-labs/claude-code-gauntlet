from __future__ import annotations

import argparse
import json
import os
import random
import re
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Literal, TypedDict

GENERATOR_VERSION = 1
Operation = Literal["prose", "line"]
DiagnosticOperation = Operation | Literal["preflight"]
OPERATIONS: tuple[Operation, ...] = ("prose", "line")
Property = Literal[
    "fixpoint", "marker", "mention", "raw-html", "renderer", "preparer", "preflight"
]


@dataclass(frozen=True, slots=True)
class Failure:
    property: Property
    prepared: str = ""
    second: str | None = None
    html: str | None = None
    error: str | None = None


class Diagnostic(TypedDict):
    generator_version: int
    seed: int
    handle: str
    index: int | None
    operation: DiagnosticOperation
    property: Property
    original: str
    prepared: str
    second: str | None
    html: str | None
    structure: str | None
    error: str | None


def generate_cases(seed: int, mention: str, count: int) -> list[str]:
    rng = random.Random(seed)
    prefix = (
        f"@{mention}",
        f"&#64;{mention} &commat;{mention}",
        f"&amp;#64;{mention} \uff20{mention}",
        "<!-- code-gauntlet-findings: {} -->",
        "<!-- deep-review-findings: {} -->",
        '<!-- code-gauntlet-finding-key: {"sha":"abcdef0",'
        '"key":"aaaaaaaaaaaaaaaa"} -->',
        "<!\n-- code-gauntlet-findings: {} --\n>",
        f"\\@{mention} \\<b> &lt;!-- &amp;lt;b&amp;gt;",
        f"```\n@{mention}\n```\n~~~\n@{mention}\n~~~",
        f"`@{mention}` ``<b>``\r\n@{mention}\r@{mention}",
        f"\u200b@{mention}\xa0&Tab; &NewLine; &#x40;{mention}",
        f"- @{mention}\n> @{mention}\n1. @{mention}",
        f"[x][ref]\n[ref]: /@{mention}\n![x](url) [[wiki]] [^note]\n"
        f"[^note]: @{mention}",
    )
    atoms = (
        *prefix,
        "`",
        "~~~",
        "<!--",
        "-->",
        "&amp;",
        "\n",
        "\r",
        "[",
        "]",
        "(",
        ")",
        "\u2060",
        "\ufeff",
    )
    templates = ("{0}\n{1}", "```\n{0}\n```\n{1}", "> {0}\n\n- {1}", "{0}&amp;\n{1}")
    cases: list[str] = []
    for index in range(count):
        if index < len(prefix):
            body = prefix[index]
        else:
            left = "".join(rng.choice(atoms) for _ in range(rng.randint(2, 12)))
            right = "".join(rng.choice(atoms) for _ in range(rng.randint(2, 12)))
            body = rng.choice(templates).format(left, right)
        sentinel = f"OUTBOUND_PROBE_HTML_{index}"
        controls = (
            f"\n&lt;b&gt;{sentinel}&lt;/b&gt;\n```\n<b>{sentinel}</b>\n```"
            f"\n<b>{sentinel}</b>"
        )
        cases.append(body + controls)
    return cases


def check_case(
    original: str,
    index: int,
    prepare: Callable[[str], str],
    render: Callable[[str], str],
) -> Failure | None:
    from gauntlet.marker import FINDING_MARKER_TOKEN, MARKER_TOKENS

    from tests.tools.probe_html import _Node, _TreeParser, _walk, is_github_mention

    prepared = ""
    try:
        prepared = prepare(original)
        second = prepare(prepared)
    # A preparation error cannot pass containment.
    except Exception as exc:  # noqa: BLE001
        return Failure("preparer", prepared, error=str(exc))
    if second != prepared:
        return Failure("fixpoint", prepared, second=second)
    # Renderers can strip comments, so inspect the actual outbound marker bytes.
    tokens = (*MARKER_TOKENS, FINDING_MARKER_TOKEN)
    if re.search(r"<!--\s*(?:" + "|".join(tokens) + r")\s*:", prepared):
        return Failure("marker", prepared)
    try:
        rendered = render(prepared)
    # A failed renderer provides no safety evidence.
    except Exception as exc:  # noqa: BLE001
        return Failure("renderer", prepared, error=str(exc))
    parser = _TreeParser()
    parser.feed(rendered)
    parser.close()

    def text(node: _Node) -> str:
        return "".join(
            child if isinstance(child, str) else text(child) for child in node.children
        )

    for node, _ in _walk(parser.root):
        if is_github_mention(node):
            return Failure("mention", prepared, html=rendered)
        if node.tag == "b" and f"OUTBOUND_PROBE_HTML_{index}" in text(node):
            return Failure("raw-html", prepared, html=rendered)
    return None


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Opt-in generated outbound containment canary; posts no comments."
    )
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--seed", type=int, default=375)
    parser.add_argument("--cases", type=int, default=64)
    parser.add_argument("--mention")
    parser.add_argument("--case-index", type=int)
    return parser


def main(
    argv: list[str] | None = None,
    *,
    render: Callable[[str], str] | None = None,
    login: Callable[[], str] | None = None,
) -> int:
    from gauntlet.text import prepare_line, prepare_prose

    from tests.tools.probe_github import github_login, render_github, run_github_canary
    from tests.tools.probe_html import structure

    parser = build_argument_parser()
    args = parser.parse_args(argv)
    seed: int = args.seed
    count: int = args.cases
    selected: int | None = args.case_index
    handle: str = args.mention or ""
    if not 1 <= count <= 256:
        parser.error("--cases must be between 1 and 256")
    if selected is not None and not 0 <= selected < count:
        parser.error("--case-index must be within --cases")
    if (
        args.mention is not None
        and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,99}", handle) is None
    ):
        parser.error("--mention must be a GitHub handle without @")
    if not args.live:
        parser.print_help()
        print("--live is required", file=sys.stderr)
        return 2
    if os.environ.get("CI") or os.environ.get("GITHUB_ACTIONS"):
        print("outbound canary refuses CI", file=sys.stderr)
        return 2
    renderer = render if render is not None else render_github
    get_login = login if login is not None else github_login
    operations: Mapping[Operation, Callable[[str], str]] = {
        "prose": prepare_prose,
        "line": prepare_line,
    }

    def report(
        failure: Failure,
        original: str,
        index: int | None,
        operation: DiagnosticOperation,
    ) -> int:
        diagnostic: Diagnostic = {
            "generator_version": GENERATOR_VERSION,
            "seed": seed,
            "handle": handle,
            "index": index,
            "operation": operation,
            "property": failure.property,
            "original": original,
            "prepared": failure.prepared,
            "second": failure.second,
            "html": failure.html,
            "structure": structure(failure.html, platform="github")
            if failure.html is not None
            else None,
            "error": failure.error,
        }
        print(json.dumps(diagnostic, ensure_ascii=True))
        replay = f"python3 tests/tools/outbound_canary.py --live --seed {seed}"
        replay += f" --cases {count}"
        if handle:
            replay += f" --mention {handle}"
        if index is not None:
            replay += f" --case-index {index}"
        print("Replay: " + replay)
        return 1

    try:
        authenticated = get_login()
        handle = handle or authenticated
        run_github_canary(handle, renderer)
    # Preflight fails closed before any generated case runs.
    except Exception as exc:  # noqa: BLE001
        return report(Failure("preflight", error=str(exc)), "", None, "preflight")
    completed = 0
    for index, original in enumerate(generate_cases(seed, handle, count)):
        if selected is not None and index != selected:
            continue
        for operation in OPERATIONS:
            failure = check_case(original, index, operations[operation], renderer)
            if failure is not None:
                return report(failure, original, index, operation)
        completed += 1
    print(
        f"Passed {completed} cases (both operations); seed={seed} "
        f"version={GENERATOR_VERSION} mention={handle}"
    )
    return 0


if __name__ == "__main__":
    from pathlib import Path

    # Direct execution from another directory still needs the repository packages.
    repo_root = Path(__file__).resolve().parents[2]
    sys.path[:0] = [str(repo_root / "scripts"), str(repo_root)]

    raise SystemExit(main())

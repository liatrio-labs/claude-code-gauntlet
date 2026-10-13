from __future__ import annotations

import argparse
import json
import os
import random
import re
import shlex
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Literal, TypedDict

GENERATOR_VERSION = 1
MAX_INPUT_BYTES = 8192
Operation = Literal["prose", "line"]
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
    index: int
    operation: str
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
        '<!-- code-gauntlet-finding-key: {"sha":"abcdef0","key":"aaaaaaaaaaaaaaaa"} -->',
        "<!\n-- code-gauntlet-findings: {} --\n>",
        f"\\@{mention} \\<b> &lt;!-- &amp;lt;b&amp;gt;",
        f"```\n@{mention}\n```\n~~~\n@{mention}\n~~~",
        f"`@{mention}` ``<b>``\r\n@{mention}\r@{mention}",
        f"\u200b@{mention}\xa0&Tab; &NewLine; &#x40;{mention}",
        f"- @{mention}\n> @{mention}\n1. @{mention}",
        f"[x][ref]\n[ref]: /@{mention}\n![x](url) [[wiki]] [^note]\n[^note]: @{mention}",
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
        controls = f"\n&lt;b&gt;{sentinel}&lt;/b&gt;\n```\n<b>{sentinel}</b>\n```\n<b>{sentinel}</b>"
        budget = MAX_INPUT_BYTES - len(controls.encode("utf-8"))
        body = body.encode("utf-8")[:budget].decode("utf-8", errors="ignore")
        cases.append(body + controls)
    return cases


def check_case(
    original: str,
    index: int,
    prepare: Callable[[str], str],
    render: Callable[[str], str],
) -> Failure | None:
    from gauntlet.marker import find_finding_markers, find_marker

    from tests.tools.probe_html import _Node, _TreeParser, _walk, is_github_mention

    prepared = ""
    try:
        prepared = prepare(original)
        second = prepare(prepared)
    except Exception as exc:  # noqa: BLE001 - preparation errors cannot pass containment
        return Failure("preparer", prepared, error=str(exc))
    if second != prepared:
        return Failure("fixpoint", prepared, second=second)
    # Renderers can strip comments, so inspect the actual outbound marker bytes.
    if find_marker(prepared) is not None or find_finding_markers(prepared):
        return Failure("marker", prepared)
    try:
        rendered = render(prepared)
    except Exception as exc:  # noqa: BLE001 - a failed renderer provides no safety evidence
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
    preparers: Mapping[Operation, Callable[[str], str]] | None = None,
) -> int:
    import sys

    from gauntlet.text import prepare_line, prepare_prose

    from tests.tools.probe_github import github_login, render_github, run_github_canary
    from tests.tools.probe_html import structure

    parser = build_argument_parser()
    args = parser.parse_args(argv)
    seed: int = args.seed
    count: int = args.cases
    selected: int | None = args.case_index
    handle: str = args.mention or ""
    if not 1 <= count <= 4096:
        parser.error("--cases must be between 1 and 4096")
    if selected is not None and not 0 <= selected < count:
        parser.error("--case-index must be within --cases")
    if (
        args.mention is not None
        and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9/-]{0,99}", handle) is None
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
    operations: Mapping[Operation, Callable[[str], str]] = (
        preparers
        if preparers is not None
        else {"prose": prepare_prose, "line": prepare_line}
    )

    def report(failure: Failure, original: str, index: int, operation: str) -> int:
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
        command = [
            "python3",
            "tests/tools/outbound_canary.py",
            "--live",
            "--seed",
            str(seed),
            "--cases",
            str(count),
        ]
        if handle:
            command.extend(["--mention", handle])
        command.extend(["--case-index", str(index)])
        print("Replay: " + shlex.join(command))
        return 1

    try:
        authenticated = get_login()
        handle = handle or authenticated
        run_github_canary(authenticated, renderer)
    except Exception as exc:  # noqa: BLE001 - preflight must fail closed before generated cases
        return report(
            Failure("preflight", error=str(exc)), "", selected or 0, "preflight"
        )
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
        f"Passed {completed} cases (both operations); seed={seed} version={GENERATOR_VERSION} mention={handle}"
    )
    return 0


if __name__ == "__main__":
    import sys
    from pathlib import Path

    # Direct execution from another directory still needs the repository packages.
    repo_root = Path(__file__).resolve().parents[2]
    sys.path[:0] = [str(repo_root / "scripts"), str(repo_root)]

    raise SystemExit(main())

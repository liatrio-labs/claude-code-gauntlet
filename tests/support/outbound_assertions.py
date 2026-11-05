import json
import re
import subprocess
from collections.abc import Sequence
from pathlib import Path

import gauntlet.text as outbound_text
from gauntlet.markdown import blocks, code_spans, open_fence

REPO = Path(__file__).resolve().parents[2]


def assert_outbound_string_invariant(
    output: str,
    *,
    check_prose_rules: bool = True,
    literal_locations: tuple[str, ...] = (),
) -> None:
    inert = [
        line.kind == "indented_code" and not line.contained
        for line in blocks(output, html=False)
    ]
    fences: list[tuple[int, int]] = []
    open_fence(output, strict=True, intervals=fences)
    if check_prose_rules:
        for start, _ in fences:
            line_end = output.find("\n", start)
            if line_end < 0:
                line_end = len(output)
            opener = re.match(r"(`{3,}|~{3,})", output[start:line_end])
            assert opener is not None, "fence opener must start at column zero"
            assert not output[start + opener.end() : line_end].strip(" \t"), (
                "fence opener must have no info string"
            )
    cursor = 0
    outside: list[tuple[str, int]] = []
    line_number = 0
    for start, end in fences:
        outside.append((output[cursor:start], line_number))
        line_number += output.count("\n", cursor, end)
        assert not outbound_text.has_marker_opener(output[start:end]), (
            "fenced output contains a marker opener"
        )
        cursor = end
    outside.append((output[cursor:], line_number))
    for fragment, first_line in outside:
        assert re.search(r"[!\[]\\*\[", fragment) is None, repr(fragment)
        markup = fragment
        # Code-owned locations cannot define references, but still contain openers.
        for location in literal_locations:
            markup = markup.replace(location, "")
        spans, _ = code_spans(markup)
        line_offset = 0
        for row, line in enumerate(markup.split("\n"), first_line):
            if inert[row]:
                line_offset += len(line) + 1
                continue
            if re.search(r"\]\\?\(", line):
                assert re.search(r"\](\\*):", line) is None, repr(line)
            if check_prose_rules:
                for tilde in re.finditer(r"~{3,}", line):
                    position = line_offset + tilde.start()
                    assert any(start <= position < end for start, end in spans), repr(
                        line
                    )
            line_offset += len(line) + 1
        slashes = 0
        line_start = 0
        first_bracket = True
        row = first_line
        for index, character in enumerate(markup):
            escaped = slashes % 2 == 1
            if character == "[" and not escaped and not inert[row]:
                prefix = markup[line_start:index]
                if first_bracket and not any(
                    "A" <= ch <= "Z" or "a" <= ch <= "z" or ch == "\\" for ch in prefix
                ):
                    at = index + 1
                    nonblank = False
                    while at < len(markup):
                        ch = markup[at]
                        if ch == "\\":
                            if at + 1 == len(markup) or markup[at + 1] == "\n":
                                break
                            nonblank = True
                            at += 2
                            continue
                        if ch == "[":
                            break
                        if ch == "]":
                            assert not (nonblank and markup[at + 1 : at + 2] == ":"), (
                                repr(markup)
                            )
                            break
                        if ch == "\n":
                            at += 1
                            while at < len(markup) and markup[at] in " \t>":
                                at += 1
                            if at == len(markup) or markup[at] == "\n":
                                break
                            continue
                        nonblank = nonblank or not ch.isspace()
                        at += 1
            if character == "[":
                first_bracket = False
            if character == "\n":
                line_start = index + 1
                first_bracket = True
                row += 1
            slashes = slashes + 1 if character == "\\" else 0
        for visible in (fragment, fragment.replace("`", "")):
            assert not re.search(r"<(?=[A-Za-z/!?])", visible), repr(visible)
            assert not re.search(r"(?<![A-Za-z0-9])@", visible), repr(visible)
            assert not outbound_text.has_marker_opener(visible), repr(visible)
        if check_prose_rules:
            for row, line in enumerate(fragment.split("\n"), first_line):
                if inert[row]:
                    continue
                assert not line.startswith("/"), line
                assert not re.match(
                    r"^(?:[ \t>]|[-+*][ \t]|[0-9]{1,9}[.)][ \t])*?(>{3,})", line
                ), line


def _run_node(script: str, value: object) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["node", "--input-type=module", "-e", script],
        cwd=REPO,
        input=json.dumps(value, ensure_ascii=False),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
        check=False,
    )


_SUMMARY_BATCH_SCRIPT = """
import { renderSummaryBody } from './workflows/src/renderReport.js';
let source = '';
for await (const chunk of process.stdin) source += chunk;
const inputs = JSON.parse(source);
for (const input of inputs) for (const finding of input.findings || []) {
  finding.disposition = 'deliver';
  finding.admission = {version:1,status:'admitted',input_proof:'fixture'};
}
process.stdout.write(JSON.stringify(inputs.map(renderSummaryBody)));
"""


def _render_summary_bodies(inputs: Sequence[object]) -> list[str]:
    result = _run_node(_SUMMARY_BATCH_SCRIPT, inputs)
    assert result.returncode == 0, result.stderr
    summaries: list[str] = json.loads(result.stdout)
    assert len(summaries) == len(inputs), (
        "summary renderer must return one body per input"
    )
    return summaries

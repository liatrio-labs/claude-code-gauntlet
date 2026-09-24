"""Fixture-driven tests for the outbound comment text contract."""

import json
import random
import subprocess
from pathlib import Path

import scripts.post_review as post_review

REPO = Path(__file__).resolve().parents[1]
FIXTURE_PATH = REPO / "tests" / "fixtures" / "outbound_comment_cases.json"
FIXTURE = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
CASES = FIXTURE["cases"]
_Q2_RULES = tuple(f"Q2.{letter}" for letter in "abcdefghij")


def _run_node(script, value):
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


def _summary_inputs():
    title_cases = [case for case in CASES if case["id"] == "title_505_code_span"]
    assert title_cases, "fixture is missing the long summary-title probe"
    return [
        {
            "findings": [
                {
                    "id": "hostile-title",
                    "title": "Review @leehopper <table>",
                    "file": "src/review.py",
                    "line_start": 4,
                    "severity": "high",
                },
                {
                    "id": "code-location",
                    "title": "Location delimiter",
                    "file": "src/dir/``/file.py",
                    "line_start": 7,
                    "line_end": 9,
                    "severity": "medium",
                },
                {
                    "id": "fold-boundary",
                    "title": title_cases[0]["input"],
                    "file": "src/title.py",
                    "line_start": 12,
                    "severity": "low",
                },
            ]
        },
        {
            "findings": [
                {
                    "id": "second-hostile-title",
                    "title": "@Override and @types/node <b>literal</b>",
                    "file": "src/types.ts",
                    "line_start": 21,
                    "severity": "low",
                }
            ]
        },
    ]


def test_fixture_schema_and_rule_coverage():
    assert 60 <= len(CASES) <= 90
    required_fields = {
        "id",
        "field_class",
        "kind",
        "input",
        "expected",
        "rule_ids",
        "github_probe",
        "note",
    }
    assert len({case["id"] for case in CASES}) == len(CASES)
    for case in CASES:
        assert set(case) == required_fields
        assert case["field_class"] in {"single_line", "prose", "rule", "location"}
        assert case["kind"] in {"regression", "control"}
        assert isinstance(case["input"], str)
        assert isinstance(case["expected"], str)
        assert case["github_probe"] is None
        assert isinstance(case["rule_ids"], list)

    for rule_id in _Q2_RULES:
        kinds = {case["kind"] for case in CASES if rule_id in case["rule_ids"]}
        assert kinds == {"regression", "control"}, rule_id

    expected_probe_ids = {
        "span_html_block",
        "span_atx",
        "span_list",
        "span_quote",
        "ordered_list_span",
        "setext_span",
        "table_cell_span",
        "list_fence",
        "dest_backtick",
        "dest_malformed_danger",
        "html_attr_backtick",
        "fence_tab",
        "fence_3sp_para",
        "piped_table",
        "pipeless_outer_table",
        "escaped_pipe_table_span",
        "double_encoded_at_in_code",
        "double_encoded_nonascii_in_code",
        "nested_ampersand_fixpoint",
        "nested_comment_fixpoint",
        "comment_split_secret",
        "long_comment_rule",
        "comment_only_rule",
        "title_505_code_span",
        "location_double_ticks",
    }
    assert expected_probe_ids <= {case["id"] for case in CASES}


def test_control_fixture_rows_match_current_sanitizers():
    prose_controls = [
        case
        for case in CASES
        if case["kind"] == "control" and case["field_class"] in {"prose", "rule"}
    ]
    for case in prose_controls:
        if case["field_class"] == "rule" and not case["expected"]:
            continue
        field = "suggestion" if case["field_class"] == "prose" else "claude_md_rule"
        rendered = post_review.render_comment_body(
            {"severity": "low", "title": "Control", "body": "", field: case["input"]}
        )
        assert case["expected"] in rendered, case["id"]

    visible_controls = [
        case
        for case in CASES
        if case["kind"] == "control"
        and case["field_class"] in {"single_line", "location"}
    ]
    inputs = []
    expected_text = []
    for case in visible_controls:
        finding = {"id": case["id"], "severity": "low", "title": "Control"}
        if case["field_class"] == "single_line":
            finding["title"] = case["input"]
        else:
            finding["file"] = case["input"]
        inputs.append({"findings": [finding]})
        expected_text.append(case["expected"])
    script = """
import { renderSummaryBody } from './workflows/src/renderReport.js';
let source = '';
for await (const chunk of process.stdin) source += chunk;
process.stdout.write(JSON.stringify(JSON.parse(source).map(renderSummaryBody)));
"""
    result = _run_node(script, inputs)
    assert result.returncode == 0, result.stderr
    summaries = json.loads(result.stdout)
    assert len(summaries) == len(expected_text)
    for case, summary in zip(visible_controls, summaries, strict=True):
        assert case["expected"] in summary, case["id"]


def test_comment_only_rule_falls_back_to_spec_text():
    comment_case = next(case for case in CASES if case["id"] == "comment_only_rule")
    rendered = post_review.render_comment_body(
        {
            "severity": "high",
            "title": "Rule fallback",
            "body": "Description",
            "claude_md_rule": comment_case["input"],
            "spec_text": "Fallback specification text",
        }
    )
    assert "Fallback specification text" in rendered
    assert "hidden rule" not in rendered


def test_prepare_prose_fixture_cases():
    prepare_prose = getattr(post_review, "prepare_prose", None)
    assert callable(prepare_prose), "missing expected Python entry point prepare_prose"
    cases = [case for case in CASES if case["field_class"] in {"prose", "rule"}]
    for case in cases:
        assert prepare_prose(case["input"]) == case["expected"], case["id"]


def test_prepare_line_fixture_cases():
    prepare_line = getattr(post_review, "prepare_line", None)
    assert callable(prepare_line), "missing expected Python entry point prepare_line"
    cases = [case for case in CASES if case["field_class"] == "single_line"]
    for case in cases:
        assert prepare_line(case["input"]) == case["expected"], case["id"]


def _generated_attack_corpus():
    generator = random.Random(1729)
    alphabet = "@<&#;`!?/0123456789abcdefghijklmnopqrstuvwxyz \n\r"
    return [
        "".join(generator.choice(alphabet) for _ in range(generator.randint(1, 64)))
        for _ in range(5000)
    ]


def test_preparation_is_idempotent_over_fixture_and_seeded_corpus():
    prepare_prose = getattr(post_review, "prepare_prose", None)
    prepare_line = getattr(post_review, "prepare_line", None)
    assert callable(prepare_prose), "missing expected Python entry point prepare_prose"
    assert callable(prepare_line), "missing expected Python entry point prepare_line"
    corpus = [case["input"] for case in CASES] + _generated_attack_corpus()
    assert len(corpus) >= 5000
    for text in corpus:
        prepared_prose = prepare_prose(text)
        assert prepare_prose(prepared_prose) == prepared_prose
        prepared_line = prepare_line(text)
        assert prepare_line(prepared_line) == prepared_line


def test_live_node_prepare_line_matches_single_line_and_location_fixtures():
    line_cases = [
        case for case in CASES if case["field_class"] in {"single_line", "location"}
    ]
    idempotency_cases = [
        case
        for case in CASES
        if "Q3" in case["rule_ids"]
        or any(rule_id in case["rule_ids"] for rule_id in _Q2_RULES)
    ]
    cases = {case["id"]: case for case in line_cases + idempotency_cases}
    inputs = [case["input"] for case in cases.values()]
    script = """
const renderer = await import('./workflows/src/renderReport.js');
if (typeof renderer.prepareLine !== 'function') {
  process.stderr.write('missing expected JavaScript twin prepareLine');
  process.exitCode = 17;
} else {
  let source = '';
  for await (const chunk of process.stdin) source += chunk;
  const values = JSON.parse(source);
  process.stdout.write(JSON.stringify(values.map((value) => {
    const once = renderer.prepareLine(value);
    return { once, twice: renderer.prepareLine(once) };
  })));
}
"""
    result = _run_node(script, inputs)
    assert result.returncode == 0, result.stderr
    js_results = json.loads(result.stdout)
    js_by_id = dict(zip(cases, js_results, strict=True))
    for case in idempotency_cases:
        result_row = js_by_id[case["id"]]
        assert result_row["twice"] == result_row["once"], case["id"]
    prepare_line = getattr(post_review, "prepare_line", None)
    assert callable(prepare_line), "missing expected Python entry point prepare_line"
    python_prepared = [prepare_line(case["input"]) for case in line_cases]
    js_prepared = [js_by_id[case["id"]]["once"] for case in line_cases]
    expected = [case["expected"] for case in line_cases]
    assert python_prepared == expected
    assert js_prepared == expected
    assert js_prepared == python_prepared


def test_generated_summary_is_unchanged_by_python_guard():
    script = """
import { renderSummaryBody } from './workflows/src/renderReport.js';
let source = '';
for await (const chunk of process.stdin) source += chunk;
process.stdout.write(JSON.stringify(JSON.parse(source).map(renderSummaryBody)));
"""
    result = _run_node(script, _summary_inputs())
    assert result.returncode == 0, result.stderr
    summaries = json.loads(result.stdout)
    assert len(summaries) >= 2
    prepare_prose = getattr(post_review, "prepare_prose", None)
    assert callable(prepare_prose), "missing expected Python guard prepare_prose"
    for summary in summaries:
        assert prepare_prose(summary) == summary
    location = "```src/dir/``/file.py:7-9```"
    assert location in summaries[0]


def test_summary_renderer_contains_hostile_title():
    script = """
import { renderSummaryBody } from './workflows/src/renderReport.js';
let source = '';
for await (const chunk of process.stdin) source += chunk;
process.stdout.write(renderSummaryBody(JSON.parse(source)));
"""
    value = {
        "findings": [
            {
                "id": "hostile-title",
                "title": "Review @leehopper <table>",
                "file": "src/review.py",
                "line_start": 4,
                "severity": "high",
            }
        ]
    }
    result = _run_node(script, value)
    assert result.returncode == 0, result.stderr
    summary = result.stdout
    assert "\uff20leehopper" in summary and "&lt;table>" in summary, summary


def test_summary_location_uses_a_safe_delimiter_for_two_ticks():
    script = """
import { renderSummaryBody } from './workflows/src/renderReport.js';
let source = '';
for await (const chunk of process.stdin) source += chunk;
process.stdout.write(renderSummaryBody(JSON.parse(source)));
"""
    value = {
        "findings": [
            {
                "id": "code-location",
                "title": "Location delimiter",
                "file": "src/dir/``/file.py",
                "line_start": 7,
                "line_end": 9,
                "severity": "medium",
            }
        ]
    }
    result = _run_node(script, value)
    assert result.returncode == 0, result.stderr
    safe_span = "```src/dir/``/file.py:7-9```"
    assert safe_span in result.stdout, result.stdout


def test_summary_title_fold_does_not_split_inline_code():
    case = next(case for case in CASES if case["id"] == "title_505_code_span")
    script = """
import { renderSummaryBody } from './workflows/src/renderReport.js';
let source = '';
for await (const chunk of process.stdin) source += chunk;
process.stdout.write(renderSummaryBody(JSON.parse(source)));
"""
    value = {
        "findings": [
            {
                "id": "fold-boundary",
                "title": case["input"],
                "file": "src/title.py",
                "line_start": 12,
                "severity": "low",
            }
        ]
    }
    result = _run_node(script, value)
    assert result.returncode == 0, result.stderr
    bullet = next(line for line in result.stdout.splitlines() if line.startswith("- "))
    title_text = bullet.split("`: ", 1)[1]
    assert title_text.count("`") % 2 == 0, result.stdout

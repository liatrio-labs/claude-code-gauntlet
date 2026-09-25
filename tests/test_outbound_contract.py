"""Fixture-driven tests for the outbound comment text contract."""

import json
import random
import re
import subprocess
from datetime import date
from pathlib import Path

import pytest  # type: ignore[import-not-found]

import scripts.post_review as post_review
from scripts.review_marker import FINDING_MARKER_TOKEN, MARKER_TOKENS
from tests.tools.render_probes import (
    _skeleton,
    check_render,
    derive_handles,
    input_sha256,
    input_text,
    pair_sha256,
    serialize_fixture,
    structure,
)

REPO = Path(__file__).resolve().parents[1]
FIXTURE_PATH = REPO / "tests" / "fixtures" / "outbound_comment_cases.json"
FIXTURE = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
CASES = FIXTURE["cases"]
_CONTAINMENT_RULES = (
    "span.equal_length_closer",
    "fence.column_zero",
    "prose.html",
    "prose.mention",
    "marker.grammar",
    "span.unmatched",
    "container.boundary",
    "prose.leading_slash",
    "prose.multiline_quote",
)
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


def test_tracked_fixture_has_canonical_byte_layout():
    assert FIXTURE_PATH.read_bytes() == serialize_fixture(CASES).encode("utf-8")


def _assert_outbound_string_invariant(output, *, check_prose_rules=True):
    fences = []
    post_review._open_fence(output, strict=True, intervals=fences)
    cursor = 0
    outside = []
    for start, end in fences:
        outside.append(output[cursor:start])
        assert not _MARKER_OPEN.search(output[start:end]), output[start:end]
        cursor = end
    outside.append(output[cursor:])
    for fragment in outside:
        for visible in (fragment, re.sub(r"`+", "", fragment)):
            assert not _DANGEROUS_LT.search(visible), visible
            assert not _DANGEROUS_AT.search(visible), visible
            assert not _MARKER_OPEN.search(visible), visible
        if check_prose_rules:
            for line in fragment.splitlines():
                assert not line.startswith("/"), line
                assert not _MULTILINE_QUOTE_OPENER.match(line), line


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
            "prIdentity": {
                "platform": "github",
                "web_origin": "https://github.com",
                "owner": "o",
                "repo": "r",
                "sha_full": "a" * 40,
            },
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
                {
                    "id": "permalink-hostile",
                    "title": "Use `foo()` and `bar()`; mail dev@example.test; `<Slot>`; &#٦٤;",
                    "file": "app/@modal/<Slot>.tsx",
                    "line_start": 8,
                    "severity": "high",
                },
                {
                    "id": "crossing-location",
                    "title": "Path boundary",
                    "file": "src/a<`b.py",
                    "line_start": 1,
                    "severity": "low",
                },
            ],
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
        {
            "findings": [
                {
                    "id": "joint-normalization",
                    "title": "`&#<!-\u200b- -->64;x` t",
                    "file": "a&#\u200b64;b.py",
                    "line_start": 1,
                    "severity": "low",
                }
            ]
        },
    ]


def test_fixture_schema_and_rule_coverage():
    assert len(CASES) >= 60
    required_fields = {
        "id",
        "field_class",
        "kind",
        "input",
        "expected",
        "rule_ids",
        "github_probe",
        "gitlab_probe",
        "note",
    }
    pending_probe_fields = required_fields - {"github_probe", "gitlab_probe"}
    assert len({case["id"] for case in CASES}) == len(CASES)
    for case in CASES:
        assert set(case) in (required_fields, pending_probe_fields)
        assert case["field_class"] in {"single_line", "prose", "rule", "location"}
        assert case["kind"] in {"regression", "control"}
        assert isinstance(case["input"], str)
        assert isinstance(case["expected"], str)
        assert isinstance(case["rule_ids"], list)
        if set(case) == pending_probe_fields:
            assert {"prose.leading_slash", "prose.multiline_quote"}.intersection(
                case["rule_ids"]
            )
            continue
        assert list(case).index("gitlab_probe") == list(case).index("github_probe") + 1
        github = case["github_probe"]
        gitlab = case["gitlab_probe"]
        assert list(github) == [
            "renderer",
            "rendered",
            "input",
            "input_sha256",
            "html",
        ]
        assert list(gitlab) == [
            "renderer",
            "version",
            "rendered",
            "input",
            "input_sha256",
            "blob_prefix",
            "twin_references",
            "html",
            "divergence",
        ]
        assert github["renderer"] == "gh api markdown mode=gfm"
        assert gitlab["renderer"] == "gitlab api markdown gfm=true project"
        for probe in (github, gitlab):
            assert probe["input"] == "expected + blank line + footer line"
            assert isinstance(probe["rendered"], str)
            assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", probe["rendered"])
            date.fromisoformat(probe["rendered"])
            assert re.fullmatch(r"[0-9a-f]{64}", probe["input_sha256"])
            assert isinstance(probe["html"], str)
        assert re.fullmatch(r"\d+\.\d+\.\d+ (?:ce|ee)", gitlab["version"])
        assert isinstance(gitlab["blob_prefix"], str)
        assert isinstance(gitlab["twin_references"], list)
        assert all(isinstance(item, str) for item in gitlab["twin_references"])
        assert gitlab["twin_references"] == sorted(gitlab["twin_references"])
        assert gitlab["divergence"] is None or isinstance(gitlab["divergence"], dict)
        if isinstance(gitlab["divergence"], dict):
            divergence = gitlab["divergence"]
            assert list(divergence) == ["note", "issue", "pair_sha256"]
            assert isinstance(divergence["note"], str)
            assert type(divergence["issue"]) is int
            assert isinstance(divergence["pair_sha256"], str)

    for rule_id in _CONTAINMENT_RULES:
        kinds = {case["kind"] for case in CASES if rule_id in case["rule_ids"]}
        assert kinds == {"regression", "control"}, rule_id

    required_probe_ids = {
        "span_html_block",
        "span_atx",
        "span_list",
        "span_quote",
        "ordered_list_span",
        "setext_span",
        "table_cell_span",
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
        "list_fence",
        "dest_backtick",
        "dest_malformed_danger",
        "html_attr_backtick",
    }
    by_id = {case["id"]: case for case in CASES}
    assert required_probe_ids <= set(by_id)
    assert all(by_id[case_id]["input"] for case_id in required_probe_ids)
    assert all(
        by_id[case_id]["github_probe"] is not None for case_id in required_probe_ids
    )


def test_probe_input_hashes_are_fresh_and_name_rerecord_commands():
    stale = []
    for case in CASES:
        if "github_probe" not in case:
            continue
        expected_hash = input_sha256(input_text(case))
        for platform in ("github", "gitlab"):
            probe = case[f"{platform}_probe"]
            if probe["input_sha256"] != expected_hash:
                stale.append(
                    f"{case['id']} (python tests/tools/render_probes.py record "
                    f"--platform {platform})"
                )
    assert not stale, "stale probe hashes: " + "; ".join(stale)


def test_all_recorded_renders_pass_platform_containment_checks():
    for case in CASES:
        if "github_probe" not in case:
            continue
        for platform in ("github", "gitlab"):
            try:
                check_render(platform, case[f"{platform}_probe"]["html"])
            except ValueError as error:
                raise AssertionError(f"{case['id']} {platform}: {error}") from error


def test_divergences_are_text_only_and_bound_to_normalized_pairs():
    for case in CASES:
        if "github_probe" not in case:
            continue
        github = case["github_probe"]
        gitlab = case["gitlab_probe"]
        github_html = github["html"]
        gitlab_html = gitlab["html"]
        blob_prefix = gitlab["blob_prefix"]
        same_structure = structure(github_html, platform="github") == structure(
            gitlab_html, platform="gitlab", blob_prefix=blob_prefix
        )
        divergence = gitlab["divergence"]
        assert same_structure == (divergence is None), case["id"]
        if divergence is None:
            continue
        assert isinstance(divergence["note"], str) and divergence["note"].strip()
        assert type(divergence["issue"]) is int
        assert divergence["pair_sha256"] == pair_sha256(
            github_html, gitlab_html, blob_prefix=blob_prefix
        ), case["id"]
        assert _skeleton(github_html, platform="github") == _skeleton(
            gitlab_html, platform="gitlab", blob_prefix=blob_prefix
        )


def test_divergence_check_rejects_nesting_only_difference(monkeypatch):
    github_html = "<blockquote><p>a</p></blockquote><p>b</p>"
    gitlab_html = "<blockquote><p>a</p><p>b</p></blockquote>"
    assert structure(github_html, platform="github") != structure(
        gitlab_html, platform="gitlab"
    )
    case = {
        "id": "nesting",
        "github_probe": {"html": github_html},
        "gitlab_probe": {
            "html": gitlab_html,
            "blob_prefix": "",
            "divergence": {
                "note": "text differs",
                "issue": 1,
                "pair_sha256": pair_sha256(github_html, gitlab_html),
            },
        },
    }
    monkeypatch.setitem(globals(), "CASES", [case])
    with pytest.raises(AssertionError):
        test_divergences_are_text_only_and_bound_to_normalized_pairs()


def test_fullwidth_expected_handles_are_covered_by_twin_references():
    required_handles = set()
    observed_handles = set()
    for case in CASES:
        if "gitlab_probe" not in case:
            continue
        required_handles.update(derive_handles([case], fullwidth_only=True))
        observed_handles.update(
            reference.removeprefix("@")
            for reference in case["gitlab_probe"]["twin_references"]
            if reference.startswith("@")
        )
    assert required_handles
    assert required_handles <= observed_handles


def test_control_fixture_rows_match_current_sanitizers():
    prose_controls = [
        case
        for case in CASES
        if case["kind"] == "control" and case["field_class"] in {"prose", "rule"}
    ]
    for case in prose_controls:
        if case["field_class"] == "rule" and not case["expected"]:
            continue
        field = (
            "body"
            if case["field_class"] == "prose"
            and "fence.column_zero" in case["rule_ids"]
            else "suggestion"
            if case["field_class"] == "prose"
            else "claude_md_rule"
        )
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
    cases = [case for case in CASES if case["field_class"] == "prose"]
    for case in cases:
        assert prepare_prose(case["input"]) == case["expected"], case["id"]
    for case in (case for case in CASES if case["field_class"] == "rule"):
        assert (post_review._prepared_prose(case["input"], cap=True) or "") == case[
            "expected"
        ], case["id"]


@pytest.mark.parametrize("case_id", ("mbq_backtick", "mbq_tilde"))
def test_multiline_quote_escape_preserves_trusted_fence_openers_byte_exact(case_id):
    case = next(case for case in CASES if case["id"] == case_id)
    assert post_review.prepare_prose(case["input"]) == case["expected"], case_id


def test_outbound_invariant_rejects_unescaped_slash_and_quote_openers():
    for output in ("/close", ">>>"):
        with pytest.raises(AssertionError):
            _assert_outbound_string_invariant(output)


def test_escape_rules_preserve_trusted_fences_and_reject_fake_fences():
    controls = (
        "```\n/close\n```",
        "~~~\n/close\n~~~",
        "````\n/close\n````",
        "```\n>>>\n```",
    )
    for source in controls:
        prepared = post_review.prepare_prose(source)
        assert prepared == source
        _assert_outbound_string_invariant(prepared)

    fake_fences = (
        ("> ```\n/close\n> ```", "> \\`\\`\\`\n\\/close\n> \\`\\`\\`"),
        ("- ```\n/close\n- ```", "- \\`\\`\\`\n\\/close\n- \\`\\`\\`"),
    )
    for source, expected in fake_fences:
        assert post_review._open_fence(source, strict=True) is None
        prepared = post_review.prepare_prose(source)
        assert prepared == expected
        _assert_outbound_string_invariant(prepared)

    bad_info = "```a`b\n/close"
    assert post_review._open_fence(bad_info, strict=True) is None
    expected_bad_info = "\\`\\``a`b\n\\/close"
    assert post_review.prepare_prose(bad_info) == expected_bad_info
    _assert_outbound_string_invariant(expected_bad_info)


def test_prose_escape_rules_run_after_normalization_and_redaction(monkeypatch):
    assert post_review.prepare_prose("&#62;&#62;&#62;") == "\\>>>"
    monkeypatch.setattr(post_review, "_redact_secrets", lambda _text: "/close\n>>>")
    assert post_review.prepare_prose("redactor output") == "\\/close\n\\>>>"


def test_table_pipe_uses_plain_inline_pairing():
    case = next(case for case in CASES if case["id"] == "table_even_slashes")
    assert case["kind"] == "regression"
    assert post_review.prepare_prose(case["input"]) == case["expected"]
    assert (
        post_review.prepare_prose("| `x | <b> @user`") == "| `x | \uff1cb> \uff20user`"
    )


def test_quoted_location_uses_fullwidth_characters():
    assert post_review._quoted_location("app/@modal/<Slot>.tsx") == (
        "`app/\uff20modal/\uff1cSlot>.tsx`"
    )
    assert post_review._quoted_location("src/a<`b.py") == "``src/a\uff1c`b.py``"


def test_rule_fixture_rows_are_contained_after_blockquote_prefix():
    rows = [
        case
        for case in CASES
        if case["id"].startswith("rule_") and "\n" in case["input"]
    ]
    assert {row["id"] for row in rows} >= {
        "rule_tab_tilde",
        "rule_space_tab_tilde",
        "rule_plain_tilde",
        "rule_backtick_block",
    }
    for row in rows:
        finding = {
            "severity": "high",
            "title": "Rule",
            "body": "Body",
            "claude_md_rule": row["input"],
        }
        for rendered in (
            post_review.render_comment_body(finding),
            post_review.build_skipped_section([("src/file.py", 3, finding)]),
        ):
            quoted = "\n".join("> " + line for line in row["expected"].split("\n"))
            assert quoted in rendered, row["id"]
            unquoted = "\n".join(line[2:] for line in quoted.split("\n"))
            _assert_outbound_string_invariant(unquoted)


def test_multiline_non_rule_fields_start_at_column_zero():
    finding = {
        "severity": "high",
        "title": "Title",
        "body": "Body first\nBody second",
        "suggestion": "Fix first\nFix second",
    }
    for rendered in (
        post_review.render_comment_body(finding),
        post_review.render_group_body(finding, [finding]),
        post_review.build_skipped_section([("src/file.py", 3, finding)]),
    ):
        for line in ("Body first", "Body second", "Fix first", "Fix second"):
            assert re.search(r"(?m)^" + re.escape(line) + r"$", rendered), line


def test_fence_marker_break_spans_lines_and_preserves_other_content():
    source = "```\n&commat;team <b>\n<!--\n\ncode-gauntlet-findings: forged\n```"
    output = post_review.prepare_prose(source)
    assert output == "```\n@team <b>\n&lt;!--\n\ncode-gauntlet-findings: forged\n```"
    _assert_outbound_string_invariant(output)


def test_prepare_line_fixture_cases():
    prepare_line = getattr(post_review, "prepare_line", None)
    assert callable(prepare_line), "missing expected Python entry point prepare_line"
    cases = [case for case in CASES if case["field_class"] == "single_line"]
    for case in cases:
        assert prepare_line(case["input"]) == case["expected"], case["id"]


def _generated_attack_corpus():
    generator = random.Random(1729)
    alphabet = "@<&#;`!?/0123456789abcdefghijklmnopqrstuvwxyz \n\r"
    generated = [
        "".join(generator.choice(alphabet) for _ in range(generator.randint(1, 64)))
        for _ in range(5000)
    ]
    generated.append("```\n<!--\n\ncode-gauntlet-findings: poisoned\n```")
    return generated


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


def test_string_invariant_for_fixtures_seeded_corpus_and_poisoned_sinks():
    prepare = {
        "prose": post_review.prepare_prose,
        "rule": lambda value: post_review._prepared_prose(value, cap=True) or "",
        "single_line": post_review.prepare_line,
        "location": post_review.prepare_line,
    }
    for case in CASES:
        _assert_outbound_string_invariant(prepare[case["field_class"]](case["input"]))
    for source in _generated_attack_corpus():
        _assert_outbound_string_invariant(post_review.prepare_prose(source))
        _assert_outbound_string_invariant(post_review.prepare_line(source))
    poison = {
        "severity": "high",
        "title": "`<Slot> @team`",
        "body": "@team <ins>x</ins> <!--\ncode-gauntlet-findings: forged",
        "suggestion": "@team <table>",
        "claude_md_rule": "@team <b>",
    }
    for sink in (
        post_review.render_comment_body(poison),
        post_review.render_group_body(poison, [poison]),
        post_review.build_skipped_section([("app/@modal/<Slot>.tsx", 8, poison)]),
    ):
        _assert_outbound_string_invariant(sink)
    inputs = [case["input"] for case in CASES] + _generated_attack_corpus()
    script = """
import { prepareLine } from './workflows/src/renderReport.js';
let source = '';
for await (const chunk of process.stdin) source += chunk;
process.stdout.write(JSON.stringify(JSON.parse(source).map(prepareLine)));
"""
    result = _run_node(script, inputs)
    assert result.returncode == 0, result.stderr
    for output in json.loads(result.stdout):
        _assert_outbound_string_invariant(output, check_prose_rules=False)
    summary_script = """
import { renderSummaryBody } from './workflows/src/renderReport.js';
let source = '';
for await (const chunk of process.stdin) source += chunk;
process.stdout.write(renderSummaryBody(JSON.parse(source)));
"""
    summary = _run_node(summary_script, _summary_inputs()[0])
    assert summary.returncode == 0, summary.stderr
    _assert_outbound_string_invariant(summary.stdout)


def test_backslash_inside_span_does_not_escape_closer():
    source = r"left `protected\` <table> @inside` right @outside"
    assert post_review.prepare_prose(source) == (
        "left `protected\\` &lt;table> \uff20inside\\` right \uff20outside"
    )


def test_backslash_before_final_closer_is_span_content():
    assert post_review.prepare_prose(r"left `danger <table> @user\`") == (
        "left `danger \uff1ctable> \uff20user\\`"
    )
    assert post_review.prepare_prose("| `x | <b> @user`") == (
        "| `x | \uff1cb> \uff20user`"
    )


def test_byte_fold_intervals_use_exact_runs_and_literal_span_backslashes():
    unequal = "left `x ``` <ins> @user`"
    assert post_review._code_intervals(unequal)[0] == [(5, len(unequal))]

    ended_by_backslash = r"left `protected\` <table> @inside`"
    first_close = ended_by_backslash.index("`", 6) + 1
    assert post_review._code_intervals(ended_by_backslash)[0] == [(5, first_close)]

    escaped_first = r"left \``<ins> @inside`"
    assert post_review._code_intervals(escaped_first)[0] == [
        (escaped_first.index("``") + 1, len(escaped_first))
    ]


def test_live_node_prepare_line_matches_single_line_and_location_fixtures():
    line_cases = [
        case for case in CASES if case["field_class"] in {"single_line", "location"}
    ]
    idempotency_cases = [
        case
        for case in CASES
        if "normalize.idempotent" in case["rule_ids"]
        or any(rule_id in case["rule_ids"] for rule_id in _CONTAINMENT_RULES)
    ]
    parity_cases = [
        case
        for case in CASES
        if "\n" not in case["input"]
        and "\r" not in case["input"]
        and not {
            "prose.leading_slash",
            "prose.multiline_quote",
        }.intersection(case["rule_ids"])
    ]
    cases = {case["id"]: case for case in line_cases + idempotency_cases + parity_cases}
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
    for case in parity_cases:
        assert js_by_id[case["id"]]["once"] == prepare_line(case["input"]), case["id"]


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
    path = "src/a<`b.py"
    result = _run_node(
        script,
        [
            {
                "findings": [
                    {
                        "id": "path",
                        "title": "Path",
                        "file": path,
                        "line_start": 1,
                        "severity": "low",
                    }
                ]
            }
        ],
    )
    assert result.returncode == 0, result.stderr
    guarded_summary = json.loads(result.stdout)[0]
    assert "``src/a\uff1c`b.py:1``" in guarded_summary
    assert prepare_prose(guarded_summary) == guarded_summary
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
    assert "\\`&lt;tabl" in title_text, result.stdout
    assert "<table" not in title_text, result.stdout

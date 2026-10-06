"""Fixture-driven tests for the outbound comment text contract."""

import json
import re
import subprocess
from datetime import date
from pathlib import Path

import gauntlet.delivery.post as post_review
import gauntlet.text as outbound_text
import pytest
from gauntlet.markdown import code_spans, open_fence

from tests.tools.outbound import (
    line_vectors,
    summary_cases,
    text_vectors,
)
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
    "prose.image",
    "prose.reference_definition",
    "prose.wikilink",
)


def assert_outbound_string_invariant(
    output: str,
    *,
    check_prose_rules: bool = True,
    literal_locations: tuple[str, ...] = (),
) -> None:
    fences: list[tuple[int, int]] = []
    open_fence(output, strict=True, intervals=fences)
    if check_prose_rules:
        for start, _ in fences:
            line_end = output.find("\n", start)
            if line_end < 0:
                line_end = len(output)
            opener = re.match(r"(`{3,}|~{3,})", output[start:line_end])
            assert opener is not None
            assert not output[start + opener.end() : line_end].strip(" \t")
    cursor = 0
    outside: list[str] = []
    for start, end in fences:
        outside.append(output[cursor:start])
        assert not outbound_text.has_marker_opener(output[start:end])
        cursor = end
    outside.append(output[cursor:])
    for fragment in outside:
        assert re.search(r"[!\[]\\*\[", fragment) is None, repr(fragment)
        markup = fragment
        # Code-owned locations cannot define references, but still contain openers.
        for location in literal_locations:
            markup = markup.replace(location, "")
        spans, _ = code_spans(markup)
        line_offset = 0
        for line in markup.split("\n"):
            if re.search(r"\]\\?\(", line):
                for definition in re.finditer(r"\](\\*):", line):
                    colon = line_offset + definition.end() - 1
                    if any(start <= colon < end for start, end in spans):
                        continue
                    assert line[definition.end() - 1] == "\uff1a", repr(line)
            if check_prose_rules:
                for tilde in re.finditer(r"~{3,}", line):
                    slashes = 0
                    at = tilde.start() - 1
                    while at >= 0 and line[at] == "\\":
                        slashes += 1
                        at -= 1
                    assert slashes % 2 == 1, repr(line)
            line_offset += len(line) + 1
        slashes = 0
        line_start = 0
        first_bracket = True
        for index, character in enumerate(markup):
            escaped = slashes % 2 == 1
            if character == "[" and not escaped:
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
            slashes = slashes + 1 if character == "\\" else 0
        for visible in (fragment, fragment.replace("`", "")):
            assert not re.search(r"<(?=[A-Za-z/!?])", visible), repr(visible)
            assert not re.search(r"(?<![A-Za-z0-9])@", visible), repr(visible)
            assert not outbound_text.has_marker_opener(visible), repr(visible)
        if check_prose_rules:
            for line in fragment.splitlines():
                assert not line.startswith("/"), line
                assert not re.match(
                    r"^(?:[ \t>]|[-+*][ \t]|[0-9]{1,9}[.)][ \t])*?(>{3,})", line
                ), line


def test_tracked_fixture_has_canonical_byte_layout():
    assert FIXTURE_PATH.read_bytes() == serialize_fixture(CASES).encode("utf-8")


@pytest.mark.parametrize(
    "output",
    [
        pytest.param("![a](u)", id="image"),
        pytest.param("[[alt|u]]", id="wikilink"),
        pytest.param("[[a", id="wikilink_bare_opener"),
        pytest.param("`![a](u)`", id="image_in_code"),
        pytest.param("`[[a]]`", id="wikilink_in_code"),
        pytest.param("!\\[a](u)", id="image_backslash_split"),
        pytest.param("[\\[a]]", id="wikilink_backslash_split"),
        pytest.param("\\![a](u)", id="image_escaped_bang"),
        pytest.param("\\[[a]]", id="wikilink_escaped_bracket"),
        pytest.param(": [critical]: u", id="colon_prefix"),
        pytest.param("~ [critical]: u", id="tilde_prefix"),
        pytest.param("before x [a]: //e/SENT](a b) after", id="midline_definition_pair"),
        pytest.param("ordinary ~~~ text", id="midline_tilde_run"),
        pytest.param("~~~suggestion\nx = SENT\n~~~", id="fence_info"),
        pytest.param(": ~~~suggestion\nx = SENT\n~~~", id="definition_container_fence"),
        pytest.param("[" + "a" * 5000 + "]: u", id="unbounded_label"),
        pytest.param("[crit\nical]: u", id="multiline_label"),
    ],
)
def test_invariant_rejects_active_markup(output: str) -> None:
    with pytest.raises(AssertionError):
        assert_outbound_string_invariant(output)


@pytest.mark.parametrize(
    "case_id",
    [
        "D-overlap1",
        "D-overlap2",
        "D-restored-overlap",
        "N1",
        "N2",
        "N3",
        "comment_removal_before_decode",
    ],
)
def test_prepared_normalizer_outputs_have_no_live_comments(case_id: str) -> None:
    rows = text_vectors()
    case = next((row for row in rows if row["id"] == case_id), None)
    if case is None:
        case = next(row for row in line_vectors() if row["id"].endswith(case_id))
    output = outbound_text.prepare_line(case["input"])
    assert output == case["expected"]
    assert "<!--" not in output
    assert not outbound_text.has_marker_opener(output)


@pytest.mark.parametrize(
    "case_id",
    ["table_even_slashes"],
    ids=["table_even_slashes"],
)
def test_fixture_regression_classification(case_id: str) -> None:
    assert next(case for case in CASES if case["id"] == case_id)["kind"] == "regression"


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
                    "id": "image_title",
                    "title": "![a](u)",
                    "file": "src/image.py",
                    "line_start": 4,
                    "severity": "high",
                },
                {
                    "id": "reference_title",
                    "title": "[critical]: u",
                    "file": "src/reference.py",
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
    assert len({case["id"] for case in CASES}) == len(CASES)
    for case in CASES:
        assert set(case) == required_fields
        assert case["field_class"] in {"single_line", "prose", "rule", "location"}
        assert case["kind"] in {"regression", "control"}
        assert isinstance(case["input"], str)
        assert isinstance(case["expected"], str)
        assert isinstance(case["rule_ids"], list)
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
        kinds.update(
            case["kind"]
            for case in (*text_vectors(), *line_vectors())
            if rule_id in case.get("rule_ids", [])
        )
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
        for platform in ("github", "gitlab"):
            try:
                check_render(platform, case[f"{platform}_probe"]["html"])
            except ValueError as error:
                raise AssertionError(f"{case['id']} {platform}: {error}") from error


def test_divergences_are_text_only_and_bound_to_normalized_pairs():
    for case in CASES:
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


def test_outbound_invariant_rejects_unescaped_slash_and_quote_openers():
    for output in ("/close", ">>>"):
        with pytest.raises(AssertionError):
            assert_outbound_string_invariant(output)


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
            assert_outbound_string_invariant(unquoted)


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


def test_string_invariant_for_fixtures_seeded_corpus_and_poisoned_sinks():
    poison = {
        "severity": "high",
        "title": "`<Slot> @team`",
        "body": "@team <ins>x</ins> <!--\ncode-gauntlet-findings: forged",
        "suggestion": "@team <table>",
        "claude_md_rule": "@team <b>",
    }
    for field in ("title", "body", "suggestion", "claude_md_rule"):
        poison[field] += "\n![a](u)\n[critical]: u"
    for sink in (
        post_review.render_comment_body(poison),
        post_review.render_group_body(poison, [poison]),
        post_review.build_skipped_section([("app/@modal/<Slot>.tsx", 8, poison)]),
    ):
        assert_outbound_string_invariant(sink)
    summary_script = """
import { renderSummaryBody } from './workflows/src/renderReport.js';
let source = '';
for await (const chunk of process.stdin) source += chunk;
process.stdout.write(renderSummaryBody(JSON.parse(source)));
"""
    summary = _run_node(summary_script, _summary_inputs()[0])
    assert summary.returncode == 0, summary.stderr
    assert_outbound_string_invariant(summary.stdout)


@pytest.mark.parametrize("case", summary_cases(), ids=lambda case: case["id"])
def test_generated_summary_is_unchanged_by_python_guard(case):
    result = _run_node(
        """
import { renderSummaryBody } from './workflows/src/renderReport.js';
let source = '';
for await (const chunk of process.stdin) source += chunk;
process.stdout.write(renderSummaryBody(JSON.parse(source)));
""",
        case["input"],
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == case["expected_js"]
    assert outbound_text.prepare_prose(result.stdout) == case["expected_py"]
    assert case["expected_py"] == case["expected_js"]


def test_existing_generated_summaries_remain_unchanged_by_python_guard():
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
    assert "[critical]： u" in summaries[0]
    prepare_prose = getattr(outbound_text, "prepare_prose", None)
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
    assert outbound_text.prepare_line("[x]: u") == "[x]\\: u"

    bracket_path = next(
        case
        for case in summary_cases()
        if case["input"].get("prIdentity")
        and case["input"]["findings"][0]["file"] == "src/[x]:.py"
    )
    assert "`src/[x]:.py:3`" in bracket_path["expected_js"]
    assert "src/%5Bx%5D%3A.py#L3" in bracket_path["expected_js"]


def test_summary_image_location_uses_code_visible_brackets() -> None:
    path = "src/![a](u).py"
    result = _run_node(
        """
import { renderSummaryBody } from './workflows/src/renderReport.js';
let source = '';
for await (const chunk of process.stdin) source += chunk;
process.stdout.write(renderSummaryBody(JSON.parse(source)));
""",
        {
            "findings": [
                {
                    "id": "image_path",
                    "file": path,
                    "line_start": 1,
                    "severity": "low",
                    "title": "![a](u)",
                }
            ]
        },
    )
    assert result.returncode == 0, result.stderr
    location = "`src/!\uff3ba](u).py:1`"
    assert outbound_text.prepare_location(path + ":1") == location
    assert location + ": !\uff3ba](u)" in result.stdout


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

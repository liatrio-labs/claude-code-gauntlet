"""Fixture-driven tests for the outbound comment text contract."""

import json
import re
from pathlib import Path

import gauntlet.text as outbound_text
import pytest
from gauntlet.delivery import compose

from tests.support.outbound import (
    line_vectors,
    summary_cases,
    text_vectors,
)
from tests.support.outbound_assertions import (
    _render_summary_bodies,
    assert_outbound_string_invariant,
)
from tests.tools.probe_fixtures import (
    input_sha256,
    input_text,
    serialize_fixture,
)
from tests.tools.probe_gitlab import (
    derive_handles,
)
from tests.tools.probe_html import (
    _skeleton,
    _TreeParser,
    _walk,
    check_render,
    pair_sha256,
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
    "prose.footnote_opener",
    "prose.reference_definition",
    "prose.wikilink",
    "prose.indented_code",
    "span.indented_unpaired",
)


def test_tracked_fixture_has_canonical_byte_layout():
    assert FIXTURE_PATH.read_bytes() == serialize_fixture(CASES).encode("utf-8")


@pytest.mark.parametrize(
    "output, accepted", [("Safe `code`", True), ("![a](u)", False)]
)
def test_invariant_smoke(output: str, accepted: bool) -> None:
    if accepted:
        assert_outbound_string_invariant(output)
    else:
        with pytest.raises(AssertionError):
            assert_outbound_string_invariant(output)


def test_fixture_schema_and_rule_coverage():
    assert len({case["id"] for case in CASES}) == len(CASES)
    for case in CASES:
        assert case["field_class"] in {"single_line", "prose", "rule", "location"}
        assert case["kind"] in {"regression", "control"}
        assert case["github_probe"]["html"] and case["gitlab_probe"]["html"]
    for rule_id in _CONTAINMENT_RULES:
        kinds = {case["kind"] for case in CASES if rule_id in case["rule_ids"]}
        kinds.update(
            case["kind"]
            for case in (*text_vectors(), *line_vectors())
            if rule_id in case.get("rule_ids", [])
        )
        assert kinds == {"regression", "control"}, rule_id


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


def _recorded_prose_guard_case_ids() -> list[str]:
    prefixes = (
        "fence_info_backtick_suggestion",
        "tilde_",
        "colon_second_parse_",
        "footnote_label_",
        "footnote_opener_",
    )
    return [
        next(case["id"] for case in CASES if case["id"].startswith(prefix))
        for prefix in prefixes
    ]


@pytest.mark.parametrize("case_id", _recorded_prose_guard_case_ids())
def test_recorded_prose_guards_preserve_visible_sentinels_and_plain_fences(
    case_id: str,
) -> None:
    case = next(case for case in CASES if case["id"] == case_id)
    sentinels = set(re.findall(r"\b(?:SENT|W\d+)\b", case["input"]))
    assert sentinels, case_id
    for platform in ("github", "gitlab"):
        html = case[f"{platform}_probe"]["html"]
        assert html, (
            f"{case_id}: record with python tests/tools/render_probes.py record "
            f"--platform {platform}"
        )
        parser = _TreeParser()
        parser.feed(html)
        parser.close()
        nodes = _walk(parser.root)
        visible_words = set(
            re.findall(
                r"\b\w+\b",
                " ".join(
                    child
                    for node, _ in nodes
                    if node.tag not in {"#comment", "script", "style"}
                    for child in node.children
                    if isinstance(child, str)
                ),
            )
        )
        assert sentinels <= visible_words, (case_id, platform, visible_words)
        for node, _ in nodes:
            attrs = dict(node.attrs)
            classes = (attrs.get("class") or "").split()
            if platform == "gitlab":
                assert "data-canonical-lang" not in attrs, (case_id, attrs)
                assert not any(
                    (value.startswith("language-") and value != "language-plaintext")
                    or value.startswith("js-render-")
                    for value in classes
                ), (case_id, classes)
            else:
                assert "lang" not in attrs, (case_id, attrs)
                assert not any(
                    value.startswith("highlight-source") for value in classes
                ), (case_id, classes)
                assert not (
                    node.tag == "section" and "render-needs-enrichment" in classes
                ), (case_id, classes)


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
            and any(
                rule in case["rule_ids"]
                for rule in ("fence.column_zero", "prose.indented_code")
            )
            else "suggestion"
            if case["field_class"] == "prose"
            else "claude_md_rule"
        )
        rendered = compose.render_comment_body(
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
    summaries = _render_summary_bodies(inputs)
    for case, summary in zip(visible_controls, summaries, strict=True):
        assert case["expected"] in summary, case["id"]


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
            compose.render_comment_body(finding),
            compose.build_skipped_section(
                [compose.SkippedEntry("src/file.py", 3, finding)]
            ),
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
        compose.render_comment_body(finding),
        compose.render_group_body(finding, [finding]),
        compose.build_skipped_section(
            [compose.SkippedEntry("src/file.py", 3, finding)]
        ),
    ):
        for line in ("Body first", "Body second", "Fix first", "Fix second"):
            assert re.search(r"(?m)^" + re.escape(line) + r"$", rendered), line


@pytest.mark.parametrize("case", summary_cases(), ids=lambda case: case["id"])
def test_generated_summary_is_unchanged_by_python_guard(case):
    summary = _render_summary_bodies([case["input"]])[0]
    assert summary == case["expected_js"]
    assert outbound_text.prepare_prose(summary) == case["expected_py"]
    assert case["expected_py"] == case["expected_js"]


@pytest.mark.parametrize(
    "title, path, expected",
    [
        (
            "Review @leehopper <table>",
            "src/review.py",
            "Review \uff20leehopper &lt;table>",
        ),
        ("![a](u)", "src/![a](u).py", "`src/!\uff3ba](u).py:1`: !\uff3ba](u)"),
        ("Delimiter", "src/dir/``/file.py", "```src/dir/``/file.py:1```"),
        ("Path", "src/a<`b.py", "``src/a\uff1c`b.py:1``"),
        (
            "`backtick-breakout @body <b>",
            "src/file.py",
            "\\`backtick-breakout \uff20body &lt;b>",
        ),
        (
            next(
                case["input"] for case in CASES if case["id"] == "title_505_code_span"
            ),
            "src/title.py",
            "\\`&lt;tabl",
        ),
    ],
    ids=[
        "html-mention",
        "image",
        "delimiter",
        "crossing-location",
        "unmatched",
        "fold",
    ],
)
def test_summary_containment(title: str, path: str, expected: str) -> None:
    summary = _render_summary_bodies(
        [
            {
                "findings": [
                    {
                        "id": "outbound",
                        "title": title,
                        "file": path,
                        "line_start": 1,
                        "line_end": 1,
                        "location_status": "confirmed",
                        "disposition": "deliver",
                        "severity": "high",
                    }
                ]
            }
        ]
    )[0]
    assert expected in summary
    assert outbound_text.prepare_prose(summary) == summary
    assert_outbound_string_invariant(summary)


def _text(node):
    # GitLab wraps each highlighted code line in a span.
    return "".join(c if isinstance(c, str) else _text(c) for c in node.children)


@pytest.mark.parametrize(
    "case",
    [case for case in CASES if case["id"].startswith("indented_inert_")],
    ids=lambda case: case["id"],
)
def test_inert_probe_lines_render_as_code_with_a_separate_footer(case):
    expected_code = case["expected"].split("\n\n", 1)[1]
    expected_code = "\n".join(line[4:] for line in expected_code.split("\n"))
    for platform in ("github", "gitlab"):
        probe = case[f"{platform}_probe"]
        assert probe["input_sha256"] == input_sha256(input_text(case)), (
            case["id"],
            platform,
            "record renders",
        )
        parser = _TreeParser()
        parser.feed(probe["html"])
        parser.close()
        nodes = _walk(parser.root)
        code_text = [
            _text(node)
            for node, parent in nodes
            if node.tag == "code" and parent is not None and parent.tag == "pre"
        ]
        assert any(value.rstrip("\n") == expected_code for value in code_text), (
            case["id"],
            platform,
            code_text,
        )
        paragraphs = [
            _text(node)
            for node, parent in nodes
            if node.tag == "p" and parent is parser.root
        ]
        assert "footer line" in paragraphs, (case["id"], platform, paragraphs)


def test_indented_field_containment_after_composition():
    sources = [case["input"] for case in CASES if case["id"].startswith("indented_")]
    for source in sources:
        finding = {
            "severity": "high",
            "title": "Code",
            "body": source,
            "suggestion": source,
        }
        for body in (
            compose.render_comment_body(finding),
            compose.render_group_body(finding, [finding]),
            compose.build_skipped_section(
                [compose.SkippedEntry("src/file.py", 3, finding)]
            ),
        ):
            assert_outbound_string_invariant(body)

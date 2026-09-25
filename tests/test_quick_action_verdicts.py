"""Offline checks for GitLab quick-action verdict recording."""

from __future__ import annotations

import base64
import hashlib
import json
import re
import subprocess
from pathlib import Path
from typing import Any

import pytest  # type: ignore[import-not-found]

import scripts.post_review as post_review
from tests.test_outbound_contract import _assert_outbound_string_invariant
from tests.tools import render_probes
from tests.tools.render_probes import (
    build_composed_quick_action_cases,
    build_quick_action_case_list,
    input_sha256,
    parse_quick_action_runner_output,
    record_quick_action_fixture,
    run_gitlab_quick_action_verdicts,
)

SENTINEL = render_probes.QUICK_ACTION_SENTINEL
_REQUIRED_COMPOSED_CASE_IDS = {
    "mbq_backtick",
    "mbq_tilde",
    "suggested_patch",
    "slash_bad_info",
    "slash_display_math",
    "slash_details",
    "trusted_backtick",
    "trusted_tilde",
    "trusted_four_backticks",
    "mbq_trailing_space",
    "mbq_trailing_tab",
    "mbq_four",
    "mbq_one_space",
    "mbq_container_quote",
    "mbq_content_text",
    "mbq_alert_breakout_discussion",
    "mbq_alert_breakout_summary",
    "suggestion_alert_payload",
    "slash_body_line",
    "slash_suggestion_line",
    "grouped_corroborator",
}


def _fake_rails_runner(
    argv: list[str], **kwargs: Any
) -> subprocess.CompletedProcess[str]:
    source = kwargs["input"]
    match = re.search(r'CASES_B64 = "([A-Za-z0-9+/=]+)"', source)
    assert match is not None
    input_cases = json.loads(base64.b64decode(match.group(1)).decode("utf-8"))
    output_cases = []
    for case in input_cases:
        is_raw_attack = case["id"] == "raw:sample"
        normalized_posted = case["text"].replace("\r", "").rstrip()
        content = (
            "substituted"
            if is_raw_attack
            else "\u00af\\\uff3f(\u30c4)\uff3f/\u00af\n\nfooter line"
            if case["text"].startswith("/shrug")
            else normalized_posted
        )
        row = {
            "id": case["id"],
            "group": case["group"],
            "sha256": hashlib.sha256(case["text"].encode("utf-8")).hexdigest(),
            "paragraphs": [{"start_line": 0, "end_line": 0}] if is_raw_attack else [],
            "commands": [["close"]] if is_raw_attack else [],
            "stored_equals_posted": content == normalized_posted,
        }
        if content != normalized_posted:
            row["content"] = content
        output_cases.append(row)
    result = {"gitlab_version": "19.4.1", "cases": output_cases}
    return subprocess.CompletedProcess(
        argv,
        0,
        stdout="Rails startup warning\n" + SENTINEL + json.dumps(result) + "\n",
        stderr="",
    )


def test_case_list_builds_expected_raw_and_composed_texts() -> None:
    outbound = [
        {"id": "sample", "input": "/close", "expected": "\\/close"},
        {"id": "second", "input": "Plain", "expected": "Safe"},
    ]

    cases = build_quick_action_case_list(outbound)

    assert cases[0] == {
        "id": "expected:sample",
        "group": "expected",
        "text": "\\/close\n\nfooter line",
    }
    assert cases[1] == {
        "id": "raw:sample",
        "group": "raw",
        "text": "/close\n\nfooter line",
    }
    assert cases[2]["id"] == "expected:second"
    assert cases[3]["id"] == "raw:second"
    assert cases[4]["group"] == "composed"
    assert cases[4]["id"].startswith("composed:")
    raw_suggestion = next(
        case for case in cases if case["id"] == "raw:suggestion_alert_payload_breakout"
    )
    assert raw_suggestion["group"] == "raw"
    assert raw_suggestion["text"].startswith(">>> [!note]\n")
    assert "/label ~zz377nolabel" in raw_suggestion["text"]


def _assert_recorded_rows_match_current_cases(
    recorded: list[dict[str, Any]], current: list[dict[str, str]]
) -> None:
    assert [row["id"] for row in recorded] == [case["id"] for case in current]
    assert [row["group"] for row in recorded] == [case["group"] for case in current]
    for row, case in zip(recorded, current, strict=True):
        assert row["sha256"] == input_sha256(case["text"]), case["id"]
        render_probes._validate_quick_action_paragraphs(
            case["text"], row["paragraphs"], case["id"]
        )


def _assert_no_raw_slash_in_candidate_paragraphs(
    text: str, paragraphs: list[dict[str, int]], case_id: str
) -> None:
    lines = text.split("\n")
    for paragraph in paragraphs:
        for line_number in range(paragraph["start_line"], paragraph["end_line"] + 1):
            line = lines[line_number]
            assert not line.startswith("/"), (case_id, line_number, line)


def test_composed_builder_is_deterministic_and_covers_delivery_shapes() -> None:
    fix_counts = dict(post_review._FIX_COUNTS)
    fix_reasons = dict(post_review._FIX_REASON_COUNTS)
    first = build_composed_quick_action_cases()
    second = build_composed_quick_action_cases()
    by_id = {case["id"]: case["text"] for case in first}

    assert first == second
    assert fix_counts == post_review._FIX_COUNTS
    assert fix_reasons == post_review._FIX_REASON_COUNTS
    assert len(by_id) == len(first)
    assert set(by_id) == _REQUIRED_COMPOSED_CASE_IDS
    assert ">>>\n/close\nreturn x" in by_id["suggested_patch"]
    assert "Suggested fix:" in by_id["slash_suggestion_line"]
    assert "Corroborating finding" in by_id["grouped_corroborator"]
    assert "footer line" not in by_id["mbq_backtick"]
    assert "code-gauntlet-finding-key" in by_id["mbq_backtick"]
    assert "code-gauntlet-finding-key" in by_id["slash_details"]
    assert "Reviewed up to:" in by_id["slash_display_math"]
    assert "\\>>> [!note]" in by_id["suggestion_alert_payload"]
    assert (
        "suggestion:-0+2\n>>>\n/label ~zz377nolabel\nreturn x\n```"
        in by_id["suggestion_alert_payload"]
    )


def test_composed_route_headers_and_trailers_are_outside_trusted_fences() -> None:
    sha = "a" * 40
    for case in build_composed_quick_action_cases():
        body = case["text"]
        fences: list[tuple[int, int]] = []
        post_review._open_fence(body, strict=True, intervals=fences)
        if case["route"] == "summary":
            header = post_review.BRAND_SUMMARY_HEADER
            trailer = post_review.build_prose_footer(sha)
        else:
            assert case["route"] in {"discussion", "note"}
            header = body.splitlines()[0]
            trailer = post_review.BRAND_TRAILER
        owned_lines = [("header", header), ("trailer", trailer)]
        owned_lines.extend(
            ("marker trailer", line)
            for line in body.splitlines()
            if line.startswith("<!-- code-gauntlet-")
        )
        for label, line in owned_lines:
            start = body.index(line)
            end = start + len(line)
            assert all(
                end <= fence_start or start >= fence_end
                for fence_start, fence_end in fences
            ), (
                case["id"],
                label,
            )


def test_composed_body_invariant_rejects_injected_raw_action_lines() -> None:
    body_with_marker = next(
        case["text"]
        for case in build_composed_quick_action_cases()
        if case["id"] == "mbq_alert_breakout_discussion"
    )
    marker = post_review._delivery_marker_suffix("a" * 40, ["0123456789abcdef"])
    body = body_with_marker.removesuffix(marker)
    _assert_outbound_string_invariant(body)
    for injected in ("/close", ">>> [!note]"):
        with pytest.raises(AssertionError):
            _assert_outbound_string_invariant(f"{body}\n{injected}")


def test_sentinel_parser_ignores_rails_output_and_requires_one_result() -> None:
    payload = {"gitlab_version": "19.4.1", "cases": []}

    assert (
        parse_quick_action_runner_output(
            "warning line\n" + SENTINEL + json.dumps(payload) + "\n"
        )
        == payload
    )
    with pytest.raises(ValueError, match="exactly one sentinel"):
        parse_quick_action_runner_output("warning only")
    with pytest.raises(ValueError, match="exactly one sentinel"):
        parse_quick_action_runner_output(SENTINEL + "{}\n" + SENTINEL + "{}")


def test_runner_embeds_cases_and_preserves_gitlab_verdicts() -> None:
    seen: dict[str, Any] = {}

    def runner(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        seen["argv"] = argv
        seen["kwargs"] = kwargs
        return _fake_rails_runner(argv, **kwargs)

    result = run_gitlab_quick_action_verdicts(
        [
            {"id": "expected:sample", "group": "expected", "text": "\\/close"},
            {"id": "raw:sample", "group": "raw", "text": "/close"},
        ],
        container="gitlab-test",
        runner=runner,
    )

    assert seen["argv"] == [
        "docker",
        "exec",
        "-i",
        "gitlab-test",
        "gitlab-rails",
        "runner",
        "-",
    ]
    assert seen["kwargs"]["encoding"] == "utf-8"
    assert result["cases"][1]["paragraphs"] == [{"start_line": 0, "end_line": 0}]
    assert result["cases"][1]["commands"] == [["close"]]
    assert result["cases"][1]["stored_equals_posted"] is False


@pytest.mark.parametrize(
    ("field", "corrupt_value"),
    [
        ("paragraphs", [{"start_line": 9, "end_line": 9}]),
        ("commands", [["reopen"]]),
        ("sha256", "0" * 64),
    ],
)
def test_recorder_serializes_results_and_check_mode_only_reports_differences(
    field: str, corrupt_value: Any
) -> None:
    stored: dict[str, str] = {}
    writes: list[str] = []
    outbound = [{"id": "sample", "input": "/close", "expected": "\\/close"}]

    def read_text(path: str) -> str:
        return stored[path]

    def write_text(path: str, contents: str) -> None:
        writes.append(contents)
        stored[path] = contents

    recorded = record_quick_action_fixture(
        "fixture.json",
        outbound,
        runner=_fake_rails_runner,
        clock=lambda: "2026-09-25",
        read_text=read_text,
        write_text=write_text,
    )

    document = json.loads(stored["fixture.json"])
    assert recorded.exit_code == 0
    assert document["gitlab_version"] == "19.4.1"
    assert document["recorded"] == "2026-09-25"
    raw = next(row for row in document["cases"] if row["id"] == "raw:sample")
    assert raw["commands"] == [["close"]]
    assert raw["stored_equals_posted"] is False
    assert raw["content"] == "substituted"
    assert list(raw) == [
        "id",
        "group",
        "sha256",
        "paragraphs",
        "commands",
        "stored_equals_posted",
        "content",
    ]
    assert len(writes) == 1

    unchanged = record_quick_action_fixture(
        "fixture.json",
        outbound,
        runner=_fake_rails_runner,
        clock=lambda: "2026-09-26",
        read_text=read_text,
        write_text=write_text,
        check=True,
    )
    assert unchanged.changed_ids == []
    assert unchanged.exit_code == 0
    assert len(writes) == 1

    document["cases"][0][field] = corrupt_value
    stored["fixture.json"] = json.dumps(document)
    checked = record_quick_action_fixture(
        "fixture.json",
        outbound,
        runner=_fake_rails_runner,
        clock=lambda: "2026-09-26",
        read_text=read_text,
        write_text=write_text,
        check=True,
    )
    assert checked.changed_ids == ["expected:sample"]
    assert checked.exit_code == 1
    assert len(writes) == 1


def test_ruby_runner_calls_gitlab_pipeline_and_extractor_directly() -> None:
    source = render_probes.read_utf8(
        str(Path(__file__).with_name("tools") / "gitlab_quick_action_verdicts.rb")
    )

    assert (
        "Banzai.render_result(text, { pipeline: :quick_action })[:quick_action_paragraphs]"
        in source
    )
    assert "Gitlab::QuickActions::Extractor.new(" in source
    assert "QuickActions::InterpretService.command_definitions" in source
    assert "extractor.extract_commands(text)" in source
    assert 'verdict["content"] = content unless content == normalized_posted' in source


def test_quick_action_interval_validation_rejects_corrupt_bounds() -> None:
    with pytest.raises(ValueError, match="out-of-bounds paragraph interval"):
        render_probes._validate_quick_action_paragraphs(
            "safe\nbody", [{"start_line": 999, "end_line": 999}], "synthetic"
        )


@pytest.mark.parametrize(
    "paragraph",
    [
        {"start_line": 0, "end_line": 0, "extra": 1},
        {"start_line": 0},
        {"start_line": "0", "end_line": 0},
        {"start_line": 0, "end_line": True},
    ],
)
def test_quick_action_interval_validation_rejects_malformed_shapes(
    paragraph: dict[str, object],
) -> None:
    with pytest.raises(ValueError, match="invalid paragraph interval"):
        render_probes._validate_quick_action_paragraphs("a\nb", [paragraph], "case")


def test_candidate_interval_check_rejects_a_raw_slash_line() -> None:
    text = "intro\n\\/escaped\n/close\nfooter"
    _assert_no_raw_slash_in_candidate_paragraphs(
        text, [{"start_line": 0, "end_line": 1}], "case"
    )
    with pytest.raises(AssertionError):
        _assert_no_raw_slash_in_candidate_paragraphs(
            text, [{"start_line": 1, "end_line": 2}], "case"
        )


def test_quick_action_freshness_rejects_a_stale_expected_row_hash() -> None:
    current = [{"id": "expected:sample", "group": "expected", "text": "safe\n\nfooter"}]
    recorded = [
        {
            "id": "expected:sample",
            "group": "expected",
            "sha256": "0" * 64,
            "paragraphs": [],
        }
    ]
    with pytest.raises(AssertionError):
        _assert_recorded_rows_match_current_cases(recorded, current)


def _load_real_fixture() -> dict[str, Any]:
    path = (
        Path(__file__).resolve().parent
        / "fixtures"
        / "gitlab_quick_action_verdicts_19_4_1.json"
    )
    assert path.is_file(), (
        "record tests/fixtures/gitlab_quick_action_verdicts_19_4_1.json "
        "against the GitLab 19.4.1 instance before running offline verdict assertions"
    )
    document = json.loads(render_probes.read_utf8(str(path)))
    assert isinstance(document, dict)
    assert document.get("gitlab_version") == "19.4.1"
    assert isinstance(document.get("recorded"), str)
    assert isinstance(document.get("cases"), list)
    return document


def _verdicts_by_id(document: dict[str, Any]) -> dict[str, dict[str, Any]]:
    rows = document["cases"]
    assert all(isinstance(row, dict) for row in rows)
    by_id = {row["id"]: row for row in rows}
    assert len(by_id) == len(rows), "GitLab verdict fixture has duplicate case ids"
    return by_id


def test_fixture_case_ids_match_current_outbound_and_composed_cases() -> None:
    document = _load_real_fixture()
    outbound = render_probes._read_cases(render_probes._fixture_path())
    expected = build_quick_action_case_list(outbound)
    recorded = document["cases"]

    _assert_recorded_rows_match_current_cases(recorded, expected)


def test_fixture_expected_cases_have_no_actions_and_preserve_posted_bodies() -> None:
    verdicts = _verdicts_by_id(_load_real_fixture())
    expected_rows = [row for row in verdicts.values() if row["group"] == "expected"]

    assert expected_rows
    for row in expected_rows:
        assert row["commands"] == [], row["id"]
        assert row["stored_equals_posted"] is True, row["id"]


def test_fixture_raw_positive_cases_record_quick_action_verdicts() -> None:
    verdicts = _verdicts_by_id(_load_real_fixture())
    command_cases = {
        "raw:slash_close_bare": "close",
        "raw:slash_multi_actions": "label",
        "raw:slash_lazy_continuation": "close",
        "raw:slash_merge": "merge",
        "raw:slash_uppercase": "close",
        "raw:slash_long_s": None,
        "raw:slash_substitution": "shrug",
        "raw:mbq_backtick": "close",
        "raw:mbq_tilde": "close",
        "raw:mbq_trailing_space": "close",
        "raw:mbq_trailing_tab": "close",
        "raw:mbq_four": "close",
        "raw:mbq_one_space": "close",
        "raw:mbq_alert_note": "close",
        "raw:mbq_alert_warning": "close",
        "raw:mbq_alert_one_space": "close",
        "raw:mbq_alert_suffix": "close",
    }

    for case_id, command_name in command_cases.items():
        row = verdicts[case_id]
        commands = row["commands"]
        assert commands, case_id
        names = [command[0] for command in commands if command]
        if command_name is not None:
            assert command_name in names, case_id
        assert row["stored_equals_posted"] is False, case_id

    substitution = verdicts["raw:slash_substitution"]
    assert (
        substitution["content"] == "\u00af\\\uff3f(\u30c4)\uff3f/\u00af\n\nfooter line"
    )

    suggestion_breakout = verdicts["raw:suggestion_alert_payload_breakout"]
    assert any(
        command and command[0] == "label" for command in suggestion_breakout["commands"]
    )
    assert suggestion_breakout["stored_equals_posted"] is False


def test_fixture_raw_container_breakout_is_a_no_action_control() -> None:
    row = _verdicts_by_id(_load_real_fixture())["raw:mbq_container_quote"]
    assert row["commands"] == []
    assert row["stored_equals_posted"] is True


def test_fixture_composed_verdicts_match_current_bodies_and_have_no_actions() -> None:
    document = _load_real_fixture()
    assert document["gitlab_version"] == "19.4.1"
    verdicts = _verdicts_by_id(document)
    composed_cases = build_composed_quick_action_cases()
    composed = {case["id"]: case["text"] for case in composed_cases}
    assert set(composed) == _REQUIRED_COMPOSED_CASE_IDS

    recorded_composed = {
        case_id.removeprefix("composed:"): row
        for case_id, row in verdicts.items()
        if row["group"] == "composed"
    }
    assert set(recorded_composed) == set(composed)
    for case_id, body in composed.items():
        row = recorded_composed[case_id]
        render_probes._validate_quick_action_paragraphs(
            body, row["paragraphs"], f"composed:{case_id}"
        )
        _assert_no_raw_slash_in_candidate_paragraphs(body, row["paragraphs"], case_id)
        assert row["sha256"] == input_sha256(body), case_id
        assert row["commands"] == [], case_id
        assert row["stored_equals_posted"] is True, case_id

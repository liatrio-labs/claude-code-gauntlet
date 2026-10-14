from __future__ import annotations

import base64
import hashlib
import json
import re
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any, NamedTuple

import gauntlet.delivery.post as post_review
import pytest
from gauntlet.markdown import open_fence

from tests.support.outbound_assertions import (
    assert_outbound_string_invariant as _assert_outbound_string_invariant,
)
from tests.tools import probe_fixtures, probe_quick_actions
from tests.tools.probe_cases import (
    build_composed_quick_action_cases,
    build_quick_action_case_list,
)
from tests.tools.probe_quick_actions import (
    parse_quick_action_runner_output,
    record_quick_action_fixture,
    run_gitlab_quick_action_verdicts,
)

SENTINEL = probe_quick_actions.QUICK_ACTION_SENTINEL


COMPOSED_PAYLOADS_CASES = {
    "patch-action-payload": ("suggested_patch", ">>>\n/close\nreturn x"),
    "suggestion-payload": ("slash_suggestion_line", "Suggested fix:"),
    "corroborator-payload": ("grouped_corroborator", "Corroborating finding"),
    "escaped-alert-payload": ("suggestion_alert_payload", "\\>>> [!note]"),
    "trusted-suggestion-label-payload": (
        "suggestion_alert_payload",
        "suggestion:-0+2\n>>>\n/label ~zz377nolabel\nreturn x\n```",
    ),
}

RUNNER_SENTINEL_PROTOCOL_CASES = {
    "rails-noise-ignored": (
        'warning line\nGITLAB_QUICK_ACTION_VERDICTS_JSON:{"gitlab_version":"19.4.1","cases":[]}\n',
        {"gitlab_version": "19.4.1", "cases": []},
        None,
    ),
    "missing-sentinel": (
        "warning only",
        None,
        "GitLab quick-action runner output must contain exactly one sentinel result",
    ),
}


INVALID_INTERVAL_CASES = {
    "bounds": (
        {"start_line": 999, "end_line": 999},
        "out-of-bounds paragraph interval",
    ),
    "extra-field": (
        {"start_line": 0, "end_line": 0, "extra": 1},
        "invalid paragraph interval",
    ),
}

RAW_CONTROL_CASES = {
    "slash_close_bare": "close",
    "slash_multi_actions": "label",
    "slash_lazy_continuation": "close",
    "slash_merge": "merge",
    "slash_uppercase": "close",
    "slash_long_s": None,
    "slash_substitution": "shrug",
    "mbq_backtick": "close",
    "mbq_tilde": "close",
    "mbq_trailing_space": "close",
    "mbq_trailing_tab": "close",
    "mbq_four": "close",
    "mbq_one_space": "close",
    "mbq_alert_note": "close",
    "mbq_alert_warning": "close",
    "mbq_alert_one_space": "close",
    "mbq_alert_suffix": "close",
    "suggestion_alert_payload_breakout": "label",
}


class Oracle(NamedTuple):
    document: dict[str, Any]
    current: list[dict[str, str]]
    composed: list[dict[str, Any]]


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
        content = "substituted" if is_raw_attack else normalized_posted
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
        assert (
            row["sha256"] == hashlib.sha256(case["text"].encode("utf-8")).hexdigest()
        ), case["id"]
        probe_quick_actions._validate_quick_action_paragraphs(
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


@pytest.fixture(scope="module")
def oracle() -> Oracle:
    path = (
        Path(__file__).parent / "fixtures" / "gitlab_quick_action_verdicts_19_4_1.json"
    )
    assert path.is_file(), (
        "record the GitLab 19.4.1 verdict fixture before running offline assertions"
    )
    document = json.loads(probe_fixtures.read_utf8(str(path)))
    current = build_quick_action_case_list(
        probe_fixtures._read_cases(probe_fixtures._fixture_path())
    )
    composed = build_composed_quick_action_cases()
    return Oracle(document, current, composed)


def test_recorded_oracle_freshness_and_safe_effects(oracle) -> None:
    document, current, composed = oracle
    assert isinstance(document, dict)
    assert document["gitlab_version"] == "19.4.1"
    assert isinstance(document["recorded"], str)
    rows = document["cases"]
    assert isinstance(rows, list)
    assert all(isinstance(row, dict) for row in rows)
    assert len({row["id"] for row in rows}) == len(rows)
    assert composed == build_composed_quick_action_cases()
    _assert_recorded_rows_match_current_cases(rows, current)
    safe = [
        (row, case)
        for row, case in zip(rows, current, strict=True)
        if row["group"] in {"expected", "composed"}
    ]
    assert any(row["group"] == "expected" for row, _ in safe)
    assert any(row["group"] == "composed" for row, _ in safe)
    for row, case in safe:
        assert row["commands"] == [], row["id"]
        assert row["stored_equals_posted"] is True, row["id"]
        if row["group"] == "composed":
            _assert_no_raw_slash_in_candidate_paragraphs(
                case["text"], row["paragraphs"], row["id"]
            )


@pytest.mark.parametrize(
    ("case_id", "payload"),
    COMPOSED_PAYLOADS_CASES.values(),
    ids=COMPOSED_PAYLOADS_CASES,
)
def test_composed_payloads(oracle, case_id, payload) -> None:
    composed = oracle.composed
    assert payload in next(case["text"] for case in composed if case["id"] == case_id)


def test_composed_route_headers_and_trailers_are_outside_trusted_fences(oracle) -> None:
    for case in oracle.composed:
        body = case["text"]
        fences: list[tuple[int, int]] = []
        open_fence(body, strict=True, intervals=fences)
        if case["route"] == "summary":
            header = "### \u2694\ufe0f Code Gauntlet"
            trailer = "Generated by code-gauntlet | Reviewed up to: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        else:
            assert case["route"] in {"discussion", "note"}
            header = "**\U0001f7e1 [MEDIUM] Probe finding**"
            trailer = "\u2694\ufe0f *Code Gauntlet*"
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


@pytest.mark.parametrize(
    ("stdout", "expected", "error"),
    RUNNER_SENTINEL_PROTOCOL_CASES.values(),
    ids=RUNNER_SENTINEL_PROTOCOL_CASES,
)
def test_runner_sentinel_protocol(stdout, expected, error) -> None:
    if error:
        with pytest.raises(ValueError, match="^" + re.escape(error) + "$"):
            parse_quick_action_runner_output(stdout)
    else:
        assert parse_quick_action_runner_output(stdout) == expected


def test_runner_embeds_cases_and_preserves_gitlab_verdicts() -> None:
    seen: dict[str, Any] = {}

    def runner(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        seen["argv"] = argv
        seen["kwargs"] = kwargs
        return _fake_rails_runner(argv, **kwargs)

    result = run_gitlab_quick_action_verdicts(
        [
            {
                "id": "expected:sample",
                "group": "expected",
                "text": "\\/close\n\uff20safe\r\n",
            },
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
    assert seen["kwargs"]["timeout"] == 180
    encoded = re.search(r'CASES_B64 = "([A-Za-z0-9+/=]+)"', seen["kwargs"]["input"])
    assert encoded is not None
    assert json.loads(base64.b64decode(encoded.group(1)).decode("utf-8")) == [
        {
            "id": "expected:sample",
            "group": "expected",
            "text": "\\/close\n\uff20safe\r\n",
        },
        {"id": "raw:sample", "group": "raw", "text": "/close"},
    ]
    assert result["cases"][1]["paragraphs"] == [{"start_line": 0, "end_line": 0}]
    assert result["cases"][1]["commands"] == [["close"]]
    assert result["cases"][1]["stored_equals_posted"] is False


def test_recorder_serializes_results_and_check_mode_only_reports_differences() -> None:
    stored: dict[str, str] = {}
    writes: list[str] = []
    outbound = [{"id": "sample", "input": "/close", "expected": "\\/close"}]

    def read_text(path: str) -> str:
        return stored[path]

    def write_text(path: str, contents: str) -> None:
        writes.append(contents)
        stored[path] = contents

    def record(
        check: bool = False,
        clock: Callable[[], str] = lambda: "2026-09-25",
    ) -> probe_quick_actions.QuickActionRecordResult:
        return record_quick_action_fixture(
            "fixture.json",
            outbound,
            runner=_fake_rails_runner,
            clock=clock,
            read_text=read_text,
            write_text=write_text,
            check=check,
        )

    recorded = record()
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

    unchanged = record(check=True, clock=lambda: "2026-09-26")
    assert unchanged.changed_ids == []
    assert unchanged.exit_code == 0
    assert len(writes) == 1

    document["cases"][0]["paragraphs"] = [{"start_line": 9, "end_line": 9}]
    stored["fixture.json"] = json.dumps(document)
    checked = record(check=True, clock=lambda: "2026-09-26")
    assert checked.changed_ids == ["expected:sample"]
    assert checked.exit_code == 1
    assert len(writes) == 1


def test_quick_action_literal_serializer() -> None:
    assert probe_quick_actions.serialize_quick_action_fixture(
        {
            "gitlab_version": "19.4.1",
            "recorded": "2026-09-25",
            "cases": [
                {
                    "id": "raw:sample",
                    "group": "raw",
                    "sha256": "abc",
                    "paragraphs": [{"start_line": 0, "end_line": 0}],
                    "commands": [["close"]],
                    "stored_equals_posted": False,
                    "content": "\uff20changed",
                }
            ],
        }
    ) == (
        '{\n  "gitlab_version": "19.4.1",\n  "recorded": "2026-09-25",\n  "cases": [\n'
        '    {"id":"raw:sample","group":"raw","sha256":"abc","paragraphs":[{"start_line":0,"end_line":0}],"commands":[["close"]],"stored_equals_posted":false,"content":"\uff20changed"}\n'
        "  ]\n}\n"
    )


@pytest.mark.parametrize(
    ("paragraph", "error"),
    INVALID_INTERVAL_CASES.values(),
    ids=INVALID_INTERVAL_CASES,
)
def test_recorder_rejects_bad_intervals(paragraph, error) -> None:
    writes = []

    def runner(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        result = _fake_rails_runner(argv, **kwargs)
        document = parse_quick_action_runner_output(result.stdout)
        document["cases"][0]["paragraphs"] = [paragraph]
        return subprocess.CompletedProcess(
            argv, 0, stdout=SENTINEL + json.dumps(document)
        )

    with pytest.raises(ValueError, match=error):
        record_quick_action_fixture(
            "unused",
            [{"id": "sample", "input": "/close", "expected": "safe"}],
            runner=runner,
            write_text=lambda path, contents: writes.append(contents),
        )
    assert writes == []


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


@pytest.mark.parametrize(
    ("case_name", "command_name"),
    RAW_CONTROL_CASES.items(),
    ids=RAW_CONTROL_CASES,
)
def test_recorded_raw_positive_controls(oracle, case_name, command_name) -> None:
    case_id = f"raw:{case_name}"
    row = next(row for row in oracle.document["cases"] if row["id"] == case_id)
    commands = row["commands"]
    assert commands, case_id
    if command_name is not None:
        assert command_name in [command[0] for command in commands if command], case_id
    assert row["stored_equals_posted"] is False, case_id
    if command_name == "shrug":
        assert row["content"] == "\u00af\\\uff3f(\u30c4)\uff3f/\u00af\n\nfooter line"


def test_recorded_container_no_action_control(oracle) -> None:
    document = oracle.document
    row = next(
        row for row in document["cases"] if row["id"] == "raw:mbq_container_quote"
    )
    assert row["commands"] == []
    assert row["stored_equals_posted"] is True

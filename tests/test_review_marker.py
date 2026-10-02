"""Pure marker and documentation contracts.

Build/detect round trips cover summary composition for both platforms.
Readers preserve legacy tokens, unknown fields and whitespace variants.
Versions never select a reader; marker signals take precedence over prose.
Malformed and nonfinite input is rejected without raising; scans bound work.
Footer halves suppress duplicates independently and require the reviewed SHA.
Writer signatures exclude the removed findings slot.
Finding markers retain every member key, last-wins order and distinct tokens.
Timestamp selection uses UTC instants, missing-value ordering and input ties.
Documentation guards cover detector fields, quoted signals and incremental gates.
Output directories are literal; markdown delivery surfaces the persisted path.
"""

import inspect
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import gauntlet.marker as review_marker
import pytest
from gauntlet.marker import (
    FINDING_MARKER_TOKEN,
    LEGACY_MARKER_TOKEN,
    LEGACY_PRODUCT,
    MARKER_TOKEN,
    MARKER_TOKENS,
    PRODUCT,
    build_finding_marker,
    build_footer,
    build_marker,
    build_prose_footer,
    detect_signal,
    find_finding_marker,
    find_finding_markers,
    find_marker,
    has_prose_footer,
    parse_prose_footer,
    select_latest,
)

REPO = Path(__file__).resolve().parents[1]
# Hex-only SHAs remain valid in every marker and prose fixture.
SHA_40 = "a" * 40
SHA_8 = "a" * 8
HEAD_SHA_40 = "b" * 40
# Repeated-character keys avoid entropy-based credential lint on fixtures.
KEY_16 = "a" * 16
OTHER_KEY_16 = "b" * 16


_QUOTE_RE = re.compile("`([^`\\n]+)`")


def _read(rel_path):
    return (REPO / rel_path).read_text(encoding="utf-8")


COMPACT_MARKER = json.dumps({"version": "3.0", "sha": SHA_40}, separators=(",", ":"))


@pytest.mark.parametrize(
    "sha, count",
    [
        pytest.param(SHA_40, 0, id="full-sha-count-0"),
        pytest.param(SHA_40, 1, id="full-sha-count-1"),
        pytest.param(SHA_40, 250, id="full-sha-count-250"),
        pytest.param(SHA_8, 0, id="short-sha-count-0"),
        pytest.param(SHA_8, 1, id="short-sha-count-1"),
        pytest.param(SHA_8, 250, id="short-sha-count-250"),
    ],
)
def test_round_trip__build_footer_round_trip_matrix(sha, count):
    footer = build_footer(count, sha)
    signal = detect_signal(footer)
    assert signal is not None
    assert signal["sha"] == sha


@pytest.mark.parametrize(
    "pre_existing, sha",
    [
        pytest.param("", SHA_40, id="empty-body-full-sha"),
        pytest.param("", SHA_8, id="empty-body-short-sha"),
        pytest.param(
            "## Summary\nSome pre-existing narrative text.\n",
            SHA_40,
            id="authored-body-full-sha",
        ),
        pytest.param(
            "## Summary\nSome pre-existing narrative text.\n",
            SHA_8,
            id="authored-body-short-sha",
        ),
    ],
)
def test_round_trip__realistic_github_review_body_round_trip(pre_existing, sha):
    findings = [{"title": "x"}, {"title": "y"}, {"title": "z"}]
    review_body = pre_existing
    review_body += build_footer(len(findings), sha, body=review_body)
    signal = detect_signal(review_body)
    assert signal is not None
    assert signal["signal"] == "marker"
    assert signal["sha"] == sha
    assert not signal["legacy"]


@pytest.mark.parametrize(
    "pre_existing, sha",
    [
        pytest.param("", SHA_40, id="empty-body-full-sha"),
        pytest.param("", SHA_8, id="empty-body-short-sha"),
        pytest.param(
            "## MR Review\nContext for the reviewer.\n",
            SHA_40,
            id="authored-body-full-sha",
        ),
        pytest.param(
            "## MR Review\nContext for the reviewer.\n",
            SHA_8,
            id="authored-body-short-sha",
        ),
    ],
)
def test_round_trip__realistic_gitlab_summary_note_round_trip(pre_existing, sha):
    findings = [{"title": "a"}]
    summary_body = pre_existing
    summary_body += build_footer(len(findings), sha, body=summary_body)
    signal = detect_signal(summary_body)
    assert signal is not None
    assert signal["signal"] == "marker"
    assert signal["sha"] == sha


def test_tolerance__current_shape():
    text = build_marker(SHA_40, 3)
    signal = detect_signal(text)
    assert signal is not None
    assert signal["sha"] == SHA_40
    assert not signal["legacy"]


def test_tolerance__legacy_token():
    payload = json.dumps({"version": "3.0", "findings_count": 2, "sha": SHA_40})
    text = f"<!-- {LEGACY_MARKER_TOKEN}: {payload} -->"
    signal = detect_signal(text)
    assert signal is not None
    assert signal["sha"] == SHA_40
    assert signal["legacy"]


def test_tolerance__documented_but_never_written_v1_shape():
    payload = json.dumps({"version": 1, "sha": SHA_40, "findings": [{"a": 1}]})
    text = f"<!-- {MARKER_TOKEN}: {payload} -->"
    signal = detect_signal(text)
    assert signal is not None
    assert signal["sha"] == SHA_40


def test_tolerance__version_absent_entirely():
    payload = json.dumps({"findings_count": 1, "sha": SHA_40})
    text = f"<!-- {MARKER_TOKEN}: {payload} -->"
    signal = detect_signal(text)
    assert signal is not None
    assert signal["sha"] == SHA_40


def test_tolerance__unknown_future_keys_preserved_in_marker():
    payload = json.dumps(
        {"version": "3.0", "sha": SHA_40, "future_field": "xyz", "nested": {"a": 1}}
    )
    text = f"<!-- {MARKER_TOKEN}: {payload} -->"
    signal = detect_signal(text)
    assert signal is not None
    assert signal["marker"]["future_field"] == "xyz"
    assert signal["marker"]["nested"] == {"a": 1}


@pytest.mark.parametrize(
    "text",
    [
        pytest.param(f"<!--{MARKER_TOKEN}:{COMPACT_MARKER}-->", id="compact"),
        pytest.param(
            f"<!-- {MARKER_TOKEN}:\n{COMPACT_MARKER}\n-->", id="payload-newlines"
        ),
        pytest.param(
            f"<!--\n{MARKER_TOKEN}: {COMPACT_MARKER}\n-->", id="token-newline"
        ),
    ],
)
def test_tolerance__whitespace_free_and_newline_broken_forms_both_parse(text):
    signal = detect_signal(text)
    assert signal is not None
    assert signal["sha"] == SHA_40


def test_tolerance__findings_array_containing_literal_close_comment_inside_a_string():
    payload = json.dumps(
        {
            "version": "3.0",
            "sha": SHA_40,
            "findings": ["this string literally contains --> inside it"],
        }
    )
    text = f"<!-- {MARKER_TOKEN}: {payload} -->"
    signal = detect_signal(text)
    assert signal is not None
    assert signal["sha"] == SHA_40
    assert signal["marker"]["findings"] == [
        "this string literally contains --> inside it"
    ]


@pytest.mark.parametrize(
    "present, version",
    [
        pytest.param(True, "3.0", id="current-version"),
        pytest.param(True, 1, id="integer-version"),
        pytest.param(True, "99", id="future-version"),
        pytest.param(True, None, id="null-version"),
        pytest.param(False, None, id="absent-version"),
    ],
)
def test_version_is_never_dispatched_on__version_variants_yield_identical_sha_and_signal(
    present, version
):
    payload = {"findings_count": 5, "sha": SHA_40}
    if present:
        payload["version"] = version
    text = f"<!-- {MARKER_TOKEN}: {json.dumps(payload)} -->"
    signal = detect_signal(text)
    assert signal is not None
    assert (signal["sha"], signal["signal"]) == (SHA_40, "marker")


def test_prose_footer__current_product_prose_detected():
    text = build_prose_footer(SHA_40)
    assert has_prose_footer(text)
    signal = detect_signal(text)
    assert signal is not None
    assert signal["signal"] == "footer"
    assert not signal["legacy"]
    assert signal["sha"] == SHA_40


def test_prose_footer__legacy_product_prose_detected():
    text = f"Generated by {LEGACY_PRODUCT} | Reviewed up to: {SHA_40}"
    assert has_prose_footer(text)
    signal = detect_signal(text)
    assert signal is not None
    assert signal["signal"] == "footer"
    assert signal["legacy"]
    assert signal["sha"] == SHA_40


def test_prose_footer__sha_parsed_from_reviewed_up_to_label():
    assert parse_prose_footer(f"Reviewed up to: {SHA_40}") == SHA_40
    assert parse_prose_footer(f"Reviewed up to: `{SHA_8}` |") == SHA_8
    assert parse_prose_footer(f"Reviewed up to: **{SHA_8}**") == SHA_8
    assert parse_prose_footer(f"Reviewed up to: {SHA_8} |") == SHA_8


def test_prose_footer__marker_wins_when_both_prose_and_marker_present():
    prose = build_prose_footer(HEAD_SHA_40)
    marker = build_marker(SHA_40, 4)
    text = f"{prose}\n\n{marker}"
    signal = detect_signal(text)
    assert signal is not None
    assert signal["signal"] == "marker"
    assert signal["sha"] == SHA_40


def test_prose_footer__footer_only_body_yields_footer_signal():
    text = build_prose_footer(SHA_40)
    signal = detect_signal(text)
    assert signal is not None
    assert signal["signal"] == "footer"
    assert signal["marker"] is None


def test_malformed__empty_string():
    assert detect_signal("") is None
    assert find_marker("") is None
    assert not has_prose_footer("")
    assert parse_prose_footer("") is None


def test_malformed__marker_with_broken_json():
    text = f"<!-- {MARKER_TOKEN}: {{not valid json at all -->"
    assert find_marker(text) is None
    assert detect_signal(text) is None


def test_malformed__marker_valid_json_but_no_sha():
    payload = json.dumps({"version": "3.0", "findings_count": 2})
    text = f"<!-- {MARKER_TOKEN}: {payload} -->"
    assert detect_signal(text) is None
    parsed = find_marker(text)
    assert parsed is not None
    assert "sha" not in parsed


def test_malformed__marker_sha_not_hex():
    payload = json.dumps({"version": "3.0", "sha": "not-a-real-sha!!"})
    text = f"<!-- {MARKER_TOKEN}: {payload} -->"
    assert detect_signal(text) is None


def test_malformed__one_broken_one_valid_marker_returns_the_valid_one():
    broken = f"<!-- {MARKER_TOKEN}: {{broken json -->"
    valid = build_marker(SHA_40, 1)
    text = f"{broken}\n\n{valid}"
    signal = detect_signal(text)
    assert signal is not None
    assert signal["sha"] == SHA_40


@pytest.mark.parametrize(
    "const",
    [
        pytest.param("NaN", id="nan"),
        pytest.param("Infinity", id="positive-infinity"),
        pytest.param("-Infinity", id="negative-infinity"),
    ],
)
def test_malformed__nan_infinity_constants_reject_the_marker_as_malformed(const):
    text = (
        f'<!-- {MARKER_TOKEN}: {{"version":"3.0","sha":"{SHA_40}","weird":{const}}} -->'
    )
    assert find_marker(text) is None
    assert detect_signal(text) is None


def test_malformed__detector_stdout_stays_valid_strict_json_when_marker_carries_nan():
    text = f'<!-- {MARKER_TOKEN}: {{"sha":"{SHA_40}","x":NaN}} -->'
    signal = detect_signal(text)
    assert signal is None
    dumped = json.dumps({"marker": signal})
    assert "NaN" not in dumped
    assert json.loads(dumped)["marker"] is None


@pytest.mark.parametrize(
    "text",
    [
        pytest.param("no marker or footer here at all", id="plain-text"),
        pytest.param(f"<!-- {MARKER_TOKEN}: -->", id="empty-marker"),
        pytest.param(f"<!-- {MARKER_TOKEN}:", id="missing-payload"),
        pytest.param(f'<!-- {MARKER_TOKEN}: {{"sha" -->', id="truncated-json"),
        pytest.param("Reviewed up to:", id="bare-reviewed-label"),
        pytest.param("Generated by", id="bare-generated-label"),
        pytest.param("<!-- -->", id="empty-comment"),
        pytest.param("{" * 50, id="brace-noise"),
    ],
)
def test_malformed__assorted_garbage_never_raises(text):
    detect_signal(text)
    find_marker(text)
    has_prose_footer(text)
    parse_prose_footer(text)


def test_max_marker_scans__valid_marker_within_the_scan_window_is_still_found():
    noise = "<!-- code-gauntlet-findings: {broken -->\n" * 10
    valid = build_marker(SHA_40, 1)
    text = noise + valid
    signal = detect_signal(text)
    assert signal is not None
    assert signal["sha"] == SHA_40


def test_max_marker_scans__valid_marker_beyond_the_scan_window_is_not_found():
    valid = build_marker(SHA_40, 1)
    trailing_noise = "<!-- code-gauntlet-findings: {broken -->\n" * (
        review_marker._MAX_MARKER_SCANS + 5
    )
    text = valid + "\n" + trailing_noise
    assert find_marker(text) is None
    assert detect_signal(text) is None


def test_max_marker_scans__many_malformed_candidates_never_raise_or_hang():
    garbage = "<!-- code-gauntlet-findings: {" * 5000
    result = find_marker(garbage)
    assert result is None


def test_idempotence__marker_already_present_omits_marker_half():
    body = build_marker(SHA_40, 2)
    addition = build_footer(3, SHA_40, body=body)
    assert "<!--" not in addition, "marker half must be omitted"
    assert "Generated by" in addition, "prose half is still needed"
    full = body + addition
    signal = detect_signal(full)
    assert signal is not None
    assert signal["sha"] == SHA_40


def test_idempotence__prose_already_present_omits_prose_half():
    body = build_prose_footer(SHA_40)
    addition = build_footer(1, SHA_40, body=body)
    assert "Generated by" not in addition, "prose half must be omitted"
    assert MARKER_TOKEN in addition, "marker half is still needed"
    full = body + addition
    signal = detect_signal(full)
    assert signal is not None
    assert signal["sha"] == SHA_40
    assert signal["signal"] == "marker"


def test_idempotence__both_already_present_returns_empty_string():
    body = build_footer(1, SHA_40)
    addition = build_footer(1, SHA_40, body=body)
    assert addition == ""


def test_idempotence__double_append_is_a_no_op_and_one_sha_resolves():
    body = ""
    body += build_footer(2, SHA_40, body=body)
    length_after_first = len(body)
    body += build_footer(2, SHA_40, body=body)
    assert len(body) == length_after_first, "double-append must add nothing"
    signal = detect_signal(body)
    assert signal is not None
    assert signal["sha"] == SHA_40


def test_idempotence__sha_less_marker_plus_bare_prose_still_gets_a_detectable_signal():
    body = f'<!-- {MARKER_TOKEN}: {{"version":"3.0"}} -->\n\nGenerated by {PRODUCT}\n'
    assert detect_signal(body) is None, (
        "the pre-existing body must carry no usable signal (motivating case)"
    )
    addition = build_footer(1, SHA_40, body=body)
    assert addition != "", (
        "build_footer must not go silent just because unusable marker/prose fragments are already present"
    )
    full = body + addition
    signal = detect_signal(full)
    assert signal is not None
    assert signal["sha"] == SHA_40


# A nested writer can still reject a restored forwarded slot; inspect each signature.
def test_removed_findings_slot__build_marker_has_no_findings_parameter():
    assert "findings" not in inspect.signature(build_marker).parameters


def test_removed_findings_slot__build_footer_has_no_findings_parameter():
    assert "findings" not in inspect.signature(build_footer).parameters


@pytest.mark.parametrize(
    "sha", [pytest.param(SHA_40, id="full-sha"), pytest.param(SHA_8, id="short-sha")]
)
def test_finding_marker__build_parse_round_trip(sha):
    parsed = find_finding_marker(build_finding_marker(sha, KEY_16))
    assert parsed == {"sha": sha, "key": KEY_16}


def test_finding_marker__round_trip_through_a_realistic_comment_body():
    body = "**\U0001f7e0 [HIGH] SQL injection risk**\n\nUse a parameterized query.\n"
    text = f"{body}\n\n{build_finding_marker(SHA_40, KEY_16)}"
    assert find_finding_marker(text)["key"] == KEY_16


def test_finding_marker__last_marker_wins():
    forged = build_finding_marker(SHA_40, OTHER_KEY_16)
    real = build_finding_marker(SHA_40, KEY_16)
    assert find_finding_marker(f"{forged}\n\n{real}")["key"] == KEY_16


def test_finding_marker__every_marker_in_one_body_is_returned():
    text = f"Body\n\n{build_finding_marker(SHA_40, KEY_16)}\n{build_finding_marker(SHA_40, OTHER_KEY_16)}"
    assert {m["key"] for m in find_finding_markers(text)} == {KEY_16, OTHER_KEY_16}


# Forty is independent of the summary scan cap: every group member must survive.
def test_finding_marker__more_than_scan_limit_finding_markers_are_all_returned():
    n = 40
    sha = "a" * 40
    keys = [f"{i:016x}" for i in range(n)]
    body = "Body\n\n" + "\n".join(build_finding_marker(sha, key) for key in keys)
    expected = [{"sha": sha, "key": key} for key in reversed(keys)]
    markers = find_finding_markers(body)
    assert len(markers) == n
    assert markers == expected
    assert find_finding_marker(body) == expected[0]
    assert find_finding_marker(body)["key"] == keys[-1]
    assert find_finding_marker(body)["key"] != keys[0]


def test_finding_marker__many_malformed_finding_candidates_finish_and_preserve_valid_tail():
    child = "\n".join(
        [
            "from gauntlet.marker import build_finding_marker, find_finding_marker, find_finding_markers",
            "n = 40  # a literal: the finding reader has no scan cap to derive from",
            "sha = 'a' * 40",
            "keys = [f'{i:016x}' for i in range(n)]",
            "invalid_json = '<!-- code-gauntlet-finding-key: {not json} -->\\n' * 5000",
            'invalid_key = (\'<!-- code-gauntlet-finding-key: {\\"sha\\":\\"\' + sha + \'\\",\\"key\\":[]} -->\\n\') * 5000',
            "unclosed = '<!-- code-gauntlet-finding-key: {' * 5000",
            "malformed = invalid_json + invalid_key + unclosed",
            "if find_finding_markers(malformed) != [] or find_finding_marker(malformed) is not None:",
            "    raise AssertionError('malformed-only body produced a marker')",
            "valid = '\\n'.join(build_finding_marker(sha, key) for key in keys)",
            "expected = [{'sha': sha, 'key': key} for key in reversed(keys)]",
            "combined = malformed + '\\n' + valid",
            "if find_finding_markers(combined) != expected:",
            "    raise AssertionError('valid marker tail was truncated or reordered')",
            "if find_finding_marker(combined) != expected[0]:",
            "    raise AssertionError('singular reader did not return the valid tail')",
            "print('STAGE_A_MALFORMED_OK')",
        ]
    )
    env = os.environ.copy()
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONPATH"] = str(REPO / "scripts")
    try:
        completed = subprocess.run(
            [sys.executable, "-c", child],
            cwd=str(REPO),
            env=env,
            capture_output=True,
            check=True,
            text=True,
            timeout=10,
            encoding="utf-8",
        )
    except subprocess.CalledProcessError as exc:
        details = (exc.stderr or "").strip().splitlines()
        pytest.fail(details[-1] if details else "malformed-input child failed")
    assert completed.stdout.strip() == "STAGE_A_MALFORMED_OK"


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param({"sha": SHA_40, "key": ["not", "a", "string"]}, id="list-key"),
        pytest.param({"sha": SHA_40, "key": {"nested": "object"}}, id="dict-key"),
        pytest.param({"sha": SHA_40, "key": 123456789}, id="integer-key"),
        pytest.param({"sha": SHA_40, "key": None}, id="null-key"),
        pytest.param({"sha": SHA_40, "key": KEY_16.upper()}, id="uppercase-key"),
        pytest.param({"sha": SHA_40, "key": KEY_16[:15]}, id="short-key"),
        pytest.param({"sha": SHA_40, "key": KEY_16 + "0"}, id="long-key"),
        pytest.param({"sha": SHA_40, "key": "g" * 16}, id="nonhex-key"),
        pytest.param({"sha": SHA_40}, id="missing-key"),
        pytest.param({"key": KEY_16}, id="missing-sha"),
        pytest.param({"sha": "not-a-sha", "key": KEY_16}, id="nonsha-string"),
        pytest.param({"sha": ["a" * 40], "key": KEY_16}, id="list-sha"),
    ],
)
def test_finding_marker__malformed_payloads_are_ignored_and_never_raise(payload):
    text = f"<!-- {FINDING_MARKER_TOKEN}: {json.dumps(payload)} -->"
    assert find_finding_marker(text) is None


@pytest.mark.parametrize(
    "text",
    [
        pytest.param("", id="empty"),
        pytest.param(None, id="null"),
        pytest.param(123, id="integer"),
        pytest.param(f"<!-- {FINDING_MARKER_TOKEN}: -->", id="empty-payload"),
        pytest.param(f"<!-- {FINDING_MARKER_TOKEN}: {{broken -->", id="broken-json"),
        pytest.param(
            f"<!-- {FINDING_MARKER_TOKEN}: {{not json at all}} -->", id="braced-nonjson"
        ),
        pytest.param(f"<!-- {FINDING_MARKER_TOKEN}: [] -->", id="array-payload"),
        pytest.param(
            f'<!-- {FINDING_MARKER_TOKEN}: {{"sha":"{SHA_40}","key":"{KEY_16}"}}',
            id="missing-close-comment",
        ),
        pytest.param("{" * 50, id="brace-noise"),
    ],
)
def test_finding_marker__assorted_garbage_never_raises(text):
    assert find_finding_marker(text) is None


def test_finding_marker__a_valid_marker_after_a_malformed_one_is_still_found():
    broken = f"<!-- {FINDING_MARKER_TOKEN}: {{broken -->"
    text = f"{broken}\n{build_finding_marker(SHA_40, KEY_16)}"
    assert find_finding_marker(text)["key"] == KEY_16


def test_finding_marker__the_two_marker_kinds_never_see_each_other():
    summary = build_marker(SHA_40, 3)
    finding = build_finding_marker(SHA_40, KEY_16)
    both = f"{summary}\n\n{finding}"
    assert find_finding_marker(both) == {"sha": SHA_40, "key": KEY_16}
    assert find_marker(both)["findings_count"] == 3
    assert detect_signal(both)["signal"] == "marker"
    assert find_finding_marker(summary) is None
    assert find_marker(finding) is None
    assert detect_signal(finding) is None


def test_select_latest__newest_timestamp_wins_across_mixed_sources():
    entries = [
        {
            "body": build_marker("a" * 8, 1),
            "timestamp": "2026-01-01T00:00:00Z",
            "source": "review",
            "id": 1,
        },
        {
            "body": build_prose_footer("b" * 8),
            "timestamp": "2026-06-15T12:00:00Z",
            "source": "issue_comment",
            "id": 2,
        },
        {
            "body": build_marker("c" * 8, 1),
            "timestamp": "2026-03-01T00:00:00Z",
            "source": "note",
            "id": 3,
        },
    ]
    result = select_latest(entries)
    assert result is not None
    assert result["sha"] == "b" * 8
    assert result["source"] == "issue_comment"
    assert result["timestamp"] == "2026-06-15T12:00:00Z"


def test_select_latest__none_timestamp_sorts_lowest():
    entries = [
        {
            "body": build_marker("a" * 8, 1),
            "timestamp": None,
            "source": "review",
            "id": 1,
        },
        {
            "body": build_marker("b" * 8, 1),
            "timestamp": "2020-01-01T00:00:00Z",
            "source": "review",
            "id": 2,
        },
    ]
    result = select_latest(entries)
    assert result is not None
    assert result["sha"] == "b" * 8


def test_select_latest__unparseable_timestamp_sorts_lowest():
    entries = [
        {
            "body": build_marker("a" * 8, 1),
            "timestamp": "not-a-timestamp",
            "source": "review",
            "id": 1,
        },
        {
            "body": build_marker("b" * 8, 1),
            "timestamp": "2020-01-01T00:00:00Z",
            "source": "review",
            "id": 2,
        },
    ]
    result = select_latest(entries)
    assert result is not None
    assert result["sha"] == "b" * 8


def test_select_latest__ties_break_to_latest_input_order():
    same_ts = "2026-01-01T00:00:00Z"
    entries = [
        {
            "body": build_marker("a" * 8, 1),
            "timestamp": same_ts,
            "source": "review",
            "id": 1,
        },
        {
            "body": build_marker("b" * 8, 1),
            "timestamp": same_ts,
            "source": "note",
            "id": 2,
        },
    ]
    result = select_latest(entries)
    assert result is not None
    assert result["sha"] == "b" * 8, "the later entry in input order must win a tie"


def test_select_latest__no_signal_entries_are_ignored():
    entries = [
        {
            "body": "nothing detectable in this body",
            "timestamp": "2026-06-01T00:00:00Z",
            "source": "x",
            "id": 1,
        },
        {
            "body": build_marker("a" * 8, 1),
            "timestamp": "2020-01-01T00:00:00Z",
            "source": "review",
            "id": 2,
        },
    ]
    result = select_latest(entries)
    assert result is not None
    assert result["sha"] == "a" * 8


def test_select_latest__all_entries_without_signal_returns_none():
    entries = [
        {"body": "nope", "timestamp": "2026-01-01T00:00:00Z", "source": "x", "id": 1},
        {
            "body": "still nope",
            "timestamp": "2026-02-01T00:00:00Z",
            "source": "y",
            "id": 2,
        },
    ]
    assert select_latest(entries) is None


def test_select_latest__empty_entries_returns_none():
    assert select_latest([]) is None


def test_output_directory_glob_regressions__skill_and_agent_paths_do_not_embed_globs_in_output_dir():
    path_glob = re.compile("\\{output_dir\\}[\\\\/][^\\s`\\\"')]*[*?\\[]")
    glob_call = re.compile("glob\\.(?:glob|iglob)\\s*\\(")
    join_glob = re.compile(
        "os\\.path\\.join\\(\\s*['\\\"]\\{output_dir\\}['\\\"]\\s*,[^)]*[*?\\[]"
    )
    offenders = []
    for root_name in ("skills", "agents"):
        root = REPO / root_name
        for path in root.rglob("*.md"):
            for line_number, line in enumerate(
                path.read_text(encoding="utf-8").splitlines(), start=1
            ):
                if path_glob.search(line) or join_glob.search(line):
                    offenders.append(
                        f"{path.relative_to(REPO)}:{line_number}: output_dir glob path"
                    )
                for match in glob_call.finditer(line):
                    pattern_argument = line[match.end() :].split(",", 1)[0]
                    if "{output_dir}" in pattern_argument:
                        offenders.append(
                            f"{path.relative_to(REPO)}:{line_number}: glob pattern uses output_dir"
                        )
    assert offenders == [], "unsafe output_dir glob use:\n" + "\n".join(offenders)


PHASE1_REL = "skills/code-gauntlet/references/phase1-preflight.md"
SKILL_REL = "skills/code-gauntlet/SKILL.md"
PHASE2_REL = "skills/code-gauntlet/references/phase2-triage.md"
HEADLESS_MODE_REL = "skills/code-gauntlet/references/headless-mode.md"
REPORT_FORMAT_REL = "skills/code-gauntlet/references/report-format.md"
DELIVERY_GUIDE_REL = "skills/code-gauntlet/references/delivery-guide.md"
POST_REVIEW_REL = "scripts/gauntlet/delivery/post.py"
QUOTE_SCAN_RELS = (PHASE1_REL, SKILL_REL, REPORT_FORMAT_REL, DELIVERY_GUIDE_REL)


# Read and write docs both participate so discovery cannot become vacuous.
def _doc_quotes():
    cases = []
    for rel in QUOTE_SCAN_RELS:
        counts = {}
        for span in _QUOTE_RE.findall(_read(rel)):
            if span in MARKER_TOKENS:
                kind = "token"
                label = span
            elif "Generated by" in span and (PRODUCT in span or LEGACY_PRODUCT in span):
                kind = "footer"
                label = "footer-legacy" if LEGACY_PRODUCT in span else "footer-current"
            else:
                continue
            counts[label] = counts.get(label, 0) + 1
            name = f"{Path(rel).stem}-{label}-{counts[label]}"
            cases.append((name, rel, span, kind))
    return cases


def test_doc_quote_discovery_is_nonempty():
    cases = _doc_quotes()
    assert len(cases) > 0, "no doc quotes a detectable raw signal"
    assert sum((kind == "footer" for _, _, _, kind in cases)) > 0, (
        "no doc quotes its own complete footer"
    )


@pytest.mark.parametrize(
    "rel",
    [
        pytest.param(PHASE1_REL, id="phase1-preflight"),
        pytest.param(SKILL_REL, id="skill"),
    ],
)
def test_doc_contract__phase1_and_skill_reference_detect_prior_review_script(rel):
    assert "detect_prior_review.py" in _read(rel)


@pytest.mark.parametrize(
    "rel,span,kind",
    [pytest.param(rel, span, kind, id=name) for name, rel, span, kind in _doc_quotes()],
)
def test_doc_contract__every_quoted_marker_token_or_footer_string_is_actually_detected(
    rel, span, kind
):
    if kind == "token":
        payload = json.dumps({"version": "3.0", "findings_count": 1, "sha": SHA_40})
        text = f"<!-- {span}: {payload} -->"
        signal = detect_signal(text)
        assert signal is not None, f"quoted token {span!r} in {rel} was not detected"
        assert signal["legacy"] == (span == LEGACY_MARKER_TOKEN)
    else:
        assert "Reviewed up to" in span, (
            f"doc footer quote {span!r} in {rel} lost its label"
        )
        # Only the placeholder changes; the docs must supply their own footer label.
        text = re.sub(r"\{\w*sha\w*\}", SHA_40, span)
        signal = detect_signal(text)
        assert signal is not None, f"quoted footer {span!r} in {rel} was not detected"
        assert signal["sha"] == SHA_40


def test_doc_contract__phase1_preflight_covers_the_required_output_fields():
    required_fields = {
        "previously_reviewed",
        "incremental_safe",
        "head_advanced",
        "new_commit_count",
        "last_reviewed_sha_short",
        "last_reviewed_sha",
        "marker",
        "errors",
        "sha_resolvable",
    }
    text = _read(PHASE1_REL)
    missing = {
        field
        for field in required_fields
        if f"`{field}`" not in text and f'"{field}"' not in text
    }
    assert missing == set(), (
        f"phase1-preflight.md does not surface required output field(s): {sorted(missing)}"
    )


def test_doc_contract__phase2_triage_prescribes_the_bounded_incremental_diff():
    text = _read(PHASE2_REL)
    assert "git diff {last_reviewed_sha}..HEAD --" in text
    for match in re.finditer(
        "git diff (?:--name-only )?\\{last_reviewed_sha\\}\\.\\.\\.HEAD", text
    ):
        context = text[max(0, match.start() - 200) : match.start()]
        assert (
            re.search("[Dd]o \\*\\*not\\*\\* use|never use|do not use", context)
            is not None
        ), (
            "the three-dot incremental diff appears without a prohibition in the preceding 200 chars \u2014 the doc may be prescribing it"
        )


def test_doc_contract__post_review_defines_no_local_build_footer():
    text = _read(POST_REVIEW_REL)
    assert re.search("(?m)^def build_footer\\(", text) is None


@pytest.mark.parametrize(
    "rel, gate_marker, truncate_marker",
    [
        pytest.param(
            SKILL_REL,
            'echo "=== prior_review ==="',
            'echo "=== stale_truncate ==="',
            id="skill",
        ),
        pytest.param(
            PHASE2_REL,
            "**3. Previously-reviewed gate**",
            "**4. Truncate stale files**",
            id="phase2-triage",
        ),
    ],
)
def test_doc_contract__previously_reviewed_gate_precedes_stale_truncation_in_skill_and_phase2(
    rel, gate_marker, truncate_marker
):
    text = _read(rel)
    gate_idx = text.find(gate_marker)
    truncate_idx = text.find(truncate_marker)
    assert gate_idx != -1, f"gate section marker not found in {rel}"
    assert truncate_idx != -1, f"truncation section marker not found in {rel}"
    assert gate_idx < truncate_idx, (
        f"{rel}: the previously-reviewed gate must appear BEFORE stale-file truncation in doc order \u2014 truncation zeroes the artifacts a 'Skip \u2014 keep the existing review' answer exists to preserve, so if this ordering regresses, a repeat run at the same SHA silently destroys the prior review's findings/report before the gate can even offer to keep them"
    )
    skill = _read(SKILL_REL)
    truncate_block = skill[skill.find('echo "=== stale_truncate ==="') :]
    truncate_block = truncate_block[: truncate_block.find("```", 1)]
    assert "printf '%s\\n' \"$PRIOR_JSON\" | python3 " in truncate_block
    assert '"{plugin_root}/scripts/stale_truncate.py"' in truncate_block
    assert '--head-sha "$HEAD_SHA_SHORT"' in truncate_block
    assert "--unconditional" not in truncate_block


@pytest.mark.parametrize(
    "rel",
    [
        pytest.param(SKILL_REL, id="skill"),
        pytest.param(HEADLESS_MODE_REL, id="headless-mode"),
    ],
)
def test_doc_contract__headless_skip_semantics_agree_between_skill_and_headless_mode(
    rel,
):
    text = _read(rel)
    matching_lines = [
        line
        for line in text.splitlines()
        if "resolved.reviewed_policy" in line
        and re.search("\\bskip\\b", line, re.IGNORECASE)
        and ("stops the run" in line)
        and ("previously_reviewed" in line)
    ]
    assert matching_lines, (
        f"{rel} has no line stating the headless `skip` stop-condition for the previously-reviewed gate (resolved.reviewed_policy + skip + 'stops the run') \u2014 sync {SKILL_REL} and {HEADLESS_MODE_REL} so both describe the same behavior."
    )
    for line in matching_lines:
        assert "sha_is_ancestor" in line, (
            f"{rel}: a headless `skip`-stops-the-run statement for the previously-reviewed gate does not require `sha_is_ancestor` \u2014 this contradicts the other file; sync {SKILL_REL} and {HEADLESS_MODE_REL} so both agree that skip does NOT stop the run on rewritten history (sha_is_ancestor == false)."
        )


def test_doc_contract__phase2_triage_never_appends_to_tracked_gitignore():
    text = _read(PHASE2_REL)
    assert ">> .gitignore" not in text, (
        f"{PHASE2_REL} appends to the repo's tracked .gitignore \u2014 this dirties the reviewed user's repo with an undisclosed edit; use `.git/info/exclude` instead (matching SKILL.md's forbidding rule)."
    )


def test_doc_contract__skill_invokes_ensure_output_dir_and_has_no_gitignore_composite():
    skill = _read(SKILL_REL)
    assert "ensure_output_dir.py" in skill, (
        f"{SKILL_REL} must invoke scripts/ensure_output_dir.py in Phase 1"
    )
    assert 'echo "=== gitignore ==="' not in skill, (
        f"{SKILL_REL} still has Composite A gitignore bash \u2014 ignore establishment moved to ensure_output_dir.py in Phase 1"
    )
    assert ">> .gitignore" not in skill, (
        f"{SKILL_REL} must never append to the tracked .gitignore"
    )


def test_doc_contract__phase2_points_at_ensure_output_dir_not_independent_gitignore():
    text = _read(PHASE2_REL)
    assert "ensure_output_dir.py" in text, (
        f"{PHASE2_REL} must point at ensure_output_dir.py for ignore establishment"
    )
    assert "skip if using env var override" not in text, (
        f"{PHASE2_REL} still tells the model to skip ignore on env override \u2014 the script handles in-repo vs out-of-repo"
    )
    assert "artifacts will show as untracked files" not in text, (
        f"{PHASE2_REL} still documents disclose-and-continue for unwritable exclude \u2014 that is now a Phase 1 hard stop"
    )


@pytest.mark.parametrize(
    "rel",
    [
        pytest.param(
            "skills/code-gauntlet/references/phase8-delivery.md", id="phase8-delivery"
        ),
        pytest.param(DELIVERY_GUIDE_REL, id="delivery-guide"),
        pytest.param(PHASE1_REL, id="phase1-preflight"),
        pytest.param(
            "skills/code-gauntlet/references/review-md-spec.md", id="review-md-spec"
        ),
    ],
)
def test_doc_contract__markdown_delivery_is_path_surface_not_root_write(rel):
    phase8 = "skills/code-gauntlet/references/phase8-delivery.md"
    phase8_text = _read(phase8)
    assert "artifactPaths.report" in phase8_text, (
        f"{phase8} the Markdown only branch must name artifactPaths.report as the delivery source"
    )
    assert "do not write a new file" in phase8_text.lower().replace("**", ""), (
        f"{phase8} the Markdown only branch must forbid a fresh write"
    )
    text = _read(rel)
    assert (
        re.search(
            "[Ww]rite(?:\\s+the\\s+full\\s+report)?\\s+to\\s+`?\\./code-gauntlet-", text
        )
        is None
    ), f"{rel} still instructs writing a root-level code-gauntlet-* file"
    assert "Save as code-gauntlet-{date}.md" not in text, (
        f"{rel} still promises creating code-gauntlet-{{date}}.md"
    )
    assert "Save as `code-gauntlet-{date}.md`" not in text, (
        f"{rel} still promises creating code-gauntlet-{{date}}.md"
    )


def test_doc_contract__headless_markdown_delivery_is_path_only():
    text = _read(HEADLESS_MODE_REL)
    assert "artifactPaths.report" in text, (
        f"{HEADLESS_MODE_REL} must state headless markdown = persisted report path"
    )
    assert "no additional file is written" in text, (
        f"{HEADLESS_MODE_REL} must state no additional markdown file is written"
    )


@pytest.mark.parametrize(
    "token",
    [
        pytest.param("1e999", id="positive-overflow"),
        pytest.param("-1e999", id="negative-overflow"),
        pytest.param("1E999", id="uppercase-exponent-overflow"),
    ],
)
def test_marker_constraints__numeric_overflow_token_rejects_the_marker(token):
    text = f'<!-- {MARKER_TOKEN}: {{"version":{token},"sha":"{SHA_40}"}} -->'
    assert find_marker(text) is None
    assert detect_signal(text) is None


def test_marker_constraints__finite_numbers_still_parse():
    text = f'<!-- {MARKER_TOKEN}: {{"version":1,"findings_count":250,"sha":"{SHA_40}"}} -->'
    signal = detect_signal(text)
    assert signal is not None
    assert signal["sha"] == SHA_40


def test_marker_constraints__marker_guard_requires_this_sha():
    stale = build_marker("d" * 40, 1)
    appended = build_footer(2, SHA_40, body=stale)
    assert f'"sha":"{SHA_40}"' in appended
    recovered = detect_signal(stale + appended)
    assert recovered["sha"] == SHA_40


def test_marker_constraints__prose_guard_requires_this_sha():
    stale = "---\n" + build_prose_footer("d" * 40)
    appended = build_footer(1, SHA_40, body=stale)
    assert f"Reviewed up to: {SHA_40}" in appended


def test_marker_constraints__utc_offset_timestamps_order_by_absolute_instant():
    older, newer = ("a" * 40, "b" * 40)
    entries = [
        {
            "body": build_footer(1, older),
            "timestamp": "2026-07-26T01:00:00+02:00",
            "source": "review",
        },
        {
            "body": build_footer(1, newer),
            "timestamp": "2026-07-26T00:30:00Z",
            "source": "review",
        },
    ]
    assert select_latest(entries)["sha"] == newer
    assert select_latest(list(reversed(entries)))["sha"] == newer


def test_marker_constraints__sort_keys_stay_mutually_comparable():
    keys = [
        review_marker._sort_key(t)
        for t in (
            "2026-07-26T00:30:00Z",
            "2026-07-26T01:00:00+02:00",
            "2026-07-26T00:30:00",
        )
    ]
    assert all(k and k[0].isdigit() for k in keys), keys
    assert keys[0] == keys[2]


def test_marker_constraints__deeply_nested_marker_never_raises():
    payload = "[" * 40000 + "]" * 40000
    text = f'<!-- {MARKER_TOKEN}: {{"sha":"{SHA_40}","x":{payload}}} -->'
    try:
        assert find_marker(text) is None
    except RecursionError:
        pytest.fail("find_marker raised RecursionError")

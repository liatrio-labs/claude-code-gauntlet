"""Public delivery rights, quote checks, and platform payloads."""

import copy
import json

import pytest
from gauntlet.delivery import compose, fold, post
from gauntlet.delivery.input import validate_review_input
from gauntlet.forge import JsonFetch, PostResult, parse_remote
from gauntlet.marker import find_finding_markers, find_marker

from tests.support.posting import (
    CONTEXT,
    CONTRACT_DIFF,
    FOOTER,
    HEADER,
    SHA,
    UNKNOWN_WARNING,
    invoke_posting,
    review,
)
from tests.support.prior import prior

QUOTE = "    original_value = compute_value()\n    return original_value"
DIFF = (
    "diff --git a/foo.py b/foo.py\n--- a/foo.py\n+++ b/foo.py\n"
    "@@ -1,1 +1,4 @@\n def f():\n+    original_value = compute_value()\n"
    "+    return original_value\n+    finish_operation()\n"
)


def located(**values):
    return {
        "id": "F1",
        "file": "foo.py",
        "line_start": 2,
        "line_end": 3,
        "claimed_line_start": 200,
        "claimed_line_end": 201,
        "location_status": "relocated",
        "disposition": "deliver",
        "anchor": QUOTE,
        "line": "conflicting alias",
        "end_line": {"conflicting": "alias"},
        "title": "Range bug",
        "body": "The value is stale.",
        "severity": "high",
        "suggestion": "Return the current value.",
        "suggested_fix_code": "    return current_value()",
        **values,
    }


@pytest.fixture
def posting(tmp_path, monkeypatch, capsys, forge_factory):
    def run(data, **options):
        return invoke_posting(
            tmp_path, monkeypatch, capsys, forge_factory, data, **options
        )

    return run


def surfaces(run):
    payload = run.payload
    assert run.code == 0, run.err
    assert payload is not None
    if payload["platform"] == "github":
        return payload["payload"]["body"], payload["payload"]["comments"]
    return payload["summary"]["body"], payload["discussions"]


@pytest.mark.parametrize(
    "platform,stamp,status",
    [
        ("github", "deliver", "confirmed"),
        ("gitlab", "body", "confirmed"),
        ("github", "held_back", "confirmed"),
        ("gitlab", "report_only", "confirmed"),
        ("github", "eliminate", "confirmed"),
        ("gitlab", None, "confirmed"),
        ("github", "unknown", "confirmed"),
        ("github", "deliver", "unconfirmed"),
    ],
)
def test_disposition_location_surfaces(posting, platform, stamp, status):
    member = located(disposition=stamp, location_status=status, title="Private claim")
    if stamp is None:
        member.pop("disposition")
    before = copy.deepcopy(member)
    findings = [member]
    if stamp is None:
        findings.append(located(id="held", disposition="held_back"))
    summary, comments = surfaces(
        posting(review(platform, findings, review_body=""), diff=DIFF)
    )
    assert member == before
    assert len(comments) == int(stamp == "deliver" and status == "confirmed")
    assert (
        "Private claim" in summary
        or any("Private claim" in c["body"] for c in comments)
    ) == (stamp in ("deliver", "body"))
    assert "```suggestion" not in summary
    assert ("```suggestion" in compose.render_finding_sections(member)) == (
        stamp == "deliver" and status == "confirmed"
    )


@pytest.mark.parametrize("platform", ["github", "gitlab"])
def test_legacy_file_uses_location_rights(posting, platform):
    findings = [
        located(title="Confirmed claim"),
        located(id="U", title="Unconfirmed claim", location_status="unconfirmed"),
        {
            "file": "foo.py",
            "line": 2,
            "end_line": 3,
            "title": "Legacy aliases",
            "body": "Caller supplied body",
        },
    ]
    for finding in findings:
        finding.pop("disposition", None)
    data = review(platform, findings, review_body="")
    before = copy.deepcopy(data)
    summary, comments = surfaces(posting(data, diff=DIFF))
    assert data == before
    assert len(comments) == 1
    assert "Confirmed claim" in comments[0]["body"]
    assert "```suggestion" in comments[0]["body"]
    assert "Unconfirmed claim" in summary and "Legacy aliases" in summary
    assert "location unconfirmed" in summary and "```suggestion" not in summary


def test_body_disposition_digest_and_malformed_reasons(posting):
    body_findings = [
        located(
            id=f"BODY-{i}",
            disposition="body",
            dimension=dimension,
            title=f"Body claim {i}",
        )
        for i, dimension in enumerate(("security", "bug", "security"))
    ]
    digest, comments = surfaces(
        posting(review("github", body_findings, review_body=""), diff=DIFF)
    )
    assert comments == []
    assert digest.count("### security\n") == digest.count("### bug\n") == 1
    assert all(digest.count(f"Body claim {i}") == 1 for i in range(3))
    malformed = located(disposition="deliver", disposition_reasons="forged")
    summary, comments = surfaces(
        posting(review("github", [malformed], review_body=""), diff=DIFF)
    )
    assert not comments and "Range bug" not in summary


@pytest.mark.parametrize("platform", ["github", "gitlab"])
@pytest.mark.parametrize("status", [None, "unknown", "unconfirmed"])
@pytest.mark.parametrize("primary", [False, True])
def test_unconfirmed_delivery_is_summary_only(platform, status, primary, posting):
    unresolved = located(
        id="U",
        title="Unresolved observation",
        location_status=status,
        consolidation_key="k",
        consolidation_primary=primary,
    )
    if status is None:
        unresolved.pop("location_status")
    resolved = located(consolidation_key="k", consolidation_primary=not primary)
    data = review(platform, [resolved, unresolved])
    before = copy.deepcopy(data)
    normalized = validate_review_input(data)
    assert data == before
    assert "line" not in normalized["findings"][1]
    summary, comments = surfaces(posting(data, diff=DIFF))
    assert len(comments) == int(not primary)
    if primary:
        assert "Unresolved observation" in summary
        assert "foo.py (location unconfirmed)" in summary
    else:
        assert "Unresolved observation" not in summary
        assert "Unresolved observation" in comments[0]["body"]
    assert "```suggestion" not in summary
    assert "998" not in summary and "999" not in summary and "200" not in summary


@pytest.mark.parametrize("platform", ["github", "gitlab"])
@pytest.mark.parametrize("anchor", [None, "another_source_value()", "original_value"])
def test_wrong_quote_cannot_authorize_inline(platform, anchor, posting):
    summary, comments = surfaces(
        posting(review(platform, [located(anchor=anchor)]), diff=DIFF)
    )
    assert comments == []
    assert "location unconfirmed" in summary
    assert "Range bug" in summary and "Return the current value." in summary
    assert "```suggestion" not in summary


@pytest.mark.parametrize("platform", ["github", "gitlab"])
@pytest.mark.parametrize("status", ["confirmed", "relocated"])
def test_fragment_posts_inline_without_fence(platform, status, posting):
    member = located(
        location_status=status,
        line_end=2,
        anchor="  compute_value()  ",
        claimed_line_end=200,
    )
    summary, comments = surfaces(posting(review(platform, [member]), diff=DIFF))
    assert len(comments) == 1
    comment = comments[0]
    assert (comment.get("position") or comment)[
        "new_line" if platform == "gitlab" else "line"
    ] == 2
    assert "```suggestion" not in comment["body"]
    assert "Return the current value." in comment["body"]
    assert "location unconfirmed" not in summary


@pytest.mark.parametrize("platform", ["github", "gitlab"])
def test_canonical_quote_controls_range_and_fence(platform, posting):
    _, comments = surfaces(posting(review(platform, [located()]), diff=DIFF))
    comment = comments[0]
    if platform == "github":
        assert {k: v for k, v in comment.items() if k != "body"} == {
            "path": "foo.py",
            "start_line": 2,
            "start_side": "RIGHT",
            "line": 3,
            "side": "RIGHT",
        }
        assert "```suggestion\n    return current_value()\n```" in comment["body"]
    else:
        assert comment["position"] == {
            "position_type": "text",
            "base_sha": "base1",
            "head_sha": "head1",
            "start_sha": "start1",
            "new_path": "foo.py",
            "new_line": 2,
            "old_path": "foo.py",
        }
        assert "```suggestion:-0+1\n    return current_value()\n```" in comment["body"]


@pytest.mark.parametrize("platform", ["github", "gitlab"])
def test_missing_diff_keeps_observation_in_body(platform, posting):
    summary, comments = surfaces(
        posting(review(platform, [located()]), diff_status=128, diff_error="denied")
    )
    assert not comments
    assert "#### `foo.py:2`" in summary and "Range bug" in summary
    assert "```suggestion" not in summary


@pytest.mark.parametrize("platform", ["github", "gitlab"])
@pytest.mark.parametrize("status", ["confirmed", "relocated"])
@pytest.mark.parametrize("start,end", [(40, 41), (4, 5)])
def test_off_diff_location_keeps_canonical_body_heading(
    platform, status, start, end, posting
):
    run = posting(
        review(
            platform,
            [located(location_status=status, line_start=start, line_end=end)],
        ),
        diff=DIFF,
    )
    summary, comments = surfaces(run)
    assert comments == []
    assert f"#### `foo.py:{start}`" in summary
    assert "```suggestion" not in summary
    assert "line not found in diff" in run.err


def test_off_diff_group_warning_lists_valid_lines_and_members(posting):
    primary = located(
        line_start=40,
        line_end=41,
        consolidation_key="k",
        consolidation_primary=True,
    )
    member = located(id="F2", title="Member", consolidation_key="k")
    for finding in (primary, member):
        finding.pop("suggested_fix_code")
    run = posting(review("github", [primary, member]), diff=DIFF)
    assert run.payload["skipped"] == [
        "Skipping finding 'Range bug' at foo.py:40 \u2014 line not found in diff."
        " Valid lines for this file: [1, 2, 3, 4] [group members: F1, F2]"
    ]


@pytest.mark.parametrize("platform", ["github", "gitlab"])
def test_overlap_withholds_only_later_fence(platform, posting):
    second = located(
        id="F2",
        title="Second",
        line_start=3,
        line_end=4,
        anchor="    return original_value\n    finish_operation()",
    )
    _, comments = surfaces(posting(review(platform, [located(), second]), diff=DIFF))
    assert len(comments) == 2
    assert "```suggestion" in comments[0]["body"]
    assert "```suggestion" not in comments[1]["body"]
    assert "Return the current value." in comments[1]["body"]


def test_gitlab_rejected_primary_promotes_resolved_member(posting):
    primary = located(consolidation_key="k", consolidation_primary=True)
    member = located(
        id="F2",
        title="Member",
        line_start=4,
        line_end=4,
        anchor="finish_operation()",
        claimed_line_end=200,
        consolidation_key="k",
    )
    run = posting(
        review("gitlab", [primary, member]),
        diff=DIFF,
        dry_run=False,
        submissions={
            "notes": [PostResult({}, None, None)],
            "discussions": [
                PostResult(None, "denied", None),
                PostResult({}, None, None),
            ],
        },
    )
    assert run.code == 0
    discussions = [r for r in run.requests if r.endpoint.endswith("/discussions")]
    assert len(discussions) == 2
    assert "Corroborating finding" in discussions[0].payload["body"]
    assert discussions[1].payload["position"]["new_line"] == 4
    assert "Corroborating finding" not in discussions[1].payload["body"]


def test_gitlab_retry_posts_only_missing_member(posting):
    primary = located(consolidation_key="k", consolidation_primary=True)
    member = located(
        id="F2",
        title="Member",
        line_start=4,
        line_end=4,
        anchor="finish_operation()",
        claimed_line_end=200,
        consolidation_key="k",
    )
    first = posting(review("gitlab", [primary, member]), diff=DIFF, dry_run=False)
    body = next(
        r.payload["body"] for r in first.requests if r.endpoint.endswith("/discussions")
    )
    markers = find_finding_markers(body)
    assert len(markers) == 2
    rerun = posting(
        review("gitlab", [primary, member]),
        diff=DIFF,
        dry_run=False,
        entries=prior(
            compose.finding_key(
                "foo.py", 2, primary["title"], compose.key_material_body(primary)
            )
        ),
    )
    assert len(rerun.requests) == 1
    assert rerun.requests[0].payload["position"]["new_line"] == 4
    assert "Member" in rerun.requests[0].payload["body"]


def test_gitlab_legacy_group_rerun_skips_render_and_count(posting):
    primary = located(consolidation_key="k", consolidation_primary=True)
    member = located(
        id="F2",
        title="Member",
        line_start=4,
        line_end=4,
        anchor="finish_operation()",
        consolidation_key="k",
    )
    range_key = compose.finding_key(
        "foo.py", 2, primary["title"], compose.key_material_body(primary)
    )
    run = posting(
        review("gitlab", [primary, member]),
        diff=DIFF,
        dry_run=False,
        entries=prior(range_key, legacy=(range_key,)),
    )
    assert run.code == 0
    assert run.requests == ()
    assert "2 inline discussion(s) already on the MR" in run.out
    assert "suggested fix(es)" not in run.out


def test_gitlab_retry_posts_only_missing_primary(posting):
    primary = located(consolidation_key="k", consolidation_primary=True)
    member = located(
        id="F2",
        title="Member",
        line_start=4,
        line_end=4,
        anchor="finish_operation()",
        consolidation_key="k",
    )
    key = compose.finding_key(
        "foo.py", 4, member["title"], compose.key_material_body(member)
    )
    run = posting(
        review("gitlab", [primary, member]),
        diff=DIFF,
        dry_run=False,
        entries=prior(key),
    )
    assert run.code == 0 and len(run.requests) == 1
    assert run.requests[0].payload["position"]["new_line"] == 2
    assert "Corroborating finding" not in run.requests[0].payload["body"]
    assert "1 inline discussion(s) already on the MR" in run.out


def test_gitlab_rerun_keys_resolved_path(posting):
    member = located(file="b/foo.py")
    key = compose.finding_key(
        "foo.py", 2, member["title"], compose.key_material_body(member)
    )
    run = posting(
        review("gitlab", [member]),
        diff=DIFF,
        dry_run=False,
        entries=prior(key),
    )
    assert run.code == 0 and run.requests == ()


def test_gitlab_rerun_suppresses_fold_notice(posting, monkeypatch):
    monkeypatch.setitem(
        fold.PLATFORM_BODY_LIMITS["gitlab"]["surfaces"]["discussion"], "bytes", 500
    )
    member = located(body="X" * 1000)
    key = compose.finding_key(
        "foo.py", 2, member["title"], compose.key_material_body(member)
    )
    run = posting(
        review("gitlab", [member]),
        diff=DIFF,
        dry_run=False,
        entries=prior(key),
    )
    assert run.code == 0 and run.requests == () and run.err == ""


@pytest.mark.parametrize("platform,limit", [("github", 65536), ("gitlab", 1000000)])
def test_summary_envelope_blocks_submission(platform, limit, posting, monkeypatch):
    monkeypatch.setattr(
        compose,
        "compose_review_body",
        lambda *args, **kwargs: compose.ComposedBody(
            "\u754c" * (limit // 3 + 1), 0, 0, 0, ()
        ),
    )
    run = posting(review(platform), dry_run=False)
    assert run.code == 1 and run.requests == ()
    assert "body limit; nothing was posted" in run.err


def test_existing_gitlab_summary_suppresses_guard_and_notices(posting, monkeypatch):
    monkeypatch.setattr(
        compose,
        "compose_review_body",
        lambda *args, **kwargs: compose.ComposedBody(
            "\u754c" * 333334,
            0,
            1,
            100,
            (compose.OmittedEntry("foo.py:40", "Range bug"),),
        ),
    )
    run = posting(review("gitlab"), dry_run=False, entries=prior())
    assert run.code == 0 and run.requests == () and run.err == ""
    assert "body limit" not in run.out


def test_gitlab_summary_reports_omission_after_capture(posting):
    run = posting(
        review(
            "gitlab",
            [located(line_start=40, line_end=41)],
            review_body="X" * 1000000,
        ),
        diff=DIFF,
    )
    summary, _ = surfaces(run)
    assert "[folded:" in summary and "Range bug" not in summary
    assert "Skipped finding 'Range bug' at foo.py:40 not shown" in run.err
    assert run.out.index("MR summary note captured") < run.out.index(
        "1 skipped finding(s) not shown"
    )
    assert "review_body folded by" in run.out


def test_gitlab_envelope_checked_before_dedup_keeps_sibling(posting, monkeypatch):
    monkeypatch.setitem(
        fold.PLATFORM_BODY_LIMITS["gitlab"]["surfaces"]["discussion"], "bytes", 200
    )
    primary = located(body="X" * 500)
    key = compose.finding_key(
        "foo.py", 2, primary["title"], compose.key_material_body(primary)
    )
    run = posting(
        review("gitlab", [primary, CONTEXT]),
        diff=DIFF + CONTRACT_DIFF,
        dry_run=False,
        entries=prior(key),
    )
    assert run.code == 0
    assert len(run.requests) == 1
    assert run.requests[0].payload["position"]["new_path"] == "src/edited.py"
    assert "body limit; skipping this delivery" in run.err


@pytest.mark.parametrize("platform,limit", [("github", 65536), ("gitlab", 1000000)])
def test_summary_folding_preserves_footer(platform, limit, posting):
    run = posting(
        review(
            platform,
            [located(location_status="unconfirmed")],
            review_body="X" * (limit + 100),
        )
    )
    summary, comments = surfaces(run)
    assert not comments
    assert len(summary.encode()) <= limit
    assert "[folded:" in summary
    assert "Reviewed up to: " + SHA in summary


@pytest.mark.parametrize(
    "platform,surface", [("github", "inline"), ("gitlab", "discussion")]
)
def test_oversized_inline_is_not_submitted(platform, surface, posting, monkeypatch):
    monkeypatch.setitem(
        fold.PLATFORM_BODY_LIMITS[platform]["surfaces"][surface], "bytes", 100
    )
    run = posting(review(platform, [located(body="X" * 500)]), diff=DIFF, dry_run=False)
    assert all(not r.endpoint.endswith("/discussions") for r in run.requests)
    if platform == "github":
        assert run.code == 1 and not run.requests
    else:
        assert run.code == 1 and len(run.requests) == 1
    assert "body limit" in run.err


@pytest.mark.parametrize("refs", [JsonFetch([], None), JsonFetch(None, "denied")])
def test_gitlab_version_failure_posts_nothing(refs, posting):
    run = posting(review("gitlab", [located()]), diff=DIFF, refs=refs, dry_run=False)
    assert run.code == 1 and not run.requests


@pytest.mark.parametrize(
    "data,message",
    [
        ({"owner": [], "repo": "r", "pr_number": 1}, "owner must be a string"),
        ({"owner": "o", "repo": "r", "pr_number": True}, "pr_number must be"),
        (
            {"owner": "o", "repo": "r", "pr_number": 1, "findings": [None]},
            "must be an object",
        ),
    ],
)
def test_invalid_review_input(data, message, posting):
    run = posting(data)
    assert run.code == 1 and message in run.err and not run.requests


def test_capture_is_repeatable_and_preserves_input(posting, tmp_path):
    data = review(findings=[located()])
    first = posting(data, diff=DIFF)
    second = posting(data, diff=DIFF)
    assert first.raw == second.raw
    assert json.loads((tmp_path / "findings.json").read_text(encoding="utf-8")) == data


def test_composer_defensively_denies_unconfirmed_fence():
    member = located(location_status="unconfirmed")
    assert "```suggestion" not in compose.render_comment_body(member)
    section = compose.build_skipped_section(
        [compose.SkippedEntry("foo.py", 999, member)]
    )
    assert "foo.py (location unconfirmed)" in section and "999" not in section
    member["location_status"] = "confirmed"
    section = compose.build_skipped_section(
        [compose.SkippedEntry("foo.py", 999, member)]
    )
    assert "foo.py:2" in section and "999" not in section


GH_ARGS = (
    "--owner",
    "o",
    "--repo",
    "r",
    "--pr-number",
    "5",
    "--platform",
    "github",
    "--sha",
    SHA,
)
EMPTY_SUMMARY = HEADER + "Summary" + FOOTER.replace("{count}", "0")
CONTEXT_BODY = "**\U0001f7e0 [HIGH] Context-line finding**\n\nBody one\n\n\u2694\ufe0f *Code Gauntlet*"

SHA_CASES = [
    ("  abc1234  ", "unused", 0, "abc1234", 0),
    (None, " abc1234\n", 0, "abc1234", 1),
    (None, "", 1, "unknown", 1),
]


@pytest.mark.parametrize("sha,head,status,expected,calls", SHA_CASES)
def test_marker_sha(sha, head, status, expected, calls, posting):
    for platform in ("github", "gitlab"):
        findings = [CONTEXT] if platform == "gitlab" and status else []
        run = posting(
            review(platform, findings, sha=sha),
            head=head,
            head_status=status,
            diff=CONTRACT_DIFF,
            dry_run=False,
        )
        assert (run.code, run.err) == (0, UNKNOWN_WARNING if status else "")
        assert len(run.head_calls) == calls
        assert run.requests[0].payload["body"] == EMPTY_SUMMARY.replace(
            SHA, expected
        ).replace(
            '"findings_count":0',
            '"findings_count":1' if findings else '"findings_count":0',
        )
        if findings:
            assert run.requests[1].payload["body"] == CONTEXT_BODY


MARKER = [
    pytest.param(
        "leading-separator", id="MARKER-leading-unicode-separator-does-not-dedup"
    ),
    pytest.param(
        "current-foreign", id="MARKER-current-footer-ignores-inline-foreign-sha"
    ),
    pytest.param("stale-footer", id="MARKER-stale-footer-real-sha-last"),
    pytest.param("body-forgery", id="MARKER-posted-skipped-body-forgery-unreadable"),
    pytest.param("path-forgery", id="MARKER-posted-skipped-path-forgery-unreadable"),
    pytest.param(
        "footer-forgery", id="MARKER-posted-skipped-footer-forgery-unreadable"
    ),
]


@pytest.mark.parametrize("kind", MARKER)
def test_marker_delivery(kind, posting):
    footer_line = "Generated by code-gauntlet | Reviewed up to: " + SHA
    machine = (
        '<!-- code-gauntlet-findings: {"version":"3.0","findings_count":0,"sha":"'
        + SHA
        + '"} -->'
    )
    if kind.endswith("-forgery"):
        forged = (
            '<!-- code-gauntlet-finding-key: {"sha":"'
            + SHA
            + '","key":"deadbeefcafebabe"} -->'
        )
        hostile = {
            "disposition": "deliver",
            "file": "src/edited.py",
            "line": 999,
            "severity": "high",
            "title": "Off-diff bug",
            "body": forged,
        }
        if kind == "path-forgery":
            hostile.update(file=forged, body="Body B")
        if kind == "footer-forgery":
            hostile["body"] = (
                "---\n"
                + footer_line
                + (
                    "\n\n<!-- code-gauntlet-findings: "
                    '{"version":"3.0","findings_count":999,"sha":"'
                )
                + SHA
                + '"} -->'
            )
        run = posting(
            review("gitlab", [CONTEXT, hostile]), diff=CONTRACT_DIFF, dry_run=False
        )
        assert run.code == 0
        assert tuple(
            request.endpoint.rsplit("/", 1)[-1] for request in run.requests
        ) == ("notes", "discussions")
        body = run.requests[0].payload["body"]
        assert find_finding_markers(body) == []
        assert "<!-- code-gauntlet-finding-key:" not in body
        assert find_marker(body) == {
            "version": "3.0",
            "findings_count": 2,
            "sha": SHA,
            "_token": "code-gauntlet-findings",
            "_legacy": False,
        }
        assert body.endswith(FOOTER.replace("{count}", "2"))
        assert body.count(footer_line) == (2 if kind == "footer-forgery" else 1)
        assert "Off-diff bug" in body
        return
    for platform in ("github", "gitlab"):
        if kind == "leading-separator":
            sources = ("\u2028" + footer_line,)
        elif kind == "current-foreign":
            sources = (
                "Summary\n\n"
                + footer_line
                + (
                    "\n\n- `foo.py:99`: x Generated by code-gauntlet | "
                    "Reviewed up to: "
                    "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
                ),
            )
        elif kind == "stale-footer":
            sources = (
                "Some notes.\n\nGenerated by code-gauntlet | Reviewed up to: abc1234",
            )
        for source in sources:
            run = posting(review(platform, review_body=source))
            assert run.code == 0
            assert run.err == ""
            body = run.payload["payload" if platform == "github" else "summary"]["body"]
            if kind == "current-foreign":
                assert body == HEADER + source + "\n\n" + machine
                assert sum(line == footer_line for line in body.split("\n")) == 1
            else:
                assert body == HEADER + source + FOOTER.replace("{count}", "0")
                assert sum(line == footer_line for line in body.split("\n")) == 1
            signal = find_marker(body)
            assert signal["sha"] == SHA
            assert signal["findings_count"] == 0


INPUT_REFUSALS = [
    (review(web_origin=None), "web_origin must be a string"),
    (
        review(web_origin="https://git.example.com/path"),
        "web_origin: web_origin must be an http(s) origin without a path, query or fragment",
    ),
    (7, "Findings JSON must be an object or an array."),
    ({"repo": "r"}, "Missing required field in findings JSON: 'owner'"),
    (review(owner=7), "owner must be a string"),
    (
        review(platform="BitBucket"),
        "Unsupported platform: 'bitbucket'. Use 'github' or 'gitlab'.",
    ),
    ({**review(), "findings": None}, "findings must be an array"),
    (review(findings=[None]), "findings[0] must be an object"),
]


@pytest.mark.parametrize("data,error", INPUT_REFUSALS)
def test_invalid_input_preserves_existing_artifact(
    data, error, tmp_path, monkeypatch, capsys, forge_factory
):
    artifact = tmp_path / "post-review-payload.json"
    artifact.write_bytes(b"sentinel artifact\n")
    run = invoke_posting(
        tmp_path,
        monkeypatch,
        capsys,
        forge_factory,
        data,
        dry_run=False,
        keep_existing_artifact=True,
    )
    assert (run.code, run.out, run.err) == (1, "", "post_review: " + error + "\n")
    assert run.forge_factory_calls == run.head_calls == run.requests == ()
    assert run.raw == b"sentinel artifact\n"


@pytest.mark.parametrize("platform", ["github", "gitlab"])
def test_review_input_defaults_and_unknown_fields(platform, posting):
    data = review(platform)
    del data["findings"]
    run = posting(data)
    assert (run.code, run.err) == (0, "")
    assert (
        run.payload["payload" if platform == "github" else "summary"]["body"]
        == EMPTY_SUMMARY
    )
    data = review(platform, [located(future={"nested": [1]})], future_wrapper=7)
    run = posting(data, diff=DIFF)
    assert (run.code, run.err) == (0, "")
    comments = (
        run.payload["payload"]["comments"]
        if platform == "github"
        else run.payload["discussions"]
    )
    assert "Range bug" in comments[0]["body"]
    assert "Return the current value." in comments[0]["body"]


@pytest.mark.parametrize(
    "data",
    [
        [],
        review(owner="other", repo="other", pr_number=9, sha="bbbbbbb", review_body=""),
    ],
)
def test_review_input_overrides_and_report(data, posting, tmp_path):
    report = tmp_path / "report.md"
    report.write_text(
        "# Review\n\n## Summary\n\nFrom report\n\n## Findings\n", encoding="utf-8"
    )
    run = posting(
        data,
        arguments=(*GH_ARGS, "--report", str(report)),
    )
    assert (run.code, run.err, run.head_calls) == (0, "", ())
    assert run.payload["endpoint"] == "repos/o/r/pulls/5/reviews"
    assert run.payload["payload"]["body"] == HEADER + "From report" + FOOTER.replace(
        "{count}", "0"
    )
    existing = posting(review(), arguments=("--report", "missing-report.md"))
    assert (existing.code, existing.err) == (0, "")
    assert existing.payload["payload"]["body"] == EMPTY_SUMMARY


def test_detected_platform(posting, monkeypatch):
    monkeypatch.setattr(
        post, "origin_remote", lambda: parse_remote("git@github.com:o/r")
    )
    data = review()
    del data["platform"]
    run = posting(data)
    assert (run.code, run.err) == (0, "")
    assert run.out.startswith("Detected platform: github (from git remote: github.com)")
    assert run.payload["platform"] == "github"


def test_gitlab_identity_failure_warns_and_posts_without_dedup(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from gauntlet.forge import Poster
    from gauntlet.prior_review import PriorDelivery

    from tests.support.forge import FakeGitLab

    failure = (
        "could not resolve posting identity: /user denied; set CODE_GAUNTLET_POSTER"
    )
    fake = FakeGitLab(
        entries=[JsonFetch([], "gitlab notes denied")],
        posters=[Poster(None, failure)],
    )
    state = post.gitlab_prior_delivery(
        "o", "r", 5, SHA, forge=fake, session=post.DeliverySession(dry_run=False)
    )
    assert state == PriorDelivery(
        False, frozenset(), frozenset(), f"gitlab notes denied; {failure}"
    )
    warning = capsys.readouterr().err
    assert failure in warning and "posting them" in warning


@pytest.mark.parametrize(
    "origin, arguments, host",
    [
        (None, (), None),
        ("https://Git.Example.COM:8443/", (), "git.example.com"),
        (
            "invalid",
            ("--web-origin", "http://Override.Example:8080"),
            "override.example",
        ),
    ],
)
def test_posting_origin(origin, arguments, host, posting):
    data = review()
    if origin is not None:
        data["web_origin"] = origin
    run = posting(data, arguments=arguments)
    assert (run.code, run.err) == (0, "")
    assert run.forge_factory_calls == (("github", host),)


@pytest.mark.parametrize("platform", ["github", "gitlab"])
def test_position_delivery(platform, posting):
    patch = (
        "diff --git a/old_name.py b/new_name.py\nrename from old_name.py\nrename to new_name.py\n"
        "--- a/old_name.py\n+++ b/new_name.py\n@@ -1,3 +1,3 @@\n def f():\n"
        "-    previous_value = compute_value()\n+    original_value = compute_value()\n"
        "     return original_value\n"
    )
    _, comments = surfaces(
        posting(review(platform, [located(file="new_name.py")]), diff=patch)
    )
    comment = comments[0]
    if platform == "github":
        assert comment["path"] == "new_name.py" and comment["line"] == 3
    else:
        assert comment["position"]["new_path"] == "new_name.py"
        assert comment["position"]["old_path"] == "old_name.py"
        assert "old_line" not in comment["position"]


def test_capture_bare_array_and_wrapper_bytes(posting):
    member = located()
    bare = posting([member], arguments=GH_ARGS, diff=DIFF)
    wrapped = posting(review(findings=[member], review_body=""), diff=DIFF)
    assert bare.code == wrapped.code == 0
    assert bare.raw == wrapped.raw
    assert len(bare.payload["payload"]["comments"]) == 1


def test_string_pr_number_and_explicit_group_primary(posting):
    members = [
        located(consolidation_key="g"),
        located(
            id="F2",
            title="Primary",
            consolidation_key="g",
            consolidation_primary=True,
            line_start=4,
            line_end=4,
            anchor="finish_operation()",
        ),
    ]
    run = posting(review(pr_number="5", findings=members), diff=DIFF)
    _, comments = surfaces(run)
    assert run.payload["endpoint"] == "repos/o/r/pulls/5/reviews"
    assert len(comments) == 1 and comments[0]["line"] == 4
    assert comments[0]["body"].startswith("**\U0001f7e0 [HIGH] Primary**")
    assert "Range bug" in comments[0]["body"]


def test_capture_gl_multibyte_boundary(posting, monkeypatch):
    monkeypatch.setitem(
        fold.PLATFORM_BODY_LIMITS["gitlab"]["surfaces"]["discussion"], "bytes", 500
    )
    member = located(body="\u754c" * 700)
    dry = posting(review("gitlab", [member]), diff=DIFF)
    _, comments = surfaces(dry)
    live = posting(review("gitlab", [member]), diff=DIFF, dry_run=False)
    assert live.code == 0
    discussion = next(r for r in live.requests if r.endpoint.endswith("/discussions"))
    body = discussion.payload["body"]
    assert len(body.encode("utf-8")) <= 500
    markers = find_finding_markers(body)
    assert len(markers) == 1
    suffix = post._delivery_marker_suffix(SHA, [markers[0]["key"]])
    assert body.removesuffix(suffix) == comments[0]["body"]
    assert "[folded:" in body

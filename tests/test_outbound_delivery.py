"""Tests for the text that the delivery composers place in comment bodies."""

import contextlib
import hashlib
import io
import json
import sys
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast
from unittest.mock import patch

import gauntlet.delivery.post as post_review
import gauntlet.marker as review_marker
import gauntlet.text as outbound_text
import pytest
from gauntlet.delivery import compose
from gauntlet.delivery.input import Finding, ReviewInput
from gauntlet.forge import JsonFetch, Platform, PostRequest, PostResult, ReviewTarget
from gauntlet.marker import FindingMarkerWire
from gauntlet.prior_review import PriorDelivery

from tests.support.delivery import finding as delivery_finding
from tests.support.diff import diff_facts
from tests.support.forge import FakeForge, FakeForgeFactory, FakeGitLab, ForgeCall
from tests.support.outbound_assertions import (
    assert_outbound_string_invariant as _assert_outbound_string_invariant,
)
from tests.support.prior import prior_notes

SHA = "a" * 40
FAKE_FINDING_MARKER = review_marker.build_finding_marker(SHA, "0123456789abcdef")
pytestmark = pytest.mark.usefixtures("poster_state")
POISON_FIELDS = (
    "title",
    "body",
    "suggestion",
    "claude_md_rule",
    "spec_text",
    "agent",
    "dimension",
)


def test_direct_sessions_interleave_live_and_dry_delivery(
    capsys: pytest.CaptureFixture[str],
) -> None:
    live = post_review.DeliverySession(dry_run=False)
    dry = post_review.DeliverySession(dry_run=True)
    facts = diff_facts({("x.py", 2): None}, line_texts={("x.py", 2): "old"})
    wrapper = {"owner": "o", "repo": "r", "pr_number": 7, "sha": SHA}
    finding = {"file": "x.py", "line": 2, "title": "T", "body": "b"}

    class InterleavingForge(FakeGitLab):
        def submit(self, request: PostRequest) -> PostResult:
            if request.endpoint.endswith("/notes"):
                post_review.post_github(
                    cast(
                        ReviewInput,
                        {
                            **wrapper,
                            "findings": [{**finding, "suggested_fix_code": "new"}],
                        },
                    ),
                    facts,
                    forge=FakeForge(),
                    session=dry,
                )
            return super().submit(request)

    version = {
        "base_commit_sha": "base",
        "head_commit_sha": "head",
        "start_commit_sha": "start",
    }
    forge = InterleavingForge(refs=[JsonFetch([version], None)])
    assert (
        post_review.post_gitlab(
            cast(
                ReviewInput,
                {
                    **wrapper,
                    "findings": [
                        {**finding, "end_line": 2, "suggested_fix_code": "new"}
                    ],
                },
            ),
            facts,
            forge=forge,
            session=live,
        )
        == 0
    )
    dry_payload = dry.dry_run_payload("github")
    assert dry_payload["platform"] == "github"
    assert dry_payload["payload"]["comments"] == [
        {
            "path": "x.py",
            "line": 2,
            "side": "RIGHT",
            "body": "**\U0001f4a1 [LOW] T**\n\nb\n\n\u2694\ufe0f *Code Gauntlet*",
        }
    ]
    assert dry_payload["skipped"] == [
        "suggested-fix downgraded: x.py:2 (missing_end_line)"
    ]
    assert live.captured == []
    assert live.skipped == []
    discussions = [
        call.request.payload
        for call in forge.calls
        if call.request is not None and call.request.endpoint.endswith("/discussions")
    ]
    assert len(discussions) == 1
    discussion_body = discussions[0]["body"]
    assert isinstance(discussion_body, str)
    assert "```suggestion\nnew\n```" in discussion_body
    out, err = capsys.readouterr()
    assert err == "WARNING: suggested-fix downgraded: x.py:2 (missing_end_line)\n"
    assert out == (
        "Review captured (dry-run).\n  1 inline comment(s) captured.\n"
        "  0 suggested fix(es) passed the apply-check.\n"
        "  1 suggested fix(es) downgraded to prose.\n"
        "MR summary note posted.\n  1 inline discussion(s) posted.\n"
        "  1 suggested fix(es) passed the apply-check.\n"
        "  0 suggested fix(es) downgraded to prose.\n"
    )


@pytest.mark.parametrize(
    ("agent", "dimension", "confidence", "expected_identity"),
    [
        pytest.param(
            "[[a",
            "|https://example.test/p.png]]",
            "0.9",
            "[\uff3ba (|https://example.test/p.png]], confidence 0.9)",
            id="agent_dimension",
        ),
    ],
)
def test_corroborator_cross_field_wikilink(
    agent: str, dimension: str, confidence: str, expected_identity: str
) -> None:
    primary: dict[str, object] = {
        "file": "src/edited.py",
        "line": 2,
        "severity": "high",
        "title": "Primary",
        "body": "Primary body",
    }
    corroborator: dict[str, object] = {
        "agent": agent,
        "dimension": dimension,
        "confidence": confidence,
        "title": "Corroboration",
        "body": "Corroborating body",
    }
    body = compose.render_group_body(primary, [corroborator])
    _assert_outbound_string_invariant(body)
    assert expected_identity in body


def _deliver(
    platform: Platform,
    findings: list[Finding],
    review_body: str = "",
    *,
    live: bool = False,
    prior: PriorDelivery | None = None,
    reject_first: bool = False,
    lines: dict[tuple[str, int], int | None] | None = None,
    texts: dict[tuple[str, int], str] | None = None,
) -> tuple[
    list[PostRequest]
    | post_review.GitHubDryRunPayload
    | post_review.GitLabDryRunPayload,
    list[ForgeCall],
]:
    if lines is None:
        lines = {("src/edited.py", 2): None}
    if texts is None:
        texts = {("src/edited.py", 2): "changed"}
    data: ReviewInput = {
        "owner": "o",
        "repo": "r",
        "pr_number": 7,
        "sha": SHA,
        "review_body": review_body,
        "findings": findings,
    }
    success = PostResult({}, None, None)
    fake = (
        FakeForge()
        if platform == "github"
        else FakeGitLab(
            refs=[
                JsonFetch(
                    [
                        {
                            "base_commit_sha": "base",
                            "head_commit_sha": "head",
                            "start_commit_sha": "start",
                        }
                    ],
                    None,
                )
            ],
            entries=[prior_notes(prior, SHA)],
            submissions={
                "notes": [success] * (len(findings) + 1),
                "discussions": (
                    [PostResult(None, "position rejected", None)]
                    if reject_first
                    else [success]
                )
                + [success] * (2 * len(findings)),
            },
        )
    )
    session = post_review.DeliverySession(dry_run=not live)
    with (
        contextlib.redirect_stdout(io.StringIO()),
        contextlib.redirect_stderr(io.StringIO()),
    ):
        if platform == "github":
            post_review.post_github(
                data, diff_facts(lines, line_texts=texts), forge=fake, session=session
            )
        else:
            assert isinstance(fake, FakeGitLab)
            post_review.post_gitlab(
                data,
                diff_facts(
                    lines,
                    line_texts=texts,
                    new_files=set(),
                    old_paths={path: path for path, _ in lines},
                ),
                forge=fake,
                session=session,
            )
        payload = (
            [
                call.request
                for call in fake.calls
                if call.method == "submit" and call.request is not None
            ]
            if live
            else session.dry_run_payload(platform)
        )
    return payload, [call for call in fake.calls if call.method == "review_entries"]


def _poison(key: str) -> str:
    marker_key = hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]
    marker = review_marker.build_finding_marker("b" * 40, marker_key)
    return (
        f"@zz363{key} <ins data-zz363{key}> {marker} ```` &#38;#64;zz363{key}"
        f"\n/zz377{key}\n>>>\n```\n/zz377{key}\n```"
        "\n![a](u)"
        "\n[critical]: u"
        "\nghp_" + "A" * 10 + "\u200b" + "A" * 20
    )


def _assert_prepared_poison(
    body: str,
    expected_markers: Iterable[FindingMarkerWire] = (),
) -> None:
    assert "@zz363" not in body
    assert "<ins data-zz363" not in body
    assert "&#64;zz363" not in body
    assert "ghp_" not in body
    expected_markers = tuple(expected_markers)
    assert review_marker.find_finding_markers(body) == list(expected_markers)
    marker = review_marker.find_marker(body)
    if marker is not None:
        body = body.replace(
            review_marker.build_marker(marker["sha"], marker["findings_count"]), ""
        )
    for finding_marker in expected_markers:
        body = body.replace(
            review_marker.build_finding_marker(
                finding_marker["sha"], finding_marker["key"]
            ),
            "",
        )
    # The poster owns this approved patch; the invariant applies to untrusted prose.
    body = body.replace("```suggestion\nreplacement = 1\n```", "")
    _assert_outbound_string_invariant(body)


@dataclass(frozen=True, slots=True)
class SinkCase:
    platform: Platform
    route: Literal["anchored", "off_diff", "grouped", "rule-fallback"]


SINK_CASES = [
    pytest.param(SinkCase("github", "anchored"), id="github-anchored"),
    pytest.param(SinkCase("github", "off_diff"), id="github-off_diff"),
    pytest.param(SinkCase("github", "grouped"), id="github-grouped"),
    pytest.param(SinkCase("gitlab", "anchored"), id="gitlab-anchored"),
    pytest.param(SinkCase("gitlab", "off_diff"), id="gitlab-off_diff"),
    pytest.param(SinkCase("gitlab", "grouped"), id="gitlab-grouped"),
    pytest.param(SinkCase("github", "rule-fallback"), id="github-rule-fallback"),
]


@pytest.mark.parametrize("case", SINK_CASES)
def test_python_sink_containment(case: SinkCase) -> None:
    members = [_poisoned_outbound_sinks_finding(primary=True)]
    if case.route == "grouped":
        members.append(_poisoned_outbound_sinks_finding())
    for member in members:
        member["line"] = 2
        member["end_line"] = 2
    if case.route == "rule-fallback":
        members[0]["claude_md_rule"] = FAKE_FINDING_MARKER
        members[0].pop("suggested_fix_code", None)
    filepath = members[0]["file"]
    assert isinstance(filepath, str)
    payload, _ = _deliver(
        case.platform,
        [cast(Finding, member) for member in members],
        _poison("review_body"),
        lines={(filepath, 2): None} if case.route != "off_diff" else {},
        texts={(filepath, 2): "context"} if case.route != "off_diff" else {},
    )
    assert not isinstance(payload, list)
    if payload["platform"] == "github":
        summary = payload["payload"]["body"]
        inline_bodies = [comment["body"] for comment in payload["payload"]["comments"]]
    else:
        summary = payload["summary"]["body"]
        inline_bodies = [discussion["body"] for discussion in payload["discussions"]]
    for body in [summary, *inline_bodies]:
        _assert_prepared_poison(body)
        marker = review_marker.find_marker(body)
        assert marker is None or marker["sha"] == SHA
    marker = review_marker.find_marker(summary)
    assert marker is not None and marker["sha"] == SHA
    assert "\uff20zz363review_body" in summary
    if case.route == "off_diff":
        assert not inline_bodies
        assert "could not be anchored inline" in summary
        assert "\uff20zz363title" in summary
    else:
        assert len(inline_bodies) == 1
        assert "\uff20zz363title" in inline_bodies[0]
        if case.route == "grouped":
            assert "Corroborating finding" in inline_bodies[0]
        if case.route == "rule-fallback":
            assert "\uff20zz363spec_text" in inline_bodies[0]


def test_delivery_title_keys() -> None:
    member = delivery_finding(
        omit=("title", "end_line"),
        file="src/edited.py",
        line=61,
        body="Body one",
    )
    calls, lookup = _deliver(
        "gitlab",
        [cast(Finding, member)],
        live=True,
        prior=PriorDelivery(True, frozenset(), frozenset(), None),
        lines={("src/edited.py", 61): 50},
        texts={("src/edited.py", 61): "context"},
    )
    assert lookup == [ForgeCall("review_entries", ReviewTarget("o", "r", 7))]
    assert isinstance(calls, list) and len(calls) == 1
    assert calls[0].endpoint.endswith("/discussions")
    body = calls[0].payload["body"]
    assert isinstance(body, str)
    markers = review_marker.find_finding_markers(body)
    assert markers and markers[0]["key"] == "07961d7c0f9dd168"
    assert "**\U0001f7e0 [HIGH] Finding**\n\nBody one" in body


def _assert_prepared_body(
    body: str, expected_markers: Iterable[FindingMarkerWire] = ()
) -> None:
    assert "@zz363" not in body
    assert "<table" not in body
    assert "<ins data-zz363" not in body
    assert review_marker.find_finding_markers(body) == list(expected_markers)


def test_legacy_report_summary_is_guarded_on_the_real_cli_path(
    forge_factory: FakeForgeFactory,
) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        findings_path = Path(tmp) / "findings.json"
        report_path = Path(tmp) / "report.md"
        payload_path = Path(tmp) / "post-review-payload.json"
        findings_path.write_text(
            json.dumps(
                {
                    "owner": "o",
                    "repo": "r",
                    "pr_number": 7,
                    "platform": "github",
                    "sha": SHA,
                    "findings": [],
                }
            ),
            encoding="utf-8",
        )
        report_path.write_text(
            "## Summary\n\n"
            f"@zz363sentinel <table><tr><td> report {FAKE_FINDING_MARKER}\n"
            "/close\n>>>\n\n"
            "## Findings\n\n",
            encoding="utf-8",
        )
        original_report = report_path.read_bytes()
        forge_factory.configure(FakeForge(diffs=[("", "", 0)]))
        with (
            patch.object(
                sys,
                "argv",
                [
                    "post_review.py",
                    str(findings_path),
                    "--dry-run",
                    "--report",
                    str(report_path),
                ],
            ),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            post_review.CLI.invoke(sys.argv[1:])
        assert report_path.read_bytes() == original_report
        payload: post_review.GitHubDryRunPayload = json.loads(
            payload_path.read_text(encoding="utf-8")
        )
    review_body = payload["payload"]["body"]
    _assert_prepared_body(review_body)
    invariant_body = review_body.replace(review_marker.build_marker(SHA, 0), "")
    _assert_outbound_string_invariant(invariant_body)
    assert "\uff20zz363sentinel" in review_body
    assert "\\/close\n\\>>>" in review_body


def test_markup_rekeys_once() -> None:
    finding: Finding = {
        "file": "src/edited.py",
        "line": 2,
        "severity": "high",
        "title": "@leehopper <table>",
        "body": "Body one",
    }
    old_key, new_key = "13c2bc08cfac5226", "6970f2dcb5f585fd"
    for prior_keys, expected_posts in (({old_key}, 1), ({old_key, new_key}, 0)):
        calls, _ = _deliver(
            "gitlab",
            [finding],
            live=True,
            prior=PriorDelivery(True, frozenset(prior_keys), frozenset(), None),
        )
        assert isinstance(calls, list) and len(calls) == expected_posts
        if calls:
            assert calls[0].endpoint.endswith("/discussions")
            markers = review_marker.find_finding_markers(calls[0].payload["body"])
            assert markers and markers[0]["key"] == new_key


@pytest.mark.parametrize(
    "source",
    ["[\ncritical\n]: u"],
    ids=["multiline_label"],
)
def test_reference_multiline_fields_keep_severity_label(source: str) -> None:
    rendered = compose.render_comment_body(
        {
            "severity": "critical",
            "title": "Title",
            "body": source,
        }
    )
    assert "]\\: u" in rendered
    assert "[CRITICAL]" in rendered
    assert rendered.endswith(compose.BRAND_TRAILER)


def _poisoned_outbound_sinks_finding(*, primary: bool = False) -> dict[str, object]:
    finding: dict[str, object] = {key: _poison(key) for key in POISON_FIELDS}
    finding.update(
        file="app/@zz363file/<Slot>.tsx",
        line=2,
        end_line=2,
        severity="high",
        confidence=90,
        rule_source="repo_precedent",
        suggested_fix_code="replacement = 1",
        consolidation_key="poison-group",
        consolidation_primary=primary,
    )
    return finding


@pytest.mark.parametrize(
    ("platform", "end_line", "patch_text", "patch_header"),
    [
        pytest.param("github", 2, "replacement = 1", "```suggestion", id="single-line"),
        pytest.param(
            "gitlab",
            3,
            "replacement = 1\nreplacement = 2",
            "```suggestion:-0+1",
            id="gitlab-offsets",
        ),
    ],
)
def test_prose_active_fences_preserve_owned_patch(
    platform: Platform, end_line: int, patch_text: str, patch_header: str
) -> None:
    finding: Finding = {
        "file": "src/edited.py",
        "line": 2,
        "end_line": end_line,
        "severity": "high",
        "title": "before x [a]: //example.test/SENT](a b) after",
        "body": "term\n: ~~~suggestion\n  x = SENT\n  ~~~\n\ntail",
        "suggestion": "ordinary ~~~ text",
        "claude_md_rule": "x [^a]: note SENT](a b) after",
        "suggested_fix_code": patch_text,
    }
    payload, _ = _deliver(
        platform,
        [finding],
        lines={("src/edited.py", 2): None, ("src/edited.py", 3): None},
        texts={("src/edited.py", 2): "changed", ("src/edited.py", 3): "changed"},
    )

    def string_leaves(value: object) -> list[str]:
        if isinstance(value, str):
            return [value]
        if isinstance(value, dict):
            return [part for child in value.values() for part in string_leaves(child)]
        if isinstance(value, list):
            return [part for child in value for part in string_leaves(child)]
        return []

    rendered = "\n".join(string_leaves(payload))
    assert "before x [a]\uff1a //example.test/SENT](a b) after" in rendered
    assert ": \\~\\~\\~suggestion\n  x = SENT\n  \\~\\~\\~" in rendered
    assert "ordinary \\~\\~\\~ text" in rendered
    assert "x \uff3b^a]\uff1a note SENT](a b) after" in rendered
    assert f"{patch_header}\n{patch_text}\n```" in rendered
    assert compose.BRAND_TRAILER in rendered

    corroborator = {
        "severity": "low",
        "title": "corroborator [a]: //e/SENT](a b)",
        "body": "term\n~ ~~~mermaid\n  x = SENT\n  ~~~\n\ntail",
        "agent": "Corroborator",
    }
    grouped = compose.render_group_body(finding, [corroborator])
    assert "corroborator [a]\uff1a //e/SENT](a b)" in grouped
    assert "~ \\~\\~\\~mermaid" in grouped

    skipped = compose.build_skipped_section(
        [
            compose.SkippedEntry(
                "src/a.py",
                3,
                {"severity": "high", "title": "Skipped", "body": finding["body"]},
            )
        ]
    )
    assert ": \\~\\~\\~suggestion" in skipped


@pytest.mark.parametrize(
    "route, line, endpoint",
    [
        ("rejected", 3, "/discussions"),
        ("partial", 3, "/discussions"),
        ("partial", None, "/notes"),
    ],
    ids=["rejected-discussion", "missing-discussion", "missing-note"],
)
def test_gitlab_live_fallback(route: str, line: int | None, endpoint: str) -> None:
    primary = _poisoned_outbound_sinks_finding(primary=True)
    corroborator = _poisoned_outbound_sinks_finding()
    corroborator["line"] = line
    corroborator["end_line"] = line
    primary_key = compose.finding_key(
        primary["file"],
        primary["line"],
        outbound_text.prepare_line(primary["title"]),
        compose.key_material_body(primary),
    )
    corroborator_key = compose.finding_key(
        corroborator["file"],
        line,
        outbound_text.prepare_line(corroborator["title"]),
        compose.key_material_body(corroborator),
    )
    filepath = primary["file"]
    assert isinstance(filepath, str)
    calls, lookup = _deliver(
        "gitlab",
        [cast(Finding, primary), cast(Finding, corroborator)],
        live=True,
        reject_first=route == "rejected",
        prior=PriorDelivery(
            True,
            frozenset({primary_key}) if route == "partial" else frozenset(),
            frozenset(),
            None,
        ),
        lines={(filepath, 2): None, (filepath, 3): None},
        texts={(filepath, 2): "primary", (filepath, 3): "corroborator"},
    )
    assert lookup == [ForgeCall("review_entries", ReviewTarget("o", "r", 7))]
    assert isinstance(calls, list)
    assert len(calls) == (2 if route == "rejected" else 1)
    if route == "rejected":
        grouped_body = calls[0].payload["body"]
        assert isinstance(grouped_body, str)
        assert "Corroborating finding" in grouped_body
    request = calls[-1]
    assert request.endpoint.endswith(endpoint)
    assert ("position" in request.payload) == (line is not None)
    body = request.payload["body"]
    assert isinstance(body, str)
    assert "Corroborating finding" not in body
    _assert_prepared_poison(body, [{"sha": SHA, "key": corroborator_key}])

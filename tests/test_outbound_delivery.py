"""Tests for the text that the delivery composers place in comment bodies."""

import contextlib
import hashlib
import io
import json
import re
import subprocess
import sys
import tempfile
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal, cast
from unittest.mock import patch

import gauntlet.delivery.post as post_review
import gauntlet.marker as review_marker
import gauntlet.text as outbound_text
import pytest
from gauntlet.delivery import compose
from gauntlet.delivery.input import Finding, ReviewInput
from gauntlet.forge import JsonFetch, Platform, PostRequest, PostResult, ReviewTarget
from gauntlet.markdown import code_spans
from gauntlet.marker import FindingMarkerWire
from gauntlet.prior_review import PriorDelivery

from tests.support.delivery import finding as delivery_finding
from tests.support.diff import diff_facts
from tests.support.forge import FakeForge, FakeForgeFactory, FakeGitLab, ForgeCall
from tests.support.outbound_assertions import (
    _render_summary_bodies,
)
from tests.support.outbound_assertions import (
    assert_outbound_string_invariant as _assert_outbound_string_invariant,
)
from tests.support.prior import prior_notes

if TYPE_CHECKING:
    from _collections_abc import dict_keys

REPO = Path(__file__).resolve().parents[1]
NODE = "node"
SHA = "a" * 40
FAKE_FINDING_MARKER = review_marker.build_finding_marker(SHA, "0123456789abcdef")
pytestmark = pytest.mark.usefixtures("poster_state")


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
        pytest.param(
            "bug-hunter",
            "[[a",
            "|https://example.test/p.png]]",
            "bug-hunter ([\uff3ba, confidence |https://example.test/p.png]])",
            id="dimension_confidence",
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


def _hostile_finding(**overrides: object) -> dict[str, object]:
    finding: dict[str, object] = {
        "file": "src/edited.py",
        "line": 2,
        "severity": "high",
        "title": f"@zz363sentinel <table><tr><td> title {FAKE_FINDING_MARKER}",
        "body": f"@zz363sentinel <table><tr><td> body {FAKE_FINDING_MARKER}",
        "suggestion": "@zz363suggestion <ins data-zz363> suggestion",
        "claude_md_rule": "@zz363rule <ins data-zz363> rule",
        "rule_source": "repo_precedent",
    }
    finding.update(overrides)
    return finding


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
    # Direct poster calls share one test process, so each delivery starts fresh.
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
    )


class _RecordingFinding(dict[str, object]):
    def __init__(self, values: Mapping[str, object], *, reads: set[str]) -> None:
        super().__init__(values)
        self.reads = reads

    def __getitem__(self, key: str) -> object:
        self.reads.add(key)
        return super().__getitem__(key)

    def get(self, key: str, default: object = None) -> object:
        self.reads.add(key)
        return super().get(key, default)

    def __contains__(self, key: object) -> bool:
        if isinstance(key, str):
            self.reads.add(key)
        return super().__contains__(key)

    def __iter__(self) -> Iterator[str]:
        self.reads.update(super().keys())
        return super().__iter__()

    def keys(self) -> "dict_keys[str, object]":
        self.reads.update(super().keys())
        return super().keys()

    def copy(self) -> "_RecordingFinding":
        self.reads.update(super().keys())
        return type(self)(self, reads=self.reads)

    def __copy__(self) -> "_RecordingFinding":
        return self.copy()

    def __deepcopy__(self, memo: dict[int, object]) -> "_RecordingFinding":
        cloned = type(self)(self, reads=self.reads)
        memo[id(self)] = cloned
        return cloned


def _js_finding_property_union() -> set[str]:
    source = (
        "import {FINDING_PROP_TYPES,DIMENSIONS} from './workflows/src/registry.js';"
        "const names=[...new Set([...Object.keys(FINDING_PROP_TYPES),"
        "...DIMENSIONS.flatMap(d=>Object.keys(d.schemaExtra||{})),"
        "'body','line','end_line'])];"
        "process.stdout.write(JSON.stringify(names));"
    )
    result = subprocess.run(
        [NODE, "--input-type=module", "-e", source],
        cwd=REPO,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )
    if result.returncode:
        raise AssertionError(result.stderr)
    names: list[str] = json.loads(result.stdout)
    return set(names)


def _contains_code_span(text: str, needle: str) -> bool:
    found = False
    for line in text.splitlines():
        offset = 0
        while True:
            index = line.find(needle, offset)
            if index < 0:
                break
            found = True
            runs = list(re.finditer(r"`+", line))
            enclosed = False
            for opening in runs:
                if opening.end() > index:
                    break
                for closing in runs:
                    if closing.start() < index + len(needle):
                        continue
                    inner = line[opening.end() : closing.start()]
                    inner_runs = re.findall(r"`+", inner)
                    if len(opening.group()) == len(closing.group()) and len(
                        opening.group()
                    ) > max(map(len, inner_runs), default=0):
                        enclosed = True
                        break
                if enclosed:
                    break
            if not enclosed:
                return False
            offset = index + len(needle)
    return found


def test_every_location_sentinel_occurrence_must_be_quoted() -> None:
    assert _contains_code_span("`@zz363file` and ``@zz363file``", "@zz363file")
    assert not _contains_code_span("`@zz363file` and @zz363file", "@zz363file")


def _assert_prepared_poison(
    body: str,
    property_names: set[str],
    expected_markers: Iterable[FindingMarkerWire] = (),
) -> None:
    location_names = {"file", "line", "end_line", "line_start", "line_end"}
    patch_name = "suggested_fix_code"
    for key in property_names:
        for visible in (f"@zz363{key}", f"\uff20zz363{key}"):
            if key in location_names:
                if visible in body:
                    assert _contains_code_span(body, visible), key
            elif key == patch_name:
                if visible in body:
                    suggestion_blocks = re.findall(
                        r"(?ms)^`{3,}suggestion[^\n]*\n.*?^`{3,}\s*$", body
                    )
                    assert any(visible in block for block in suggestion_blocks)
            else:
                assert f"@zz363{key}" not in body, key
        if key not in location_names and key != patch_name:
            assert f"<ins data-zz363{key}" not in body, key
    expected_markers = tuple(expected_markers)
    assert "@zz363unknown_key" not in body
    assert "&#64;zz363" not in body
    assert "&;zz363" not in body
    assert review_marker.find_finding_markers(body) == list(expected_markers)
    without_live_markers = body
    summary_marker = review_marker.find_marker(body)
    if summary_marker is not None:
        without_live_markers = without_live_markers.replace(
            review_marker.build_marker(
                summary_marker["sha"], summary_marker["findings_count"]
            ),
            "",
        )
    for marker in expected_markers:
        without_live_markers = without_live_markers.replace(
            review_marker.build_finding_marker(marker["sha"], marker["key"]), ""
        )
    spans, _fences = code_spans(without_live_markers)
    locations = tuple(
        without_live_markers[start:end]
        for start, end in spans
        if any(
            f"zz363{name}" in without_live_markers[start:end] for name in location_names
        )
    )
    _assert_outbound_string_invariant(without_live_markers, literal_locations=locations)


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


@pytest.fixture(scope="module")
def poison_fields() -> set[str]:
    property_names = _js_finding_property_union() | {"unknown_key"}
    reads: set[str] = set()
    primary = _poisoned_outbound_sinks_finding(property_names, reads, primary=True)
    corroborator = _poisoned_outbound_sinks_finding(property_names, reads)
    compose.render_comment_body(primary)
    compose.render_group_body(primary, [corroborator])
    compose.build_skipped_section(
        [compose.SkippedEntry(primary.get("file"), primary.get("line"), primary)]
    )
    fallback = _poisoned_outbound_sinks_finding(property_names, reads)
    fallback["claude_md_rule"] = FAKE_FINDING_MARKER
    compose.render_comment_body(fallback)
    for case in (
        SinkCase(platform, route)
        for platform in ("github", "gitlab")
        for route in ("anchored", "off_diff", "grouped")
    ):
        members = [
            _poisoned_outbound_sinks_finding(property_names, reads, primary=True)
        ]
        if case.route == "grouped":
            members.append(_poisoned_outbound_sinks_finding(property_names, reads))
        for member in members:
            member["line"] = 2
            member["end_line"] = 2
        filepath = members[0]["file"]
        assert isinstance(filepath, str)
        _deliver(
            case.platform,
            [cast(Finding, member) for member in members],
            _poison("review_body"),
            lines={(filepath, 2): None} if case.route != "off_diff" else {},
            texts={(filepath, 2): "context"} if case.route != "off_diff" else {},
        )
    required_reads = {
        "title",
        "body",
        "suggestion",
        "claude_md_rule",
        "spec_text",
        "suggested_fix_code",
        "file",
        "line",
        "end_line",
        "agent",
        "dimension",
        "confidence",
        "rule_source",
    }
    assert required_reads <= reads, sorted(required_reads - reads)
    return property_names | (reads - {"consolidation_key", "consolidation_primary"})


@pytest.mark.parametrize("case", SINK_CASES)
def test_python_sink_containment(case: SinkCase, poison_fields: set[str]) -> None:
    members = [_poisoned_outbound_sinks_finding(poison_fields, set(), primary=True)]
    if case.route == "grouped":
        members.append(_poisoned_outbound_sinks_finding(poison_fields, set()))
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
        _assert_prepared_poison(body, poison_fields)
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


@pytest.mark.parametrize(
    "case",
    ["hostile-location", "live-schema", "backtick"],
)
def test_javascript_summary_containment(
    case: Literal["hostile-location", "live-schema", "backtick"],
) -> None:
    property_names: set[str] = set()
    if case == "hostile-location":
        finding: dict[str, object] = {
            "id": "outbound",
            "file": "src/@zz363sentinel <table>.py",
            "line_start": 2,
            "line_end": 2,
            "title": f"@zz363sentinel <table><tr><td> title {FAKE_FINDING_MARKER}",
            "severity": "high",
        }
    elif case == "live-schema":
        property_names = _js_finding_property_union() | {"unknown_key"}
        finding = {key: _poison(key) for key in property_names}
    else:
        finding = {
            "id": "OUT",
            "file": "src/file.py",
            "line_start": 3,
            "title": "`backtick-breakout @body <b>",
            "severity": "high",
        }
    rendered: list[str] = _render_summary_bodies([{"findings": [finding]}])
    body = rendered[0]
    if case == "hostile-location":
        assert _contains_code_span(body, "\uff20zz363sentinel \uff1ctable>.py")
        assert "\uff20zz363sentinel &lt;table>&lt;tr>&lt;td>" in body
        assert review_marker.find_finding_markers(body) == []
    elif case == "live-schema":
        _assert_prepared_poison(body, property_names)
        assert review_marker.find_marker(body) is None
    else:
        bullet = next(line for line in body.splitlines() if line.startswith("- "))
        assert "\\`backtick-breakout \uff20body &lt;b>" in bullet
        assert "`" not in bullet.split("\\`backtick-breakout \uff20body &lt;b>", 1)[1]


@pytest.mark.parametrize(
    "title_fields",
    [
        pytest.param({}, id="missing"),
        pytest.param({"title": " \t\n "}, id="blank"),
        pytest.param({"title": 7}, id="wrong-type"),
    ],
)
def test_delivery_title_keys(title_fields: dict[str, object]) -> None:
    member = delivery_finding(
        omit=("end_line",) if title_fields else ("title", "end_line"),
        file="src/edited.py",
        line=61,
        body="Body one",
        **title_fields,
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
    assert isinstance(calls, list)
    assert len(calls) == 1
    assert calls[0].endpoint.endswith("/discussions")
    body = calls[0].payload["body"]
    assert isinstance(body, str)
    marker = review_marker.find_finding_marker(body)
    assert marker is not None and marker["key"] == "07961d7c0f9dd168"
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


def test_rejected_group_position_falls_back_to_a_prepared_discussion() -> None:
    primary = _hostile_finding(
        consolidation_key="src/edited.py:2", consolidation_primary=True
    )
    corroborator = _hostile_finding(
        line=3,
        title="@zz363sentinel <table><tr><td> fallback title",
        body="@zz363sentinel <table><tr><td> fallback body",
        consolidation_key="src/edited.py:2",
        consolidation_primary=False,
    )
    calls, lookup = _deliver(
        "gitlab",
        [cast(Finding, primary), cast(Finding, corroborator)],
        prior=PriorDelivery(False, frozenset(), frozenset(), None),
        reject_first=True,
        live=True,
        lines={("src/edited.py", 2): None, ("src/edited.py", 3): None},
        texts={("src/edited.py", 2): "primary", ("src/edited.py", 3): "corroborator"},
    )
    assert isinstance(calls, list)
    discussions = [
        request.payload
        for request in calls
        if request.endpoint.endswith("/discussions")
    ]
    assert len(discussions) == 2
    group_body = discussions[0]["body"]
    assert isinstance(group_body, str)
    assert "Corroborating finding" in group_body
    fallback_body = discussions[1]["body"]
    assert isinstance(fallback_body, str)
    assert "Corroborating finding" not in fallback_body
    markers = review_marker.find_finding_markers(fallback_body)
    assert len(markers) == 1
    assert markers[0]["sha"] == SHA
    assert markers[0]["key"] != "0123456789abcdef"
    _assert_prepared_body(fallback_body, expected_markers=markers)
    assert lookup == [ForgeCall("review_entries", ReviewTarget("o", "r", 7))]


def test_changed_content_key_reposts_once_after_old_key() -> None:
    finding: Finding = {
        "file": "src/edited.py",
        "line": 2,
        "severity": "high",
        "title": "@leehopper <table>",
        "body": "Body one",
    }
    old_material = (
        "src/edited.py\0"
        "2\0"
        "@leehopper <table>\0"
        "**\U0001f7e0 [HIGH] @leehopper <table>**\n\nBody one"
    )
    new_material = (
        "src/edited.py\0"
        "2\0"
        "\uff20leehopper &lt;table>\0"
        "**\U0001f7e0 [HIGH] \uff20leehopper &lt;table>**\n\nBody one"
    )
    old_key = "13c2bc08cfac5226"
    new_key = "6970f2dcb5f585fd"
    assert hashlib.sha256(old_material.encode()).hexdigest()[:16] == old_key
    assert hashlib.sha256(new_material.encode()).hexdigest()[:16] == new_key
    first_calls, _ = _deliver(
        "gitlab",
        [finding],
        prior=PriorDelivery(True, frozenset({old_key}), frozenset(), None),
        live=True,
        lines={("src/edited.py", 2): None, ("src/edited.py", 3): None},
        texts={("src/edited.py", 2): "primary", ("src/edited.py", 3): "corroborator"},
    )
    assert isinstance(first_calls, list)
    discussions = [
        request.payload
        for request in first_calls
        if request.endpoint.endswith("/discussions")
    ]
    assert len(discussions) == 1
    marker = review_marker.find_finding_marker(discussions[0]["body"])
    assert marker is not None and marker["key"] == new_key
    second_calls, _ = _deliver(
        "gitlab",
        [finding],
        prior=PriorDelivery(True, frozenset({old_key, new_key}), frozenset(), None),
        live=True,
        lines={("src/edited.py", 2): None, ("src/edited.py", 3): None},
        texts={("src/edited.py", 2): "primary", ("src/edited.py", 3): "corroborator"},
    )
    assert not second_calls


@pytest.mark.parametrize(
    ("title", "body", "old_sections", "sections", "old_key", "new_key"),
    [
        pytest.param(
            "![a](u)",
            "Body one",
            "**\U0001f7e0 [HIGH] ![a](u)**\n\nBody one",
            "**\U0001f7e0 [HIGH] !\uff3ba](u)**\n\nBody one",
            "0899e2af08bef3b4",
            "6b732848339348e1",
            id="image_title_rekey",
        ),
        pytest.param(
            "Title",
            "[critical]: u",
            "**\U0001f7e0 [HIGH] Title**\n\n[critical]: u",
            "**\U0001f7e0 [HIGH] Title**\n\n[critical]\\: u",
            "ce5c0d6d62bad3f1",
            "3923f3f55e6f877b",
            id="reference_body_rekey",
        ),
    ],
)
def test_markup_rekeys_once(
    title: str, body: str, old_sections: str, sections: str, old_key: str, new_key: str
) -> None:
    finding: Finding = {
        "file": "src/edited.py",
        "line": 2,
        "severity": "high",
        "title": title,
        "body": body,
    }
    old_material = "src/edited.py\0" + "2\0" + title + "\0" + old_sections
    assert hashlib.sha256(old_material.encode("utf-8")).hexdigest()[:16] == old_key
    assert compose.key_material_body(finding) == sections
    assert (
        compose.finding_key(
            finding["file"],
            finding["line"],
            outbound_text.prepare_line(title),
            sections,
        )
        == new_key
    )
    calls, _lookup = _deliver(
        "gitlab",
        [finding],
        live=True,
        prior=PriorDelivery(True, frozenset({old_key}), frozenset(), None),
    )
    assert isinstance(calls, list)
    discussions = [
        call.payload for call in calls if call.endpoint.endswith("/discussions")
    ]
    assert len(discussions) == 1
    marker = review_marker.find_finding_marker(discussions[0]["body"])
    assert marker is not None and marker["key"] == new_key
    calls, _lookup = _deliver(
        "gitlab",
        [finding],
        live=True,
        prior=PriorDelivery(True, frozenset({old_key, new_key}), frozenset(), None),
    )
    assert not calls


@pytest.mark.parametrize(
    "field", ("title", "body", "suggestion", "claude_md_rule", "spec_text")
)
@pytest.mark.parametrize(
    "source,expected",
    [
        pytest.param("![a](u)", "!\uff3ba](u)", id="image"),
        pytest.param("[critical]: u", "[critical]\\: u", id="reference"),
    ],
)
def test_markup_fields_preserve_patch_and_footer(
    field: str, source: str, expected: str
) -> None:
    patch_text = "![a](u)\n[critical]: u"
    finding = {
        "severity": "critical",
        "title": "Title",
        "body": "Body",
        field: source,
        "suggested_fix_code": patch_text,
    }
    rendered = compose.render_comment_body(finding)
    assert expected in rendered
    assert "```suggestion\n" + patch_text + "\n```" in rendered
    assert rendered.endswith(compose.BRAND_TRAILER)
    assert "[CRITICAL]" in rendered


@pytest.mark.parametrize(
    "source",
    ["[\ncritical\n]: u", "[critical\n]: u", "> [critical\n> ]: u"],
    ids=["multiline_label", "multiline_closer", "multiline_quote"],
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


@pytest.mark.parametrize(
    "line, endpoint",
    [(3, "/discussions"), (None, "/notes")],
    ids=["anchored-discussion", "unanchored-note"],
)
def test_partial_prior_delivery_posts_only_the_missing_corroborator(
    line: int | None, endpoint: str
) -> None:
    primary = _hostile_finding(
        consolidation_key="src/edited.py:2", consolidation_primary=True
    )
    title = outbound_text.prepare_line(primary["title"])
    prior_key = compose.finding_key(
        primary["file"],
        primary["line"],
        title,
        compose.key_material_body(primary),
    )
    corroborator = _hostile_finding(
        line=line,
        consolidation_key="src/edited.py:2",
        consolidation_primary=False,
    )
    calls, lookup = _deliver(
        "gitlab",
        [cast(Finding, primary), cast(Finding, corroborator)],
        prior=PriorDelivery(True, frozenset({prior_key}), frozenset(), None),
        live=True,
        lines={("src/edited.py", 2): None, ("src/edited.py", 3): None},
        texts={("src/edited.py", 2): "primary", ("src/edited.py", 3): "corroborator"},
    )
    assert isinstance(calls, list)
    assert len(calls) == 1
    request = calls[0]
    payload = request.payload
    assert request.endpoint.endswith(endpoint)
    body = payload["body"]
    assert isinstance(body, str)
    markers = review_marker.find_finding_markers(body)
    assert len(markers) == 1
    assert markers[0]["key"] != prior_key
    _assert_prepared_body(body, expected_markers=markers)
    assert lookup == [ForgeCall("review_entries", ReviewTarget("o", "r", 7))]


def _poisoned_outbound_sinks_finding(
    property_names: set[str], reads: set[str], *, primary: bool = False
) -> _RecordingFinding:
    finding = _RecordingFinding(
        {key: _poison(key) for key in property_names}, reads=reads
    )
    finding["consolidation_key"] = "poison-group"
    finding["consolidation_primary"] = primary
    return finding


def test_poisoned_gitlab_live_fallback_discussion_and_note() -> None:
    property_names = set(_js_finding_property_union()) | {"unknown_key"}
    primary = _poisoned_outbound_sinks_finding(property_names, set(), primary=True)
    corroborator = _poisoned_outbound_sinks_finding(property_names, set())
    primary["line"] = 2
    primary["end_line"] = 2
    corroborator["line"] = 2
    corroborator["end_line"] = 2
    filepath = primary["file"]
    assert isinstance(filepath, str)
    rejected_calls = _deliver(
        "gitlab",
        [cast(Finding, primary), cast(Finding, corroborator)],
        prior=PriorDelivery(False, frozenset(), frozenset(), None),
        reject_first=True,
        live=True,
        review_body=_poison("review_body"),
        lines={(filepath, 2): None},
        texts={(filepath, 2): "context"},
    )[0]
    assert isinstance(rejected_calls, list)
    discussions = [
        request.payload
        for request in rejected_calls
        if request.endpoint.endswith("/discussions")
    ]
    assert len(discussions) == 2
    fallback_body = discussions[-1]["body"]
    assert isinstance(fallback_body, str)
    fallback_markers = review_marker.find_finding_markers(fallback_body)
    assert len(fallback_markers) == 1
    _assert_prepared_poison(
        fallback_body, property_names, expected_markers=fallback_markers
    )

    primary = _poisoned_outbound_sinks_finding(property_names, set(), primary=True)
    corroborator = _poisoned_outbound_sinks_finding(property_names, set())
    primary["line"] = 2
    primary["end_line"] = 2
    corroborator["line"] = 2
    corroborator["end_line"] = 2
    corroborator["line"] = None
    filepath = primary["file"]
    assert isinstance(filepath, str)
    with patch(
        "gauntlet.delivery.compose.finding_key",
        side_effect=lambda _file, member_line, _title, _body: (
            "1" * 16 if member_line is not None else "2" * 16
        ),
    ):
        note_calls = _deliver(
            "gitlab",
            [cast(Finding, primary), cast(Finding, corroborator)],
            prior=PriorDelivery(True, frozenset({"1" * 16}), frozenset(), None),
            live=True,
            review_body=_poison("review_body"),
            lines={(filepath, 2): None},
            texts={(filepath, 2): "context"},
        )[0]
    assert isinstance(note_calls, list)
    assert len(note_calls) == 1
    request = note_calls[0]
    payload = request.payload
    assert request.endpoint.endswith("/notes")
    assert "position" not in payload
    note_body = payload["body"]
    assert isinstance(note_body, str)
    note_markers = review_marker.find_finding_markers(note_body)
    assert note_markers == [{"sha": SHA, "key": "2" * 16}]
    _assert_prepared_poison(note_body, property_names, expected_markers=note_markers)


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

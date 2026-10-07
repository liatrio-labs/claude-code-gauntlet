"""Tests for the text that the delivery composers place in comment bodies."""

import contextlib
import hashlib
import io
import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import gauntlet.delivery.post as post_review
import gauntlet.marker as review_marker
import gauntlet.text as outbound_text
import pytest
from gauntlet.delivery import compose
from gauntlet.forge import JsonFetch, Platform, PostRequest, PostResult, ReviewTarget
from gauntlet.markdown import code_spans
from gauntlet.prior_review import PriorDelivery

from tests.support.diff import diff_facts
from tests.support.forge import FakeForge, FakeGitLab, ForgeCall
from tests.support.prior import prior_notes
from tests.test_outbound_contract import (
    assert_outbound_string_invariant as _assert_outbound_string_invariant,
)

REPO = Path(__file__).resolve().parents[1]
NODE = "node"
OUTBOUND_CASES = json.loads(
    (REPO / "tests/fixtures/outbound_comment_cases.json").read_text(encoding="utf-8")
)["cases"]
SHA = "a" * 40
FAKE_FINDING_MARKER = review_marker.build_finding_marker(SHA, "0123456789abcdef")
pytestmark = pytest.mark.usefixtures("poster_state")


def test_direct_sessions_interleave_live_and_dry_delivery(capsys):
    live = post_review.DeliverySession(dry_run=False)
    dry = post_review.DeliverySession(dry_run=True)
    facts = diff_facts({("x.py", 2): None}, line_texts={("x.py", 2): "old"})
    wrapper = {"owner": "o", "repo": "r", "pr_number": 7, "sha": SHA}
    finding = {"file": "x.py", "line": 2, "title": "T", "body": "b"}

    class InterleavingForge(FakeGitLab):
        def submit(self, request):
            if request.endpoint.endswith("/notes"):
                post_review.post_github(
                    {**wrapper, "findings": [{**finding, "suggested_fix_code": "new"}]},
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
            {
                **wrapper,
                "findings": [{**finding, "end_line": 2, "suggested_fix_code": "new"}],
            },
            facts,
            forge=forge,
            session=live,
        )
        == 0
    )
    assert dry.dry_run_payload("github")["payload"]["comments"] == [
        {
            "path": "x.py",
            "line": 2,
            "side": "RIGHT",
            "body": "**\U0001f4a1 [LOW] T**\n\nb\n\n\u2694\ufe0f *Code Gauntlet*",
        }
    ]
    assert dry.dry_run_payload("github")["skipped"] == [
        "suggested-fix downgraded: x.py:2 (missing_end_line)"
    ]
    assert live.dry_run_payload("gitlab") == {
        "platform": "gitlab",
        "summary": {},
        "discussions": [],
        "skipped": [],
    }
    discussions = [
        call.request.payload
        for call in forge.calls
        if call.request is not None and call.request.endpoint.endswith("/discussions")
    ]
    assert len(discussions) == 1
    assert "```suggestion\nnew\n```" in discussions[0]["body"]
    out, err = capsys.readouterr()
    assert err == "WARNING: suggested-fix downgraded: x.py:2 (missing_end_line)\n"
    assert out == (
        "Review captured (dry-run).\n  1 inline comment(s) captured.\n"
        "  0 suggested fix(es) passed the apply-check.\n  1 suggested fix(es) downgraded to prose.\n"
        "MR summary note posted.\n  1 inline discussion(s) posted.\n"
        "  1 suggested fix(es) passed the apply-check.\n  0 suggested fix(es) downgraded to prose.\n"
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
            "[[a",
            "correctness",
            "|https://example.test/p.png]]",
            "[\uff3ba (correctness, confidence |https://example.test/p.png]])",
            id="agent_confidence",
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


def _hostile_finding(**overrides):
    finding = {
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


def _assert_no_hostile_prose(test, body, expected_markers=()):
    test.assertNotIn("@zz363", body)
    test.assertNotIn("<table", body)
    test.assertNotIn("<ins data-zz363", body)
    test.assertEqual(review_marker.find_finding_markers(body), list(expected_markers))


def _deliver(
    platform: Platform,
    findings: list[dict[str, object]],
    review_body: str = "",
    *,
    live: bool = False,
    prior: PriorDelivery | None = None,
    reject_first: bool = False,
    lines: dict[tuple[str, int], int | None] | None = None,
    texts: dict[tuple[str, int], str] | None = None,
    check_position: bool = True,
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
    data = {
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
        contextlib.ExitStack() as stack,
        contextlib.redirect_stdout(io.StringIO()),
        contextlib.redirect_stderr(io.StringIO()),
    ):
        if not check_position:
            stack.enter_context(
                patch.object(post_review, "validate_position", return_value=[])
            )
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


def _poison(key):
    marker_key = hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]
    marker = review_marker.build_finding_marker("b" * 40, marker_key)
    return (
        f"@zz363{key} <ins data-zz363{key}> {marker} ```` &#38;#64;zz363{key}"
        f"\n/zz377{key}\n>>>\n```\n/zz377{key}\n```"
        "\n![a](u)"
        "\n[critical]: u"
    )


class _RecordingFinding(dict):
    def __init__(self, *args, reads=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.reads = reads if reads is not None else set()

    # The read log is test bookkeeping; a recorded finding compares as its plain dict.
    __eq__ = dict.__eq__
    __hash__ = None

    def __getitem__(self, key):
        self.reads.add(key)
        return super().__getitem__(key)

    def get(self, key, default=None):
        self.reads.add(key)
        return super().get(key, default)

    def __contains__(self, key):
        self.reads.add(key)
        return super().__contains__(key)

    def __iter__(self):
        self.reads.update(super().keys())
        return super().__iter__()

    def keys(self):
        self.reads.update(super().keys())
        return super().keys()

    def copy(self):
        self.reads.update(super().keys())
        return type(self)(self, reads=self.reads)

    def __copy__(self):
        return self.copy()

    def __deepcopy__(self, memo):
        cloned = type(self)(self, reads=self.reads)
        memo[id(self)] = cloned
        return cloned


def _js_finding_property_union():
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
    return json.loads(result.stdout)


def _contains_code_span(text, needle):
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


def test_every_location_sentinel_occurrence_must_be_quoted():
    assert _contains_code_span("`@zz363file` and ``@zz363file``", "@zz363file")
    assert not _contains_code_span("`@zz363file` and @zz363file", "@zz363file")


def _assert_poison_containment(test, body, property_names, expected_markers=()):
    location_names = {"file", "line", "end_line", "line_start", "line_end"}
    patch_name = "suggested_fix_code"
    for key in property_names:
        for visible in (f"@zz363{key}", f"\uff20zz363{key}"):
            if key in location_names:
                if visible in body:
                    test.assertTrue(_contains_code_span(body, visible), key)
            elif key == patch_name:
                if visible in body:
                    suggestion_blocks = re.findall(
                        r"(?ms)^`{3,}suggestion[^\n]*\n.*?^`{3,}\s*$", body
                    )
                    test.assertTrue(
                        any(visible in block for block in suggestion_blocks)
                    )
            else:
                test.assertNotIn(f"@zz363{key}", body, key)
        if key not in location_names and key != patch_name:
            test.assertNotIn(f"<ins data-zz363{key}", body, key)
    test.assertNotIn("@zz363unknown_key", body)
    test.assertNotIn("&#64;zz363", body)
    test.assertEqual(review_marker.find_finding_markers(body), list(expected_markers))
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


@pytest.mark.usefixtures("forge_factory", "poster_state")
class TestOutboundComposerContracts(unittest.TestCase):
    def test_javascript_summary_index_contains_hostile_title_and_location(self):
        finding = {
            "id": "outbound",
            "file": "src/@zz363sentinel <table>.py",
            "line_start": 2,
            "line_end": 2,
            "title": f"@zz363sentinel <table><tr><td> title {FAKE_FINDING_MARKER}",
            "severity": "high",
        }
        script = (
            "import {renderSummaryBody} from './workflows/src/renderReport.js';"
            f"process.stdout.write(JSON.stringify(renderSummaryBody({json.dumps({'findings': [finding]})})));"
        )
        result = subprocess.run(
            [NODE, "--input-type=module", "-e", script],
            cwd=REPO,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        rendered = json.loads(result.stdout)
        self.assertTrue(
            _contains_code_span(rendered, "\uff20zz363sentinel \uff1ctable>.py")
        )
        self.assertIn("\uff20zz363sentinel &lt;table>&lt;tr>&lt;td>", rendered)
        self.assertEqual(review_marker.find_finding_markers(rendered), [])

    def test_github_payload_guards_review_inline_and_skipped_fields(self):
        anchored = _hostile_finding()
        skipped = _hostile_finding(line=99)
        payload = _deliver(
            "github",
            [anchored, skipped],
            f"@zz363sentinel <table><tr><td> review {FAKE_FINDING_MARKER}",
        )[0]
        review_body = payload["payload"]["body"]
        inline_body = payload["payload"]["comments"][0]["body"]
        _assert_no_hostile_prose(self, review_body)
        _assert_no_hostile_prose(self, inline_body)
        self.assertIn("\uff20zz363sentinel", review_body)
        self.assertIn("\uff20zz363sentinel", inline_body)
        self.assertIn("could not be anchored inline", review_body)
        self.assertEqual(review_marker.find_marker(review_body)["sha"], SHA)

    def test_gitlab_payload_guards_summary_discussion_and_skipped_fields(self):
        primary = _hostile_finding(
            consolidation_key="src/edited.py:2", consolidation_primary=True
        )
        corroborator = _hostile_finding(
            title="@zz363sentinel <table><tr><td> corroborator title",
            body="@zz363sentinel <table><tr><td> corroborator body",
            consolidation_key="src/edited.py:2",
            consolidation_primary=False,
        )
        skipped = _hostile_finding(line=99)
        payload = _deliver(
            "gitlab",
            [primary, corroborator, skipped],
            f"@zz363sentinel <table><tr><td> summary {FAKE_FINDING_MARKER}",
        )[0]
        summary = payload["summary"]["body"]
        discussion = payload["discussions"][0]["body"]
        _assert_no_hostile_prose(self, summary)
        _assert_no_hostile_prose(self, discussion)
        self.assertIn("\uff20zz363sentinel", summary)
        self.assertIn("Corroborating finding", discussion)
        self.assertIn("\uff20zz363sentinel", discussion)
        self.assertIn("could not be anchored inline", summary)
        self.assertEqual(review_marker.find_marker(summary)["sha"], SHA)


class TestDeliveryTitleKeys(unittest.TestCase):
    @staticmethod
    def _live_key(finding):
        fake = FakeGitLab()
        filepath = finding["file"]
        line = finding["line"]

        with (
            patch(
                "gauntlet.delivery.post.fetch_gitlab_shas",
                return_value=("base", "head", "start"),
            ),
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            post_review.post_gitlab(
                {
                    "owner": "o",
                    "repo": "r",
                    "pr_number": 7,
                    "sha": SHA,
                    "findings": [finding],
                },
                diff_facts(
                    {(filepath, line): 50},
                    line_texts={(filepath, line): "context"},
                    new_files=set(),
                    old_paths={filepath: filepath},
                ),
                forge=fake,
                session=post_review.DeliverySession(dry_run=False),
            )
        discussion = next(
            call.request.payload
            for call in fake.calls
            if call.method == "submit" and "position" in call.request.payload
        )
        return review_marker.find_finding_marker(discussion["body"])["key"]

    def test_absent_title_key_is_pinned(self):
        finding = {
            "file": "src/edited.py",
            "line": 61,
            "severity": "high",
            "body": "Body one",
        }
        self.assertEqual(self._live_key(finding), "07961d7c0f9dd168")

    def test_none_title_key_is_pinned(self):
        finding = {
            "file": "src/edited.py",
            "line": 61,
            "severity": "high",
            "title": None,
            "body": "Body one",
        }
        self.assertEqual(self._live_key(finding), "07961d7c0f9dd168")

    def test_empty_title_key_is_pinned(self):
        finding = {
            "file": "src/edited.py",
            "line": 61,
            "severity": "high",
            "title": "",
            "body": "Body one",
        }
        self.assertEqual(self._live_key(finding), "07961d7c0f9dd168")

    def test_whitespace_title_key_is_pinned(self):
        finding = {
            "file": "src/edited.py",
            "line": 61,
            "severity": "high",
            "title": " \t\n ",
            "body": "Body one",
        }
        self.assertEqual(self._live_key(finding), "07961d7c0f9dd168")


class TestPoisonedOutboundSinks(unittest.TestCase):
    @staticmethod
    def _capture(platform, findings, *, anchored):
        first = findings[0]
        filepath, line = first.get("file"), first.get("line")
        return _deliver(
            platform,
            findings,
            _poison("review_body"),
            lines={(filepath, line): None} if anchored else {},
            texts={(filepath, line): "context"} if anchored else {},
            check_position=False,
        )[0]

    def test_poisoned_python_fields_never_reach_any_comment_sink(self):
        property_names = set(_js_finding_property_union()) | {
            "body",
            "line",
            "end_line",
            "unknown_key",
        }
        reads = set()
        discovery_primary = _poisoned_outbound_sinks_finding(
            property_names, reads, primary=True
        )
        discovery_corroborator = _poisoned_outbound_sinks_finding(property_names, reads)
        compose.render_comment_body(discovery_primary)
        compose.render_group_body(discovery_primary, [discovery_corroborator])
        compose.build_skipped_section(
            [
                compose.SkippedEntry(
                    discovery_primary.get("file"),
                    discovery_primary.get("line"),
                    discovery_primary,
                )
            ]
        )
        discovery_fallback = _poisoned_outbound_sinks_finding(property_names, reads)
        discovery_fallback["claude_md_rule"] = FAKE_FINDING_MARKER
        compose.render_comment_body(discovery_fallback)
        for platform in ("github", "gitlab"):
            for route in ("anchored", "off_diff", "grouped"):
                members = [
                    _poisoned_outbound_sinks_finding(
                        property_names, reads, primary=True
                    )
                ]
                if route == "grouped":
                    members.append(
                        _poisoned_outbound_sinks_finding(property_names, reads)
                    )
                self._capture(platform, members, anchored=route != "off_diff")
        property_names.update(reads - {"consolidation_key", "consolidation_primary"})
        primary = _poisoned_outbound_sinks_finding(property_names, reads, primary=True)
        corroborator = _poisoned_outbound_sinks_finding(property_names, reads)

        direct_primary = {
            key: value for key, value in primary.items() if key != "suggested_fix_code"
        }
        direct_corroborator = {
            key: value
            for key, value in corroborator.items()
            if key != "suggested_fix_code"
        }
        direct_bodies = [
            compose.render_comment_body(direct_primary),
            compose.render_group_body(direct_primary, [direct_corroborator]),
            compose.build_skipped_section(
                [
                    compose.SkippedEntry(
                        primary.get("file"), primary.get("line"), direct_primary
                    )
                ]
            ),
        ]
        fallback = _poisoned_outbound_sinks_finding(property_names, reads)
        fallback["claude_md_rule"] = FAKE_FINDING_MARKER
        fallback.pop("suggested_fix_code", None)
        direct_bodies.append(compose.render_comment_body(fallback))

        for platform in ("github", "gitlab"):
            for route in ("anchored", "off_diff", "grouped"):
                routed_primary = _poisoned_outbound_sinks_finding(
                    property_names, reads, primary=True
                )
                members = [routed_primary]
                if route == "grouped":
                    members.append(
                        _poisoned_outbound_sinks_finding(property_names, reads)
                    )
                payload = self._capture(platform, members, anchored=route != "off_diff")
                if platform == "github":
                    bodies = [payload["payload"]["body"]]
                    bodies.extend(c["body"] for c in payload["payload"]["comments"])
                else:
                    bodies = [payload["summary"]["body"]]
                    bodies.extend(d["body"] for d in payload["discussions"])
                for body in bodies:
                    _assert_poison_containment(self, body, property_names)
                    self.assertEqual(review_marker.find_finding_markers(body), [])
                    marker = review_marker.find_marker(body)
                    self.assertTrue(marker is None or marker["sha"] == SHA)

        for body in direct_bodies:
            _assert_poison_containment(self, body, property_names)
            self.assertEqual(review_marker.find_finding_markers(body), [])
            self.assertNotIn("@zz363review_body", body)
            self.assertNotIn("<ins data-zz363review_body", body)

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
        self.assertTrue(required_reads <= reads, sorted(required_reads - reads))

    def test_recording_discovery_keeps_the_required_field_floor(self):
        property_names = set(_js_finding_property_union()) | {"unknown_key"}
        reads = set()
        primary = _poisoned_outbound_sinks_finding(property_names, reads, primary=True)
        corroborator = _poisoned_outbound_sinks_finding(property_names, reads)
        self._capture("github", [primary], anchored=True)
        self._capture("gitlab", [primary, corroborator], anchored=True)
        fallback = _poisoned_outbound_sinks_finding(property_names, reads)
        fallback["claude_md_rule"] = FAKE_FINDING_MARKER
        compose.render_comment_body(fallback)
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
        self.assertTrue(required_reads <= reads, sorted(required_reads - reads))

    def test_poisoned_javascript_summary_uses_the_live_schema_property_union(self):
        property_names = set(_js_finding_property_union()) | {"unknown_key"}
        finding = {key: _poison(key) for key in property_names}
        source = (
            "import {renderSummaryBody} from './workflows/src/renderReport.js';"
            f"const finding=JSON.parse({json.dumps(json.dumps(finding))});"
            "process.stdout.write(JSON.stringify(renderSummaryBody({findings:[finding]})));"
        )
        result = subprocess.run(
            [NODE, "--input-type=module", "-e", source],
            cwd=REPO,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        body = json.loads(result.stdout)
        _assert_poison_containment(self, body, property_names)
        self.assertEqual(review_marker.find_finding_markers(body), [])
        self.assertIsNone(review_marker.find_marker(body))


@pytest.mark.parametrize(
    "title", [None, 7, "", " \t\n "], ids=["null", "nonstring", "empty", "whitespace"]
)
def test_title_key_helper_reads_prior_snapshot(title, monkeypatch):
    finding: dict[str, object] = {
        "file": "src/edited.py",
        "line": 61,
        "severity": "high",
        "title": title,
        "body": "Body one",
    }
    fake = FakeGitLab(
        entries=[prior_notes(PriorDelivery(True, frozenset(), frozenset(), None), SHA)]
    )
    monkeypatch.setattr(sys.modules[__name__], "FakeGitLab", lambda: fake)
    TestDeliveryTitleKeys._live_key(finding)
    assert [call for call in fake.calls if call.method == "review_entries"] == [
        ForgeCall("review_entries", ReviewTarget("o", "r", 7))
    ]
    writes = [call.request for call in fake.calls if call.method == "submit"]
    assert len(writes) == 1
    assert writes[0].endpoint.endswith("/discussions")


def _assert_prepared_poison(body, property_names, expected_markers=()):
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
    assert "@zz363unknown_key" not in body
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
    _assert_outbound_string_invariant(without_live_markers)


def _assert_prepared_body(body, expected_markers=()):
    assert "@zz363" not in body
    assert "<table" not in body
    assert "<ins data-zz363" not in body
    assert review_marker.find_finding_markers(body) == list(expected_markers)


def test_outbound_composer_contracts__legacy_report_summary_is_guarded_on_the_real_cli_path(
    tmp_path, forge_factory
):
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
        payload = json.loads(payload_path.read_text(encoding="utf-8"))
    review_body = payload["payload"]["body"]
    _assert_prepared_body(review_body)
    invariant_body = review_body.replace(review_marker.build_marker(SHA, 0), "")
    _assert_outbound_string_invariant(invariant_body)
    assert "\uff20zz363sentinel" in review_body
    assert "\\/close\n\\>>>" in review_body


def test_gitlab_live_fallback_contracts__rejected_group_position_falls_back_to_a_prepared_discussion(
    tmp_path, forge_factory
):
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
        [primary, corroborator],
        prior=PriorDelivery(False, frozenset(), frozenset(), None),
        reject_first=True,
        live=True,
        lines={("src/edited.py", 2): None, ("src/edited.py", 3): None},
        texts={("src/edited.py", 2): "primary", ("src/edited.py", 3): "corroborator"},
    )
    discussions = [
        request.payload
        for request in calls
        if request.endpoint.endswith("/discussions")
    ]
    assert len(discussions) == 2
    assert "Corroborating finding" in discussions[0]["body"]
    fallback_body = discussions[1]["body"]
    assert "Corroborating finding" not in fallback_body
    markers = review_marker.find_finding_markers(fallback_body)
    assert len(markers) == 1
    assert markers[0]["sha"] == SHA
    assert markers[0]["key"] != "0123456789abcdef"
    _assert_prepared_body(fallback_body, expected_markers=markers)
    assert lookup == [ForgeCall("review_entries", ReviewTarget("o", "r", 7))]


def test_gitlab_live_fallback_contracts__changed_content_key_reposts_once_after_old_key(
    tmp_path, forge_factory
):
    finding = {
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
        "**🟠 [HIGH] @leehopper <table>**\n\nBody one"
    )
    new_material = (
        "src/edited.py\0"
        "2\0"
        "\uff20leehopper &lt;table>\0"
        "**🟠 [HIGH] \uff20leehopper &lt;table>**\n\nBody one"
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
    discussions = [
        request.payload
        for request in first_calls
        if request.endpoint.endswith("/discussions")
    ]
    assert len(discussions) == 1
    assert review_marker.find_finding_marker(discussions[0]["body"])["key"] == new_key
    second_calls, _ = _deliver(
        "gitlab",
        [finding],
        prior=PriorDelivery(True, frozenset({old_key, new_key}), frozenset(), None),
        live=True,
        lines={("src/edited.py", 2): None, ("src/edited.py", 3): None},
        texts={("src/edited.py", 2): "primary", ("src/edited.py", 3): "corroborator"},
    )
    assert not (second_calls)


@pytest.mark.usefixtures("forge_factory")
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
        pytest.param(
            "Title",
            "Body before x [a]: //example.test/SENT](a b) after",
            "**\U0001f7e0 [HIGH] Title**\n\nBody before x [a]: //example.test/SENT](a b) after",
            "**\U0001f7e0 [HIGH] Title**\n\nBody before x [a]\uff1a //example.test/SENT](a b) after",
            "abb8231504bb53a2",
            "ec309d4c822c0bd4",
            id="second_parse_body_rekey",
        ),
        pytest.param(
            "Title",
            "body <!--&#38;#45;&#38;#45;&#38;#62;z-->tail",
            "**\U0001f7e0 [HIGH] Title**\n\nbody tail",
            "**\U0001f7e0 [HIGH] Title**\n\nbody z-->tail",
            "de054858eaa0eaa1",
            "a467e2f5c3fed0ce",
            id="normalizer_n1_rekey",
        ),
        pytest.param(
            "Title",
            "body <!--x--\u200b>y-->z",
            "**\U0001f7e0 [HIGH] Title**\n\nbody z",
            "**\U0001f7e0 [HIGH] Title**\n\nbody y-->z",
            "2948992bd57f5d38",
            "5c01326a67cf1f80",
            id="normalizer_n2_rekey",
        ),
        pytest.param(
            "Title",
            "body <!-<!--x-->- y --<!--z-->> w",
            "**\U0001f7e0 [HIGH] Title**\n\nbody  w",
            "**\U0001f7e0 [HIGH] Title**\n\nbody > w",
            "be38b8d6f2d56312",
            "28b8fc9a36fb591a",
            id="normalizer_n3_rekey",
        ),
    ],
)
def test_markup_rekeys_once(
    title: str, body: str, old_sections: str, sections: str, old_key: str, new_key: str
) -> None:
    finding: dict[str, object] = {
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
def test_image_fields_preserve_patch_and_footer(field: str) -> None:
    patch_text = "![a](u)\n[critical]: u"
    finding = {
        "severity": "critical",
        "title": "Title",
        "body": "Body",
        field: "![a](u)",
        "suggested_fix_code": patch_text,
    }
    rendered = compose.render_comment_body(finding)
    assert "!\uff3ba](u)" in rendered
    assert "```suggestion\n" + patch_text + "\n```" in rendered
    assert rendered.endswith(compose.BRAND_TRAILER)
    assert "[CRITICAL]" in rendered


@pytest.mark.parametrize(
    "field", ("title", "body", "suggestion", "claude_md_rule", "spec_text")
)
def test_reference_fields_preserve_patch_and_footer(field: str) -> None:
    patch_text = "![a](u)\n[critical]: u"
    finding = {
        "severity": "critical",
        "title": "Title",
        "body": "Body",
        field: "[critical]: u",
        "suggested_fix_code": patch_text,
    }
    rendered = compose.render_comment_body(finding)
    assert "[critical]\\: u" in rendered
    assert "```suggestion\n" + patch_text + "\n```" in rendered
    assert rendered.endswith(compose.BRAND_TRAILER)
    assert "[CRITICAL]" in rendered


@pytest.mark.parametrize("field", ("body", "suggestion", "claude_md_rule", "spec_text"))
@pytest.mark.parametrize(
    "source",
    ["[\ncritical\n]: u", "[critical\n]: u", "> [critical\n> ]: u"],
    ids=["multiline_label", "multiline_closer", "multiline_quote"],
)
def test_reference_multiline_fields_keep_severity_label(
    field: str, source: str
) -> None:
    rendered = compose.render_comment_body(
        {
            "severity": "critical",
            "title": "Title",
            "body": "Body",
            field: source,
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
def test_gitlab_live_fallback_contracts__partial_prior_delivery_posts_only_the_missing_corroborator(
    tmp_path, forge_factory, line, endpoint
):
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
        [primary, corroborator],
        prior=PriorDelivery(True, frozenset({prior_key}), frozenset(), None),
        live=True,
        lines={("src/edited.py", 2): None, ("src/edited.py", 3): None},
        texts={("src/edited.py", 2): "primary", ("src/edited.py", 3): "corroborator"},
    )
    assert len(calls) == 1
    request = calls[0]
    payload = request.payload
    assert request.endpoint.endswith(endpoint)
    body = payload["body"]
    markers = review_marker.find_finding_markers(body)
    assert len(markers) == 1
    assert markers[0]["key"] != prior_key
    _assert_prepared_body(body, expected_markers=markers)
    assert lookup == [ForgeCall("review_entries", ReviewTarget("o", "r", 7))]


def _poisoned_outbound_sinks_finding(property_names, reads, *, primary=False):
    finding = _RecordingFinding(
        {key: _poison(key) for key in property_names}, reads=reads
    )
    finding["consolidation_key"] = "poison-group"
    finding["consolidation_primary"] = primary
    return finding


def test_poisoned_outbound_sinks__poisoned_gitlab_live_fallback_discussion_and_note(
    tmp_path, forge_factory
):
    property_names = set(_js_finding_property_union()) | {"unknown_key"}
    primary = _poisoned_outbound_sinks_finding(property_names, set(), primary=True)
    corroborator = _poisoned_outbound_sinks_finding(property_names, set())
    primary["consolidation_key"] = "poison-group"
    corroborator["consolidation_key"] = "poison-group"
    rejected_calls = _deliver(
        "gitlab",
        [primary, corroborator],
        prior=PriorDelivery(False, frozenset(), frozenset(), None),
        reject_first=True,
        live=True,
        review_body=_poison("review_body"),
        check_position=False,
        lines={
            (
                [primary, corroborator][0].get("file"),
                [primary, corroborator][0].get("line"),
            ): None
        },
        texts={
            (
                [primary, corroborator][0].get("file"),
                [primary, corroborator][0].get("line"),
            ): "context"
        },
    )[0]
    discussions = [
        request.payload
        for request in rejected_calls
        if request.endpoint.endswith("/discussions")
    ]
    assert len(discussions) == 2
    fallback_body = discussions[-1]["body"]
    fallback_markers = review_marker.find_finding_markers(fallback_body)
    assert len(fallback_markers) == 1
    _assert_prepared_poison(
        fallback_body, property_names, expected_markers=fallback_markers
    )

    primary = _poisoned_outbound_sinks_finding(property_names, set(), primary=True)
    corroborator = _poisoned_outbound_sinks_finding(property_names, set())
    primary["consolidation_key"] = "poison-group"
    corroborator["consolidation_key"] = "poison-group"
    corroborator["line"] = None
    with patch(
        "gauntlet.delivery.compose.finding_key",
        side_effect=lambda _file, member_line, _title, _body: (
            "1" * 16 if member_line is not None else "2" * 16
        ),
    ):
        note_calls = _deliver(
            "gitlab",
            [primary, corroborator],
            prior=PriorDelivery(True, frozenset({"1" * 16}), frozenset(), None),
            live=True,
            review_body=_poison("review_body"),
            check_position=False,
            lines={
                (
                    [primary, corroborator][0].get("file"),
                    [primary, corroborator][0].get("line"),
                ): None
            },
            texts={
                (
                    [primary, corroborator][0].get("file"),
                    [primary, corroborator][0].get("line"),
                ): "context"
            },
        )[0]
    assert len(note_calls) == 1
    request = note_calls[0]
    payload = request.payload
    assert request.endpoint.endswith("/notes")
    assert "position" not in payload
    note_markers = review_marker.find_finding_markers(payload["body"])
    assert note_markers == [{"sha": SHA, "key": "2" * 16}]
    _assert_prepared_poison(
        payload["body"], property_names, expected_markers=note_markers
    )


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
    finding = {
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

    def string_leaves(value):
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


def test_javascript_backtick_title_boundary():
    script = """
import {renderSummaryBody} from './workflows/src/renderReport.js';
let source = '';
for await (const chunk of process.stdin) source += chunk;
process.stdout.write(renderSummaryBody(JSON.parse(source)));
"""
    result = subprocess.run(
        [NODE, "--input-type=module", "-e", script],
        cwd=REPO,
        input=json.dumps(
            {
                "findings": [
                    {
                        "id": "OUT",
                        "file": "src/file.py",
                        "line_start": 3,
                        "title": "`backtick-breakout @body <b>",
                        "severity": "high",
                    }
                ]
            }
        ),
        text=True,
        capture_output=True,
        encoding="utf-8",
        check=True,
    )
    bullet = next(line for line in result.stdout.splitlines() if line.startswith("- "))
    assert "\\`backtick-breakout \uff20body &lt;b>" in bullet
    assert "`" not in bullet.split("\\`backtick-breakout \uff20body &lt;b>", 1)[1]


@pytest.mark.parametrize(
    "title,expected_key",
    [
        pytest.param(7, "07961d7c0f9dd168", id="non-string-title"),
    ],
)
def test_title_key_wiring(title, expected_key):
    member = {
        "file": "src/edited.py",
        "line": 61,
        "severity": "high",
        "title": title,
        "body": "Body one",
    }
    payloads, _ = _deliver(
        "gitlab",
        [member],
        live=True,
        lines={("src/edited.py", 61): 50},
        texts={("src/edited.py", 61): "context"},
    )
    body = next(p.payload["body"] for p in payloads if "position" in p.payload)
    assert review_marker.find_finding_marker(body)["key"] == expected_key
    assert "**\U0001f7e0 [HIGH] Finding**\n\nBody one" in body

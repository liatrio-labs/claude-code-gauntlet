"""Delivery tests pin parsed diffs, payloads, composition, idempotency, and failure degradation."""

import contextlib
import copy
import hashlib
import inspect
import io
import json
import os
import re
import subprocess
import sys
import unittest
from dataclasses import dataclass, replace
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar, Literal, cast
from unittest.mock import patch

import gauntlet.delivery.fold as fold
import gauntlet.delivery.post as post_review
import gauntlet.marker as review_marker
import gauntlet.prior_review as detect_prior_review
import pytest
from gauntlet import diff as diff_api
from gauntlet import proc
from gauntlet.delivery.post import (
    _blockquote,
    _delivery_marker_suffix,
    _inline_body_over_limit,
    _render_group_sections,
    _report_inline_budget,
    _suggestion_fence,
    build_footer,
    build_skipped_section,
    compose_inline_body,
    compose_review_body,
    consolidate_delivery,
    fetch_diff_facts,
    render_comment_body,
    render_group_body,
    resolve_marker_sha,
    summary_body_from_report,
    validate_position,
)
from gauntlet.forge import (
    ForgeUnavailable,
    JsonFetch,
    PostRequest,
    PostResult,
    ReviewTarget,
    github_review_request,
)
from gauntlet.markdown import fence_closer
from gauntlet.prior_review import PriorDelivery

from tests.support.diff import diff_facts
from tests.support.forge import FakeForge, FakeForgeFactory, FakeGitLab, ForgeCall
from tests.support.prior import prior_notes


def test_prior_delivery_uses_injected_forge(forge_factory, monkeypatch):
    fake = forge_factory.configure(
        FakeGitLab(
            entries=[
                JsonFetch([{"body": review_marker.build_footer(1, "a" * 40)}], None)
            ]
        )
    )
    monkeypatch.setattr(post_review, "DRY_RUN", False)
    assert post_review.gitlab_prior_delivery(
        "o", "r", 5, "a" * 40, forge=fake
    ) == PriorDelivery(True, frozenset(), frozenset(), None)
    assert forge_factory.calls == []
    assert fake.calls == [ForgeCall("review_entries", ReviewTarget("o", "r", 5))]


pytestmark = pytest.mark.usefixtures(
    "forge_factory", "poster_state", "poster_workspace"
)


REPO = Path(__file__).resolve().parents[1]
_MISSING_SEVERITY = object()


@pytest.mark.parametrize(
    "platform, diff, expected, new_files, old_paths, texts",
    [
        (
            "github",
            "--- a/foo.py\n+++ b/foo.py\n@@ -1,1 +1,2 @@\n existing\n+added\n",
            {("foo.py", 1): 1, ("foo.py", 2): None},
            set(),
            {"foo.py": "foo.py"},
            {("foo.py", 1): "existing", ("foo.py", 2): "added"},
        ),
        (
            "gitlab",
            "--- b/bar.py\n+++ b/bar.py\n@@ -5,1 +5,2 @@\n ctx\n+new_line\n",
            {("b/bar.py", 5): 5, ("b/bar.py", 6): None},
            set(),
            {"b/bar.py": "b/bar.py"},
            {("b/bar.py", 5): "ctx", ("b/bar.py", 6): "new_line"},
        ),
    ],
    ids=["github-dispatches-to-gh-pr-diff", "gitlab-dispatches-to-glab-mr-diff"],
)
@pytest.mark.parametrize("status", [0, 128], ids=["success", "nonzero"])
def test_diff_fetch_integration(
    platform, diff, expected, new_files, old_paths, texts, status, capsys
):
    fake = FakeForge(platform, diffs=[(diff, "fatal: not a git repository", status)])
    target = ReviewTarget("myorg", "myrepo", 42)
    got = fetch_diff_facts(target, forge=fake)
    assert fake.calls == [ForgeCall("diff", target)]
    if status:
        assert got is None
        assert capsys.readouterr().err == (
            "WARNING: Could not fetch diff (exit 128): fatal: not a git repository. "
            "Skipping line validation \u2014 all findings will be posted.\n"
        )
    else:
        assert got == diff_facts(
            expected, new_files=new_files, old_paths=old_paths, line_texts=texts
        )
        assert capsys.readouterr().err == ""


@pytest.mark.parametrize("platform", ["github", "gitlab"])
def test_diff_fetch_empty_success(
    platform: Literal["github", "gitlab"], capsys: pytest.CaptureFixture[str]
) -> None:
    fake = FakeForge(platform, diffs=[("", "", 0)])
    target = ReviewTarget("myorg", "myrepo", 42)
    assert fetch_diff_facts(target, forge=fake) == diff_api.DiffFacts(
        {}, frozenset(), {}, {}
    )
    assert fake.calls == [ForgeCall("diff", target)]
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize(
    "finding, expected",
    [
        pytest.param({}, (True, None), id="absent-fix"),
        pytest.param(
            {"suggested_fix_code": 42}, (False, "non_string"), id="non-string"
        ),
        pytest.param({"suggested_fix_code": ""}, (False, "empty"), id="empty"),
        pytest.param(
            {"suggested_fix_code": "replacement", "line": 1},
            (False, "missing_end_line"),
            id="missing-end",
        ),
        pytest.param(
            {"suggested_fix_code": "replacement", "line": True, "end_line": 1},
            (False, "invalid_range"),
            id="invalid-range",
        ),
        pytest.param(
            {
                "suggested_fix_code": "replacement",
                "file": "x",
                "line": 1,
                "end_line": 1,
            },
            (False, "no_diff_oracle"),
            id="absent-oracle",
        ),
    ],
)
def test_fix_failure_order_without_diff(
    finding: dict[str, object], expected: tuple[bool, str | None]
) -> None:
    assert post_review._fence_verdict(finding, (1, 1), None) == expected


@pytest.mark.parametrize(
    "facts, apply_range, expected",
    [
        pytest.param(None, None, (False, "no_diff_oracle"), id="absent-no-anchor"),
        pytest.param(
            None, (1, 2), (False, "no_diff_oracle"), id="absent-mismatched-anchor"
        ),
        pytest.param(
            diff_facts({("b/x", 2): 2, ("x", 2): 2}),
            (1, 1),
            (False, "no_diff_oracle"),
            id="ambiguous-off-diff",
        ),
    ],
)
def test_fix_oracle_order(
    facts: diff_api.DiffFacts | None,
    apply_range: tuple[int, int] | None,
    expected: tuple[bool, str],
) -> None:
    finding = {
        "suggested_fix_code": "replacement",
        "file": "b/x",
        "line": 1,
        "end_line": 1,
    }
    assert post_review._fence_verdict(finding, apply_range, facts) == expected


@pytest.mark.parametrize(
    "function",
    [
        post_review.post_github,
        post_review.post_gitlab,
        fetch_diff_facts,
        post_review.fetch_gitlab_shas,
    ],
    ids=["github-poster", "gitlab-poster", "diff-fetch", "versions-fetch"],
)
def test_required_keyword_forge(function):
    parameter = inspect.signature(function).parameters["forge"]
    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
    assert parameter.default is inspect.Parameter.empty


@pytest.mark.parametrize("platform", ["github", "gitlab"])
def test_missing_forge_tool(platform, capsys):
    tool = "gh" if platform == "github" else "glab"
    message = f"'{tool}' CLI tool not found. Install it and ensure it is authenticated before running this script."
    fake = (FakeForge if platform == "github" else FakeGitLab)(
        availability=[ForgeUnavailable(message)]
    )
    data = {"owner": "o", "repo": "r", "pr_number": 1}
    with pytest.raises(SystemExit) as exc:
        if platform == "github":
            post_review.post_github(data, diff_facts({}, line_texts={}), forge=fake)
        else:
            post_review.post_gitlab(
                data,
                diff_facts({}, line_texts={}, new_files=set(), old_paths={}),
                forge=fake,
            )
    assert exc.value.code == 1
    assert capsys.readouterr().err == f"ERROR: {message}\n"
    assert fake.calls == [ForgeCall("ensure_available")]


@pytest.mark.parametrize(
    "result, message",
    [
        (JsonFetch([], None), "MR versions endpoint returned an empty list."),
        (
            JsonFetch(
                None,
                "Failed to fetch MR versions (exit 3): denied\nEnsure glab is authenticated and the MR IID is correct.",
            ),
            "Failed to fetch MR versions (exit 3): denied\nEnsure glab is authenticated and the MR IID is correct.",
        ),
        (
            JsonFetch(None, "Could not parse MR versions response: bad json"),
            "Could not parse MR versions response: bad json",
        ),
    ],
    ids=["empty-list", "nonzero", "parse"],
)
def test_versions_refusal(result, message, capsys):
    target = ReviewTarget("o", "r", 1)
    fake = FakeGitLab(refs=[result])
    with pytest.raises(SystemExit) as exc:
        post_review.fetch_gitlab_shas(target, forge=fake)
    assert exc.value.code == 1
    assert capsys.readouterr().err == f"ERROR: {message}\n"
    assert fake.calls == [ForgeCall("ensure_available"), ForgeCall("diff_refs", target)]


@pytest.mark.parametrize("dry_run", [False, True], ids=["live", "dry-run"])
@pytest.mark.parametrize("fatal", [False, True], ids=["tolerant", "fatal"])
@pytest.mark.parametrize(
    "result",
    [
        PostResult({"id": 7}, None, None),
        PostResult({"raw": "text"}, None, "Could not parse API response as JSON: text"),
        PostResult(None, "API call failed (exit 1).", None),
    ],
    ids=["success", "warning", "rejected"],
)
def test_semantic_post_policy(dry_run, fatal, result, monkeypatch, capsys):
    monkeypatch.setattr(post_review, "DRY_RUN", dry_run)
    request = github_review_request(
        ReviewTarget("o", "r", 1), {"body": "review", "event": "COMMENT"}
    )
    fake = FakeForge(submissions=[result])
    if fatal and result.error and not dry_run:
        with pytest.raises(SystemExit) as exc:
            post_review.post_json(request, forge=fake)
        assert exc.value.code == 1
        assert capsys.readouterr().err == f"ERROR: {result.error}\n"
    else:
        if fatal:
            response = post_review.post_json(request, forge=fake)
            assert response == ({} if dry_run else result.response)
        else:
            assert post_review.try_post_json(request, forge=fake) == (
                ({}, None) if dry_run else (result.response, result.error)
            )
        assert capsys.readouterr().err == (
            f"WARNING: {result.warning}\n" if result.warning and not dry_run else ""
        )
    assert ([request] if dry_run else []) == post_review._CAPTURED
    assert fake.calls == ([] if dry_run else [ForgeCall("submit", request=request)])


@pytest.mark.parametrize("platform", ["github", "gitlab"])
def test_empty_semantic_capture_defaults(platform):
    expected = {"platform": platform, "skipped": []}
    if platform == "github":
        expected.update(endpoint="", method="POST", payload={})
    else:
        expected.update(summary={}, discussions=[])
    assert post_review.build_dry_run_payload(platform) == expected


@pytest.mark.parametrize(
    "dry_run, expected_methods",
    [
        (
            False,
            [
                "diff",
                "ensure_available",
                "ensure_available",
                "diff_refs",
                "review_entries",
                "submit",
            ],
        ),
        (True, ["diff", "ensure_available", "ensure_available", "diff_refs"]),
    ],
    ids=["live-gitlab", "dry-run-gitlab"],
)
def test_selected_forge_is_passed_through_main(
    dry_run: bool,
    expected_methods: list[str],
    tmp_path: Path,
    forge_factory: FakeForgeFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def no_process(*args: object, **kwargs: object) -> proc.CompletedProcess[str]:
        pytest.fail("explicit platform and SHA must use only the selected Forge")

    monkeypatch.setattr(proc, "run", no_process)
    fake = forge_factory.configure(
        FakeGitLab(
            diffs=[("", "", 0)],
            refs=[
                JsonFetch(
                    [
                        {
                            "base_commit_sha": "b",
                            "head_commit_sha": "h",
                            "start_commit_sha": "s",
                        }
                    ],
                    None,
                )
            ],
        )
    )
    path = tmp_path / "findings.json"
    path.write_text(
        json.dumps(_review_data(platform="gitlab", pr_number=7, sha="a" * 40)),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        sys, "argv", ["post_review.py", str(path)] + (["--dry-run"] if dry_run else [])
    )
    post_review.main()
    assert forge_factory.calls == ["gitlab"]
    assert [call.method for call in fake.calls] == expected_methods
    assert all(
        call.target == ReviewTarget("o", "r", 7)
        for call in fake.calls
        if call.target is not None
    )


@pytest.mark.parametrize(
    ("case", "message"),
    [
        ("missing", "Findings file not found:"),
        ("invalid_json", "Invalid JSON in findings file:"),
        ("invalid_utf8", None),
    ],
    ids=["missing", "invalid-json", "invalid-utf8"],
)
def test_main_handles_findings_file_read_errors(
    case, message, monkeypatch, tmp_path, capsys
):
    findings_path = tmp_path / "findings.json"
    if case == "invalid_json":
        findings_path.write_text("{", encoding="utf-8")
    elif case == "invalid_utf8":
        findings_path.write_bytes(b"\xff")

    monkeypatch.setattr(sys, "argv", ["post_review.py", str(findings_path)])
    if case == "invalid_utf8":
        with pytest.raises(UnicodeDecodeError):
            post_review.main()
        return

    with pytest.raises(SystemExit) as caught:
        post_review.main()

    assert caught.value.code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert message in captured.err
    if case == "missing":
        assert captured.err == f"ERROR: Findings file not found: {findings_path}\n"


def _severity_matrix():
    """Return fixed public-seam oracles for closed, total severity rendering."""
    low = "\U0001f4a1"
    high = "\U0001f7e0"
    matrix = [
        ("oversized", "s" * 70000, "LOW", low),
        ("int", 3, "LOW", low),
        ("list", ["high"], "LOW", low),
        ("dict", {"severity": "high"}, "LOW", low),
        ("none", None, "LOW", low),
        ("padded", " high ", "HIGH", high),
        ("tabbed-case", "\tHiGh\r\n", "HIGH", high),
        ("uppercase", "HIGH", "HIGH", high),
        ("title-case", "High", "HIGH", high),
        ("lowercase", "high", "HIGH", high),
        ("unknown", "unknown", "LOW", low),
        ("embedded-newline", "high\nfoo", "LOW", low),
        ("missing", _MISSING_SEVERITY, "LOW", low),
        ("empty", "", "LOW", low),
        ("whitespace-only", " \t\r\n ", "LOW", low),
        ("critical", "critical", "CRITICAL", "\U0001f534"),
        ("medium", "medium", "MEDIUM", "\U0001f7e1"),
        ("low", "low", "LOW", low),
        ("bom-padded", "\ufeffhigh\ufeff", "HIGH", high),
        ("line-separator-padded", "\u2028high\u2029", "HIGH", high),
        ("next-line-padded", "\x85high\x85", "LOW", low),
        ("file-separator-padded", "\x1chigh\x1c", "LOW", low),
    ]
    js_trim_chars = (
        "\t",
        "\n",
        "\v",
        "\f",
        "\r",
        " ",
        "\u00a0",
        "\u1680",
        "\u2000",
        "\u2001",
        "\u2002",
        "\u2003",
        "\u2004",
        "\u2005",
        "\u2006",
        "\u2007",
        "\u2008",
        "\u2009",
        "\u200a",
        "\u2028",
        "\u2029",
        "\u202f",
        "\u205f",
        "\u3000",
        "\ufeff",
    )
    python_only_chars = ("\x1c", "\x1d", "\x1e", "\x1f", "\x85")
    matrix.extend(
        (f"trim-U+{ord(char):04X}", f"{char}high{char}", "HIGH", high)
        for char in js_trim_chars
    )
    matrix.extend(
        (f"python-only-U+{ord(char):04X}", f"{char}high{char}", "LOW", low)
        for char in python_only_chars
    )
    return matrix


# ---------------------------------------------------------------------------
# render_comment_body
# ---------------------------------------------------------------------------


class TestOutboundSanitizeHelpers(unittest.TestCase):
    def test_blockquote_prefixes_every_line_bare_gt_on_blank(self):
        self.assertEqual(
            _blockquote("a\n\nb"),
            "> a\n>\n> b",
        )

    def test_blockquote_normalizes_cr_before_prefix(self):
        # Defense in depth: even if a CR reached _blockquote, it must not
        # become a line ending after a single '>' that escapes the quote.
        self.assertEqual(_blockquote("a\rb"), "> a\n> b")
        self.assertEqual(_blockquote("a\r\nb"), "> a\n> b")

    def test_cited_rule_cr_cannot_escape_blockquote(self):
        finding = {
            "severity": "medium",
            "title": "T",
            "body": "b",
            "claude_md_rule": "keep\rescape **bold**",
        }
        body = render_comment_body(finding)
        self.assertIn("**Cited rule:**", body)
        # CR stripped by sanitize → single line inside the quote.
        self.assertIn("> keepescape **bold**", body)
        self.assertNotIn("\r", body)

    def test_offsets_are_stated_in_the_header(self):
        open_f, close_f = _suggestion_fence("return None", offsets=(0, 2))
        self.assertEqual(open_f, "```suggestion:-0+2")
        self.assertEqual(close_f, "```")

    def test_both_offsets_are_stated_even_when_one_is_zero(self):
        """GitLab's parser takes ``-m`` and ``+n`` independently, so either could
        be omitted — emitting both makes the header state the whole range."""
        open_f, _ = _suggestion_fence("x", offsets=(2, 0))
        self.assertEqual(open_f, "```suggestion:-2+0")

    def test_no_offsets_and_zero_offsets_are_the_plain_header(self):
        """``suggestion:-0+0`` is an exact synonym for ``suggestion``, so the
        single-line bytes every platform understands are what ships."""
        for offsets in (None, (0, 0)):
            with self.subTest(offsets=offsets):
                open_f, _ = _suggestion_fence("x", offsets=offsets)
                self.assertEqual(open_f, "```suggestion")

    def test_offsets_compose_with_the_backtick_escalation(self):
        """Fence length and header are independent — GitLab's parser is
        fence-length blind, and its own docs show ````suggestion:-0+2."""
        open_f, close_f = _suggestion_fence("line1\n```\nline3", offsets=(0, 2))
        self.assertEqual(open_f, "````suggestion:-0+2")
        self.assertEqual(close_f, "````")


class TestRenderCommentBody(unittest.TestCase):
    def test_critical_severity_emoji(self):
        finding = {
            "severity": "critical",
            "title": "SQL Injection",
            "body": "User input is not sanitized before being passed to the database query.",
        }
        body = render_comment_body(finding)
        self.assertIn("[CRITICAL]", body)
        self.assertIn("\U0001f534", body)  # 🔴

    def test_high_severity_emoji(self):
        finding = {
            "severity": "high",
            "title": "Bug",
            "body": "Description of the bug.",
        }
        body = render_comment_body(finding)
        self.assertIn("[HIGH]", body)
        self.assertIn("\U0001f7e0", body)  # 🟠

    def test_medium_severity_emoji(self):
        finding = {
            "severity": "medium",
            "title": "Issue",
            "body": "Description of the issue.",
        }
        body = render_comment_body(finding)
        self.assertIn("[MEDIUM]", body)
        self.assertIn("\U0001f7e1", body)  # 🟡

    def test_low_severity_emoji(self):
        finding = {"severity": "low", "title": "Nit", "body": "Minor issue."}
        body = render_comment_body(finding)
        self.assertIn("[LOW]", body)
        self.assertIn("\U0001f4a1", body)  # 💡

    def test_with_suggestion_block(self):
        finding = {
            "severity": "high",
            "title": "Fix",
            "body": "Need to fix this.",
            "suggested_fix_code": "return None",
        }
        body = render_comment_body(finding)
        self.assertIn("```suggestion", body)
        self.assertIn("return None", body)

    def test_without_suggestion_block(self):
        finding = {
            "severity": "medium",
            "title": "Issue",
            "body": "Some description.",
        }
        body = render_comment_body(finding)
        self.assertNotIn("```suggestion", body)

    def test_missing_body(self):
        finding = {"severity": "low", "title": "Nit"}
        body = render_comment_body(finding)
        self.assertIn("[LOW]", body)
        self.assertIn("Nit", body)

    def test_unknown_severity_falls_back_to_bulb(self):
        finding = {"severity": "unknown", "title": "Thing", "body": "desc"}
        body = render_comment_body(finding)
        self.assertIn("\U0001f4a1", body)  # 💡 fallback
        self.assertEqual(
            body,
            "**\U0001f4a1 [LOW] Thing**\n\ndesc\n\n" + post_review.BRAND_TRAILER,
        )
        self.assertNotIn("[UNKNOWN]", body)

    def test_the_rendered_emoji_is_read_from_the_generated_constants(self):
        """The delivered byte comes THROUGH SEVERITY_EMOJI, not from a local literal.

        The severity tests above pin characters with hard-coded escapes, which is the
        second oracle — but they stay green if the renderer re-inlines its own
        `emoji_map`, and the delivered emoji then diverges silently from the generated
        constants and from every generated legend. Substituting a sentinel the repo
        renders nowhere is what pins the wire itself.
        """
        for severity, emoji in post_review.SEVERITY_EMOJI.items():
            with self.subTest(severity=severity):
                body = render_comment_body(
                    {"severity": severity, "title": "t", "body": "b"}
                )
                self.assertIn(emoji, body)
        sentinel = "\u26a1"  # HIGH VOLTAGE SIGN — in no legend, in no severity map
        with patch.dict(post_review.SEVERITY_EMOJI, {"medium": sentinel}):
            self.assertIn(
                sentinel,
                render_comment_body({"severity": "medium", "title": "t", "body": "b"}),
            )
        with patch.dict(post_review.SEVERITY_EMOJI, {"low": sentinel}):
            self.assertEqual(
                render_comment_body({"severity": "nope", "title": "t", "body": "b"}),
                "**\u26a1 [LOW] t**\n\nb\n\n" + post_review.BRAND_TRAILER,
            )
        # A map with no `low` key is the generator's placeholder shape. The poster
        # stays total there: the fallback glyph comes from SEVERITY_EMOJI_FALLBACK.
        placeholder_map = patch.dict(
            post_review.SEVERITY_EMOJI, {"severity": "{emoji}"}, clear=True
        )
        with (
            placeholder_map,
            patch.object(post_review, "SEVERITY_EMOJI_FALLBACK", sentinel),
        ):
            self.assertEqual(
                render_comment_body({"severity": "nope", "title": "t", "body": "b"}),
                "**\u26a1 [LOW] t**\n\nb\n\n" + post_review.BRAND_TRAILER,
            )

    def test_severity_labels_are_closed_and_total(self):
        """Public rendering normalizes malformed, padded, and missing severities."""
        for case_id, raw, label, glyph in _severity_matrix():
            with self.subTest(case=case_id):
                finding = {"title": "Thing", "body": "desc"}
                if raw is not _MISSING_SEVERITY:
                    finding["severity"] = raw
                before = copy.deepcopy(finding)
                expected = f"**{glyph} [{label}] Thing**\n\ndesc"
                body = render_comment_body(finding)
                if case_id == "oversized":
                    self.assertLess(len(body.encode("utf-8")), 256)
                    self.assertNotIn("s" * 1000, body)
                self.assertEqual(body, expected + f"\n\n{post_review.BRAND_TRAILER}")
                self.assertEqual(finding, before)

    def test_severity_fallback_and_labels_match_js_twin(self):
        """The poster matches the live report normalizer and its generated map seam."""
        matrix = _severity_matrix()
        node_inputs = []
        for _case_id, raw, _label, _glyph in matrix:
            row = {"missing": raw is _MISSING_SEVERITY}
            if raw is not _MISSING_SEVERITY:
                row["value"] = raw
            node_inputs.append(row)
        node_script = """
import { normalizeReportSeverity } from './workflows/src/renderReport.js';

let source = '';
for await (const chunk of process.stdin) source += chunk;
const rows = JSON.parse(source);
const results = rows.map((row) => normalizeReportSeverity(
  row.missing ? undefined : row.value,
));
process.stdout.write(JSON.stringify(results));
"""
        result = subprocess.run(
            ["node", "--input-type=module", "-e", node_script],
            cwd=REPO,
            input=json.dumps(node_inputs, ensure_ascii=False),
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
            encoding="utf-8",
        )
        node_labels = json.loads(result.stdout)
        self.assertEqual(len(node_labels), len(matrix))

        for (case_id, raw, label, glyph), node_label in zip(
            matrix, node_labels, strict=True
        ):
            with self.subTest(case=case_id):
                self.assertEqual(node_label, label.lower())
                finding = {"title": "Thing", "body": "desc"}
                if raw is not _MISSING_SEVERITY:
                    finding["severity"] = raw
                before = copy.deepcopy(finding)
                expected_label = node_label.upper()
                expected = f"**{glyph} [{expected_label}] Thing**\n\ndesc"
                rendered = render_comment_body(finding)
                if case_id == "oversized":
                    self.assertLess(len(rendered.encode("utf-8")), 256)
                    self.assertNotIn("s" * 1000, rendered)
                self.assertEqual(
                    rendered, expected + f"\n\n{post_review.BRAND_TRAILER}"
                )
                self.assertEqual(post_review.key_material_body(finding), expected)
                self.assertEqual(finding, before)

        with patch.dict(post_review.SEVERITY_EMOJI, {"severity": "{emoji}"}):
            self.assertEqual(
                render_comment_body(
                    {"severity": "severity", "title": "Thing", "body": "desc"}
                ),
                "**{emoji} [SEVERITY] Thing**\n\ndesc\n\n" + post_review.BRAND_TRAILER,
            )

    def test_empty_suggested_fix_code_treated_as_absent(self):
        finding = {
            "severity": "high",
            "title": "Bug",
            "body": "desc",
            "suggested_fix_code": "",
        }
        body = render_comment_body(finding)
        self.assertNotIn("```suggestion", body)

    def test_suggested_fix_code_none_treated_as_absent(self):
        finding = {
            "severity": "high",
            "title": "Bug",
            "body": "desc",
            "suggested_fix_code": None,
        }
        body = render_comment_body(finding)
        self.assertNotIn("```suggestion", body)

    def test_multiline_suggested_fix_code(self):
        finding = {
            "severity": "medium",
            "title": "Fix",
            "body": "desc",
            "suggested_fix_code": "line1\nline2\nline3",
        }
        body = render_comment_body(finding)
        self.assertIn("```suggestion", body)
        self.assertIn("line1\nline2\nline3", body)

    def test_edge_blank_lines_survive_into_the_fence(self):
        """A replacement's leading/trailing blank lines are CONTENT (#63): the
        fence carries the stated bytes minus the one terminating newline, so
        what the apply-check measured is what one click commits."""
        finding = {
            "severity": "medium",
            "title": "Fix",
            "body": "desc",
            "suggested_fix_code": "\nline1\nline2\n\n",
        }
        body = render_comment_body(finding)
        self.assertIn("```suggestion\n\nline1\nline2\n\n```", body)

    def test_only_one_trailing_newline_comes_off(self):
        finding = {
            "severity": "medium",
            "title": "Fix",
            "body": "desc",
            "suggested_fix_code": "line1\n",
        }
        self.assertIn("```suggestion\nline1\n```", render_comment_body(finding))

    # -- suggestion (issue #47) -------------------------------------------

    def test_suggestion_present_renders_prose_block(self):
        finding = {
            "severity": "high",
            "title": "Bug",
            "body": "desc",
            "suggestion": "Use parameterized queries instead.",
        }
        body = render_comment_body(finding)
        self.assertIn("**Suggested fix:**", body)
        self.assertIn("Use parameterized queries instead.", body)

    def test_suggestion_absent_no_heading(self):
        finding = {"severity": "high", "title": "Bug", "body": "desc"}
        body = render_comment_body(finding)
        self.assertNotIn("Suggested fix:", body)

    def test_suggestion_empty_string_no_heading(self):
        finding = {"severity": "high", "title": "Bug", "body": "desc", "suggestion": ""}
        body = render_comment_body(finding)
        self.assertNotIn("Suggested fix:", body)

    def test_suggestion_none_no_heading(self):
        finding = {
            "severity": "high",
            "title": "Bug",
            "body": "desc",
            "suggestion": None,
        }
        body = render_comment_body(finding)
        self.assertNotIn("Suggested fix:", body)

    def test_suggestion_whitespace_only_no_heading(self):
        finding = {
            "severity": "high",
            "title": "Bug",
            "body": "desc",
            "suggestion": "   \n  ",
        }
        body = render_comment_body(finding)
        self.assertNotIn("Suggested fix:", body)

    # -- claude_md_rule / spec_text (issue #47) ---------------------------

    def test_claude_md_rule_present_renders_cited_rule(self):
        finding = {
            "severity": "medium",
            "title": "Convention violation",
            "body": "desc",
            "claude_md_rule": "Scripts must be stdlib-only Python.",
        }
        body = render_comment_body(finding)
        self.assertIn("**Cited rule:**", body)
        self.assertIn("> Scripts must be stdlib-only Python.", body)

    def test_spec_text_present_claude_md_rule_absent_renders_as_cited_rule(self):
        finding = {
            "severity": "medium",
            "title": "Intent mismatch",
            "body": "desc",
            "spec_text": "The spec says X must happen before Y.",
        }
        body = render_comment_body(finding)
        self.assertIn("**Cited rule:**", body)
        self.assertIn("> The spec says X must happen before Y.", body)

    def test_both_claude_md_rule_and_spec_text_present_rule_wins(self):
        finding = {
            "severity": "medium",
            "title": "Both",
            "body": "desc",
            "claude_md_rule": "The CLAUDE.md rule.",
            "spec_text": "The spec text.",
        }
        body = render_comment_body(finding)
        self.assertIn("**Cited rule:**", body)
        self.assertIn("> The CLAUDE.md rule.", body)
        self.assertNotIn("The spec text.", body)

    def test_neither_claude_md_rule_nor_spec_text_no_heading(self):
        finding = {"severity": "medium", "title": "Neither", "body": "desc"}
        body = render_comment_body(finding)
        self.assertNotIn("Cited rule:", body)

    def test_claude_md_rule_empty_falls_back_to_spec_text(self):
        finding = {
            "severity": "medium",
            "title": "Fallback",
            "body": "desc",
            "claude_md_rule": "",
            "spec_text": "The spec text wins here.",
        }
        body = render_comment_body(finding)
        self.assertIn("**Cited rule:**", body)
        self.assertIn("> The spec text wins here.", body)

    # -- ordering / combinations -------------------------------------------

    def test_suggestion_and_suggested_fix_code_both_render_prose_before_fence(self):
        finding = {
            "severity": "high",
            "title": "Fix",
            "body": "desc",
            "suggestion": "Explain the fix in words.",
            "suggested_fix_code": "return None",
        }
        # The SECTIONS, not the wire body: the identity trailer follows them (T-TRAIL
        # owns that), and this test's claim is about where the fence sits among the
        # sections.
        body = post_review._finding_sections(finding)
        self.assertIn("**Suggested fix:**", body)
        self.assertIn("Explain the fix in words.", body)
        self.assertIn("```suggestion", body)
        self.assertIn("return None", body)
        prose_idx = body.index("**Suggested fix:**")
        fence_idx = body.index("```suggestion")
        self.assertLess(
            prose_idx,
            fence_idx,
            "the prose suggestion block must come before the fence",
        )
        # The fence is still the last of the sections.
        self.assertTrue(body.rstrip("\n").endswith("```"))

    def test_multiline_suggestion_renders_without_corrupting_markdown(self):
        finding = {
            "severity": "medium",
            "title": "Fix",
            "body": "desc",
            "suggestion": "First do this.\nThen do that.\nFinally this.",
        }
        body = render_comment_body(finding)
        self.assertIn("**Suggested fix:**", body)
        self.assertIn("First do this.\nThen do that.\nFinally this.", body)

    def test_non_string_suggestion_and_rule_do_not_crash(self):
        finding = {
            "severity": "medium",
            "title": "Weird types",
            "body": "desc",
            "suggestion": 42,
            "claude_md_rule": 7,
        }
        body = render_comment_body(finding)
        self.assertIn("**Suggested fix:**", body)
        self.assertIn("42", body)
        self.assertIn("**Cited rule:**", body)
        self.assertIn("> 7", body)

    def test_no_new_fields_produces_byte_identical_output(self):
        """Regression pin: a finding with none of the new fields must produce
        exactly the same output as before this change, so the addition cannot
        silently reflow an ordinary comment.

        Pinned on the SECTIONS: the identity trailer is appended after them and is
        pinned separately (T-TRAIL), so this literal stays the pre-#122 bytes.
        """
        finding = {
            "severity": "high",
            "title": "SQL Injection",
            "body": "User input is not sanitized before being passed to the database query.",
        }
        body = post_review._finding_sections(finding)
        self.assertEqual(
            body,
            "**\U0001f7e0 [HIGH] SQL Injection**\n\n"
            "User input is not sanitized before being passed to the database query.",
        )

    def test_whitespace_only_claude_md_rule_falls_back_to_spec_text(self):
        # Symmetry with the empty-string fallback above: blank-but-present must be
        # indistinguishable from absent on BOTH halves of the cited-rule lookup, or an
        # agent that emits "  " suppresses the spec_text it should have deferred to.
        finding = {
            "severity": "low",
            "title": "Intent drift",
            "body": "desc",
            "claude_md_rule": "   ",
            "spec_text": "The endpoint MUST return 422 on a schema violation.",
        }
        body = render_comment_body(finding)
        self.assertIn("**Cited rule:**", body)
        self.assertIn("> The endpoint MUST return 422 on a schema violation.", body)

    def test_non_string_spec_text_does_not_crash(self):
        finding = {"severity": "low", "title": "T", "body": "b", "spec_text": 9}
        body = render_comment_body(finding)
        self.assertIn("**Cited rule:**", body)
        self.assertIn("> 9", body)

    def test_leading_newlines_are_stripped_from_rendered_values(self):
        # The sections are joined with their own blank lines, so a value padded at the FRONT
        # (a model that opens its suggestion with a newline) put a second blank line under the
        # heading. Only newlines are stripped — leading spaces belong to the value.
        finding = {
            "severity": "medium",
            "title": "T",
            "body": "b",
            "suggestion": "\n\n  indented advice",
        }
        body = render_comment_body(finding)
        self.assertIn("**Suggested fix:**\n  indented advice", body)

    def test_trailing_newlines_are_stripped_from_rendered_values(self):
        # _rendered_text promises this; without it a multi-line suggestion pushes a blank
        # line into whatever section follows (and, at the end, trails the comment).
        finding = {
            "severity": "medium",
            "title": "T",
            "body": "b",
            "suggestion": "Line one\nLine two\n\n",
            "claude_md_rule": "Rule text\n",
        }
        body = render_comment_body(finding)
        self.assertIn("**Suggested fix:**\nLine one\nLine two", body)
        self.assertIn("**Cited rule:**", body)
        self.assertIn("> Rule text", body)
        self.assertFalse(body.endswith("\n"))

    def test_non_string_suggested_fix_code_does_not_crash(self):
        # Pre-#47 this reached .rstrip() and raised AttributeError. The field now goes
        # through the same normalizer as the prose fields.
        finding = {
            "severity": "low",
            "title": "T",
            "body": "b",
            "suggested_fix_code": 123,
        }
        self.assertIn("```suggestion\n123\n```", render_comment_body(finding))

    def test_whitespace_only_suggested_fix_code_renders_no_fence(self):
        # A whitespace-only replacement would render a one-click-apply block that BLANKS
        # the cited lines — treat it as absent, like every other optional field.
        finding = {
            "severity": "low",
            "title": "T",
            "body": "b",
            "suggested_fix_code": "   \n  ",
        }
        self.assertNotIn("```suggestion", render_comment_body(finding))

    def test_artifact_only_fields_never_reach_the_comment_body(self):
        # A deliberate scoping decision from issue #47, pinned so it is a decision and not
        # a comment: these fields are carried end-to-end to the artifact and the report, but
        # the posted comment stays short. Changing that should require changing this test.
        finding = {
            "severity": "high",
            "title": "Missing rollback test",
            "body": "The rollback path is untested.",
            "suggestion": "Add a test that raises PaymentGatewayError.",
            "criticality": 9,
            "failure_scenario": "SENTINEL_FAILURE_SCENARIO",
            "evidence": "SENTINEL_EVIDENCE",
            "confidence": 90,
            "dimension": "test_coverage",
            "origin": "new",
        }
        body = render_comment_body(finding)
        self.assertIn("**Suggested fix:**", body)
        for sentinel in (
            "SENTINEL_FAILURE_SCENARIO",
            "SENTINEL_EVIDENCE",
            "criticality",
            "test_coverage",
            "90",
        ):
            self.assertNotIn(
                sentinel, body, f"{sentinel!r} leaked into the comment body"
            )

    def test_render_comment_body_ends_with_exactly_one_brand_trailer(self):
        """The identity trailer is the LAST line of a delivered comment body, and it
        is there exactly once — one mark per delivered SURFACE, never per element.

        Pinned by codepoint (U+2694 U+FE0F), never by a pasted glyph. The header line
        must stay byte-unchanged: ``bench/runner/score.py``'s ``_SEVERITY_RE`` reads
        ``[SEVERITY]`` out of it, so a trailer that reflowed the header would move a
        machine-parsed string.
        """
        trailer = "\u2694\ufe0f *Code Gauntlet*"
        finding = {
            "severity": "high",
            "title": "SQL Injection",
            "body": "User input is not sanitized.",
        }
        body = render_comment_body(finding)
        self.assertEqual(body.count(trailer), 1)
        self.assertEqual(body.splitlines()[-1], trailer)
        self.assertTrue(body.endswith(f"\n\n{trailer}"))
        self.assertEqual(
            body.splitlines()[0],
            "**\U0001f7e0 [HIGH] SQL Injection**",
            "the severity header line is machine-parsed — it must not move",
        )


class TestOutboundRenderBounding(unittest.TestCase):
    """Issue #122 — render_comment_body composed behaviors."""

    def test_rule_source_labels_are_keyed_on_claude_md_rule(self):
        labels = {
            "documented_rule": "Cited rule",
            "code_comment": "Cited comment",
            "repo_precedent": "Repo precedent",
            "self_inconsistency": "Inconsistency",
        }
        for source, label in labels.items():
            with self.subTest(source=source):
                body = render_comment_body(
                    {
                        "severity": "medium",
                        "title": "T",
                        "body": "b",
                        "claude_md_rule": "The cited text.",
                        "rule_source": source,
                    }
                )
                self.assertIn(f"**{label}:**", body)
                self.assertNotIn(source, body)

    def test_rule_source_unknown_values_use_fallback_and_do_not_render_raw(self):
        for source in ("constructor", "toString", "__proto__", "unknown_kind"):
            with self.subTest(source=source):
                body = render_comment_body(
                    {
                        "severity": "medium",
                        "title": "T",
                        "body": "b",
                        "claude_md_rule": "The cited text.",
                        "rule_source": source,
                    }
                )
                self.assertIn("**Cited rule:**", body)
                self.assertNotIn(source, body)

    def test_rule_source_is_ignored_when_spec_text_wins(self):
        body = render_comment_body(
            {
                "severity": "medium",
                "title": "T",
                "body": "b",
                "spec_text": "The specification text.",
                "rule_source": "repo_precedent",
            }
        )
        self.assertIn("**Cited rule:**", body)
        self.assertNotIn("**Repo precedent:**", body)
        self.assertNotIn("repo_precedent", body)

    def test_comment_only_claude_md_rule_omits_cited_rule_heading(self):
        finding = {
            "severity": "medium",
            "title": "T",
            "body": "b",
            "claude_md_rule": "<!-- steer the reviewer -->",
        }
        body = render_comment_body(finding)
        self.assertNotIn("Cited rule:", body)

    def test_comment_only_claude_md_rule_falls_back_to_spec_text(self):
        finding = {
            "severity": "medium",
            "title": "T",
            "body": "b",
            "claude_md_rule": "<!-- steer the reviewer -->",
            "spec_text": "The spec says X must happen before Y.",
        }
        body = render_comment_body(finding)
        self.assertIn("**Cited rule:**", body)
        self.assertIn("> The spec says X must happen before Y.", body)

    def test_cited_rule_is_blockquoted_multiline(self):
        finding = {
            "severity": "medium",
            "title": "T",
            "body": "b",
            "claude_md_rule": "line1\n\nline3",
        }
        body = render_comment_body(finding)
        self.assertIn("**Cited rule:**\n> line1\n>\n> line3", body)

    def test_long_rule_capped_with_marker(self):
        finding = {
            "severity": "medium",
            "title": "T",
            "body": "b",
            "claude_md_rule": "R" * 600,
        }
        body = render_comment_body(finding)
        self.assertIn("…[truncated]", body)
        # Quoted content before marker is 500 R's
        self.assertIn("> " + ("R" * 500) + "…[truncated]", body)

    def test_suggestion_sanitized_but_uncapped(self):
        finding = {
            "severity": "medium",
            "title": "T",
            "body": "b",
            "suggestion": ("fix it <!-- no --> " + ("s" * 600)),
        }
        body = render_comment_body(finding)
        self.assertIn("**Suggested fix:**", body)
        self.assertNotIn("<!--", body)
        self.assertNotIn("…[truncated]", body)
        self.assertIn("s" * 600, body)

    def test_fence_contains_payload_with_inner_triple_backticks(self):
        payload = "before\n```\nafter"
        open_f = "````suggestion"
        close_f = "````"
        finding = {
            "severity": "low",
            "title": "T",
            "body": "b",
            "suggested_fix_code": payload,
        }
        body = render_comment_body(finding)
        self.assertIn(open_f, body)
        self.assertIn(close_f, body)
        # Parse: content between first open and last close equals payload
        start = body.index(open_f) + len(open_f) + 1  # +1 for newline
        end = body.rindex("\n" + close_f)
        self.assertEqual(body[start:end], payload)

    def test_suggested_fix_code_token_redacted_inside_fence(self):
        tok = "ghp_" + ("C" * 36)
        payload = f"token = '{tok}'"
        finding = {
            "severity": "low",
            "title": "T",
            "body": "b",
            "suggested_fix_code": payload,
        }
        body = render_comment_body(finding)
        self.assertNotIn(tok, body)
        self.assertIn("[REDACTED]", body)
        self.assertIn("```suggestion", body)  # no backticks in redacted form → 3-fence


# ---------------------------------------------------------------------------
# build_footer
# ---------------------------------------------------------------------------


class TestBuildFooter(unittest.TestCase):
    def test_footer_contains_metadata(self):
        footer = build_footer(5, "abc1234")
        self.assertIn("code-gauntlet-findings:", footer)
        self.assertIn('"findings_count":5', footer)
        self.assertIn('"sha":"abc1234"', footer)
        self.assertIn("<!--", footer)
        self.assertIn("-->", footer)

    def test_footer_valid_json(self):
        footer = build_footer(3, "def5678")
        # Extract the JSON from the HTML comment
        import re

        m = re.search(r"code-gauntlet-findings:\s*({.*})", footer)
        self.assertIsNotNone(m)
        data = json.loads(m.group(1))
        self.assertEqual(data["findings_count"], 3)
        self.assertEqual(data["sha"], "def5678")
        self.assertEqual(data["version"], "3.0")


# ---------------------------------------------------------------------------
# resolve_marker_sha (Issue #39 D6)
# ---------------------------------------------------------------------------


class TestResolveMarkerSha(unittest.TestCase):
    """The persisted payload's own ``sha`` (the commit the review actually ran
    against) is preferred over a freshly re-resolved HEAD, so a HEAD that moved
    between the workflow run and the post cannot mislabel the marker."""

    @patch("gauntlet.delivery.post.get_head_sha", return_value="deadbeef")
    def test_prefers_data_sha_when_sha_shaped(self, mock_head):
        data = {"sha": "0f1e2d3c4b5a69788716253413121110090807a"}
        self.assertEqual(resolve_marker_sha(data), data["sha"])
        mock_head.assert_not_called()

    @patch("gauntlet.delivery.post.get_head_sha", return_value="deadbeef")
    def test_falls_back_to_head_when_sha_absent(self, mock_head):
        self.assertEqual(resolve_marker_sha({}), "deadbeef")
        mock_head.assert_called_once()

    @patch("gauntlet.delivery.post.get_head_sha", return_value="deadbeef")
    def test_falls_back_to_head_when_sha_none(self, mock_head):
        self.assertEqual(resolve_marker_sha({"sha": None}), "deadbeef")

    @patch("gauntlet.delivery.post.get_head_sha", return_value="deadbeef")
    def test_rejects_non_sha_shaped_value_and_falls_back(self, mock_head):
        self.assertEqual(resolve_marker_sha({"sha": "not-a-real-sha!!"}), "deadbeef")

    @patch("gauntlet.delivery.post.get_head_sha", return_value="deadbeef")
    def test_rejects_non_string_sha_and_falls_back(self, mock_head):
        self.assertEqual(resolve_marker_sha({"sha": 12345}), "deadbeef")

    @patch("gauntlet.delivery.post.get_head_sha", return_value="deadbeef")
    def test_accepts_short_sha_shaped_value(self, mock_head):
        self.assertEqual(resolve_marker_sha({"sha": "abc1234"}), "abc1234")
        mock_head.assert_not_called()

    @patch("gauntlet.delivery.post.warn")
    @patch("gauntlet.delivery.post.get_head_sha", return_value="unknown")
    def test_degraded_fallback_when_head_sha_itself_unresolvable(
        self, mock_head, mock_warn
    ):
        """git rev-parse HEAD failing makes get_head_sha() return "unknown" —
        not SHA-shaped, so the caller must be warned the posted marker will be
        undetectable rather than have the failure pass silently."""
        self.assertEqual(resolve_marker_sha({}), "unknown")
        mock_warn.assert_called_once()
        self.assertIn("unknown", mock_warn.call_args[0][0])


@pytest.mark.parametrize(
    "stdout, status, expected",
    [(" abc1234\n", 0, "abc1234"), ("", 0, ""), ("abc1234", 1, "unknown")],
    ids=["success", "empty-success", "nonzero"],
)
def test_poster_head_sha_uses_local_git(
    stdout: str, status: int, expected: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[str]] = []

    def run(command: list[str], **kwargs: object) -> proc.CompletedProcess[str]:
        calls.append(command)
        assert kwargs == {"cwd": None, "timeout": None, "errors": "strict"}
        return proc.CompletedProcess(command, status, stdout, "")

    monkeypatch.setattr(proc, "run", run)
    assert post_review.get_head_sha() == expected
    assert calls == [["git", "rev-parse", "HEAD"]]


# ---------------------------------------------------------------------------
# validate_position — the position gate
# ---------------------------------------------------------------------------


def _parse_fixture(diff, platform="gitlab"):
    """Use the real parser so position tests cannot invent the oracles they want."""
    return diff_api.parse_diff(diff, policy=diff_api.posting_policy(platform))


_MR_SHAS = ("base1", "head1", "start1")

# The four keys every position carries whatever the finding is. Spelled here so a test
# case states only the fields its own case is about.
_POSITION_INVARIANTS = {
    "position_type": "text",
    "base_sha": "base1",
    "head_sha": "head1",
    "start_sha": "start1",
}


def _position(**fields):
    """A position carrying the invariant keys plus *fields*."""
    return dict(_POSITION_INVARIANTS, **fields)


class TestValidatePosition(unittest.TestCase):
    """One test per violation branch, each mutating a sound position in exactly ONE way.

    Every case asserts a single problem, so a check that fires on the wrong input shows
    up as an extra entry rather than hiding inside a truthy list.
    """

    # A sound context-line position for a modified file. Every violation test below
    # starts here and breaks one field.
    def _sound(self, **fields):
        return _position(
            new_path="src/edited.py",
            new_line=61,
            old_line=50,
            old_path="src/edited.py",
            **fields,
        )

    def _check(self, position, **overrides):
        kwargs = {
            "shas": _MR_SHAS,
            "facts": diff_facts(
                {("src/edited.py", 61): 50},
                old_paths={"src/edited.py": "src/edited.py"},
            ),
            "filepath": "src/edited.py",
            "line": 61,
        }
        kwargs.update(overrides)
        return validate_position(position, **kwargs)

    def _only_problem(self, position, **overrides):
        problems = self._check(position, **overrides)
        self.assertEqual(len(problems), 1, problems)
        return problems[0]

    def test_sound_position_reports_nothing(self):
        self.assertEqual(self._check(self._sound()), [])

    # -- new_line ----------------------------------------------------------

    def test_new_line_missing(self):
        position = self._sound()
        del position["new_line"]
        self.assertIn("new_line is missing", self._only_problem(position))

    def test_new_line_non_integer(self):
        """A float line number survives every lookup: ``61.0`` hashes and compares equal
        to ``61``, so it passes line validation and reaches the wire as ``61.0``."""
        self.assertTrue(
            diff_api.is_line_valid(
                diff_facts({("src/edited.py", 61): 50}), "src/edited.py", 61.0
            )
        )
        position = dict(self._sound(), new_line=61.0)
        problem = self._only_problem(position, line=61.0)
        self.assertIn("new_line must be an integer", problem)

    def test_new_line_boolean(self):
        """``True`` is an ``int`` to ``isinstance`` and hashes equal to ``1``, so a
        boolean line number passes line validation and ships as JSON ``true``."""
        self.assertTrue(
            diff_api.is_line_valid(diff_facts({("f.py", 1): None}), "f.py", True)
        )
        position = _position(new_path="f.py", new_line=True, old_path="f.py")
        problem = self._only_problem(
            position,
            filepath="f.py",
            line=True,
            facts=diff_facts({("f.py", 1): None}, new_files=set(), old_paths={}),
        )
        self.assertIn("new_line must be an integer", problem)

    def test_new_line_disagrees_with_the_finding(self):
        position = dict(self._sound(), new_line=62)
        problem = self._only_problem(position)
        self.assertIn("new_line is 62, expected 61", problem)

    # -- line_code ---------------------------------------------------------

    def test_line_code_present(self):
        position = dict(self._sound(), line_code="abc_50_61")
        self.assertIn("line_code", self._only_problem(position))

    # -- old_line ----------------------------------------------------------

    def test_old_line_absent_on_a_context_line(self):
        position = self._sound()
        del position["old_line"]
        self.assertIn("old_line is missing, expected 50", self._only_problem(position))

    def test_old_line_present_on_an_added_line(self):
        """An added line has no old side; sending one anchors the comment to a line the
        old revision never had."""
        position = dict(self._sound(), new_line=62, old_line=51)
        problem = self._only_problem(
            position,
            line=62,
            facts=diff_facts(
                {("src/edited.py", 62): None},
                new_files=set(),
                old_paths={"src/edited.py": "src/edited.py"},
            ),
        )
        self.assertIn("old_line must not be sent for this position", problem)

    def test_old_line_wrong_value(self):
        position = dict(self._sound(), old_line=49)
        self.assertIn("old_line is 49, expected 50", self._only_problem(position))

    # -- old_path ----------------------------------------------------------

    def test_old_path_absent_on_a_modified_file(self):
        position = self._sound()
        del position["old_path"]
        self.assertIn(
            "old_path is missing, expected 'src/edited.py'",
            self._only_problem(position),
        )

    def test_old_path_present_on_an_added_file(self):
        position = _position(
            new_path="src/added.py",
            new_line=1,
            old_path="src/added.py",
        )
        problem = self._only_problem(
            position,
            filepath="src/added.py",
            line=1,
            facts=diff_facts(
                {("src/added.py", 1): None}, new_files={"src/added.py"}, old_paths={}
            ),
        )
        self.assertIn("old_path must not be sent for this position", problem)

    def test_old_path_carries_the_post_rename_path(self):
        """The rename class: the post-rename path is a path the old side does not
        contain, and presence alone cannot tell it from the pre-rename one."""
        parsed_facts = _parse_fixture(GL_DIFF_RENAME)
        valid_lines = parsed_facts.valid_lines
        new_files = parsed_facts.new_files
        old_paths = parsed_facts.old_paths
        position = _position(
            new_path="new_name.py",
            new_line=3,
            old_line=3,
            old_path="new_name.py",
        )
        problem = self._only_problem(
            position,
            filepath="new_name.py",
            line=3,
            facts=diff_facts(valid_lines, new_files=new_files, old_paths=old_paths),
        )
        self.assertIn("old_path is 'new_name.py', expected 'old_name.py'", problem)

    # -- the loop-invariant keys -------------------------------------------

    def test_sha_key_missing(self):
        """A dropped SHA is a guaranteed 400 that no per-finding fact can reveal — the
        position must be checked for CARRYING the fetched value, not just for the value
        being usable."""
        for key in ("base_sha", "head_sha", "start_sha"):
            with self.subTest(key=key):
                position = self._sound()
                del position[key]
                self.assertIn(f"{key} is missing", self._only_problem(position))

    def test_sha_value_disagrees_with_the_fetch(self):
        position = dict(self._sound(), head_sha="wrong")
        self.assertIn(
            "head_sha is 'wrong', expected 'head1'", self._only_problem(position)
        )

    def test_position_type_wrong(self):
        position = dict(self._sound(), position_type="txet")
        self.assertIn(
            "position_type is 'txet', expected 'text'", self._only_problem(position)
        )

    def test_new_path_disagrees_with_the_resolved_path(self):
        position = dict(self._sound(), new_path="b/src/edited.py")
        self.assertIn("new_path is 'b/src/edited.py'", self._only_problem(position))

    def test_unrecognised_key(self):
        """`line_range` is `line_code`'s sibling: both are derived server-side and both
        answer the identical 400. Nothing enumerates them — an unexpected key is a
        malformed position whether or not anyone knew to name it."""
        position = dict(self._sound(), line_range={"start": {}})
        self.assertIn("line_range must not be sent", self._only_problem(position))

    # -- no false positives ------------------------------------------------

    def test_legitimate_positions_report_nothing(self):
        """Every position kind the poster legitimately builds, against parser output."""
        contract = _parse_fixture(GL_DIFF_CONTRACT)
        rename = _parse_fixture(GL_DIFF_RENAME)
        real_a_dir = _parse_fixture(GL_DIFF_REAL_A_DIR)
        cases = [
            (
                "context line in a modified file",
                contract,
                "src/edited.py",
                61,
                _position(
                    new_path="src/edited.py",
                    new_line=61,
                    old_line=50,
                    old_path="src/edited.py",
                ),
            ),
            (
                "added line in a modified file",
                contract,
                "src/edited.py",
                62,
                _position(
                    new_path="src/edited.py",
                    new_line=62,
                    old_path="src/edited.py",
                ),
            ),
            (
                "line in a newly added file",
                contract,
                "src/app/clients/api/__init__.py",
                1,
                _position(new_path="src/app/clients/api/__init__.py", new_line=1),
            ),
            (
                "context line in a renamed file",
                rename,
                "new_name.py",
                3,
                _position(
                    new_path="new_name.py",
                    new_line=3,
                    old_line=3,
                    old_path="old_name.py",
                ),
            ),
            (
                "literal a/ directory path",
                real_a_dir,
                "a/foo.py",
                1,
                _position(
                    new_path="a/foo.py",
                    new_line=1,
                    old_line=1,
                    old_path="a/foo.py",
                ),
            ),
        ]
        for label, parsed, filepath, line, position in cases:
            valid_lines, new_files, old_paths = (
                parsed.valid_lines,
                parsed.new_files,
                parsed.old_paths,
            )
            with self.subTest(case=label):
                self.assertEqual(
                    validate_position(
                        position,
                        _MR_SHAS,
                        diff_facts(
                            valid_lines, new_files=new_files, old_paths=old_paths
                        ),
                        filepath,
                        line,
                    ),
                    [],
                )

    def test_skipped_validation_position_reports_nothing(self):
        """With no diff to consult the poster ships the finding's raw path and no
        old_line; the gate must not invent an expectation it cannot have."""
        position = _position(new_path="b/x.py", new_line=3, old_path="b/x.py")
        self.assertEqual(
            validate_position(position, _MR_SHAS, None, "b/x.py", 3),
            [],
        )


# ---------------------------------------------------------------------------
# GitLab discussion payload — new file vs modified file
# ---------------------------------------------------------------------------


class TestGitlabPositionPayload(unittest.TestCase):
    """Regression tests for GitLab's discussions API payload shape.

    GitLab returns HTTP 500 (after silently creating the discussion record,
    which then dangles as a hung thread) when a position object includes
    ``old_path`` for a file that's newly added in the MR. ``post_gitlab``
    must omit ``old_path`` for new files and include it for modified files.

    Unit level — ``valid_lines``/``new_files`` are injected. End-to-end detection from
    a real ``glab mr diff`` is pinned by ``TestGitlabPositionContract``; injecting
    ``new_files`` here is exactly why #127 D2 shipped, so this class must never be the
    only cover.
    """

    def _capture_position(self, data, facts: diff_api.DiffFacts | None):
        """Run post_gitlab and return the position dict from the discussion call."""
        from gauntlet.delivery.post import post_gitlab

        fake = FakeGitLab()
        # Git yields a full object id or "unknown", never an abbreviated marker SHA.
        with (
            patch("gauntlet.delivery.post.get_head_sha", return_value="deadbeef" * 5),
            patch(
                "gauntlet.delivery.post.fetch_gitlab_shas",
                return_value=("base", "head", "start"),
            ),
            patch(
                "gauntlet.delivery.post.gitlab_prior_delivery_state",
                return_value=PriorDelivery(False, frozenset(), frozenset(), None),
            ),
        ):
            post_gitlab(data, facts, forge=fake)

        captured = [call.request for call in fake.calls if call.method == "submit"]
        self.assertGreaterEqual(
            len(captured), 2, "expected summary + at least one discussion call"
        )
        request = captured[1]
        assert request is not None
        return request.payload["position"]

    def test_new_file_position_omits_old_path(self):
        data = {
            "owner": "o",
            "repo": "r",
            "pr_number": 1,
            "findings": [
                {"file": "src/added.py", "line": 5, "title": "Bug", "body": "x"}
            ],
        }
        valid_lines = {("src/added.py", 5): None}
        new_files = {"src/added.py"}
        position = self._capture_position(
            data, diff_facts(valid_lines, new_files=new_files)
        )
        self.assertNotIn(
            "old_path", position, "old_path must be omitted for newly-added files"
        )
        self.assertNotIn("old_line", position, "an added file's lines have no old side")
        self.assertEqual(position["new_path"], "src/added.py")
        self.assertEqual(position["new_line"], 5)

    def test_modified_file_position_includes_old_path(self):
        data = {
            "owner": "o",
            "repo": "r",
            "pr_number": 1,
            "findings": [
                {"file": "src/edited.py", "line": 10, "title": "Bug", "body": "x"}
            ],
        }
        valid_lines = {("src/edited.py", 10): 7}
        new_files = set()
        position = self._capture_position(
            data, diff_facts(valid_lines, new_files=new_files)
        )
        self.assertEqual(position["old_path"], "src/edited.py")
        self.assertEqual(position["new_path"], "src/edited.py")
        self.assertEqual(position["new_line"], 10)
        # Both sides, or GitLab answers 400 `line_code can't be blank` (#127 D1).
        self.assertEqual(position["old_line"], 7)

    def test_new_files_none_falls_back_to_modified_behavior(self):
        """If new_files is None (e.g., diff fetch failed), retain old_path.

        Better to risk a 500 on a new-file finding than to lose anchoring on
        modified-file findings — and the diff-fetch-failed path is rare.
        """
        data = {
            "owner": "o",
            "repo": "r",
            "pr_number": 1,
            "findings": [
                {"file": "src/edited.py", "line": 10, "title": "Bug", "body": "x"}
            ],
        }
        position = self._capture_position(data, None)
        self.assertEqual(position["old_path"], "src/edited.py")
        # A skipped validation has no old-side data to send, so the position degrades to
        # today's shape rather than crashing on a direct `valid_lines[...]` index.
        self.assertNotIn("old_line", position)

    def test_real_a_dir_modified_file_keeps_old_path_despite_stripped_collision(self):
        """A modified file under a real top-level `a/` directory must keep old_path
        even when an unrelated new file shares its stripped basename.

        Regression for the is_new_file stripped-prefix fallback: "a/foo.py" is
        modified (real a/ directory, GitLab verbatim spelling) while "foo.py" is a
        DIFFERENT, newly-added top-level file in the same diff. Before the fix,
        is_new_file's independent `^[ab]/` strip matched "foo.py" against new_files
        and wrongly reported the modified file as new, dropping old_path.
        """
        data = {
            "owner": "o",
            "repo": "r",
            "pr_number": 1,
            "findings": [{"file": "a/foo.py", "line": 10, "title": "Bug", "body": "x"}],
        }
        valid_lines = {("a/foo.py", 10): 7}
        new_files = {"foo.py"}
        position = self._capture_position(
            data, diff_facts(valid_lines, new_files=new_files)
        )
        self.assertEqual(position["old_path"], "a/foo.py")
        self.assertEqual(position["new_path"], "a/foo.py")

    def test_skipped_validation_ships_the_findings_raw_path(self):
        """With no diff to consult, the finding's own spelling travels untouched.

        Pre-branch main shipped the raw path here and delivery worked; an
        unconditional `^[ab]/` strip would rewrite a real `b/`-rooted path into one the
        forge does not have, and there is no parsed key left to catch the mistake.
        """
        data = {
            "owner": "o",
            "repo": "r",
            "pr_number": 1,
            "findings": [{"file": "b/x.py", "line": 3, "title": "Bug", "body": "x"}],
        }
        position = self._capture_position(data, None)
        self.assertEqual(position["new_path"], "b/x.py")
        self.assertEqual(position["old_path"], "b/x.py")


# ---------------------------------------------------------------------------
# --dry-run payload capture
# ---------------------------------------------------------------------------

# A GitHub diff (gh pr diff) that makes foo.py lines 1 (context) and 2 (added)
# valid for inline comments.
GH_DIFF = (
    "diff --git a/foo.py b/foo.py\n"
    "--- a/foo.py\n"
    "+++ b/foo.py\n"
    "@@ -1,1 +1,2 @@\n"
    " existing\n"
    "+added\n"
)

# A GitLab diff (glab mr diff) that makes bar.py lines 1 and 2 valid. The `---`/`+++`
# headers are UNPREFIXED because that is what glab emits. The leading `diff --git` line is
# NOT part of plain `glab mr diff` output (tests/fixtures/glab_diff/README.md has the
# shape and its sources); it survives here because these are delivery-path tests that do
# not turn on diff shape, and the parser ignores the line either way.
GL_DIFF = (
    "diff --git a/bar.py b/bar.py\n"
    "--- bar.py\n"
    "+++ bar.py\n"
    "@@ -1,1 +1,2 @@\n"
    " ctx\n"
    "+newline\n"
)


_GLAB_FIXTURE_DIR = os.path.join(os.path.dirname(__file__), "fixtures", "glab_diff")


def _glab_fixture(name):
    """Read one byte-exact `glab mr diff` fixture."""
    with open(os.path.join(_GLAB_FIXTURE_DIR, name), encoding="utf-8") as fh:
        return fh.read()


# A modified file followed by an added one, in the shape plain `glab mr diff` emits.
# src/edited.py: new 61 = old 50 (context), new 62 = added, new 63 = old 52 (context).
# src/app/clients/api/__init__.py: new 1..16 — added file, signalled only by `@@ -0,0`.
GL_DIFF_CONTRACT = _glab_fixture("modified.diff") + _glab_fixture("added.diff")

# A RENAMED file: the `---` header names the PRE-rename path and the `+++` header the
# post-rename one. That old-side path is what GitLab needs in `position.old_path` (#130).
# new 3 = old 3 (context), new 4 = added, new 5 = old 5 (context), new 6 = old 6 (a BLANK
# context line, which a unified diff spells as a lone space).
GL_DIFF_RENAME = _glab_fixture("rename.diff")

GL_DIFF_GIT_STYLE = _glab_fixture("git_style.diff")
GL_DIFF_GIT_STYLE_SPACES = _glab_fixture("git_style_spaces.diff")

# A glab diff for a repo with a REAL top-level `a/` directory. `glab mr diff` prints
# paths verbatim, so `a/foo.py` here is a directory named `a` — not git's synthetic
# old-side prefix. The `diff --git` decoration is not glab's (see GL_DIFF above).
# new 1 = old 1 (context), new 2 = added.
GL_DIFF_REAL_A_DIR = (
    "diff --git a/a/foo.py b/a/foo.py\n"
    "--- a/foo.py\n"
    "+++ a/foo.py\n"
    "@@ -1,2 +1,2 @@\n"
    " ctx\n"
    "-x\n"
    "+y\n"
)


GL_CONTRACT_VERSIONS = [
    {
        "base_commit_sha": "base1",
        "head_commit_sha": "head1",
        "start_commit_sha": "start1",
    }
]

# One finding on each of the three position kinds GL_DIFF_CONTRACT produces: a context
# line, an added line in a modified file, and a line in an added file.
GL_CONTRACT_FINDINGS = [
    {
        "file": "src/edited.py",
        "line": 61,
        "severity": "high",
        "title": "Context-line finding",
        "body": "Body one",
    },
    {
        "file": "src/edited.py",
        "line": 62,
        "severity": "medium",
        "title": "Added-line finding",
        "body": "Body two",
    },
    {
        "file": "src/app/clients/api/__init__.py",
        "line": 1,
        "severity": "low",
        "title": "New-file finding",
        "body": "Body three",
    },
]

_GL_CONSOLIDATION_KEY = "src/edited.py:60"


def _gl_primary(line=61):
    """The stamped primary of a consolidation group over the contract diff."""
    return dict(
        GL_CONTRACT_FINDINGS[0],
        line=line,
        consolidation_key=_GL_CONSOLIDATION_KEY,
        consolidation_primary=True,
    )


def _gl_corroborator(tag, line):
    """A stamped non-primary member of the same group as ``_gl_primary``."""
    return {
        "file": "src/edited.py",
        "line": line,
        "severity": "medium",
        "title": f"Corroborator {tag}",
        "body": f"Body corr {tag}",
        "agent": "bug-detector",
        "dimension": "correctness",
        "confidence": 70,
        "consolidation_key": _GL_CONSOLIDATION_KEY,
        "consolidation_primary": False,
    }


def _member_key(member):
    """The delivery key one group member carries, whatever shape delivers it.

    Derived from the member's OWN anchor and single-finding render — the same
    inputs the individual-discussion path uses — which is what makes a group
    discussion and an individual one interchangeable for rerun dedup.

    ``suggested_fix_code`` is dropped before the render, unconditionally, because
    a delivery key must not depend on the apply-check's verdict (#63 D2). Spelled
    out here rather than borrowed from post_review so this helper cannot agree
    with a broken implementation by construction.
    """
    material = {k: v for k, v in member.items() if k != "suggested_fix_code"}
    return post_review.finding_key(
        member["file"],
        member["line"],
        member["title"],
        post_review._finding_sections(material),
    )


# The warning test uses the CLI's actual stderr spelling.
GLAB_400_STDERR = (
    "glab: 400 Bad request - Note "
    '{:line_code=>["can\'t be blank", "must be a valid line code"]} (HTTP 400)'
)


@dataclass(frozen=True, slots=True)
class _ObservedForgeCalls:
    calls: list[ForgeCall]


@contextlib.contextmanager
def _poster_run(
    factory,
    *,
    diff="",
    versions=None,
    remote="git@github.com:o/r.git\n",
    head_sha="deadbeefcafe\n",
    note_rc=0,
    diff_rc=0,
    discussion_rcs=None,
    calls=None,
    payloads=None,
    entries=None,
):
    reply = (diff, "fatal: could not read the diff" if diff_rc else "", diff_rc)
    # A group may attempt its discussion and then each member's fallback.
    data = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    findings = data if isinstance(data, list) else data.get("findings", [])
    attempts = 2 * len(findings) + 1
    rejected = iter(discussion_rcs or [])
    success = PostResult({}, None, None)
    notes = [
        PostResult(None, "glab: 401 Unauthorized (HTTP 401)", None)
        if note_rc
        else success
    ] * attempts
    discussions = [
        PostResult(None, GLAB_400_STDERR, None) if next(rejected, 0) else success
        for _ in range(attempts)
    ]
    github = factory.configure(FakeForge(diffs=[reply]))
    gitlab = factory.configure(
        FakeGitLab(
            diffs=[reply],
            refs=[JsonFetch(versions if versions is not None else [], None)],
            submissions={"notes": notes, "discussions": discussions},
            entries=[entries] if entries is not None else None,
        )
    )
    observed = _ObservedForgeCalls(calls=[])
    with patch("gauntlet.proc.run", side_effect=_git_run(remote, head_sha)):
        try:
            yield observed
        finally:
            observed.calls.extend(github.calls + gitlab.calls)
            writes = [call for call in observed.calls if call.method == "submit"]
            if calls is not None:
                calls.extend(observed.calls)
            if payloads is not None:
                payloads.extend(call.request.payload for call in writes)


_FLAG_WRAPPER = (
    "--owner",
    "o",
    "--repo",
    "r",
    "--pr-number",
    "5",
    "--platform",
    "github",
    "--sha",
    "a" * 40,
)


def _finding(**over: object) -> dict[str, object]:
    return {
        "file": "foo.py",
        "line": 2,
        "severity": "high",
        "title": "Bug A",
        "body": "Body A",
        **over,
    }


def _review_data(**overrides: object) -> dict[str, object]:
    return {
        "owner": "o",
        "repo": "r",
        "pr_number": 5,
        "findings": [],
        **overrides,
    }


def _run_main(
    tmp_path: Path,
    factory: FakeForgeFactory,
    data: object,
    *,
    dry_run: bool = True,
    expect_exit: bool = False,
    args: tuple[str, ...] = (),
    **poster_options: object,
) -> SimpleNamespace:
    findings_path = tmp_path / "findings.json"
    findings_path.write_text(json.dumps(data), encoding="utf-8")
    argv = ["post_review.py", str(findings_path)]
    if dry_run:
        argv.append("--dry-run")
    argv.extend(args)
    stdout, stderr = io.StringIO(), io.StringIO()
    exit_code = None
    with (
        patch.object(sys, "argv", argv),
        _poster_run(factory, **poster_options) as observed,
        contextlib.redirect_stdout(stdout),
        contextlib.redirect_stderr(stderr),
    ):
        try:
            post_review.main()
        except SystemExit as exc:
            if not expect_exit:
                raise
            exit_code = exc.code
        else:
            if expect_exit:
                pytest.fail("main returned without the expected SystemExit")
    payload_path = tmp_path / "post-review-payload.json"
    payload = (
        json.loads(payload_path.read_text(encoding="utf-8"))
        if dry_run and payload_path.is_file()
        else None
    )
    calls = cast("_ObservedForgeCalls", observed)
    return SimpleNamespace(
        calls=calls.calls,
        mock_run=calls,
        out=stdout.getvalue(),
        err=stderr.getvalue(),
        exit_code=exit_code,
        payload=payload,
    )


def _git_run(remote, head_sha):
    def run(command, **kwargs):
        if command[:3] == ["git", "remote", "get-url"]:
            return proc.CompletedProcess(command, 0, remote, "")
        if command[:2] == ["git", "rev-parse"]:
            return proc.CompletedProcess(command, 0, head_sha, "")
        raise AssertionError(f"Unexpected Git call: {command}")

    return run


@pytest.mark.parametrize("expect_exit, propagated", [(False, True), (True, False)])
def test_main_runner_exit_policy(
    tmp_path: Path,
    forge_factory: FakeForgeFactory,
    monkeypatch: pytest.MonkeyPatch,
    expect_exit: bool,
    propagated: bool,
) -> None:
    def exit_main() -> None:
        raise SystemExit(7)

    monkeypatch.setattr(post_review, "main", exit_main)
    if propagated:
        with pytest.raises(SystemExit) as exc:
            _run_main(tmp_path, forge_factory, _review_data(), expect_exit=expect_exit)
        assert exc.value.code == 7
    else:
        run = _run_main(
            tmp_path, forge_factory, _review_data(), expect_exit=expect_exit
        )
        assert run.exit_code == 7


@pytest.mark.parametrize(
    "expect_exit, error",
    [(False, None), (True, "main returned without the expected SystemExit")],
)
def test_main_runner_requires_expected_exit(
    tmp_path: Path,
    forge_factory: FakeForgeFactory,
    monkeypatch: pytest.MonkeyPatch,
    expect_exit: bool,
    error: str | None,
) -> None:
    def return_main() -> None:
        return None

    monkeypatch.setattr(post_review, "main", return_main)
    if error is not None:
        with pytest.raises(pytest.fail.Exception) as exc:
            _run_main(tmp_path, forge_factory, _review_data(), expect_exit=expect_exit)
        assert str(exc.value) == error
    else:
        run = _run_main(
            tmp_path, forge_factory, _review_data(), expect_exit=expect_exit
        )
        assert run.exit_code is None


def _gitlab_posts(run, suffix):
    return [
        call
        for call in run.calls
        if call.method == "submit" and call.request.endpoint.endswith(suffix)
    ]


def _discussion_posts(mock_run):
    return _gitlab_posts(mock_run, "/discussions")


def _note_posts(mock_run):
    return _gitlab_posts(mock_run, "/notes")


class _DryRunTestBase(unittest.TestCase):
    def _write(self, data):
        with open(self.findings_path, "w", encoding="utf-8") as f:
            json.dump(data, f)

    def _payload(self):
        with open(
            os.path.join(self.tmp, "post-review-payload.json"), encoding="utf-8"
        ) as f:
            return json.load(f)


class TestInlinePosterBoundaries(_DryRunTestBase):
    """The real dry-run posters reserve their live inline envelopes."""

    SHA = "a" * 40

    def _fit_finding(self, platform, limit, finding, seed):
        trailer_bytes = len(("\n\n" + post_review.BRAND_TRAILER).encode("utf-8"))
        marker_bytes = 0
        if platform == "gitlab":
            key = finding_key_for_test(finding)
            marker_bytes = len(_delivery_marker_suffix(self.SHA, [key]).encode("utf-8"))
        target = limit - trailer_bytes - marker_bytes
        finding["body"] = seed
        fixed = len(_render_group_sections(finding, []).encode("utf-8"))
        finding["body"] += "x" * (target - fixed)
        self.assertEqual(
            len(_render_group_sections(finding, []).encode("utf-8")), target
        )

    def _run_boundary(self, platform, limit, delta, seed):
        path = "foo.py" if platform == "github" else "bar.py"
        finding = {
            "file": path,
            "line": 1,
            "severity": "high",
            "title": "Boundary finding",
            "body": "placeholder",
        }
        self._fit_finding(platform, limit, finding, seed)
        sibling = {
            "file": path,
            "line": 2,
            "severity": "low",
            "title": "Healthy sibling",
            "body": "short body",
        }
        if delta:
            finding["body"] += "x"
        data = {
            "platform": platform,
            "owner": "o",
            "repo": "r",
            "pr_number": 5,
            "sha": self.SHA,
            "review_body": "Summary",
            "findings": [finding, sibling],
        }
        diff = GH_DIFF if platform == "github" else GL_DIFF
        versions = (
            [
                {
                    "base_commit_sha": "base1",
                    "head_commit_sha": "head1",
                    "start_commit_sha": "start1",
                }
            ]
            if platform == "gitlab"
            else None
        )
        self._write(data)
        with (
            patch.object(
                sys, "argv", ["post_review.py", self.findings_path, "--dry-run"]
            ),
            _poster_run(self.forge_factory, diff=diff, versions=versions),
        ):
            post_review.main()
        return self._payload(), finding

    def test_github_exact_and_plus_one_inline_bodies_keep_the_same_comments(self):
        for delta in (0, 1):
            with self.subTest(delta=delta):
                payload, finding = self._run_boundary(
                    "github", 65536, delta, "ascii seed"
                )
                comments = payload["payload"]["comments"]
                self.assertEqual(len(comments), 2)
                self.assertEqual([c["line"] for c in comments], [1, 2])
                body = comments[0]["body"]
                self.assertIn("Boundary finding", body)
                self.assertEqual(body.count(post_review.BRAND_TRAILER), 1)
                if not delta:
                    self.assertEqual(body, render_comment_body(finding))
                else:
                    self.assertIn("_[folded:", body)
                self.assertLessEqual(len(body.encode("utf-8")), 65536)
                self.assertEqual(
                    comments[1]["body"],
                    render_comment_body(
                        {
                            "file": "foo.py",
                            "line": 2,
                            "severity": "low",
                            "title": "Healthy sibling",
                            "body": "short body",
                        }
                    ),
                )
                self.assertEqual(payload["skipped"], [])
                self.assertIn("Generated by code-gauntlet", payload["payload"]["body"])
                self.assertEqual(
                    set(payload),
                    {"platform", "endpoint", "method", "payload", "skipped"},
                )
                self.assertEqual({"path", "line", "side", "body"}, set(comments[0]))

    def test_github_multibyte_exact_and_plus_one_inline_bodies_are_bounded(self):
        for delta in (0, 1):
            with self.subTest(delta=delta):
                payload, finding = self._run_boundary("github", 65536, delta, "😀 seed")
                body = payload["payload"]["comments"][0]["body"]
                self.assertLessEqual(len(body.encode("utf-8")), 65536)
                if not delta:
                    self.assertEqual(len(body.encode("utf-8")), 65536)
                    self.assertEqual(body, render_comment_body(finding))
                else:
                    self.assertIn("Boundary finding", body)
                    self.assertIn("_[folded:", body)
                self.assertEqual(payload["skipped"], [])

    def test_gitlab_exact_and_plus_one_multibyte_discussions_preserve_payload_shape(
        self,
    ):
        for delta in (0, 1):
            with self.subTest(delta=delta):
                payload, finding = self._run_boundary(
                    "gitlab", 1000000, delta, "界 seed"
                )
                discussions = payload["discussions"]
                body = discussions[0]["body"]
                suffix = _delivery_marker_suffix(
                    self.SHA, [finding_key_for_test(finding)]
                )
                self.assertLessEqual(len((body + suffix).encode("utf-8")), 1000000)
                if not delta:
                    self.assertEqual(len((body + suffix).encode("utf-8")), 1000000)
                    self.assertEqual(body, render_comment_body(finding))
                else:
                    self.assertIn("Boundary finding", body)
                    self.assertIn("_[folded:", body)
                self.assertEqual(body.count(post_review.BRAND_TRAILER), 1)
                self.assertEqual(
                    discussions[1]["body"],
                    render_comment_body(
                        {
                            "file": "bar.py",
                            "line": 2,
                            "severity": "low",
                            "title": "Healthy sibling",
                            "body": "short body",
                        }
                    ),
                )
                self.assertEqual(payload["skipped"], [])
                self.assertEqual(
                    set(payload), {"platform", "summary", "discussions", "skipped"}
                )
                self.assertEqual(set(discussions[0]), {"body", "position"})

    def test_gitlab_exact_and_plus_one_discussions_reserve_the_live_marker(self):
        for delta in (0, 1):
            with self.subTest(delta=delta):
                payload, finding = self._run_boundary(
                    "gitlab", 1000000, delta, "ascii seed"
                )
                discussions = payload["discussions"]
                self.assertEqual(len(discussions), 2)
                self.assertEqual(
                    [d["position"]["new_line"] for d in discussions], [1, 2]
                )
                body = discussions[0]["body"]
                key = finding_key_for_test(finding)
                suffix = _delivery_marker_suffix(self.SHA, [key])
                self.assertEqual(len(suffix.encode("utf-8")), 113)
                self.assertEqual(body.count(post_review.BRAND_TRAILER), 1)
                self.assertLessEqual(len((body + suffix).encode("utf-8")), 1000000)
                if not delta:
                    self.assertEqual(len((body + suffix).encode("utf-8")), 1000000)
                else:
                    self.assertIn("_[folded:", body)
                self.assertEqual(payload["skipped"], [])
                self.assertIn("Generated by code-gauntlet", payload["summary"]["body"])

    def test_gitlab_multibyte_discussions_are_bounded_with_the_live_marker(self):
        payload, finding = self._run_boundary("gitlab", 1000000, 1, "😀 seed")
        body = payload["discussions"][0]["body"]
        suffix = _delivery_marker_suffix(self.SHA, [finding_key_for_test(finding)])
        self.assertLessEqual(len((body + suffix).encode("utf-8")), 1000000)
        self.assertIn("_[folded:", body)

    def _run_note_boundary(self, limit, delta):
        primary = _gl_primary()
        note = _gl_corroborator("Note", None)
        self._fit_finding("gitlab", limit, note, "界 seed")
        if delta:
            note["body"] += "x"
        sibling = dict(GL_CONTRACT_FINDINGS[1])
        data = {
            "platform": "gitlab",
            "owner": "o",
            "repo": "r",
            "pr_number": 5,
            "sha": self.SHA,
            "review_body": "Summary",
            "findings": [primary, note, sibling],
        }
        self._write(data)
        prior = PriorDelivery(
            False, frozenset({finding_key_for_test(primary)}), frozenset(), None
        )
        with (
            patch.object(
                sys, "argv", ["post_review.py", self.findings_path, "--dry-run"]
            ),
            patch(
                "gauntlet.delivery.post.gitlab_prior_delivery",
                return_value=prior,
            ),
            _poster_run(
                self.forge_factory, diff=GL_DIFF_CONTRACT, versions=GL_CONTRACT_VERSIONS
            ),
        ):
            post_review.main()
        return self._payload(), note

    def test_gitlab_exact_and_plus_one_multibyte_unanchored_notes_preserve_sibling(
        self,
    ):
        for delta in (0, 1):
            with self.subTest(delta=delta):
                payload, finding = self._run_note_boundary(1000000, delta)
                notes = [d for d in payload["discussions"] if "position" not in d]
                sibling = [d for d in payload["discussions"] if "position" in d]
                self.assertEqual(len(notes), 1)
                self.assertEqual(len(sibling), 1)
                body = notes[0]["body"]
                suffix = _delivery_marker_suffix(
                    self.SHA, [finding_key_for_test(finding)]
                )
                self.assertLessEqual(len((body + suffix).encode("utf-8")), 1000000)
                if not delta:
                    self.assertEqual(len((body + suffix).encode("utf-8")), 1000000)
                    self.assertEqual(body, render_comment_body(finding))
                else:
                    self.assertIn("Corroborator Note", body)
                    self.assertIn("_[folded:", body)
                self.assertEqual(body.count(post_review.BRAND_TRAILER), 1)
                self.assertEqual(
                    sibling[0]["body"], render_comment_body(GL_CONTRACT_FINDINGS[1])
                )
                self.assertEqual(payload["skipped"], [])
                self.assertEqual(
                    set(payload), {"platform", "summary", "discussions", "skipped"}
                )
                self.assertEqual(set(notes[0]), {"body"})


def finding_key_for_test(finding):
    """Compute the mirror's singleton key without depending on a captured marker."""
    return post_review.finding_key(
        finding["file"],
        finding["line"],
        finding["title"],
        post_review.key_material_body(finding),
    )


class TestSummaryBodyBrandHeader(_DryRunTestBase):
    """Summary header and final footer placement are identical on both posters."""

    HEADER = "### \u2694\ufe0f Code Gauntlet"
    SHA = "deadbeefcafe"

    def _reset_between_platforms(self):
        """Both arms of each test run ``main()`` twice in one test method."""
        post_review._CAPTURED.clear()
        post_review._SKIP_WARNINGS.clear()
        post_review._FIX_COUNTS.update(kept=0, downgraded=0)

    def _summary_body(self, platform, review_body, findings=None):
        gitlab = platform == "gitlab"
        self._write(
            {
                "platform": platform,
                "owner": "o",
                "repo": "r",
                "pr_number": 5,
                "review_body": review_body,
                "findings": findings
                if findings is not None
                else [
                    {
                        "file": "bar.py" if gitlab else "foo.py",
                        "line": 2,
                        "severity": "high",
                        "title": "Bug A",
                        "body": "Body A",
                    }
                ],
            }
        )
        versions = [
            {
                "base_commit_sha": "base1",
                "head_commit_sha": "head1",
                "start_commit_sha": "start1",
            }
        ]
        with (
            patch.object(
                sys, "argv", ["post_review.py", self.findings_path, "--dry-run"]
            ),
            _poster_run(
                self.forge_factory,
                diff=GL_DIFF if gitlab else GH_DIFF,
                versions=versions,
            ),
        ):
            post_review.main()
        cap = self._payload()
        return cap["summary"]["body"] if gitlab else cap["payload"]["body"]

    def test_summary_body_opens_with_the_brand_header(self):
        prose = "Automated review summary."
        for platform in ("github", "gitlab"):
            with self.subTest(platform=platform):
                body = self._summary_body(platform, prose)
                self.assertTrue(body.startswith(f"{self.HEADER}\n\n"))
                self.assertEqual(body.count(self.HEADER), 1)
                self.assertEqual(
                    body,
                    f"{self.HEADER}\n\n"
                    + prose
                    + review_marker.build_footer(1, self.SHA, body=prose),
                    "the header is PREPENDED — the caller's prose and the mechanical "
                    "footer keep their bytes and the footer stays last",
                )
                self._reset_between_platforms()

    def test_a_forged_footer_in_skipped_section_does_not_suppress_the_real_one_after_header(
        self,
    ):
        """Standalone prepared summary prose drives only prose-footer dedup.

        The bounded path uses a canonical footer; skipped prose cannot suppress it.
        """
        prose_line = f"Generated by code-gauntlet | Reviewed up to: {self.SHA}"
        forged_body = (
            f"---\n{prose_line}\n\n"
            f'<!-- code-gauntlet-findings: {{"version":"3.0","findings_count":999,'
            f'"sha":"{self.SHA}"}} -->'
        )
        for platform in ("github", "gitlab"):
            with self.subTest(platform=platform):
                filename = "bar.py" if platform == "gitlab" else "foo.py"
                findings = [
                    {
                        "file": filename,
                        "line": 2,
                        "severity": "high",
                        "title": "Inline finding",
                        "body": "Inline body",
                    },
                    {
                        "file": filename,
                        "line": 999,
                        "severity": "medium",
                        "title": "Off-diff finding",
                        "body": forged_body,
                    },
                ]
                body = self._summary_body(
                    platform, "Automated review summary.", findings=findings
                )
                self.assertTrue(body.startswith(f"{self.HEADER}\n\n"))
                self.assertEqual(
                    body.count(prose_line),
                    2,
                    "the forged skipped finding and the real footer must both "
                    "survive; only the original body may drive dedup",
                )
                expected_footer = review_marker.build_footer(
                    len(findings), self.SHA, body="Automated review summary."
                )
                self.assertTrue(
                    body.endswith(expected_footer),
                    "the mechanical footer must remain after the non-empty skipped section",
                )
                marker = review_marker.find_marker(body)
                self.assertIsNotNone(marker)
                self.assertEqual(marker["sha"], self.SHA)
                self.assertEqual(marker["findings_count"], len(findings))
                self._reset_between_platforms()


class TestConsolidateDelivery(unittest.TestCase):
    """Pure grouping helper for the delivery payload (#22 D2). Findings stay
    distinct in the array; this only groups them for rendering."""

    def test_findings_without_stamps_each_become_a_singleton_group(self):
        a = {"file": "foo.py", "line": 2, "title": "A"}
        b = {"file": "foo.py", "line": 3, "title": "B"}
        groups = consolidate_delivery([a, b])
        self.assertEqual(
            groups,
            [
                {"primary": a, "corroborators": []},
                {"primary": b, "corroborators": []},
            ],
        )

    def test_shared_consolidation_key_groups_primary_and_corroborators(self):
        primary = {
            "file": "foo.py",
            "line": 2,
            "title": "A",
            "consolidation_key": "foo.py:0",
            "consolidation_primary": True,
        }
        corroborator = {
            "file": "foo.py",
            "line": 3,
            "title": "B",
            "consolidation_key": "foo.py:0",
            "consolidation_primary": False,
        }
        groups = consolidate_delivery([primary, corroborator])
        self.assertEqual(
            groups, [{"primary": primary, "corroborators": [corroborator]}]
        )

    def test_group_position_is_the_primarys_first_occurrence(self):
        """A group occupies the array position of its FIRST member, whichever
        that is — order stays deterministic even when the primary is not the
        first element carrying the key."""
        corroborator = {
            "title": "B",
            "consolidation_key": "k",
            "consolidation_primary": False,
        }
        other = {"title": "C"}
        primary = {
            "title": "A",
            "consolidation_key": "k",
            "consolidation_primary": True,
        }
        groups = consolidate_delivery([corroborator, other, primary])
        self.assertEqual(
            groups,
            [
                {"primary": primary, "corroborators": [corroborator]},
                {"primary": other, "corroborators": []},
            ],
        )

    def test_multiple_corroborators_preserve_relative_order(self):
        primary = {
            "title": "A",
            "consolidation_key": "k",
            "consolidation_primary": True,
        }
        c1 = {"title": "B", "consolidation_key": "k", "consolidation_primary": False}
        c2 = {"title": "C", "consolidation_key": "k", "consolidation_primary": False}
        groups = consolidate_delivery([primary, c1, c2])
        self.assertEqual(groups[0]["corroborators"], [c1, c2])

    def test_distinct_keys_produce_distinct_groups(self):
        a = {"title": "A", "consolidation_key": "k1", "consolidation_primary": True}
        b = {"title": "B", "consolidation_key": "k2", "consolidation_primary": True}
        groups = consolidate_delivery([a, b])
        self.assertEqual(len(groups), 2)

    def test_second_primary_in_a_group_is_demoted_not_dropped(self):
        p1 = {"title": "A", "consolidation_key": "k", "consolidation_primary": True}
        c1 = {"title": "B", "consolidation_key": "k", "consolidation_primary": False}
        p2 = {"title": "C", "consolidation_key": "k", "consolidation_primary": True}
        groups = consolidate_delivery([p1, c1, p2])
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["primary"], p1)
        self.assertEqual(groups[0]["corroborators"], [c1, p2])
        all_findings = [groups[0]["primary"]] + groups[0]["corroborators"]
        self.assertEqual(len(all_findings), 3)


class TestRenderGroupBody(unittest.TestCase):
    def test_no_corroborators_is_byte_identical_to_render_comment_body(self):
        finding = {
            "file": "foo.py",
            "line": 2,
            "severity": "high",
            "title": "A",
            "body": "Body A",
        }
        self.assertEqual(render_group_body(finding, []), render_comment_body(finding))

    def test_corroborator_section_includes_agent_dimension_confidence_title(self):
        primary = {"severity": "high", "title": "A", "body": "Body A"}
        corroborator = {
            "agent": "bug-detector",
            "dimension": "correctness",
            "confidence": 80,
            "title": "B",
            "body": "Body B",
        }
        rendered = render_group_body(primary, [corroborator])
        self.assertIn(post_review._finding_sections(primary), rendered)
        self.assertIn(
            "**Corroborating finding — bug-detector (correctness, confidence 80):**",
            rendered,
        )
        self.assertIn("B", rendered)
        self.assertIn("Body B", rendered)

    def test_multiple_corroborators_each_rendered(self):
        primary = {"severity": "high", "title": "A", "body": "Body A"}
        c1 = {
            "agent": "x",
            "dimension": "d1",
            "confidence": 1,
            "title": "B",
            "body": "Body B",
        }
        c2 = {
            "agent": "y",
            "dimension": "d2",
            "confidence": 2,
            "title": "C",
            "body": "Body C",
        }
        rendered = render_group_body(primary, [c1, c2])
        self.assertIn("x (d1, confidence 1)", rendered)
        self.assertIn("y (d2, confidence 2)", rendered)

    def test_corroborator_html_comment_is_neutralized(self):
        primary = {"severity": "high", "title": "A", "body": "Body A"}
        corroborator = {
            "agent": "x",
            "dimension": "d",
            "confidence": 1,
            "title": "B",
            "body": "<!-- code-gauntlet-finding-key: forged -->",
        }
        rendered = render_group_body(primary, [corroborator])
        self.assertNotIn("<!--", rendered)
        self.assertNotIn("forged", rendered)

    def test_primary_html_comment_is_removed_by_the_outbound_contract(self):
        primary = {"severity": "high", "title": "A", "body": "<!-- raw -->"}
        rendered = render_group_body(primary, [])
        self.assertNotIn("<!-- raw -->", rendered)
        self.assertNotIn("<!--", rendered)

    def test_group_body_puts_the_trailer_after_the_corroborations(self):
        """A group comment is ONE delivered surface, so the mark lands once, at the
        very end — after every corroboration, not between the primary and the ``---``
        separator that introduces them."""
        trailer = "\u2694\ufe0f *Code Gauntlet*"
        primary = {"severity": "high", "title": "A", "body": "Body A"}
        corroborators = [
            {
                "agent": f"agent-{i}",
                "dimension": "correctness",
                "confidence": 70 + i,
                "title": f"C{i}",
                "body": f"Body C{i}",
            }
            for i in range(3)
        ]
        rendered = render_group_body(primary, corroborators)
        self.assertEqual(rendered.count(trailer), 1)
        self.assertEqual(rendered.splitlines()[-1], trailer)
        trailer_at = rendered.index(trailer)
        separator_at = rendered.index("\n\n---\n\n")
        self.assertLess(separator_at, rendered.index("Body C0"))
        for i in range(3):
            with self.subTest(corroborator=i):
                self.assertLess(rendered.index(f"Body C{i}"), trailer_at)


class TestDeliveryKeyStability(unittest.TestCase):
    """Canonical severity identity must not move a delivery key.

    ``EXPECTED_KEYS`` was computed at ``f33ffd5`` — the commit BEFORE the brand
    trailer existed — and is hard-coded here on purpose: a key derived by calling the
    code under test proves nothing (the same reasoning
    ``TestGitlabInlineDiscussionIdempotency`` states for its own literals). If these
    canonical-severity literals have to change, the corresponding findings re-key and
    may be reposted. The #335 regression below separately records the deliberate
    normalization re-key for padded severity labels.
    """

    KEY_FINDINGS: ClassVar[list[dict]] = [
        {
            "file": "src/alpha.py",
            "line": 10,
            "title": "Unchecked index",
            "body": "The loop reads one past the end.",
            "severity": "high",
        },
        {
            "file": "src/beta.py",
            "line": 22,
            "title": "Rule violation",
            "body": "This bypasses the documented gate.",
            "severity": "medium",
            "suggestion": "Call the gate instead of inlining the check.",
            "claude_md_rule": "Always route through the gate.",
        },
        {
            "file": "src/gamma.py",
            "line": 3,
            "title": "Off-by-one",
            "body": "Range end is exclusive.",
            "severity": "low",
            "suggested_fix_code": "-for i in range(n + 1):\n+for i in range(n):\n",
        },
    ]
    EXPECTED_KEYS: ClassVar[list[str]] = [
        "c6dbc10300a69daf",
        "0c5f42fa8a664b7f",
        "0cf7f5fb032bd562",
    ]

    def test_delivery_keys_are_unchanged_by_the_identity_trailer(self):
        keys = [
            post_review.finding_key(
                f["file"], f["line"], f["title"], post_review.key_material_body(f)
            )
            for f in self.KEY_FINDINGS
        ]
        self.assertEqual(keys, self.EXPECTED_KEYS)
        self.assertEqual(len(set(keys)), 3, "the three findings must not collide")

    def test_key_material_carries_no_identity_bytes(self):
        """Structural, not suffix-stripping: the key material is the SECTIONS, so no
        part of the mark can reach it however the trailer is later composed."""
        for f in self.KEY_FINDINGS:
            with self.subTest(title=f["title"]):
                self.assertNotIn("\u2694", post_review.key_material_body(f))
                self.assertNotIn("Code Gauntlet", post_review.key_material_body(f))

    def test_key_material_uses_the_unbranded_sections_seam(self):
        """Branding must stay structurally outside the bytes that keys hash."""
        finding = {
            "file": "src/example.py",
            "line": 8,
            "title": "Example",
            "body": "Body",
            "suggested_fix_code": "patch",
        }
        expected_material = dict(finding)
        del expected_material["suggested_fix_code"]
        with (
            patch.object(
                post_review, "_finding_sections", return_value="SECTIONS"
            ) as sections,
            patch.object(
                post_review, "render_comment_body", return_value="BRANDED"
            ) as branded,
        ):
            self.assertEqual(post_review.key_material_body(finding), "SECTIONS")
        sections.assert_called_once_with(expected_material)
        branded.assert_not_called()

    def test_rule_source_does_not_change_key_material(self):
        finding = {
            "file": "src/example.py",
            "line": 8,
            "title": "Example",
            "body": "Body",
            "claude_md_rule": "The cited rule.",
        }
        grounded = {**finding, "rule_source": "repo_precedent"}
        self.assertEqual(
            post_review.key_material_body(finding),
            post_review.key_material_body(grounded),
        )
        self.assertIn("**Cited rule:**", post_review.key_material_body(grounded))

    def test_key_material_severity_labels_are_closed_and_total(self):
        """Key material uses the same fixed normalized sections as the public body."""
        for case_id, raw, label, glyph in _severity_matrix():
            with self.subTest(case=case_id):
                finding = {
                    "title": "Thing",
                    "body": "desc",
                    "suggested_fix_code": "patch",
                    "rule_source": "repo_precedent",
                }
                if raw is not _MISSING_SEVERITY:
                    finding["severity"] = raw
                before = copy.deepcopy(finding)
                expected = f"**{glyph} [{label}] Thing**\n\ndesc"
                material = post_review.key_material_body(finding)
                if case_id == "oversized":
                    self.assertLess(len(material.encode("utf-8")), 256)
                    self.assertNotIn("s" * 1000, material)
                self.assertEqual(material, expected)
                self.assertEqual(finding, before)

    def test_case_stays_stable_and_padded_severity_intentionally_rekeys(self):
        """Canonical case variants keep their pins while #335 re-keys padded labels."""
        expected_body = (
            "**\U0001f7e0 [HIGH] Unchecked index**\n\nThe loop reads one past the end."
        )
        OLD_LITERAL = (
            "**\U0001f4a1 [ HIGH ] Unchecked index**\n\n"
            "The loop reads one past the end."
        )
        old_padded_key = hashlib.sha256(
            "\x00".join(
                (
                    "src/alpha.py",
                    "10",
                    "Unchecked index",
                    OLD_LITERAL,
                )
            ).encode("utf-8")
        ).hexdigest()[:16]
        for severity in ("high", "HIGH", "High", " high "):
            with self.subTest(severity=severity):
                finding = dict(self.KEY_FINDINGS[0], severity=severity)
                self.assertEqual(post_review.key_material_body(finding), expected_body)
                key = post_review.finding_key(
                    finding["file"],
                    finding["line"],
                    finding["title"],
                    post_review.key_material_body(finding),
                )
                self.assertEqual(key, self.EXPECTED_KEYS[0])
                if severity == " high ":
                    self.assertNotEqual(key, old_padded_key)


class TestDryRunStdout(_DryRunTestBase):
    """In dry-run, the post paths must not claim anything was posted."""

    def test_gitlab_dry_run_stdout_has_no_posted_claim(self):
        run = _run_main(
            Path(self.tmp),
            self.forge_factory,
            {
                "platform": "gitlab",
                "owner": "o",
                "repo": "r",
                "pr_number": 5,
                "review_body": "MR review",
                "findings": [
                    {
                        "file": "bar.py",
                        "line": 2,
                        "severity": "medium",
                        "title": "Issue X",
                        "body": "Desc X",
                    }
                ],
            },
            diff=GL_DIFF,
            versions=GL_CONTRACT_VERSIONS,
        )
        out = run.out
        self.assertNotIn("note posted.", out)
        self.assertNotIn("discussion(s) posted.", out)
        self.assertIn("MR summary note captured (dry-run).", out)
        self.assertIn("inline discussion(s) captured.", out)


# ---------------------------------------------------------------------------
# Wrapper and manual wrap produce byte-identical dry-run payloads
# ---------------------------------------------------------------------------


def test_wrapper_and_manual_wrap_produce_byte_identical_payloads(
    tmp_path: Path, forge_factory: FakeForgeFactory
) -> None:
    findings = [
        {
            "file": "foo.py",
            "line": 2,
            "severity": "high",
            "title": "Bug A",
            "body": "Body A",
        },
        {
            "file": "foo.py",
            "line": 1,
            "end_line": 2,
            "severity": "low",
            "title": "Bug B",
            "body": "Body B",
        },
    ]

    manual = {
        "owner": "o",
        "repo": "r",
        "pr_number": 5,
        "sha": "0123456789abcdef0123456789abcdef01234567",
        "review_body": "Summary",
        "findings": findings,
    }
    wrapper = {
        "owner": "o",
        "repo": "r",
        "pr_number": 5,
        "sha": "0123456789abcdef0123456789abcdef01234567",
        "platform": "github",
        "review_body": "Summary",
        "findings": findings,
    }
    manual_run = _run_main(tmp_path, forge_factory, manual, diff=GH_DIFF)
    assert manual_run.exit_code is None
    payload_path = tmp_path / "post-review-payload.json"
    manual_bytes = payload_path.read_bytes()
    # Each run must produce its own bytes, even if a future runner tolerates an exit.
    payload_path.unlink()
    wrapper_run = _run_main(tmp_path, forge_factory, wrapper, diff=GH_DIFF)
    assert wrapper_run.exit_code is None
    wrapper_bytes = payload_path.read_bytes()
    assert manual_bytes == wrapper_bytes


# ---------------------------------------------------------------------------
# GitLab position contract, end-to-end from a real `glab mr diff` (issue #127)
# ---------------------------------------------------------------------------


class TestGitlabPositionContract(_DryRunTestBase):
    """The position payload as the REAL parser produces it.

    ``TestGitlabPositionPayload`` injects ``valid_lines``/``new_files`` and so could
    never have caught #127 D2 (glab-style added-file detection) — the fixture supplied
    the answer the parser was failing to compute. This class drives ``main()`` in
    dry-run against ``GL_DIFF_CONTRACT``, so ``fetch_diff_facts`` feeds ``post_gitlab``
    and every asserted key is one the production chain actually emitted.
    """

    def setUp(self):
        super().setUp()
        self._write(
            {
                "platform": "gitlab",
                "owner": "o",
                "repo": "r",
                "pr_number": 5,
                "review_body": "MR review",
                "findings": GL_CONTRACT_FINDINGS,
            }
        )
        with (
            patch.object(
                sys, "argv", ["post_review.py", self.findings_path, "--dry-run"]
            ),
            _poster_run(
                self.forge_factory, diff=GL_DIFF_CONTRACT, versions=GL_CONTRACT_VERSIONS
            ),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            post_review.main()

    def _positions(self):
        return [d["position"] for d in self._payload()["discussions"]]

    def test_context_line_position_carries_the_correct_old_line(self):
        position = self._positions()[0]
        self.assertEqual(position["new_line"], 61)
        self.assertEqual(position["old_line"], 50)
        self.assertEqual(position["old_path"], "src/edited.py")

    def test_added_line_position_omits_old_line(self):
        position = self._positions()[1]
        self.assertEqual(position["new_line"], 62)
        self.assertNotIn("old_line", position)
        # The FILE is still modified, so old_path stays.
        self.assertEqual(position["old_path"], "src/edited.py")

    def test_line_code_never_appears_anywhere_in_the_gitlab_payload(self):
        """``line_code`` is derived server-side. Both documented attempts to compute it
        client-side (a position sibling, and inside ``line_range``) reproduced the
        identical 400 — one scan covers positions, bodies and the summary note."""
        self.assertNotIn("line_code", json.dumps(self._payload()))

    def test_new_line_is_always_sent(self):
        for position in self._positions():
            self.assertIsInstance(position["new_line"], int)


@pytest.mark.parametrize(
    ("diff", "filepath", "line", "expected"),
    [
        (
            GL_DIFF_RENAME,
            "new_name.py",
            3,
            {"old_path": "old_name.py", "old_line": 3},
        ),
        (GL_DIFF_RENAME, "new_name.py", 4, {"old_path": "old_name.py"}),
        (
            GL_DIFF_GIT_STYLE,
            "new_name.py",
            3,
            {"old_path": "old_name.py", "old_line": 3},
        ),
        (GL_DIFF_GIT_STYLE, "new_name.py", 2, {"old_path": "old_name.py"}),
        (GL_DIFF_GIT_STYLE_SPACES, "new name.py", 2, {"old_path": "old name.py"}),
        (GL_DIFF_CONTRACT, "src/app/clients/api/__init__.py", 1, {}),
        (GL_DIFF_GIT_STYLE, "src/added.py", 1, {}),
        (GL_DIFF_GIT_STYLE_SPACES, "my file.py", 1, {}),
    ],
    ids=[
        "plain-rename-context-line",
        "plain-rename-added-line",
        "git-style-rename-context-line",
        "git-style-rename-added-line",
        "git-style-rename-with-spaces",
        "plain-added-file",
        "git-style-added-file",
        "git-style-added-file-with-space",
    ],
)
def test_gitlab_position_old_side_comes_from_the_parsed_diff(
    tmp_path, forge_factory, diff, filepath, line, expected
):
    """GitLab needs a renamed file's PRE-rename path in ``old_path`` and answers HTTP
    500 to any ``old_path`` on an added file. Both facts exist only in the diff."""
    finding = {
        "file": filepath,
        "line": line,
        "severity": "high",
        "title": "Finding",
        "body": "Body",
    }
    run = _run_main(
        tmp_path,
        forge_factory,
        {
            "platform": "gitlab",
            "owner": "o",
            "repo": "r",
            "pr_number": 5,
            "review_body": "Review",
            "findings": [finding],
        },
        diff=diff,
        versions=GL_CONTRACT_VERSIONS,
    )
    payload = run.payload

    (discussion,) = payload["discussions"]
    position = discussion["position"]
    assert {
        key: position[key] for key in ("old_path", "old_line") if key in position
    } == expected
    assert position["new_path"] == filepath
    assert position["new_line"] == line


def _skipped_group(line, members):
    """A consolidation group that cannot anchor: *members* is ``(id, title)`` pairs,
    primary first, ``id`` omitted when ``None``."""
    findings = []
    for index, (finding_id, title) in enumerate(members):
        finding = {
            "file": "missing.py",
            "line": line,
            "severity": "high",
            "title": title,
            "body": "body",
            "consolidation_key": "same-location",
        }
        if finding_id is not None:
            finding["id"] = finding_id
        if index == 0:
            finding["consolidation_primary"] = True
        findings.append(finding)
    return findings


_SKIP_REASONS = {
    "no-line": (None, "Finding 'Primary' has no line number — skipping."),
    "line-not-in-diff": (
        99,
        "Skipping finding 'Primary' at missing.py:99 — line not found in diff."
        " Valid lines for this file: []",
    ),
}


@pytest.mark.parametrize("reason", sorted(_SKIP_REASONS))
@pytest.mark.parametrize(
    ("platform", "diff"),
    [("github", GH_DIFF), ("gitlab", GL_DIFF_RENAME)],
    ids=["github", "gitlab"],
)
def test_skipped_group_warning_names_every_member(
    tmp_path, forge_factory, platform, diff, reason
):
    line, message = _SKIP_REASONS[reason]
    members = [("finding-1", "Primary"), ("finding-2", "Corroborator")]

    run = _run_main(
        tmp_path,
        forge_factory,
        {
            "platform": platform,
            "owner": "o",
            "repo": "r",
            "pr_number": 5,
            "review_body": "Review",
            "findings": _skipped_group(line, members),
        },
        diff=diff,
        versions=GL_CONTRACT_VERSIONS,
    )
    payload = run.payload

    assert payload["skipped"] == [f"{message} [group members: finding-1, finding-2]"]


@pytest.mark.parametrize(
    ("members", "suffix"),
    [
        ([("finding-1", "Primary")], ""),
        (
            [(None, "Primary"), ("finding-2", "Corroborator")],
            " [group members: Primary, finding-2]",
        ),
    ],
    ids=["single-member-is-unsuffixed", "id-less-member-falls-back-to-its-title"],
)
def test_skipped_group_warning_member_labels(tmp_path, forge_factory, members, suffix):
    line, message = _SKIP_REASONS["line-not-in-diff"]

    run = _run_main(
        tmp_path,
        forge_factory,
        {
            "platform": "gitlab",
            "owner": "o",
            "repo": "r",
            "pr_number": 5,
            "review_body": "Review",
            "findings": _skipped_group(line, members),
        },
        diff=GL_DIFF_RENAME,
        versions=GL_CONTRACT_VERSIONS,
    )
    payload = run.payload

    assert payload["skipped"] == [message + suffix]


# ---------------------------------------------------------------------------
# GitLab delivery outcomes, end-to-end through main()
# ---------------------------------------------------------------------------


class _GitlabLiveRunBase(_DryRunTestBase):
    """Share input snapshots across position, failure and retry tests."""

    def _run_main(
        self,
        dry_run=False,
        findings=None,
        prior=None,
        sha="a" * 40,
        versions=None,
        **fake_run_kwargs,
    ):
        data = {
            "platform": "gitlab",
            "owner": "o",
            "repo": "r",
            "pr_number": 5,
            "review_body": "MR review",
            "findings": GL_CONTRACT_FINDINGS if findings is None else findings,
        }
        # sha=None omits the key entirely, so resolve_marker_sha falls through to
        # `git rev-parse HEAD` — the only way to reach its "unknown" outcome.
        if sha is not None:
            data["sha"] = sha
        return _run_main(
            Path(self.tmp),
            self.forge_factory,
            data,
            dry_run=dry_run,
            diff=GL_DIFF_CONTRACT,
            versions=GL_CONTRACT_VERSIONS if versions is None else versions,
            entries=prior_notes(prior, sha or "a" * 40),
            **fake_run_kwargs,
        )


class TestGitlabPositionGate(_GitlabLiveRunBase):
    """The gate on the real delivery path: same call in both modes, no exit before the
    dry-run payload is on disk."""

    def test_validate_position_is_called_once_per_delivered_finding(self):
        """With the guards intact, deleting the call site changes not one byte of the
        output — a call count is the only thing that catches its removal."""
        real = post_review.validate_position
        calls = []

        def spy(*args):
            calls.append(args)
            return real(*args)

        with patch("gauntlet.delivery.post.validate_position", side_effect=spy):
            run = self._run_main(dry_run=True)
        self.assertIsNone(run.exit_code)
        self.assertEqual(len(calls), len(GL_CONTRACT_FINDINGS))
        self.assertEqual(len(self._payload()["discussions"]), 3)

    def test_empty_sha_dies_before_the_summary_note(self):
        """A loop-invariant config failure is reported once, before anything reaches the
        MR — not as one rejection per finding after the note is already on it."""
        run = self._run_main(
            versions=[
                {
                    "base_commit_sha": "base1",
                    "head_commit_sha": "",
                    "start_commit_sha": "start1",
                }
            ],
            expect_exit=True,
        )
        self.assertEqual(run.exit_code, 1)
        self.assertEqual(_note_posts(run.mock_run), [])
        self.assertEqual(_discussion_posts(run.mock_run), [])
        self.assertIn("head_sha", run.err)

    def test_dry_run_malformed_position_exits_one_and_still_writes_the_payload(self):
        """A float line number reaches the position dict unchanged — the exact class of
        payload that used to be reported as "captured" and then 400 on the live run."""
        findings = [dict(GL_CONTRACT_FINDINGS[0], line=61.0)]
        run = self._run_main(dry_run=True, findings=findings, expect_exit=True)
        self.assertEqual(run.exit_code, 1)
        self.assertIn("malformed GitLab position", run.err)
        self.assertIn("new_line must be an integer", run.err)
        self.assertIn("  1 finding(s) had a malformed position", run.out)
        # Its own counter and its own line: the skip counter's meaning is pinned
        # elsewhere and must not absorb this.
        self.assertNotIn("finding(s) skipped.", run.out)
        self.assertIn("Dry run — no comments posted", run.out)
        payload = self._payload()
        self.assertEqual(payload["discussions"], [])
        self.assertTrue(
            any("malformed GitLab position" in w for w in payload["skipped"]),
            "the payload must say why the finding is absent from it",
        )

    def test_live_malformed_position_is_never_sent(self):
        """Live, a malformed position is a per-finding loss like a rejection: the sound
        findings still land, and the run stays a success with warnings. Inline
        discussions have no idempotency key, so exiting non-zero on a PARTIAL delivery
        would invite the rerun that double-posts everything that already landed."""
        findings = [dict(GL_CONTRACT_FINDINGS[0], line=61.0), GL_CONTRACT_FINDINGS[1]]
        run = self._run_main(findings=findings)
        self.assertIsNone(run.exit_code)
        posts = _discussion_posts(run.mock_run)
        self.assertEqual(len(posts), 1, "only the sound position may be posted")
        self.assertIn("  1 inline discussion(s) posted.", run.out)
        self.assertIn("  1 finding(s) had a malformed position", run.out)

    def test_malformed_position_loss_counts_the_whole_group_not_just_the_primary(self):
        """A consolidation group's loss counters must reflect every finding in the
        group, not just the primary that anchors the position — posted + skipped +
        invalid + failed + already_present must sum to the total finding count.

        The primary's loss is its own (1 invalid); each corroborator then falls
        back to its own individual discussion and is counted on its own merits.
        """
        primary = _gl_primary(line=61.0)  # malformed -> validate_position rejects
        run = self._run_main(
            dry_run=True,
            findings=[primary, _gl_corroborator("A", 61), _gl_corroborator("B", 61)],
            expect_exit=True,
        )
        # Dry-run reports a malformed position as a non-zero exit (pinned above);
        # what changed is the COUNT — only the primary is lost to it.
        self.assertEqual(run.exit_code, 1)
        self.assertIn("  1 finding(s) had a malformed position", run.out)
        self.assertIn("  2 inline discussion(s) captured.", run.out)

    def test_group_fallback_partial_position_failure(self):
        """A malformed primary position must not take its validated corroborators
        down with it — they fall back to their own individual discussions."""
        run = self._run_main(
            findings=[
                _gl_primary(line=61.0),
                _gl_corroborator("A", 61),
                _gl_corroborator("B", 62),
            ]
        )
        self.assertIsNone(run.exit_code)
        self.assertIn("  2 inline discussion(s) posted.", run.out)
        self.assertIn("  1 finding(s) had a malformed position", run.out)
        posts = _discussion_posts(run.mock_run)
        self.assertEqual(len(posts), 2, "one individual discussion per corroborator")

    def test_group_fallback_partial_post_failure(self):
        """A rejected GROUP discussion falls back to one individual discussion per
        corroborator: the primary counts 1 failed, the corroborators post on their
        own."""
        payloads = []
        run = self._run_main(
            findings=[
                _gl_primary(),
                _gl_corroborator("A", 61),
                _gl_corroborator("B", 62),
            ],
            discussion_rcs=[1, 0, 0],
            payloads=payloads,
        )
        self.assertIsNone(run.exit_code)
        self.assertIn("  2 inline discussion(s) posted.", run.out)
        self.assertIn("  1 inline discussion(s) not delivered", run.out)
        posts = _discussion_posts(run.mock_run)
        self.assertEqual(
            len(posts), 3, "one rejected group attempt + one POST per corroborator"
        )
        bodies = [p["body"] for p in payloads if "position" in p]
        self.assertEqual(len(bodies), 3)
        # The two fallback discussions each carry ONE finding, rendered by the
        # single-finding renderer — not the group body.
        self.assertIn("Corroborating finding", bodies[0])
        self.assertNotIn("Corroborating finding", bodies[1])
        self.assertNotIn("Corroborating finding", bodies[2])
        self.assertIn("Corroborator A", bodies[1])
        self.assertIn("Corroborator B", bodies[2])

    def test_group_fallback_total_failure(self):
        """When the primary AND every corroborator lose on their own merits, the
        losses still sum to the group size — nothing vanishes."""
        run = self._run_main(
            findings=[
                _gl_primary(line=61.0),  # invalid
                _gl_corroborator("A", 61),  # rejected below -> failed
                _gl_corroborator("B", 999),  # not in the diff -> invalid
            ],
            discussion_rcs=[1],
            expect_exit=True,
        )
        self.assertEqual(run.exit_code, 1)
        self.assertIn("  0 inline discussion(s) posted.", run.out)
        self.assertIn("  2 finding(s) had a malformed position", run.out)
        self.assertIn("  1 inline discussion(s) not delivered", run.out)

    def test_group_success_counts_every_member_as_posted(self):
        """One discussion carries the whole group, so the posted counter reports the
        group size, not 1."""
        run = self._run_main(findings=[_gl_primary(), _gl_corroborator("A", 61)])
        self.assertIsNone(run.exit_code)
        self.assertEqual(len(_discussion_posts(run.mock_run)), 1)
        self.assertIn("  2 inline discussion(s) posted.", run.out)

    def test_group_already_delivered_counts_every_member(self):
        """A group whose single discussion is already on the MR counts all of its
        findings as already-present — one discussion, group_size findings."""
        primary = _gl_primary()
        corroborator = _gl_corroborator("A", 61)
        run = self._run_main(
            findings=[primary, corroborator],
            prior=PriorDelivery(
                True,
                frozenset({_member_key(primary), _member_key(corroborator)}),
                frozenset(),
                None,
            ),
        )
        self.assertIsNone(run.exit_code)
        self.assertEqual(_discussion_posts(run.mock_run), [])
        self.assertIn(
            "  2 inline discussion(s) already on the MR from an earlier run", run.out
        )

    def test_group_rerun_after_full_delivery_posts_nothing(self):
        """Every member's key is what a delivered group leaves behind, so a rerun
        recognizes all of them and issues no discussion POST at all."""
        primary = _gl_primary()
        corrs = [_gl_corroborator("A", 61), _gl_corroborator("B", 62)]
        keys = {_member_key(m) for m in [primary, *corrs]}
        run = self._run_main(
            findings=[primary, *corrs],
            prior=PriorDelivery(True, frozenset(keys), frozenset(), None),
        )
        self.assertIsNone(run.exit_code)
        self.assertEqual(len(_discussion_posts(run.mock_run)), 0)
        self.assertIn(
            "  3 inline discussion(s) already on the MR from an earlier run", run.out
        )

    def test_large_group_rerun_after_full_delivery_posts_nothing(self):
        """The live writer and reader must round-trip every member of a large group."""
        primary = _gl_primary()
        corrs = [_gl_corroborator(str(i), 61) for i in range(39)]
        # One member has no anchor of its own, so the group body is the only place
        # its content and key can land; the assertions below prove they did.
        corrs[0]["line"] = None
        members = [primary, *corrs]
        expected_keys = {_member_key(member) for member in members}
        payloads = []

        first = self._run_main(findings=members, payloads=payloads)
        self.assertIsNone(first.exit_code)
        self.assertEqual(len(_note_posts(first.mock_run)), 1)
        self.assertEqual(len(_discussion_posts(first.mock_run)), 1)
        discussion_body = next(
            payload["body"] for payload in payloads if "position" in payload
        )
        self.assertIn(corrs[0]["body"], discussion_body)
        discussion_markers = review_marker.find_finding_markers(discussion_body)
        self.assertEqual(len(discussion_markers), 40)
        self.assertEqual(
            {marker["key"] for marker in discussion_markers}, expected_keys
        )

        entries = [{"body": payload["body"]} for payload in payloads]
        state = detect_prior_review.gitlab_prior_delivery_state(
            "o", "r", 5, "a" * 40, forge=FakeGitLab(entries=[JsonFetch(entries, None)])
        )
        self.assertTrue(state.summary_posted)
        self.assertEqual(state.finding_keys, expected_keys)
        self.assertEqual(len(state.finding_keys), 40)
        self.assertEqual(state.legacy_group_keys, set())
        self.assertIsNone(state.error)

        rerun = self._run_main(findings=members, prior=state)
        self.assertIsNone(rerun.exit_code)
        self.assertEqual(_discussion_posts(rerun.mock_run), [])
        self.assertEqual(_note_posts(rerun.mock_run), [])
        self.assertIn("  0 inline discussion(s) posted.", rerun.out)
        self.assertIn(
            "  40 inline discussion(s) already on the MR from an earlier run",
            rerun.out,
        )

    def test_group_partial_prior_delivery_posts_only_missing_members(self):
        """A prior run's fallback landed the corroborators individually. Reposting the
        GROUP would duplicate them, so only the missing primary is delivered — on its
        own, with the single-finding body."""
        primary = _gl_primary()
        corrs = [_gl_corroborator("A", 61), _gl_corroborator("B", 62)]
        payloads = []
        run = self._run_main(
            findings=[primary, *corrs],
            prior=PriorDelivery(
                True, frozenset(_member_key(c) for c in corrs), frozenset(), None
            ),
            payloads=payloads,
        )
        self.assertIsNone(run.exit_code)
        posts = _discussion_posts(run.mock_run)
        self.assertEqual(len(posts), 1, "only the undelivered primary may be posted")
        self.assertIn("  1 inline discussion(s) posted.", run.out)
        self.assertIn(
            "  2 inline discussion(s) already on the MR from an earlier run", run.out
        )
        body = next(p["body"] for p in payloads if "position" in p)
        self.assertNotIn("Corroborating finding", body)
        self.assertIn(
            post_review.build_finding_marker("a" * 40, _member_key(primary)), body
        )

    def test_group_body_carries_a_marker_for_every_member(self):
        """The group's single discussion is the delivery record for all of its
        findings, so it carries one finding-key marker per member — a later rerun
        matches each member individually, whatever shape delivered it."""
        primary = _gl_primary()
        corrs = [_gl_corroborator("A", 61), _gl_corroborator("B", 62)]
        payloads = []
        run = self._run_main(findings=[primary, *corrs], payloads=payloads)
        self.assertIsNone(run.exit_code)
        body = next(p["body"] for p in payloads if "position" in p)
        for member in [primary, *corrs]:
            self.assertIn(
                post_review.build_finding_marker("a" * 40, _member_key(member)),
                body,
                f"missing marker for {member['title']}",
            )

    def test_folded_body_marker_uses_the_unfolded_key_material_and_reruns_cleanly(self):
        # Mutation: derive the marker key from the folded body; the reader must still
        # recognize the delivery key computed from the original finding sections.
        finding = dict(GL_CONTRACT_FINDINGS[0], body="x" * 999900)
        expected_key = _member_key(finding)
        payloads = []
        run = self._run_main(findings=[finding], payloads=payloads)
        self.assertIsNone(run.exit_code)
        discussion = next(p for p in payloads if "position" in p)
        self.assertIn("_[folded:", discussion["body"])
        self.assertEqual(
            review_marker.find_finding_marker(discussion["body"]),
            {"sha": "a" * 40, "key": expected_key},
        )
        rerun = self._run_main(
            findings=[finding],
            prior=PriorDelivery(True, frozenset({expected_key}), frozenset(), None),
        )
        self.assertIsNone(rerun.exit_code)
        self.assertEqual(_discussion_posts(rerun.mock_run), [])

    def test_group_body_carries_a_marker_for_unanchorable_corroborator_too(self):
        """A corroborator with no line of its own can only ever be delivered inside
        the group body — its marker must be there too, or a rerun can never
        recognize it as delivered (unanchored corroborators lost on rerun)."""
        primary = _gl_primary()
        unanchored = _gl_corroborator("A", None)
        payloads = []
        run = self._run_main(findings=[primary, unanchored], payloads=payloads)
        self.assertIsNone(run.exit_code)
        body = next(p["body"] for p in payloads if "position" in p)
        self.assertIn(
            post_review.build_finding_marker("a" * 40, _member_key(unanchored)),
            body,
            "the unanchorable corroborator's marker must round-trip through the "
            "group body",
        )

    def test_rerun_recognizes_unanchorable_corroborator_from_group_body(self):
        """Given the round-trip above, a rerun that sees both markers on the MR
        must treat the WHOLE group — unanchorable member included — as already
        delivered, and post nothing new."""
        primary = _gl_primary()
        unanchored = _gl_corroborator("A", None)
        keys = {_member_key(primary), _member_key(unanchored)}
        run = self._run_main(
            findings=[primary, unanchored],
            prior=PriorDelivery(True, frozenset(keys), frozenset(), None),
        )
        self.assertIsNone(run.exit_code)
        self.assertEqual(_discussion_posts(run.mock_run), [])
        self.assertIn(
            "  2 inline discussion(s) already on the MR from an earlier run",
            run.out,
        )

    def test_unanchorable_corroborator_delivered_when_siblings_already_posted(self):
        """The primary was delivered individually by an earlier fallback run; the
        unanchorable corroborator was not (it has no anchor of its own, so it
        never got its own fallback discussion). It must still reach the MR —
        as a position-less note — and count toward delivery rather than being
        silently dropped as `already_present`."""
        primary = _gl_primary()
        unanchored = _gl_corroborator("A", None)
        payloads = []
        run = self._run_main(
            findings=[primary, unanchored],
            prior=PriorDelivery(
                True, frozenset({_member_key(primary)}), frozenset(), None
            ),
            payloads=payloads,
        )
        self.assertIsNone(run.exit_code)
        note_bodies = [p["body"] for p in payloads if "position" not in p]
        self.assertTrue(
            any("Corroborator A" in b for b in note_bodies),
            "the unanchorable corroborator's content must land on the MR "
            "somewhere, not vanish",
        )
        self.assertIn(
            post_review.build_finding_marker("a" * 40, _member_key(unanchored)),
            "".join(note_bodies),
        )
        note = next(body for body in note_bodies if "Corroborator A" in body)
        marker = post_review.build_finding_marker("a" * 40, _member_key(unanchored))
        self.assertEqual(note.count(post_review.BRAND_TRAILER), 1)
        self.assertTrue(
            note[: note.index(marker)].rstrip().endswith(post_review.BRAND_TRAILER),
            "the position-less note must end with the identity trailer before its marker",
        )
        self.assertIn("  1 inline discussion(s) already on the MR", run.out)
        self.assertIn("  1 inline discussion(s) posted.", run.out)

    def test_unanchored_note_boundary_reserves_its_live_marker(self):
        # Mutation: omit the marker reserve at the unanchored-note render site; the
        # plus-one body then incorrectly takes the fast path and remains unbounded.
        for delta in (0, 1):
            with self.subTest(delta=delta):
                primary = _gl_primary()
                unanchored = _gl_corroborator("A", None)
                target = 1000000 - 24 - 113
                fixed_finding = dict(unanchored, body="")
                fixed = len(
                    post_review._finding_sections(fixed_finding).encode("utf-8")
                )
                unanchored["body"] = "x" * (target - fixed + delta)
                payloads = []
                run = self._run_main(
                    findings=[primary, unanchored, GL_CONTRACT_FINDINGS[1]],
                    prior=PriorDelivery(
                        True, frozenset({_member_key(primary)}), frozenset(), None
                    ),
                    payloads=payloads,
                )
                self.assertIsNone(run.exit_code)
                note = next(
                    p["body"]
                    for p in payloads
                    if "position" not in p and "Corroborator A" in p["body"]
                )
                key = _member_key(unanchored)
                suffix = _delivery_marker_suffix("a" * 40, [key])
                self.assertEqual(len(suffix.encode("utf-8")), 113)
                self.assertLessEqual(len(note.encode("utf-8")), 1000000)
                if not delta:
                    self.assertEqual(len(note.encode("utf-8")), 1000000)
                else:
                    self.assertIn("_[folded:", note)
                self.assertEqual(note.count(post_review.BRAND_TRAILER), 1)
                if delta:
                    self.assertIn("Inline body folded by", run.err)
                else:
                    self.assertNotIn("Inline body folded by", run.err)

    def test_unanchored_note_over_limit_is_not_posted_and_does_not_strand_siblings(
        self,
    ):
        """An over-limit fallback note is failed through the live delivery loop.

        The note-only run must die after the summary note, while a healthy
        sibling still posts and keeps the batch successful. The note limit is
        below the live marker reserve, so even its folded envelope cannot fit.

        Mutation: remove the ``_inline_body_over_limit`` call from
        ``deliver_unanchored`` — RED because the over-limit note is then posted.
        """
        primary = _gl_primary()
        unanchored = _gl_corroborator("A", None)
        primary_key = _member_key(primary)

        cases = [
            ("note only", [primary, unanchored], True),
            (
                "healthy sibling",
                [primary, unanchored, dict(GL_CONTRACT_FINDINGS[1], title="Healthy")],
                False,
            ),
        ]
        for label, findings, expect_failure in cases:
            with self.subTest(label=label):
                payloads = []
                with patch.dict(
                    fold.PLATFORM_BODY_LIMITS["gitlab"]["surfaces"]["note"],
                    {"bytes": 100},
                ):
                    run = self._run_main(
                        findings=findings,
                        prior=PriorDelivery(
                            True, frozenset({primary_key}), frozenset(), None
                        ),
                        payloads=payloads,
                        expect_exit=expect_failure,
                    )

                note_bodies = [p["body"] for p in payloads if "position" not in p]
                self.assertFalse(
                    any("Corroborator A" in body for body in note_bodies),
                    "the over-limit corroborator note must not reach the POST",
                )
                self.assertIn("corroborator note", run.err)
                self.assertIn("100-byte GitLab body limit", run.err)
                self.assertIn("1 inline discussion(s) not delivered", run.out)
                if expect_failure:
                    self.assertEqual(run.exit_code, 1)
                    self.assertIn("0 inline discussion(s) posted", run.out)
                else:
                    self.assertIsNone(run.exit_code)
                    healthy_posts = [
                        p
                        for p in payloads
                        if "position" in p and "Healthy" in p["body"]
                    ]
                    self.assertEqual(len(healthy_posts), 1)
                    self.assertIn("1 inline discussion(s) posted", run.out)

    def test_legacy_group_body_rerun_posts_nothing_and_counts_the_whole_group(self):
        """A pre-#208 group body already carries the unanchorable corroborator's
        CONTENT (rendered into its corroboration section) even though it never
        carried that corroborator's KEY. A rerun must recognize the primary's
        key landing in such a body as proof the whole group is delivered — not
        attempt to `deliver_unanchored` a duplicate (Bugbot: rerun duplicates
        prior group content)."""
        primary = _gl_primary()
        unanchored = _gl_corroborator("A", None)
        payloads = []
        run = self._run_main(
            findings=[primary, unanchored],
            # legacy_group_keys carries the primary's key: detect_prior_review
            # decided this is a legacy under-marked group body. delivered_keys
            # carries ONLY the primary's key too — the unanchorable member's
            # key was never written by the pre-fix code that posted this note.
            prior=PriorDelivery(
                True,
                frozenset({_member_key(primary)}),
                frozenset({_member_key(primary)}),
                None,
            ),
            payloads=payloads,
        )
        self.assertIsNone(run.exit_code)
        self.assertEqual(_discussion_posts(run.mock_run), [])
        note_bodies = [p["body"] for p in payloads if "position" not in p]
        self.assertFalse(
            any("Corroborator A" in b for b in note_bodies),
            "the legacy group body already carries this content — posting it "
            "again would duplicate it on the MR",
        )
        self.assertIn(
            "  2 inline discussion(s) already on the MR from an earlier run",
            run.out,
        )

    def test_non_legacy_partial_delivery_still_delivers_the_missing_member(self):
        """Pin that legacy-group detection does NOT over-fire: an ordinary
        partial delivery (anchored siblings posted individually, no legacy
        group body involved) must still deliver the member that was left
        behind, exactly as before."""
        primary = _gl_primary()
        unanchored = _gl_corroborator("A", None)
        payloads = []
        run = self._run_main(
            findings=[primary, unanchored],
            prior=PriorDelivery(
                True, frozenset({_member_key(primary)}), frozenset(), None
            ),
            payloads=payloads,
        )
        self.assertIsNone(run.exit_code)
        note_bodies = [p["body"] for p in payloads if "position" not in p]
        self.assertTrue(
            any("Corroborator A" in b for b in note_bodies),
            "with no legacy group body on the MR, the missing member must "
            "still be delivered",
        )

    def test_live_all_malformed_exits_one_with_nothing_posted(self):
        findings = [dict(f, line=float(f["line"])) for f in GL_CONTRACT_FINDINGS]
        run = self._run_main(findings=findings, expect_exit=True)
        self.assertEqual(run.exit_code, 1)
        self.assertEqual(_discussion_posts(run.mock_run), [])
        self.assertIn("  0 inline discussion(s) posted.", run.out)
        self.assertIn("nothing new was posted inline", run.err)


# ---------------------------------------------------------------------------
# GitLab per-finding fault tolerance (issue #127 D3)
# ---------------------------------------------------------------------------


class TestGitlabFaultTolerance(_GitlabLiveRunBase):
    """A single rejected position must not strand the findings behind it.

    The summary note is posted FIRST, so aborting mid-loop left partial,
    non-retryable state on the MR. The loop now warns, counts and continues; the run
    only exits non-zero when nothing NEW reached the MR and something was rejected.
    """

    def test_one_rejection_does_not_abort_the_remaining_findings(self):
        run = self._run_main(discussion_rcs=[1, 0, 0])
        self.assertIsNone(
            run.exit_code, "a partial delivery is a success with warnings"
        )
        self.assertEqual(len(_discussion_posts(run.mock_run)), 3)
        self.assertIn("  2 inline discussion(s) posted.", run.out)
        self.assertIn("  1 inline discussion(s) not delivered", run.out)

    def test_rejection_warning_names_the_finding_and_the_api_error(self):
        run = self._run_main(discussion_rcs=[1, 0, 0])
        self.assertIn("Context-line finding", run.err)
        self.assertIn("src/edited.py:61", run.err)
        self.assertIn("line_code", run.err)

    def test_all_rejected_exits_non_zero_after_attempting_every_finding(self):
        run = self._run_main(discussion_rcs=[1, 1, 1], expect_exit=True)
        self.assertEqual(run.exit_code, 1)
        # The exit is a REPORT, not an abort: every finding was attempted first.
        self.assertEqual(len(_discussion_posts(run.mock_run)), 3)
        self.assertIn("  0 inline discussion(s) posted.", run.out)
        self.assertIn("  3 inline discussion(s) not delivered", run.out)

    def test_zero_posted_with_no_rejections_exits_zero(self):
        off_diff = [dict(f, line=999) for f in GL_CONTRACT_FINDINGS]
        run = self._run_main(findings=off_diff)
        self.assertIsNone(run.exit_code)
        self.assertIn("  0 inline discussion(s) posted.", run.out)
        self.assertIn("  3 finding(s) skipped.", run.out)
        self.assertNotIn("rejected", run.out)

    def test_summary_note_failure_is_still_fatal(self):
        run = self._run_main(note_rc=1, expect_exit=True)
        self.assertEqual(run.exit_code, 1)
        self.assertEqual(
            _discussion_posts(run.mock_run),
            [],
            "auth/MR failure dooms every inline post behind it — do not attempt them",
        )

    def test_impossible_discussion_is_failed_without_stranding_healthy_sibling(self):
        # Mutation: bypass the final UTF-8 body-plus-marker check; the oversized
        # discussion would then reach try_post_json and the wire.
        oversized = dict(GL_CONTRACT_FINDINGS[0], title="Too large", body="x" * 1000)
        healthy = dict(GL_CONTRACT_FINDINGS[1], title="Healthy sibling")
        payloads = []
        with patch.dict(
            fold.PLATFORM_BODY_LIMITS["gitlab"]["surfaces"]["discussion"],
            {"bytes": 200},
        ):
            run = self._run_main(
                findings=[oversized, healthy],
                payloads=payloads,
            )
        self.assertIsNone(run.exit_code)
        discussions = [p for p in payloads if "position" in p]
        self.assertEqual(len(discussions), 1)
        self.assertIn("Healthy sibling", discussions[0]["body"])
        self.assertNotIn("Too large", discussions[0]["body"])
        self.assertIn("  1 inline discussion(s) posted.", run.out)
        self.assertIn("  1 inline discussion(s) not delivered", run.out)
        self.assertIn("200-byte GitLab body limit", run.err)

    def test_dry_run_never_reports_rejections(self):
        run = self._run_main(dry_run=True, discussion_rcs=[1, 1, 1])
        self.assertIsNone(run.exit_code)
        self.assertIn("  3 inline discussion(s) captured.", run.out)
        self.assertNotIn("rejected", run.out)
        self.assertEqual(len(self._payload()["discussions"]), 3)


# ---------------------------------------------------------------------------
# GitLab summary-note idempotency (issue #127 D4)
# ---------------------------------------------------------------------------


class TestGitlabSummaryIdempotency(_DryRunTestBase):
    """A partial-delivery retry reads one snapshot and preserves its summary."""

    def _run_main(self, prior, data=None, dry_run=False, head_sha="deadbeefcafe\n"):
        if data is None:
            data = {
                "platform": "gitlab",
                "owner": "o",
                "repo": "r",
                "pr_number": 5,
                "sha": "a" * 40,
                "review_body": "MR review",
                "findings": GL_CONTRACT_FINDINGS,
            }
        return _run_main(
            Path(self.tmp),
            self.forge_factory,
            data,
            dry_run=dry_run,
            diff=GL_DIFF_CONTRACT,
            versions=GL_CONTRACT_VERSIONS,
            head_sha=head_sha,
            entries=prior_notes(prior, "a" * 40),
        )

    def test_summary_skipped_when_this_shas_marker_is_already_on_the_mr(self):
        run = self._run_main(prior=PriorDelivery(True, frozenset(), frozenset(), None))
        self.assertEqual(_note_posts(run.mock_run), [])
        # The retry still delivers the inline comments — that is the whole point.
        self.assertEqual(len(_discussion_posts(run.mock_run)), 3)
        self.assertIn("already on the MR", run.out)
        self.assertNotIn("MR summary note posted.", run.out)
        self.assertEqual(
            [c for c in run.mock_run.calls if c.method == "review_entries"],
            [ForgeCall("review_entries", ReviewTarget("o", "r", 5))],
        )

    def test_summary_posted_when_the_marker_records_a_different_sha(self):
        run = self._run_main(prior=PriorDelivery(False, frozenset(), frozenset(), None))
        self.assertEqual(len(_note_posts(run.mock_run)), 1)
        self.assertIn("MR summary note posted.", run.out)

    def test_one_fetch_serves_both_idempotency_checks(self):
        """Two reads could observe summary and discussions from different snapshots."""
        run = self._run_main(prior=PriorDelivery(True, frozenset(), frozenset(), None))
        self.assertEqual(
            len([c for c in run.mock_run.calls if c.method == "review_entries"]), 1
        )

    def test_notes_fetch_failure_degrades_to_posting(self):
        run = self._run_main(
            prior=PriorDelivery(
                False,
                frozenset(),
                frozenset(),
                "gitlab notes: fetch failed (exit 1): boom",
            )
        )
        self.assertEqual(len(_note_posts(run.mock_run)), 1)
        self.assertIn("could not check for an existing summary note", run.err)
        self.assertIn("boom", run.err)

    def test_unresolvable_sha_skips_the_check_and_posts(self):
        """get_head_sha's "unknown" fallback is not a usable dedup key."""
        run = self._run_main(
            prior=PriorDelivery(True, frozenset(), frozenset(), None),
            data={
                "platform": "gitlab",
                "owner": "o",
                "repo": "r",
                "pr_number": 5,
                "review_body": "MR review",
                "findings": GL_CONTRACT_FINDINGS,
            },
            head_sha="unknown\n",
        )
        self.assertEqual(
            [c for c in run.mock_run.calls if c.method == "review_entries"], []
        )
        self.assertEqual(len(_note_posts(run.mock_run)), 1)

    def test_summary_check_delegates_to_the_reader_module(self):
        """post_review must not grow its own parse of the signals it writes."""
        self.assertEqual(
            post_review.gitlab_prior_delivery_state.__module__,
            "gauntlet.prior_review",
        )


# ---------------------------------------------------------------------------
# GitLab per-finding delivery idempotency (issue #132)
# ---------------------------------------------------------------------------


class TestGitlabInlineDiscussionIdempotency(_GitlabLiveRunBase):
    """A rerun after a partial delivery must not duplicate the inline discussions
    that DID land — issue #132, the half issue #127 D4 left open for the summary.

    The expected keys below are LITERAL constants, computed once and hardcoded.
    Deriving them in the assertions by calling ``post_review.finding_key`` would make
    every test here agree with the implementation by construction — including a
    broken implementation that keys every finding identically.
    """

    # GL_CONTRACT_FINDINGS, in order: src/edited.py:61, src/edited.py:62, and line 1 of
    # the added file, whose path is whatever `added.diff` records — a key is over the
    # path, so re-recording that fixture re-pins these literals.
    CONTEXT_LINE_KEY = "f87d51ec25846a5e"
    ADDED_LINE_KEY = "ee15b1fc2a6db296"
    NEW_FILE_KEY = "a9cb7253f6710b82"
    ALL_KEYS: ClassVar[set[str]] = {CONTEXT_LINE_KEY, ADDED_LINE_KEY, NEW_FILE_KEY}

    @staticmethod
    def _discussion_payloads(payloads):
        return [p for p in payloads if "position" in p]

    def test_keys_match_their_pinned_literals(self):
        """The tautology guard itself: the derivation must reproduce hardcoded
        values, and the three findings must not collide onto one key."""
        keys = [
            post_review.finding_key(
                f["file"], f["line"], f["title"], post_review.key_material_body(f)
            )
            for f in GL_CONTRACT_FINDINGS
        ]
        self.assertEqual(
            keys, [self.CONTEXT_LINE_KEY, self.ADDED_LINE_KEY, self.NEW_FILE_KEY]
        )

    def test_a_finding_already_on_the_mr_is_not_reposted(self):
        payloads = []
        run = self._run_main(
            prior=PriorDelivery(
                True, frozenset({self.CONTEXT_LINE_KEY}), frozenset(), None
            ),
            payloads=payloads,
        )
        self.assertIsNone(run.exit_code)
        self.assertEqual(len(_discussion_posts(run.mock_run)), 2)
        bodies = [p["body"] for p in self._discussion_payloads(payloads)]
        self.assertEqual(len(bodies), 2)
        self.assertNotIn(
            "Context-line finding",
            "\n".join(bodies),
            "the finding whose key is already on the MR must not be reposted",
        )
        self.assertIn("  2 inline discussion(s) posted.", run.out)
        self.assertIn("1 inline discussion(s) already on the MR", run.out)
        self.assertNotIn("rejected", run.out)

    def test_rerun_with_everything_already_present_posts_nothing_and_exits_zero(self):
        run = self._run_main(
            prior=PriorDelivery(True, frozenset(set(self.ALL_KEYS)), frozenset(), None)
        )
        self.assertIsNone(run.exit_code, "a fully-delivered rerun is a success")
        self.assertEqual(_discussion_posts(run.mock_run), [])
        self.assertEqual(_note_posts(run.mock_run), [])
        self.assertIn("  0 inline discussion(s) posted.", run.out)
        self.assertIn("3 inline discussion(s) already on the MR", run.out)

    def test_already_present_fold_does_not_emit_a_fold_notice(self):
        finding = dict(GL_CONTRACT_FINDINGS[0], body="x" * 1000001)
        key = finding_key_for_test(finding)
        run = self._run_main(
            findings=[finding],
            prior=PriorDelivery(True, frozenset({key}), frozenset(), None),
        )
        self.assertIsNone(run.exit_code)
        self.assertEqual(_discussion_posts(run.mock_run), [])
        self.assertNotIn("Inline body folded by", run.err)

    def test_already_present_plus_one_rejection_fails_honestly(self):
        """The bare ``posted == 0`` die used to call this "all 1 inline discussion(s)
        were rejected — nothing was posted inline", which was wrong twice: one
        rejection out of three findings is not "all", and two of this review's
        discussions ARE on the MR."""
        run = self._run_main(
            prior=PriorDelivery(
                True,
                frozenset({self.CONTEXT_LINE_KEY, self.ADDED_LINE_KEY}),
                frozenset(),
                None,
            ),
            discussion_rcs=[1],
            expect_exit=True,
        )
        self.assertEqual(run.exit_code, 1)
        self.assertIn("attempted this run were not delivered", run.err)
        self.assertIn("2 from an earlier run remain on the MR", run.err)
        self.assertNotIn("nothing was posted inline", run.err)

    def test_already_present_plus_one_malformed_fails_honestly(self):
        """The malformed-position exit reports the same outcome as the rejection exit
        above — nothing NEW landed — so it owes the operator the same true statement
        about what an earlier run left standing. A malformed position is caught before
        the wire, so it is never one of the "attempted" discussions."""
        run = self._run_main(
            findings=[
                GL_CONTRACT_FINDINGS[0],
                GL_CONTRACT_FINDINGS[1],
                dict(GL_CONTRACT_FINDINGS[2], line=1.0),
            ],
            prior=PriorDelivery(
                True,
                frozenset({self.CONTEXT_LINE_KEY, self.ADDED_LINE_KEY}),
                frozenset(),
                None,
            ),
            expect_exit=True,
        )
        self.assertEqual(run.exit_code, 1)
        self.assertEqual(_discussion_posts(run.mock_run), [])
        self.assertIn("had a malformed position", run.err)
        self.assertIn("2 from an earlier run remain on the MR", run.err)
        self.assertNotIn("nothing was posted inline", run.err)

    def test_malformed_position_emits_no_fold_notice(self):
        # Mutation: report the fold before the malformed-position return; the
        # notice then describes a discussion this run never put on the wire.
        run = self._run_main(
            findings=[
                GL_CONTRACT_FINDINGS[0],
                GL_CONTRACT_FINDINGS[1],
                dict(GL_CONTRACT_FINDINGS[2], line=1.0, body="x" * 1000001),
            ],
            prior=PriorDelivery(
                True,
                frozenset({self.CONTEXT_LINE_KEY, self.ADDED_LINE_KEY}),
                frozenset(),
                None,
            ),
            expect_exit=True,
        )
        self.assertEqual(run.exit_code, 1)
        self.assertEqual(_discussion_posts(run.mock_run), [])
        self.assertIn("had a malformed position", run.err)
        self.assertNotIn("Inline body folded by", run.err)

    def test_fetch_failure_delivers_every_finding(self):
        """Availability over dedup: a failed read must never be taken for "already
        delivered" — a possible duplicate beats a silently dropped review."""
        run = self._run_main(
            prior=PriorDelivery(
                False,
                frozenset(),
                frozenset(),
                "gitlab notes: fetch failed (exit 1): boom",
            )
        )
        self.assertIsNone(run.exit_code)
        self.assertEqual(len(_discussion_posts(run.mock_run)), 3)
        self.assertEqual(len(_note_posts(run.mock_run)), 1)
        self.assertIn("could not check for an existing summary note", run.err)
        self.assertIn("boom", run.err)

    def test_live_posted_bodies_carry_a_marker_the_reader_recovers(self):
        """The round trip that makes the rerun possible: what the live wire carries
        must parse back to the same key, through the real writer and real reader."""
        payloads = []
        run = self._run_main(payloads=payloads)
        self.assertIsNone(run.exit_code)
        bodies = [p["body"] for p in self._discussion_payloads(payloads)]
        self.assertEqual(len(bodies), 3)
        for body, finding, key in zip(
            bodies,
            GL_CONTRACT_FINDINGS,
            (self.CONTEXT_LINE_KEY, self.ADDED_LINE_KEY, self.NEW_FILE_KEY),
            strict=True,
        ):
            self.assertTrue(
                body.startswith(render_comment_body(finding)),
                "the marker is appended — the rendered comment is untouched",
            )
            self.assertEqual(
                review_marker.find_finding_marker(body),
                {"sha": "a" * 40, "key": key},
            )

    def test_key_is_derived_from_the_diff_spelling_not_the_raw_finding_path(self):
        """The key must be built from the path the position ships. A finding whose
        raw path needs resolving must land on the SAME key as its resolved twin —
        otherwise the marker written on the wire and the marker a rerun looks up
        drift apart the moment a `b/`-prefixed path appears."""
        payloads = []
        prefixed = dict(GL_CONTRACT_FINDINGS[0], file="b/src/edited.py")
        run = self._run_main(findings=[prefixed], payloads=payloads)
        self.assertIsNone(run.exit_code)
        discussion = self._discussion_payloads(payloads)[0]
        self.assertEqual(discussion["position"]["new_path"], "src/edited.py")
        self.assertEqual(
            review_marker.find_finding_marker(discussion["body"]),
            {"sha": "a" * 40, "key": self.CONTEXT_LINE_KEY},
        )

    def test_an_unmarkable_sha_posts_without_writing_an_unreadable_marker(self):
        """`git rev-parse` failing yields "unknown", which find_finding_marker is
        guaranteed to reject. Delivery still happens — it just carries no marker,
        rather than a permanent one nothing can read."""
        payloads = []
        run = self._run_main(sha=None, head_sha="unknown\n", payloads=payloads)
        self.assertIsNone(run.exit_code)
        self.assertEqual(
            [c for c in run.mock_run.calls if c.method == "review_entries"], []
        )
        bodies = [p["body"] for p in self._discussion_payloads(payloads)]
        self.assertEqual(len(bodies), 3)
        for body, finding in zip(bodies, GL_CONTRACT_FINDINGS, strict=True):
            self.assertEqual(body, render_comment_body(finding))

    def test_real_dry_and_live_gitlab_bodies_differ_only_by_all_markers(self):
        # Mutations: reserve markers only outside DRY_RUN, reserve only the first
        # key, or omit the note-path reserve; each live/dry comparison turns red.
        limit = 500

        def pad_finding(finding, target):
            fixed = len(post_review._finding_sections(finding).encode("utf-8"))
            finding["body"] += "x" * (target - fixed)

        singleton = dict(GL_CONTRACT_FINDINGS[0], body="界 seed")
        pad_finding(singleton, limit - 24 - 113 + 1)

        primary = _gl_primary()
        corroborator = _gl_corroborator("A", 61)
        group_fixed = len(
            _render_group_sections(primary, [corroborator]).encode("utf-8")
        )
        primary["body"] += "x" * (limit - 24 - 225 + 1 - group_fixed)

        note_primary = _gl_primary()
        note = _gl_corroborator("Note", None)
        pad_finding(note, limit - 24 - 113 + 1)
        cases = [
            ([singleton], [finding_key_for_test(singleton)], None),
            (
                [primary, corroborator],
                [finding_key_for_test(primary), _member_key(corroborator)],
                None,
            ),
            (
                [note_primary, note],
                [_member_key(note)],
                _member_key(note_primary),
            ),
        ]
        for findings, keys, prior_key in cases:
            with self.subTest(keys=keys):
                dry_prior = (
                    PriorDelivery(False, frozenset({prior_key}), frozenset(), None)
                    if prior_key
                    else PriorDelivery(False, frozenset(), frozenset(), None)
                )
                with (
                    patch(
                        "gauntlet.delivery.post.gitlab_prior_delivery",
                        return_value=dry_prior,
                    ),
                    patch.dict(
                        fold.PLATFORM_BODY_LIMITS["gitlab"]["surfaces"]["discussion"],
                        {"bytes": limit},
                    ),
                    patch.dict(
                        fold.PLATFORM_BODY_LIMITS["gitlab"]["surfaces"]["note"],
                        {"bytes": limit},
                    ),
                ):
                    self._run_main(dry_run=True, findings=findings)
                dry_discussions = self._payload()["discussions"]
                dry = next(
                    d
                    for d in dry_discussions
                    if ("position" in d) == (prior_key is None)
                )

                live_payloads = []
                live_prior = (
                    PriorDelivery(False, frozenset({prior_key}), frozenset(), None)
                    if prior_key
                    else None
                )
                with (
                    patch.dict(
                        fold.PLATFORM_BODY_LIMITS["gitlab"]["surfaces"]["discussion"],
                        {"bytes": limit},
                    ),
                    patch.dict(
                        fold.PLATFORM_BODY_LIMITS["gitlab"]["surfaces"]["note"],
                        {"bytes": limit},
                    ),
                ):
                    run = self._run_main(
                        findings=findings,
                        prior=live_prior,
                        payloads=live_payloads,
                    )
                self.assertIsNone(run.exit_code)
                live_discussions = [
                    p
                    for p in live_payloads
                    if "position" in p or p["body"].find("Corroborator Note") >= 0
                ]
                self.assertEqual(len(live_discussions), 1)
                live = live_discussions[0]
                suffix = _delivery_marker_suffix("a" * 40, keys)
                self.assertEqual(
                    len(suffix.encode("utf-8")), 113 if len(keys) == 1 else 225
                )
                self.assertEqual(live["body"], dry["body"] + suffix)
                self.assertLessEqual(len(live["body"].encode("utf-8")), limit)

    def test_dry_run_fetches_nothing_captures_everything_and_stays_marker_free(self):
        """bench pins dry-run and scores the captured bodies as candidate text, so a
        marker in a capture would change what is scored. The capture must also ignore
        the dedup state entirely — every finding is captured, none deduped away."""
        run = self._run_main(
            dry_run=True,
            prior=PriorDelivery(True, frozenset(set(self.ALL_KEYS)), frozenset(), None),
        )
        self.assertEqual(
            [c for c in run.mock_run.calls if c.method == "review_entries"], []
        )
        captured = self._payload()
        self.assertEqual(len(captured["discussions"]), 3)
        for disc, finding in zip(
            captured["discussions"], GL_CONTRACT_FINDINGS, strict=True
        ):
            self.assertEqual(disc["body"], render_comment_body(finding))
        self.assertNotIn(
            review_marker.FINDING_MARKER_TOKEN,
            json.dumps(captured),
            "no dry-run capture may carry the delivery marker",
        )


# ---------------------------------------------------------------------------
# Issue #192 — skipped findings degrade into the review body, they are never
# silently dropped.
# ---------------------------------------------------------------------------


class TestBuildSkippedSection(unittest.TestCase):
    def test_empty_list_returns_empty_string(self):
        self.assertEqual(build_skipped_section([], 0), "")

    def test_renders_location_title_and_both_counts(self):
        finding = {
            "file": "src/app.py",
            "line": 216,
            "severity": "high",
            "title": "SQL injection risk",
            "body": "Untrusted input reaches the query.",
        }
        section = build_skipped_section([("src/app.py", 216, finding)], 4)
        self.assertIn("### ⚠️ 1 finding could not be anchored inline", section)
        self.assertIn("4 inline comments were posted", section)
        self.assertIn("following 1 finding", section)
        self.assertIn("`src/app.py:216`", section)
        self.assertIn("SQL injection risk", section)
        self.assertIn(post_review._finding_sections(finding), section)

    def test_no_line_finding_renders_bare_path(self):
        finding = {"file": "src/app.py", "title": "No line", "body": "b"}
        section = build_skipped_section([("src/app.py", None, finding)], 0)
        self.assertIn("`src/app.py`", section)
        self.assertNotIn("src/app.py:None", section)

    def test_reuses_render_comment_body_for_redaction(self):
        """The section must go through the SAME sanitize/redact path as an inline
        comment — a second rendering path is exactly the drift this guards against."""
        finding = {
            "file": "src/app.py",
            "line": 5,
            "title": "Leaked token",
            "body": "b",
            "suggestion": "Rotate the token: ghp_" + "a" * 36,
        }
        section = build_skipped_section([("src/app.py", 5, finding)], 0)
        self.assertIn("[REDACTED]", section)
        self.assertNotIn("ghp_" + "a" * 36, section)

    def test_skipped_section_entries_carry_no_trailer(self):
        """The section rides INSIDE the summary comment, whose body already carries
        the brand header. One mark per delivered surface means zero here — four
        entries must contribute zero trailers, not four."""
        trailer = "\u2694\ufe0f *Code Gauntlet*"
        entries = [
            (
                "src/app.py",
                10 + i,
                {"severity": "high", "title": f"T{i}", "body": f"B{i}"},
            )
            for i in range(4)
        ]
        section = build_skipped_section(entries, 4)
        for i in range(4):
            self.assertIn(f"T{i}", section)
        self.assertEqual(section.count(trailer), 0)

    def test_composition_separates_prose_from_skipped_section(self):
        # Mutation: restore the skipped section's leading newline or the old direct
        # interpolation; this exact fragment boundary turns red with a setext heading.
        finding = {"file": "src/app.py", "line": 9, "title": "Skipped", "body": "b"}
        body = compose_review_body(
            "prose",
            [[("src/app.py", 9, finding)]],
            platform="github",
            findings_count=1,
            sha="abc1234",
            inline_count=0,
        ).body
        self.assertIn(
            "prose\n\n---\n\n### ⚠️ 1 finding could not be anchored inline", body
        )
        self.assertNotIn("prose\n\n\n---", body)

    def test_bounded_composition_keeps_the_frame_separated(self):
        # Mutation: join the bounded frame with one newline; the exact prose/frame
        # boundary turns red even when the fast path test still passes.
        finding = {
            "file": "src/app.py",
            "line": 9,
            "title": "Oversized skipped",
            "body": "x" * 65536,
        }
        body = compose_review_body(
            "prose",
            [[("src/app.py", 9, finding)]],
            platform="github",
            findings_count=1,
            sha="abc1234",
            inline_count=0,
        ).body
        self.assertIn("prose\n\n---\n\n### ", body)
        self.assertNotIn("prose\n\n\n---", body)


class TestInlineBodyBudget(unittest.TestCase):
    SHA = "a" * 40
    KEY_A = "b" * 16
    KEY_B = "c" * 16

    def test_marker_suffix_has_literal_wire_sizes_and_unmarkable_sha_is_empty(self):
        singleton = _delivery_marker_suffix(self.SHA, [self.KEY_A])
        pair = _delivery_marker_suffix(self.SHA, [self.KEY_A, self.KEY_B])
        self.assertEqual(len(singleton.encode("utf-8")), 113)
        self.assertEqual(len(pair.encode("utf-8")), 225)
        self.assertEqual(
            singleton,
            "\n\n" + post_review.build_finding_marker(self.SHA, self.KEY_A),
        )
        self.assertEqual(_delivery_marker_suffix("unknown", [self.KEY_A]), "")
        self.assertEqual(_delivery_marker_suffix(self.SHA, []), "")

    def test_dry_capture_is_live_body_without_singleton_pair_or_note_markers(self):
        cases = [
            ("**finding**\n\nbody", [self.KEY_A], "discussion"),
            ("**finding**\n\nbody", [self.KEY_A, self.KEY_B], "discussion"),
            (
                post_review._finding_sections(
                    {"file": "?", "title": "note", "body": "body"}
                ),
                [self.KEY_A],
                "note",
            ),
        ]
        for sections, keys, surface in cases:
            with self.subTest(keys=keys):
                suffix = _delivery_marker_suffix(self.SHA, keys)
                captured = compose_inline_body(
                    sections,
                    platform="gitlab",
                    surface=surface,
                    marker_suffix=suffix,
                ).body
                live = captured + suffix
                self.assertEqual(live[len(captured) :], suffix)
                self.assertEqual(live[: len(captured)], captured)
        unmarkable_suffix = _delivery_marker_suffix("unknown", [self.KEY_A])
        self.assertEqual(unmarkable_suffix, "")
        unmarkable = compose_inline_body(
            "**finding**\n\nbody",
            platform="gitlab",
            surface="discussion",
            marker_suffix=unmarkable_suffix,
        ).body
        self.assertEqual(unmarkable + unmarkable_suffix, unmarkable)

    def test_fast_path_reserves_marker_bytes_in_both_modes(self):
        # Mutation: reserve zero marker bytes; the over case would incorrectly take
        # the fast path while the under case remains byte-identical.
        suffix = _delivery_marker_suffix(self.SHA, [self.KEY_A])
        with patch.dict(
            fold.PLATFORM_BODY_LIMITS["github"]["surfaces"]["inline"],
            {"bytes": 350},
        ):
            over = compose_inline_body(
                "x" * 214,
                platform="github",
                surface="inline",
                marker_suffix=suffix,
            )
            under = compose_inline_body(
                "x" * 212,
                platform="github",
                surface="inline",
                marker_suffix=suffix,
            )
        self.assertGreater(over.folded_bytes, 0)
        self.assertLessEqual(len((over.body + suffix).encode("utf-8")), 350)
        self.assertEqual(under.folded_bytes, 0)
        self.assertEqual(len((under.body + suffix).encode("utf-8")), 349)

    def test_multibyte_fast_path_uses_utf8_bytes(self):
        # Mutation: count Unicode characters instead of UTF-8 bytes; this exact
        # envelope would admit one character too many or miss the boundary.
        sections = "a" * 133 + "😀"
        with patch.dict(
            fold.PLATFORM_BODY_LIMITS["github"]["surfaces"]["inline"],
            {"bytes": 160},
        ):
            composed = compose_inline_body(
                sections, platform="github", surface="inline"
            )
        self.assertGreater(composed.folded_bytes, 0)
        self.assertLessEqual(len(composed.body.encode("utf-8")), 160)

    def test_inline_fold_cuts_comments_and_overlong_lines_at_safe_boundaries(self):
        with patch.dict(
            fold.PLATFORM_BODY_LIMITS["github"]["surfaces"]["inline"],
            {"bytes": 220},
        ):
            folded, dropped = fold.fold_inline_body(
                "a" * 10 + "😀" + "b" * 200,
                140,
                "github",
                "inline",
            )
        self.assertGreater(dropped, 0)
        self.assertTrue(folded.startswith("a" * 10 + "😀"))
        self.assertLessEqual(len(folded.encode("utf-8")), 140)

    def test_legacy_finding_with_oversized_severity_is_bounded(self):
        """Severity normalization bounds the legacy body before inline budgeting."""
        finding = {
            "severity": "s" * 70000,
            "title": "Legacy finding",
            "body": "body",
        }
        composed = compose_inline_body(
            _render_group_sections(finding, []),
            platform="github",
            surface="inline",
        )
        self.assertEqual(composed.folded_bytes, 0)
        self.assertIn("**\U0001f4a1 [LOW] Legacy finding**", composed.body)
        self.assertTrue(
            composed.body.endswith("\n\nbody\n\n" + post_review.BRAND_TRAILER)
        )
        self.assertLessEqual(len(composed.body.encode("utf-8")), 65536)

    def test_inline_disclosure_uses_stderr_and_raw_unanchored_location(self):
        composed = post_review.InlineBody("body", 7)
        post_review._SKIP_WARNINGS.clear()
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            _report_inline_budget(composed, "gitlab", "note", None, None)
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(post_review._SKIP_WARNINGS, [])
        self.assertEqual(
            stderr.getvalue(),
            "WARNING: Inline body folded by 7 bytes at ?:None: this corroborator "
            "note reached the 1000000-byte GitLab body limit.\n",
        )

    def test_impossible_inline_envelope_is_platform_specific(self):
        with patch.dict(
            fold.PLATFORM_BODY_LIMITS["github"]["surfaces"]["inline"],
            {"bytes": 20},
        ):
            composed = compose_inline_body("x", platform="github", surface="inline")
            with self.assertRaises(SystemExit):
                _inline_body_over_limit(composed, "", "github", "inline")

        with patch.dict(
            fold.PLATFORM_BODY_LIMITS["gitlab"]["surfaces"]["note"],
            {"bytes": 20},
        ):
            composed = compose_inline_body("x", platform="gitlab", surface="note")
            with patch("gauntlet.delivery.post.warn") as mock_warn:
                self.assertTrue(_inline_body_over_limit(composed, "", "gitlab", "note"))
            mock_warn.assert_called_once()


class TestSummaryBodyBudget(_DryRunTestBase):
    """Oracle tests for the complete per-platform summary-body budget."""

    SHA = "a" * 40
    CANONICAL_FOOTER = (
        "\n\n---\n"
        "Generated by code-gauntlet | Reviewed up to: "
        "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\n\n"
        '<!-- code-gauntlet-findings: {{"version":"3.0","findings_count":'
        "{findings_count}"
        ',"sha":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}} -->'
    )
    # The rendered canonical footer is 211 UTF-8 bytes for every one-digit count.

    def _invalid_finding(self, title="Skipped", body="b", **extra):
        finding = {
            "file": "foo.py",
            "line": 99,
            "severity": "high",
            "title": title,
            "body": body,
        }
        finding.update(extra)
        return finding

    def _run_poster(self, platform, review_body, findings, *, prior=False):
        gitlab = platform == "gitlab"
        data = {
            "platform": platform,
            "owner": "o",
            "repo": "r",
            "pr_number": 324,
            "sha": self.SHA,
            "review_body": review_body,
            "findings": findings,
        }
        self._write(data)
        versions = [
            {
                "base_commit_sha": "base",
                "head_commit_sha": "head",
                "start_commit_sha": "start",
            }
        ]
        stdout = io.StringIO()
        stderr = io.StringIO()
        with (
            patch.object(
                sys, "argv", ["post_review.py", self.findings_path, "--dry-run"]
            ),
            _poster_run(
                self.forge_factory,
                diff=GL_DIFF if gitlab else GH_DIFF,
                versions=versions,
            ),
            patch(
                "gauntlet.delivery.post.gitlab_prior_delivery",
                return_value=PriorDelivery(prior, frozenset(), frozenset(), None),
            ),
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            exit_code = None
            try:
                post_review.main()
            except SystemExit as exc:
                exit_code = exc.code
        payload_path = os.path.join(self.tmp, "post-review-payload.json")
        payload = self._payload() if os.path.exists(payload_path) else None
        return payload, stdout.getvalue(), stderr.getvalue(), exit_code

    def _exact_review_body(self, platform, limit):
        # Hand arithmetic only: header 24 + two 2-byte separators + frame (359 for
        # GitHub, 350 for GitLab) + piece 46 + footer 211.
        fixed_bytes = {
            "github": 24 + 2 + 2 + 359 + 46 + 211,
            "gitlab": 24 + 2 + 2 + 350 + 46 + 211,
        }[platform]
        return "x" * (limit - fixed_bytes)

    def test_github_exact_fit_is_posted_whole(self):
        # Mutation: use < instead of <= in the fast path; exact-fit output turns red.
        finding = self._invalid_finding()
        review_body = self._exact_review_body("github", 65536)
        payload, _, _, exit_code = self._run_poster("github", review_body, [finding])
        body = payload["payload"]["body"]
        self.assertFalse(exit_code)
        self.assertEqual(len(body.encode("utf-8")), 65536)
        self.assertIn("### ⚠️ 1 finding could not be anchored inline", body)
        self.assertNotIn("are not shown", body)
        self.assertNotIn("[folded:", body)

    def test_github_plus_one_omits_one_group(self):
        # Mutation: raise the GitHub limit or remove the bounded path; the omission turns red.
        finding = self._invalid_finding()
        review_body = self._exact_review_body("github", 65536) + "x"
        payload, _, _, exit_code = self._run_poster("github", review_body, [finding])
        body = payload["payload"]["body"]
        self.assertFalse(exit_code)
        self.assertLessEqual(len(body.encode("utf-8")), 65536)
        self.assertIn(
            "_1 of these 1 finding is not shown: this review body reached the "
            "65536-byte GitHub body limit._",
            body,
        )
        self.assertTrue(
            body.endswith(self.CANONICAL_FOOTER.format(findings_count=1)),
            "the canonical footer must remain last",
        )
        marker = review_marker.find_marker(body)
        self.assertEqual(marker["sha"], self.SHA)
        self.assertEqual(marker["findings_count"], 1)

    def test_gitlab_exact_fit_is_posted_whole(self):
        # Mutation: use < instead of <= in the fast path; exact-fit output turns red.
        finding = self._invalid_finding()
        review_body = self._exact_review_body("gitlab", 1000000)
        payload, _, _, exit_code = self._run_poster("gitlab", review_body, [finding])
        body = payload["summary"]["body"]
        self.assertFalse(exit_code)
        self.assertEqual(len(body.encode("utf-8")), 1000000)
        self.assertNotIn("are not shown", body)
        self.assertNotIn("[folded:", body)

    def test_gitlab_plus_one_omits_one_group(self):
        # Mutation: raise the GitLab limit or remove the bounded path; the omission turns red.
        finding = self._invalid_finding()
        review_body = self._exact_review_body("gitlab", 1000000) + "x"
        payload, _, _, exit_code = self._run_poster("gitlab", review_body, [finding])
        body = payload["summary"]["body"]
        self.assertFalse(exit_code)
        self.assertLessEqual(len(body.encode("utf-8")), 1000000)
        self.assertIn(
            "_1 of these 1 finding is not shown: this summary note reached the "
            "1000000-byte GitLab body limit._",
            body,
        )
        self.assertTrue(body.endswith(self.CANONICAL_FOOTER.format(findings_count=1)))
        marker = review_marker.find_marker(body)
        self.assertEqual(marker["sha"], self.SHA)
        self.assertEqual(marker["findings_count"], 1)

    def test_cjk_entries_use_utf8_bytes_and_pin_the_tradeoff(self):
        # Mutation: replace UTF-8 length with code-point length; these shown/omitted counts turn red.
        cases = (
            ("github", "界" * 21000, "界" * 2000, 65536),
            ("gitlab", "界" * 330000, "界" * 4000, 1000000),
        )
        for platform, review_body, entry_body, limit in cases:
            with self.subTest(platform=platform):
                finding = self._invalid_finding(title="CJK skipped", body=entry_body)
                payload, _, _, exit_code = self._run_poster(
                    platform, review_body, [finding]
                )
                body = (
                    payload["summary"]["body"]
                    if platform == "gitlab"
                    else payload["payload"]["body"]
                )
                self.assertFalse(exit_code)
                self.assertLessEqual(len(body.encode("utf-8")), limit)
                # Hand-typed trade-off: UTF-8 fitting yields shown=0 and omitted=1;
                # a code-point budget would incorrectly show the CJK entry.
                self.assertIn("界" * 10, body)
                self.assertIn("following 0 findings", body)
                self.assertNotIn("CJK skipped", body)
                self.assertIn(
                    "_1 of these 1 finding is not shown: this "
                    + (
                        "review body reached the 65536-byte GitHub body limit._"
                        if platform == "github"
                        else "summary note reached the 1000000-byte GitLab body limit._"
                    ),
                    body,
                )

    def test_first_fit_keeps_later_groups_after_a_misfit(self):
        # Mutation: stop at the first misfit, use smallest-first, use largest-first,
        # use best-fit, or reverse the group order; each policy below must turn this
        # first-fit oracle red.
        groups = [
            [("g1.py", 1, {"title": "G1", "body": "a" * 1960, "severity": "high"})],
            [("g2.py", 2, {"title": "G2", "body": "b" * 62960, "severity": "high"})],
            [
                (
                    "g3.py",
                    3,
                    {"title": "G3", "body": "c" * 33960, "severity": "high"},
                )
            ],
            [
                (
                    "g4.py",
                    4,
                    {"title": "G4", "body": "d" * 29960, "severity": "high"},
                )
            ],
        ]
        # Hand arithmetic: 24 + 2 + 367 + 2 + 99 + 211 = 705 reserved bytes, so
        # 64831 bytes remain. The pieces are 1998, 62998, 33998, and 29998 bytes.
        # First-fit takes G1 = 1998, G2 = 62998 misfits with 62833 left, G3 = 33998
        # fits with 28835 left, and G4 = 29998 misfits. It omits (G2, G4).
        # Stop-at-first-misfit takes G1, then omits (G2, G3, G4) after G2 misfits.
        # Smallest-first order is G1, G4, G3, G2: G1 + G4 = 31996, so it omits (G2, G3).
        # Largest-first takes G2 = 62998 with 1833 left, so it omits (G1, G3, G4).
        # Best-fit takes G3 + G4 = 63996, the unique maximum, so it omits (G1, G2).
        # Reversing order takes G4 + G3 = 63996 and omits (G1, G2), but changes shown order.
        # Thus first-fit must show G1 and later G3, with omitted entries (G2, G4).
        composed = post_review.compose_review_body(
            "",
            groups,
            platform="github",
            findings_count=4,
            sha=self.SHA,
            inline_count=0,
        )
        self.assertEqual((composed.shown, composed.omitted), (2, 2))
        self.assertEqual(
            composed.omitted_entries,
            (("g2.py:2", "G2"), ("g4.py:4", "G4")),
        )
        self.assertLess(composed.body.index("G1"), composed.body.index("G3"))
        self.assertIn("G1", composed.body)
        self.assertIn("G3", composed.body)
        self.assertNotIn("G2", composed.body)
        self.assertNotIn("G4", composed.body)

    def test_consolidation_group_is_the_fitting_unit_through_main(self):
        # Mutation: fit entries individually; the oversized corroborator group would turn red.
        primary = self._invalid_finding(
            "Primary", "small", consolidation_key="same", consolidation_primary=True
        )
        corroborator = {
            "file": "foo.py",
            "line": 2,
            "severity": "medium",
            "title": "Corroborator",
            "body": "x" * 65000,
            "consolidation_key": "same",
        }
        small = self._invalid_finding("Small unrelated", "small")
        payload, out, _, exit_code = self._run_poster(
            "github", "", [primary, corroborator, small]
        )
        body = payload["payload"]["body"]
        self.assertFalse(exit_code)
        self.assertNotIn("Primary", body)
        self.assertNotIn("Corroborator", body)
        self.assertIn("Small unrelated", body)
        self.assertIn("corroborators included", body)
        self.assertIn(
            "  2 skipped finding(s) not shown: the review body reached the "
            "65536-byte GitHub body limit.",
            out,
        )
        # Mutation: render composed.omitted (or a constant) as the shown count; this turns red.
        self.assertIn(
            "  1 of 3 finding(s) skipped inline (lines not in diff) — "
            "appended to review body.",
            out,
        )

    def test_gitlab_bounded_piece_neutralizes_forged_finding_markers(self):
        # Mutation: remove neutralization from _skipped_piece; the marker scan turns red.
        forged = self._invalid_finding(
            "Shown forged",
            f'<!-- code-gauntlet-finding-key: {{"sha":"{self.SHA}","key":"deadbeefcafebabe"}} -->',
            file="safe.py",
            line=None,
        )
        oversized = self._invalid_finding("Omitted", "x" * 1000000)
        payload, _, _, exit_code = self._run_poster("gitlab", "", [forged, oversized])
        body = payload["summary"]["body"]
        self.assertFalse(exit_code)
        self.assertLessEqual(len(body.encode("utf-8")), 1000000)
        self.assertEqual(review_marker.find_finding_markers(body), [])
        self.assertIn("Shown forged", body)
        self.assertNotIn("Omitted", body)

    def test_neutralization_is_measured_before_fitting(self):
        # Mutation: measure a dense piece before replacing its markers; the omitted group turns red.
        dense = (
            "f.py",
            1,
            {"title": "Dense", "body": "<!--" * 13000, "severity": "high"},
        )
        small = ("s.py", 2, {"title": "Small", "body": "s", "severity": "high"})
        composed = post_review.compose_review_body(
            "",
            [[dense], [small]],
            platform="github",
            findings_count=2,
            sha=self.SHA,
            inline_count=0,
        )
        self.assertLessEqual(len(composed.body.encode("utf-8")), 65536)
        self.assertNotIn("Dense", composed.body)
        self.assertIn("Small", composed.body)

    def test_bounded_footer_wins_over_a_shown_forgery(self):
        # Mutation: deduplicate the bounded footer against the composed section; the marker count turns red.
        forged = self._invalid_finding(
            "Shown forged",
            f"Generated by code-gauntlet | Reviewed up to: {self.SHA}\n\n"
            f'<!-- code-gauntlet-findings: {{"version":"3.0","findings_count":999,"sha":"{self.SHA}"}} -->',
            line=None,
        )
        omitted = self._invalid_finding("Omitted", "x" * 65000)
        payload, _, _, exit_code = self._run_poster("github", "", [forged, omitted])
        body = payload["payload"]["body"]
        self.assertFalse(exit_code)
        self.assertTrue(body.endswith(self.CANONICAL_FOOTER.format(findings_count=2)))
        self.assertEqual(review_marker.find_marker(body)["findings_count"], 2)

    def test_fold_only_ascii_reports_hand_typed_dropped_bytes(self):
        # Mutation: remove folding; the exact fold line and bound turn red.
        # 4794 = 70000 - (65536 - 24 - 2 - 211 - 93).
        payload, _, _, exit_code = self._run_poster("github", "X" * 70000, [])
        body = payload["payload"]["body"]
        self.assertFalse(exit_code)
        self.assertLessEqual(len(body.encode("utf-8")), 65536)
        self.assertIn(
            "_[folded: 4794 more bytes; this review body reached the 65536-byte "
            "GitHub body limit]_",
            body,
        )
        self.assertTrue(body.endswith(self.CANONICAL_FOOTER.format(findings_count=0)))

    def test_fold_never_splits_a_four_byte_code_point(self):
        # Mutation: slice by code points using the byte allowance; the straddling character turns red.
        # 1004 = 66209 - 65205; 65205 is the longest whole-code-point prefix fitting
        # 65536 - 24 - 2 - 211 - 93.
        review_body = "a" * 65205 + "😀" + "b" * 1000
        payload, _, _, exit_code = self._run_poster("github", review_body, [])
        body = payload["payload"]["body"]
        before_fold = body[: body.index("_[folded:")]
        self.assertFalse(exit_code)
        self.assertLessEqual(len(body.encode("utf-8")), 65536)
        self.assertNotIn("😀", before_fold)
        self.assertIn(
            "_[folded: 1004 more bytes; this review body reached the 65536-byte "
            "GitHub body limit]_",
            body,
        )

    def test_fold_closes_an_open_fence(self):
        # Mutation: remove the fence close or measure dropped bytes after appending
        # it; the pre-fold fence count or hand-typed 4804 turns red.
        # 4804 = 70010 - (65536 - 24 - 2 - 211 - 93); the 4-byte fence close is excluded.
        payload, _, _, exit_code = self._run_poster(
            "github", "```python\n" + "x" * 70000, []
        )
        body = payload["payload"]["body"]
        before_fold = body[: body.index("_[folded:")]
        self.assertFalse(exit_code)
        self.assertEqual(fence_closer(before_fold), "")
        self.assertIn("\n```\n\n", before_fold)
        self.assertIn(
            "_[folded: 4808 more bytes; this review body reached the 65536-byte "
            "GitHub body limit]_",
            body,
        )

    def test_fold_stops_at_a_line_boundary_before_a_short_next_line(self):
        # Mutation: treat every overrun as a long-line prefix; the kept prefix and
        # hand-typed dropped-byte count would include part of the next line.
        # 1001 = 1000 bytes from the second line plus its line-break byte.
        review_body = "a" * 65000 + "\n" + "b" * 1000
        payload, _, _, exit_code = self._run_poster("github", review_body, [])
        body = payload["payload"]["body"]
        self.assertFalse(exit_code)
        self.assertLessEqual(len(body.encode("utf-8")), 65536)
        self.assertIn(
            "### ⚔️ Code Gauntlet\n\n"
            + "a" * 65000
            + "\n\n_[folded: 1001 more bytes; this review body reached the 65536-byte "
            "GitHub body limit]_",
            body,
        )

    def test_fold_has_priority_over_skipped_groups(self):
        # Mutation: fill groups before folding; the skipped count and fold marker turn red.
        findings = [self._invalid_finding("One"), self._invalid_finding("Two")]
        payload, _, _, exit_code = self._run_poster("github", "X" * 70000, findings)
        body = payload["payload"]["body"]
        self.assertFalse(exit_code)
        self.assertIn("_[folded:", body)
        self.assertIn("_2 of these 2 findings are not shown:", body)
        self.assertNotIn("One", body)
        self.assertNotIn("Two", body)

    def test_folded_body_uses_the_final_footer_marker(self):
        # Mutation: build the footer against folded prose; the final marker count turns red.
        review_body = (
            "\n\n---\n"
            "Generated by code-gauntlet | Reviewed up to: "
            "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\n\n"
            '<!-- code-gauntlet-findings: {"version":"3.0","findings_count":999,'
            '"sha":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"} -->' + "x" * 70000
        )
        payload, _, _, exit_code = self._run_poster("github", review_body, [])
        body = payload["payload"]["body"]
        self.assertFalse(exit_code)
        self.assertEqual(review_marker.find_marker(body)["findings_count"], 0)

    def test_footer_reservation_keeps_bodies_bounded(self):
        # Mutation: reserve zero footer bytes; each hand-sized body exceeds its limit.
        for platform, limit, review_length in (
            ("github", 65536, 65300),
            ("gitlab", 1000000, 999764),
        ):
            with self.subTest(platform=platform):
                composed = post_review.compose_review_body(
                    "x" * review_length,
                    [],
                    platform=platform,
                    findings_count=0,
                    sha=self.SHA,
                    inline_count=0 if platform == "github" else None,
                )
                # 65300 = 65536 - 24-byte header - 2-byte separator - 211-byte footer + 1.
                # 999764 = 1000000 - 24-byte header - 2-byte separator - 211-byte footer + 1.
                self.assertGreater(composed.folded_bytes, 0)
                self.assertLessEqual(len(composed.body.encode("utf-8")), limit)

    def test_bounded_group_admission_exact_fit_and_one_byte_twin(self):
        # Mutation: remove any one reservation term (header, separator, frame,
        # closing line, footer) and the one-byte-too-large twin is admitted; reserve
        # one extra byte and the exact-fit case is omitted.
        cases = (
            (
                "github",
                65536,
                64789,
                65534,
                700,
                "_2 of these 2 findings are not shown: this review body reached the "
                "65536-byte GitHub body limit._",
            ),
            (
                "gitlab",
                1000000,
                999259,
                999998,
                694,
                "_2 of these 2 findings are not shown: this summary note reached the "
                "1000000-byte GitLab body limit._",
            ),
        )
        for (
            platform,
            limit,
            admitted_length,
            exact_bytes,
            twin_bytes,
            closing_line,
        ) in cases:
            with self.subTest(platform=platform):
                inline_count = 0 if platform == "github" else None
                admitted = self._invalid_finding(
                    "Admitted", "a" * admitted_length, file="admit.py", line=1
                )
                oversized = self._invalid_finding(
                    "Omitted", "b" * limit, file="oversized.py", line=2
                )
                groups = [
                    [("admit.py", 1, admitted)],
                    [("oversized.py", 2, oversized)],
                ]
                # GitHub: 64789 + 47 = 64836, which is
                # 65536 - (24 + 2 + 1 + 2 + 361 + 2 + 97 + 211).
                # GitLab: 999259 + 47 = 999306, which is
                # 1000000 - (24 + 2 + 1 + 2 + 352 + 2 + 100 + 211).
                composed = post_review.compose_review_body(
                    "x",
                    groups,
                    platform=platform,
                    findings_count=2,
                    sha=self.SHA,
                    inline_count=inline_count,
                )
                self.assertEqual((composed.shown, composed.omitted), (1, 1))
                # The singular shown/omitted forms use two fewer bytes than the plural reservation.
                self.assertEqual(len(composed.body.encode("utf-8")), exact_bytes)
                self.assertLessEqual(len(composed.body.encode("utf-8")), limit)
                self.assertIn("Admitted", composed.body)
                self.assertNotIn("Omitted", composed.body)

                twin = dict(admitted, body="a" * (admitted_length + 1))
                twin_composed = post_review.compose_review_body(
                    "x",
                    [[("admit.py", 1, twin)], groups[1]],
                    platform=platform,
                    findings_count=2,
                    sha=self.SHA,
                    inline_count=inline_count,
                )
                self.assertEqual((twin_composed.shown, twin_composed.omitted), (0, 2))
                self.assertEqual(len(twin_composed.body.encode("utf-8")), twin_bytes)
                self.assertLessEqual(len(twin_composed.body.encode("utf-8")), limit)
                self.assertNotIn("Admitted", twin_composed.body)
                self.assertIn(closing_line, twin_composed.body)

    def test_closing_line_reservation_keeps_later_groups_bounded(self):
        # Mutation: reserve no closing-line bytes; the large group fits and the final body exceeds the limit.
        large = self._invalid_finding("Large", "x" * 64887)
        small = self._invalid_finding("Small", "s")
        composed = post_review.compose_review_body(
            "",
            [[("foo.py", 99, large)], [("foo.py", 99, small)]],
            platform="github",
            findings_count=2,
            sha=self.SHA,
            inline_count=0,
        )
        # 64887 + 43-byte large piece = 64930; the reserved allowance is
        # 64839 = 65536 - (24 + 211 + 2 + 361 + 2 + 97), while dropping the
        # 97-byte closing line would make it fit.
        self.assertEqual((composed.shown, composed.omitted), (1, 1))
        self.assertNotIn("Large", composed.body)
        self.assertIn("Small", composed.body)
        self.assertIn("_1 of these 2 findings is not shown:", composed.body)
        self.assertLessEqual(len(composed.body.encode("utf-8")), 65536)

    def test_bounded_frame_uses_n_k_and_closing_order(self):
        # Mutation: render k as n, intro as n, or move the closing line before pieces; order turns red.
        groups = [
            [("f.py", i, {"title": f"T{i}", "body": "x" * 65000, "severity": "high"})]
            if i == 0
            else [("f.py", i, {"title": f"T{i}", "body": "x", "severity": "high"})]
            for i in range(3)
        ]
        composed = post_review.compose_review_body(
            "",
            groups,
            platform="github",
            findings_count=3,
            sha=self.SHA,
            inline_count=120,
        )
        body = composed.body
        self.assertLessEqual(len(body.encode("utf-8")), 65536)
        self.assertIn("### ⚠️ 3 findings could not be anchored inline", body)
        self.assertIn("120 inline comments were posted; the following 2", body)
        self.assertLess(body.index("T1"), body.index("is not shown"))
        self.assertLess(
            body.index("is not shown"), body.index("Generated by code-gauntlet")
        )
        self.assertNotIn("[folded:", body)

    def test_bounded_frame_reserves_a_three_digit_inline_count(self):
        # Mutation: reserve the bounded frame with n instead of inline_count; this
        # under-two-byte-slack case becomes over the limit.
        groups = [
            [("one.py", 1, {"title": "One", "body": "a" * 39960, "severity": "high"})],
            [("two.py", 2, {"title": "Two", "body": "b" * 24798, "severity": "high"})],
            [
                (
                    "three.py",
                    3,
                    {"title": "Three", "body": "c" * 24793, "severity": "high"},
                )
            ],
        ]
        composed = post_review.compose_review_body(
            "",
            groups,
            platform="github",
            findings_count=3,
            sha=self.SHA,
            inline_count=120,
        )
        body_bytes = len(composed.body.encode("utf-8"))
        self.assertEqual((composed.shown, composed.omitted), (2, 1))
        self.assertEqual(composed.omitted_entries, (("two.py:2", "Two"),))
        self.assertLess(composed.body.index("One"), composed.body.index("Three"))
        self.assertLessEqual(body_bytes, 65536)
        self.assertEqual(65536 - body_bytes, 1)
        self.assertIn("### ⚠️ 3 findings could not be anchored inline", composed.body)
        self.assertIn(
            "120 inline comments were posted; the following 2 findings reference "
            "lines outside this diff and are included here instead: A finding listed "
            "here may not have an anchoring problem of its own — a consolidation "
            "group whose primary could not be anchored inline is listed here in full, "
            "corroborators included.",
            composed.body,
        )
        self.assertIn(
            "_1 of these 3 findings is not shown: this review body reached the "
            "65536-byte GitHub body limit._",
            composed.body,
        )

    def test_bounded_frame_reserves_two_digit_skipped_counts(self):
        # Mutations: clamp the heading n, intro k, closing m, or closing n to one
        # digit; each single-term mutation admits First and must turn this red.
        # Hand arithmetic: the first piece is 64791 + 44 = 64835 bytes, while its
        # full two-digit reservation is 65536 - (24 + 212 + 2 + 363 + 2 + 99) = 64834.
        # The all-omitted body is 24 + 2 + 362 + 2 + 99 + 212 = 701 bytes.
        first = {
            "file": "shown.py",
            "line": 1,
            "severity": "high",
            "title": "First",
            "body": "a" * 64791,
        }
        groups = [[("shown.py", 1, first)]]
        groups.extend(
            [
                [
                    (
                        f"omitted{i}.py",
                        i,
                        {
                            "severity": "high",
                            "title": f"Omitted {i}",
                            "body": "z" * 65000,
                        },
                    )
                ]
                for i in range(2, 11)
            ]
        )
        composed = post_review.compose_review_body(
            "",
            groups,
            platform="github",
            findings_count=10,
            sha=self.SHA,
            inline_count=0,
        )
        self.assertEqual((composed.shown, composed.omitted), (0, 10))
        self.assertEqual(len(composed.body.encode("utf-8")), 701)
        self.assertLessEqual(len(composed.body.encode("utf-8")), 65536)
        self.assertIn("### ⚠️ 10 findings could not be anchored inline", composed.body)
        self.assertIn(
            "0 inline comments were posted; the following 0 findings reference "
            "lines outside this diff and are included here instead: A finding listed "
            "here may not have an anchoring problem of its own — a consolidation "
            "group whose primary could not be anchored inline is listed here in full, "
            "corroborators included.",
            composed.body,
        )
        self.assertIn(
            "_10 of these 10 findings are not shown: this review body reached the "
            "65536-byte GitHub body limit._",
            composed.body,
        )

    def test_zero_shown_has_zero_intro_and_a_closing_count(self):
        # Mutation: render the intro with n as shown; the zero-shown text turns red.
        finding = self._invalid_finding("Only", "x" * 65000)
        composed = post_review.compose_review_body(
            "",
            [[(finding["file"], finding["line"], finding)]],
            platform="github",
            findings_count=1,
            sha=self.SHA,
            inline_count=0,
        )
        self.assertLessEqual(len(composed.body.encode("utf-8")), 65536)
        self.assertIn("0 inline comments were posted; the following 0", composed.body)
        self.assertIn("_1 of these 1 finding is not shown:", composed.body)

    def test_budget_reporting_uses_stdout_and_stderr_without_capture_mutation(self):
        # Mutation: delete budget reporting or print it outside the GitLab note branch; these streams turn red.
        payload, out, err, exit_code = self._run_poster(
            "github", "", [self._invalid_finding("Budget title", "x" * 65000)]
        )
        self.assertFalse(exit_code)
        self.assertIn(
            "  0 of 1 finding(s) skipped inline (lines not in diff) — appended to review body.",
            out,
        )
        self.assertIn(
            "  1 skipped finding(s) not shown: the review body reached the 65536-byte GitHub body limit.",
            out,
        )
        self.assertIn(
            "Skipped finding 'Budget title' at foo.py:99 not shown: the review body reached the 65536-byte GitHub body limit.",
            err,
        )
        self.assertEqual(len(payload["skipped"]), 1)

        gitlab_payload, gitlab_out, gitlab_err, gitlab_exit = self._run_poster(
            "gitlab", "", [self._invalid_finding("GitLab budget", "x" * 1000000)]
        )
        self.assertFalse(gitlab_exit)
        self.assertIsNotNone(gitlab_payload)
        self.assertLess(
            gitlab_out.index("MR summary note captured (dry-run)."),
            gitlab_out.index("  1 skipped finding(s) not shown:"),
        )
        self.assertIn(
            "  1 skipped finding(s) not shown: the summary note reached the 1000000-byte GitLab body limit.",
            gitlab_out,
        )
        self.assertIn(
            "Skipped finding 'GitLab budget' at foo.py:99 not shown: the summary note reached the 1000000-byte GitLab body limit.",
            gitlab_err,
        )

        _, fold_out, _, fold_exit = self._run_poster("github", "X" * 70000, [])
        self.assertFalse(fold_exit)
        self.assertIn(
            "  review_body folded by 4794 bytes: the review body reached the 65536-byte GitHub body limit.",
            fold_out,
        )

        prior_payload, prior_out, _, prior_exit = self._run_poster(
            "gitlab",
            "X" * 1100000,
            [self._invalid_finding("Prior budget", "x")],
            prior=True,
        )
        self.assertFalse(prior_exit)
        self.assertEqual(prior_payload["summary"], {})
        self.assertIn("already on the MR", prior_out)
        self.assertNotIn("body limit", prior_out)

    def test_refusal_is_exact_at_both_platform_boundaries(self):
        # Mutation: delete _refuse_over_limit or move the guard after post_json; exact exits turn red.
        for platform, limit, surface, label in (
            ("github", 65536, "review body", "GitHub"),
            ("gitlab", 1000000, "summary note", "GitLab"),
        ):
            with self.subTest(platform=platform):
                post_review._refuse_over_limit("x" * limit, platform)
                with (
                    self.assertRaises(SystemExit) as exc,
                    contextlib.redirect_stderr(io.StringIO()) as stderr,
                ):
                    post_review._refuse_over_limit("x" * (limit + 1), platform)
                self.assertEqual(exc.exception.code, 1)
                self.assertIn(
                    f"The composed {surface} is {limit + 1} bytes, over the {limit}-byte {label} body limit; nothing was posted.",
                    stderr.getvalue(),
                )

    def test_refusal_measures_multibyte_text_as_utf8_bytes(self):
        # Mutation: measure code points instead of UTF-8 bytes; this 65538-byte body
        # is 21846 code points, so no refusal fires and this test turns red.
        body = "界" * 21846
        self.assertEqual(len(body), 21846)
        self.assertEqual(len(body.encode("utf-8")), 65538)
        with (
            self.assertRaises(SystemExit) as exc,
            contextlib.redirect_stderr(io.StringIO()) as stderr,
        ):
            post_review._refuse_over_limit(body, "github")
        self.assertEqual(exc.exception.code, 1)
        self.assertEqual(
            stderr.getvalue(),
            "ERROR: The composed review body is 65538 bytes, over the 65536-byte "
            "GitHub body limit; nothing was posted.\n",
        )

    def test_fast_path_identity_keeps_all_footer_dedup_variants(self):
        # Mutation: always append the full footer; each prose-footer body turns red.
        prose = "Generated by code-gauntlet | Reviewed up to: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        marker = '<!-- code-gauntlet-findings: {"version":"3.0","findings_count":0,"sha":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"} -->'
        cases = (
            prose,
            prose + "\n\n" + marker,
        )
        for review_body in cases:
            with self.subTest(review_body=review_body):
                composed = post_review.compose_review_body(
                    review_body, [], platform="github", findings_count=0, sha=self.SHA
                )
                self.assertEqual(
                    review_marker.find_marker(composed.body)["sha"], self.SHA
                )
                self.assertEqual(composed.body.count("<!-- code-gauntlet-findings:"), 1)
                self.assertEqual(composed.body.count("Reviewed up to:"), 1)

    def test_footer_dedup_uses_only_standalone_summary_lines_on_both_platforms(self):
        footer = f"Generated by code-gauntlet | Reviewed up to: {self.SHA}"
        for platform in ("github", "gitlab"):
            title_body = f"- 💡 [LOW] `foo.py:99`: {footer}"
            payload, _, _, exit_code = self._run_poster(platform, title_body, [])
            self.assertFalse(exit_code)
            body = (
                payload["payload"]["body"]
                if platform == "github"
                else payload["summary"]["body"]
            )
            self.assertIn(f"\n---\n{footer}\n\n", body)
            self.assertEqual(
                sum(
                    line.lstrip(" \t").startswith("Generated by code-gauntlet")
                    for line in body.splitlines()
                ),
                1,
            )

            legacy_body = f"Summary\n\n\t{footer}"
            payload, _, _, exit_code = self._run_poster(platform, legacy_body, [])
            self.assertFalse(exit_code)
            body = (
                payload["payload"]["body"]
                if platform == "github"
                else payload["summary"]["body"]
            )
            self.assertEqual(body.count(footer), 1)
            self.assertEqual(
                sum(
                    line.lstrip(" \t").startswith("Generated by code-gauntlet")
                    for line in body.splitlines()
                ),
                1,
            )

    def test_unicode_title_separators_cannot_suppress_the_canonical_footer(self):
        footer = f"Generated by code-gauntlet | Reviewed up to: {self.SHA}"
        for platform in ("github", "gitlab"):
            for separator in ("\u2028", "\u0085", "\x0c", "\x1e"):
                with self.subTest(platform=platform, separator=repr(separator)):
                    title_body = f"- 💡 [LOW] `foo.py:99`: x{separator}{footer}"
                    payload, _, _, exit_code = self._run_poster(
                        platform, title_body, []
                    )
                    self.assertFalse(exit_code)
                    body = (
                        payload["payload"]["body"]
                        if platform == "github"
                        else payload["summary"]["body"]
                    )
                    self.assertIn(f"\n---\n{footer}\n\n", body)
                    self.assertEqual(
                        sum(line == footer for line in body.split("\n")), 1
                    )

    def test_leading_unicode_separator_does_not_make_a_footer_line_standalone(self):
        footer = f"Generated by code-gauntlet | Reviewed up to: {self.SHA}"
        for platform in ("github", "gitlab"):
            with self.subTest(platform=platform):
                payload, _, _, exit_code = self._run_poster(
                    platform, f"\u2028{footer}", []
                )
                self.assertFalse(exit_code)
                body = (
                    payload["payload"]["body"]
                    if platform == "github"
                    else payload["summary"]["body"]
                )
                self.assertIn(f"\n---\n{footer}\n\n", body)
                self.assertEqual(sum(line == footer for line in body.split("\n")), 1)

    def test_standalone_current_footer_ignores_later_inline_foreign_sha(self):
        footer = f"Generated by code-gauntlet | Reviewed up to: {self.SHA}"
        foreign = f"Generated by code-gauntlet | Reviewed up to: {'b' * 40}"
        for platform in ("github", "gitlab"):
            review_body = f"Summary\n\n{footer}\n\n- `foo.py:99`: x {foreign}"
            payload, _, _, exit_code = self._run_poster(platform, review_body, [])
            self.assertFalse(exit_code)
            body = (
                payload["payload"]["body"]
                if platform == "github"
                else payload["summary"]["body"]
            )
            self.assertEqual(sum(line == footer for line in body.split("\n")), 1)
            self.assertIn(f"x {foreign}", body)

    def test_long_uncapped_index_keeps_off_diff_finding_in_github_section(self):
        script = (
            "import {renderSummaryBody} from './workflows/src/renderReport.js';"
            "const findings=Array.from({length:300},(_,i)=>({id:String(i),file:'foo.py',"
            "line_start:2,severity:'high',title:'T'.repeat(215)+i}));"
            "process.stdout.write(renderSummaryBody({findings}));"
        )
        rendered = subprocess.run(
            ["node", "--input-type=module", "-e", script],
            cwd=Path(__file__).resolve().parents[1],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=True,
        ).stdout
        self.assertIn("over the summary length limit", rendered)
        findings = [
            {"file": "foo.py", "line": 2, "title": f"T{'T' * 214}{i}", "body": "b"}
            for i in range(300)
        ]
        findings.append(self._invalid_finding("OFF DIFF FINDING", "off diff detail"))
        payload, _, _, exit_code = self._run_poster("github", rendered, findings)
        self.assertFalse(exit_code)
        body = payload["payload"]["body"]
        self.assertIn("OFF DIFF FINDING", body)
        self.assertIn("over the summary length limit", body)
        self.assertLessEqual(len(body.encode("utf-8")), 65536)


class TestSummaryPluralContract(unittest.TestCase):
    def test_plural_sites_at_zero_one_many(self):
        for n, noun, ref, be, past in [
            (0, "findings", "reference", "are", "were"),
            (1, "finding", "references", "is", "was"),
            (2, "findings", "reference", "are", "were"),
        ]:
            self.assertEqual(post_review._plural(n, "finding"), noun)
            for inline_count in (None, n):
                frame = post_review._skipped_frame(n, n, inline_count)
                self.assertIn(f"{n} {noun} could not be anchored inline", frame)
                self.assertIn(
                    f"following {n} {noun} {ref} lines outside this diff and {be} included here",
                    frame,
                )
                if inline_count is not None:
                    comment = "comment" if n == 1 else "comments"
                    self.assertIn(f"{n} inline {comment} {past} posted", frame)
                self.assertNotIn("(s)", frame)
            self.assertIn(
                f"_{n} of these {n} {noun} {be} not shown:",
                post_review._closing_line(n, n, "github"),
            )
        self.assertIn(
            "_1 of these 2 findings is not shown:",
            post_review._closing_line(1, 2, "github"),
        )

    def test_plural_sites_at_eleven_and_twenty_one_have_exact_posted_wording(self):
        cases = (
            (
                11,
                4,
                7,
                9,
                "---\n\n### ⚠️ 11 findings could not be anchored inline\n\n"
                "9 inline comments were posted; the following 4 findings reference lines "
                "outside this diff and are included here instead: A finding listed here may "
                "not have an anchoring problem of its own — a consolidation group whose "
                "primary could not be anchored inline is listed here in full, corroborators included.",
                "_7 of these 11 findings are not shown: this review body reached the "
                "65536-byte GitHub body limit._",
            ),
            (
                21,
                13,
                8,
                17,
                "---\n\n### ⚠️ 21 findings could not be anchored inline\n\n"
                "17 inline comments were posted; the following 13 findings reference lines "
                "outside this diff and are included here instead: A finding listed here may "
                "not have an anchoring problem of its own — a consolidation group whose "
                "primary could not be anchored inline is listed here in full, corroborators included.",
                "_8 of these 21 findings are not shown: this review body reached the "
                "65536-byte GitHub body limit._",
            ),
        )
        for (
            total,
            shown,
            omitted,
            inline_count,
            expected_frame,
            expected_closing,
        ) in cases:
            with self.subTest(total=total):
                frame = post_review._skipped_frame(total, shown, inline_count)
                closing = post_review._closing_line(omitted, total, "github")
                self.assertEqual(frame, expected_frame)
                self.assertEqual(closing, expected_closing)
                groups = [
                    [
                        (
                            "foo.py",
                            index + 1,
                            {"title": "small" if index < shown else "large"},
                        )
                    ]
                    for index in range(total)
                ]

                def piece(_filepath, _line, finding):
                    return (
                        "\n\n#### item small"
                        if finding["title"] == "small"
                        else "x" * 70000
                    )

                with patch("gauntlet.delivery.post._skipped_piece", side_effect=piece):
                    composed = post_review.compose_review_body(
                        "Summary",
                        groups,
                        platform="github",
                        findings_count=total,
                        sha="a" * 40,
                        inline_count=inline_count,
                    )
                expected_body = (
                    "### ⚔️ Code Gauntlet\n\nSummary\n\n"
                    + expected_frame
                    + "\n\n#### item small" * shown
                    + "\n\n"
                    + expected_closing
                    + TestSummaryBodyBudget.CANONICAL_FOOTER.format(
                        findings_count=total
                    )
                )
                self.assertEqual(composed.shown, shown)
                self.assertEqual(composed.omitted, omitted)
                self.assertEqual(composed.body, expected_body)

    def test_singular_to_zero_frame_reservation_is_bounded(self):
        for platform in ("github", "gitlab"):
            for inline_count in (None, 1):
                finding = {"title": "Oversized", "body": "x" * 2000000}
                groups = [[("a.js", 1, finding)]]
                # Fill precisely the old singular reservation; a zero-shown frame
                # grows by one byte, so restoring that whole mechanism overflows.
                old_fixed = (
                    post_review.BRAND_SUMMARY_HEADER
                    + post_review.build_footer(1, "a" * 40, body="")
                    + "\n\n" * 3
                    + post_review._skipped_frame(1, 1, inline_count)
                    + post_review._closing_line(1, 1, platform)
                )
                old_allowance = fold.body_limit(platform).bytes - len(
                    old_fixed.encode("utf-8")
                )
                exact_old = compose_review_body(
                    "x" * old_allowance,
                    groups,
                    platform=platform,
                    findings_count=1,
                    sha="a" * 40,
                    inline_count=inline_count,
                )
                self.assertLessEqual(
                    len(exact_old.body.encode("utf-8")),
                    fold.body_limit(platform).bytes,
                )
                self.assertEqual(exact_old.omitted, 1)
                for size in (2000000, 64000):
                    composed = compose_review_body(
                        "x" * size,
                        groups,
                        platform=platform,
                        findings_count=1,
                        sha="a" * 40,
                        inline_count=inline_count,
                    )
                    self.assertLessEqual(
                        len(composed.body.encode("utf-8")),
                        fold.body_limit(platform).bytes,
                    )
                    self.assertEqual(composed.omitted, 1)
                    self.assertIn("following 0 findings reference", composed.body)
                    self.assertIn("_1 of these 1 finding is not shown:", composed.body)
                for n in (1, 2, 9, 10, 100):
                    reserve = post_review._skipped_reserve(n, inline_count, platform)
                    for shown in range(n):
                        actual = post_review._skipped_frame(
                            n, shown, inline_count
                        ) + post_review._closing_line(n - shown, n, platform)
                        self.assertLessEqual(len(actual.encode("utf-8")), reserve)

    def test_summary_parser_stops_at_change_context(self):
        self.assertEqual(
            summary_body_from_report(
                "## Summary\n\n0 findings after the gauntlet.\n\n## Change Context\n\nThe PR claims to change things.\n\n## Review Dimensions Summary\n"
            ),
            "0 findings after the gauntlet.",
        )


# A five-line hunk so a multi-line comment's end_line can land either inside or
# outside the same hunk as its (already-valid) start line.
GH_DIFF_MULTILINE = (
    "diff --git a/foo.py b/foo.py\n"
    "--- a/foo.py\n"
    "+++ b/foo.py\n"
    "@@ -1,1 +1,5 @@\n"
    " existing\n"
    "+added2\n"
    "+added3\n"
    "+added4\n"
    "+added5\n"
)


class TestGitHubMultiLineRangeValidation(_DryRunTestBase):
    """Issue #192 follow-up: a live run 422'd because ``end_line`` was never
    validated — GitHub rejects the WHOLE review POST when a multi-line comment's
    range crosses out of the diff's hunk, even though the start line was valid."""

    def _finding(self, line, end_line):
        return _finding(line=line, end_line=end_line, title="Range bug", body="Body")

    def test_end_line_outside_the_diff_falls_back_to_single_line(self):
        run = _run_main(
            Path(self.tmp),
            self.forge_factory,
            _review_data(
                platform="github",
                review_body="Summary",
                findings=[self._finding(line=2, end_line=940)],
            ),
            diff=GH_DIFF_MULTILINE,
        )
        comment = run.payload["payload"]["comments"][0]
        self.assertNotIn("start_line", comment)
        self.assertNotIn("start_side", comment)
        self.assertEqual(comment["line"], 2)

    def test_end_line_inside_the_same_hunk_preserves_the_range(self):
        run = _run_main(
            Path(self.tmp),
            self.forge_factory,
            _review_data(
                platform="github",
                review_body="Summary",
                findings=[self._finding(line=2, end_line=4)],
            ),
            diff=GH_DIFF_MULTILINE,
        )
        comment = run.payload["payload"]["comments"][0]
        self.assertEqual(comment["start_line"], 2)
        self.assertEqual(comment["start_side"], "RIGHT")
        self.assertEqual(comment["line"], 4)


class TestGitlabSkippedFindingsDegrade(_GitlabLiveRunBase):
    def _summary_note_body(self, payloads):
        notes = [p for p in payloads if "position" not in p]
        self.assertEqual(len(notes), 1)
        return notes[0]["body"]

    def test_skipped_and_no_line_findings_land_in_summary_note(self):
        off_diff = dict(GL_CONTRACT_FINDINGS[0], line=999, title="Off-diff finding")
        no_line = {
            "file": "src/edited.py",
            "title": "No-line finding",
            "body": "Body four",
        }
        findings = [*GL_CONTRACT_FINDINGS, off_diff, no_line]
        payloads = []
        run = self._run_main(findings=findings, payloads=payloads)
        self.assertIsNone(run.exit_code)

        body = self._summary_note_body(payloads)
        self.assertIn("### ⚠️ 2 findings could not be anchored inline", body)
        self.assertIn("Off-diff finding", body)
        self.assertIn("No-line finding", body)
        self.assertIn("src/edited.py`", body)  # the no-line entry has a bare path

        # Neither skipped finding was ever attempted as a discussion.
        discussion_bodies = [p["body"] for p in payloads if "position" in p]
        self.assertNotIn(render_comment_body(off_diff), discussion_bodies)
        self.assertNotIn(render_comment_body(no_line), discussion_bodies)
        self.assertEqual(len(discussion_bodies), 3)
        self.assertIn("  2 finding(s) skipped.", run.out)


class TestBuildSkippedSectionNoFileNoLine(unittest.TestCase):
    def test_no_file_and_no_line_renders_placeholder_without_raising(self):
        finding = {"title": "Mystery finding", "body": "b"}
        section = build_skipped_section([(None, None, finding)])
        self.assertIn("`?`", section)
        self.assertIn("Mystery finding", section)


# ---------------------------------------------------------------------------
# Issue #63 — the deterministic suggested_fix_code apply-check
# ---------------------------------------------------------------------------

# A hunk whose body is INDENTED WITH SPACES, so an indentation-charset conflict
# has real indentation to conflict with, and whose lines differ from any
# replacement a test writes (so the no-op check is not tripped by accident).
# foo.py: 1 = context `def f():`, 2 = added `    return 1`, 3 = added `    # tail`.
GH_DIFF_INDENTED = (
    "diff --git a/foo.py b/foo.py\n"
    "--- a/foo.py\n"
    "+++ b/foo.py\n"
    "@@ -1,1 +1,3 @@\n"
    " def f():\n"
    "+    return 1\n"
    "+    # tail\n"
)

# The same hunk in plain `glab mr diff` shape — paths verbatim, no `a/` / `b/`.
GL_DIFF_INDENTED = (
    "--- foo.py\n+++ foo.py\n@@ -1,1 +1,3 @@\n def f():\n+    return 1\n+    # tail\n"
)

# The same hunk again, but the hunk BODY is CRLF-terminated (only the body — the
# parser splits the whole stdout on "\n" only, so a body line ending "\r\n" leaves
# a trailing "\r" in that line's parsed text; header lines stay plain "\n" so the
# path keys this fixture produces are the ordinary, un-suffixed ones). This is what
# a real CRLF-in-the-repo diff hands the parser: transport, not content.
GH_DIFF_INDENTED_CRLF_BODY = (
    "diff --git a/foo.py b/foo.py\n"
    "--- a/foo.py\n"
    "+++ b/foo.py\n"
    "@@ -1,1 +1,3 @@\n"
    " def f():\n"
    "+    return 1\r\n"
    "+    # tail\r\n"
)

# The same file, long enough for a finding to STATE a span wider than GitLab's
# offset cap: every line 1..(cap + 3) is an added, addressable line, so
# `range_not_in_diff` — which precedes the anchor check — cannot be what a
# cap-exceeded span reports.
_GL_LONG_LINE_COUNT = post_review._GITLAB_SUGGESTION_OFFSET_CAP + 3
GL_DIFF_LONG = (
    f"--- foo.py\n+++ foo.py\n@@ -0,0 +1,{_GL_LONG_LINE_COUNT} @@\n"
    + "".join(f"+    line{n}\n" for n in range(1, _GL_LONG_LINE_COUNT + 1))
)

# The #229 collision shape, git-shaped: a real top-level `b/` directory's
# `b/foo.py` alongside a SEPARATE, real `foo.py` in the same diff. `foo.py` gets
# TWO added lines (2, 3); `b/foo.py` gets only ONE (2) — so a finding spelled
# `b/foo.py` at line 2 hits its OWN key directly (exact-hit collision), while one
# at line 3 validates ONLY through the stripped fallback to `foo.py` (exact-miss
# collision, today's silent cross-file case).
GH_DIFF_FENCE_COLLISION = (
    "diff --git a/foo.py b/foo.py\n"
    "--- a/foo.py\n"
    "+++ b/foo.py\n"
    "@@ -1,1 +1,3 @@\n"
    " line1\n"
    "+TOP_V1\n"
    "+TOP_V2\n"
    "diff --git a/b/foo.py b/b/foo.py\n"
    "--- a/b/foo.py\n"
    "+++ b/b/foo.py\n"
    "@@ -1,1 +1,2 @@\n"
    " line1\n"
    "+SUB_V1\n"
)

# The same collision shape, verbatim (plain `glab mr diff` — no `diff --git`
# decoration): a real top-level `a/` directory's `a/x.py` alongside a separate
# real `x.py`.
GL_DIFF_FENCE_COLLISION = (
    "--- a/x.py\n+++ a/x.py\n@@ -1,1 +1,2 @@\n line1\n+SUB_V1\n"
    "--- x.py\n+++ x.py\n@@ -1,1 +1,2 @@\n line1\n+TOP_V1\n"
)

# A single real file, PREFIXED-SPELLING recall check: `src/edited.py` is the
# diff's only file (git-shaped, so the header strips to the unprefixed key) — a
# finding spelled `b/src/edited.py` has no colliding sibling to be ambiguous
# with, so both its anchor AND its fence must still resolve.
GH_DIFF_PREFIXED_NO_COLLISION = (
    "diff --git a/src/edited.py b/src/edited.py\n"
    "--- a/src/edited.py\n"
    "+++ b/src/edited.py\n"
    "@@ -1,1 +1,2 @@\n"
    " line1\n"
    "+CURRENT\n"
)

# The same recall check, verbatim (GitLab): `src/edited.py` is the diff's only
# file, unprefixed and unambiguous.
GL_DIFF_PREFIXED_NO_COLLISION = (
    "--- src/edited.py\n+++ src/edited.py\n@@ -1,1 +1,2 @@\n line1\n+CURRENT\n"
)

# The #229 MULTI-LINE ANCHOR delta (distinct from the fence-ambiguity shape
# above): `foo.py` carries lines {1, 2}; the real top-level `b/foo.py` carries
# {3, 4} — two SEPARATE files, no collision (each stripped/exact spelling
# names only ONE of them at any given line). A finding spelled `b/foo.py`
# stating a range that spans BOTH files' line sets is what post_github's
# multiline check must now judge against the RESOLVED path.
GH_DIFF_MULTILINE_ANCHOR_COLLISION = (
    "diff --git a/foo.py b/foo.py\n"
    "--- a/foo.py\n"
    "+++ b/foo.py\n"
    "@@ -1,1 +1,2 @@\n"
    " line0\n"
    "+FOO_L1\n"
    "diff --git a/b/foo.py b/b/foo.py\n"
    "--- a/b/foo.py\n"
    "+++ b/b/foo.py\n"
    "@@ -3,1 +3,2 @@\n"
    " line2\n"
    "+SUB_L4\n"
)

# #223: one file, wide enough (lines 1..6) that two findings' STATED ranges can
# genuinely overlap or genuinely touch-without-overlapping within it.
GH_DIFF_OVERLAP = (
    "diff --git a/foo.py b/foo.py\n"
    "--- a/foo.py\n"
    "+++ b/foo.py\n"
    "@@ -1,1 +1,6 @@\n"
    " def f():\n"
    "+    line2\n"
    "+    line3\n"
    "+    line4\n"
    "+    line5\n"
    "+    line6\n"
)

# The same hunk, plain `glab mr diff` shape.
GL_DIFF_OVERLAP = (
    "--- foo.py\n+++ foo.py\n@@ -1,1 +1,6 @@\n"
    " def f():\n"
    "+    line2\n"
    "+    line3\n"
    "+    line4\n"
    "+    line5\n"
    "+    line6\n"
)

_FENCE = "```suggestion"


class TestSuggestedFixGate(unittest.TestCase):
    """The pure gate helper: one case per reason in the closed vocabulary.

    Ground truth comes from the REAL parser (``_parse_fixture``), not a
    hand-written mapping — a gate checked against the answer the test wanted
    would agree with a broken parser.
    """

    def setUp(self):
        parsed = _parse_fixture(GH_DIFF_INDENTED, platform="github")
        parsed_facts = parsed
        self.facts = parsed_facts
        self.valid_lines = parsed_facts.valid_lines
        self.line_texts = parsed_facts.line_texts

    def _finding(self, **over):
        finding = {
            "file": "foo.py",
            "line": 2,
            "end_line": 3,
            "suggested_fix_code": "    return 2\n    # done",
        }
        finding.update(over)
        return finding

    def _gate(self, finding, apply_range=(2, 3), **over):
        kwargs = {
            "apply_range": apply_range,
            "facts": self.facts,
            "path_lookup": "foo.py",
        }
        kwargs.update(over)
        return post_review._suggested_fix_gate(finding, **kwargs)

    def _reason(self, finding, **over):
        ok, reason = self._gate(finding, **over)
        self.assertFalse(ok, f"expected a downgrade, got ok with reason {reason!r}")
        self.assertIn(
            reason,
            post_review._FIX_REASONS,
            "every downgrade must name a member of the closed vocabulary",
        )
        return reason

    # -- the passing cases -------------------------------------------------

    def test_absent_field_is_not_a_downgrade(self):
        finding = self._finding()
        del finding["suggested_fix_code"]
        self.assertEqual(self._gate(finding), (True, None))

    def test_sound_multi_line_fix_passes(self):
        self.assertEqual(self._gate(self._finding()), (True, None))

    def test_sound_single_line_fix_passes(self):
        finding = self._finding(line=2, end_line=2, suggested_fix_code="    return 2")
        self.assertEqual(self._gate(finding, apply_range=(2, 2)), (True, None))

    def test_legitimate_reindentation_passes(self):
        """The indentation check is deliberately weak: only a tab/space charset
        conflict is caught, so re-indenting a span is not a downgrade."""
        finding = self._finding(suggested_fix_code="        return 2\n        # done")
        self.assertEqual(self._gate(finding), (True, None))

    def test_a_partial_content_oracle_reads_as_no_oracle(self):
        """A span the texts cannot fully answer is NO oracle, never "no
        difference" — the latter would silently skip the content checks and
        pass the fence through unchecked. The range oracle alone is not
        enough: a patch is downgraded, not rendered, when the content oracle
        cannot answer for every line of the stated span.
        """
        finding = self._finding(suggested_fix_code="    return 1\n    # tail")
        self.assertEqual(
            self._gate(
                finding,
                facts=replace(self.facts, line_texts={("foo.py", 2): "    return 1"}),
            ),
            (False, "no_diff_oracle"),
        )

    # -- 1. non_string -----------------------------------------------------

    def test_non_string_fix(self):
        self.assertEqual(
            self._reason(self._finding(suggested_fix_code=42)), "non_string"
        )

    def test_null_fix_is_non_string(self):
        """The contracts say OMIT, never null — a null that arrives anyway is
        not a string and is downgraded, not rendered."""
        self.assertEqual(
            self._reason(self._finding(suggested_fix_code=None)), "non_string"
        )

    # -- 2. empty ----------------------------------------------------------

    def test_whitespace_only_fix_is_empty(self):
        self.assertEqual(
            self._reason(self._finding(suggested_fix_code="  \n  ")), "empty"
        )

    # -- 3. carriage_return --------------------------------------------------

    def test_an_interior_lone_cr_downgrades(self):
        """CommonMark treats a lone ``\\r`` as a line ending — ``"foo\\rbar"`` is
        ONE line to this gate's ``split("\\n")`` but TWO lines in the rendered
        fence and the applied patch. That gap is evadable (it dodges the no-op,
        indentation, and line-count checks entirely), so any interior ``\\r``
        fails closed before those measurements run."""
        finding = self._finding(suggested_fix_code="foo\rbar\rbaz")
        self.assertEqual(self._reason(finding), "carriage_return")

    def test_a_crlf_terminated_replacement_downgrades(self):
        """A replacement whose lines end ``\\r\\n`` is exactly the ambiguous
        CRLF-file case a one-click apply must not ship — the prose suggestion
        still carries the fix."""
        finding = self._finding(suggested_fix_code="    return 2\r\n    # done\r\n")
        self.assertEqual(self._reason(finding), "carriage_return")

    # -- 4. redacted -------------------------------------------------------

    def test_a_fix_the_redactor_rewrites_is_never_shipped(self):
        """One click would commit the literal ``[REDACTED]`` into the file."""
        secret = "ghp_" + "A" * 24
        finding = self._finding(suggested_fix_code=f"    token = {secret!r}")
        self.assertEqual(self._reason(finding), "redacted")

    # -- 5. missing_end_line -----------------------------------------------

    def test_absent_end_line(self):
        finding = self._finding()
        del finding["end_line"]
        self.assertEqual(self._reason(finding, apply_range=(2, 2)), "missing_end_line")

    def test_null_end_line_is_absent(self):
        """#205 DELETES ``line_end`` when a span exceeds ``maxLineSpan``; a null
        left behind by anything else must read the same way."""
        self.assertEqual(
            self._reason(self._finding(end_line=None), apply_range=(2, 2)),
            "missing_end_line",
        )

    # -- 6. invalid_range --------------------------------------------------

    def test_end_line_before_line(self):
        self.assertEqual(
            self._reason(self._finding(line=3, end_line=2), apply_range=(3, 2)),
            "invalid_range",
        )

    def test_non_integer_line(self):
        self.assertEqual(
            self._reason(self._finding(line=2.0), apply_range=(2.0, 3)),
            "invalid_range",
        )

    def test_boolean_line_is_not_an_integer(self):
        """``True`` is an ``int`` to ``isinstance`` and hashes equal to ``1`` —
        the same trap ``validate_position`` documents."""
        self.assertEqual(
            self._reason(
                self._finding(line=True, end_line=True), apply_range=(True, True)
            ),
            "invalid_range",
        )

    def test_line_below_one(self):
        self.assertEqual(
            self._reason(self._finding(line=0, end_line=1), apply_range=(0, 1)),
            "invalid_range",
        )

    # -- 7. no_diff_oracle -------------------------------------------------

    def test_a_missing_diff_fails_closed(self):
        """A failed diff fetch leaves NO oracle, so the range and content checks
        cannot run at all. The ANCHOR fails open there — a wrong anchor costs a
        misplaced comment. A patch cannot: a wrong patch corrupts the file, and
        the prose suggestion carries the same content at no risk.
        """
        self.assertEqual(
            self._reason(self._finding(), facts=None),
            "no_diff_oracle",
        )

    # -- 8. range_not_in_diff ----------------------------------------------

    def test_end_line_outside_the_diff(self):
        self.assertEqual(
            self._reason(self._finding(end_line=940), apply_range=(2, 940)),
            "range_not_in_diff",
        )

    def test_unknown_path(self):
        self.assertEqual(
            self._reason(self._finding(), path_lookup="other.py"),
            "range_not_in_diff",
        )

    # -- 9. anchor_mismatch ------------------------------------------------

    def test_apply_range_narrower_than_the_stated_range(self):
        """The site's one click really replaces less than the patch states, so
        applying it would overwrite one line and leave the other. Reached with a
        wrong anchor, and wherever no wider apply range can be expressed at all —
        a GitLab span past the platform offset cap renames THIS outcome
        (``span_exceeds_platform_cap``) rather than adding a check."""
        self.assertEqual(
            self._reason(self._finding(), apply_range=(2, 2)), "anchor_mismatch"
        )

    def test_no_apply_range_at_all(self):
        """A position-less note and the degraded body section carry no anchor —
        a fence there can never be applied."""
        self.assertEqual(
            self._reason(self._finding(), apply_range=None), "anchor_mismatch"
        )

    # -- 10. no_op_replacement ----------------------------------------------

    def test_replacement_equal_to_the_span(self):
        finding = self._finding(suggested_fix_code="    return 1\n    # tail")
        self.assertEqual(self._reason(finding), "no_op_replacement")

    def test_no_op_ignores_a_transport_carriage_return(self):
        """A CRLF diff leaves a trailing ``\\r`` on every parsed line's SPAN
        text. That is transport, not content, so it must not make a no-op
        look like a change. Parsed by the REAL parser (``_parse_fixture``),
        not a hand-built dict — G1 now downgrades any REPLACEMENT carrying a
        ``\\r`` before this check even runs, so a replacement-side ``\\r`` can
        no longer pin this tolerance; only the span side can, which G1 leaves
        untouched.
        """
        parsed_facts = _parse_fixture(GH_DIFF_INDENTED_CRLF_BODY, platform="github")
        valid_lines = parsed_facts.valid_lines
        line_texts = parsed_facts.line_texts
        self.assertEqual(line_texts[("foo.py", 2)], "    return 1\r")
        self.assertEqual(line_texts[("foo.py", 3)], "    # tail\r")
        finding = self._finding(suggested_fix_code="    return 1\n    # tail")
        ok, reason = self._gate(
            finding,
            facts=replace(self.facts, valid_lines=valid_lines, line_texts=line_texts),
        )
        self.assertFalse(ok)
        self.assertEqual(reason, "no_op_replacement")

    # -- 11. indentation_mismatch ------------------------------------------

    def test_tabs_into_a_space_indented_span(self):
        finding = self._finding(suggested_fix_code="\treturn 2\n\t# done")
        self.assertEqual(self._reason(finding), "indentation_mismatch")

    def test_spaces_into_a_tab_indented_span(self):
        """The symmetric case, against a tab-indented span."""
        tabbed = (
            "diff --git a/t.py b/t.py\n"
            "--- a/t.py\n"
            "+++ b/t.py\n"
            "@@ -1,1 +1,2 @@\n"
            " def f():\n"
            "+\treturn 1\n"
        )
        parsed_facts = _parse_fixture(tabbed, platform="github")
        valid_lines = parsed_facts.valid_lines
        line_texts = parsed_facts.line_texts
        finding = {
            "file": "t.py",
            "line": 2,
            "end_line": 2,
            "suggested_fix_code": "    return 2",
        }
        ok, reason = post_review._suggested_fix_gate(
            finding,
            apply_range=(2, 2),
            path_lookup="t.py",
            facts=diff_facts(valid_lines, line_texts=line_texts),
        )
        self.assertFalse(ok)
        self.assertEqual(reason, "indentation_mismatch")

    def test_an_unindented_span_conflicts_with_nothing(self):
        """Lines without leading whitespace say nothing about the file's
        indentation style, so they contribute nothing to the charset."""
        parsed_facts = _parse_fixture(GH_DIFF_MULTILINE, platform="github")
        valid_lines = parsed_facts.valid_lines
        line_texts = parsed_facts.line_texts
        finding = {
            "file": "foo.py",
            "line": 2,
            "end_line": 3,
            "suggested_fix_code": "\tfixed2\n\tfixed3",
        }
        self.assertEqual(
            post_review._suggested_fix_gate(
                finding,
                apply_range=(2, 3),
                path_lookup="foo.py",
                facts=diff_facts(valid_lines, line_texts=line_texts),
            ),
            (True, None),
        )

    # -- edge blank lines are content --------------------------------------

    def test_a_leading_blank_line_is_content_not_padding(self):
        """The fence normalizer takes the terminator off and NOTHING else, so a
        stated leading blank line survives — which makes this a real change
        against the same two lines, not a no-op."""
        finding = self._finding(suggested_fix_code="\n    return 1\n    # tail")
        self.assertEqual(self._gate(finding), (True, None))

    def test_a_second_trailing_newline_is_content(self):
        finding = self._finding(suggested_fix_code="    return 1\n    # tail\n\n")
        self.assertEqual(self._gate(finding), (True, None))

    def test_exactly_one_trailing_newline_is_the_terminator(self):
        finding = self._finding(suggested_fix_code="    return 1\n    # tail\n")
        self.assertEqual(self._reason(finding), "no_op_replacement")

    # -- 12. replacement_too_large ----------------------------------------

    def test_too_many_lines(self):
        body = "\n".join(f"    line{n}" for n in range(post_review._FIX_MAX_LINES + 1))
        self.assertEqual(
            self._reason(self._finding(suggested_fix_code=body)),
            "replacement_too_large",
        )

    def test_too_many_characters(self):
        body = "    " + "x" * post_review._FIX_MAX_CHARS
        self.assertEqual(
            self._reason(self._finding(suggested_fix_code=body)),
            "replacement_too_large",
        )

    def test_exactly_at_the_bounds_passes(self):
        body = "\n".join(f"    line{n}" for n in range(post_review._FIX_MAX_LINES))
        self.assertLessEqual(len(body), post_review._FIX_MAX_CHARS)
        self.assertEqual(
            self._gate(self._finding(suggested_fix_code=body)), (True, None)
        )

    def test_the_terminator_does_not_count_as_a_line(self):
        """ONE definition of lines and chars everywhere (#63): both are measured
        on the NORMALIZED text — ``split("\\n")`` elements, and ``len()`` in code
        points. The terminating newline is not a 101st line."""
        body = "\n".join(f"    line{n}" for n in range(post_review._FIX_MAX_LINES))
        self.assertEqual(
            self._gate(self._finding(suggested_fix_code=body + "\n")), (True, None)
        )

    def test_a_blank_line_past_the_terminator_does_count(self):
        body = "\n".join(f"    line{n}" for n in range(post_review._FIX_MAX_LINES))
        self.assertEqual(
            self._reason(self._finding(suggested_fix_code=body + "\n\n")),
            "replacement_too_large",
        )

    def test_the_terminator_does_not_count_toward_the_char_bound(self):
        body = "    " + "x" * (post_review._FIX_MAX_CHARS - 4)
        self.assertEqual(len(body), post_review._FIX_MAX_CHARS)
        self.assertEqual(
            self._gate(self._finding(suggested_fix_code=body + "\n")), (True, None)
        )

    # -- fence-path ambiguity (issue #229) ----------------------------------

    def test_exact_hit_collision_fails_closed(self):
        """The finding's raw spelling directly matches a real diff key — but
        its stripped form ALSO names a real, DIFFERENT diff key in the same
        diff, so the fence cannot tell which file a patch targets even though
        it validates cleanly against its own. Today's verified one-click-
        corruption case (#229): the fence used to validate against
        ``b/x.py``'s own text while the patch may have meant ``x.py``.

        Mutation: gut ``path_is_ambiguous`` to ``return False``
        unconditionally — RED (``(True, None)`` instead of the downgrade).
        """
        valid_lines = {("b/x.py", 10): 10, ("x.py", 10): 10}
        line_texts = {("b/x.py", 10): "subline", ("x.py", 10): "topline"}
        finding = {
            "file": "b/x.py",
            "line": 10,
            "end_line": 10,
            "suggested_fix_code": "changed",
        }
        self.assertEqual(
            post_review._suggested_fix_gate(
                finding,
                apply_range=(10, 10),
                path_lookup="b/x.py",
                facts=diff_facts(valid_lines, line_texts=line_texts),
            ),
            (False, "no_diff_oracle"),
        )

    def test_exact_miss_collision_fails_closed(self):
        """The finding's raw spelling is ABSENT from the diff at its stated
        line; only the stripped form validates there — the silent cross-file
        case. Still ambiguous at the PATH level (both spellings name real,
        distinct files somewhere in this diff), so the fence still fails
        closed even though :func:`diff_path_spelling` would resolve the
        anchor to the sibling without complaint.
        """
        valid_lines = {("b/x.py", 9): 9, ("x.py", 10): 10}
        line_texts = {("b/x.py", 9): "subline", ("x.py", 10): "topline"}
        finding = {
            "file": "b/x.py",
            "line": 10,
            "end_line": 10,
            "suggested_fix_code": "changed",
        }
        self.assertEqual(
            post_review._suggested_fix_gate(
                finding,
                apply_range=(10, 10),
                path_lookup="x.py",
                facts=diff_facts(valid_lines, line_texts=line_texts),
            ),
            (False, "no_diff_oracle"),
        )

    def test_off_diff_sibling_residual_still_validates_the_fence(self):
        """RATIFIED (issue #229): the diff has no real ``b/x.py`` at all —
        only its stripped sibling ``x.py`` — so the finding's raw spelling is
        not itself a diff path and the ambiguity check does not fire. The
        fence still cross-resolves and validates against ``x.py``'s own text,
        exactly as before #229; a change here must be deliberate, not
        incidental.
        """
        valid_lines = {("x.py", 10): 10}
        line_texts = {("x.py", 10): "topline"}
        finding = {
            "file": "b/x.py",
            "line": 10,
            "end_line": 10,
            "suggested_fix_code": "changed",
        }
        self.assertEqual(
            post_review._suggested_fix_gate(
                finding,
                apply_range=(10, 10),
                path_lookup="x.py",
                facts=diff_facts(valid_lines, line_texts=line_texts),
            ),
            (True, None),
        )

    def test_missing_file_field_is_never_ambiguous(self):
        """A finding missing its ``file`` key entirely still reaches this
        gate (``report_patches.py`` admits such findings — see its L470-472
        candidate filter, which only requires ``suggested_fix_code``), so this
        goes through the REAL call site (``_suggested_fix_gate``, which reads
        ``finding.get("file", "?")``) rather than asserting on
        ``path_is_ambiguous`` in isolation. ``"?"`` has no ``a/``/``b/``
        prefix, so the predicate never fires for it and the gate falls
        through to the ordinary range check instead of raising ``KeyError``.
        """
        finding = {
            "line": 2,
            "end_line": 3,
            "suggested_fix_code": "    return 2\n    # done",
        }
        self.assertEqual(
            self._gate(finding, path_lookup="?"),
            (False, "range_not_in_diff"),
        )

    # -- the vocabulary is closed -----------------------------------------

    def test_every_reason_constant_is_in_the_closed_set(self):
        self.assertEqual(
            post_review._FIX_REASONS,
            frozenset(
                {
                    "non_string",
                    "empty",
                    "carriage_return",
                    "redacted",
                    "marker_shaped",
                    "missing_end_line",
                    "invalid_range",
                    "no_diff_oracle",
                    "range_not_in_diff",
                    "anchor_mismatch",
                    "span_exceeds_platform_cap",
                    "no_op_replacement",
                    "indentation_mismatch",
                    "replacement_too_large",
                    "overlaps_kept_fence",
                }
            ),
        )

    def test_the_closed_vocabulary_has_fifteen_members(self):
        """Adding a reason is a deliberate act — this is the tripwire that says
        so out loud."""
        self.assertEqual(len(post_review._FIX_REASONS), 15)


class TestGatedFindingRejectsUnknownReason(unittest.TestCase):
    """``_gated_finding`` consults ``_FIX_REASONS`` at every downgrade.

    A typo'd reason string in a future edit to ``_suggested_fix_gate`` must
    fail loudly at the FIRST downgrade it produces, not get silently recorded
    into the stable warning line. This is what makes ``_FIX_REASONS`` more
    than a comment other tests happen to pin.
    """

    def test_a_reason_outside_the_closed_vocabulary_raises(self):
        finding = {"file": "foo.py", "line": 2, "suggested_fix_code": "x"}
        with (
            patch(
                "gauntlet.delivery.post._suggested_fix_gate",
                return_value=(False, "bogus"),
            ),
            self.assertRaises(ValueError) as ctx,
        ):
            post_review._gated_finding(finding, (2, 2), diff_facts({}, line_texts={}))
        self.assertIn("bogus", str(ctx.exception))

    def test_a_renamed_anchor_failure_is_checked_against_the_same_set(self):
        """``mismatch_reason`` renames one gate outcome; it cannot widen the
        vocabulary the warning line's readers rely on."""
        finding = {"file": "foo.py", "line": 2, "suggested_fix_code": "x"}
        with (
            patch(
                "gauntlet.delivery.post._suggested_fix_gate",
                return_value=(False, "anchor_mismatch"),
            ),
            self.assertRaises(ValueError) as ctx,
        ):
            post_review._gated_finding(
                finding, (2, 2), diff_facts({}, line_texts={}), mismatch_reason="bogus"
            )
        self.assertIn("bogus", str(ctx.exception))

    def test_a_typo_d_demote_reason_is_checked_against_the_same_set(self):
        """``demote_reason`` (#223) is consulted only when the gate PASSES — it
        cannot widen the vocabulary either, exactly like ``mismatch_reason``."""
        finding = {"file": "foo.py", "line": 2, "suggested_fix_code": "x"}
        with (
            patch(
                "gauntlet.delivery.post._suggested_fix_gate",
                return_value=(True, None),
            ),
            self.assertRaises(ValueError) as ctx,
        ):
            post_review._gated_finding(
                finding, (2, 2), diff_facts({}, line_texts={}), demote_reason="bogus"
            )
        self.assertIn("bogus", str(ctx.exception))


class TestGatedFindingDemoteReason(unittest.TestCase):
    """``_gated_finding``'s ``demote_reason`` keyword (#223).

    A set-level caller (a poster's overlap pre-pass) forces a fence that PASSED
    the per-finding gate to downgrade anyway, through the SAME tally/warn/strip
    tail an ordinary gate failure uses — never a second, parallel strip path.
    """

    def _finding(self, **over):
        finding = {
            "file": "foo.py",
            "line": 2,
            "end_line": 3,
            "suggested_fix_code": "fixed",
        }
        finding.update(over)
        return finding

    def test_demote_reason_none_is_a_no_op_when_the_gate_passes(self):
        """The default keeps every pre-#223 caller byte-identical."""
        finding = self._finding()
        with patch("gauntlet.delivery.post._fence_verdict", return_value=(True, None)):
            result = post_review._gated_finding(
                finding, (2, 3), diff_facts({}, line_texts={})
            )
        self.assertIs(result, finding)
        self.assertEqual(post_review._FIX_COUNTS["kept"], 1)
        self.assertEqual(post_review._FIX_COUNTS["downgraded"], 0)

    def test_a_set_demote_reason_downgrades_a_gate_pass(self):
        finding = self._finding()
        with (
            patch("gauntlet.delivery.post._fence_verdict", return_value=(True, None)),
            patch("gauntlet.delivery.post.warn_skip") as mock_warn,
        ):
            result = post_review._gated_finding(
                finding,
                (2, 3),
                diff_facts({}, line_texts={}),
                demote_reason=post_review._FIX_OVERLAPS_KEPT_FENCE,
            )
        self.assertIsNot(result, finding)
        self.assertNotIn("suggested_fix_code", result)
        self.assertEqual(post_review._FIX_COUNTS["kept"], 0)
        self.assertEqual(post_review._FIX_COUNTS["downgraded"], 1)
        self.assertEqual(post_review._FIX_REASON_COUNTS.get("overlaps_kept_fence"), 1)
        mock_warn.assert_called_once_with(
            "suggested-fix downgraded: foo.py:2 (overlaps_kept_fence)"
        )

    def test_a_gate_failure_keeps_its_own_reason_over_demote_reason(self):
        """Per-fence reasons win: a demote_reason is consulted only on an ``ok``
        gate outcome, so a genuine gate failure is never masked by it. Mutate
        this by deleting the ``if ok:`` guard around the demote_reason branch —
        the failing finding's ``missing_end_line`` becomes ``overlaps_kept_fence``
        and this test goes red.
        """
        finding = self._finding()
        with (
            patch(
                "gauntlet.delivery.post._fence_verdict",
                return_value=(False, "missing_end_line"),
            ),
            patch("gauntlet.delivery.post.warn_skip") as mock_warn,
        ):
            post_review._gated_finding(
                finding,
                (2, 3),
                diff_facts({}, line_texts={}),
                demote_reason=post_review._FIX_OVERLAPS_KEPT_FENCE,
            )
        self.assertEqual(post_review._FIX_REASON_COUNTS.get("missing_end_line"), 1)
        self.assertIsNone(post_review._FIX_REASON_COUNTS.get("overlaps_kept_fence"))
        mock_warn.assert_called_once_with(
            "suggested-fix downgraded: foo.py:2 (missing_end_line)"
        )

    def tearDown(self):
        post_review.reset_run_state()


class TestGitLabFenceOffsets(unittest.TestCase):
    """``_gitlab_fence_offsets``: the one producer of GitLab's ``-m+n`` pair.

    GitLab resolves the header against ``position.new_line``, so the pair is a
    function of the ANCHOR and the stated range — never of the finding alone.
    """

    def test_a_single_line_range_needs_no_offsets(self):
        self.assertEqual(post_review._gitlab_fence_offsets(2, 2, 2), ((0, 0), False))

    def test_a_span_below_the_anchor(self):
        self.assertEqual(post_review._gitlab_fence_offsets(2, 2, 4), ((0, 2), False))

    def test_a_span_above_the_anchor(self):
        """Unit-only: every delivery path anchors a finding at its own ``line``,
        so ``m`` is 0 everywhere it is reachable today. The helper still answers
        for an anchor inside the range, because the anchor is its input."""
        self.assertEqual(post_review._gitlab_fence_offsets(4, 2, 4), ((2, 0), False))

    def test_an_anchor_before_the_range_is_unrealizable(self):
        self.assertEqual(post_review._gitlab_fence_offsets(1, 2, 4), (None, False))

    def test_an_anchor_after_the_range_is_unrealizable(self):
        self.assertEqual(post_review._gitlab_fence_offsets(5, 2, 4), (None, False))

    def test_the_cap_is_inclusive(self):
        cap = post_review._GITLAB_SUGGESTION_OFFSET_CAP
        self.assertEqual(
            post_review._gitlab_fence_offsets(2, 2, 2 + cap), ((0, cap), False)
        )

    def test_one_line_past_the_cap_is_cap_exceeded_not_unrealizable(self):
        """GitLab CLAMPS an offset above the cap instead of rejecting it, so a
        header carrying one would apply a range it does not state. The second
        return is what lets that failure be named."""
        cap = post_review._GITLAB_SUGGESTION_OFFSET_CAP
        self.assertEqual(post_review._gitlab_fence_offsets(2, 2, 3 + cap), (None, True))

    def test_an_above_offset_past_the_cap_is_cap_exceeded(self):
        cap = post_review._GITLAB_SUGGESTION_OFFSET_CAP
        anchor = 2 + cap + 1
        self.assertEqual(
            post_review._gitlab_fence_offsets(anchor, 2, anchor), (None, True)
        )

    def test_a_non_integer_bound_is_not_a_cap_failure(self):
        """A missing or non-integer bound is the gate's business
        (``missing_end_line`` / ``invalid_range``); the helper only declines to
        answer, which leaves the single anchored line as the apply range."""
        for end_line in (None, "3", 3.0, True):
            with self.subTest(end_line=end_line):
                self.assertEqual(
                    post_review._gitlab_fence_offsets(2, 2, end_line), (None, False)
                )


class TestGitLabAnchoredDecision(unittest.TestCase):
    """``_gitlab_anchored`` — the whole GitLab render-site decision, once.

    Both the poster and the benchmark's payload mirror call it, so the mirror
    cannot drift into fiction that stays green.
    """

    def setUp(self):
        parsed = _parse_fixture(GL_DIFF_INDENTED, platform="gitlab")
        parsed_facts = parsed
        self.facts = parsed_facts
        self.valid_lines = parsed_facts.valid_lines
        self.line_texts = parsed_facts.line_texts

    def _finding(self, **over):
        finding = {
            "file": "foo.py",
            "line": 2,
            "end_line": 3,
            "title": "T",
            "body": "b",
            "suggested_fix_code": "    return 2\n    # done",
        }
        finding.update(over)
        return finding

    def _anchored(self, finding, anchor=2):
        return post_review._gitlab_anchored(
            finding, anchor, diff_facts(self.valid_lines, line_texts=self.line_texts)
        )

    def test_a_kept_fence_comes_with_the_offsets_that_realize_its_range(self):
        gated, offsets = self._anchored(self._finding())
        self.assertIn("suggested_fix_code", gated)
        self.assertEqual(offsets, (0, 1))

    def test_it_never_mutates_the_finding_it_is_given(self):
        """Offsets travel out of band. A key written onto the finding would move
        every delivery key it seeds — `_key_material_finding` renders the
        ORIGINAL dict, not this copy."""
        finding = self._finding()
        before = dict(finding)
        self._anchored(finding)
        self.assertEqual(finding, before)

    def test_a_downgrade_leaves_the_input_untouched(self):
        """The strip happens on a copy — the caller's dict still carries the
        field, and the render-time offsets are moot once the fence is gone."""
        finding = self._finding(suggested_fix_code="    return 1\n    # tail")
        gated, offsets = self._anchored(finding)
        self.assertNotIn("suggested_fix_code", gated)
        self.assertIn("suggested_fix_code", finding)
        self.assertEqual(offsets, (0, 1))

    def test_an_unrealizable_span_falls_back_to_the_single_anchored_line(self):
        """The anchor is outside the stated range (unreachable from the poster,
        which anchors every finding at its own line), so no header expresses it:
        the gate judges the one line the position really carries."""
        gated, offsets = self._anchored(self._finding(), anchor=1)
        self.assertNotIn("suggested_fix_code", gated)
        self.assertIsNone(offsets)

    def test_demote_reason_passes_through_to_a_kept_fence(self):
        """#223: a caller with a set-level overlap decision states it here,
        exactly as it would at a GitHub render site."""
        gated, offsets = post_review._gitlab_anchored(
            self._finding(),
            2,
            diff_facts(self.valid_lines, line_texts=self.line_texts),
            demote_reason=post_review._FIX_OVERLAPS_KEPT_FENCE,
        )
        self.assertNotIn("suggested_fix_code", gated)
        self.assertEqual(offsets, (0, 1))
        post_review.reset_run_state()


class TestGithubApplyRange(unittest.TestCase):
    """``_github_apply_range`` — GitHub's multi-line/apply_range decision, once
    (#223/#224). Extracted verbatim from ``post_github``'s render loop; the
    render loop, the overlap pre-pass, and the benchmark's payload mirror all
    call it rather than each computing their own copy.
    """

    def setUp(self):
        parsed = _parse_fixture(GH_DIFF_INDENTED, platform="github")
        self.valid_lines = parsed.valid_lines

    def test_a_valid_multi_line_span_is_multiline(self):
        self.assertEqual(
            post_review._github_apply_range(
                diff_facts(self.valid_lines), "foo.py", 2, 3
            ),
            (True, (2, 3)),
        )

    def test_no_end_line_is_single_line(self):
        self.assertEqual(
            post_review._github_apply_range(
                diff_facts(self.valid_lines), "foo.py", 2, None
            ),
            (False, (2, 2)),
        )

    def test_end_line_equal_to_line_is_single_line(self):
        self.assertEqual(
            post_review._github_apply_range(
                diff_facts(self.valid_lines), "foo.py", 2, 2
            ),
            (False, (2, 2)),
        )

    def test_an_end_line_outside_the_diff_falls_back_to_single_line(self):
        self.assertEqual(
            post_review._github_apply_range(
                diff_facts(self.valid_lines), "foo.py", 2, 940
            ),
            (False, (2, 2)),
        )


class TestOverlapLosers(unittest.TestCase):
    """``_overlap_losers`` — the pure, first-wins overlap resolver (#223).

    Records are ``(index, path_lookup, apply_range)``, already known to be
    candidates (a real, gate-passing apply range) — the resolver itself never
    consults ``suggested_fix_code`` or the gate; that filtering is each
    poster's/the mirror's candidate predicate, tested separately (see the
    poster-level overlap tests).
    """

    def test_first_wins_when_two_records_overlap(self):
        losers = post_review._overlap_losers(
            [(0, "foo.py", (2, 4)), (1, "foo.py", (3, 5))]
        )
        self.assertEqual(losers, {1})

    def test_same_line_single_line_pair_collides(self):
        """Matches GitLab's own ``Range#overlaps?``: two single-line fences on
        the identical line collide."""
        losers = post_review._overlap_losers(
            [(0, "foo.py", (5, 5)), (1, "foo.py", (5, 5))]
        )
        self.assertEqual(losers, {1})

    def test_touching_disjoint_ranges_both_keep_their_fences(self):
        """``[1, 3]`` and ``[4, 6]`` share no line index — GitLab's own
        ``Range#overlaps?`` does not conflict them, and neither does this."""
        losers = post_review._overlap_losers(
            [(0, "foo.py", (1, 3)), (1, "foo.py", (4, 6))]
        )
        self.assertEqual(losers, set())

    def test_a_loser_occupies_nothing_so_it_cannot_block_a_later_record(self):
        """A[1,5] B[4,8] C[7,10] in that order: B collides with A (4<=5) and is
        demoted, but a demoted record never claims its interval — C is judged
        only against the KEPT set {A}, and C does NOT overlap A (7 > 5), so C
        survives even though it overlaps B, which never got to keep [4,8].
        Keeps A and C; only B is a loser (memo R4's worked example).
        """
        losers = post_review._overlap_losers(
            [
                (0, "foo.py", (1, 5)),
                (1, "foo.py", (4, 8)),
                (2, "foo.py", (7, 10)),
            ]
        )
        self.assertEqual(losers, {1})

    def test_records_on_different_paths_never_collide(self):
        losers = post_review._overlap_losers(
            [(0, "foo.py", (2, 4)), (1, "bar.py", (2, 4))]
        )
        self.assertEqual(losers, set())

    def test_cross_spelling_collision_via_path_lookup(self):
        """The resolver keys strictly on the ``path_lookup`` VALUE it is given —
        the same key the gate itself uses (``diff_path_spelling``) — so two
        records built from differently-spelled raw findings that a poster
        already resolved to the same diff path collide correctly."""
        losers = post_review._overlap_losers(
            [(0, "src/edited.py", (2, 4)), (1, "src/edited.py", (3, 5))]
        )
        self.assertEqual(losers, {1})

    def test_no_records_demotes_nobody(self):
        self.assertEqual(post_review._overlap_losers([]), set())


class TestRangesOverlap(unittest.TestCase):
    """``_ranges_overlap`` — the closed-interval intersection both
    ``_overlap_losers`` and the GitLab corroborator query (#223 R6) share."""

    def test_identical_single_line_ranges_overlap(self):
        self.assertTrue(post_review._ranges_overlap((5, 5), (5, 5)))

    def test_touching_disjoint_ranges_do_not_overlap(self):
        self.assertFalse(post_review._ranges_overlap((1, 3), (4, 6)))

    def test_partial_overlap(self):
        self.assertTrue(post_review._ranges_overlap((1, 5), (4, 8)))

    def test_one_range_containing_the_other_overlaps(self):
        self.assertTrue(post_review._ranges_overlap((1, 10), (4, 6)))


class TestPosterOraclesAreRequiredArguments(unittest.TestCase):
    """Both posters require one ``DiffFacts`` or ``None`` argument, with no default."""

    def _defaults(self, func):
        return {
            name: p.default
            for name, p in inspect.signature(func).parameters.items()
            if p.default is not inspect.Parameter.empty
        }

    def test_post_github_has_no_defaulted_arguments(self):
        self.assertEqual(self._defaults(post_review.post_github), {})

    def test_post_gitlab_has_no_defaulted_arguments(self):
        self.assertEqual(self._defaults(post_review.post_gitlab), {})


class _FixGateRunBase(_DryRunTestBase):
    """Drives the real ``main()`` over a diff and returns payload + streams."""

    PLATFORM = "github"
    DIFF = GH_DIFF_INDENTED

    def _run(
        self,
        findings,
        dry_run=True,
        diff=None,
        versions=None,
        payloads=None,
        prior=None,
        **fake_run_kwargs,
    ):
        if versions is None and self.PLATFORM == "gitlab":
            versions = GL_CONTRACT_VERSIONS
        return _run_main(
            Path(self.tmp),
            self.forge_factory,
            {
                "platform": self.PLATFORM,
                "owner": "o",
                "repo": "r",
                "pr_number": 5,
                "review_body": "Summary",
                "sha": "a" * 40,
                "findings": findings,
            },
            dry_run=dry_run,
            diff=self.DIFF if diff is None else diff,
            versions=versions,
            payloads=payloads,
            entries=prior_notes(prior, "a" * 40),
            **fake_run_kwargs,
        )

    def _finding(self, **over):
        finding = {
            "file": "foo.py",
            "line": 2,
            "end_line": 3,
            "severity": "high",
            "title": "Range bug",
            "body": "Body",
            "suggestion": "Return two instead.",
            "suggested_fix_code": "    return 2\n    # done",
        }
        finding.update(over)
        return finding

    def _comment_body(self, run):
        return run.payload["payload"]["comments"][0]["body"]

    def _bodies(self, run):
        posts = (
            run.payload["payload"]["comments"]
            if self.PLATFORM == "github"
            else run.payload["discussions"]
        )
        return [post["body"] for post in posts]

    def _path(self, run, index):
        if self.PLATFORM == "github":
            return run.payload["payload"]["comments"][index]["path"]
        return run.payload["discussions"][index]["position"]["new_path"]

    def _assert_downgraded(self, run, reason, where="foo.py:2"):
        self.assertIn(
            f"suggested-fix downgraded: {where} ({reason})", run.payload["skipped"]
        )


class _SuggestedFixSharedProofs:
    def test_a_failed_diff_fetch_downgrades_the_fence(self):
        run = self._run([self._finding()], diff_rc=1)
        body = self._bodies(run)[0]
        self.assertNotIn(_FENCE, body)
        self.assertIn("Return two instead.", body)
        self._assert_downgraded(run, "no_diff_oracle")

    def test_body_section_entries_lose_the_fence(self):
        if self.PLATFORM == "github":
            primary = {
                "file": "foo.py",
                "line": 999,
                "severity": "high",
                "title": "A",
                "body": "Body A",
                "consolidation_key": "foo.py:0",
                "consolidation_primary": True,
            }
            corroborator = self._finding(
                title="B",
                consolidation_key="foo.py:0",
                consolidation_primary=False,
            )
            findings = [primary, corroborator]
            reason = "anchor_mismatch"
            where = "foo.py:2"
        else:
            findings = [self._finding(line=999, end_line=999)]
            reason = "range_not_in_diff"
            where = "foo.py:999"
        run = self._run(findings)
        body = (
            run.payload["payload"]["body"]
            if self.PLATFORM == "github"
            else run.payload["summary"]["body"]
        )
        self.assertEqual(self._bodies(run), [])
        self.assertIn("could not be anchored inline", body)
        self.assertNotIn(_FENCE, body)
        self._assert_downgraded(run, reason, where=where)


class TestGitHubSuggestedFixGate(_SuggestedFixSharedProofs, _FixGateRunBase):
    """The GitHub inline path: the fence survives only at the anchor it states."""

    def test_multi_line_fix_is_kept_at_a_matching_multi_line_anchor(self):
        run = self._run([self._finding()])
        comment = run.payload["payload"]["comments"][0]
        self.assertEqual(comment["start_line"], 2)
        self.assertEqual(comment["line"], 3)
        self.assertIn(_FENCE, comment["body"])
        self.assertIn("    return 2\n    # done", comment["body"])
        self.assertEqual(run.payload["skipped"], [])

    def test_single_line_fix_is_kept_at_a_single_line_anchor(self):
        run = self._run([self._finding(end_line=2, suggested_fix_code="    return 2")])
        comment = run.payload["payload"]["comments"][0]
        self.assertNotIn("start_line", comment)
        self.assertIn(_FENCE, comment["body"])

    def test_a_range_outside_the_diff_loses_the_fence_with_the_range(self):
        """The anchor decision is made ABOVE the body render, so the gate sees the
        range the comment really applies at. ``end_line`` outside the hunk degrades
        the comment to single-line — and ``range_not_in_diff`` precedes
        ``anchor_mismatch``, so the range failure is what the downgrade names.
        """
        run = self._run([self._finding(end_line=940)])
        comment = run.payload["payload"]["comments"][0]
        self.assertNotIn("start_line", comment)
        self.assertEqual(comment["line"], 2)
        self.assertNotIn(_FENCE, comment["body"])
        self._assert_downgraded(run, "range_not_in_diff")

    def test_the_gate_sees_the_anchor_the_comment_really_carries(self):
        """Why the anchor decision is hoisted above the body render.

        A gate that judged the STATED range instead would agree with the anchor
        only by luck: here the second finding states 2..940 and the comment it
        produces applies at line 2 alone.

        The overlap pre-pass (#223) calls the gate once per candidate, in group
        order, ahead of the render loop; the render loop then gates both
        findings again, in the same order. `seen` asserts the FULL sequence —
        pre-pass calls followed by render-site calls — so a render loop that
        swapped in a bare `primary` and skipped its own gate call still goes
        red, even though the pre-pass calls alone would otherwise mask it.
        """
        seen = []
        real = post_review._suggested_fix_gate

        def spy(finding, **kwargs):
            seen.append(kwargs["apply_range"])
            return real(finding, **kwargs)

        with patch("gauntlet.delivery.post._suggested_fix_gate", side_effect=spy):
            run = self._run([self._finding(), self._finding(end_line=940)])
        anchors = [
            (c.get("start_line", c["line"]), c["line"])
            for c in run.payload["payload"]["comments"]
        ]
        self.assertEqual(anchors, [(2, 3), (2, 2)])
        # The pre-pass gates both candidates first (in group order), THEN the
        # render loop gates both findings again (in the same order) — the
        # full sequence, not just its tail, so a render loop that swapped in
        # a bare `primary` (skipping its own gate call) still goes red even
        # though the pre-pass calls alone would otherwise mask it.
        self.assertEqual(seen, anchors + anchors)

    def test_the_prose_suggestion_still_renders_after_a_downgrade(self):
        run = self._run([self._finding(end_line=940)])
        body = self._comment_body(run)
        self.assertIn("**Suggested fix:**", body)
        self.assertIn("Return two instead.", body)

    def test_edge_blank_lines_reach_the_fence_intact(self):
        """Stated == checked == applied: the gate measured these bytes, so the
        fence carries exactly them (less the one terminating newline)."""
        code = "\n    return 2\n    # done\n\n"
        run = self._run([self._finding(suggested_fix_code=code)])
        self.assertEqual(run.payload["skipped"], [])
        self.assertIn(
            "```suggestion\n\n    return 2\n    # done\n\n```", self._comment_body(run)
        )

    def test_group_primary_keeps_its_fence_in_the_group_body(self):
        """A group comment anchors on the PRIMARY's range, and only the primary's
        fence exists in the body (`_render_corroboration` emits none)."""
        primary = self._finding(
            consolidation_key="foo.py:0", consolidation_primary=True
        )
        corroborator = {
            "file": "foo.py",
            "line": 3,
            "severity": "medium",
            "title": "B",
            "body": "Body B",
            "agent": "bug-detector",
            "dimension": "correctness",
            "confidence": 70,
            "consolidation_key": "foo.py:0",
            "consolidation_primary": False,
        }
        run = self._run([primary, corroborator])
        body = self._comment_body(run)
        self.assertIn(_FENCE, body)
        self.assertEqual(body.count(_FENCE), 1)
        self.assertIn("Corroborating finding", body)

    def test_reasons_reachable_through_the_delivery_path(self):
        secret = "ghp_" + "A" * 24
        cases = [
            ("non_string", self._finding(suggested_fix_code=42)),
            ("empty", self._finding(suggested_fix_code="   ")),
            (
                "redacted",
                self._finding(suggested_fix_code=f"    token = {secret!r}"),
            ),
            ("missing_end_line", self._finding(end_line=None)),
            ("invalid_range", self._finding(line=3, end_line=2)),
            (
                "no_op_replacement",
                self._finding(suggested_fix_code="    return 1\n    # tail"),
            ),
            (
                "indentation_mismatch",
                self._finding(suggested_fix_code="\treturn 2\n\t# done"),
            ),
            (
                "replacement_too_large",
                self._finding(
                    suggested_fix_code="\n".join(
                        f"    line{n}" for n in range(post_review._FIX_MAX_LINES + 1)
                    )
                ),
            ),
        ]
        for reason, finding in cases:
            with self.subTest(reason=reason):
                run = self._run([finding])
                where = f"foo.py:{finding.get('line')}"
                self.assertNotIn(_FENCE, self._comment_body(run))
                self._assert_downgraded(run, reason, where=where)

    # -- the apply-check readout -------------------------------------------

    def test_stdout_reports_both_halves_of_the_acceptance_rate(self):
        run = self._run([self._finding(), self._finding(end_line=940, line=2)])
        self.assertIn("  1 suggested fix(es) passed the apply-check.", run.out)
        self.assertIn("  1 suggested fix(es) downgraded to prose.", run.out)

    def test_the_readout_claims_no_delivery_in_either_mode(self):
        """These two lines report the GATE's verdict, not an outcome on the
        forge — so neither carries a delivery verb, and both read the same live
        and dry-run. The per-platform count lines above own delivery."""
        for dry_run in (True, False):
            with self.subTest(dry_run=dry_run):
                run = self._run([self._finding()], dry_run=dry_run)
                self.assertIn("  1 suggested fix(es) passed the apply-check.", run.out)
                self.assertNotIn("fence(s) posted", run.out)
                self.assertNotIn("fence(s) captured", run.out)

    def test_nothing_printed_when_no_finding_carried_the_field(self):
        finding = self._finding()
        del finding["suggested_fix_code"]
        run = self._run([finding])
        self.assertIsNone(run.exit_code)
        self.assertNotIn("apply-check", run.out)
        self.assertNotIn("downgraded to prose", run.out)

    def test_the_gate_is_identical_under_dry_run_and_live(self):
        """``validate_position``'s precedent: a check that runs only under
        --dry-run cannot be the thing that makes --dry-run trustworthy."""
        findings = [self._finding(), self._finding(line=2, end_line=940)]
        dry = self._run(findings)
        payloads = []
        live = self._run(findings, dry_run=False, payloads=payloads)
        live_bodies = [c["body"] for c in payloads[0]["comments"]]
        dry_bodies = [c["body"] for c in dry.payload["payload"]["comments"]]
        self.assertEqual(live_bodies, dry_bodies)
        self.assertIn("(range_not_in_diff)", live.err)

    def test_caller_supplied_fences_go_through_the_same_gate(self):
        """One path, no fork: a hand-assembled findings JSON is gated exactly as
        an agent-emitted one (requirement 1 — previously unvalidated fences
        posted straight through)."""
        run = self._run([self._finding(suggested_fix_code="    return 1\n    # tail")])
        self.assertNotIn(_FENCE, self._comment_body(run))
        self._assert_downgraded(run, "no_op_replacement")


class TestGitLabSuggestedFixGate(_SuggestedFixSharedProofs, _FixGateRunBase):
    """A GitLab position is ALWAYS single-line; the fence header is what widens
    the apply range. ``suggestion:-m+n`` replaces ``[anchor - m, anchor + n]``
    (#219), so the offsets are derived from the anchor the discussion is posted
    at and the gate judges the range they realize."""

    PLATFORM = "gitlab"
    DIFF = GL_DIFF_INDENTED

    # Computed from the pre-S2 render and pinned independently of the key helper under test.
    RANGE_BUG_KEY = "1422e1e3b48521d1"
    PRIMARY_A_KEY = "3c008a7625ca81b2"

    def test_single_line_fix_is_kept(self):
        run = self._run([self._finding(end_line=2, suggested_fix_code="    return 2")])
        self.assertIn(_FENCE, run.payload["discussions"][0]["body"])
        self.assertEqual(run.payload["skipped"], [])

    def test_multi_line_fix_ships_an_offset_header(self):
        """The finding anchors at 2 and states 2..3, so one click must replace
        both lines — which the HEADER says, not the position (#219)."""
        run = self._run([self._finding()])
        self.assertIn(
            "```suggestion:-0+1\n    return 2\n    # done\n```",
            run.payload["discussions"][0]["body"],
        )
        self.assertEqual(run.payload["skipped"], [])
        self.assertIn("  1 suggested fix(es) passed the apply-check.", run.out)

    def test_the_offset_header_leaves_the_position_alone(self):
        """The widening lives entirely in the fence: GitLab resolves the header
        against ``position.new_line``, so the position is byte-identical to the
        single-line case — no ``line_range``, no new keys."""
        multi = self._run([self._finding()])
        single = self._run(
            [self._finding(end_line=2, suggested_fix_code="    return 2")]
        )
        self.assertEqual(
            multi.payload["discussions"][0]["position"],
            single.payload["discussions"][0]["position"],
        )

    def test_every_emitted_header_is_centred_on_the_position_it_ships_with(self):
        """The wire-level invariant behind the body factory: ``deliver`` renders
        with the SAME line it writes into ``position.new_line``, so a header can
        never state a range that anchor does not centre."""
        findings = [
            self._finding(),
            self._finding(end_line=2, suggested_fix_code="    return 2"),
        ]
        run = self._run(findings)
        headers = 0
        for finding, discussion in zip(
            findings, run.payload["discussions"], strict=True
        ):
            match = re.search(r"```suggestion:-(\d+)\+(\d+)", discussion["body"])
            if match is None:
                continue
            anchor = discussion["position"]["new_line"]
            self.assertEqual(anchor - int(match.group(1)), finding["line"])
            self.assertEqual(anchor + int(match.group(2)), finding["end_line"])
            headers += 1
        self.assertEqual(headers, 1)

    def test_the_offset_header_is_identical_under_dry_run_and_live(self):
        """``validate_position``'s precedent, extended to the new path: the live
        body is the dry-run body plus the delivery marker, nothing else."""
        findings = [self._finding()]
        dry_body = self._run(findings).payload["discussions"][0]["body"]
        payloads = []
        self._run(findings, dry_run=False, payloads=payloads)
        live_body = next(p["body"] for p in payloads if "position" in p)
        self.assertIn("```suggestion:-0+1", dry_body)
        self.assertTrue(live_body.startswith(dry_body))

    def test_a_span_past_the_platform_cap_is_downgraded_by_name(self):
        """Hand-assembled: the pipeline's own ``maxLineSpan`` intake bound
        (default 100) drops a span this wide upstream, so only caller-supplied
        JSON reaches here. GitLab CLAMPS an offset above its cap rather than
        rejecting it, so the header would apply a range it does not state."""
        cap = post_review._GITLAB_SUGGESTION_OFFSET_CAP
        finding = self._finding(
            end_line=2 + cap + 1, suggested_fix_code="    patched\n    also patched"
        )
        run = self._run([finding], diff=GL_DIFF_LONG)
        self.assertNotIn(_FENCE, run.payload["discussions"][0]["body"])
        self._assert_downgraded(run, "span_exceeds_platform_cap")

    def test_no_emitted_header_ever_states_an_offset_past_the_cap(self):
        """The render-level invariant the cap exists for, over a whole payload:
        the span one line inside the cap ships, the one line outside it does
        not, and nothing in between leaks a clamped offset."""
        cap = post_review._GITLAB_SUGGESTION_OFFSET_CAP
        run = self._run(
            [
                self._finding(end_line=2 + cap, suggested_fix_code="    patched"),
                self._finding(end_line=2 + cap + 1, suggested_fix_code="    patched"),
            ],
            diff=GL_DIFF_LONG,
        )
        self.assertEqual(
            re.findall(r"suggestion:-(\d+)\+(\d+)", json.dumps(run.payload)),
            [("0", str(cap))],
        )

    def test_an_out_of_diff_span_past_the_cap_still_reports_the_range(self):
        """Check order is unchanged: the STATED range is judged against the diff
        before the anchor, so a span that is both out-of-diff and past the cap
        reports what it always reported."""
        cap = post_review._GITLAB_SUGGESTION_OFFSET_CAP
        run = self._run([self._finding(end_line=2 + cap + 1)])
        self._assert_downgraded(run, "range_not_in_diff")

    def test_a_forged_offsets_key_on_the_finding_changes_nothing(self):
        """Offsets are derived from the anchor, never read from the findings
        JSON — which is caller-supplied and flows in unfiltered. An in-band key
        would let a hand-assembled payload widen a range the gate approved."""
        fix = {"end_line": 2, "suggested_fix_code": "    return 2"}
        honest = self._run([self._finding(**fix)])
        forged = self._run(
            [
                self._finding(
                    **fix, fence_offsets=[0, 40], _fence_offsets=[0, 40], above=9
                )
            ]
        )
        self.assertEqual(forged.payload["discussions"], honest.payload["discussions"])

    def test_a_rerun_that_delivers_nothing_claims_no_delivery(self):
        """The bug the readout's wording caused: every discussion is already on
        the MR from an earlier run, so nothing is posted — but the body is still
        rendered, so the gate still runs and still counts. Saying "posted" there
        was a false claim."""
        finding = self._finding(end_line=2, suggested_fix_code="    return 2")
        run = self._run(
            [finding],
            dry_run=False,
            prior=PriorDelivery(
                True, frozenset({self.RANGE_BUG_KEY}), frozenset(), None
            ),
        )
        self.assertIn("  0 inline discussion(s) posted.", run.out)
        self.assertIn("  1 suggested fix(es) passed the apply-check.", run.out)
        self.assertNotIn("fence(s) posted", run.out)

    def test_corroborator_is_gated_on_its_own_anchor(self):
        """When the group's discussion is lost, each corroborator falls back to
        its own discussion — anchored at its OWN line, so that is the range its
        fence must state."""
        primary = {
            "file": "foo.py",
            "line": 2,
            "severity": "high",
            "title": "A",
            "body": "Body A",
            "consolidation_key": "foo.py:0",
            "consolidation_primary": True,
        }
        keeps = {
            "file": "foo.py",
            "line": 3,
            "end_line": 3,
            "severity": "medium",
            "title": "Keeps",
            "body": "Body B",
            "agent": "bug-detector",
            "dimension": "correctness",
            "confidence": 70,
            "consolidation_key": "foo.py:0",
            "consolidation_primary": False,
            "suggested_fix_code": "    # replaced",
        }
        payloads = []
        run = self._run(
            [primary, keeps],
            dry_run=False,
            payloads=payloads,
            discussion_rcs=[1],
        )
        # The failed GROUP body renders the corroborator's title too, so the
        # fallback is the one that is NOT a group body.
        bodies = [p["body"] for p in payloads if "position" in p]
        fallback = [
            b for b in bodies if "Keeps" in b and "Corroborating finding" not in b
        ]
        self.assertEqual(len(fallback), 1)
        self.assertIn(_FENCE, fallback[0])
        self.assertIn("  1 suggested fix(es) passed the apply-check.", run.out)

    def test_a_group_body_carries_the_primarys_offset_header_once(self):
        """One discussion carries the whole group, anchored on the PRIMARY — so
        the primary's offsets are the body's, and a corroborator (which renders
        no fence at all) adds none."""
        primary = self._finding(
            consolidation_key="foo.py:0", consolidation_primary=True
        )
        corroborator = {
            "file": "foo.py",
            "line": 3,
            "end_line": 3,
            "severity": "medium",
            "title": "B",
            "body": "Body B",
            "agent": "bug-detector",
            "dimension": "correctness",
            "confidence": 70,
            "consolidation_key": "foo.py:0",
            "consolidation_primary": False,
            "suggested_fix_code": "    # replaced",
        }
        body = self._run([primary, corroborator]).payload["discussions"][0]["body"]
        self.assertEqual(body.count(_FENCE), 1)
        self.assertIn("```suggestion:-0+1", body)
        self.assertIn("Corroborating finding", body)

    def test_a_fence_bearing_rerun_still_recognizes_its_own_delivery(self):
        """A finding that ships an offset header today was keyed, before #219,
        off a render that never carried a fence at all — `_key_material_finding`
        strips the field unconditionally, so the standing marker still matches
        and the discussion is not posted a second time (#132)."""
        finding = self._finding()
        run = self._run(
            [finding],
            dry_run=False,
            prior=PriorDelivery(
                True, frozenset({self.RANGE_BUG_KEY}), frozenset(), None
            ),
        )
        self.assertIn("  0 inline discussion(s) posted.", run.out)
        self.assertIn("  1 inline discussion(s) already on the MR", run.out)
        self.assertIn("  1 suggested fix(es) passed the apply-check.", run.out)

    def test_corroborator_with_a_multi_line_range_is_offset_at_its_own_anchor(self):
        """The primary sits at line 2 and the corroborator's span at 5..6 — a
        header measured from the PRIMARY's anchor (3, 4) would be a different,
        unrealizable claim than one measured from the corroborator's own line
        (0, 1). Sharing an anchor between the two would leave this unable to
        tell which one the offsets actually came from."""
        primary = {
            "file": "foo.py",
            "line": 2,
            "severity": "high",
            "title": "A",
            "body": "Body A",
            "consolidation_key": "foo.py:0",
            "consolidation_primary": True,
        }
        spanning = {
            "file": "foo.py",
            "line": 5,
            "end_line": 6,
            "severity": "medium",
            "title": "Spans",
            "body": "Body B",
            "agent": "bug-detector",
            "dimension": "correctness",
            "confidence": 70,
            "consolidation_key": "foo.py:0",
            "consolidation_primary": False,
            "suggested_fix_code": "    return 2\n    # done",
        }
        payloads = []
        self._run(
            [primary, spanning],
            dry_run=False,
            diff=GL_DIFF_LONG,
            payloads=payloads,
            discussion_rcs=[1],
        )
        bodies = [p["body"] for p in payloads if "position" in p]
        fallback = [
            b for b in bodies if "Spans" in b and "Corroborating finding" not in b
        ]
        self.assertEqual(len(fallback), 1)
        # Its own fallback discussion anchors at its OWN line, so that is the
        # anchor the header's offsets are measured from.
        self.assertIn("```suggestion:-0+1", fallback[0])

    def test_unanchored_note_never_carries_a_fence(self):
        """A position-less ``/notes`` POST has nothing to apply against."""
        primary = {
            "file": "foo.py",
            "line": 2,
            "severity": "high",
            "title": "A",
            "body": "Body A",
            "consolidation_key": "foo.py:0",
            "consolidation_primary": True,
        }
        unanchored = {
            "file": "foo.py",
            "severity": "medium",
            "title": "Unanchored",
            "body": "Body B",
            "agent": "bug-detector",
            "dimension": "correctness",
            "confidence": 70,
            "consolidation_key": "foo.py:0",
            "consolidation_primary": False,
            "suggested_fix_code": "    # replaced",
        }
        payloads = []
        self._run(
            [primary, unanchored],
            dry_run=False,
            payloads=payloads,
            prior=PriorDelivery(
                True, frozenset({self.PRIMARY_A_KEY}), frozenset(), None
            ),
        )
        notes = [p["body"] for p in payloads if "position" not in p]
        unanchored_notes = [b for b in notes if "Unanchored" in b]
        self.assertEqual(len(unanchored_notes), 1)
        unanchored_note = unanchored_notes[0]
        trailer = "\u2694\ufe0f *Code Gauntlet*"
        self.assertEqual(unanchored_note.count(trailer), 1)
        self.assertIn(f"{trailer}\n\n<!-- code-gauntlet-finding-key:", unanchored_note)
        self.assertNotIn(_FENCE, "".join(notes))


class _OverlapDemotionProofs:
    def test_the_later_overlapping_fence_demotes(self):
        a = self._finding(
            title="First",
            line=2,
            end_line=4,
            suggested_fix_code="    a2\n    a3\n    a4",
        )
        b = self._finding(
            title="Second",
            line=3,
            end_line=5,
            suggested_fix_code="    b3\n    b4\n    b5",
        )
        run = self._run([a, b])
        bodies = self._bodies(run)
        self.assertEqual(len(bodies), 2)
        self.assertIn(_FENCE, bodies[0])
        self.assertNotIn(_FENCE, bodies[1])
        self._assert_downgraded(run, "overlaps_kept_fence", where="foo.py:3")
        self.assertIn("  1 suggested fix(es) passed the apply-check.", run.out)
        self.assertIn("  1 suggested fix(es) downgraded to prose.", run.out)

    def test_touching_disjoint_ranges_both_keep_their_fences(self):
        a = self._finding(
            title="First", line=2, end_line=3, suggested_fix_code="    a2\n    a3"
        )
        b = self._finding(
            title="Second", line=4, end_line=5, suggested_fix_code="    b4\n    b5"
        )
        run = self._run([a, b])
        bodies = self._bodies(run)
        self.assertIn(_FENCE, bodies[0])
        self.assertIn(_FENCE, bodies[1])
        self.assertEqual(run.payload["skipped"], [])

    def test_a_fenceless_finding_never_blocks_a_later_fence(self):
        fenceless = {
            "file": "foo.py",
            "line": 3,
            "severity": "low",
            "title": "No fence",
            "body": "No suggested_fix_code on this one.",
        }
        fenced = self._finding(
            title="Fenced",
            line=3,
            end_line=5,
            suggested_fix_code="    b3\n    b4\n    b5",
        )
        run = self._run([fenceless, fenced])
        bodies = self._bodies(run)
        self.assertEqual(len(bodies), 2)
        self.assertIn(_FENCE, bodies[1])
        self.assertNotIn("overlaps_kept_fence", "\n".join(run.payload["skipped"]))

    def test_a_gate_failing_finding_never_blocks_a_later_fence(self):
        failing = self._finding(
            title="Fails its own gate",
            line=3,
            end_line=940,
            suggested_fix_code="x",
        )
        fenced = self._finding(
            title="Fenced",
            line=3,
            end_line=5,
            suggested_fix_code="    b3\n    b4\n    b5",
        )
        run = self._run([failing, fenced])
        bodies = self._bodies(run)
        self.assertNotIn(_FENCE, bodies[0])
        self.assertIn(_FENCE, bodies[1])
        self._assert_downgraded(run, "range_not_in_diff", where="foo.py:3")
        self.assertNotIn("overlaps_kept_fence", "\n".join(run.payload["skipped"]))

    def test_a_non_candidate_ahead_of_the_pair_does_not_shift_the_index_basis(self):
        """A fence-less finding occupies its position in the full delivery order.

        The later candidates must retain those original indexes when the pre-pass
        records winners. Mutant: number only candidates in the platform overlap
        pre-pass, shifting both entries and demoting the winner.
        """
        fenceless = {
            "file": "foo.py",
            "line": 6,
            "severity": "low",
            "title": "No fence",
            "body": "No suggested_fix_code on this one.",
        }
        a = self._finding(
            title="First",
            line=2,
            end_line=4,
            suggested_fix_code="    a2\n    a3\n    a4",
        )
        b = self._finding(
            title="Second",
            line=3,
            end_line=5,
            suggested_fix_code="    b3\n    b4\n    b5",
        )
        run = self._run([fenceless, a, b])
        bodies = self._bodies(run)
        self.assertEqual(len(bodies), 3)
        self.assertIn(_FENCE, bodies[1])
        self.assertNotIn(_FENCE, bodies[2])
        self._assert_downgraded(run, "overlaps_kept_fence", where="foo.py:3")


class TestGitHubOverlapDemotion(_OverlapDemotionProofs, _FixGateRunBase):
    """Two kept fences whose apply ranges overlap in the same file (#223):
    the LATER one in delivery order (= array order — consolidate_delivery
    does not sort) demotes to prose; the comment it rode in on still posts.
    """

    DIFF = GH_DIFF_OVERLAP

    def test_a_lineless_candidate_is_skipped_by_the_prepass(self):
        """The pre-pass runs two early-exits before it can compute an
        apply_range: ``if line is None: continue`` and, right after,
        ``if not is_line_valid(...): continue``. For a lineless candidate,
        ``is_line_valid`` alone already excludes it — ``(filepath, None)`` is
        never a key in ``valid_lines`` — so deleting the ``line is None``
        check BY ITSELF leaves this test green; it is currently defensive,
        not independently load-bearing. What the pair jointly prevents is
        real: ``end_line`` below is an INTEGER specifically so that removing
        BOTH early-exits reaches ``_github_apply_range``, whose
        ``end_line >= line`` comparison (``int >= None``) raises
        ``TypeError`` for a lineless candidate rather than being politely
        skipped (verified by mutation, not by reading — see the PR record)."""
        a = {
            "file": "foo.py",
            "severity": "low",
            "title": "No line",
            "body": "No line number at all.",
            "suggested_fix_code": "x",
            "end_line": 5,
        }
        b = self._finding(
            title="Fenced",
            line=3,
            end_line=5,
            suggested_fix_code="    b3\n    b4\n    b5",
        )
        run = self._run([a, b])
        comments = run.payload["payload"]["comments"]
        self.assertEqual(
            len(comments), 1, "the lineless finding is skipped, not posted"
        )
        self.assertIn(_FENCE, comments[0]["body"])

    def test_an_off_diff_candidate_is_skipped_by_the_prepass(self):
        """The pre-pass's own ``is_line_valid`` guard is defensive, not
        load-bearing: an off-diff apply_range already fails `_fence_verdict`'s
        own gate (``range_not_in_diff``), so a finding whose OWN line is
        off-diff is never a CANDIDATE regardless of this guard — deleting it
        alone leaves this test green. What actually keeps the finding out of
        ``comments`` is post_github's separate per-primary line check in the
        render loop itself, which runs whether or not the pre-pass's guard
        exists. This pins the outcome (no interval claimed, not posted), not
        that this particular guard is what produces it."""
        a = self._finding(
            title="Off diff", line=999, end_line=999, suggested_fix_code="x"
        )
        b = self._finding(
            title="Fenced",
            line=3,
            end_line=5,
            suggested_fix_code="    b3\n    b4\n    b5",
        )
        run = self._run([a, b])
        comments = run.payload["payload"]["comments"]
        self.assertEqual(
            len(comments), 1, "the off-diff finding is skipped, not posted"
        )
        self.assertIn(_FENCE, comments[0]["body"])


class TestGitLabOverlapDemotion(_OverlapDemotionProofs, _FixGateRunBase):
    """GitLab's dry-run equivalent of ``TestGitHubOverlapDemotion`` — the same
    demotion decision through ``_gitlab_anchored``/``deliver``.
    """

    PLATFORM = "gitlab"
    DIFF = GL_DIFF_OVERLAP

    # Computed from the pre-S2 render and pinned independently of key_material_body.
    FIRST_KEY = "4e747ece3feb9629"
    CORROBORATOR_KEY = "5e05d974eb959dd6"
    PRIMARY_KEY = "680eba8695bad92c"
    REACTIVE_CORROBORATOR_KEY = "db44929d16839554"
    SECOND_KEY = "46708f208c1414bf"

    def test_partial_rerun_demotion_is_independent_of_delivery_state(self):
        """#223 R8: the demoted SET is a pure function of findings + diff,
        computed before ``gitlab_prior_delivery`` is even fetched — and
        ``gitlab_prior_delivery`` does not fetch AT ALL under ``--dry-run``
        (its own docstring), so this must run LIVE to exercise it. Here the
        SURVIVOR (first in delivery order) is already on the MR from an
        earlier run — its own discussion is not reposted — but the LATER,
        overlapping finding still demotes on this rerun exactly as it would
        on a first run; the demotion does not depend on what has or hasn't
        gone out live.
        """
        a = self._finding(
            title="First",
            line=2,
            end_line=4,
            suggested_fix_code="    a2\n    a3\n    a4",
        )
        b = self._finding(
            title="Second",
            line=3,
            end_line=5,
            suggested_fix_code="    b3\n    b4\n    b5",
        )
        payloads = []
        run = self._run(
            [a, b],
            dry_run=False,
            prior=PriorDelivery(True, frozenset({self.FIRST_KEY}), frozenset(), None),
            payloads=payloads,
        )
        self.assertIsNone(run.exit_code)
        discussion_bodies = [p["body"] for p in payloads if "position" in p]
        # A's key is already delivered, so deliver()'s fast path renders/gates
        # it (the summary below proves it rendered KEPT) but never posts it —
        # only B's discussion reaches the wire, and it must still carry the
        # demotion regardless of A's delivery state.
        self.assertEqual(len(discussion_bodies), 1)
        self.assertNotIn(_FENCE, discussion_bodies[0])
        self.assertIn(
            "suggested-fix downgraded: foo.py:3 (overlaps_kept_fence)", run.err
        )
        self.assertIn("  1 suggested fix(es) passed the apply-check.", run.out)
        self.assertIn("  1 suggested fix(es) downgraded to prose.", run.out)

    def test_partial_delivery_threads_the_loser_demotion_through_its_own_group(self):
        """#223: the overlap LOSER can also be the undelivered half of its
        OWN group's partial-delivery split — a different code path than
        ``test_partial_rerun_demotion_is_independent_of_delivery_state``
        above (there, the loser was a single-member group with no
        corroborator, reaching the full ``deliver(..., corroborators, ...)``
        call at the bottom of the loop). Here A[2,4] is kept; P[3,5],
        group k1's primary, is the overlap LOSER; k1's corroborator (line 6)
        is already delivered but P itself is not — the "some but not all"
        branch posts P alone through ``body_factory(f,
        demote_reason=demote_reason)``, and THAT threading is what this
        pins. Mutation: drop the ``demote_reason=demote_reason`` kwarg from
        that call (posting ``body_factory(f)`` instead) — RED (P's fence
        renders instead of demoting).
        """
        a = self._finding(
            title="First",
            line=2,
            end_line=4,
            suggested_fix_code="    a2\n    a3\n    a4",
        )
        p = self._finding(
            title="Second",
            line=3,
            end_line=5,
            suggested_fix_code="    b3\n    b4\n    b5",
            consolidation_key="k1",
            consolidation_primary=True,
        )
        corroborator = {
            "file": "foo.py",
            "line": 6,
            "severity": "medium",
            "title": "Corroborator",
            "body": "Body C",
            "agent": "bug-detector",
            "dimension": "correctness",
            "confidence": 70,
            "consolidation_key": "k1",
        }
        payloads = []
        run = self._run(
            [a, p, corroborator],
            dry_run=False,
            # Only the corroborator's key is already delivered — P's own key
            # is not, so k1's "some but not all" branch delivers P alone.
            prior=PriorDelivery(
                True, frozenset({self.CORROBORATOR_KEY}), frozenset(), None
            ),
            payloads=payloads,
        )
        self.assertIsNone(run.exit_code)
        discussion_bodies = [pl["body"] for pl in payloads if "position" in pl]
        # A posts kept; the corroborator is already_present (no repost); P
        # posts alone, demoted.
        self.assertEqual(len(discussion_bodies), 2)
        self.assertIn(_FENCE, discussion_bodies[0])
        self.assertNotIn(_FENCE, discussion_bodies[1])
        fallback = discussion_bodies[1]
        self.assertIn(
            f"**{post_review.SEVERITY_EMOJI['high']} [HIGH] Second**", fallback
        )
        self.assertNotIn("Corroborator", fallback)
        self.assertNotIn("\n\n---\n\n", fallback)
        # The prose suggestion still ships — only the one-click fence is
        # withheld (``_finding``'s default ``suggestion`` text, unchanged).
        self.assertIn("**Suggested fix:**\nReturn two instead.", discussion_bodies[1])
        self.assertIn(
            "suggested-fix downgraded: foo.py:3 (overlaps_kept_fence)", run.err
        )

    def test_a_reactively_promoted_corroborator_queries_the_kept_interval_map(self):
        """#223 R6: a corroborator promoted to its own discussion (here, via the
        partial-delivery split — its group's primary already delivered, itself
        not) is a REACTIVE fence site: it was never a candidate in the pre-pass
        (only a group's primary is), so it QUERIES the read-only
        ``kept_intervals`` map built from the primary and demotes when its own
        stated interval intersects it. Mutate ``deliver_corroborator`` to drop
        that query (always pass ``demote_reason=None``) and this goes red.
        """
        primary = self._finding(
            title="Primary",
            line=2,
            end_line=4,
            suggested_fix_code="    a2\n    a3\n    a4",
            consolidation_key="k1",
            consolidation_primary=True,
        )
        corroborator = self._finding(
            title="Corroborator",
            line=3,
            end_line=5,
            suggested_fix_code="    b3\n    b4\n    b5",
            consolidation_key="k1",
        )
        self.assertNotEqual(self.PRIMARY_KEY, self.REACTIVE_CORROBORATOR_KEY)
        payloads = []
        run = self._run(
            [primary, corroborator],
            dry_run=False,
            # Primary already delivered; corroborator is not — the "some but
            # not all" split that routes the corroborator through
            # deliver_corroborator instead of the group's own discussion.
            prior=PriorDelivery(True, frozenset({self.PRIMARY_KEY}), frozenset(), None),
            payloads=payloads,
        )
        self.assertIsNone(run.exit_code)
        discussion_bodies = [p["body"] for p in payloads if "position" in p]
        self.assertEqual(len(discussion_bodies), 1, "only the corroborator posts")
        self.assertNotIn(_FENCE, discussion_bodies[0])
        self.assertIn(
            "suggested-fix downgraded: foo.py:3 (overlaps_kept_fence)", run.err
        )

    def test_a_reactively_promoted_corroborator_ignores_a_losers_phantom_interval(
        self,
    ):
        """#223 R6 / the ``kept_intervals`` guard: the interval map reactive
        corroborator sites consult carries only WINNING candidates' apply
        ranges — a LOSER's own range must never occupy anything (a
        demoted record "occupies nothing", :func:`_overlap_losers`'s own
        docstring). Winner A[2,4] keeps; loser P[3,5] (group k2's primary,
        already delivered so its own group never re-renders) overlaps A and
        demotes; group k2's corroborator [5,5] overlaps ONLY P's [3,5]
        range, not A's [2,4] — so it must KEEP its fence. Mutate the
        ``if index not in losers:`` guard away (append every record's
        interval unconditionally) and P's phantom [3,5] wrongly demotes the
        corroborator too — RED.
        """
        a = self._finding(
            title="First",
            line=2,
            end_line=4,
            suggested_fix_code="    a2\n    a3\n    a4",
        )
        p = self._finding(
            title="Second",
            line=3,
            end_line=5,
            suggested_fix_code="    b3\n    b4\n    b5",
            consolidation_key="k2",
            consolidation_primary=True,
        )
        corroborator = self._finding(
            title="Corroborator",
            line=5,
            end_line=5,
            suggested_fix_code="    fixed5",
            consolidation_key="k2",
        )
        payloads = []
        run = self._run(
            [a, p, corroborator],
            dry_run=False,
            # P's own key is already delivered — its group never re-renders,
            # but the corroborator's key is not, so it splits off through
            # deliver_corroborator.
            prior=PriorDelivery(True, frozenset({self.SECOND_KEY}), frozenset(), None),
            payloads=payloads,
        )
        self.assertIsNone(run.exit_code)
        discussion_bodies = [pl["body"] for pl in payloads if "position" in pl]
        # A posts kept; P's group is already_present (no repost); the
        # corroborator posts on its own via deliver_corroborator.
        self.assertEqual(len(discussion_bodies), 2)
        self.assertIn(_FENCE, discussion_bodies[0])  # A
        self.assertIn(_FENCE, discussion_bodies[1])  # corroborator keeps its fence
        self.assertNotIn(
            "suggested-fix downgraded: foo.py:5 (overlaps_kept_fence)", run.err
        )


class TestGitHubFencePathAmbiguity(_FixGateRunBase):
    """Issue #229: a real top-level ``b/`` directory's ``b/foo.py`` alongside a
    separate real ``foo.py`` in the SAME diff makes a finding spelled
    ``b/foo.py`` ambiguous — neither its exact spelling nor the stripped
    fallback can say which file a patch targets, so the FENCE fails closed
    even though the comment still posts (the anchor is unaffected — see
    ``diff_path_spelling``'s docstring)."""

    DIFF = GH_DIFF_FENCE_COLLISION

    def test_exact_hit_collision_downgrades_the_fence(self):
        """``b/foo.py``'s OWN key validates the finding's line directly — the
        verified one-click-corruption case #229 fixes: the fence used to
        validate against ``b/foo.py``'s real text while a patch may have
        meant ``foo.py``.

        Mutation: gut ``path_is_ambiguous`` to ``return False``
        unconditionally — RED (the fence renders instead of downgrading).
        """
        run = self._run(
            [
                self._finding(
                    file="b/foo.py", line=2, end_line=2, suggested_fix_code="CHANGED"
                )
            ]
        )
        comment = run.payload["payload"]["comments"][0]
        self.assertEqual(comment["path"], "b/foo.py")
        self.assertNotIn(_FENCE, comment["body"])
        self._assert_downgraded(run, "no_diff_oracle", where="b/foo.py:2")

    def test_exact_miss_collision_downgrades_the_fence_but_anchor_still_resolves(
        self,
    ):
        """``b/foo.py`` has no line 3 of its own — the finding validates ONLY
        through the stripped fallback to ``foo.py``'s line 3 (today's silent
        cross-file case). The ANCHOR still resolves there (ratified), but the
        fence must still fail closed regardless.

        Mutation: revert post_github's path resolution back to raw
        ``primary["file"]`` — RED (``comment["path"]`` becomes ``b/foo.py``,
        a path GitHub does not have).
        """
        run = self._run(
            [
                self._finding(
                    file="b/foo.py", line=3, end_line=3, suggested_fix_code="CHANGED"
                )
            ]
        )
        comment = run.payload["payload"]["comments"][0]
        self.assertEqual(comment["path"], "foo.py")
        self.assertNotIn(_FENCE, comment["body"])
        self._assert_downgraded(run, "no_diff_oracle", where="b/foo.py:3")


class _FencePathRecallProofs:
    def test_prefixed_finding_keeps_its_fence(self):
        run = self._run(
            [
                self._finding(
                    file="b/src/edited.py",
                    line=2,
                    end_line=2,
                    suggested_fix_code="CHANGED",
                )
            ]
        )
        self.assertEqual(self._path(run, 0), "src/edited.py")
        self.assertIn(_FENCE, self._bodies(run)[0])
        self.assertEqual(run.payload["skipped"], [])


class TestGitHubFencePathRecall(_FencePathRecallProofs, _FixGateRunBase):
    """A prefixed finding with NO colliding sibling in the diff must still
    validate — both its anchor and its fence — exactly as before #229 (the
    ambiguity check is scoped to genuine two-file collisions, not to every
    prefixed spelling). The GitHub twin of ``TestGitlabFindingPathNormalization``,
    extended to cover the FENCE as well as the position."""

    DIFF = GH_DIFF_PREFIXED_NO_COLLISION


class TestGitLabFencePathAmbiguity(_FixGateRunBase):
    """The verbatim/GitLab twin of ``TestGitHubFencePathAmbiguity``: a real
    top-level ``a/`` directory alongside a separate real ``x.py``, with no
    ``diff --git`` line at all (producer detection from the diff's own bytes
    is a ``report_patches.py`` concept — ``post_review.py`` is told the
    platform directly)."""

    PLATFORM = "gitlab"
    DIFF = GL_DIFF_FENCE_COLLISION

    def test_collision_downgrades_the_fence(self):
        run = self._run(
            [
                self._finding(
                    file="a/x.py", line=2, end_line=2, suggested_fix_code="CHANGED"
                )
            ]
        )
        discussion = run.payload["discussions"][0]
        # The ANCHOR is unaffected by the fence's ambiguity: "a/x.py" is an
        # EXACT hit on its own diff key here, so the position keeps the
        # finding's own spelling — only the fence fails closed.
        self.assertEqual(discussion["position"]["new_path"], "a/x.py")
        self.assertNotIn(_FENCE, discussion["body"])
        self._assert_downgraded(run, "no_diff_oracle", where="a/x.py:2")


class TestGitLabFencePathRecall(_FencePathRecallProofs, _FixGateRunBase):
    """The verbatim/GitLab twin of ``TestGitHubFencePathRecall``."""

    PLATFORM = "gitlab"
    DIFF = GL_DIFF_PREFIXED_NO_COLLISION


class TestGitHubMultilineAnchorUsesResolvedPath(_FixGateRunBase):
    """The multi-line anchor decision (``post_github``'s ``multiline`` local)
    now validates against the RESOLVED filepath, not the finding's raw
    spelling — a second #229 delta, distinct from the fence-ambiguity gate
    (this shape has no path collision at all: `foo.py` and `b/foo.py` share
    no line, so neither spelling is ambiguous).

    `foo.py` carries lines {1, 2}; the real top-level `b/foo.py` carries
    {3, 4} — two SEPARATE files. A finding spelled `b/foo.py` stating 1..3
    resolves, AT ITS OWN LINE (1), to `foo.py` — so the whole range is
    validated against `foo.py` and correctly finds line 3 missing there,
    posting a single-line comment. Before #229's path-resolution fix, the
    UNRESOLVED raw spelling let `range_is_valid` accept a range that mixed
    BOTH files' line sets (1, 2 via the stripped fallback to `foo.py`; 3 as
    an EXACT hit on `b/foo.py` itself) and posted a multi-line comment
    naming `b/foo.py` lines 1 and 2, which that file does not have.

    Mutation: revert post_github's path resolution back to raw
    ``primary["file"]`` (post_review.py's ``filepath = diff_path_spelling(...)``
    line) — RED (the payload reverts to the old multi-line shape).
    """

    DIFF = GH_DIFF_MULTILINE_ANCHOR_COLLISION

    def test_a_range_spanning_two_files_anchors_single_line_on_the_resolved_file(
        self,
    ):
        finding = {
            "file": "b/foo.py",
            "line": 1,
            "end_line": 3,
            "severity": "high",
            "title": "Wide Range",
            "body": "Body",
        }
        run = self._run([finding])
        comment = run.payload["payload"]["comments"][0]
        self.assertEqual(comment["path"], "foo.py")
        self.assertEqual(comment["line"], 1)
        self.assertNotIn("start_line", comment)
        self.assertNotIn("start_side", comment)


class TestDeliveryKeysAreFenceIndependent(_GitlabLiveRunBase):
    """Delivery keys must not depend on ``suggested_fix_code`` AT ALL (#63 D2).

    Prior-delivery dedup (#132/#208) is retry-safe only while a finding's key is
    the same across runs and across delivery shapes. Gating the field before the
    key render would make the key depend on the gate's verdict; stripping it
    unconditionally makes it depend on nothing.
    """

    def _keys(self, findings):
        payloads = []
        run = self._run_main(findings=findings, payloads=payloads)
        self.assertIsNone(run.exit_code)
        markers = [
            review_marker.find_finding_marker(p["body"])
            for p in payloads
            if "position" in p
        ]
        return [m["key"] for m in markers if m]

    def test_key_is_byte_equal_with_and_without_the_field(self):
        plain = dict(GL_CONTRACT_FINDINGS[0])
        with_fix = dict(plain, end_line=61, suggested_fix_code="patched_ctx")
        self.assertEqual(self._keys([with_fix]), self._keys([plain]))

    def test_key_is_byte_equal_whether_the_gate_kept_or_stripped_the_fence(self):
        # The stripped arm states a range the diff does not contain: since #219 a
        # 61..62 span is KEPT on GitLab (the header widens the apply range), so a
        # merely-wider range would leave both arms on the same side of the gate
        # and say nothing.
        plain = dict(GL_CONTRACT_FINDINGS[0])
        kept = dict(plain, end_line=61, suggested_fix_code="patched_ctx")
        stripped = dict(plain, end_line=999, suggested_fix_code="patched_ctx")

        def _keys_and_body(findings):
            payloads = []
            run = self._run_main(findings=findings, payloads=payloads)
            self.assertIsNone(run.exit_code)
            body = next(p["body"] for p in payloads if "position" in p)
            markers = [
                review_marker.find_finding_marker(p["body"])
                for p in payloads
                if "position" in p
            ]
            return [m["key"] for m in markers if m], body

        kept_keys, kept_body = _keys_and_body([kept])
        stripped_keys, stripped_body = _keys_and_body([stripped])
        # The two arms must actually straddle the gate — a byte-equal key over
        # two runs that landed on the SAME side of it would say nothing about
        # the field being stripped from the key render before the gate's
        # verdict is even known.
        self.assertIn(_FENCE, kept_body)
        self.assertNotIn(_FENCE, stripped_body)
        self.assertEqual(kept_keys, stripped_keys)

    def test_key_is_byte_equal_grouped_and_individual(self):
        """The #132 invariant: a member's key is its own single-finding render,
        whichever shape ships it."""
        member = dict(
            GL_CONTRACT_FINDINGS[1],
            end_line=62,
            suggested_fix_code="patched_add",
        )
        individual = self._keys([member])
        primary = dict(
            GL_CONTRACT_FINDINGS[0],
            consolidation_key="src/edited.py:60",
            consolidation_primary=True,
        )
        grouped_member = dict(
            member,
            consolidation_key="src/edited.py:60",
            consolidation_primary=False,
        )
        payloads = []
        run = self._run_main(findings=[primary, grouped_member], payloads=payloads)
        self.assertIsNone(run.exit_code)
        group_body = next(p["body"] for p in payloads if "position" in p)
        self.assertIn(
            post_review.build_finding_marker("a" * 40, individual[0]), group_body
        )


def test_suggestion_fence_uses_shared_fence_run(monkeypatch):
    monkeypatch.setattr(post_review, "fence_run", lambda _payload: "````")
    assert post_review._suggestion_fence("plain") == ("````suggestion", "````")


class TestGatedFindingWarnLabel(unittest.TestCase):
    """``_gated_finding``'s ``warn_label`` keyword (issue #226): the default
    keeps delivery's warning bytes unchanged; a caller (the report-side gate)
    can substitute its own label so the two records stay distinguishable."""

    def setUp(self):
        post_review.reset_run_state()
        self.addCleanup(post_review.reset_run_state)

    def _finding(self):
        return {
            "file": "f.py",
            "line": 3,
            "suggested_fix_code": "",  # empty after normalization -> "empty"
        }

    def test_default_label_matches_delivery_bytes_exactly(self):
        with patch("gauntlet.delivery.post.warn_skip") as mock_warn:
            post_review._gated_finding(
                self._finding(), (3, 3), diff_facts({}, line_texts={})
            )
        mock_warn.assert_called_once_with("suggested-fix downgraded: f.py:3 (empty)")

    def test_custom_label_replaces_only_the_leading_word(self):
        with patch("gauntlet.delivery.post.warn_skip") as mock_warn:
            post_review._gated_finding(
                self._finding(),
                (3, 3),
                diff_facts({}, line_texts={}),
                warn_label="report-patch",
            )
        mock_warn.assert_called_once_with("report-patch downgraded: f.py:3 (empty)")

    def test_custom_label_never_leaks_into_the_default_caller(self):
        """warn_label is per-call, not a module-level toggle: a caller that
        passes it must not change what a caller relying on the default sees."""
        post_review._gated_finding(
            self._finding(),
            (3, 3),
            diff_facts({}, line_texts={}),
            warn_label="report-patch",
        )
        self.assertIn(
            "report-patch downgraded: f.py:3 (empty)", post_review._SKIP_WARNINGS
        )
        post_review._gated_finding(
            self._finding(), (3, 3), diff_facts({}, line_texts={})
        )
        self.assertIn(
            "suggested-fix downgraded: f.py:3 (empty)", post_review._SKIP_WARNINGS
        )


class TestFixReasonCounts(unittest.TestCase):
    """``_FIX_REASON_COUNTS`` — the per-reason tally the report-side gate
    (scripts/report_patches.py) reads to render a downgrade breakdown."""

    def setUp(self):
        post_review.reset_run_state()
        self.addCleanup(post_review.reset_run_state)

    def test_starts_empty(self):
        self.assertEqual(post_review._FIX_REASON_COUNTS, {})

    def test_tallies_by_reason_across_multiple_downgrades(self):
        empty = {"file": "a.py", "line": 1, "suggested_fix_code": ""}
        also_empty = {"file": "b.py", "line": 2, "suggested_fix_code": "   "}
        no_end_line = {
            "file": "c.py",
            "line": 1,
            "suggested_fix_code": "x",
        }  # no end_line -> missing_end_line

        post_review._gated_finding(empty, (1, 1), diff_facts({}, line_texts={}))
        post_review._gated_finding(also_empty, (2, 2), diff_facts({}, line_texts={}))
        post_review._gated_finding(no_end_line, None, diff_facts({}, line_texts={}))

        self.assertEqual(
            post_review._FIX_REASON_COUNTS,
            {"empty": 2, "missing_end_line": 1},
        )

    def test_a_kept_finding_does_not_tally(self):
        parsed_facts = _parse_fixture(GH_DIFF_INDENTED, platform="github")
        valid_lines = parsed_facts.valid_lines
        line_texts = parsed_facts.line_texts
        finding = {
            "file": "foo.py",
            "line": 2,
            "end_line": 3,
            "suggested_fix_code": "    return 2\n    # done",
        }
        result = post_review._gated_finding(
            finding, (2, 3), diff_facts(valid_lines, line_texts=line_texts)
        )
        self.assertIn("suggested_fix_code", result)
        self.assertEqual(post_review._FIX_REASON_COUNTS, {})

    def test_reset_run_state_clears_the_tally(self):
        post_review._gated_finding(
            {"file": "a.py", "line": 1, "suggested_fix_code": ""},
            (1, 1),
            diff_facts({}, line_texts={}),
        )
        self.assertTrue(post_review._FIX_REASON_COUNTS)
        post_review.reset_run_state()
        self.assertEqual(post_review._FIX_REASON_COUNTS, {})


def _read_payload(directory):
    return json.loads(
        (directory / "post-review-payload.json").read_text(encoding="utf-8")
    )


@pytest.mark.parametrize(
    "platform, sha, review_body",
    [
        pytest.param("github", "a" * 40, "", id="github-empty"),
        pytest.param(
            "github",
            "b" * 40,
            "## Summary\nSome pre-existing narrative text.\n",
            id="github-narrative",
        ),
        pytest.param("gitlab", "c" * 40, "", id="gitlab-empty"),
        pytest.param(
            "gitlab",
            "d" * 40,
            "## MR Review\nContext for the reviewer.\n",
            id="gitlab-narrative",
        ),
    ],
)
def test_review_marker_round_trip_through_real_poster(
    platform: str, sha: str, review_body: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(post_review, "DRY_RUN", True)
    data = _review_data(platform=platform, sha=sha, review_body=review_body)
    if platform == "github":
        post_review.post_github(data, diff_facts({}, line_texts={}), forge=FakeForge())
    else:
        monkeypatch.setattr(
            "gauntlet.delivery.post.fetch_gitlab_shas",
            lambda *_args, **_kwargs: ("base", "head", "start"),
        )
        post_review.post_gitlab(
            data,
            diff_facts({}, line_texts={}, new_files=set(), old_paths={}),
            forge=FakeGitLab(),
        )
    body = post_review._CAPTURED[0].payload["body"]
    signal = review_marker.detect_signal(body)
    assert signal is not None, f"no signal recovered from posted body: {body!r}"
    assert signal["sha"] == sha


@pytest.mark.parametrize(
    "platform, facts, expected_warnings, expected_inline",
    [
        pytest.param(
            "github",
            diff_facts({("src/app.py", 10): 10, ("src/app.py", 20): None}),
            [
                "Skipping finding 'Bug' at src/app.py:99 \u2014 line not found in diff. Valid lines for this file: [10, 20]"
            ],
            0,
            id="github-valid-lines",
        ),
        pytest.param(
            "github",
            diff_facts({}),
            [
                "Skipping finding 'Bug' at src/app.py:99 \u2014 line not found in diff. Valid lines for this file: []"
            ],
            0,
            id="github-empty-lines",
        ),
        pytest.param("github", None, [], 1, id="github-validation-skipped"),
        pytest.param(
            "gitlab",
            diff_facts({("src/app.py", 5): 5, ("src/app.py", 15): None}),
            [
                "Skipping finding 'Bug' at src/app.py:99 \u2014 line not found in diff. Valid lines for this file: [5, 15]"
            ],
            0,
            id="gitlab-valid-lines",
        ),
    ],
)
def test_skip_warning_diagnostics(
    platform: str,
    facts: diff_api.DiffFacts | None,
    expected_warnings: list[str],
    expected_inline: int,
) -> None:
    data = _review_data(
        platform=platform,
        sha="a" * 40,
        review_body="MR review",
        findings=[{"file": "src/app.py", "line": 99, "title": "Bug"}],
    )
    fake = (
        FakeForge()
        if platform == "github"
        else FakeGitLab(refs=[JsonFetch(GL_CONTRACT_VERSIONS, None)])
    )
    if platform == "github":
        post_review.post_github(data, facts, forge=fake)
    else:
        assert isinstance(fake, FakeGitLab)
        post_review.post_gitlab(
            data,
            facts,
            forge=fake,
        )
    assert expected_warnings == post_review._SKIP_WARNINGS
    requests = [call.request for call in fake.calls if call.method == "submit"]
    assert len(requests) == 1
    request = requests[0]
    assert request is not None
    if platform == "github":
        comments = cast("list[dict[str, object]]", request.payload["comments"])
        assert len(comments) == expected_inline
        if comments:
            assert (comments[0]["path"], comments[0]["line"]) == ("src/app.py", 99)
    else:
        assert request.endpoint == "projects/o%2Fr/merge_requests/5/notes"


def test_dry_run_github__dry_run_captures_payload_and_makes_no_post(
    tmp_path: Path, forge_factory: FakeForgeFactory
) -> None:
    finding_a = _finding()
    finding_b = _finding(line=99, severity="low", title="Bug B", body="Body B")
    mock_run = _run_main(
        tmp_path,
        forge_factory,
        _review_data(
            platform="github", review_body="Summary", findings=[finding_a, finding_b]
        ),
        diff=GH_DIFF,
    )

    post_calls = [c for c in mock_run.calls if c.method == "submit"]
    assert post_calls == [], "no POST subprocess call in dry-run"

    payload_path = os.path.join(str(tmp_path), "post-review-payload.json")
    assert os.path.exists(payload_path)
    cap = _read_payload(tmp_path)

    assert cap["platform"] == "github"
    assert cap["endpoint"] == "repos/o/r/pulls/5/reviews"
    assert cap["method"] == "POST"
    assert cap["payload"]["event"] == "COMMENT"
    # Comments must match the live-path rendering byte-for-byte.
    assert len(cap["payload"]["comments"]) == 1
    comment = cap["payload"]["comments"][0]
    assert comment["body"] == render_comment_body(finding_a)
    assert comment["path"] == "foo.py"
    assert comment["line"] == 2
    assert comment["side"] == "RIGHT"

    expected = (
        "Skipping finding 'Bug B' at foo.py:99 "
        "— line not found in diff. Valid lines for this file: [1, 2]"
    )
    assert expected in cap["skipped"]
    bodies = [c["body"] for c in cap["payload"]["comments"]]
    assert render_comment_body(finding_b) not in bodies


def test_dry_run_gitlab__dry_run_captures_summary_and_discussions(
    tmp_path: Path, forge_factory: FakeForgeFactory
) -> None:
    finding_x = _finding(
        file="bar.py", severity="medium", title="Issue X", body="Desc X"
    )
    data = _review_data(
        platform="gitlab", review_body="MR review", findings=[finding_x]
    )
    versions = GL_CONTRACT_VERSIONS
    mock_run = _run_main(tmp_path, forge_factory, data, diff=GL_DIFF, versions=versions)

    post_calls = [c for c in mock_run.calls if c.method == "submit"]
    assert post_calls == [], "no POST subprocess call in dry-run"

    versions_calls = [c for c in mock_run.calls if c.method == "diff_refs"]
    assert versions_calls, "fetch_gitlab_shas versions GET must still run in dry-run"

    cap = _read_payload(tmp_path)
    assert cap["platform"] == "gitlab"
    assert "MR review" in cap["summary"]["body"]
    assert len(cap["discussions"]) == 1
    disc = cap["discussions"][0]
    assert disc["body"] == render_comment_body(finding_x)
    assert disc["position"]["new_path"] == "bar.py"
    assert disc["position"]["new_line"] == 2


def test_inline_poster_boundaries__github_impossible_inline_envelope_dies_before_any_post(
    tmp_path: Path, forge_factory: FakeForgeFactory
) -> None:
    # Mutation: bypass the final UTF-8 body-plus-marker check; post_json would
    # then be called with the oversized review batch.
    data = _review_data(
        platform="github",
        sha=TestInlinePosterBoundaries.SHA,
        review_body="Summary",
        findings=[
            _finding(line=1, title="Too large", body="body"),
            _finding(severity="low", title="Healthy sibling", body="short body"),
        ],
    )
    with (
        patch.dict(
            cast(
                "dict[str, dict[str, object]]",
                fold.PLATFORM_BODY_LIMITS["github"]["surfaces"],
            )["inline"],
            {"bytes": 20},
        ),
        patch("gauntlet.delivery.post.post_json") as post,
    ):
        run = _run_main(
            tmp_path, forge_factory, data, dry_run=False, expect_exit=True, diff=GH_DIFF
        )
    assert run.exit_code == 1
    post.assert_not_called()
    assert "inline review comment" in run.err
    assert "20-byte GitHub body limit" in run.err
    assert "nothing was posted" in run.err


def _delivery_consolidation_findings(
    filepath: str,
) -> tuple[dict[str, object], dict[str, object]]:
    primary: dict[str, object] = {
        "file": filepath,
        "line": 2,
        "severity": "high",
        "title": "A",
        "body": "Body A",
        "consolidation_key": f"{filepath}:0",
        "consolidation_primary": True,
    }
    corroborator: dict[str, object] = {
        "file": filepath,
        "line": 2 if filepath == "bar.py" else 3,
        "severity": "medium",
        "title": "B",
        "body": "Body B",
        "agent": "bug-detector",
        "dimension": "correctness",
        "confidence": 70,
        "consolidation_key": f"{filepath}:0",
        "consolidation_primary": False,
    }
    return primary, corroborator


@pytest.mark.parametrize(
    "platform, review_body, diff, versions, filepath, primary_line, expected_count, expected_anchor, expected_fragments, expected_body",
    [
        (
            "github",
            "Summary",
            GH_DIFF_MULTILINE,
            None,
            "foo.py",
            2,
            1,
            ("foo.py", 2),
            ("A", "B", "Body A", "Body B"),
            "**\U0001f7e0 [HIGH] A**\n\nBody A\n\n---\n\n**Corroborating finding \u2014 bug-detector (correctness, confidence 70):**\n\n**B**\n\nBody B\n\n\u2694\ufe0f *Code Gauntlet*",
        ),
        (
            "github",
            "Summary",
            GH_DIFF_MULTILINE,
            None,
            "foo.py",
            999,
            0,
            None,
            ("could not be anchored inline", "A", "B", "Body A", "Body B"),
            None,
        ),
        (
            "github",
            "Summary",
            GH_DIFF_MULTILINE,
            None,
            "foo.py",
            None,
            0,
            None,
            ("could not be anchored inline", "A", "B", "Body A", "Body B"),
            None,
        ),
        (
            "gitlab",
            "MR review",
            GL_DIFF,
            GL_CONTRACT_VERSIONS,
            "bar.py",
            1,
            1,
            ("bar.py", 1),
            ("A", "B", "Body A", "Body B"),
            "**\U0001f7e0 [HIGH] A**\n\nBody A\n\n---\n\n**Corroborating finding \u2014 bug-detector (correctness, confidence 70):**\n\n**B**\n\nBody B\n\n\u2694\ufe0f *Code Gauntlet*",
        ),
        (
            "gitlab",
            "MR review",
            GL_DIFF,
            GL_CONTRACT_VERSIONS,
            "bar.py",
            999,
            0,
            None,
            ("could not be anchored inline", "A", "B", "Body A", "Body B"),
            None,
        ),
        (
            "gitlab",
            "MR review",
            GL_DIFF,
            GL_CONTRACT_VERSIONS,
            "bar.py",
            None,
            0,
            None,
            ("could not be anchored inline", "A", "B", "Body A", "Body B"),
            None,
        ),
    ],
    ids=[
        "github-group",
        "github-off-diff-primary",
        "github-no-line-primary",
        "gitlab-group",
        "gitlab-off-diff-primary",
        "gitlab-no-line-primary",
    ],
)
def test_delivery_consolidation(
    tmp_path: Path,
    forge_factory: FakeForgeFactory,
    platform: str,
    review_body: str,
    diff: str,
    versions: list[dict[str, str]] | None,
    filepath: str,
    primary_line: int | None,
    expected_count: int,
    expected_anchor: tuple[str, int] | None,
    expected_fragments: tuple[str, ...],
    expected_body: str | None,
) -> None:
    primary, corroborator = _delivery_consolidation_findings(filepath)
    if primary_line is None:
        del primary["line"]
    else:
        primary["line"] = primary_line
    _run_main(
        tmp_path,
        forge_factory,
        _review_data(
            platform=platform,
            review_body=review_body,
            findings=[primary, corroborator],
        ),
        diff=diff,
        versions=versions,
    )

    payload = _read_payload(tmp_path)
    if platform == "github":
        posts = payload["payload"]["comments"]
        body = payload["payload"]["body"]
        anchor = (posts[0]["path"], posts[0]["line"]) if posts else None
    else:
        posts = payload["discussions"]
        body = payload["summary"]["body"]
        anchor = (
            (posts[0]["position"]["new_path"], posts[0]["position"]["new_line"])
            if posts
            else None
        )
    assert (len(posts), anchor) == (expected_count, expected_anchor)
    if expected_body is not None:
        assert posts[0]["body"] == expected_body
    if platform == "github":
        assert "Bug B" not in str(payload["skipped"])
    proof_body = posts[0]["body"] if posts else body
    for fragment in expected_fragments:
        assert fragment in proof_body


@pytest.mark.parametrize(
    "platform, review_body, finding, diff, versions, body_key, expected_fragments",
    [
        (
            "github",
            "Summary",
            _finding(),
            GH_DIFF,
            None,
            "payload",
            (
                "Generated by code-gauntlet",
                "Reviewed up to:",
                "code-gauntlet-findings:",
                "Summary",
            ),
        ),
        (
            "gitlab",
            "MR review",
            _finding(file="bar.py", severity="medium", title="Issue X", body="Desc X"),
            GL_DIFF,
            GL_CONTRACT_VERSIONS,
            "summary",
            (
                "Generated by code-gauntlet",
                "Reviewed up to:",
                "code-gauntlet-findings:",
                "MR review",
            ),
        ),
    ],
    ids=["github-review-body", "gitlab-summary-note"],
)
def test_both_footer_halves_posted(
    tmp_path: Path,
    forge_factory: FakeForgeFactory,
    platform: str,
    review_body: str,
    finding: dict[str, object],
    diff: str,
    versions: list[dict[str, str]] | None,
    body_key: str,
    expected_fragments: tuple[str, ...],
) -> None:
    _run_main(
        tmp_path,
        forge_factory,
        _review_data(platform=platform, review_body=review_body, findings=[finding]),
        diff=diff,
        versions=versions,
    )

    payload = _read_payload(tmp_path)
    body = payload[body_key]["body"]
    for fragment in expected_fragments:
        assert fragment in body


def test_both_footer_halves_posted__review_body_with_the_same_prose_sha_gets_no_second_copy(
    tmp_path: Path, forge_factory: FakeForgeFactory
) -> None:
    """A footer already naming this commit is left alone rather than duplicated."""
    pre_existing = (
        "Some notes.\n\n---\n"
        "Generated by code-gauntlet | Reviewed up to: deadbeefcafe\n"
    )
    finding_a = _finding()
    _run_main(
        tmp_path,
        forge_factory,
        _review_data(platform="github", review_body=pre_existing, findings=[finding_a]),
        diff=GH_DIFF,
    )
    body = _read_payload(tmp_path)["payload"]["body"]
    assert body.count("Generated by code-gauntlet") == 1, (
        f"an exact-sha match must not duplicate: {body!r}"
    )


def test_both_footer_halves_posted__review_body_with_a_stale_prose_sha_still_gets_the_real_one(
    tmp_path: Path, forge_factory: FakeForgeFactory
) -> None:
    """A hand-composed footer naming a DIFFERENT commit must not suppress the
    real one: suppressing it would leave the posted review advertising a
    commit the review never examined. Two lines is the honest outcome — the
    reader takes the last — and only an exact-sha match is deduplicated
    (see test_both_footer_halves_posted__review_body_with_the_same_prose_sha_gets_no_second_copy)."""
    pre_existing = (
        "Some notes.\n\nGenerated by code-gauntlet | Reviewed up to: abc1234\n"
    )
    finding_a = _finding()
    _run_main(
        tmp_path,
        forge_factory,
        _review_data(platform="github", review_body=pre_existing, findings=[finding_a]),
        diff=GH_DIFF,
    )

    body = _read_payload(tmp_path)["payload"]["body"]
    # The real head sha must be present and last, so parse_prose_footer (and
    # any human) reads the commit actually reviewed, not the stale one.
    assert "Reviewed up to: deadbeefcafe" in body
    assert body.rindex("deadbeefcafe") > body.rindex("abc1234")
    signal = review_marker.detect_signal(body)
    assert signal is not None
    assert signal["sha"] == "deadbeefcafe"
    # The mechanical marker is still appended (the guards are independent).
    assert "code-gauntlet-findings:" in body


def test_gitlab_real_a_directory_path__gitlab_real_a_directory_path_is_preserved(
    tmp_path: Path, forge_factory: FakeForgeFactory
) -> None:
    data = _review_data(
        platform="gitlab",
        review_body="MR review",
        findings=[
            {
                "file": "a/foo.py",
                "line": 1,
                "severity": "high",
                "title": "Finding in a real a/ directory",
                "body": "Body",
            }
        ],
    )
    with contextlib.redirect_stdout(io.StringIO()):
        _run_main(
            tmp_path,
            forge_factory,
            data,
            diff=GL_DIFF_REAL_A_DIR,
            versions=GL_CONTRACT_VERSIONS,
        )
    discussions = _read_payload(tmp_path)["discussions"]
    assert len(discussions) == 1, "the finding must not be warn-skipped"
    position = discussions[0]["position"]
    assert position["new_path"] == "a/foo.py"
    assert position["old_path"] == "a/foo.py"
    assert position["old_line"] == 1


def test_gitlab_finding_path_normalization__prefixed_finding_path_ships_normalized_position(
    tmp_path: Path, forge_factory: FakeForgeFactory
) -> None:
    _run_main(
        tmp_path,
        forge_factory,
        _review_data(
            platform="gitlab",
            review_body="MR review",
            findings=[
                {
                    "file": "b/src/edited.py",
                    "line": 61,
                    "severity": "high",
                    "title": "Prefixed-path finding",
                    "body": "Body",
                }
            ],
        ),
        diff=GL_DIFF_CONTRACT,
        versions=GL_CONTRACT_VERSIONS,
    )
    position = _read_payload(tmp_path)["discussions"][0]["position"]
    assert position["new_path"] == "src/edited.py"
    assert position["old_path"] == "src/edited.py"
    assert position["old_line"] == 50


def test_gitlab_summary_idempotency__dry_run_makes_no_idempotency_call_and_always_captures_the_summary(
    tmp_path: Path, forge_factory: FakeForgeFactory
) -> None:
    """The hard "no network in dry-run" pin: the check would say "skip" if it were
    consulted, and build_dry_run_payload's "first capture is the summary" shape
    depends on the note being captured regardless."""
    run = _run_main(
        tmp_path,
        forge_factory,
        {
            "platform": "gitlab",
            "owner": "o",
            "repo": "r",
            "pr_number": 5,
            "sha": "a" * 40,
            "review_body": "MR review",
            "findings": GL_CONTRACT_FINDINGS,
        },
        dry_run=True,
        diff=GL_DIFF_CONTRACT,
        versions=GL_CONTRACT_VERSIONS,
        head_sha="deadbeefcafe\n",
        entries=prior_notes(
            PriorDelivery(True, frozenset(), frozenset(), None), "a" * 40
        ),
    )
    assert [c for c in run.mock_run.calls if c.method == "review_entries"] == []
    assert "code-gauntlet-findings:" in _read_payload(tmp_path)["summary"]["body"]
    assert (
        post_review._CAPTURED[0].payload["body"]
        == _read_payload(tmp_path)["summary"]["body"]
    )


@pytest.mark.parametrize(
    "platform, code_points", [("github", 21846), ("gitlab", 333334)]
)
def test_summary_body_budget_guard(
    platform: str,
    code_points: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Mutations: delete the guard, move it after post_json, or make
    # utf8_len return len; this multi-byte body then passes and turns red.
    # GitHub: 21846 code points are 65538 UTF-8 bytes, over 65536.
    # GitLab: 333334 code points are 1000002 UTF-8 bytes, over 1000000.
    oversized = post_review.ComposedBody("\u754c" * code_points, 0, 0, 0, ())
    monkeypatch.setattr(
        post_review, "compose_review_body", lambda *args, **kwargs: oversized
    )
    fake = (
        FakeForge()
        if platform == "github"
        else FakeGitLab(refs=[JsonFetch(GL_CONTRACT_VERSIONS, None)])
    )
    data = _review_data(pr_number=1, sha="a" * 40)
    with pytest.raises(SystemExit) as exc:
        if platform == "github":
            post_review.post_github(data, diff_facts({}, line_texts={}), forge=fake)
        else:
            assert isinstance(fake, FakeGitLab)
            post_review.post_gitlab(
                data,
                diff_facts({}, line_texts={}, new_files=set(), old_paths={}),
                forge=fake,
            )
    assert exc.value.code == 1
    assert [call for call in fake.calls if call.method == "submit"] == []


def test_summary_body_delivery__report_slice_keeps_summary_authored_h2_until_real_code_heading(
    tmp_path: Path, forge_factory: FakeForgeFactory
) -> None:
    # Mutation: use a prefix match instead of CODE_OWNED_HEADINGS; the authored
    # H2 lines and the real Findings stop point would no longer produce this body.
    report_path = os.path.join(str(tmp_path), "report.md")
    report = (
        "## Summary\n\n"
        "Summary prose\n"
        "## Not a section\n"
        "kept\n"
        "## Findings (finding text)\n"
        "also kept\n\n"
        "## Findings\n\n"
        "### High\n"
    )
    with open(report_path, "w", encoding="utf-8") as fh:
        fh.write(report)
    assert (
        summary_body_from_report(report)
        == "Summary prose\n## Not a section\nkept\n## Findings (finding text)\nalso kept"
    )

    crlf_report = (
        "## Summary\n\n"
        "Summary prose\r\n"
        "## Findings (finding text)\n"
        "forged\n\n"
        "2 finding(s) after the gauntlet — 1 high.\n\n"
        "## Findings\n\n"
        "### High\n"
    )
    assert (
        summary_body_from_report(crlf_report)
        == "Summary prose\r\n## Findings (finding text)\nforged\n\n2 finding(s) after the gauntlet — 1 high."
    )

    _run_main(
        tmp_path,
        forge_factory,
        [],
        args=(
            "--report",
            report_path,
            "--owner",
            "o",
            "--repo",
            "r",
            "--pr-number",
            "5",
            "--platform",
            "github",
            "--sha",
            "a" * 40,
        ),
        diff=GH_DIFF,
    )
    body = _read_payload(tmp_path)["payload"]["body"]
    assert "Summary prose\n## Not a section\nkept" in body
    assert "## Findings (finding text)\nalso kept" in body
    assert "\n## Findings\n" not in body


def test_summary_body_delivery__bare_array_flags_form_the_real_wrapper_in_order(
    tmp_path: Path, forge_factory: FakeForgeFactory
) -> None:
    # Mutation: return the bare array directly or omit one flag assignment; the
    # hand-typed seven-key wrapper and values expose the lost code-owned shape.
    with (
        patch(
            "gauntlet.delivery.post.fetch_diff_facts",
            return_value=None,
        ),
        patch("gauntlet.delivery.post.post_github", return_value=0) as mock_post,
    ):
        _run_main(
            tmp_path,
            forge_factory,
            [],
            args=_FLAG_WRAPPER,
        )
    data = mock_post.call_args.args[0]
    assert mock_post.call_args.kwargs["forge"] is forge_factory("github")
    assert list(data) == [
        "owner",
        "repo",
        "pr_number",
        "sha",
        "platform",
        "review_body",
        "findings",
    ]
    assert data["owner"] == "o"
    assert data["repo"] == "r"
    assert data["pr_number"] == 5
    assert data["sha"] == "a" * 40
    assert data["platform"] == "github"
    assert data["review_body"] == ""
    assert data["findings"] == []


def test_summary_body_delivery__report_summary_fold_closes_four_backtick_fence_before_footer(
    tmp_path: Path, forge_factory: FakeForgeFactory
) -> None:
    # Mutation: bypass fold_review_body in compose_review_body; the footer and
    # hidden marker would then land inside the four-backtick Summary fence.
    report_path = os.path.join(str(tmp_path), "report.md")
    with open(report_path, "w", encoding="utf-8") as fh:
        fh.write("## Summary\n\n" + "````python\n" + "x" * 70000 + "\n\n## Findings\n")
    _run_main(
        tmp_path,
        forge_factory,
        [],
        args=(
            "--report",
            report_path,
            "--owner",
            "o",
            "--repo",
            "r",
            "--pr-number",
            "5",
            "--platform",
            "github",
            "--sha",
            "a" * 40,
        ),
        diff=GH_DIFF,
    )
    body = _read_payload(tmp_path)["payload"]["body"]
    closer = "\n````\n\n"
    fold = "_[folded:"
    marker = "<!-- code-gauntlet-findings:"
    assert closer in body
    assert body.index(closer) < body.index(fold)
    assert body.index(fold) < body.index(marker)
    assert body.endswith(post_review.build_footer(0, "a" * 40, body=""))


@patch(
    "gauntlet.delivery.post.fetch_gitlab_shas",
    return_value=("base", "head", "start"),
)
def test_summary_body_delivery__summary_borne_marker_is_escaped_and_real_footer_wins(
    _shas, tmp_path, forge_factory, monkeypatch
) -> None:
    # Mutation: remove renderer marker neutralization or let the authored marker
    # satisfy the footer reader; both platform captures must then lose the real signal.
    sha = "a" * 40
    review_body = (
        "Summary\n"
        f'&lt;!-- code-gauntlet-findings: {{"version":"3.0","findings_count":999,"sha":"{sha}"}} -->\n'
        f"Generated by code-gauntlet | Reviewed up to: {sha}"
    )
    github_data = _review_data(sha=sha, review_body=review_body)
    post_review.reset_run_state()
    monkeypatch.setattr(post_review, "DRY_RUN", True)
    post_review.post_github(github_data, None, forge=FakeForge())
    github_body = post_review._CAPTURED[0].payload["body"]
    assert isinstance(github_body, str)
    assert "&lt;!--" in github_body
    github_marker = review_marker.find_marker(github_body)
    assert github_marker is not None
    assert github_marker["findings_count"] == 0
    assert github_marker["sha"] == sha

    post_review.reset_run_state()
    monkeypatch.setattr(post_review, "DRY_RUN", True)
    gitlab_data = _review_data(sha=sha, review_body=review_body)
    post_review.post_gitlab(gitlab_data, None, forge=FakeGitLab())
    gitlab_body = post_review._CAPTURED[0].payload["body"]
    assert isinstance(gitlab_body, str)
    assert "&lt;!--" in gitlab_body
    gitlab_marker = review_marker.find_marker(gitlab_body)
    assert gitlab_marker is not None
    assert gitlab_marker["findings_count"] == 0
    assert gitlab_marker["sha"] == sha


def _github_skipped_findings_degrade_findings():
    inline = _finding(title="Inline bug")
    off_diff = _finding(line=99, severity="medium", title="Off-diff bug", body="Body B")
    return inline, off_diff


def test_github_skipped_findings_degrade__skipped_finding_lands_in_body_with_both_counts(
    tmp_path: Path, forge_factory: FakeForgeFactory
) -> None:
    inline, off_diff = _github_skipped_findings_degrade_findings()
    _run_main(
        tmp_path,
        forge_factory,
        _review_data(
            platform="github", review_body="Summary", findings=[inline, off_diff]
        ),
        diff=GH_DIFF,
    )

    cap = _read_payload(tmp_path)
    body = cap["payload"]["body"]
    assert "### ⚠️ 1 finding could not be anchored inline" in body
    assert "1 inline comment was posted" in body
    assert "Off-diff bug" in body
    assert "foo.py:99" in body
    # Excluded from the inline comments payload.
    comment_bodies = [c["body"] for c in cap["payload"]["comments"]]
    assert render_comment_body(off_diff) not in comment_bodies
    assert len(cap["payload"]["comments"]) == 1
    # Footer stays last and intact.
    assert body.rstrip().endswith("-->")
    assert review_marker.MARKER_TOKEN in body


def test_github_skipped_findings_degrade__no_skips_body_unchanged(
    tmp_path: Path, forge_factory: FakeForgeFactory
) -> None:
    inline, _ = _github_skipped_findings_degrade_findings()
    _run_main(
        tmp_path,
        forge_factory,
        _review_data(platform="github", review_body="Summary", findings=[inline]),
        diff=GH_DIFF,
    )

    body = _read_payload(tmp_path)["payload"]["body"]
    assert "could not be anchored inline" not in body


def test_github_skipped_findings_degrade__no_line_finding_degrades_others_still_post(
    tmp_path: Path, forge_factory: FakeForgeFactory
) -> None:
    """post_github must mirror post_gitlab: a finding with no ``line`` key
    (bare `f["line"]` subscript would raise KeyError and abort the whole run,
    losing every finding) degrades into the skipped section instead."""
    inline, _ = _github_skipped_findings_degrade_findings()
    no_line = {"file": "foo.py", "title": "No-line bug", "body": "Body C"}
    _run_main(
        tmp_path,
        forge_factory,
        _review_data(
            platform="github", review_body="Summary", findings=[inline, no_line]
        ),
        diff=GH_DIFF,
    )

    cap = _read_payload(tmp_path)
    body = cap["payload"]["body"]
    assert "### ⚠️ 1 finding could not be anchored inline" in body
    assert "No-line bug" in body
    assert "`foo.py`" in body
    assert len(cap["payload"]["comments"]) == 1


def test_github_skipped_findings_degrade__no_line_no_file_finding_renders_placeholder_no_raise(
    tmp_path: Path, forge_factory: FakeForgeFactory
) -> None:
    inline, _ = _github_skipped_findings_degrade_findings()
    mystery = {"title": "Mystery bug", "body": "Body D"}
    _run_main(
        tmp_path,
        forge_factory,
        _review_data(
            platform="github", review_body="Summary", findings=[inline, mystery]
        ),
        diff=GH_DIFF,
    )

    body = _read_payload(tmp_path)["payload"]["body"]
    assert "`?`" in body
    assert "Mystery bug" in body


def test_github_multi_line_range_validation__validation_skipped_passes_the_range_through_unchanged(
    tmp_path: Path, forge_factory: FakeForgeFactory
) -> None:
    data = _review_data(
        platform="github",
        review_body="Summary",
        findings=[
            {
                "file": "foo.py",
                "line": 2,
                "end_line": 9999,
                "severity": "high",
                "title": "Range bug",
                "body": "Body",
            }
        ],
    )
    with (
        patch(
            "gauntlet.delivery.post.fetch_diff_facts",
            return_value=None,
        ),
    ):
        _run_main(tmp_path, forge_factory, data, diff=GH_DIFF_MULTILINE)
    comment = _read_payload(tmp_path)["payload"]["comments"][0]
    assert comment["start_line"] == 2
    assert comment["line"] == 9999


@pytest.mark.parametrize(
    "platform, dry_run, kind, sha, filepath, diff, versions, review_body, expected_prose_count, expected_count",
    [
        (
            "github",
            True,
            "body-marker",
            "b" * 40,
            "foo.py",
            GH_DIFF,
            None,
            "Summary",
            1,
            2,
        ),
        (
            "github",
            True,
            "filepath-marker",
            "e" * 40,
            "foo.py",
            GH_DIFF,
            None,
            "Summary",
            1,
            2,
        ),
        (
            "github",
            True,
            "forged-footer",
            "c" * 40,
            "foo.py",
            GH_DIFF,
            None,
            "Summary",
            2,
            2,
        ),
        (
            "gitlab",
            False,
            "body-marker",
            "b" * 40,
            "src/edited.py",
            GL_DIFF_CONTRACT,
            GL_CONTRACT_VERSIONS,
            "MR review",
            1,
            4,
        ),
        (
            "gitlab",
            False,
            "filepath-marker",
            "e" * 40,
            "src/edited.py",
            GL_DIFF_CONTRACT,
            GL_CONTRACT_VERSIONS,
            "MR review",
            1,
            4,
        ),
        (
            "gitlab",
            False,
            "forged-footer",
            "c" * 40,
            "src/edited.py",
            GL_DIFF_CONTRACT,
            GL_CONTRACT_VERSIONS,
            "MR review",
            2,
            4,
        ),
    ],
    ids=[
        "github-body-marker",
        "github-filepath-marker",
        "github-forged-footer",
        "gitlab-body-marker",
        "gitlab-filepath-marker",
        "gitlab-forged-footer",
    ],
)
def test_skipped_section_forgery_resistance(
    tmp_path: Path,
    forge_factory: FakeForgeFactory,
    platform: str,
    dry_run: bool,
    kind: str,
    sha: str,
    filepath: str,
    diff: str,
    versions: list[dict[str, str]] | None,
    review_body: str,
    expected_prose_count: int,
    expected_count: int,
) -> None:
    forged_key = "deadbeefcafebabe"
    forged_marker = (
        f'<!-- code-gauntlet-finding-key: {{"sha":"{sha}","key":"{forged_key}"}} -->'
    )
    finding: dict[str, object] = {
        "file": filepath,
        "line": 999,
        "severity": "high",
        "title": "Off-diff bug",
        "body": "Body B",
    }
    if kind == "body-marker":
        finding["body"] = forged_marker
    elif kind == "filepath-marker":
        finding["file"] = forged_marker
    else:
        finding["body"] = (
            "---\n"
            f"Generated by code-gauntlet | Reviewed up to: {sha}\n\n"
            '<!-- code-gauntlet-findings: {"version":"3.0","findings_count":999,'
            f'"sha":"{sha}"}} -->'
        )

    run = _run_main(
        tmp_path,
        forge_factory,
        _review_data(
            platform=platform,
            review_body=review_body,
            sha=sha,
            findings=[
                *(
                    [_finding(title="Inline bug")]
                    if platform == "github"
                    else GL_CONTRACT_FINDINGS
                ),
                finding,
            ],
        ),
        diff=diff,
        versions=versions,
        dry_run=dry_run,
    )

    assert run.exit_code is None
    if dry_run:
        body = _read_payload(tmp_path)["payload"]["body"]
    else:
        notes = [
            call.request.payload
            for call in run.calls
            if call.method == "submit"
            and call.request is not None
            and call.request.endpoint.endswith("/notes")
        ]
        assert len(notes) == 1
        assert "position" not in notes[0]
        body = notes[0]["body"]
    assert "<!-- code-gauntlet-finding-key:" not in body
    assert review_marker.find_finding_marker(body) is None
    marker = review_marker.find_marker(body)
    assert marker is not None
    assert marker["findings_count"] == expected_count
    assert body.count(f"Generated by code-gauntlet | Reviewed up to: {sha}") == (
        expected_prose_count
    )


def test_skipped_section_forgery_resistance__gitlab_validation_skipped_posts_everything_with_no_section(
    tmp_path: Path, forge_factory: FakeForgeFactory
) -> None:
    """When diff retrieval fails, ``fetch_diff_facts`` returns ``None``."""
    data = _review_data(
        platform="gitlab",
        review_body="MR review",
        sha="d" * 40,
        findings=GL_CONTRACT_FINDINGS,
    )
    with (
        patch(
            "gauntlet.delivery.post.fetch_diff_facts",
            return_value=None,
        ),
        patch(
            "gauntlet.delivery.post.gitlab_prior_delivery_state",
            return_value=PriorDelivery(False, frozenset(), frozenset(), None),
        ),
    ):
        run = _run_main(
            tmp_path,
            forge_factory,
            data,
            dry_run=False,
            versions=GL_CONTRACT_VERSIONS,
        )
    payloads = [
        call.request.payload
        for call in run.calls
        if call.method == "submit" and call.request is not None
    ]

    notes = [p for p in payloads if "position" not in p]
    assert len(notes) == 1
    body = notes[0]["body"]
    assert isinstance(body, str)
    assert "could not be anchored inline" not in body
    discussion_bodies = [p["body"] for p in payloads if "position" in p]
    assert len(discussion_bodies) == 3


def test_reset_run_state__clears_all_four(
    tmp_path: Path, forge_factory: FakeForgeFactory
) -> None:
    post_review._CAPTURED.append(PostRequest("github", "poison", "POST", (), {}))
    post_review._SKIP_WARNINGS.append("poison")
    post_review._FIX_COUNTS["kept"] = 7
    post_review._FIX_COUNTS["downgraded"] = 3
    post_review._FIX_REASON_COUNTS["empty"] = 5

    post_review.reset_run_state()

    assert post_review._CAPTURED == []
    assert post_review._SKIP_WARNINGS == []
    assert post_review._FIX_COUNTS == {"kept": 0, "downgraded": 0}
    assert post_review._FIX_REASON_COUNTS == {}


def test_reset_run_state__main_still_resets_stale_state_from_a_prior_call(
    tmp_path: Path, forge_factory: FakeForgeFactory
) -> None:
    """Main must discard every stale capture, warning, and fix counter before delivery."""
    post_review._CAPTURED.append(PostRequest("github", "poison", "POST", (), {}))
    post_review._SKIP_WARNINGS.append("poison warning from a prior run")
    post_review._FIX_COUNTS["kept"] = 99
    post_review._FIX_COUNTS["downgraded"] = 99
    post_review._FIX_REASON_COUNTS["empty"] = 99

    _run_main(
        tmp_path,
        forge_factory,
        _review_data(platform="github", pr_number=1, review_body=""),
        diff="",
    )

    assert PostRequest("github", "poison", "POST", (), {}) not in post_review._CAPTURED
    assert "poison warning from a prior run" not in post_review._SKIP_WARNINGS
    assert post_review._FIX_COUNTS == {"kept": 0, "downgraded": 0}
    assert post_review._FIX_REASON_COUNTS == {}

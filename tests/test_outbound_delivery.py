"""Tests for the text that the delivery composers place in comment bodies."""

import contextlib
import hashlib
import io
import json
import random
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import scripts.post_review as post_review
import scripts.review_marker as review_marker

REPO = Path(__file__).resolve().parents[1]
NODE = "/Users/lee/.local/share/mise/installs/node/24.21.0/bin/node"
OUTBOUND_CASES = json.loads(
    (REPO / "tests/fixtures/outbound_comment_cases.json").read_text(encoding="utf-8")
)["cases"]
SHA = "a" * 40
FAKE_FINDING_MARKER = review_marker.build_finding_marker(SHA, "0123456789abcdef")


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


def _capture_dry_run(platform, findings, review_body=""):
    valid_lines = {("src/edited.py", 2): None}
    line_texts = {("src/edited.py", 2): "changed"}
    data = {
        "owner": "o",
        "repo": "r",
        "pr_number": 7,
        "sha": SHA,
        "review_body": review_body,
        "findings": findings,
    }
    post_review._CAPTURED.clear()
    post_review._SKIP_WARNINGS.clear()
    with (
        patch.object(post_review, "DRY_RUN", True),
        patch("scripts.post_review.check_tool"),
        patch(
            "scripts.post_review.fetch_gitlab_shas",
            return_value=("base", "head", "start"),
        ),
        patch(
            "scripts.post_review.gitlab_prior_delivery",
            return_value=(False, set(), set()),
        ),
        contextlib.redirect_stdout(io.StringIO()),
        contextlib.redirect_stderr(io.StringIO()),
    ):
        if platform == "github":
            post_review.post_github(data, valid_lines, line_texts)
        else:
            post_review.post_gitlab(data, valid_lines, set(), {}, line_texts)
        payload = post_review.build_dry_run_payload(platform)
    post_review._CAPTURED.clear()
    post_review._SKIP_WARNINGS.clear()
    return payload


def _poison(key):
    marker_key = hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]
    marker = review_marker.build_finding_marker("b" * 40, marker_key)
    return f"@zz363{key} <ins data-zz363{key}> {marker} ```` &#38;#64;zz363{key}"


class _RecordingFinding(dict):
    def __init__(self, *args, reads=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.reads = reads if reads is not None else set()

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
    for line in text.splitlines():
        offset = 0
        while True:
            index = line.find(needle, offset)
            if index < 0:
                break
            runs = list(re.finditer(r"`+", line))
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
                        return True
            offset = index + len(needle)
    return False


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
                test.assertNotIn(visible, body, key)
        if key not in location_names and key != patch_name:
            test.assertNotIn(f"<ins data-zz363{key}", body, key)
    test.assertNotIn("@zz363unknown_key", body)
    test.assertNotIn("&#64;zz363", body)
    test.assertEqual(review_marker.find_finding_markers(body), list(expected_markers))


class TestOutboundComposerContracts(unittest.TestCase):
    def test_python_fixture_rows_match_the_ordered_prose_entry_points(self):
        prepare_prose = getattr(post_review, "prepare_prose", None)
        prepare_line = getattr(post_review, "prepare_line", None)
        self.assertTrue(callable(prepare_prose), "prepare_prose entry point is missing")
        self.assertTrue(callable(prepare_line), "prepare_line entry point is missing")
        prepare = {
            "single_line": prepare_line,
            "location": prepare_line,
            "prose": prepare_prose,
            "rule": lambda value: post_review._prepared_prose(value, cap=True) or "",
        }
        for row in OUTBOUND_CASES:
            with self.subTest(case=row["id"]):
                self.assertEqual(
                    prepare[row["field_class"]](row["input"]), row["expected"]
                )

    def test_prose_preparation_is_idempotent_for_fixtures_and_seeded_inputs(self):
        prepare_prose = getattr(post_review, "prepare_prose", None)
        self.assertTrue(callable(prepare_prose), "prepare_prose entry point is missing")
        values = [row["input"] for row in OUTBOUND_CASES]
        rng = random.Random(917)
        alphabet = "@<&#!/?`\\0123456789abc\n\r "
        values.extend(
            "".join(rng.choice(alphabet) for _ in range(rng.randint(1, 64)))
            for _ in range(5000)
        )
        for value in values:
            with self.subTest(value=value[:24]):
                once = prepare_prose(value)
                self.assertEqual(prepare_prose(once), once)

    def test_primary_title_and_body_are_contained_before_the_footer(self):
        rendered = post_review.render_comment_body(_hostile_finding())
        _assert_no_hostile_prose(self, rendered)
        self.assertIn("\uff20zz363sentinel", rendered)
        self.assertIn(post_review.BRAND_TRAILER, rendered)
        self.assertTrue(rendered.endswith(post_review.BRAND_TRAILER))

    def test_suggestion_and_cited_rule_are_prepared_as_prose(self):
        rendered = post_review.render_comment_body(_hostile_finding())
        self.assertIn("\uff20zz363suggestion", rendered)
        self.assertIn("\uff20zz363rule", rendered)
        self.assertNotIn("@zz363", rendered)
        self.assertNotIn("<ins data-zz363", rendered)

    def test_patch_code_remains_byte_exact_inside_its_suggestion_fence(self):
        patch_text = "print('@zz363patch <table> &commat;')"
        rendered = post_review.render_comment_body(
            _hostile_finding(suggested_fix_code=patch_text)
        )
        self.assertIn(f"\n```suggestion\n{patch_text}\n```", rendered)

    def test_corroborator_header_and_body_use_the_same_text_contract(self):
        primary = {"severity": "high", "title": "Safe", "body": "Safe body"}
        corroborator = _hostile_finding(
            agent="@zz363agent <ins data-zz363>",
            dimension="@zz363dimension <ins data-zz363>",
            confidence="@zz363confidence <ins data-zz363>",
        )
        rendered = post_review.render_group_body(primary, [corroborator])
        self.assertNotIn("@zz363", rendered)
        self.assertNotIn("<ins data-zz363", rendered)
        self.assertNotIn("<table", rendered)
        self.assertEqual(review_marker.find_finding_markers(rendered), [])

    def test_skipped_section_contains_hostile_titles_and_bodies(self):
        rendered = post_review.build_skipped_section(
            [("src/edited.py", 99, _hostile_finding(line=99))]
        )
        _assert_no_hostile_prose(self, rendered)
        self.assertIn("\uff20zz363sentinel", rendered)

    def test_legacy_review_body_is_guarded_before_composition(self):
        incoming = f"@zz363sentinel <table><tr><td> legacy {FAKE_FINDING_MARKER}"
        composed = post_review.compose_review_body(
            incoming, [], platform="github", findings_count=0, sha=SHA
        )
        _assert_no_hostile_prose(self, composed.body)
        self.assertIn("\uff20zz363sentinel", composed.body)
        self.assertEqual(review_marker.find_marker(composed.body)["sha"], SHA)

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
        self.assertNotIn("@zz363", rendered)
        self.assertNotIn("<table", rendered)
        self.assertEqual(review_marker.find_finding_markers(rendered), [])

    def test_github_payload_guards_review_inline_and_skipped_fields(self):
        anchored = _hostile_finding()
        skipped = _hostile_finding(line=99)
        payload = _capture_dry_run(
            "github",
            [anchored, skipped],
            f"@zz363sentinel <table><tr><td> review {FAKE_FINDING_MARKER}",
        )
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
        payload = _capture_dry_run(
            "gitlab",
            [primary, corroborator, skipped],
            f"@zz363sentinel <table><tr><td> summary {FAKE_FINDING_MARKER}",
        )
        summary = payload["summary"]["body"]
        discussion = payload["discussions"][0]["body"]
        _assert_no_hostile_prose(self, summary)
        _assert_no_hostile_prose(self, discussion)
        self.assertIn("\uff20zz363sentinel", summary)
        self.assertIn("Corroborating finding", discussion)
        self.assertIn("\uff20zz363sentinel", discussion)
        self.assertIn("could not be anchored inline", summary)
        self.assertEqual(review_marker.find_marker(summary)["sha"], SHA)

    def test_legacy_report_summary_is_guarded_on_the_real_cli_path(self):
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
                f"@zz363sentinel <table><tr><td> report {FAKE_FINDING_MARKER}\n\n"
                "## Findings\n\n",
                encoding="utf-8",
            )
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
                patch("scripts.post_review.check_tool"),
                patch(
                    "scripts.post_review.parse_diff_lines",
                    return_value=({}, set(), {}, {}),
                ),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                post_review.main()
            payload = json.loads(payload_path.read_text(encoding="utf-8"))
        review_body = payload["payload"]["body"]
        _assert_no_hostile_prose(self, review_body)
        self.assertIn("\uff20zz363sentinel", review_body)


class TestFoldAndGateContracts(unittest.TestCase):
    def test_midline_fold_retires_the_open_code_span_in_both_composers(self):
        text = "x" * 65213 + " `<table><tr><td>`" + "y" * 1000
        for name, fold in (
            (
                "inline",
                lambda: post_review._fold_inline_body(text, 65336, "github", "inline"),
            ),
            (
                "review",
                lambda: post_review._fold_review_body(text, 65336, "github"),
            ),
        ):
            with self.subTest(composer=name):
                folded, dropped = fold()
                self.assertGreater(dropped, 0)
                prefix = folded.split("\n\n_[folded:", 1)[0]
                self.assertNotIn("<table", prefix)

    def test_line_boundary_fold_keeps_a_complete_single_line_code_span(self):
        span = "`<table><tr><td>`"
        text = "x" * 65213 + "\n" + span + "\n" + "tail " * 1000
        for name, fold in (
            (
                "inline",
                lambda: post_review._fold_inline_body(text, 65336, "github", "inline"),
            ),
            (
                "review",
                lambda: post_review._fold_review_body(text, 65336, "github"),
            ),
        ):
            with self.subTest(composer=name):
                folded, dropped = fold()
                self.assertGreater(dropped, 0)
                self.assertIn("\n" + span + "\n", folded)

    def test_unclosed_comment_cut_ignores_comment_opener_inside_code_span(self):
        text = "`<!--` code survives\n<!-- unclosed comment"
        self.assertEqual(
            post_review._cut_unclosed_comment(text), "`<!--` code survives\n"
        )

    def test_patch_with_a_finding_marker_opener_is_rejected_as_marker_shaped(self):
        finding = {
            "file": "src/edited.py",
            "line": 1,
            "end_line": 1,
            "suggested_fix_code": "<!-- code-gauntlet-finding-key: forged",
        }
        result = post_review._suggested_fix_gate(
            finding,
            apply_range=(1, 1),
            line_texts={("src/edited.py", 1): "original"},
            valid_lines={("src/edited.py", 1): 1},
            path_lookup="src/edited.py",
        )
        self.assertEqual(result, (False, "marker_shaped"))


class TestDeliveryTitleKeys(unittest.TestCase):
    @staticmethod
    def _live_key(finding):
        calls = []
        filepath = finding["file"]
        line = finding["line"]

        def fake_try_post_json(_cmd, payload):
            calls.append(payload)
            return {}, None

        with (
            patch.object(post_review, "DRY_RUN", False),
            patch("scripts.post_review.check_tool"),
            patch(
                "scripts.post_review.fetch_gitlab_shas",
                return_value=("base", "head", "start"),
            ),
            patch(
                "scripts.post_review.gitlab_prior_delivery",
                return_value=(False, set(), set()),
            ),
            patch("scripts.post_review.try_post_json", side_effect=fake_try_post_json),
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
                {(filepath, line): 50},
                set(),
                {filepath: filepath},
                {(filepath, line): "context"},
            )
        discussion = next(payload for payload in calls if "position" in payload)
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

    def test_safe_finding_key_keeps_its_existing_pin(self):
        finding = {
            "file": "src/edited.py",
            "line": 61,
            "severity": "high",
            "title": "Context-line finding",
            "body": "Body one",
        }
        key = post_review.finding_key(
            finding["file"],
            finding["line"],
            finding["title"],
            post_review.key_material_body(finding),
        )
        self.assertEqual(key, "f87d51ec25846a5e")


class TestGitlabLiveFallbackContracts(unittest.TestCase):
    @staticmethod
    def _post_live(findings, prior, *, reject_first_discussion=False):
        calls = []
        discussion_attempts = 0

        def fake_try_post_json(cmd, payload):
            nonlocal discussion_attempts
            calls.append((cmd, payload))
            if cmd[-1].endswith("/discussions"):
                discussion_attempts += 1
                if reject_first_discussion and discussion_attempts == 1:
                    return None, "position rejected"
            return {}, None

        valid_lines = {
            ("src/edited.py", 2): None,
            ("src/edited.py", 3): None,
        }
        line_texts = {
            ("src/edited.py", 2): "primary",
            ("src/edited.py", 3): "corroborator",
        }
        post_review._CAPTURED.clear()
        with (
            patch.object(post_review, "DRY_RUN", False),
            patch("scripts.post_review.check_tool"),
            patch(
                "scripts.post_review.fetch_gitlab_shas",
                return_value=("base", "head", "start"),
            ),
            patch(
                "scripts.post_review.gitlab_prior_delivery", return_value=prior
            ) as lookup,
            patch("scripts.post_review.try_post_json", side_effect=fake_try_post_json),
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            post_review.post_gitlab(
                {
                    "owner": "o",
                    "repo": "r",
                    "pr_number": 7,
                    "sha": SHA,
                    "findings": findings,
                },
                valid_lines,
                set(),
                {"src/edited.py": "src/edited.py"},
                line_texts,
            )
        return calls, lookup

    def test_rejected_group_position_falls_back_to_a_prepared_discussion(self):
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
        calls, lookup = self._post_live(
            [primary, corroborator],
            (False, set(), set()),
            reject_first_discussion=True,
        )
        discussions = [
            payload for cmd, payload in calls if cmd[-1].endswith("/discussions")
        ]
        self.assertEqual(len(discussions), 2)
        self.assertIn("Corroborating finding", discussions[0]["body"])
        fallback_body = discussions[1]["body"]
        self.assertNotIn("Corroborating finding", fallback_body)
        markers = review_marker.find_finding_markers(fallback_body)
        self.assertEqual(len(markers), 1)
        self.assertEqual(markers[0]["sha"], SHA)
        self.assertNotEqual(markers[0]["key"], "0123456789abcdef")
        _assert_no_hostile_prose(self, fallback_body, expected_markers=markers)
        lookup.assert_called_once_with("o", "r", 7, SHA)


class TestPoisonedOutboundSinks(unittest.TestCase):
    @staticmethod
    def _finding(property_names, reads, *, primary=False):
        finding = _RecordingFinding(
            {key: _poison(key) for key in property_names}, reads=reads
        )
        finding["consolidation_key"] = "poison-group"
        finding["consolidation_primary"] = primary
        return finding

    @staticmethod
    def _capture(platform, findings, *, anchored):
        first = findings[0]
        filepath, line = first.get("file"), first.get("line")
        valid_lines = {(filepath, line): None} if anchored else {}
        line_texts = {(filepath, line): "context"} if anchored else {}
        post_review._CAPTURED.clear()
        post_review._SKIP_WARNINGS.clear()
        with (
            patch.object(post_review, "DRY_RUN", True),
            patch("scripts.post_review.check_tool"),
            patch(
                "scripts.post_review.fetch_gitlab_shas",
                return_value=("base", "head", "start"),
            ),
            patch(
                "scripts.post_review.gitlab_prior_delivery",
                return_value=(False, set(), set()),
            ),
            patch("scripts.post_review.validate_position", return_value=[]),
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            data = {
                "owner": "o",
                "repo": "r",
                "pr_number": 7,
                "sha": SHA,
                "review_body": _poison("review_body"),
                "findings": findings,
            }
            if platform == "github":
                post_review.post_github(data, valid_lines, line_texts)
            else:
                post_review.post_gitlab(data, valid_lines, set(), {}, line_texts)
            return post_review.build_dry_run_payload(platform)

    @staticmethod
    def _capture_live_gitlab(findings, prior, *, reject_first=False):
        calls = []
        discussion_count = 0
        primary = findings[0]
        filepath, line = primary.get("file"), primary.get("line")
        valid_lines = {(filepath, line): None}
        line_texts = {(filepath, line): "context"}

        def fake_try_post_json(cmd, payload):
            nonlocal discussion_count
            calls.append((cmd, payload))
            if cmd[-1].endswith("/discussions"):
                discussion_count += 1
                if reject_first and discussion_count == 1:
                    return None, "position rejected"
            return {}, None

        post_review._CAPTURED.clear()
        with (
            patch.object(post_review, "DRY_RUN", False),
            patch("scripts.post_review.check_tool"),
            patch(
                "scripts.post_review.fetch_gitlab_shas",
                return_value=("base", "head", "start"),
            ),
            patch("scripts.post_review.gitlab_prior_delivery", return_value=prior),
            patch("scripts.post_review.validate_position", return_value=[]),
            patch("scripts.post_review.try_post_json", side_effect=fake_try_post_json),
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            post_review.post_gitlab(
                {
                    "owner": "o",
                    "repo": "r",
                    "pr_number": 7,
                    "sha": SHA,
                    "review_body": _poison("review_body"),
                    "findings": findings,
                },
                valid_lines,
                set(),
                {filepath: filepath},
                line_texts,
            )
        return calls

    def test_poisoned_python_fields_never_reach_any_comment_sink(self):
        property_names = set(_js_finding_property_union()) | {"unknown_key"}
        reads = set()
        primary = self._finding(property_names, reads, primary=True)
        corroborator = self._finding(property_names, reads)

        direct_bodies = [
            post_review.render_comment_body(primary),
            post_review.render_group_body(primary, [corroborator]),
            post_review.build_skipped_section(
                [(primary.get("file"), primary.get("line"), primary)]
            ),
        ]
        fallback = self._finding(property_names, reads)
        fallback["claude_md_rule"] = FAKE_FINDING_MARKER
        direct_bodies.append(post_review.render_comment_body(fallback))

        for platform in ("github", "gitlab"):
            for route in ("anchored", "off_diff", "grouped"):
                routed_primary = self._finding(property_names, reads, primary=True)
                members = [routed_primary]
                if route == "grouped":
                    members.append(self._finding(property_names, reads))
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
        primary = self._finding(property_names, reads, primary=True)
        corroborator = self._finding(property_names, reads)
        self._capture("github", [primary], anchored=True)
        self._capture("gitlab", [primary, corroborator], anchored=True)
        fallback = self._finding(property_names, reads)
        fallback["claude_md_rule"] = FAKE_FINDING_MARKER
        post_review.render_comment_body(fallback)
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

    def test_poisoned_gitlab_live_fallback_discussion_and_note(self):
        property_names = set(_js_finding_property_union()) | {"unknown_key"}
        primary = self._finding(property_names, set(), primary=True)
        corroborator = self._finding(property_names, set())
        primary["consolidation_key"] = "poison-group"
        corroborator["consolidation_key"] = "poison-group"
        rejected_calls = self._capture_live_gitlab(
            [primary, corroborator], (False, set(), set()), reject_first=True
        )
        discussions = [
            payload
            for cmd, payload in rejected_calls
            if cmd[-1].endswith("/discussions")
        ]
        self.assertEqual(len(discussions), 2)
        fallback_body = discussions[-1]["body"]
        fallback_markers = review_marker.find_finding_markers(fallback_body)
        self.assertEqual(len(fallback_markers), 1)
        _assert_poison_containment(
            self, fallback_body, property_names, expected_markers=fallback_markers
        )

        primary = self._finding(property_names, set(), primary=True)
        corroborator = self._finding(property_names, set())
        primary["consolidation_key"] = "poison-group"
        corroborator["consolidation_key"] = "poison-group"
        corroborator["line"] = None
        with patch(
            "scripts.post_review.finding_key",
            side_effect=lambda _file, member_line, _title, _body: (
                "1" * 16 if member_line is not None else "2" * 16
            ),
        ):
            note_calls = self._capture_live_gitlab(
                [primary, corroborator], (True, {"1" * 16}, set())
            )
        self.assertEqual(len(note_calls), 1)
        cmd, payload = note_calls[0]
        self.assertTrue(cmd[-1].endswith("/notes"))
        self.assertNotIn("position", payload)
        note_markers = review_marker.find_finding_markers(payload["body"])
        self.assertEqual(note_markers, [{"sha": SHA, "key": "2" * 16}])
        _assert_poison_containment(
            self, payload["body"], property_names, expected_markers=note_markers
        )

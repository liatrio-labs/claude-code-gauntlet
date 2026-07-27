"""
Tests for scripts/review_marker.py (Issue #39).

This module is written FROM THE DESIGN SPEC ALONE (issue #39), not by reading
scripts/review_marker.py or scripts/detect_prior_review.py — those land from a
concurrent implementation and this file is the independent, double-entry check
against the settled contract. Where this file and the implementation disagree,
that disagreement is the signal the split was designed to produce.

Covers:
  - TestRoundTrip           — REQUIREMENT 6, the headline test: build -> detect
    recovers the exact sha, including the realistic post_github/post_gitlab
    composition (review_body + build_footer(..., body=review_body)).
  - TestTolerance            — every marker shape that can exist in the wild is
    still detected: legacy token, documented-but-unwritten v1 shape, missing
    version, unknown future keys, whitespace-free comments, a findings array
    containing a literal "-->" inside a string value.
  - TestVersionIsNeverDispatchedOn — version is informational only.
  - TestProseFooter          — current/legacy product names, marker-wins-over-
    footer precedence, footer-only detection.
  - TestMalformed             — detect_signal/find_marker never raise; malformed
    input yields None, not an exception; NaN/Infinity/-Infinity in a marker
    payload are rejected (parse_constant) rather than silently parsed.
  - TestIdempotence          — build_footer's independent per-half guards,
    keyed on what detect_signal itself would accept (not "any parseable
    fragment"), including the sha-less-marker-plus-bare-prose case that used
    to suppress both halves and leave no signal at all.
  - TestBuildMarkerFindingsSlot — the #36 extension point: findings=None is
    byte-absent from the payload; a supplied list round-trips.
  - TestSelectLatest         — newest-timestamp-wins entry selection.
  - TestDocContract           — phase1-preflight.md / SKILL.md / phase2-triage.md
    reference the new script and the fields the incremental gate depends on,
    every raw signal string the skill docs quote is actually detected by
    detect_signal, and post_review.py has no second build_footer definition.
"""

import json
import os
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import scripts.review_marker as review_marker
from scripts.review_marker import (
    MARKER_TOKEN,
    LEGACY_MARKER_TOKEN,
    MARKER_TOKENS,
    PRODUCT,
    LEGACY_PRODUCT,
    build_marker,
    build_prose_footer,
    build_footer,
    find_marker,
    has_prose_footer,
    parse_prose_footer,
    detect_signal,
    select_latest,
)

REPO = Path(__file__).resolve().parents[1]

# Fixed hex-only SHAs reused across tests. Using letters restricted to a-f keeps
# every value valid under review_marker.SHA_RE (`[0-9a-f]{7,40}`) regardless of
# where it lands in a test string.
SHA_40 = "a" * 40
SHA_8 = "a" * 8
HEAD_SHA_40 = "b" * 40


# ---------------------------------------------------------------------------
# TestRoundTrip — Requirement 6, the headline test.
# ---------------------------------------------------------------------------

class TestRoundTrip(unittest.TestCase):

    def test_build_footer_round_trip_matrix(self):
        for sha in (SHA_40, SHA_8):
            for count in (0, 1, 250):
                with self.subTest(sha=sha, count=count):
                    footer = build_footer(count, sha)
                    signal = detect_signal(footer)
                    self.assertIsNotNone(signal)
                    self.assertEqual(signal["sha"], sha)

    def test_realistic_github_review_body_round_trip(self):
        """Mirrors post_github: review_body += build_footer(n, sha, body=review_body)."""
        findings = [{"title": "x"}, {"title": "y"}, {"title": "z"}]
        for pre_existing in ("", "## Summary\nSome pre-existing narrative text.\n"):
            for sha in (SHA_40, SHA_8):
                with self.subTest(pre_existing=repr(pre_existing), sha=sha):
                    review_body = pre_existing
                    review_body += build_footer(len(findings), sha, body=review_body)
                    signal = detect_signal(review_body)
                    self.assertIsNotNone(signal)
                    self.assertEqual(signal["signal"], "marker")
                    self.assertEqual(signal["sha"], sha)
                    self.assertFalse(signal["legacy"])

    def test_realistic_gitlab_summary_note_round_trip(self):
        """Mirrors post_gitlab's summary_payload body composition — same call shape."""
        findings = [{"title": "a"}]
        for pre_existing in ("", "## MR Review\nContext for the reviewer.\n"):
            for sha in (SHA_40, SHA_8):
                with self.subTest(pre_existing=repr(pre_existing), sha=sha):
                    summary_body = pre_existing
                    summary_body += build_footer(len(findings), sha, body=summary_body)
                    signal = detect_signal(summary_body)
                    self.assertIsNotNone(signal)
                    self.assertEqual(signal["signal"], "marker")
                    self.assertEqual(signal["sha"], sha)


# ---------------------------------------------------------------------------
# TestTolerance — every marker shape that can exist in the wild.
# ---------------------------------------------------------------------------

class TestTolerance(unittest.TestCase):

    def test_current_shape(self):
        text = build_marker(SHA_40, 3)
        signal = detect_signal(text)
        self.assertIsNotNone(signal)
        self.assertEqual(signal["sha"], SHA_40)
        self.assertFalse(signal["legacy"])

    def test_legacy_token(self):
        payload = json.dumps({"version": "3.0", "findings_count": 2, "sha": SHA_40})
        text = f"<!-- {LEGACY_MARKER_TOKEN}: {payload} -->"
        signal = detect_signal(text)
        self.assertIsNotNone(signal)
        self.assertEqual(signal["sha"], SHA_40)
        self.assertTrue(signal["legacy"])

    def test_documented_but_never_written_v1_shape(self):
        payload = json.dumps({"version": 1, "sha": SHA_40, "findings": [{"a": 1}]})
        text = f"<!-- {MARKER_TOKEN}: {payload} -->"
        signal = detect_signal(text)
        self.assertIsNotNone(signal)
        self.assertEqual(signal["sha"], SHA_40)

    def test_version_absent_entirely(self):
        payload = json.dumps({"findings_count": 1, "sha": SHA_40})
        text = f"<!-- {MARKER_TOKEN}: {payload} -->"
        signal = detect_signal(text)
        self.assertIsNotNone(signal)
        self.assertEqual(signal["sha"], SHA_40)

    def test_unknown_future_keys_preserved_in_marker(self):
        payload = json.dumps({
            "version": "3.0", "sha": SHA_40,
            "future_field": "xyz", "nested": {"a": 1},
        })
        text = f"<!-- {MARKER_TOKEN}: {payload} -->"
        signal = detect_signal(text)
        self.assertIsNotNone(signal)
        self.assertEqual(signal["marker"]["future_field"], "xyz")
        self.assertEqual(signal["marker"]["nested"], {"a": 1})

    def test_whitespace_free_and_newline_broken_forms_both_parse(self):
        compact = json.dumps({"version": "3.0", "sha": SHA_40}, separators=(",", ":"))
        shapes = [
            f"<!--{MARKER_TOKEN}:{compact}-->",
            f"<!-- {MARKER_TOKEN}:\n{compact}\n-->",
            f"<!--\n{MARKER_TOKEN}: {compact}\n-->",
        ]
        for text in shapes:
            with self.subTest(text=text):
                signal = detect_signal(text)
                self.assertIsNotNone(signal)
                self.assertEqual(signal["sha"], SHA_40)

    def test_findings_array_containing_literal_close_comment_inside_a_string(self):
        payload = json.dumps({
            "version": "3.0", "sha": SHA_40,
            "findings": ["this string literally contains --> inside it"],
        })
        text = f"<!-- {MARKER_TOKEN}: {payload} -->"
        signal = detect_signal(text)
        self.assertIsNotNone(signal)
        self.assertEqual(signal["sha"], SHA_40)
        self.assertEqual(
            signal["marker"]["findings"],
            ["this string literally contains --> inside it"],
        )


# ---------------------------------------------------------------------------
# TestVersionIsNeverDispatchedOn
# ---------------------------------------------------------------------------

class TestVersionIsNeverDispatchedOn(unittest.TestCase):

    def test_version_variants_yield_identical_sha_and_signal(self):
        results = []
        for version in ("3.0", 1, "99", None):
            payload = {"version": version, "findings_count": 5, "sha": SHA_40}
            text = f"<!-- {MARKER_TOKEN}: {json.dumps(payload)} -->"
            signal = detect_signal(text)
            self.assertIsNotNone(signal, f"version={version!r} produced no signal")
            results.append((signal["sha"], signal["signal"]))

        # version key omitted entirely
        payload = {"findings_count": 5, "sha": SHA_40}
        text = f"<!-- {MARKER_TOKEN}: {json.dumps(payload)} -->"
        signal = detect_signal(text)
        self.assertIsNotNone(signal)
        results.append((signal["sha"], signal["signal"]))

        self.assertEqual(
            len(set(results)), 1,
            f"version must never affect the detected sha/signal: {results}",
        )


# ---------------------------------------------------------------------------
# TestProseFooter
# ---------------------------------------------------------------------------

class TestProseFooter(unittest.TestCase):

    def test_current_product_prose_detected(self):
        text = build_prose_footer(SHA_40)
        self.assertTrue(has_prose_footer(text))
        signal = detect_signal(text)
        self.assertIsNotNone(signal)
        self.assertEqual(signal["signal"], "footer")
        self.assertFalse(signal["legacy"])
        self.assertEqual(signal["sha"], SHA_40)

    def test_legacy_product_prose_detected(self):
        text = f"Generated by {LEGACY_PRODUCT} | Reviewed up to: {SHA_40}"
        self.assertTrue(has_prose_footer(text))
        signal = detect_signal(text)
        self.assertIsNotNone(signal)
        self.assertEqual(signal["signal"], "footer")
        self.assertTrue(signal["legacy"])
        self.assertEqual(signal["sha"], SHA_40)

    def test_sha_parsed_from_reviewed_up_to_label(self):
        self.assertEqual(parse_prose_footer(f"Reviewed up to: {SHA_40}"), SHA_40)
        self.assertEqual(parse_prose_footer(f"Reviewed up to: `{SHA_8}` |"), SHA_8)
        self.assertEqual(parse_prose_footer(f"Reviewed up to: **{SHA_8}**"), SHA_8)
        self.assertEqual(parse_prose_footer(f"Reviewed up to: {SHA_8} |"), SHA_8)

    def test_marker_wins_when_both_prose_and_marker_present(self):
        prose = build_prose_footer(HEAD_SHA_40)
        marker = build_marker(SHA_40, 4)
        text = f"{prose}\n\n{marker}"
        signal = detect_signal(text)
        self.assertIsNotNone(signal)
        self.assertEqual(signal["signal"], "marker")
        self.assertEqual(signal["sha"], SHA_40)

    def test_footer_only_body_yields_footer_signal(self):
        text = build_prose_footer(SHA_40)
        signal = detect_signal(text)
        self.assertIsNotNone(signal)
        self.assertEqual(signal["signal"], "footer")
        self.assertIsNone(signal["marker"])


# ---------------------------------------------------------------------------
# TestMalformed — never raises.
# ---------------------------------------------------------------------------

class TestMalformed(unittest.TestCase):

    def test_empty_string(self):
        self.assertIsNone(detect_signal(""))
        self.assertIsNone(find_marker(""))
        self.assertFalse(has_prose_footer(""))
        self.assertIsNone(parse_prose_footer(""))

    def test_marker_with_broken_json(self):
        text = f"<!-- {MARKER_TOKEN}: {{not valid json at all -->"
        self.assertIsNone(find_marker(text))
        self.assertIsNone(detect_signal(text))

    def test_marker_valid_json_but_no_sha(self):
        payload = json.dumps({"version": "3.0", "findings_count": 2})
        text = f"<!-- {MARKER_TOKEN}: {payload} -->"
        self.assertIsNone(detect_signal(text))
        parsed = find_marker(text)
        self.assertIsNotNone(parsed)
        self.assertNotIn("sha", parsed)

    def test_marker_sha_not_hex(self):
        payload = json.dumps({"version": "3.0", "sha": "not-a-real-sha!!"})
        text = f"<!-- {MARKER_TOKEN}: {payload} -->"
        self.assertIsNone(detect_signal(text))

    def test_one_broken_one_valid_marker_returns_the_valid_one(self):
        broken = f"<!-- {MARKER_TOKEN}: {{broken json -->"
        valid = build_marker(SHA_40, 1)
        text = f"{broken}\n\n{valid}"
        signal = detect_signal(text)
        self.assertIsNotNone(signal)
        self.assertEqual(signal["sha"], SHA_40)

    def test_nan_infinity_constants_reject_the_marker_as_malformed(self):
        """_scan_json_at passes parse_constant=_reject_constant, so JSON's
        non-standard NaN/Infinity/-Infinity literals (which stdlib json.loads
        accepts by default) make a marker payload simply malformed rather than
        silently parsed — otherwise a hostile marker could re-emit a bare NaN
        token via json.dumps downstream and break a caller's strict-JSON
        parse of the detector's own stdout."""
        for const in ("NaN", "Infinity", "-Infinity"):
            with self.subTest(const=const):
                text = (
                    f'<!-- {MARKER_TOKEN}: '
                    f'{{"version":"3.0","sha":"{SHA_40}","weird":{const}}} -->'
                )
                self.assertIsNone(find_marker(text))
                self.assertIsNone(detect_signal(text))

    def test_detector_stdout_stays_valid_strict_json_when_marker_carries_nan(self):
        """Pins the actual motivation: a marker payload containing NaN must
        not survive into anything a caller would json.dumps and expect to be
        strict JSON. Since the malformed marker yields no signal, there is
        nothing to embed, and detect_signal's own None result trivially
        round-trips through strict json.dumps/json.loads (no bare NaN
        token)."""
        text = f'<!-- {MARKER_TOKEN}: {{"sha":"{SHA_40}","x":NaN}} -->'
        signal = detect_signal(text)
        self.assertIsNone(signal)
        dumped = json.dumps({"marker": signal})
        self.assertNotIn("NaN", dumped)
        self.assertIsNone(json.loads(dumped)["marker"])

    def test_assorted_garbage_never_raises(self):
        garbage = [
            "no marker or footer here at all",
            f"<!-- {MARKER_TOKEN}: -->",
            f"<!-- {MARKER_TOKEN}:",
            f'<!-- {MARKER_TOKEN}: {{"sha" -->',
            "Reviewed up to:",
            "Generated by",
            "<!-- -->",
            "{" * 50,
        ]
        for text in garbage:
            with self.subTest(text=text):
                try:
                    detect_signal(text)
                    find_marker(text)
                    has_prose_footer(text)
                    parse_prose_footer(text)
                except Exception as exc:  # pragma: no cover - this is the failure path
                    self.fail(f"raised {exc!r} on {text!r}")


# ---------------------------------------------------------------------------
# TestIdempotence
# ---------------------------------------------------------------------------

class TestIdempotence(unittest.TestCase):

    def test_marker_already_present_omits_marker_half(self):
        body = build_marker(SHA_40, 2)
        addition = build_footer(3, SHA_40, body=body)
        self.assertNotIn("<!--", addition, "marker half must be omitted")
        self.assertIn("Generated by", addition, "prose half is still needed")
        full = body + addition
        signal = detect_signal(full)
        self.assertIsNotNone(signal)
        self.assertEqual(signal["sha"], SHA_40)

    def test_prose_already_present_omits_prose_half(self):
        body = build_prose_footer(SHA_40)
        addition = build_footer(1, SHA_40, body=body)
        self.assertNotIn("Generated by", addition, "prose half must be omitted")
        self.assertIn(MARKER_TOKEN, addition, "marker half is still needed")
        full = body + addition
        signal = detect_signal(full)
        self.assertIsNotNone(signal)
        self.assertEqual(signal["sha"], SHA_40)
        self.assertEqual(signal["signal"], "marker")

    def test_both_already_present_returns_empty_string(self):
        body = build_footer(1, SHA_40)  # body="" -> both halves emitted fresh
        addition = build_footer(1, SHA_40, body=body)
        self.assertEqual(addition, "")

    def test_double_append_is_a_no_op_and_one_sha_resolves(self):
        body = ""
        body += build_footer(2, SHA_40, body=body)
        length_after_first = len(body)
        body += build_footer(2, SHA_40, body=body)
        self.assertEqual(len(body), length_after_first, "double-append must add nothing")
        signal = detect_signal(body)
        self.assertIsNotNone(signal)
        self.assertEqual(signal["sha"], SHA_40)

    def test_sha_less_marker_plus_bare_prose_still_gets_a_detectable_signal(self):
        """Regression pin: build_footer's per-half guards used to key on
        find_marker (any parseable payload, sha or not) and has_prose_footer
        (any 'Generated by <product>' substring, sha or not) independently. A
        body carrying BOTH a sha-less marker AND a bare 'Generated by
        code-gauntlet' line (no parseable 'Reviewed up to:' sha) suppressed
        BOTH halves under that old logic, so build_footer returned "" and the
        posted review carried no detectable signal at all. The guards now key
        on what detect_signal itself would accept: the marker half is
        suppressed only when detect_signal already reports signal=="marker",
        and the prose half only when a product line AND a parseable sha are
        both present."""
        body = (
            f'<!-- {MARKER_TOKEN}: {{"version":"3.0"}} -->\n\n'
            f"Generated by {PRODUCT}\n"
        )
        self.assertIsNone(
            detect_signal(body),
            "the pre-existing body must carry no usable signal (motivating case)",
        )
        addition = build_footer(1, SHA_40, body=body)
        self.assertNotEqual(
            addition, "",
            "build_footer must not go silent just because unusable marker/prose "
            "fragments are already present",
        )
        full = body + addition
        signal = detect_signal(full)
        self.assertIsNotNone(signal)
        self.assertEqual(signal["sha"], SHA_40)


# ---------------------------------------------------------------------------
# TestBuildMarkerFindingsSlot — #36 extension point.
# ---------------------------------------------------------------------------

class TestBuildMarkerFindingsSlot(unittest.TestCase):

    @staticmethod
    def _payload_from(marker_text):
        m = re.search(r":\s*({.*})\s*-->", marker_text)
        return json.loads(m.group(1))

    def test_findings_none_omits_the_key_entirely(self):
        marker_text = build_marker(SHA_40, 2, findings=None)
        payload = self._payload_from(marker_text)
        self.assertNotIn("findings", payload)
        self.assertEqual(list(payload.keys()), ["version", "findings_count", "sha"])

    def test_supplied_findings_appear_last_and_survive_round_trip(self):
        findings = [{"id": 1, "title": "x"}, {"id": 2, "title": "y"}]
        marker_text = build_marker(SHA_40, 2, findings=findings)
        payload = self._payload_from(marker_text)
        self.assertEqual(list(payload.keys()), ["version", "findings_count", "sha", "findings"])
        self.assertEqual(payload["findings"], findings)

        signal = detect_signal(marker_text)
        self.assertIsNotNone(signal)
        self.assertEqual(signal["marker"]["findings"], findings)


# ---------------------------------------------------------------------------
# TestSelectLatest
# ---------------------------------------------------------------------------

class TestSelectLatest(unittest.TestCase):

    def test_newest_timestamp_wins_across_mixed_sources(self):
        entries = [
            {"body": build_marker("a" * 8, 1), "timestamp": "2026-01-01T00:00:00Z",
             "source": "review", "id": 1},
            {"body": build_prose_footer("b" * 8), "timestamp": "2026-06-15T12:00:00Z",
             "source": "issue_comment", "id": 2},
            {"body": build_marker("c" * 8, 1), "timestamp": "2026-03-01T00:00:00Z",
             "source": "note", "id": 3},
        ]
        result = select_latest(entries)
        self.assertIsNotNone(result)
        self.assertEqual(result["sha"], "b" * 8)
        self.assertEqual(result["source"], "issue_comment")
        self.assertEqual(result["timestamp"], "2026-06-15T12:00:00Z")

    def test_none_timestamp_sorts_lowest(self):
        entries = [
            {"body": build_marker("a" * 8, 1), "timestamp": None,
             "source": "review", "id": 1},
            {"body": build_marker("b" * 8, 1), "timestamp": "2020-01-01T00:00:00Z",
             "source": "review", "id": 2},
        ]
        result = select_latest(entries)
        self.assertIsNotNone(result)
        self.assertEqual(result["sha"], "b" * 8)

    def test_unparseable_timestamp_sorts_lowest(self):
        # A naive lexicographic compare would rank "not-a-timestamp" above a real
        # ISO8601 string ('n' > '2' in ASCII) — this pins the required special case.
        entries = [
            {"body": build_marker("a" * 8, 1), "timestamp": "not-a-timestamp",
             "source": "review", "id": 1},
            {"body": build_marker("b" * 8, 1), "timestamp": "2020-01-01T00:00:00Z",
             "source": "review", "id": 2},
        ]
        result = select_latest(entries)
        self.assertIsNotNone(result)
        self.assertEqual(result["sha"], "b" * 8)

    def test_ties_break_to_latest_input_order(self):
        same_ts = "2026-01-01T00:00:00Z"
        entries = [
            {"body": build_marker("a" * 8, 1), "timestamp": same_ts,
             "source": "review", "id": 1},
            {"body": build_marker("b" * 8, 1), "timestamp": same_ts,
             "source": "note", "id": 2},
        ]
        result = select_latest(entries)
        self.assertIsNotNone(result)
        self.assertEqual(result["sha"], "b" * 8, "the later entry in input order must win a tie")

    def test_no_signal_entries_are_ignored(self):
        entries = [
            {"body": "nothing detectable in this body", "timestamp": "2026-06-01T00:00:00Z",
             "source": "x", "id": 1},
            {"body": build_marker("a" * 8, 1), "timestamp": "2020-01-01T00:00:00Z",
             "source": "review", "id": 2},
        ]
        result = select_latest(entries)
        self.assertIsNotNone(result)
        self.assertEqual(result["sha"], "a" * 8)

    def test_all_entries_without_signal_returns_none(self):
        entries = [
            {"body": "nope", "timestamp": "2026-01-01T00:00:00Z", "source": "x", "id": 1},
            {"body": "still nope", "timestamp": "2026-02-01T00:00:00Z", "source": "y", "id": 2},
        ]
        self.assertIsNone(select_latest(entries))

    def test_empty_entries_returns_none(self):
        self.assertIsNone(select_latest([]))


# ---------------------------------------------------------------------------
# TestDocContract — the read-path doc and the parser must provably agree.
# ---------------------------------------------------------------------------

_QUOTE_RE = re.compile(r"`([^`\n]+)`")


def _read(rel_path):
    return (REPO / rel_path).read_text(encoding="utf-8")


class TestDocContract(unittest.TestCase):

    PHASE1_REL = "skills/code-gauntlet/references/phase1-preflight.md"
    SKILL_REL = "skills/code-gauntlet/SKILL.md"
    PHASE2_REL = "skills/code-gauntlet/references/phase2-triage.md"
    REPORT_FORMAT_REL = "skills/code-gauntlet/references/report-format.md"
    DELIVERY_GUIDE_REL = "skills/code-gauntlet/references/delivery-guide.md"
    POST_REVIEW_REL = "scripts/post_review.py"

    # The docs that may quote a raw signal string. phase1-preflight.md / SKILL.md
    # are the read path (spec Deliverable 5); report-format.md / delivery-guide.md
    # are the write path, and after D5 they are the only files that still render
    # the marker/footer verbatim ("what the code writes"). Scanning all four is
    # what keeps this guard non-vacuous: D4/D7 deliberately stripped every raw
    # signal string out of the two read-path files, so restricting the scan to
    # them would make the assertion vacuously true and toothless.
    QUOTE_SCAN_RELS = (PHASE1_REL, SKILL_REL, REPORT_FORMAT_REL, DELIVERY_GUIDE_REL)

    def test_phase1_and_skill_reference_detect_prior_review_script(self):
        for rel in (self.PHASE1_REL, self.SKILL_REL):
            with self.subTest(path=rel):
                self.assertIn("detect_prior_review.py", _read(rel))

    def test_every_quoted_marker_token_or_footer_string_is_actually_detected(self):
        """Every raw marker token / 'Generated by <product>' string the skill docs
        backtick-quote as detectable must actually be detected by detect_signal —
        this is what makes the doc and the parser provably agree instead of
        silently drifting apart (exactly how issue #39 happened: the doc named a
        string no writer ever emitted).

        The footer half deliberately does NOT synthesize its own "Reviewed up
        to:" label: the docs already quote the full line, label included
        (e.g. `` `Generated by code-gauntlet | Reviewed up to: {full_sha}` ``),
        so this substitutes a real sha for the doc's own placeholder and feeds
        the doc's OWN line to detect_signal. A test that appended a
        separately-synthesized label (`f"{span} | Reviewed up to: {SHA}"`)
        would still pass even if the docs' actual label wording drifted away
        from what parse_prose_footer looks for — exactly the kind of silent
        drift this guard exists to catch.
        """
        checked = 0
        checked_footer = 0
        for rel in self.QUOTE_SCAN_RELS:
            for span in _QUOTE_RE.findall(_read(rel)):
                if span in MARKER_TOKENS:
                    is_legacy = span == LEGACY_MARKER_TOKEN
                    payload = json.dumps(
                        {"version": "3.0", "findings_count": 1, "sha": SHA_40})
                    text = f"<!-- {span}: {payload} -->"
                    with self.subTest(path=rel, quoted=span):
                        signal = detect_signal(text)
                        self.assertIsNotNone(
                            signal, f"quoted token {span!r} was not detected")
                        self.assertEqual(signal["legacy"], is_legacy)
                    checked += 1
                elif "Generated by" in span and (PRODUCT in span or LEGACY_PRODUCT in span):
                    with self.subTest(path=rel, quoted=span):
                        self.assertIn(
                            "Reviewed up to", span,
                            f"doc footer quote {span!r} in {rel} no longer carries "
                            "its own 'Reviewed up to' label — this guard synthesizes "
                            "no label of its own, so a doc change that dropped the "
                            "label would otherwise go undetected",
                        )
                        # Substitute the doc's own sha placeholder (e.g. {full_sha},
                        # {sha}) with a real sha — the label itself is the doc's own
                        # text, untouched.
                        text = re.sub(r"\{\w*sha\w*\}", SHA_40, span)
                        signal = detect_signal(text)
                        self.assertIsNotNone(
                            signal, f"quoted footer {span!r} was not detected")
                        self.assertEqual(signal["sha"], SHA_40)
                    checked += 1
                    checked_footer += 1
        self.assertGreater(
            checked, 0,
            "no doc quotes a raw marker token or 'Generated by <product>' string "
            "any more — this guard has gone vacuous; re-point QUOTE_SCAN_RELS at "
            "whichever file now renders what the code writes.",
        )
        self.assertGreater(
            checked_footer, 0,
            "no doc quotes a 'Generated by <product> | Reviewed up to:' footer "
            "line any more — the footer half of this guard has gone vacuous; "
            "re-point QUOTE_SCAN_RELS at whichever file still renders it.",
        )

    def test_phase1_preflight_covers_the_required_output_fields(self):
        """Deliverable 4's branch-selection and degradation prose (issue #39 spec) is
        only actionable if the doc actually surfaces every field that gate depends on
        or a downstream template interpolates: the JSON contract's own field names,
        quoted either as a bare JSON key or as backtick-quoted prose. Derived from the
        spec's Output JSON contract and its branch-selection/degradation bullets —
        not from reading the detector script."""
        required_fields = {
            "previously_reviewed", "incremental_safe", "head_advanced",
            "new_commit_count", "last_reviewed_sha_short", "last_reviewed_sha",
            "marker", "errors", "sha_resolvable",
        }
        text = _read(self.PHASE1_REL)
        missing = {
            field for field in required_fields
            if f"`{field}`" not in text and f'"{field}"' not in text
        }
        self.assertEqual(
            missing, set(),
            f"phase1-preflight.md does not surface required output field(s): {sorted(missing)}",
        )

    def test_phase2_triage_prescribes_the_bounded_incremental_diff(self):
        """The incremental branch must PRESCRIBE the file-bounded two-dot diff.

        Asserting the three-dot form is present is not a guard: the doc now
        names that form only to forbid it (`incremental_safe` guarantees the SHA
        is an ancestor, so three-dot collapses to two-dot and would sweep in an
        unrelated base-branch merge). A test that accepts either form would pass
        on a doc that told the model to do the wrong thing.
        """
        text = _read(self.PHASE2_REL)
        self.assertIn("git diff {last_reviewed_sha}..HEAD --", text)
        # The unbounded form may appear only inside a prohibition.
        for match in re.finditer(r"git diff (?:--name-only )?\{last_reviewed_sha\}\.\.\.HEAD", text):
            context = text[max(0, match.start() - 200):match.start()]
            self.assertRegex(
                context, r"[Dd]o \*\*not\*\* use|never use|do not use",
                "the three-dot incremental diff appears without a prohibition "
                "in the preceding 200 chars — the doc may be prescribing it",
            )

    def test_post_review_defines_no_local_build_footer(self):
        # Guards against the duplicate build_footer definition D1 exists to kill:
        # post_review.py must import the builder from review_marker, not redefine it.
        text = _read(self.POST_REVIEW_REL)
        self.assertNotRegex(text, r"(?m)^def build_footer\(")

    def test_previously_reviewed_gate_precedes_stale_truncation_in_skill_and_phase2(self):
        """Pins the ordering that IS the data-loss fix: the previously-reviewed gate
        must run BEFORE stale-artifact truncation. Truncation zeroes every
        `code-gauntlet-*-{sha}.*` file for the current SHA, which are exactly the
        artifacts a "Skip — keep the existing review" answer is supposed to
        preserve; if truncation ever moved ahead of the gate in either doc, a
        repeat run of an already-reviewed PR at the same SHA would wipe the prior
        review's findings/report before the gate got a chance to offer keeping
        them."""
        markers = {
            self.SKILL_REL: (
                "### Previously-reviewed gate (PR/MR targets only)",
                "### Clean stale files",
            ),
            self.PHASE2_REL: (
                "**3. Previously-reviewed gate**",
                "**4. Truncate stale files**",
            ),
        }
        for rel, (gate_marker, truncate_marker) in markers.items():
            with self.subTest(path=rel):
                text = _read(rel)
                gate_idx = text.find(gate_marker)
                truncate_idx = text.find(truncate_marker)
                self.assertNotEqual(gate_idx, -1, f"gate section marker not found in {rel}")
                self.assertNotEqual(truncate_idx, -1, f"truncation section marker not found in {rel}")
                self.assertLess(
                    gate_idx, truncate_idx,
                    f"{rel}: the previously-reviewed gate must appear BEFORE stale-file "
                    "truncation in doc order — truncation zeroes the artifacts a "
                    "'Skip — keep the existing review' answer exists to preserve, so if "
                    "this ordering regresses, a repeat run at the same SHA silently "
                    "destroys the prior review's findings/report before the gate can "
                    "even offer to keep them",
                )


if __name__ == "__main__":
    unittest.main()

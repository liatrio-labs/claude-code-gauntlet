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
  - TestFindingMarker         — issue #132's per-finding delivery marker: build ->
    parse round-trip, last-wins, malformed payloads rejected without raising, and
    non-collision with the summary marker in both directions.
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
    find_marker,
    has_prose_footer,
    parse_prose_footer,
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
        payload = json.dumps(
            {
                "version": "3.0",
                "sha": SHA_40,
                "future_field": "xyz",
                "nested": {"a": 1},
            }
        )
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
        payload = json.dumps(
            {
                "version": "3.0",
                "sha": SHA_40,
                "findings": ["this string literally contains --> inside it"],
            }
        )
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
            len(set(results)),
            1,
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
                    f"<!-- {MARKER_TOKEN}: "
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
                except Exception as exc:  # noqa: BLE001  # pragma: no cover
                    self.fail(f"raised {exc!r} on {text!r}")


# ---------------------------------------------------------------------------
# TestMaxMarkerScans — the DoS-hardening bound (a hostile body of unclosed
# marker tokens must not cost O(tokens x length)).
# ---------------------------------------------------------------------------


class TestMaxMarkerScans(unittest.TestCase):
    def test_valid_marker_within_the_scan_window_is_still_found(self):
        """A body noisy with malformed candidates, where the one valid marker
        sits inside the last _MAX_MARKER_SCANS matches, still resolves —
        the cap must not cost correctness in the common case."""
        noise = "<!-- code-gauntlet-findings: {broken -->\n" * 10
        valid = build_marker(SHA_40, 1)
        text = noise + valid
        signal = detect_signal(text)
        self.assertIsNotNone(signal)
        self.assertEqual(signal["sha"], SHA_40)

    def test_valid_marker_beyond_the_scan_window_is_not_found(self):
        """A valid marker followed by more than _MAX_MARKER_SCANS malformed
        candidates falls outside the last-N window find_marker scans, and is
        therefore not recovered. This pins the cap actually bounding work
        rather than being dead code — if the implementation ever scanned
        every match instead of ``matches[-_MAX_MARKER_SCANS:]``, this test
        would start failing (recovering a signal it should not)."""
        valid = build_marker(SHA_40, 1)
        trailing_noise = "<!-- code-gauntlet-findings: {broken -->\n" * (
            review_marker._MAX_MARKER_SCANS + 5
        )
        text = valid + "\n" + trailing_noise
        self.assertIsNone(find_marker(text))
        self.assertIsNone(detect_signal(text))

    def test_many_malformed_candidates_never_raise_or_hang(self):
        """Sanity check on the scenario the bound exists for: a body with far
        more marker-token occurrences than the scan window, all malformed."""
        garbage = "<!-- code-gauntlet-findings: {" * 5000
        try:
            result = find_marker(garbage)
        except Exception as exc:  # noqa: BLE001  # pragma: no cover
            self.fail(f"raised {exc!r} on a garbage-heavy body")
        self.assertIsNone(result)


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
        self.assertEqual(
            len(body), length_after_first, "double-append must add nothing"
        )
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
            f'<!-- {MARKER_TOKEN}: {{"version":"3.0"}} -->\n\nGenerated by {PRODUCT}\n'
        )
        self.assertIsNone(
            detect_signal(body),
            "the pre-existing body must carry no usable signal (motivating case)",
        )
        addition = build_footer(1, SHA_40, body=body)
        self.assertNotEqual(
            addition,
            "",
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
        self.assertEqual(
            list(payload.keys()), ["version", "findings_count", "sha", "findings"]
        )
        self.assertEqual(payload["findings"], findings)

        signal = detect_signal(marker_text)
        self.assertIsNotNone(signal)
        self.assertEqual(signal["marker"]["findings"], findings)


# ---------------------------------------------------------------------------
# TestFindingMarker — issue #132's per-finding delivery marker.
# ---------------------------------------------------------------------------

# Single-character keys, like the SHAs above: a full-alphabet 16-hex fixture reads
# as a credential to the gitleaks entropy rule and fails the lint gate.
KEY_16 = "a" * 16
OTHER_KEY_16 = "b" * 16


class TestFindingMarker(unittest.TestCase):
    def test_build_parse_round_trip(self):
        for sha in (SHA_40, SHA_8):
            with self.subTest(sha=sha):
                parsed = find_finding_marker(build_finding_marker(sha, KEY_16))
                self.assertEqual(parsed, {"sha": sha, "key": KEY_16})

    def test_round_trip_through_a_realistic_comment_body(self):
        """Mirrors post_gitlab's composition: rendered body, blank line, marker."""
        body = "**🟠 [HIGH] SQL injection risk**\n\nUse a parameterized query.\n"
        text = f"{body}\n\n{build_finding_marker(SHA_40, KEY_16)}"
        self.assertEqual(find_finding_marker(text)["key"], KEY_16)

    def test_last_marker_wins(self):
        """post_review APPENDS its marker, so a marker spelled inside a finding's own
        text always precedes the mechanical one. Finding titles and bodies are NOT run
        through _sanitize_outbound_prose, so that forgery reaches the wire verbatim and
        must be shadowed rather than shadow."""
        forged = build_finding_marker(SHA_40, OTHER_KEY_16)
        real = build_finding_marker(SHA_40, KEY_16)
        self.assertEqual(find_finding_marker(f"{forged}\n\n{real}")["key"], KEY_16)

    def test_malformed_payloads_are_ignored_and_never_raise(self):
        """An unhashable key would abort post_review's delivery loop mid-flight, so a
        payload of the right syntax but the wrong types must simply not be a record."""
        payloads = [
            {"sha": SHA_40, "key": ["not", "a", "string"]},
            {"sha": SHA_40, "key": {"nested": "object"}},
            {"sha": SHA_40, "key": 123456789},
            {"sha": SHA_40, "key": None},
            {"sha": SHA_40, "key": KEY_16.upper()},  # hex, but not lowercase
            {"sha": SHA_40, "key": KEY_16[:15]},  # too short
            {"sha": SHA_40, "key": KEY_16 + "0"},  # too long
            {"sha": SHA_40, "key": "g" * 16},  # right length, not hex
            {"sha": SHA_40},  # no key at all
            {"key": KEY_16},  # no sha
            {"sha": "not-a-sha", "key": KEY_16},
            {"sha": ["a" * 40], "key": KEY_16},
        ]
        for payload in payloads:
            text = f"<!-- {FINDING_MARKER_TOKEN}: {json.dumps(payload)} -->"
            with self.subTest(payload=payload):
                try:
                    self.assertIsNone(find_finding_marker(text))
                except Exception as exc:  # noqa: BLE001  # pragma: no cover
                    self.fail(f"raised {exc!r} on {text!r}")

    def test_assorted_garbage_never_raises(self):
        garbage = [
            "",
            None,
            123,
            f"<!-- {FINDING_MARKER_TOKEN}: -->",
            f"<!-- {FINDING_MARKER_TOKEN}: {{broken -->",
            # Brace-delimited (so the regex spans it) but not JSON.
            f"<!-- {FINDING_MARKER_TOKEN}: {{not json at all}} -->",
            f"<!-- {FINDING_MARKER_TOKEN}: [] -->",
            f'<!-- {FINDING_MARKER_TOKEN}: {{"sha":"{SHA_40}","key":"{KEY_16}"}}',
            "{" * 50,
        ]
        for text in garbage:
            with self.subTest(text=text):
                try:
                    self.assertIsNone(find_finding_marker(text))
                except Exception as exc:  # noqa: BLE001  # pragma: no cover
                    self.fail(f"raised {exc!r} on {text!r}")

    def test_a_valid_marker_after_a_malformed_one_is_still_found(self):
        broken = f"<!-- {FINDING_MARKER_TOKEN}: {{broken -->"
        text = f"{broken}\n{build_finding_marker(SHA_40, KEY_16)}"
        self.assertEqual(find_finding_marker(text)["key"], KEY_16)

    def test_the_two_marker_kinds_never_see_each_other(self):
        """The tokens share no bytes, so a body carrying BOTH resolves each reader to
        its own marker — collision is structural, not a matter of parse order."""
        summary = build_marker(SHA_40, 3)
        finding = build_finding_marker(SHA_40, KEY_16)
        both = f"{summary}\n\n{finding}"

        self.assertEqual(find_finding_marker(both), {"sha": SHA_40, "key": KEY_16})
        self.assertEqual(find_marker(both)["findings_count"], 3)
        self.assertEqual(detect_signal(both)["signal"], "marker")

        # ...and each reader is blind to the other's marker standing alone.
        self.assertIsNone(find_finding_marker(summary))
        self.assertIsNone(find_marker(finding))
        self.assertIsNone(detect_signal(finding))


# ---------------------------------------------------------------------------
# TestSelectLatest
# ---------------------------------------------------------------------------


class TestSelectLatest(unittest.TestCase):
    def test_newest_timestamp_wins_across_mixed_sources(self):
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
        self.assertIsNotNone(result)
        self.assertEqual(result["sha"], "b" * 8)
        self.assertEqual(result["source"], "issue_comment")
        self.assertEqual(result["timestamp"], "2026-06-15T12:00:00Z")

    def test_none_timestamp_sorts_lowest(self):
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
        self.assertIsNotNone(result)
        self.assertEqual(result["sha"], "b" * 8)

    def test_unparseable_timestamp_sorts_lowest(self):
        # A naive lexicographic compare would rank "not-a-timestamp" above a real
        # ISO8601 string ('n' > '2' in ASCII) — this pins the required special case.
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
        self.assertIsNotNone(result)
        self.assertEqual(result["sha"], "b" * 8)

    def test_ties_break_to_latest_input_order(self):
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
        self.assertIsNotNone(result)
        self.assertEqual(
            result["sha"], "b" * 8, "the later entry in input order must win a tie"
        )

    def test_no_signal_entries_are_ignored(self):
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
        self.assertIsNotNone(result)
        self.assertEqual(result["sha"], "a" * 8)

    def test_all_entries_without_signal_returns_none(self):
        entries = [
            {
                "body": "nope",
                "timestamp": "2026-01-01T00:00:00Z",
                "source": "x",
                "id": 1,
            },
            {
                "body": "still nope",
                "timestamp": "2026-02-01T00:00:00Z",
                "source": "y",
                "id": 2,
            },
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
    HEADLESS_MODE_REL = "skills/code-gauntlet/references/headless-mode.md"
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
                        {"version": "3.0", "findings_count": 1, "sha": SHA_40}
                    )
                    text = f"<!-- {span}: {payload} -->"
                    with self.subTest(path=rel, quoted=span):
                        signal = detect_signal(text)
                        self.assertIsNotNone(
                            signal, f"quoted token {span!r} was not detected"
                        )
                        self.assertEqual(signal["legacy"], is_legacy)
                    checked += 1
                elif "Generated by" in span and (
                    PRODUCT in span or LEGACY_PRODUCT in span
                ):
                    with self.subTest(path=rel, quoted=span):
                        self.assertIn(
                            "Reviewed up to",
                            span,
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
                            signal, f"quoted footer {span!r} was not detected"
                        )
                        self.assertEqual(signal["sha"], SHA_40)
                    checked += 1
                    checked_footer += 1
        self.assertGreater(
            checked,
            0,
            "no doc quotes a raw marker token or 'Generated by <product>' string "
            "any more — this guard has gone vacuous; re-point QUOTE_SCAN_RELS at "
            "whichever file now renders what the code writes.",
        )
        self.assertGreater(
            checked_footer,
            0,
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
        text = _read(self.PHASE1_REL)
        missing = {
            field
            for field in required_fields
            if f"`{field}`" not in text and f'"{field}"' not in text
        }
        self.assertEqual(
            missing,
            set(),
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
        for match in re.finditer(
            r"git diff (?:--name-only )?\{last_reviewed_sha\}\.\.\.HEAD", text
        ):
            context = text[max(0, match.start() - 200) : match.start()]
            self.assertRegex(
                context,
                r"[Dd]o \*\*not\*\* use|never use|do not use",
                "the three-dot incremental diff appears without a prohibition "
                "in the preceding 200 chars — the doc may be prescribing it",
            )

    def test_post_review_defines_no_local_build_footer(self):
        # Guards against the duplicate build_footer definition D1 exists to kill:
        # post_review.py must import the builder from review_marker, not redefine it.
        text = _read(self.POST_REVIEW_REL)
        self.assertNotRegex(text, r"(?m)^def build_footer\(")

    def test_previously_reviewed_gate_precedes_stale_truncation_in_skill_and_phase2(
        self,
    ):
        """Pins the ordering that IS the data-loss fix: the previously-reviewed gate
        must run BEFORE stale-artifact truncation. Truncation zeroes every
        `code-gauntlet-*-{sha}.*` file for the current SHA, which are exactly the
        artifacts a "Skip — keep the existing review" answer is supposed to
        preserve; if truncation ever moved ahead of the gate in either doc, a
        repeat run of an already-reviewed PR at the same SHA would wipe the prior
        review's findings/report before the gate got a chance to offer keeping
        them.

        SKILL.md now runs both steps as labelled sections of one composite Bash
        call (issue #38 collapsed Phase 2's independent round trips), so the
        markers there are the section labels rather than headings; the ordering
        requirement is identical either way. Because the two steps now share a
        single call, the gate can no longer withhold truncation just by running
        first — so SKILL.md additionally guards the truncate branch on the
        detector's own `previously_reviewed`/`sha_resolvable`/`last_reviewed_sha`/
        `head_sha` facts (the exact-match test for "the reviewed commit IS the
        current head" — `head_advanced` alone cannot distinguish that case from
        an unresolvable SHA or rewritten history, both of which also read
        `head_advanced: false` but must NOT defer truncation), and that guard is
        pinned below."""
        markers = {
            self.SKILL_REL: (
                'echo "=== prior_review ==="',
                'echo "=== stale_truncate ==="',
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
                self.assertNotEqual(
                    gate_idx, -1, f"gate section marker not found in {rel}"
                )
                self.assertNotEqual(
                    truncate_idx, -1, f"truncation section marker not found in {rel}"
                )
                self.assertLess(
                    gate_idx,
                    truncate_idx,
                    f"{rel}: the previously-reviewed gate must appear BEFORE stale-file "
                    "truncation in doc order — truncation zeroes the artifacts a "
                    "'Skip — keep the existing review' answer exists to preserve, so if "
                    "this ordering regresses, a repeat run at the same SHA silently "
                    "destroys the prior review's findings/report before the gate can "
                    "even offer to keep them",
                )

        # The in-composite guard: truncation must be conditional on the detector's
        # facts, or running both steps in one Bash call would destroy exactly the
        # artifacts the gate exists to protect, before the user is ever asked.
        skill = _read(self.SKILL_REL)
        truncate_block = skill[skill.find('echo "=== stale_truncate ==="') :]
        truncate_block = truncate_block[: truncate_block.find("```", 1)]
        for fact in (
            "previously_reviewed",
            "sha_resolvable",
            "last_reviewed_sha",
            "head_sha",
        ):
            self.assertIn(
                fact,
                truncate_block,
                f"{self.SKILL_REL}: the stale_truncate section must gate on the "
                f"detector's `{fact}` fact. Running the gate and the truncation in "
                "one composite Bash call means doc order alone no longer protects "
                "the prior review's artifacts — only this guard does.",
            )

    def test_headless_skip_semantics_agree_between_skill_and_headless_mode(self):
        """D1 regression pin: SKILL.md's Phase 2 headless note and
        headless-mode.md's per-gate table must not contradict on when headless
        `skip` stops the run for the previously-reviewed gate. A prior mismatch
        had SKILL.md claim `skip` stops the run whenever `previously_reviewed`
        is true, while headless-mode.md said `skip` only stops the run when
        `sha_is_ancestor` is ALSO true — on rewritten history (rebase, squash,
        or a backward force-push) the reviewed commit is no longer an ancestor
        of HEAD, so the tree is effectively unreviewed and `skip` must not
        discard it. Keyed on substantive tokens (`sha_is_ancestor`, `skip`, the
        `REVIEWED_POLICY` env var) rather than an exact sentence, so it survives
        rewording; the failure message names both files so the next person
        knows to sync them."""
        for rel in (self.SKILL_REL, self.HEADLESS_MODE_REL):
            text = _read(rel)
            matching_lines = [
                line
                for line in text.splitlines()
                if "REVIEWED_POLICY" in line
                and re.search(r"\bskip\b", line, re.IGNORECASE)
                and "stops the run" in line
            ]
            with self.subTest(path=rel):
                self.assertTrue(
                    matching_lines,
                    f"{rel} has no line stating the headless `skip` stop-condition "
                    "for the previously-reviewed gate (REVIEWED_POLICY + skip + "
                    f"'stops the run') — sync {self.SKILL_REL} and "
                    f"{self.HEADLESS_MODE_REL} so both describe the same behavior.",
                )
                for line in matching_lines:
                    self.assertIn(
                        "sha_is_ancestor",
                        line,
                        f"{rel}: a headless `skip`-stops-the-run statement for the "
                        "previously-reviewed gate does not require `sha_is_ancestor` "
                        f"— this contradicts the other file; sync {self.SKILL_REL} "
                        f"and {self.HEADLESS_MODE_REL} so both agree that skip does "
                        "NOT stop the run on rewritten history "
                        "(sha_is_ancestor == false).",
                    )

    def test_phase2_triage_never_appends_to_tracked_gitignore(self):
        """D2 regression pin: phase2-triage.md must never prescribe appending to
        the repo's TRACKED .gitignore — SKILL.md explicitly forbids this (it
        silently dirties the reviewed repo's working tree with an undisclosed
        edit to a user file) and instead writes to the untracked, repo-local
        `.git/info/exclude`. A prior half-applied fix left phase2-triage.md's
        gitignore step using `>> .gitignore` while SKILL.md, and even
        phase2-triage.md's own later prose, already referenced
        `.git/info/exclude` — a model following phase2-triage.md's literal
        command would dirty the user's repo."""
        text = _read(self.PHASE2_REL)
        self.assertNotIn(
            ">> .gitignore",
            text,
            f"{self.PHASE2_REL} appends to the repo's tracked .gitignore — this "
            "dirties the reviewed user's repo with an undisclosed edit; use "
            "`.git/info/exclude` instead (matching SKILL.md's forbidding rule).",
        )

    def test_skill_invokes_ensure_output_dir_and_has_no_gitignore_composite(self):
        """Issue #86: Phase 1 owns containment via ensure_output_dir.py; Composite A
        must not carry a vestigial gitignore bash section."""
        skill = _read(self.SKILL_REL)
        self.assertIn(
            "ensure_output_dir.py",
            skill,
            f"{self.SKILL_REL} must invoke scripts/ensure_output_dir.py in Phase 1",
        )
        self.assertNotIn(
            'echo "=== gitignore ==="',
            skill,
            f"{self.SKILL_REL} still has Composite A gitignore bash — ignore "
            "establishment moved to ensure_output_dir.py in Phase 1",
        )
        self.assertNotIn(
            ">> .gitignore",
            skill,
            f"{self.SKILL_REL} must never append to the tracked .gitignore",
        )

    def test_phase2_points_at_ensure_output_dir_not_independent_gitignore(self):
        """Issue #86: phase2-triage owns a pointer, not a second ignore implementation."""
        text = _read(self.PHASE2_REL)
        self.assertIn(
            "ensure_output_dir.py",
            text,
            f"{self.PHASE2_REL} must point at ensure_output_dir.py for ignore establishment",
        )
        self.assertNotIn(
            "skip if using env var override",
            text,
            f"{self.PHASE2_REL} still tells the model to skip ignore on env override — "
            "the script handles in-repo vs out-of-repo",
        )
        self.assertNotIn(
            "artifacts will show as untracked files",
            text,
            f"{self.PHASE2_REL} still documents disclose-and-continue for unwritable "
            "exclude — that is now a Phase 1 hard stop",
        )

    def test_markdown_delivery_is_path_surface_not_root_write(self):
        """Issue #86 refined-A: the Markdown only branch surfaces artifactPaths.report;
        no default root-level code-gauntlet-{date}.md write instruction survives."""
        phase8 = "skills/code-gauntlet/references/phase8-delivery.md"
        delivery = self.DELIVERY_GUIDE_REL
        phase8_text = _read(phase8)
        self.assertIn(
            "artifactPaths.report",
            phase8_text,
            f"{phase8} the Markdown only branch must name artifactPaths.report as the delivery source",
        )
        self.assertIn(
            "do not write a new file",
            phase8_text.lower().replace("**", ""),
            f"{phase8} the Markdown only branch must forbid a fresh write",
        )
        # Forbid the old default root write as an instruction (mentions in "never"
        # sentences are OK only if they do not prescribe write-to-root).
        for rel, text in (
            (phase8, phase8_text),
            (delivery, _read(delivery)),
            (self.PHASE1_REL, _read(self.PHASE1_REL)),
            (
                "skills/code-gauntlet/references/review-md-spec.md",
                _read("skills/code-gauntlet/references/review-md-spec.md"),
            ),
        ):
            with self.subTest(path=rel):
                self.assertNotRegex(
                    text,
                    r"[Ww]rite(?:\s+the\s+full\s+report)?\s+to\s+`?\./code-gauntlet-",
                    f"{rel} still instructs writing a root-level code-gauntlet-* file",
                )
                self.assertNotIn(
                    "Save as code-gauntlet-{date}.md",
                    text,
                    f"{rel} still promises creating code-gauntlet-{{date}}.md",
                )
                self.assertNotIn(
                    "Save as `code-gauntlet-{date}.md`",
                    text,
                    f"{rel} still promises creating code-gauntlet-{{date}}.md",
                )

    def test_headless_markdown_delivery_is_path_only(self):
        """Issue #86: headless markdown must not re-invent a root write."""
        text = _read(self.HEADLESS_MODE_REL)
        self.assertIn(
            "artifactPaths.report",
            text,
            f"{self.HEADLESS_MODE_REL} must state headless markdown = persisted report path",
        )
        self.assertIn(
            "no additional file is written",
            text,
            f"{self.HEADLESS_MODE_REL} must state no additional markdown file is written",
        )


# ---------------------------------------------------------------------------
# Round-3 fix regressions — each of these MUST fail if its fix is reverted.
#
# Rounds 1-3 each regressed the previous round's work, and a mutation test then
# showed four round-3 fixes could be reverted with the whole suite still green.
# Issue #39 requirement 6 is explicit that the fixes "cannot silently regress
# again", so each one is pinned here by the exact behaviour that motivated it.
# ---------------------------------------------------------------------------


class TestRound3FixRegressions(unittest.TestCase):
    def test_numeric_overflow_token_rejects_the_marker(self):
        """R3-2: parse_constant only sees literal NaN/Infinity tokens. `1e999` is
        an ordinary numeric token that overflows to inf, and json.dumps then
        re-emits a bare `Infinity`, making the detector's own stdout invalid
        JSON. Reverting _has_non_finite makes this marker parse."""
        for token in ("1e999", "-1e999", "1E999"):
            with self.subTest(token=token):
                text = (
                    f'<!-- {MARKER_TOKEN}: {{"version":{token},"sha":"{SHA_40}"}} -->'
                )
                self.assertIsNone(find_marker(text))
                self.assertIsNone(detect_signal(text))

    def test_finite_numbers_still_parse(self):
        """The overflow guard must not reject ordinary numeric payloads."""
        text = f'<!-- {MARKER_TOKEN}: {{"version":1,"findings_count":250,"sha":"{SHA_40}"}} -->'
        signal = detect_signal(text)
        self.assertIsNotNone(signal)
        self.assertEqual(signal["sha"], SHA_40)

    def test_marker_guard_requires_this_sha(self):
        """R3-3: a stale/foreign marker must NOT suppress ours. find_marker is
        last-wins, so suppressing on 'any marker' let the stale sha be what a
        rerun reads while our prose line advertised a different commit."""
        stale = build_marker("d" * 40, 1)
        appended = build_footer(2, SHA_40, body=stale)
        self.assertIn(f'"sha":"{SHA_40}"', appended)
        recovered = detect_signal(stale + appended)
        self.assertEqual(recovered["sha"], SHA_40)

    def test_prose_guard_requires_this_sha(self):
        """R3-3 (symmetric half): a stale prose footer must not suppress ours, or
        the posted review advertises a commit the review never examined."""
        stale = "---\n" + build_prose_footer("d" * 40)
        appended = build_footer(1, SHA_40, body=stale)
        self.assertIn(f"Reviewed up to: {SHA_40}", appended)

    def test_double_append_is_still_a_no_op(self):
        """Both sha checks must not break idempotence for OUR own signal."""
        body = build_footer(3, SHA_40)
        self.assertEqual(build_footer(3, SHA_40, body=body), "")

    def test_utc_offset_timestamps_order_by_absolute_instant(self):
        """R3-4: a lexicographic compare picks the OLDER signal when a producer
        emits an offset instead of Z. 01:00+02:00 is 23:00Z the previous day, so
        it must lose to 00:30Z. Reverting _sort_key inverts this."""
        older, newer = "a" * 40, "b" * 40
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
        self.assertEqual(select_latest(entries)["sha"], newer)
        self.assertEqual(select_latest(list(reversed(entries)))["sha"], newer)

    def test_sort_keys_stay_mutually_comparable(self):
        """Naive, offset-bearing and Z-suffixed timestamps must yield one key
        shape — mixing an epoch string with an ISO string would sort every naive
        timestamp above every aware one."""
        keys = [
            review_marker._sort_key(t)
            for t in (
                "2026-07-26T00:30:00Z",
                "2026-07-26T01:00:00+02:00",
                "2026-07-26T00:30:00",
            )
        ]
        self.assertTrue(all(k and k[0].isdigit() for k in keys), keys)
        self.assertEqual(keys[0], keys[2])  # naive is read as UTC

    def test_unparseable_timestamp_sorts_lowest_not_highest(self):
        real = "b" * 40
        entries = [
            {
                "body": build_footer(1, real),
                "timestamp": "2020-01-01T00:00:00Z",
                "source": "review",
            },
            {
                "body": build_footer(1, "a" * 40),
                "timestamp": "not-a-date",
                "source": "review",
            },
        ]
        self.assertEqual(select_latest(entries)["sha"], real)

    def test_deeply_nested_marker_never_raises(self):
        """R3-5: json raises RecursionError (a RuntimeError, not a ValueError) on
        deeply nested input, which escaped the never-raise contract."""
        payload = "[" * 40000 + "]" * 40000
        text = f'<!-- {MARKER_TOKEN}: {{"sha":"{SHA_40}","x":{payload}}} -->'
        try:
            self.assertIsNone(find_marker(text))
        except RecursionError:
            self.fail("find_marker raised RecursionError")


if __name__ == "__main__":
    unittest.main()

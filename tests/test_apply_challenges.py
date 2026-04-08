"""
Tests for scripts/apply_challenges.py

Covers:
  - load_filtered: filtered-envelope, findings-envelope, bare array,
    missing file, invalid JSON, wrong shape
  - load_challenges: bare array, challenges-envelope, missing file,
    invalid JSON, wrong shape
  - _downgrade_severity: step order, lowest-severity edge, unknown severity
  - apply_challenges: score < 25 removes, score 25-49 downgrades/removes-if-at-low,
    score 50-74 contests, score >= 75 survives, surfaced re-route,
    unchallenged pass-through, missing id in challenge, missing score,
    non-integer score, justification copied
  - rank_findings: severity order, confidence tiebreak, description-length tiebreak
  - dedup_cross_agent reuse: cross-agent dedup runs post-challenge
  - max_findings cap: cap applied after ranking, cap_dropped populated
  - main() CLI integration: stdout output, --output file, --max-findings,
    prior eliminated passed through, stats fields
"""

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from scripts.apply_challenges import (
    load_filtered,
    load_challenges,
    apply_challenges,
    rank_findings,
    _downgrade_severity,
    SEVERITY_ORDER,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_finding(**kwargs):
    defaults = {
        "id": "bug-1",
        "file": "src/foo.py",
        "line_start": 10,
        "line_end": 12,
        "severity": "high",
        "confidence": 80,
        "title": "Real bug",
        "description": "The function does not handle null input correctly at runtime.",
        "agent": "bug-detector",
        "dimension": "bug",
        "report_destination": "main",
        "report_tag": "main",
        "origin": "new",
    }
    defaults.update(kwargs)
    return defaults


def _make_challenge(id_, score, justification=None):
    entry = {"id": id_, "score": score}
    if justification:
        entry["justification"] = justification
    return entry


def _write_json(data):
    """Write data to a temp file and return the path."""
    f = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
    json.dump(data, f)
    f.close()
    return f.name


# ---------------------------------------------------------------------------
# _downgrade_severity
# ---------------------------------------------------------------------------

class TestDowngradeSeverity(unittest.TestCase):

    def test_critical_to_high(self):
        self.assertEqual(_downgrade_severity("critical"), "high")

    def test_high_to_medium(self):
        self.assertEqual(_downgrade_severity("high"), "medium")

    def test_medium_to_low(self):
        self.assertEqual(_downgrade_severity("medium"), "low")

    def test_low_returns_none(self):
        self.assertIsNone(_downgrade_severity("low"))

    def test_unknown_severity_returns_none(self):
        self.assertIsNone(_downgrade_severity("unknown"))

    def test_case_insensitive(self):
        self.assertEqual(_downgrade_severity("HIGH"), "medium")


# ---------------------------------------------------------------------------
# load_filtered
# ---------------------------------------------------------------------------

class TestLoadFiltered(unittest.TestCase):

    def test_filtered_envelope(self):
        """filter_findings.py output shape"""
        data = {
            "filtered": [_make_finding(id="f1")],
            "eliminated": [_make_finding(id="e1", eliminated_by="threshold")],
            "stats": {"total": 2},
        }
        path = _write_json(data)
        try:
            findings, eliminated, envelope = load_filtered(path)
            self.assertEqual(len(findings), 1)
            self.assertEqual(findings[0]["id"], "f1")
            self.assertEqual(len(eliminated), 1)
            self.assertIsNotNone(envelope)
        finally:
            os.unlink(path)

    def test_findings_envelope(self):
        data = {"findings": [_make_finding(id="f1")]}
        path = _write_json(data)
        try:
            findings, eliminated, envelope = load_filtered(path)
            self.assertEqual(len(findings), 1)
            self.assertEqual(eliminated, [])
        finally:
            os.unlink(path)

    def test_bare_array(self):
        data = [_make_finding(id="f1"), _make_finding(id="f2")]
        path = _write_json(data)
        try:
            findings, eliminated, envelope = load_filtered(path)
            self.assertEqual(len(findings), 2)
            self.assertIsNone(envelope)
            self.assertEqual(eliminated, [])
        finally:
            os.unlink(path)

    def test_missing_file_exits(self):
        with self.assertRaises(SystemExit):
            load_filtered("/nonexistent/path.json")

    def test_invalid_json_exits(self):
        f = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
        f.write("not json {{{")
        f.close()
        try:
            with self.assertRaises(SystemExit):
                load_filtered(f.name)
        finally:
            os.unlink(f.name)

    def test_wrong_shape_exits(self):
        path = _write_json({"other_key": []})
        try:
            with self.assertRaises(SystemExit):
                load_filtered(path)
        finally:
            os.unlink(path)


# ---------------------------------------------------------------------------
# load_challenges
# ---------------------------------------------------------------------------

class TestLoadChallenges(unittest.TestCase):

    def test_bare_array(self):
        data = [{"id": "f1", "score": 80}, {"id": "f2", "score": 30}]
        path = _write_json(data)
        try:
            result = load_challenges(path)
            self.assertEqual(len(result), 2)
        finally:
            os.unlink(path)

    def test_challenges_envelope(self):
        data = {"challenges": [{"id": "f1", "score": 60}]}
        path = _write_json(data)
        try:
            result = load_challenges(path)
            self.assertEqual(len(result), 1)
            self.assertEqual(result[0]["id"], "f1")
        finally:
            os.unlink(path)

    def test_missing_file_exits(self):
        with self.assertRaises(SystemExit):
            load_challenges("/nonexistent/challenges.json")

    def test_invalid_json_exits(self):
        f = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
        f.write("[bad json")
        f.close()
        try:
            with self.assertRaises(SystemExit):
                load_challenges(f.name)
        finally:
            os.unlink(f.name)

    def test_wrong_shape_exits(self):
        path = _write_json({"other": []})
        try:
            with self.assertRaises(SystemExit):
                load_challenges(path)
        finally:
            os.unlink(path)


# ---------------------------------------------------------------------------
# apply_challenges — threshold rules
# ---------------------------------------------------------------------------

class TestApplyChallenges(unittest.TestCase):

    def test_score_below_25_removes(self):
        findings = [_make_finding(id="f1", severity="high")]
        challenges = [_make_challenge("f1", 20)]
        active, eliminated, stats = apply_challenges(findings, challenges)
        self.assertEqual(len(active), 0)
        self.assertEqual(len(eliminated), 1)
        self.assertEqual(eliminated[0]["eliminated_by"], "challenge:removed")
        self.assertEqual(stats["challenge_removed"], 1)

    def test_score_24_boundary_removes(self):
        findings = [_make_finding(id="f1")]
        challenges = [_make_challenge("f1", 24)]
        active, eliminated, stats = apply_challenges(findings, challenges)
        self.assertEqual(len(active), 0)
        self.assertEqual(stats["challenge_removed"], 1)

    # --- R02.1: security dimension with score < 25 downgrades instead of removes ---

    def test_score_below_25_security_downgrades_not_removes(self):
        """Security findings with score < 25 are downgraded, not removed."""
        findings = [_make_finding(id="f1", severity="high", dimension="security")]
        challenges = [_make_challenge("f1", 10)]
        active, eliminated, stats = apply_challenges(findings, challenges)
        self.assertEqual(len(active), 1)
        self.assertEqual(len(eliminated), 0)
        self.assertEqual(active[0]["severity"], "medium")
        self.assertTrue(active[0].get("severity_downgraded"))
        self.assertEqual(active[0].get("original_severity"), "high")
        self.assertEqual(active[0]["report_destination"], "suggestion")
        self.assertEqual(stats["challenge_downgraded"], 1)
        self.assertEqual(stats["challenge_removed"], 0)

    def test_score_below_25_security_at_low_removes(self):
        """Security findings at lowest severity (low) with score < 25 are removed."""
        findings = [_make_finding(id="f1", severity="low", dimension="security")]
        challenges = [_make_challenge("f1", 5)]
        active, eliminated, stats = apply_challenges(findings, challenges)
        self.assertEqual(len(active), 0)
        self.assertEqual(len(eliminated), 1)
        self.assertEqual(eliminated[0]["eliminated_by"], "challenge:removed")
        self.assertEqual(stats["challenge_removed"], 1)

    def test_score_below_25_non_security_removes(self):
        """Non-security findings with score < 25 are still removed."""
        findings = [_make_finding(id="f1", severity="high", dimension="bug")]
        challenges = [_make_challenge("f1", 15)]
        active, eliminated, stats = apply_challenges(findings, challenges)
        self.assertEqual(len(active), 0)
        self.assertEqual(len(eliminated), 1)
        self.assertEqual(eliminated[0]["eliminated_by"], "challenge:removed")
        self.assertEqual(stats["challenge_removed"], 1)

    def test_score_0_security_critical_downgrades_to_high(self):
        """Security finding with score 0 and critical severity downgrades to high."""
        findings = [_make_finding(id="f1", severity="critical", dimension="security")]
        challenges = [_make_challenge("f1", 0)]
        active, eliminated, stats = apply_challenges(findings, challenges)
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0]["severity"], "high")
        self.assertEqual(stats["challenge_downgraded"], 1)

    def test_score_25_boundary_downgrades(self):
        findings = [_make_finding(id="f1", severity="high")]
        challenges = [_make_challenge("f1", 25)]
        active, eliminated, stats = apply_challenges(findings, challenges)
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0]["severity"], "medium")
        self.assertEqual(stats["challenge_downgraded"], 1)

    def test_score_25_49_downgrades_severity(self):
        findings = [_make_finding(id="f1", severity="critical")]
        challenges = [_make_challenge("f1", 40)]
        active, eliminated, stats = apply_challenges(findings, challenges)
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0]["severity"], "high")
        self.assertTrue(active[0].get("severity_downgraded"))
        self.assertEqual(active[0].get("original_severity"), "critical")
        self.assertEqual(active[0]["report_destination"], "suggestion")

    def test_score_25_49_already_low_removes(self):
        findings = [_make_finding(id="f1", severity="low")]
        challenges = [_make_challenge("f1", 30)]
        active, eliminated, stats = apply_challenges(findings, challenges)
        self.assertEqual(len(active), 0)
        self.assertEqual(len(eliminated), 1)
        self.assertEqual(eliminated[0]["eliminated_by"], "challenge:downgraded")
        self.assertEqual(stats["challenge_downgraded"], 1)

    def test_score_49_boundary_downgrades(self):
        findings = [_make_finding(id="f1", severity="high")]
        challenges = [_make_challenge("f1", 49)]
        active, eliminated, stats = apply_challenges(findings, challenges)
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0]["severity"], "medium")

    def test_score_50_boundary_contests(self):
        findings = [_make_finding(id="f1")]
        challenges = [_make_challenge("f1", 50)]
        active, eliminated, stats = apply_challenges(findings, challenges)
        self.assertEqual(len(active), 1)
        self.assertTrue(active[0].get("challenge_contested"))
        self.assertEqual(stats["challenge_contested"], 1)

    def test_score_50_74_contests_finding(self):
        findings = [_make_finding(id="f1")]
        challenges = [_make_challenge("f1", 60)]
        active, eliminated, stats = apply_challenges(findings, challenges)
        self.assertEqual(len(active), 1)
        self.assertTrue(active[0].get("challenge_contested"))
        self.assertEqual(stats["challenge_contested"], 1)

    def test_score_74_boundary_contests(self):
        findings = [_make_finding(id="f1")]
        challenges = [_make_challenge("f1", 74)]
        active, eliminated, stats = apply_challenges(findings, challenges)
        self.assertTrue(active[0].get("challenge_contested"))

    def test_score_75_boundary_survives(self):
        findings = [_make_finding(id="f1")]
        challenges = [_make_challenge("f1", 75)]
        active, eliminated, stats = apply_challenges(findings, challenges)
        self.assertEqual(len(active), 1)
        self.assertFalse(active[0].get("challenge_contested"))
        self.assertEqual(stats["challenge_survived"], 1)

    def test_score_100_survives(self):
        findings = [_make_finding(id="f1")]
        challenges = [_make_challenge("f1", 100)]
        active, eliminated, stats = apply_challenges(findings, challenges)
        self.assertEqual(stats["challenge_survived"], 1)

    def test_unchallenged_passes_through(self):
        findings = [_make_finding(id="f1")]
        challenges = []  # no challenge result
        active, eliminated, stats = apply_challenges(findings, challenges)
        self.assertEqual(len(active), 1)
        self.assertEqual(stats["unchallenged"], 1)

    def test_challenge_score_annotated_on_finding(self):
        findings = [_make_finding(id="f1")]
        challenges = [_make_challenge("f1", 80, justification="Clearly valid")]
        active, _, _ = apply_challenges(findings, challenges)
        self.assertEqual(active[0]["challenge_score"], 80)
        self.assertEqual(active[0]["challenge_justification"], "Clearly valid")

    def test_justification_optional(self):
        findings = [_make_finding(id="f1")]
        challenges = [_make_challenge("f1", 80)]  # no justification
        active, _, _ = apply_challenges(findings, challenges)
        self.assertNotIn("challenge_justification", active[0])

    def test_surfaced_finding_score_below_50_rerouted_to_suggestion(self):
        """Surfaced findings with score < 50 → suggestion, even if score >= 25."""
        findings = [_make_finding(
            id="f1", origin="surfaced", report_destination="main", severity="high"
        )]
        challenges = [_make_challenge("f1", 30)]  # 25-49 → downgrade
        active, _, _ = apply_challenges(findings, challenges)
        # Downgrade keeps it but moves to suggestion
        self.assertEqual(active[0]["report_destination"], "suggestion")

    def test_surfaced_finding_score_50_74_rerouted_to_suggestion(self):
        findings = [_make_finding(
            id="f1", origin="surfaced", report_destination="main"
        )]
        challenges = [_make_challenge("f1", 60)]
        active, _, _ = apply_challenges(findings, challenges)
        self.assertEqual(active[0]["report_destination"], "suggestion")
        self.assertTrue(active[0].get("challenge_contested"))

    def test_surfaced_finding_score_75_not_rerouted(self):
        """Surfaced findings that survive (score >= 75) keep their destination."""
        findings = [_make_finding(
            id="f1", origin="surfaced", report_destination="main"
        )]
        challenges = [_make_challenge("f1", 80)]
        active, _, _ = apply_challenges(findings, challenges)
        self.assertEqual(active[0]["report_destination"], "main")

    def test_challenge_missing_id_skipped(self):
        findings = [_make_finding(id="f1")]
        challenges = [{"score": 80}]  # no id
        active, _, stats = apply_challenges(findings, challenges)
        self.assertEqual(stats["unchallenged"], 1)  # finding not matched

    def test_challenge_missing_score_skipped(self):
        findings = [_make_finding(id="f1")]
        challenges = [{"id": "f1"}]  # no score
        active, _, stats = apply_challenges(findings, challenges)
        self.assertEqual(stats["unchallenged"], 1)  # challenge skipped

    def test_challenge_non_integer_score_skipped(self):
        findings = [_make_finding(id="f1")]
        challenges = [{"id": "f1", "score": "high"}]
        active, _, stats = apply_challenges(findings, challenges)
        self.assertEqual(stats["unchallenged"], 1)

    def test_multiple_findings_mixed_scores(self):
        findings = [
            _make_finding(id="f1", severity="critical"),  # removed
            _make_finding(id="f2", severity="high"),      # downgraded
            _make_finding(id="f3"),                       # contested
            _make_finding(id="f4"),                       # survived
            _make_finding(id="f5"),                       # unchallenged
        ]
        challenges = [
            _make_challenge("f1", 10),
            _make_challenge("f2", 35),
            _make_challenge("f3", 60),
            _make_challenge("f4", 90),
        ]
        active, eliminated, stats = apply_challenges(findings, challenges)
        active_ids = [f["id"] for f in active]
        self.assertNotIn("f1", active_ids)
        self.assertIn("f2", active_ids)
        self.assertIn("f3", active_ids)
        self.assertIn("f4", active_ids)
        self.assertIn("f5", active_ids)
        self.assertEqual(stats["challenge_removed"], 1)
        self.assertEqual(stats["challenge_downgraded"], 1)
        self.assertEqual(stats["challenge_contested"], 1)
        self.assertEqual(stats["challenge_survived"], 1)
        self.assertEqual(stats["unchallenged"], 1)
        self.assertEqual(len(eliminated), 1)
        self.assertEqual(eliminated[0]["id"], "f1")


# ---------------------------------------------------------------------------
# rank_findings
# ---------------------------------------------------------------------------

class TestRankFindings(unittest.TestCase):

    def test_severity_order(self):
        findings = [
            _make_finding(id="low", severity="low", confidence=90),
            _make_finding(id="critical", severity="critical", confidence=70),
            _make_finding(id="high", severity="high", confidence=80),
            _make_finding(id="medium", severity="medium", confidence=85),
        ]
        ranked = rank_findings(findings)
        ids = [f["id"] for f in ranked]
        self.assertEqual(ids, ["critical", "high", "medium", "low"])

    def test_confidence_tiebreak(self):
        findings = [
            _make_finding(id="low-conf", severity="high", confidence=70),
            _make_finding(id="high-conf", severity="high", confidence=95),
        ]
        ranked = rank_findings(findings)
        self.assertEqual(ranked[0]["id"], "high-conf")

    def test_description_length_tiebreak(self):
        findings = [
            _make_finding(id="short", severity="high", confidence=80, description="Short."),
            _make_finding(id="long", severity="high", confidence=80,
                          description="A much longer description with more detail."),
        ]
        ranked = rank_findings(findings)
        self.assertEqual(ranked[0]["id"], "long")

    # --- R02.4: risk_level tertiary key ---

    def test_risk_level_tiebreak_higher_first(self):
        """risk_level used as tertiary key; higher value ranks first."""
        findings = [
            _make_finding(id="low-risk", severity="high", confidence=80,
                          description="Same", risk_level=2),
            _make_finding(id="high-risk", severity="high", confidence=80,
                          description="Same", risk_level=8),
        ]
        ranked = rank_findings(findings)
        self.assertEqual(ranked[0]["id"], "high-risk")

    def test_risk_level_fallback_to_description_when_absent(self):
        """When risk_level absent, falls back to description length."""
        findings = [
            _make_finding(id="short", severity="high", confidence=80,
                          description="Short."),
            _make_finding(id="long", severity="high", confidence=80,
                          description="A much longer description with more detail."),
        ]
        # Neither has risk_level
        ranked = rank_findings(findings)
        self.assertEqual(ranked[0]["id"], "long")

    def test_risk_level_beats_description_length(self):
        """risk_level takes priority over description length when present."""
        findings = [
            _make_finding(id="low-risk-long", severity="high", confidence=80,
                          description="A very long description indeed for this finding.",
                          risk_level=1),
            _make_finding(id="high-risk-short", severity="high", confidence=80,
                          description="Short.", risk_level=9),
        ]
        ranked = rank_findings(findings)
        self.assertEqual(ranked[0]["id"], "high-risk-short")

    def test_empty_list(self):
        self.assertEqual(rank_findings([]), [])

    def test_single_finding(self):
        f = _make_finding()
        self.assertEqual(rank_findings([f]), [f])


# ---------------------------------------------------------------------------
# Cross-agent dedup integration (via apply_challenges module)
# ---------------------------------------------------------------------------

class TestCrossAgentDedupIntegration(unittest.TestCase):
    """Verify that dedup_cross_agent is applied after challenge processing."""

    def test_dedup_runs_post_challenge(self):
        """Two different agents at same location: core dimension wins."""
        from scripts.apply_challenges import apply_challenges, rank_findings
        from scripts.filter_findings import dedup_cross_agent

        findings = [
            _make_finding(
                id="bug-1", file="a.py", line_start=10,
                agent="bug-detector", dimension="bug", confidence=80,
            ),
            _make_finding(
                id="test-1", file="a.py", line_start=12,
                agent="test-analyzer", dimension="test_coverage", confidence=90,
            ),
        ]
        # Both survive challenge
        challenges = [_make_challenge("bug-1", 80), _make_challenge("test-1", 80)]
        active, _, _ = apply_challenges(findings, challenges)

        # Now run dedup (as main() would)
        deduped, dropped = dedup_cross_agent(active)
        deduped_ids = [f["id"] for f in deduped]
        self.assertIn("bug-1", deduped_ids)
        self.assertNotIn("test-1", deduped_ids)
        self.assertEqual(len(dropped), 1)


# ---------------------------------------------------------------------------
# max_findings cap
# ---------------------------------------------------------------------------

class TestMaxFindingsCap(unittest.TestCase):

    def test_cap_applied_after_ranking(self):
        """Cap keeps the top-ranked findings."""
        findings = [
            _make_finding(id="low", severity="low", confidence=90),
            _make_finding(id="critical", severity="critical", confidence=70),
            _make_finding(id="high", severity="high", confidence=80),
        ]
        ranked = rank_findings(findings)
        # Simulate cap of 2
        capped = ranked[:2]
        ids = [f["id"] for f in capped]
        self.assertIn("critical", ids)
        self.assertIn("high", ids)
        self.assertNotIn("low", ids)

    def test_no_cap_keeps_all(self):
        findings = [_make_finding(id=f"f{i}") for i in range(10)]
        ranked = rank_findings(findings)
        self.assertEqual(len(ranked), 10)

    def test_max_findings_zero_means_no_limit(self):
        """--max-findings 0 is treated as no limit (same as omitting)."""
        import io
        from unittest.mock import patch
        from scripts.apply_challenges import main

        findings = [_make_finding(id=f"f{i}") for i in range(10)]
        challenges = [{"id": f"f{i}", "score": 90} for i in range(10)]

        filtered_json = json.dumps({"filtered": findings})
        challenges_json = json.dumps(challenges)

        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as ff:
            ff.write(filtered_json)
            ff.flush()
            with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as cf:
                cf.write(challenges_json)
                cf.flush()

                buf = io.StringIO()
                with patch("sys.stdout", buf), \
                     patch("sys.argv", ["apply_challenges.py", ff.name, cf.name,
                                        "--max-findings", "0"]):
                    main()

        result = json.loads(buf.getvalue())
        self.assertEqual(len(result["findings"]), 10)
        self.assertEqual(result["stats"]["cap_dropped"], 0)


# ---------------------------------------------------------------------------
# Cap annotation propagation (R02.2)
# ---------------------------------------------------------------------------

class TestCapAnnotation(unittest.TestCase):
    """Verify cap-dropped findings have elimination_reason in eliminated list."""

    def _run_main_with_cap(self, findings, challenges, max_findings):
        """Run main() with --max-findings and return parsed output."""
        import io
        from unittest.mock import patch
        from scripts.apply_challenges import main

        f_path = _write_json({"filtered": findings, "eliminated": []})
        c_path = _write_json(challenges)
        out_path = tempfile.mktemp(suffix=".json")
        try:
            argv = [
                "apply_challenges.py", f_path, c_path,
                "--output", out_path,
                "--max-findings", str(max_findings),
            ]
            with patch("sys.argv", argv):
                main()
            with open(out_path) as fh:
                return json.load(fh)
        finally:
            os.unlink(f_path)
            os.unlink(c_path)
            if os.path.exists(out_path):
                os.unlink(out_path)

    def test_cap_dropped_findings_have_elimination_reason(self):
        """Cap-dropped findings appear in eliminated with elimination_reason set."""
        findings = [
            _make_finding(id="critical", severity="critical"),
            _make_finding(id="high", severity="high"),
            _make_finding(id="medium", severity="medium"),
        ]
        challenges = [
            _make_challenge("critical", 90),
            _make_challenge("high", 90),
            _make_challenge("medium", 90),
        ]
        result = self._run_main_with_cap(findings, challenges, max_findings=2)
        elim_ids = {f["id"]: f for f in result["eliminated"]}
        self.assertIn("medium", elim_ids)
        medium_elim = elim_ids["medium"]
        self.assertEqual(medium_elim["eliminated_by"], "cap:max_findings")
        self.assertIn("elimination_reason", medium_elim)
        self.assertIn("2", medium_elim["elimination_reason"])

    def test_cap_dropped_elimination_reason_mentions_cap_size(self):
        """elimination_reason contains the cap value."""
        findings = [
            _make_finding(id=f"f{i}", severity="high") for i in range(5)
        ]
        challenges = [_make_challenge(f"f{i}", 90) for i in range(5)]
        result = self._run_main_with_cap(findings, challenges, max_findings=3)
        cap_elims = [f for f in result["eliminated"]
                     if f.get("eliminated_by") == "cap:max_findings"]
        self.assertEqual(len(cap_elims), 2)
        for elim in cap_elims:
            self.assertIn("elimination_reason", elim)
            self.assertIn("3", elim["elimination_reason"])


# ---------------------------------------------------------------------------
# main() CLI integration
# ---------------------------------------------------------------------------

class TestMainCLI(unittest.TestCase):

    def _run_main(self, findings, challenges, extra_args=None):
        """Helper: write temp files, invoke main(), return parsed output."""
        import io
        from unittest.mock import patch
        from scripts.apply_challenges import main

        f_path = _write_json({"filtered": findings, "eliminated": []})
        c_path = _write_json(challenges)
        out_path = tempfile.mktemp(suffix=".json")

        try:
            argv = ["apply_challenges.py", f_path, c_path, "--output", out_path]
            if extra_args:
                argv.extend(extra_args)
            with patch("sys.argv", argv):
                main()
            with open(out_path) as fh:
                return json.load(fh)
        finally:
            os.unlink(f_path)
            os.unlink(c_path)
            if os.path.exists(out_path):
                os.unlink(out_path)

    def test_basic_output_structure(self):
        """Output uses 'findings' key (R02.3) to match post_review.py input schema."""
        findings = [_make_finding(id="f1")]
        challenges = [_make_challenge("f1", 80)]
        result = self._run_main(findings, challenges)
        self.assertIn("findings", result)
        self.assertNotIn("filtered", result)
        self.assertIn("eliminated", result)
        self.assertIn("stats", result)
        self.assertIn("generated_at", result)

    def test_survived_finding_in_findings(self):
        findings = [_make_finding(id="f1")]
        challenges = [_make_challenge("f1", 90)]
        result = self._run_main(findings, challenges)
        self.assertEqual(len(result["findings"]), 1)
        self.assertEqual(result["findings"][0]["id"], "f1")

    def test_removed_finding_in_eliminated(self):
        findings = [_make_finding(id="f1")]
        challenges = [_make_challenge("f1", 10)]
        result = self._run_main(findings, challenges)
        self.assertEqual(len(result["findings"]), 0)
        elim_ids = [f["id"] for f in result["eliminated"]]
        self.assertIn("f1", elim_ids)

    def test_stats_fields_present(self):
        findings = [_make_finding(id="f1")]
        challenges = [_make_challenge("f1", 80)]
        result = self._run_main(findings, challenges)
        stats = result["stats"]
        for field in [
            "total_input", "challenge_removed", "challenge_downgraded",
            "challenge_contested", "challenge_survived", "unchallenged",
            "dedup_dropped", "cap_dropped", "final_count",
        ]:
            self.assertIn(field, stats, f"Missing stats field: {field}")

    def test_prior_eliminated_passed_through(self):
        """Prior Phase 6 eliminated findings appear in output eliminated list."""
        import io
        from unittest.mock import patch
        from scripts.apply_challenges import main

        prior_elim = [dict(_make_finding(id="e1"), eliminated_by="threshold")]
        f_data = {
            "filtered": [_make_finding(id="f1")],
            "eliminated": prior_elim,
        }
        f_path = _write_json(f_data)
        c_path = _write_json([_make_challenge("f1", 80)])
        out_path = tempfile.mktemp(suffix=".json")
        try:
            with patch("sys.argv", ["apply_challenges.py", f_path, c_path, "--output", out_path]):
                main()
            with open(out_path) as fh:
                result = json.load(fh)
            elim_ids = [f["id"] for f in result["eliminated"]]
            self.assertIn("e1", elim_ids)
        finally:
            os.unlink(f_path)
            os.unlink(c_path)
            if os.path.exists(out_path):
                os.unlink(out_path)

    def test_max_findings_cap(self):
        findings = [
            _make_finding(id="critical", severity="critical"),
            _make_finding(id="high", severity="high"),
            _make_finding(id="medium", severity="medium"),
        ]
        challenges = [
            _make_challenge("critical", 90),
            _make_challenge("high", 90),
            _make_challenge("medium", 90),
        ]
        result = self._run_main(findings, challenges, extra_args=["--max-findings", "2"])
        self.assertEqual(len(result["findings"]), 2)
        findings_ids = [f["id"] for f in result["findings"]]
        self.assertIn("critical", findings_ids)
        self.assertIn("high", findings_ids)
        self.assertNotIn("medium", findings_ids)
        self.assertEqual(result["stats"]["cap_dropped"], 1)

    def test_dedup_through_main_cli(self):
        """CLI integration: cross-agent dedup runs within main() pipeline."""
        findings = [
            _make_finding(id="bug-1", agent="bug-detector", dimension="bug",
                          file="src/auth.py", line_start=41, confidence=90),
            _make_finding(id="conv-1", agent="conventions-and-intent", dimension="convention",
                          file="src/auth.py", line_start=42, confidence=80),
        ]
        challenges = [
            _make_challenge("bug-1", 85),
            _make_challenge("conv-1", 85),
        ]
        result = self._run_main(findings, challenges)
        # bug-1 wins (core dimension), conv-1 deduped
        result_ids = [f["id"] for f in result["findings"]]
        self.assertIn("bug-1", result_ids)
        self.assertNotIn("conv-1", result_ids)
        self.assertGreater(result["stats"]["dedup_dropped"], 0)

    def test_stdout_output(self):
        """When --output is omitted, JSON is written to stdout."""
        import io
        from unittest.mock import patch
        from scripts.apply_challenges import main

        findings = [_make_finding(id="f1")]
        challenges = [_make_challenge("f1", 80)]
        f_path = _write_json({"filtered": findings, "eliminated": []})
        c_path = _write_json(challenges)
        try:
            captured = io.StringIO()
            with patch("sys.argv", ["apply_challenges.py", f_path, c_path]):
                with patch("sys.stdout", captured):
                    main()
            output = json.loads(captured.getvalue())
            self.assertIn("findings", output)
            self.assertNotIn("filtered", output)
        finally:
            os.unlink(f_path)
            os.unlink(c_path)


if __name__ == "__main__":
    unittest.main()

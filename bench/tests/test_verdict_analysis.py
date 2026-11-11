"""Route analysis counts distinct bugs and places independently across volume policies."""

import copy
import json
import subprocess

import pytest

from bench.runner.verdict_analysis import Sample, analyze, load_replays


def sample(pass_id, pr="pr", changed_lines=0):
    rows = [
        dict(
            id=str(i),
            file="source",
            line_start=line,
            severity="low",
            rank_index=None if i == 3 else i,
            matched_bug_ids=["bug"] if i < 2 else [],
            route=route,
            verdict=dict(
                claim="confirmed",
                consequence="latent" if route == "posted-latent" else "present_failure",
            ),
        )
        for i, (line, route) in enumerate(
            [
                (10, "posted"),
                (12, "posted-latent"),
                (50, "posted"),
                (80, "posted"),
                (110, "not-established"),
            ]
        )
    ]
    return Sample(
        "treatment", pass_id, pr, tuple(rows), frozenset(["bug"]), changed_lines
    )


def test_analysis_cli_reports_both_policies_pass_averages_confusions_places_and_flips(
    tmp_path,
):
    root = tmp_path / "replays"
    root.mkdir()
    (root / "replay.json").write_text(
        json.dumps(dict(dry_run=False, replays=2)), encoding="utf-8"
    )
    reader = tmp_path / "reader.json"
    reader.write_text(
        json.dumps(
            [
                dict(pr="pr", id="0", claim="holds", worth="yes"),
                dict(pr="pr", id="1", claim="refuted", worth="no"),
            ]
        ),
        encoding="utf-8",
    )
    for pass_id in ["replay-001", "replay-002"]:
        s = sample(pass_id)
        rows = copy.deepcopy(list(s.findings))
        if pass_id.endswith("002"):
            rows[2]["route"] = "report-only"
        dest = root / pass_id / "pr"
        dest.mkdir(parents=True)
        (dest / "source.json").write_text(
            json.dumps(dict(bugs={"bug": ["0", "1"]})), encoding="utf-8"
        )
        (dest / "outcomes.json").write_text(
            json.dumps(dict(findings=rows, changed_lines=0)), encoding="utf-8"
        )
    out = tmp_path / "analysis.json"
    subprocess.run(
        [
            "python3",
            "bench/analyze_verdicts.py",
            "--replay-dir",
            str(root),
            "--second-reader",
            str(reader),
            "--expected-known-bugs",
            "1",
            "--out",
            str(out),
        ],
        check=True,
        capture_output=True,
    )
    result = json.loads(out.read_text(encoding="utf-8"))
    unlimited = result["policies"]["all"]["treatment"]
    assert unlimited["passes"][0]["total"]["routes"]["posted"] == dict(
        known_bugs=1,
        matching_places=1,
        unmatched_places=2,
        findings=3,
        reader_matches=1,
        reader_claim_agreements=1,
        reader_worth_yes=1,
        reader_worth_no=0,
    )
    assert unlimited["passes"][0]["total"]["routes"]["posted-latent"]["known_bugs"] == 1
    assert unlimited["average"]["routes"]["posted"]["unmatched_places"] == 1.5
    assert unlimited["findings_changing_route"] == 1
    assert unlimited["passes"][1]["changed_since_previous"] == 1
    assert unlimited["average"]["claim_confusion"] == {
        "confirmed": {"holds": 1, "refuted": 1}
    }
    assert unlimited["average"]["worth_confusion"] == {"yes": {"yes": 1, "no": 1}}
    bounded = result["policies"]["bounded"]["treatment"]
    assert bounded["passes"][0]["per_pr"][0]["cap"] == 2
    assert bounded["average"]["routes"]["report-only"]["findings"] == 2
    assert bounded["findings_changing_route"] == 0
    assert bounded["average_per_pr"]["pr"]["routes"] == bounded["average"]["routes"]


def test_size_budget_grows_and_missing_pass_population_is_rejected():
    small, large = sample("1"), sample("2", changed_lines=10000)
    result = analyze([small, large], [], expected_bugs=1)
    assert (
        result["policies"]["bounded"]["treatment"]["passes"][1]["total"]["routes"][
            "report-only"
        ]["findings"]
        == 0
    )
    different = sample("2", pr="other")
    with pytest.raises(ValueError, match="different findings"):
        analyze([small, different], [], expected_bugs=1)
    with pytest.raises(ValueError, match="expected 61"):
        analyze([small], [])


def test_dry_run_is_never_treated_as_measured_data(tmp_path):
    (tmp_path / "replay.json").write_text(
        json.dumps(dict(dry_run=True)), encoding="utf-8"
    )
    with pytest.raises(ValueError, match="no verdict observations"):
        load_replays(tmp_path)


def test_artifact_route_counts_released_bugs_other_places_and_reader_agreement():
    findings = tuple(
        dict(
            id=str(i),
            file="source",
            line_start=10 + i * 20,
            severity="low",
            rank_index=i,
            route="posted-artifact",
            matched_bug_ids=["bug"] if i == 0 else [],
            verdict=dict(claim="confirmed", consequence="artifact_defect"),
        )
        for i in range(2)
    )
    s = Sample("treatment", "1", "pr", findings, frozenset(["bug"]), 100)
    reader = [
        dict(pr="pr", id="0", claim="holds", worth="yes"),
        dict(pr="pr", id="1", claim="holds", worth="no"),
    ]
    result = analyze([s], reader, expected_bugs=1)
    row = result["policies"]["all"]["treatment"]["average"]["routes"]["posted-artifact"]
    assert row == dict(
        known_bugs=1,
        matching_places=1,
        unmatched_places=1,
        findings=2,
        reader_matches=2,
        reader_claim_agreements=2,
        reader_worth_yes=1,
        reader_worth_no=1,
    )


def test_nonconfirmed_claims_have_no_invented_worth_answer():
    rows = (
        dict(
            id="r",
            file="source",
            line_start=1,
            route="report-only",
            verdict=dict(claim="refuted", justification="Existing guard"),
        ),
        dict(
            id="u",
            file="source",
            line_start=20,
            route="not-established",
            verdict=dict(
                claim="not_established",
                missing_fact="Which caller?",
                justification="Unavailable",
            ),
        ),
    )
    observations = Sample("treatment", "pass1", "pr", rows, frozenset(), 1)
    reader = [
        dict(pr="pr", id="r", claim="refuted", worth="no"),
        dict(pr="pr", id="u", claim="unverified", worth="yes"),
    ]
    result = analyze([observations], reader, expected_bugs=0)
    table = result["policies"]["all"]["treatment"]["average"]["worth_confusion"]
    assert table == {"not_applicable": {"no": 1, "yes": 1}}

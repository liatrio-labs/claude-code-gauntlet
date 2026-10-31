"""Removed-test receipts and verification at their caller boundaries."""

import json

import pytest
from gauntlet.jsjson import fnv1a32, js_stringify_pretty
from gauntlet.removed_tests import removed_tests
from gauntlet.verify.decide import VerifyContext, run_verification
from gauntlet.verify.wire import build_deltas

from tests.support.phase2 import SHA, write_context_inputs
from tests.support.verify_parity import project_findings


def deletion(path, text="assert override\nassert INVALID_TABLE"):
    lines = text.split("\n")
    return (
        f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ /dev/null\n"
        f"@@ -1,{len(lines)} +0,0 @@\n" + "".join(f"-{line}\n" for line in lines)
    )


@pytest.mark.parametrize(
    "path",
    [
        "tests/overrides.py",
        "TEST/extensionless",
        "spec/query.rb",
        "specs/query",
        "__tests__/table.ts",
        "lib/test_query",
        "lib/query_test.go",
        "lib/query_spec.rs",
        "lib/query.test.js",
        "lib/query.spec.ts",
        "lib/QueryTest.java",
        "lib/QueryTests.cs",
        "lib/QuerySpec.scala",
        "lib/QueryTests.integration.cs",
    ],
)
def test_deleted_test_file_inventory(path):
    receipt = removed_tests(deletion(path))
    assert receipt["status"] == "available"
    assert receipt["omitted"] == 0
    assert len(receipt["items"]) == 1
    assert receipt["items"][0] == {
        "id": receipt["items"][0]["id"],
        "path": path,
        "old_start": 1,
        "old_end": 2,
        "kind": "deleted_file",
    }
    assert len(receipt["items"][0]["id"]) == 16
    assert receipt == removed_tests(deletion(path))


def test_surviving_and_renamed_test_hunks_use_old_path_and_budget():
    patch = (
        "diff --git a/tests/old b/src/renamed\n"
        "rename from tests/old\nrename to src/renamed\n"
        "--- a/tests/old\n+++ b/src/renamed\n"
        "@@ -2,3 +2,2 @@\n context\n--- header-like deleted body\n+replacement\n"
        "-assert INVALID_TABLE\n@@ -10 +9,0 @@\n-assert override\n"
    )
    receipt = removed_tests(patch)
    assert receipt["status"] == "available"
    assert [
        (i["path"], i["old_start"], i["old_end"], i["kind"]) for i in receipt["items"]
    ] == [
        ("tests/old", 3, 4, "hunk"),
        ("tests/old", 10, 10, "hunk"),
    ]


def test_unprefixed_patch_preserves_real_a_directory():
    receipt = removed_tests(
        "--- a/tests/removed\n+++ /dev/null\n@@ -1 +0,0 @@\n-assert override\n"
    )
    assert receipt["status"] == "available"
    assert receipt["items"][0]["path"] == "a/tests/removed"


def test_non_source_tests_are_excluded_before_inventory_cap():
    patch = "".join(deletion(f"vendor/tests/{i}") for i in range(70))
    patch += "".join(deletion(f"node_modules/lib/tests/{i}") for i in range(70))
    patch += "".join(
        deletion(f"{directory}/tests/check")
        for directory in ("third_party", "deps", "generated", "build", "dist", "target")
    )
    patch += deletion("src/production.py")
    patch += "".join(deletion(f"tests/{i}") for i in range(62))
    receipt = removed_tests(patch)
    assert receipt["status"] == "available"
    assert receipt["omitted"] == 2
    assert [i["path"] for i in receipt["items"]] == [f"tests/{i}" for i in range(60)]


@pytest.mark.parametrize(
    "body",
    [
        "@@ malformed @@\n-assert override\n",
        "@@ -1,2 +0,0 @@\n-assert override\n",
        "@@ -1 +0,0 @@\n-assert override\n-excess\n",
        "@@ -1 +0,0 @@\n+wrong side\n",
        "@@ -1 +0,0 @@\n-assert override\n--- excess header-like body\n",
        "@@ -1,2 +0,0 @@\n-assert override\n@@ -5 +0,0 @@\n-more\n",
    ],
)
def test_malformed_diff_is_unavailable_even_after_valid_deletions(body):
    receipt = removed_tests(
        deletion("tests/good") + "--- a/tests/bad\n+++ /dev/null\n" + body
    )
    assert receipt["status"] == "unavailable"
    assert receipt["items"] == []
    assert receipt["omitted"] == 0


def test_binary_test_deletion_has_no_text_inventory_item():
    receipt = removed_tests(
        "diff --git a/tests/binary b/tests/binary\ndeleted file mode 100644\n"
        "Binary files a/tests/binary and /dev/null differ\n"
    )
    assert receipt["status"] == "available"
    assert receipt["items"] == []


def test_shared_context_carries_signed_mixed_deletion_inventory(tmp_path, invoke):
    output = tmp_path / "out"
    patch = deletion("tests/overrides") + deletion(
        "tests/invalid_table", "assert INVALID_TABLE"
    )
    write_context_inputs(output, b"rules", patch.encode())
    result = invoke(
        "write_shared_context",
        ["--output-dir", str(output), "--head-sha", SHA],
        tmp_path,
        b"triage",
    )
    assert result.returncode == 0, result.stderr
    receipt = json.loads(result.stdout)["removedTests"]
    assert [i["path"] for i in receipt["items"]] == [
        "tests/overrides",
        "tests/invalid_table",
    ]
    checksum = receipt.pop("checksum")
    assert checksum == fnv1a32(js_stringify_pretty(receipt))


def test_removed_loss_anchor_round_trips_past_blank_line(tmp_path, verify_git):
    quote = "    return None\n\n    return surviving_value"
    (tmp_path / "source").write_text(quote + "\n", encoding="utf-8")
    finding = {
        "id": "loss",
        "file": "source",
        "line_start": 1,
        "has_removed_test_location": True,
    }
    result = run_verification(
        [finding], VerifyContext(str(tmp_path), "base", target_kind="local")
    )
    delta = build_deltas([finding], result["verified"])[0]
    assert delta["location_status"] == "confirmed"
    assert (delta["line_start"], delta["line_end"]) == (1, 3)
    assert delta["anchor"] == quote


def test_removed_losses_share_admission_and_source_anchors(
    tmp_path, monkeypatch, verify_git
):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "source").write_text(
        "return surviving_value\n", encoding="utf-8", newline=""
    )
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/surviving").write_text(
        "assert unrelated_value\n", encoding="utf-8", newline=""
    )
    patch = tmp_path / "patch"
    patch.write_text(deletion("tests/old"), encoding="utf-8")
    finding = {
        "id": "loss",
        "file": "source",
        "line_start": 1,
        "severity": "high",
        "confidence": 90,
        "description": "Deleted `test_override` and `INVALID_TABLE` assertions leave surviving behavior untested.",
        "searched_paths": ["tests/surviving"],
        "candidate_tests": [],
        "production_element": {
            "file": "source",
            "line_start": 1,
            "line_end": 1,
            "anchor": "return surviving_value",
        },
        "removed_test": {
            "id": "0123456789abcdef",
            "path": "tests/old",
            "old_start": 1,
            "old_end": 2,
        },
    }
    verify_git[1]["blame"] = (
        "1234567 (Author 2024-01-01 00:00:00 +0000 1) code",
        "",
        0,
    )
    verify_git[1]["grep"] = ("", "", 1)
    inventory = removed_tests(patch.read_text(encoding="utf-8"))
    assert inventory["status"] == "available"
    request = dict(finding)
    request["removed_test"] = {
        **finding["removed_test"],
        "id": inventory["items"][0]["id"],
    }
    finding = project_findings([request])[0]
    result = run_verification(
        [finding],
        VerifyContext(str(tmp_path), "base", target_kind="branch"),
        str(patch),
    )
    assert result["verified"] == [finding]
    assert finding["origin"] == "new"
    assert finding["severity"] == "high"
    assert finding["confidence"] == 90
    assert finding["factual_verification"]["verified"] is True
    assert finding["diff_validation"]["in_diff"] is False
    assert finding["anchor"] == "return surviving_value"
    assert (
        build_deltas([finding], result["verified"])[0]["anchor"]
        == "return surviving_value"
    )
    assert finding["location_status"] == "confirmed"
    assert (finding["blame_class"], finding["blame_source"]) == ("new", "removed_loss")
    assert not any(argv[1] in ("log", "blame", "grep") for argv, _ in verify_git[0])
    from tests.support.pipeline import run_node

    claim = {
        **request,
        **finding,
        "dimension": "test_coverage",
        "test_kind": "critical_gap",
        "criticality": 8,
        "failure_scenario": "Entire behavior loses effective coverage",
        "validation": "validated",
        "policy_subject": "coverage",
        "challenge": "survived",
        "admission_assessments": [
            {"predicate": predicate, "status": "confirmed", "references": refs}
            for predicate, refs in (
                ("resolved_production_element", ["production_element"]),
                ("complete_searches", ["searched_paths:0"]),
                (
                    "complete_behavioral_absence",
                    ["production_element", "searched_paths:0"],
                ),
            )
        ],
    }
    claim["admission_assessments"].append(
        {
            "predicate": "present_defect",
            "status": "rejected",
            "references": ["primary"],
        }
    )
    values = json.loads(
        run_node(
            "import {evaluateAdmission} from './workflows/src/admission.js';"
            "import {skepticalAssessment} from './workflows/test/helpers/verdicts.js';"
            "import {disposeFindings} from './workflows/src/dispose.js';"
            "let data='';for await(const chunk of process.stdin)data+=chunk;"
            "const claim=JSON.parse(data);claim.skeptical_assessments=[skepticalAssessment(claim.id),skepticalAssessment(claim.id,'challenger')];const indirect=structuredClone(claim);"
            "indirect.admission_assessments[2].status='rejected';"
            "const bad=structuredClone(claim);bad.resolved_references.production_element.location_status='unconfirmed';"
            "const rows=[claim,{...claim,criticality:7},indirect,bad].map(evaluateAdmission);"
            "process.stdout.write(JSON.stringify(rows.map(f=>{const out=disposeFindings({findings:[f]});return [...out.findings,...out.unverified][0]})));",
            claim,
        ).stdout
    )
    assert [(row["admission"]["status"], row["disposition"]) for row in values] == [
        ("admitted", "body"),
        ("report_only", "report_only"),
        ("report_only", "report_only"),
        ("held_back", "held_back"),
    ], [row["admission"] for row in values]
    malformed = {
        "id": "loss-malformed",
        "file": "source",
        "line_start": 1,
        "severity": "high",
        "confidence": 90,
        "description": "Deleted `test_override` and `INVALID_TABLE` assertions leave surviving behavior untested.",
        "removed_test": {
            "id": "",
            "path": "tests/old",
            "old_start": 1,
            "old_end": 2,
        },
    }
    malformed = project_findings([malformed])[0]
    malformed_result = run_verification(
        [malformed],
        VerifyContext(str(tmp_path), "base", target_kind="branch"),
        str(patch),
    )
    assert malformed_result["verified"] == [malformed]
    assert malformed["origin"] == "new"
    assert malformed["severity"] == "high"
    assert malformed["confidence"] == 30
    assert malformed["factual_verification"]["reason"].startswith(
        "referenced symbol(s) not found in codebase:"
    )
    assert malformed["diff_validation"]["in_diff"] is None
    for line in (None, 0, 2, True):
        finding = {**finding, "line_start": line}
        finding.pop("anchor", None)
        assert run_verification(
            [finding],
            VerifyContext(str(tmp_path), "base", target_kind="branch"),
            str(patch),
        )["verified"] == [finding]
        assert finding["location_status"] == "unconfirmed"
        assert finding["diff_validation"]["in_diff"] is None
        assert "anchor" not in finding
    finding.update(file="absent", line_start=1)
    assert run_verification(
        [finding],
        VerifyContext(str(tmp_path), "base", target_kind="branch"),
        str(patch),
    )["verified"] == [finding]
    assert finding["location_status"] == "unconfirmed"
    assert "anchor" not in finding
    for source in (b"\0invalid source", b"short\n", b" \n", b"x" * 401):
        (tmp_path / "source").write_bytes(source)
        finding.update(file="source", line_start=1)
        run_verification(
            [finding],
            VerifyContext(str(tmp_path), "base", target_kind="branch"),
            str(patch),
        )
        assert finding["location_status"] == "unconfirmed"
        assert "anchor" not in finding
        assert finding["diff_validation"]["in_diff"] is None

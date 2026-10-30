"""Removed-test receipts and verification at their caller boundaries."""

import json

import pytest
from gauntlet.jsjson import fnv1a32, js_stringify_pretty
from gauntlet.removed_tests import removed_tests
from gauntlet.verify.decide import VerifyContext, run_verification

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


def test_removed_test_verify_preserves_new_loss_on_unchanged_production(
    tmp_path, monkeypatch, verify_git
):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "source").write_text(
        "return surviving_value\n", encoding="utf-8", newline=""
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
    finding = project_findings([finding])[0]
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
    assert finding["location_status"] == "confirmed"
    assert (finding["blame_class"], finding["blame_source"]) == ("new", "removed_loss")
    assert not any(argv[1] in ("log", "blame", "grep") for argv, _ in verify_git[0])
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

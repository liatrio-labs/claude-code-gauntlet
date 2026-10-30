"""Citation execution at the command and durable resume boundaries."""

import json

import pytest
from gauntlet.location import SourceAdapter

from tests.support.citations import QUOTE, citation_cli


def test_citation_quote_checks_are_explicit_and_bounded(tmp_path, monkeypatch, invoke):
    call, q, capture, root, output = citation_cli(tmp_path, monkeypatch, invoke)
    cases = [
        ({}, "matched"),
        ({"line_start": 3, "line_end": 3}, "matched"),
        ({"file": "vendor/generated"}, "matched"),
        ({"quote": "return a_different_value;"}, "unmatched"),
        ({"line_start": 2, "line_end": 2}, "unmatched"),
        ({"side": "old"}, "matched"),
        ({"file": "vendor/generated", "side": "old"}, "unavailable"),
        (
            {"file": "gone", "side": "old", "quote": "assert removed_behavior;"},
            "matched",
        ),
        ({"quote": "x" * 401}, "rejected"),
        ({"quote": "😀" * 201}, "rejected"),
        ({"quote": "tiny"}, "rejected"),
        ({"quote": "\n".join(["source line"] * 9), "line_end": 9}, "rejected"),
        ({"line_end": 2}, "rejected"),
        ({"line_start": True}, "rejected"),
        ({"line_start": 2**53}, "rejected"),
        ({"side": "worktree"}, "rejected"),
        ({"snapshot_id": "other"}, "rejected"),
        ({"file": "../source"}, "rejected"),
        ({"file": "./source"}, "rejected"),
        ({"file": "code-gauntlet-artifact"}, "rejected"),
        ({"file": "pr-description.json"}, "rejected"),
        ({"file": "missing"}, "unavailable"),
        ({"finding_id": "foreign"}, "rejected"),
        ({"request_id": {}}, "rejected"),
    ]
    for index, (changes, expected) in enumerate(cases):
        env, _, _ = call([{**q, **changes}], batch=f"{index:08x}")
        row = env["result"]["citations"][0]
        assert row["status"] == expected, (changes, row)
        assert row["request"] == {**q, **changes}
        assert row["source"]["snapshot_id"] == capture["manifest_sha256"]
        if expected == "matched":
            assert row["added_lines"] == (
                [3]
                if row["request"]["file"] == "source"
                and row["request"]["side"] == "head"
                and row["request"]["line_start"] <= 3 <= row["request"]["line_end"]
                else []
            )
    malformed, _, _ = call([None, q], batch="fffffff0")
    assert [r["status"] for r in malformed["result"]["citations"]] == [
        "rejected",
        "matched",
    ]
    # Source identity survives both worktree and branch changes.
    (root / "source").write_bytes(b"a changed working tree\n")
    env, _, _ = call([q], batch="abcdef00")
    assert env["result"]["citations"][0]["status"] == "matched"
    directory = output / "code-gauntlet-inputs-run/source-capture"
    (directory / "manifest.json").write_bytes(b"{}")
    env, _, _ = call([q], batch="abcdef01")
    assert env["result"]["citations"][0]["status"] == "unavailable"


def test_cite_cli_reserves_attempts_and_reuses_terminal_siblings(
    tmp_path, monkeypatch, invoke
):
    call, q, capture, root, output = citation_cli(tmp_path, monkeypatch, invoke)
    original = SourceAdapter.read_captured_source
    reads = []
    ledger_batch = "12345678"

    def read(self, file, **kwargs):
        ledger = json.loads(
            (
                output / f"code-gauntlet-inputs-run/cite-{ledger_batch}/ledger.json"
            ).read_bytes()
        )
        assert ledger["attempts"][-1]["results"] is None
        reads.append(file)
        return original(self, file, **kwargs)

    monkeypatch.setattr(SourceAdapter, "read_captured_source", read)
    requests = [
        q,
        {**q, "request_id": "rule", "role": "rule"},
        {**q, "file": "missing", "request_id": "missing"},
    ]
    first, _, _ = call(requests)
    assert reads == ["source", "missing"]
    assert [r["status"] for r in first["result"]["citations"]] == [
        "matched",
        "matched",
        "unavailable",
    ]
    assert call(requests)[0] == first
    assert reads == ["source", "missing"]
    retry, _, _ = call(requests, attempt=2)
    assert retry["result"] == first["result"]
    assert reads == ["source", "missing", "missing"]
    altered = [{**q, "quote": QUOTE + " "}, *requests[1:]]
    assert (
        call(altered)[0]["result"]["citations"][0]["detail"]
        == "corrupt_attempt_identity"
    )
    ledger_batch = "abcdef99"
    assert (
        call([q], batch="abcdef99")[0]["result"]["citations"][0]["status"] == "matched"
    )
    assert reads == ["source", "missing", "missing", "source"]
    assert (
        call(requests, attempt=3)[0]["result"]["citations"][0]["detail"]
        == "missing_attempt_identity"
    )
    ledger_path = output / "code-gauntlet-inputs-run/cite-12345678/ledger.json"
    ledger = json.loads(ledger_path.read_bytes())
    assert len(ledger["attempts"]) == 2
    assert ledger["attempts"][0]["nonce"] == "run.12345678.1"
    ledger["attempts"][1]["results"] = None
    ledger_path.write_text(json.dumps(ledger), encoding="utf-8", newline="")
    assert (
        call(requests, attempt=2)[0]["result"]["citations"][0]["detail"]
        == "uncertain_attempt_identity"
    )
    assert (
        call([{**q, "quote": QUOTE + " "}])[0]["result"]["citations"][0]["detail"]
        == "corrupt_attempt_identity"
    )
    ledger_path.unlink()
    assert call(requests)[0]["result"]["citations"][0]["status"] == "unavailable"


@pytest.mark.parametrize("interruption", ["dispatch", "lock", "reserved"])
def test_cite_retry_runs_after_interrupted_first_dispatch(
    tmp_path, monkeypatch, invoke, interruption
):
    call, q, _, _, output = citation_cli(tmp_path, monkeypatch, invoke)
    directory = output / "code-gauntlet-inputs-run/cite-12345678"
    if interruption != "dispatch":
        call([q])
        ledger_path = directory / "ledger.json"
        ledger = json.loads(ledger_path.read_bytes())
        ledger["attempts"] = (
            [] if interruption == "lock" else [{"nonce": "lost", "results": None}]
        )
        ledger_path.write_text(json.dumps(ledger), encoding="utf-8", newline="")
        (directory / "lock").touch()
    retried = call([q], attempt=2, real=True)[0]
    assert retried["result"]["citations"][0]["status"] == "matched"
    assert call([q], nonce="run.12345678.2")[0]["result"] == retried["result"]
    if interruption == "dispatch":
        assert call([q], nonce="another")[0]["result"] == retried["result"]
    exhausted = call([q], nonce="beyond-cap")[0]
    assert exhausted["result"]["citations"][0]["detail"] == "exhausted_attempts"
    assert len(json.loads((directory / "ledger.json").read_bytes())["attempts"]) == 2


def test_cite_rejects_captured_output_directory_as_terminal(
    tmp_path, monkeypatch, invoke
):
    call, q, _, _, output = citation_cli(
        tmp_path, monkeypatch, invoke, in_repo_output=True
    )
    (output / "evidence").write_text(QUOTE, encoding="utf-8", newline="")
    requests = [{**q, "file": "output/evidence"}]
    first = call(requests)[0]
    assert first["result"]["citations"][0]["status"] == "rejected"
    assert first["result"]["citations"][0]["detail"] == "rejected_origin"
    assert call(requests, attempt=2)[0]["result"] == first["result"]

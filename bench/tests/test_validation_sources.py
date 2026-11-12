"""Frozen source joins are checked against a complete local packet and recorded Verify."""

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from bench.runner.validation_replay import load_sources

ROOT = Path(__file__).resolve().parents[2]
URL = "https://github.com/synthetic/fixture/pull/1"


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


@pytest.fixture
def source_packet(tmp_path):
    data = json.loads(
        subprocess.run(
            ["node", str(ROOT / "bench/tests/fixtures/validation_source.mjs")],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=True,
        ).stdout
    )
    run = tmp_path / "run"
    packet = run / "scores/epoch/score"
    write(packet / "complete.json", {})
    write(
        packet / "sources/run.json",
        {
            "pr_identities": {
                URL: {"head_sha": data["record"]["args"]["headShaShort"] + "a" * 33}
            }
        },
    )
    artifacts = {
        "pr/workflows/wf_source.json": data["record"],
        "pr/code-gauntlet-checkpoint-all-abc1234.json": data["checkpoint"],
        "pr/phase4-input-abc1234.slice0.json": {"findings": data["inputs"]},
        **{
            f"pr/phase4-output-abc1234.{name}.json": value
            for name, value in data["outputs"].items()
        },
    }
    hashes = {}
    for relative, value in artifacts.items():
        path = packet / "sources" / relative
        write(path, value)
        hashes[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    trace_path = packet / "sources/pr/trace.json"
    write(trace_path, data["trace"])
    write(
        packet / "attempts/attempt.json",
        {"pr_url": URL, "source_artifact_hashes": hashes},
    )
    write(
        packet / "stage_trace_bindings.json",
        {
            URL: {
                "attempt_id": "attempt",
                "trace_path": "sources/pr/trace.json",
                "trace_sha256": hashlib.sha256(trace_path.read_bytes()).hexdigest(),
            }
        },
    )
    verify = next(s for s in data["trace"]["snapshots"] if s["stage"] == "verify")
    rows = [
        dict(
            pr_url=URL,
            attempt_id="attempt",
            score_ref={"score_id": "score"},
            stage="verify",
            bug_id=bug,
            coverage="complete",
            matched_version_ids=ids,
        )
        for bug, ids in [("bug", [verify["finding_version_ids"][0]]), ("unmatched", [])]
    ]
    (packet / "stages.jsonl").write_text(
        "\n".join(map(json.dumps, rows)), encoding="utf-8"
    )
    return run, packet, data


@pytest.mark.parametrize("live", [False, True])
def test_source_packet_reconstructs_verify_and_joins_bugs_by_finding_id(
    source_packet, live
):
    import shutil

    run, packet, data = source_packet
    if live:
        shutil.copytree(packet / "sources/pr", run / "attempts/pr/attempt/pr")
    (source,) = load_sources(run, "score", [URL])
    assert source.url == URL
    finding = source.payload["findings"][0]
    assert source.bugs == (("bug", (finding["id"],)), ("unmatched", ()))
    assert source.payload["verificationDispatches"] == ["slice0"]
    assert source.payload["recordedFilter"][0]["id"] == finding["id"]
    assert source.payload["recordedAudit"]
    assert len(source.artifacts) == 4


@pytest.mark.parametrize(
    "failure,match",
    [
        ("artifact_hash", "source artifact hash mismatch"),
        ("missing_artifact", "missing source artifact"),
        ("trace_hash", "score trace hash mismatch"),
        ("attempt", "source attempt identity mismatch"),
        ("score", "incomplete or mismatched source attribution"),
        ("matched_id", "incomplete or mismatched source attribution"),
        ("head", "source head identity mismatch"),
        ("missing_verify", "missing verify-stage known bug attribution"),
    ],
)
def test_source_packet_rejects_broken_hashes_and_cross_packet_joins(
    source_packet, failure, match
):
    run, packet, data = source_packet
    if failure in ("artifact_hash", "missing_artifact"):
        path = packet / "sources/pr/workflows/wf_source.json"
        if failure == "missing_artifact":
            path.unlink()
        else:
            path.write_text("{}", encoding="utf-8")
    elif failure == "trace_hash":
        (packet / "sources/pr/trace.json").write_text("{}", encoding="utf-8")
    elif failure == "attempt":
        path = packet / "attempts/attempt.json"
        value = json.loads(path.read_text(encoding="utf-8"))
        value["pr_url"] = "other"
        write(path, value)
    elif failure == "head":
        write(
            packet / "sources/run.json",
            {"pr_identities": {URL: {"head_sha": "b" * 40}}},
        )
    else:
        path = packet / "stages.jsonl"
        rows = [
            json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
        ]
        if failure == "score":
            rows[0]["score_ref"]["score_id"] = "other"
        elif failure == "matched_id":
            rows[0]["matched_version_ids"] = ["other"]
        else:
            rows[1]["stage"] = "discover"
        path.write_text("\n".join(map(json.dumps, rows)), encoding="utf-8")
    with pytest.raises(ValueError, match=match):
        load_sources(run, "score")

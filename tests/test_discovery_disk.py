"""The preparation/planner boundary never returns the growing region plan."""

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest
from gauntlet.discovery_disk import CHUNK_CHARS, RECEIPT_CHARS, artifact_name, save
from gauntlet.jsjson import fnv1a32

from tests.support.phase2 import SHA, discovery_plan_payload, write_context_inputs
from tests.support.pipeline import run_node


def encoded(request):
    return run_node(
        "import {compressMeasurement} from './workflows/src/measurementCompression.js'; let s='';for await(const c of process.stdin)s+=c;console.log(compressMeasurement(s));",
        request,
    ).stdout.strip()


@pytest.fixture
def planner(tmp_path):
    def prepare(plan, *, context_lines=100):
        context = "row\n" * context_lines
        (tmp_path / f"code-gauntlet-context-{SHA}.md").write_text(
            context, encoding="utf-8"
        )
        digest = save(
            tmp_path / artifact_name("discovery-plan", SHA),
            {
                "version": 1,
                "sha": SHA,
                "context_lines": context_lines,
                "context_sha256": hashlib.sha256(context.encode()).hexdigest(),
                "diff_sha256": "0" * 64,
                "plan": plan,
            },
        )
        return {"path": artifact_name("discovery-plan", SHA), "sha256": digest}

    def call(ref, request, *, phase="select", nonce="test", mutate=None):
        token = encoded(request)
        chunks = [token[i : i + CHUNK_CHARS] for i in range(0, len(token), CHUNK_CHARS)]
        envelopes = []
        for part, chunk in enumerate(chunks):
            args = [
                "--output-dir",
                str(tmp_path),
                "--sha",
                SHA,
                "--nonce",
                nonce,
                "--phase",
                phase,
                "--plan-checksum",
                ref["sha256"],
                "--input-checksum",
                fnv1a32(token),
                "--part",
                str(part),
                "--parts",
                str(len(chunks)),
                "--chunk",
                chunk,
            ]
            if mutate:
                mutate(args, part)
            result = subprocess.run(
                [
                    sys.executable,
                    str(
                        Path(__file__).resolve().parents[1]
                        / "scripts/plan_discovery.py"
                    ),
                    *args,
                ],
                cwd=tmp_path,
                capture_output=True,
                check=False,
            )
            assert result.returncode == 0, result.stdout
            assert len(result.stdout) <= RECEIPT_CHARS
            env = json.loads(result.stdout)
            assert env["chunk_checksum"] == fnv1a32(chunk)
            assert env["input_checksum"] == fnv1a32(token)
            assert env["token_checksum"] == fnv1a32(env["token"])
            envelopes.append(env)
        return json.loads(envelopes[-1]["token"]), envelopes

    return prepare, call


def plan_with(n=2):
    return {
        "version": 2,
        "status": "available",
        "fact_packets": [],
        "regions": [
            {
                "id": f"r{i}",
                "file": f"src/{i}",
                "line_start": 1,
                "line_end": 2,
                "reads": [{"offset": 3 + i % 50, "limit": 2, "bytes": 100}],
            }
            for i in range(n)
        ],
    }


def selection(tmp_path, **changes):
    return {
        "context": str(tmp_path / f"code-gauntlet-context-{SHA}.md"),
        "context_lines": 100,
        "refs": [],
        "risks": [],
        "enabled": True,
        "disabled_reason": "scope",
        **changes,
    }


@pytest.mark.parametrize("count", [2, 2000])
def test_preparation_receipt_contains_only_a_fixed_size_plan_reference(
    tmp_path, invoke, count
):
    out = tmp_path / "out"
    diff = b"diff --git a/source b/source\n--- a/source\n+++ b/source\n" + b"".join(
        f"@@ -{i * 3 + 1} +{i * 3 + 1} @@\n-old\n+new\n".encode() for i in range(count)
    )
    write_context_inputs(out, b"rules", diff)
    result = invoke(
        "write_shared_context",
        ["--output-dir", str(out), "--head-sha", SHA],
        tmp_path,
        b"triage",
    )
    assert result.returncode == 0
    receipt = json.loads(result.stdout)
    plan = discovery_plan_payload(receipt, out)
    assert len(plan["regions"]) == count
    assert len(json.dumps(receipt["discoveryPlanRef"])) == len(
        json.dumps(
            {"path": f"code-gauntlet-discovery-plan-{SHA}.json", "sha256": "x" * 64}
        )
    )
    assert (
        "regions" not in result.stdout.decode()
        and "fact_packets" not in result.stdout.decode()
    )


def test_selection_budget_includes_assignment_and_unique_framed_reads(
    tmp_path, planner
):
    prepare, call = planner
    plan = plan_with(20)
    for r in plan["regions"]:
        r["reads"] = [
            {"offset": 1, "limit": 1, "bytes": 100},
            {"offset": 3, "limit": 3, "bytes": 6000},
            {"offset": 99, "limit": 1, "bytes": 100},
        ]
    plan["fact_packets"] = [
        {
            "related_files": ["src/0"],
            "reads": [{"offset": 50, "limit": 2, "bytes": 25000}],
        },
        {
            "related_files": ["elsewhere"],
            "reads": [{"offset": 60, "limit": 2, "bytes": 25000}],
        },
    ]
    value, _ = call(prepare(plan), selection(tmp_path))
    assert value["available"]
    ledger = json.loads(
        (tmp_path / artifact_name("coverage-ledger", SHA, "test")).read_bytes()
    )
    text = (tmp_path / artifact_name("gap-assignment", SHA, "test")).read_text(
        encoding="utf-8"
    )
    data = json.loads(text.split("\n", 2)[2].rsplit("\n</untrusted", 1)[0])
    charged = (
        len(text.encode())
        + 12 * text.count("\n")
        + len(value["assignment_prompt"].encode())
        + sum(r["bytes"] for r in data["reads"])
    )
    assert ledger["assigned_bytes"] == charged <= 24000
    assert text.count("\n") + sum(r["limit"] for r in data["reads"]) <= 750
    assert len(data["regions"]) == 20
    assert ledger["omitted_fact_packets"] == 1
    assert len(data["reads"]) == 3


@pytest.mark.parametrize("read_lines", [738, 739])
def test_assignment_metadata_shares_the_hunters_750_line_budget(
    tmp_path, planner, read_lines
):
    prepare, call = planner
    plan = plan_with(1)
    plan["regions"][0]["reads"] = [
        {"offset": 1, "limit": read_lines, "bytes": 13 * read_lines}
    ]
    value, _ = call(
        prepare(plan, context_lines=800), selection(tmp_path, context_lines=800)
    )
    assert value["available"]
    assert value["assigned_count"] == (1 if read_lines == 738 else 0)
    if value["assigned_count"]:
        text = (tmp_path / artifact_name("gap-assignment", SHA, "test")).read_text(
            encoding="utf-8"
        )
        assert text.count("\n") + read_lines == 750


def test_final_ledger_records_outside_anchors_and_citation_coverage(tmp_path, planner):
    prepare, call = planner
    ref = prepare(plan_with())
    selected, _ = call(ref, selection(tmp_path, refs=[["primary", "src/0", 1, 1]]))
    final, _ = call(
        ref,
        selection(
            tmp_path,
            state_checksum=selected["state_checksum"],
            hunters=[
                {
                    "agent": "hunter",
                    "status": "complete",
                    "anchors": [["outside", "src/0", 1]],
                    "refs": [["hunter", "src/1", 2, 2]],
                }
            ],
        ),
        phase="finalize",
    )
    assert final["available"]
    ledger = json.loads(
        (tmp_path / artifact_name("coverage-ledger", SHA, "test")).read_bytes()
    )
    assert ledger["uncovered"] == ["r1"] and ledger["remaining_uncovered"] == []
    assert ledger["hunters"] == [
        {
            "agent": "hunter",
            "assigned": ["r1"],
            "outside_assignment": ["outside"],
            "status": "complete",
        }
    ]


@pytest.mark.parametrize("broken", ["plan", "context", "state"])
def test_mismatched_disk_artifacts_yield_bounded_unavailable_receipts(
    tmp_path, planner, broken
):
    prepare, call = planner
    ref = prepare(plan_with())
    selected, _ = call(ref, selection(tmp_path))
    kind = {"plan": "discovery-plan", "context": "context", "state": "discovery-state"}[
        broken
    ]
    path = (
        (tmp_path / f"code-gauntlet-context-{SHA}.md")
        if broken == "context"
        else tmp_path / artifact_name(kind, SHA, "" if broken == "plan" else "test")
    )
    if broken == "context":
        path.write_bytes(b"different row\n" * 100)
    else:
        document = json.loads(path.read_bytes())
        if broken == "plan":
            document["plan"]["regions"][0]["file"] = "different/source"
        else:
            document["assigned"] = []
        path.write_text(json.dumps(document), encoding="utf-8")
    value, _ = call(
        ref,
        selection(tmp_path, state_checksum=selected["state_checksum"], hunters=[]),
        phase="finalize",
    )
    assert value["available"] is False


def test_large_citation_inputs_use_bounded_checked_chunks(tmp_path, planner):
    prepare, call = planner
    ref = prepare(plan_with())
    refs = [
        ["reviewer", hashlib.sha256(str(i).encode()).hexdigest(), i + 1, i + 1]
        for i in range(500)
    ]
    value, envs = call(ref, selection(tmp_path, refs=refs))
    assert value["available"]
    assert len(envs) > 1
    assert all(len(json.dumps(e)) < RECEIPT_CHARS for e in envs)

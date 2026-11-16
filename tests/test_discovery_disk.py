"""The preparation/planner boundary never returns the growing region plan."""

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest
from gauntlet.discovery_disk import RECEIPT_CHARS, artifact_name, canonical_files, save
from gauntlet.jsjson import fnv1a32
from gauntlet.registry import DISCOVERY_AGENT_TYPES

from tests.support.phase2 import SHA, discovery_plan_payload, write_context_inputs
from tests.support.pipeline import run_node


def plain_requests(request, files, phase):
    return json.loads(
        run_node(
            "import {discoveryRequests} from './workflows/src/discoveryDisk.js';let s='';for await(const c of process.stdin)s+=c;const v=JSON.parse(s);console.log(JSON.stringify(discoveryRequests({changedFiles:v.files},v.phase,v.request)));",
            {"request": request, "files": files, "phase": phase},
        ).stdout
    )


@pytest.fixture
def planner(tmp_path):
    def prepare(plan, *, context_lines=100):
        context = "row\n" * context_lines
        (tmp_path / f"code-gauntlet-context-{SHA}.md").write_text(
            context, encoding="utf-8", newline=""
        )
        digest = save(
            tmp_path / artifact_name("discovery-plan", SHA),
            {
                "version": 2,
                "files": canonical_files([r["file"] for r in plan["regions"]]),
                "sha": SHA,
                "context_lines": context_lines,
                "context_sha256": hashlib.sha256(context.encode()).hexdigest(),
                "diff_sha256": "0" * 64,
                "plan": plan,
            },
        )
        return {"path": artifact_name("discovery-plan", SHA), "sha256": digest}

    def call(
        ref, request, *, phase="select", nonce="test", mutate=None, wire_parts=None
    ):
        files = json.loads((tmp_path / ref["path"]).read_bytes())["files"]
        parts = (
            wire_parts
            if wire_parts is not None
            else plain_requests(request, files, phase)
        )
        token = json.dumps(parts, ensure_ascii=False, separators=(",", ":"))
        envelopes = []
        for part, value in enumerate(parts):
            plain = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
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
                str(len(parts)),
                "--request",
                plain,
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
            assert env["request_checksum"] == fnv1a32(plain)
            assert env["input_checksum"] == fnv1a32(token)
            envelopes.append(env)
        return envelopes[-1]["result"], envelopes

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
    selected, _ = call(
        ref, selection(tmp_path, refs=[["code-gauntlet:bug-detector", "src/0", 1, 1]])
    )
    final, _ = call(
        ref,
        selection(
            tmp_path,
            state_checksum=selected["state_checksum"],
            hunters=[
                {
                    "agent": "code-gauntlet:gap-hunter",
                    "status": "complete",
                    "anchors": [["outside", "src/0", 1]],
                    "refs": [["code-gauntlet:gap-hunter", "src/1", 2, 2]],
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
            "agent": "code-gauntlet:gap-hunter",
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
            document["plan"]["regions"][0]["line_end"] = 1
        else:
            document["assigned"] = []
        path.write_text(json.dumps(document), encoding="utf-8")
    value, _ = call(
        ref,
        selection(tmp_path, state_checksum=selected["state_checksum"], hunters=[]),
        phase="finalize",
    )
    assert value["available"] is False


def test_plain_citation_inputs_use_bounded_checked_parts(tmp_path, planner):
    prepare, call = planner
    ref = prepare(plan_with(300))
    refs = [["code-gauntlet:bug-detector", f"src/{i}", 1, 1] for i in range(300)]
    value, envs = call(ref, selection(tmp_path, refs=refs))
    assert value["available"]
    assert 1 < len(envs) <= 4
    assert all(len(json.dumps(e)) < RECEIPT_CHARS for e in envs)


@pytest.mark.parametrize(
    "change",
    [
        {"citations": {"0": "2:1"}},
        {"citations": {"0": "1:1;0:1"}},
        {"citations": {"0": "0:1;0:2"}},
        {"citations": {"0": "0:2,1"}},
        {"citations": {"0": "0:1-3,2"}},
        {"citations": {"0": "0:0"}},
        {"citations": {"0": "0:1-9007199254740992"}},
        {"citations": {"unknown": "0:1"}},
        {"high": "0-2"},
        {"high": "1,0"},
        {"high": "0", "low": "0"},
        {"files_checksum": "fnv1a32:0x00000000"},
        {"mode": "unknown"},
    ],
)
def test_planner_rejects_invalid_plain_indexes_even_with_correct_transport_proofs(
    tmp_path, planner, change
):
    prepare, call = planner
    ref = prepare(plan_with())
    files = json.loads((tmp_path / ref["path"]).read_bytes())["files"]
    part = {
        "files_checksum": fnv1a32(
            json.dumps(
                {"files": files, "reviewers": DISCOVERY_AGENT_TYPES},
                separators=(",", ":"),
            )
        ),
        "mode": "full",
        **change,
    }
    value, _ = call(ref, selection(tmp_path), wire_parts=[part])
    assert value["available"] is False


@pytest.mark.parametrize("second", ["0:1", "0:1-2", "0:0", "0:1;1:1"])
def test_plain_order_is_validated_across_part_boundaries(tmp_path, planner, second):
    prepare, call = planner
    ref = prepare(plan_with())
    files = json.loads((tmp_path / ref["path"]).read_bytes())["files"]
    base = {
        "files_checksum": fnv1a32(
            json.dumps(
                {"files": files, "reviewers": DISCOVERY_AGENT_TYPES},
                separators=(",", ":"),
            )
        ),
        "mode": "full",
    }
    parts = [{**base, "citations": {"0": "0:2"}}, {**base, "citations": {"0": second}}]
    value, _ = call(ref, selection(tmp_path), wire_parts=parts)
    assert value["available"] is False


def test_preparation_indexes_the_snapshot_manifest_including_files_without_regions(
    tmp_path, invoke
):
    out = tmp_path / "out"
    write_context_inputs(
        out,
        b"rules",
        b"diff --git a/source b/source\n--- a/source\n+++ b/source\n@@ -1 +1 @@\n-old\n+new\n",
    )
    (out / f"code-gauntlet-files-{SHA}.json").write_text(
        json.dumps(["unvisited_binary", "source"]), encoding="utf-8"
    )
    result = invoke(
        "write_shared_context",
        ["--output-dir", str(out), "--head-sha", SHA],
        tmp_path,
        b"triage",
    )
    assert result.returncode == 0
    receipt = json.loads(result.stdout)
    document = json.loads((out / receipt["discoveryPlanRef"]["path"]).read_bytes())
    assert document["files"] == ["source", "unvisited_binary"]
    assert {r["file"] for r in document["plan"]["regions"]} == {"source"}
    assert "unvisited_binary" not in result.stdout.decode()


@pytest.mark.parametrize(
    "part,parts,payload,checksum,exit_code",
    [
        (-1, 1, "{}", "fnv1a32:0x00000000", 2),
        (1, 1, "{}", "fnv1a32:0x00000000", 2),
        (0, 0, "{}", "fnv1a32:0x00000000", 2),
        (0, 4, "{}", "fnv1a32:0x00000000", 0),
        (0, 5, "{}", "fnv1a32:0x00000000", 2),
        (0, 1, " " * 998 + "{}", "fnv1a32:0x00000000", 0),
        (0, 1, " " * 999 + "{}", "fnv1a32:0x00000000", 2),
        (0, 1, "{}", "bad", 2),
    ],
)
def test_planner_cli_enforces_usage_bounds_before_optional_plan_fallback(
    tmp_path, part, parts, payload, checksum, exit_code
):
    args = [
        "--output-dir",
        str(tmp_path),
        "--sha",
        SHA,
        "--nonce",
        "test",
        "--phase",
        "select",
        "--plan-checksum",
        "0" * 64,
        "--input-checksum",
        checksum,
        "--part",
        str(part),
        "--parts",
        str(parts),
        "--request",
        payload,
    ]
    result = subprocess.run(
        [
            sys.executable,
            str(Path(__file__).resolve().parents[1] / "scripts/plan_discovery.py"),
            *args,
        ],
        cwd=tmp_path,
        capture_output=True,
        check=False,
    )
    assert result.returncode == exit_code
    if exit_code:
        assert result.stdout == b""
        assert result.stderr.startswith(b"plan_discovery:")
    else:
        assert json.loads(result.stdout)["status"] == "ok"


def test_planner_cli_rejects_a_receipt_that_exceeds_the_copy_budget(tmp_path, planner):
    prepare, _ = planner
    ref = prepare(plan_with())
    output = tmp_path
    for _ in range(3):
        output /= "é" * 80
    output.mkdir(parents=True)
    for name in (ref["path"], f"code-gauntlet-context-{SHA}.md"):
        (output / name).write_bytes((tmp_path / name).read_bytes())
    files = json.loads((output / ref["path"]).read_bytes())["files"]
    parts = plain_requests(selection(tmp_path), files, "select")
    body = json.dumps(parts[0], ensure_ascii=False, separators=(",", ":"))
    checksum = fnv1a32(json.dumps(parts, ensure_ascii=False, separators=(",", ":")))
    args = [
        "--output-dir",
        str(output),
        "--sha",
        SHA,
        "--nonce",
        "test",
        "--phase",
        "select",
        "--plan-checksum",
        ref["sha256"],
        "--input-checksum",
        checksum,
        "--part",
        "0",
        "--parts",
        "1",
        "--request",
        body,
    ]
    result = subprocess.run(
        [
            sys.executable,
            str(Path(__file__).resolve().parents[1] / "scripts/plan_discovery.py"),
            *args,
        ],
        cwd=tmp_path,
        capture_output=True,
        check=False,
    )
    assert (output / artifact_name("coverage-ledger", SHA, "test")).exists()
    assert result.returncode == 2
    assert result.stdout == b""
    assert b"receipt exceeds" in result.stderr


def test_a_bad_model_anchor_costs_only_that_record_not_the_final_ledger(
    tmp_path, planner
):
    prepare, call = planner
    ref = prepare(plan_with())
    selected, _ = call(ref, selection(tmp_path))
    files = json.loads((tmp_path / ref["path"]).read_bytes())["files"]
    part = {
        "files_checksum": fnv1a32(
            json.dumps(
                {"files": files, "reviewers": DISCOVERY_AGENT_TYPES},
                separators=(",", ":"),
            )
        ),
        "state_checksum": selected["state_checksum"],
        "hunters": [{"agent": "7", "status": "complete"}],
        "citations": {"7": "1:1"},
        "anchors": {
            "7": [
                None,
                ["outside", -1, 10],
                ["zero", 0, 0],
                ["duplicate", 0, 1],
                ["duplicate", 1, 1],
                ["valid", 1, 1],
            ]
        },
    }
    final, _ = call(ref, {}, phase="finalize", wire_parts=[part])
    assert final["available"]
    ledger = json.loads(
        (tmp_path / artifact_name("coverage-ledger", SHA, "test")).read_bytes()
    )
    assert ledger["remaining_uncovered"] == ["r0"]
    assert ledger["hunters"][0]["outside_assignment"] == ["outside"]
    assert final["summary"]["remaining_uncovered_count"] == 1


def test_planner_artifacts_use_portable_names_and_exact_utf8_lf_bytes(
    tmp_path, planner
):
    prepare, call = planner
    ref = prepare(plan_with())
    nonce = 'run:<>"|?*\\/\n雪'
    value, _ = call(ref, selection(tmp_path), nonce=nonce)
    assert value["available"]
    for kind, checksum in (
        ("gap-assignment", value["assignment_checksum"]),
        ("discovery-state", value["state_checksum"]),
        ("coverage-ledger", value["ledger_checksum"]),
    ):
        name = artifact_name(kind, SHA, nonce)
        assert not set(name) & set('<>:"/\\|?*\r\n')
        data = (tmp_path / name).read_bytes()
        assert data.endswith(b"\n") and b"\r" not in data
        assert hashlib.sha256(data).hexdigest() == checksum
        assert data.decode("utf-8").encode("utf-8") == data
    final, _ = call(
        ref,
        selection(tmp_path, state_checksum=value["state_checksum"], hunters=[]),
        phase="finalize",
        nonce=nonce,
    )
    assert final["available"]


def test_large_region_can_use_the_fact_reserve_when_it_is_the_only_assignment(
    tmp_path, planner
):
    prepare, call = planner
    plan = plan_with(1)
    plan["regions"][0]["reads"][0]["bytes"] = 18500
    value, _ = call(prepare(plan), selection(tmp_path))
    assert value["available"] and value["assigned_count"] == 1
    assert 18000 < value["summary"]["assigned_bytes"] <= 24000


@pytest.mark.parametrize("reason", ["scope", "primary_failure"])
def test_disabled_hunting_keeps_uncovered_regions_in_the_ledger(
    tmp_path, planner, reason
):
    prepare, call = planner
    value, _ = call(
        prepare(plan_with()), selection(tmp_path, enabled=False, disabled_reason=reason)
    )
    assert value["available"] and value["assigned_count"] == 0
    assert value["assignment_prompt"] == ""
    ledger = json.loads(
        (tmp_path / artifact_name("coverage-ledger", SHA, "test")).read_bytes()
    )
    assert ledger["omitted"] == [{"id": f"r{i}", "reason": reason} for i in range(2)]
    assert ledger["remaining_uncovered"] == ["r0", "r1"]


@pytest.mark.parametrize("kind", ["discovery-plan", "context"])
def test_planner_refuses_linked_input_artifacts(
    tmp_path, planner, symlink_or_skip, kind
):
    prepare, call = planner
    ref = prepare(plan_with())
    name = (
        ref["path"] if kind == "discovery-plan" else f"code-gauntlet-context-{SHA}.md"
    )
    path = tmp_path / name
    original = tmp_path / "original"
    path.rename(original)
    path.symlink_to(original)
    value, _ = call(ref, selection(tmp_path))
    assert value["available"] is False
    assert not (tmp_path / artifact_name("discovery-state", SHA, "test")).exists()


def test_planner_caps_artifact_reads_before_parsing(tmp_path, planner):
    from gauntlet.discovery_disk import MAX_FILE_BYTES

    prepare, call = planner
    ref = prepare(plan_with())
    path = tmp_path / ref["path"]
    with path.open("ab") as handle:
        handle.write(b" " * MAX_FILE_BYTES)
    value, _ = call(ref, selection(tmp_path))
    assert value["available"] is False


@pytest.mark.parametrize("offset", [0, 100])
def test_plan_reads_must_stay_inside_the_bound_context(tmp_path, planner, offset):
    prepare, call = planner
    plan = plan_with(1)
    plan["regions"][0]["reads"][0]["offset"] = offset
    value, _ = call(prepare(plan), selection(tmp_path))
    assert value["available"] is False


def test_plan_read_cost_cannot_omit_the_line_prefix_charge(tmp_path, planner):
    prepare, call = planner
    plan = plan_with(1)
    plan["regions"][0]["reads"][0]["bytes"] = 2
    value, _ = call(prepare(plan), selection(tmp_path))
    assert value["available"] is False


def test_plan_region_ids_cannot_alias_two_assignments(tmp_path, planner):
    prepare, call = planner
    plan = plan_with()
    plan["regions"][1]["id"] = plan["regions"][0]["id"]
    value, _ = call(prepare(plan), selection(tmp_path))
    assert value["available"] is False


def test_plan_paths_cannot_escape_the_repository(tmp_path, planner):
    prepare, call = planner
    plan = plan_with(1)
    plan["regions"][0]["file"] = "../outside"
    value, _ = call(prepare(plan), selection(tmp_path))
    assert value["available"] is False


def test_plan_head_ranges_cannot_run_backwards(tmp_path, planner):
    prepare, call = planner
    plan = plan_with(1)
    plan["regions"][0]["line_end"] = 0
    value, _ = call(prepare(plan), selection(tmp_path))
    assert value["available"] is False


def test_unavailable_plan_cannot_smuggle_region_reads(tmp_path, planner):
    prepare, call = planner
    plan = plan_with()
    plan["status"] = "unavailable"
    value, _ = call(prepare(plan), selection(tmp_path))
    assert value["available"] is False


def test_plan_fact_reads_need_the_same_bounds_as_regions(tmp_path, planner):
    prepare, call = planner
    plan = plan_with()
    plan["fact_packets"] = [{"related_files": ["src/0"], "reads": []}]
    value, _ = call(prepare(plan), selection(tmp_path))
    assert value["available"] is False


def test_request_parts_must_agree_on_selection_mode(tmp_path, planner):
    prepare, call = planner
    ref = prepare(plan_with())
    files = json.loads((tmp_path / ref["path"]).read_bytes())["files"]
    base = plain_requests(selection(tmp_path), files, "select")[0]
    value, _ = call(ref, {}, wire_parts=[base, {**base, "mode": "scope"}])
    assert value["available"] is False


def test_finalization_cannot_attribute_citations_to_an_undeclared_hunter(
    tmp_path, planner
):
    prepare, call = planner
    ref = prepare(plan_with())
    selected, _ = call(ref, selection(tmp_path))
    files = json.loads((tmp_path / ref["path"]).read_bytes())["files"]
    part = plain_requests(
        selection(tmp_path, state_checksum=selected["state_checksum"], hunters=[]),
        files,
        "finalize",
    )[0]
    part["citations"] = {"7": "0:1"}
    final, _ = call(ref, {}, phase="finalize", wire_parts=[part])
    assert final["available"] is False


def test_ledger_artifact_names_can_be_checked_without_accepting_other_files():
    from gauntlet.discovery_disk import valid_artifact_name

    name = artifact_name("coverage-ledger", SHA, "run:1")
    assert valid_artifact_name(name, "coverage-ledger")
    assert not valid_artifact_name("../" + name, "coverage-ledger")
    assert not valid_artifact_name(
        artifact_name("discovery-state", SHA, "run:1"), "coverage-ledger"
    )


def test_planner_rejects_citation_rows_without_a_file_index(tmp_path, planner):
    prepare, call = planner
    ref = prepare(plan_with())
    files = json.loads((tmp_path / ref["path"]).read_bytes())["files"]
    part = plain_requests(selection(tmp_path), files, "select")[0]
    part["citations"] = {"0": "1"}
    value, _ = call(ref, {}, wire_parts=[part])
    assert value["available"] is False


def test_request_parts_cannot_repeat_a_risk_priority(tmp_path, planner):
    prepare, call = planner
    ref = prepare(plan_with())
    files = json.loads((tmp_path / ref["path"]).read_bytes())["files"]
    part = plain_requests(selection(tmp_path), files, "select")[0]
    part["high"] = "0"
    value, _ = call(ref, {}, wire_parts=[part, part])
    assert value["available"] is False


@pytest.mark.parametrize(
    "violation", ["missing_proof", "duplicate_hunter", "unknown_anchor"]
)
def test_finalization_requires_a_bound_and_declared_hunter_set(
    tmp_path, planner, violation
):
    prepare, call = planner
    ref = prepare(plan_with())
    selected, _ = call(ref, selection(tmp_path))
    files = json.loads((tmp_path / ref["path"]).read_bytes())["files"]
    part = plain_requests(
        selection(tmp_path, state_checksum=selected["state_checksum"], hunters=[]),
        files,
        "finalize",
    )[0]
    if violation == "missing_proof":
        part.pop("state_checksum")
    elif violation == "duplicate_hunter":
        part["hunters"] = [{"agent": "7", "status": "complete"}] * 2
    else:
        part["anchors"] = {"unknown": [["finding", 0, 1]]}
    value, _ = call(ref, {}, wire_parts=[part], phase="finalize")
    assert value["available"] is False


def test_rewritten_stored_request_parts_cannot_change_the_selection(tmp_path, planner):
    prepare, call = planner
    ref = prepare(plan_with(300))
    refs = [["code-gauntlet:bug-detector", f"src/{i}", 1, 1] for i in range(300)]

    def rewrite_first_part(args, part):
        if part == 1:
            (stored,) = tmp_path.glob(
                f"code-gauntlet-discovery-*-select-*-0-{SHA}.json"
            )
            stored.write_bytes(b"{}")

    value, _ = call(ref, selection(tmp_path, refs=refs), mutate=rewrite_first_part)
    assert value["available"] is False

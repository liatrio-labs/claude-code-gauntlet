"""Prove delivery replay at the live caller and CLI boundaries."""

import copy
import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest
from gauntlet.jsjson import js_stringify_pretty

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = Path(__file__).parent / "fixtures"
SOURCE_DIGEST = "0" * 64


def _hash(value):
    return hashlib.sha256(value).hexdigest()


@pytest.fixture(scope="module")
def evidence():
    return json.loads(
        subprocess.run(
            ["node", str(FIXTURES / "delivery_replay.mjs")],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=True,
        ).stdout
    )


def _write(path, value):
    path.write_bytes(js_stringify_pretty(value).encode("utf-8"))


def _manifest(tmp_path, evidence, mode="return", trace=True):
    row = copy.deepcopy(evidence[mode])
    record = row["record"]
    args = record["args"]
    for output in record["state"]["phaseOutputs"].values():
        for bucket in ("findings", "filtered", "unverified", "eliminated"):
            if bucket in output:
                output[bucket] = [
                    {
                        k: v
                        for k, v in f.items()
                        if k
                        not in ("trace_id", "discovery_agent", "discovery_dimension")
                    }
                    for f in output[bucket]
                ]
    source_paths = {}
    for role, value in (
        ("workflow", record),
        (
            "checkpoint",
            json.loads(
                next(
                    text
                    for path, text in evidence["return"]["artifacts"].items()
                    if "checkpoint-all" in path
                )
            ),
        ),
        ("trace", row["trace"]),
    ):
        path = tmp_path / f"{role}.json"
        _write(path, value)
        source_paths[role] = {"path": path.name, "sha256": _hash(path.read_bytes())}
    manifest = {
        "schema_version": 1,
        "source_plugin_sha256": SOURCE_DIGEST,
        "args": args,
        "sources": {k: v for k, v in source_paths.items() if k != "trace"},
        "trace": {
            "source": source_paths["trace"] if trace else None,
        },
    }
    path = tmp_path / "manifest.json"
    _write(path, manifest)
    return path


def _cli(manifest, out):
    return subprocess.run(
        [
            sys.executable,
            str(ROOT / "bench/replay_delivery.py"),
            "--manifest",
            str(manifest),
            "--out",
            str(out),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


@pytest.mark.parametrize("read_input, exit_code", [(False, 1), (True, 0), (True, 1)])
def test_replay_surfaces_bounded_stderr_without_blocking(
    tmp_path, evidence, read_input, exit_code
):
    manifest = _manifest(tmp_path, evidence)
    wait_for_input = """
const reader = createInterface({ input: process.stdin });
await reader[Symbol.asyncIterator]().next();
"""
    node = f"""
import {{ writeFileSync }} from 'node:fs';
import {{ createInterface }} from 'node:readline';
{wait_for_input if read_input else ""}
writeFileSync(2, 'diagnostic-start\\n' + 'x'.repeat(1024 * 1024) + '\\ndiagnostic-end');
process.exit({exit_code});
"""
    launcher = """
import sys
sys.path.insert(0, 'scripts')
from bench.runner import delivery_replay
delivery_replay.NODE = sys.argv.pop(1)
sys.exit(delivery_replay.main())
"""
    out = tmp_path / "out"
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            launcher,
            node,
            "--manifest",
            str(manifest),
            "--out",
            str(out),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=10,
    )
    assert result.returncode == 2, result.stderr
    assert "refused: incomplete replay dispatch capture" in result.stderr
    assert "Node stderr (tail):" in result.stderr
    assert result.stderr.endswith("diagnostic-end\n")
    assert "diagnostic-start" not in result.stderr
    assert len(result.stderr) < 4300
    assert not out.exists()


@pytest.mark.parametrize("mode", ["return", "derived", "legacy", "writer-failure"])
def test_replay_preserves_bytes_and_occurrence_identity(tmp_path, mode, evidence):
    manifest = _manifest(tmp_path, evidence, mode)
    before = {p: p.read_bytes() for p in tmp_path.iterdir()}
    runs = []
    for name in ("first", "second"):
        out = tmp_path / name
        result = _cli(manifest, out)
        assert result.returncode == 0, result.stderr
        runs.append({p.name: p.read_bytes() for p in out.iterdir()})
    assert runs[0] == runs[1]
    assert all(p.read_bytes() == data for p, data in before.items())
    files = runs[0]
    artifacts = {p: data for p, data in files.items() if p.startswith("code-gauntlet-")}
    assert artifacts == {
        Path(p).name: text.encode("utf-8")
        for p, text in evidence[mode]["artifacts"].items()
    }
    trace = json.loads(files["measurement-trace.json"])
    assert trace == evidence[mode]["trace"]
    assert files["envelope.json"] == js_stringify_pretty(evidence[mode]["out"]).encode(
        "utf-8"
    )
    holdback = next(s for s in trace["snapshots"] if s["stage"] == "holdback")
    select = next(s for s in trace["snapshots"] if s["stage"] == "select")
    assert any(d["decision"] == "held_back" for d in holdback["decisions"])
    assert any(d["reason_code"] == "delivery_cap" for d in select["decisions"])
    assert any(
        d["reason_code"] == "challenge_removed"
        for s in trace["snapshots"]
        for d in s["decisions"]
    )
    assert any(v["finding"].get("consolidation_key") for v in trace["versions"])
    receipt = json.loads(files["replay-receipt.json"])
    assert receipt["source_plugin_sha256"] == SOURCE_DIGEST
    assert receipt["trace_availability"] == "available"
    assert receipt["artifact_hashes"] == {
        p: _hash(data) for p, data in files.items() if p != "replay-receipt.json"
    }
    if mode == "return":
        _manifest(tmp_path, evidence, trace=False)
        result = _cli(manifest, tmp_path / "unavailable")
        assert result.returncode == 0, result.stderr
        assert not (tmp_path / "unavailable/measurement-trace.json").exists()
        assert (
            json.loads((tmp_path / "unavailable/replay-receipt.json").read_bytes())[
                "trace_availability"
            ]
            == "unavailable"
        )
        assert {
            p.name: p.read_bytes()
            for p in (tmp_path / "unavailable").glob("code-gauntlet-*")
        } == artifacts


@pytest.mark.parametrize(
    "case",
    [
        "missing",
        "tampered",
        "phase",
        "statistics",
        "methodology",
        "challenge",
        "duplicate",
        "ambiguous",
        "trace-version",
        "unsupported-dispatch",
        "dispatch-drift",
        "return-fallback",
        "checkpoint-drift",
        "args-drift",
        "symlink",
        "overwrite",
        "denied-env",
        "denied-auth",
        "denied-pairing",
        "denied-config",
        "denied-home-claude",
        "denied-home-config",
        "denied-home-codex",
        "denied-credential",
    ],
)
def test_replay_cli_refuses_incomplete_or_ambiguous_evidence(
    tmp_path, case, monkeypatch, evidence
):
    manifest = _manifest(
        tmp_path,
        evidence,
        "writer-failure"
        if case == "challenge"
        else "return"
        if case in ("return-fallback", "methodology")
        else "derived",
        trace=case != "return-fallback",
    )
    wire = json.loads(manifest.read_bytes())
    workflow = tmp_path / "workflow.json"
    record = json.loads(workflow.read_bytes())
    out = tmp_path / "out"
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    if case == "missing":
        wire["sources"]["checkpoint"]["path"] = "absent.json"
    elif case == "tampered":
        wire["sources"]["workflow"]["sha256"] = "0" * 64
    elif case == "phase":
        del record["state"]["phaseOutputs"]["discover"]
    elif case == "statistics":
        del record["state"]["phaseOutputs"]["validate"]["stats"]
    elif case == "methodology":
        del record["state"]["phaseOutputs"]["merge"]["methodology"]
    elif case == "duplicate":
        findings = record["state"]["phaseOutputs"]["challenge"]["findings"]
        findings.append(copy.deepcopy(findings[0]))
    elif case in ("ambiguous", "trace-version"):
        trace_path = tmp_path / "trace.json"
        trace = json.loads(trace_path.read_bytes())
        if case == "trace-version":
            trace["versions"].append(trace["versions"][0])
        else:
            holdback = next(s for s in trace["snapshots"] if s["stage"] == "holdback")
            challenge = next(s for s in trace["snapshots"] if s["stage"] == "challenge")
            extra = copy.deepcopy(
                next(
                    v
                    for v in trace["versions"]
                    if v["version_id"] == challenge["finding_version_ids"][0]
                )
            )
            extra["version_id"], extra["trace_id"] = "v999", "nonce-xyz:99:0"
            extra["finding"]["trace_id"] = extra["trace_id"]
            trace["versions"].append(extra)
            holdback["finding_version_ids"].append(extra["version_id"])
        _write(trace_path, trace)
        wire["trace"]["source"]["sha256"] = _hash(trace_path.read_bytes())
    elif case == "unsupported-dispatch":
        record["persistence"][0]["label"] = "review-agent"
    elif case == "dispatch-drift":
        record["persistence"][0]["prompt_sha256"] = "0" * 64
    elif case == "return-fallback":
        record["state"]["phaseOutputs"]["challenge"]["findings"][0]["diagnostic"] = (
            "x" * 300000
        )
    elif case in ("checkpoint-drift", "challenge"):
        path = tmp_path / "checkpoint.json"
        checkpoint = json.loads(path.read_bytes())
        if case == "challenge":
            del checkpoint["phases"]["challenge"]
        else:
            checkpoint["counts"]["discover"] += 1
        _write(path, checkpoint)
        wire["sources"]["checkpoint"]["sha256"] = _hash(path.read_bytes())
    elif case == "args-drift":
        wire["args"]["generatedAt"] = "different timestamp"
    elif case == "symlink":
        (tmp_path / "linked.json").symlink_to(workflow)
        wire["sources"]["workflow"]["path"] = "linked.json"
    elif case == "overwrite":
        out = tmp_path
    else:
        wire["sources"]["workflow"]["path"] = {
            "denied-env": ".env.json",
            "denied-auth": "auth.json",
            "denied-pairing": "pairing/input.json",
            "denied-config": "home/.ssh/input.json",
            "denied-home-claude": "home/.claude/input.json",
            "denied-home-config": "home/.config/input.json",
            "denied-home-codex": "home/.codex/input.json",
            "denied-credential": "credentials.json",
        }[case]
    _write(workflow, record)
    if case != "tampered":
        wire["sources"]["workflow"]["sha256"] = _hash(workflow.read_bytes())
    if case.startswith("denied-"):
        denied = tmp_path / wire["sources"]["workflow"]["path"]
        denied.parent.mkdir(parents=True, exist_ok=True)
        _write(denied, record)
    _write(manifest, wire)
    before = {
        p: p.read_bytes()
        for p in (
            manifest,
            workflow,
            tmp_path / "trace.json",
            tmp_path / "checkpoint.json",
        )
    }
    result = _cli(manifest, out)
    assert result.returncode == 2, result.stderr
    assert "refused" in result.stderr
    if case.startswith("denied-"):
        assert "denied evidence path" in result.stderr
    assert all(p.read_bytes() == data for p, data in before.items())
    assert not (tmp_path / "out").exists()


@pytest.mark.parametrize(
    "directory", [".claude/worktrees/review", ".claude/handoff", ".config/evidence"]
)
def test_replay_accepts_project_configuration_paths(tmp_path, directory, evidence):
    root = tmp_path / directory
    root.mkdir(parents=True)
    manifest = _manifest(root, evidence)
    result = _cli(manifest, root / "out")
    assert result.returncode == 0, result.stderr

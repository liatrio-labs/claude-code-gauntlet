"""Replay the production delivery seam with a confined, deterministic host."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import re
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from tempfile import TemporaryDirectory, TemporaryFile
from typing import Any, Literal, TypedDict

from gauntlet.artifacts import assemble, plan_checksum
from gauntlet.jsjson import js_stringify_pretty
from gauntlet.registry import Disposition

from bench.runner.decode import decode
from bench.runner.epochs import digest
from bench.runner.observations import ReasonCode
from bench.runner.provenance import PLUGIN_PATHS, ROOT, reconstruct_delivery
from bench.runner.stages import MeasurementTrace, load_trace


class SourceWire(TypedDict):
    path: str
    sha256: str


class TraceBindingWire(TypedDict):
    source: SourceWire | None


class _RequiredReplayInputWire(TypedDict):
    schema_version: Literal[1]
    source_plugin_sha256: str
    args: dict[str, Any]
    sources: dict[Literal["workflow", "checkpoint"], SourceWire]
    trace: TraceBindingWire


class ReplayInputWire(_RequiredReplayInputWire, total=False):
    diff: SourceWire


class DispatchWire(TypedDict):
    label: str
    agent_type: str
    prompt_sha256: str
    result: Any
    error: str | None


class DeliveryStateWire(TypedDict):
    limits: dict[str, Any]
    gaps: list[str]
    completed: list[str]
    phaseOutputs: dict[str, dict[str, Any]]
    resolvedReview: dict[str, Any]
    reviewMdSubtrees: list[dict[str, Any]]
    derivedAgentFlags: dict[str, Any]
    phaseReached: str


class WorkflowWire(TypedDict):
    args: dict[str, Any]
    pipelineVersion: str | None
    state: DeliveryStateWire
    persistence: list[DispatchWire]


class ReplayReceiptWire(TypedDict):
    schema_version: Literal[1]
    source_plugin_sha256: str
    target_plugin_sha256: str
    input_hashes: dict[str, str]
    artifact_hashes: dict[str, str]
    trace_availability: Literal["available", "unavailable"]


class DeliveryDeltaWire(TypedDict):
    member: int
    id: object
    issue: object
    before: object
    after: Disposition
    reasons: list[ReasonCode]
    surface: Literal["inline", "body"] | None


@dataclass(frozen=True, slots=True)
class ReplayInputs:
    source_plugin_sha256: str
    workflow: WorkflowWire
    checkpoint: bytes
    trace: MeasurementTrace | None
    input_hashes: tuple[tuple[str, str], ...]
    source_paths: tuple[Path, ...]
    diff: bytes | None = None


@dataclass(frozen=True, slots=True)
class ReplayResult:
    artifacts: tuple[tuple[str, bytes], ...]
    receipt: bytes


PHASES = ("summarize", "discover", "merge", "verify", "validate", "filter", "challenge")
UpstreamStage = Literal["verify", "filter"]
UPSTREAM_STAGES: tuple[UpstreamStage, ...] = ("verify", "filter")
IDENTITY = ("trace_id", "discovery_agent", "discovery_dimension")
DISPATCHES = {
    "artifact-writer": "code-gauntlet:artifact-writer",
    "assemble-artifacts": "code-gauntlet:executor",
}
DENIED_NAMES = {"pairing", "auth.json", "secrets.json", "token.json"}
CONFIG_DIRS = (
    ".claude",
    ".config",
    ".ssh",
    ".codex",
    ".aws",
    ".gnupg",
    ".kube",
    ".azure",
)


def _hash(data):
    return hashlib.sha256(data).hexdigest()


def _safe(path):
    parts = {p.lower() for p in path.parts}
    home = Path.home().resolve()
    denied = (
        bool(parts & DENIED_NAMES)
        or any(p.startswith(".env") or "credential" in p for p in parts)
        or any(path.is_relative_to(home / name) for name in CONFIG_DIRS)
    )
    if denied:
        raise ValueError("denied evidence path")
    return path


def _projection(finding):
    return {k: v for k, v in finding.items() if k not in IDENTITY}


def rejoin_trace_identity(challenge, trace, stage: UpstreamStage | None = None):
    """Use full projections and holdback references, never a finding-id join."""
    (holdback,) = [s for s in trace.snapshots if s.stage in ("holdback", "dispose")]
    versions = {v.version_id: v for v in trace.versions}
    allowed = {versions[v].trace_id for v in holdback.finding_version_ids}
    allowed.update(d.trace_id for d in holdback.decisions)
    allowed.update(
        d.trace_id
        for s in trace.snapshots
        if s.stage == "challenge"
        for d in s.decisions
    )
    if stage is not None:
        allowed.update(
            d.trace_id for s in trace.snapshots if s.stage == stage for d in s.decisions
        )
    candidates = [v.projection() for v in trace.versions if v.trace_id in allowed]
    used: set[str] = set()
    for bucket in ("findings", "unverified", "eliminated"):
        for finding in challenge[bucket]:
            # Skipped challenges carry a marker but may have no recorded version.
            marker = finding.get("challenge")
            if bucket == "unverified" and marker in ("capped", "failed"):
                reason = "challenge_cap" if marker == "capped" else "challenge_failed"
                referenced = {
                    d.trace_id
                    for s in trace.snapshots
                    if s.stage in ("challenge", "holdback", "dispose")
                    for d in s.decisions
                    if d.reason_code == reason
                }
                prior = {k: v for k, v in finding.items() if k != "challenge"}
                candidates.extend(
                    {**v.projection(), "challenge": marker}
                    for v in trace.versions
                    if v.trace_id in referenced and _projection(v.finding) == prior
                )
            matches = {f["trace_id"] for f in candidates if _projection(f) == finding}
            if len(matches) != 1 or used & matches:
                raise ValueError("missing or ambiguous challenge occurrence")
            (tid,) = matches
            used.add(tid)
            matched = next(
                f
                for f in reversed(candidates)
                if f["trace_id"] == tid and _projection(f) == finding
            )
            finding.clear()
            finding.update(matched)


def load_replay_inputs(manifest_path):
    path = _safe(Path(manifest_path).absolute())
    root = _safe(path.resolve(strict=True)).parent
    if path.is_symlink() or path.suffix != ".json":
        raise ValueError("manifest must be a regular JSON artifact")
    raw = path.read_bytes()
    wire = decode(ReplayInputWire, json.loads(raw))
    if set(wire["sources"]) != {"workflow", "checkpoint"} or not re.fullmatch(
        r"[a-f0-9]{64}", wire["source_plugin_sha256"]
    ):
        raise ValueError("incomplete source binding")
    sources = dict(wire["sources"])
    binding = wire["trace"]
    if binding["source"] is not None:
        sources["trace"] = binding["source"]
    if "diff" in wire:
        sources["diff"] = wire["diff"]
    contents, paths, hashes = {}, [path.resolve()], [("manifest", _hash(raw))]
    for role, source in sources.items():
        candidate = _safe(root / source["path"])
        resolved = _safe(candidate.resolve(strict=True))
        if (
            candidate.is_symlink()
            or not resolved.is_relative_to(root)
            or resolved.suffix != (".patch" if role == "diff" else ".json")
            or resolved in paths
        ):
            raise ValueError("source escapes evidence root or repeats an artifact")
        data = resolved.read_bytes()
        if _hash(data) != source["sha256"]:
            raise ValueError("source byte hash mismatch")
        paths.append(resolved)
        hashes.append((role, _hash(data)))
        contents[role] = data
    record = decode(WorkflowWire, json.loads(contents["workflow"]))
    state = record["state"]
    if (
        record["args"] != wire["args"]
        or state["completed"] != list(PHASES)
        or state["phaseReached"] != "challenge"
        or tuple(state["phaseOutputs"]) != PHASES
    ):
        raise ValueError("missing original workflow phase inputs")
    for phase in PHASES:
        output = state["phaseOutputs"][phase]
        required = {
            "summarize": {"summary"},
            "discover": {"findings", "dispatched", "degraded"},
            "merge": {"findings", "methodology"},
            "verify": {"findings", "verified", "inputProof"},
            "validate": {"findings", "stats"},
            "filter": {"filtered", "stats"},
            "challenge": {"findings", "unverified", "eliminated", "stats"},
        }[phase]
        if not required <= output.keys():
            raise ValueError("missing rendering inputs for " + phase)
    for dispatch in record["persistence"]:
        if DISPATCHES.get(dispatch["label"]) != dispatch["agent_type"]:
            raise ValueError("unsupported persistence dispatch")
    for output in state["phaseOutputs"].values():
        for bucket in ("findings", "filtered", "unverified", "eliminated"):
            if bucket in output:
                output[bucket] = [_projection(f) for f in output[bucket]]
    trace = None
    if "trace" in contents:
        trace = load_trace(
            root / sources["trace"]["path"], expected_sha256=sources["trace"]["sha256"]
        )
        if trace.run_nonce != wire["args"].get("nonce") or not trace.complete:
            raise ValueError("trace belongs to another or incomplete review")
        rejoin_trace_identity(state["phaseOutputs"]["challenge"], trace)
        for stage in UPSTREAM_STAGES:
            rejoin_trace_identity(
                {
                    "findings": [],
                    "unverified": [],
                    "eliminated": state["phaseOutputs"][stage].get("eliminated", []),
                },
                trace,
                stage,
            )
    return ReplayInputs(
        wire["source_plugin_sha256"],
        record,
        contents["checkpoint"],
        trace,
        tuple(hashes),
        tuple(paths),
        contents.get("diff"),
    )


NODE = r"""
import { createInterface } from 'node:readline';
import { createHash } from 'node:crypto';
import { deliverFromCheckpoint } from './workflows/src/run.js';
import { parseWriterPayload } from './workflows/src/persist.js';
import { plannedArtifactPaths, persistPlanPath } from './workflows/src/outputPaths.js';
import { validateArgs } from './workflows/src/args.js';
import { checkpointChecksumErrors, checkpointShapeErrors, readCheckpoints, slimPersistedCheckpoints, applyChallengeReplayBelt } from './workflows/src/checkpoints.js';
import { writerPayload } from './workflows/src/persistPlan.js';
import { createMeasurementTrace, recordStage, appendMeasurement } from './workflows/src/measurementTrace.js';
const reader = createInterface({ input: process.stdin });
const lines = reader[Symbol.asyncIterator]();
const input = JSON.parse((await lines.next()).value);
const send = (value) => process.stdout.write(JSON.stringify(value) + '\n');
const { args, state } = input;
try {
  if (!validateArgs(args).ok || checkpointChecksumErrors(args.checkpoints).length || checkpointShapeErrors(readCheckpoints(args)).length) throw new Error('invalid original args');
  if (!input.checkpoint?.phases?.challenge || checkpointChecksumErrors(input.checkpoint).length || checkpointShapeErrors(readCheckpoints({ checkpoints: input.checkpoint })).length) throw new Error('invalid source checkpoint');
  state.measurementTrace = input.trace ? createMeasurementTrace(args.nonce) : null;
  if (input.trace) {
    for (const version of input.trace.versions) recordStage(state.measurementTrace, 'discover', [version.finding]);
    state.measurementTrace.snapshots = input.trace.snapshots.filter((s) => !['dispose', 'holdback', 'select', 'deliver'].includes(s.stage));
  }
  state.measured = (out) => appendMeasurement(out, state.measurementTrace);
  state.runPhase = async (name, thunk) => {
    const out = await thunk();
    state.phaseOutputs[name] = out;
    state.completed.push(name);
    state.phaseReached = name;
    return out;
  };
  const ctx = { pipelineVersion: input.pipelineVersion,
    agent: async (prompt, dispatch) => {
      const payload = parseWriterPayload(prompt);
      const paths = plannedArtifactPaths(args.outputDir, args.headShaShort);
      const entries = dispatch.label === 'artifact-writer' && payload && !Array.isArray(payload)
        ? Object.entries(paths).map(([key, path]) => ({ path, text: key === 'report' ? payload.report : JSON.stringify(payload[key], null, 2) })) : payload;
      send({ dispatch, prompt_sha256: createHash('sha256').update(prompt).digest('hex'), entries });
      const reply = JSON.parse((await lines.next()).value);
      if (reply.error) throw new Error(reply.error);
      return reply.result;
    } };
  send({ paths: { ...plannedArtifactPaths(args.outputDir, args.headShaShort), plan: persistPlanPath(args.outputDir, args.headShaShort) } });
  const out = await deliverFromCheckpoint(ctx, args, state);
  const checkpoint = slimPersistedCheckpoints(state.phaseOutputs, state.completed, state.phaseReached);
  const challenge = JSON.parse(JSON.stringify(state.phaseOutputs.challenge));
  applyChallengeReplayBelt(challenge);
  const baseline = writerPayload({ findings: [...challenge.findings, ...challenge.unverified, ...challenge.eliminated] }).findings;
  send({ out, checkpoint, baseline });
} catch (error) { send({ error: error.message }); }
reader.close();
process.stdin.destroy();
"""


def _assemble_entries(artifacts, plan_path):
    plan = json.loads(artifacts[plan_path])
    with TemporaryDirectory(prefix="delivery-assembly-") as tmp:
        mapping = {p: str(Path(tmp) / Path(p).name) for p in artifacts}
        for key in ("postReview", "checkpoint"):
            mapping[plan[key]["path"]] = str(Path(tmp) / Path(plan[key]["path"]).name)
        relocated = copy.deepcopy(plan)
        for row in relocated["expect"] + relocated["derive"]:
            row["path"] = mapping[row["path"]]
        for key in ("postReview", "checkpoint"):
            for field in ("path", "source"):
                if field in relocated[key]:
                    relocated[key][field] = mapping[relocated[key][field]]
        relocated["planChecksum"] = plan_checksum(relocated)
        for path, text in artifacts.items():
            Path(mapping[path]).write_bytes(text.encode("utf-8"))
        Path(mapping[plan_path]).write_bytes(
            js_stringify_pretty(relocated).encode("utf-8")
        )
        receipt = assemble(mapping[plan_path])
        reverse = {v: k for k, v in mapping.items()}
        for row in receipt["verified"] + receipt["written"]:
            row["path"] = reverse[row["path"]]
        receipt["planChecksum"] = plan["planChecksum"]
        if not receipt["ok"]:
            raise ValueError("local assembly refused: " + str(receipt["errors"]))
        for row in receipt["written"]:
            artifacts[row["path"]] = (
                Path(mapping[row["path"]]).read_bytes().decode("utf-8")
            )
        return receipt


def _target_digest():
    entries = subprocess.run(
        ["git", "ls-files", "--stage", "-z", "--", *PLUGIN_PATHS],
        cwd=ROOT,
        capture_output=True,
        check=True,
    ).stdout.split(b"\0")
    rows = [e.decode("utf-8").split("\t", 1) for e in entries if e]
    paths = [name for _, name in rows]
    for name in paths:
        path = _safe(ROOT / name)
        if path.is_symlink():
            raise ValueError("unsupported plugin symlink")
    hashes = (
        subprocess.run(
            ["git", "hash-object", "--no-filters", "--", *paths],
            cwd=ROOT,
            capture_output=True,
            check=True,
        )
        .stdout.decode("ascii")
        .splitlines()
    )
    return digest(
        sorted(
            f"{header.split()[0]} {sha} {header.split()[2]}\t{name}"
            for (header, name), sha in zip(rows, hashes, strict=True)
        )
    )


def _answer_dispatch(message, expected, artifacts, paths, *, candidate=False):
    dispatch = message["dispatch"]
    if candidate:
        if DISPATCHES.get(dispatch["label"]) != dispatch["agentType"]:
            raise ValueError("unsupported persistence dispatch")
        if dispatch["label"] == "assemble-artifacts":
            return {
                "result": _assemble_entries(artifacts, paths["plan"]),
                "error": None,
            }
        entries = message["entries"]
        if not isinstance(entries, list) or not entries:
            raise ValueError("missing local persistence entries")
        written = []
        for entry in entries:
            if entry["path"] not in paths.values():
                raise ValueError("unplanned artifact path")
            artifacts[entry["path"]] = entry["text"]
            written.append(entry["path"])
        return {"result": {"written": written}, "error": None}
    if expected is None or (
        dispatch["label"],
        dispatch["agentType"],
        message["prompt_sha256"],
    ) != (
        expected["label"],
        expected["agent_type"],
        expected["prompt_sha256"],
    ):
        raise ValueError("unsupported or changed persistence dispatch")
    reply = {"result": expected["result"], "error": expected["error"]}
    if expected["error"] or expected["result"] is None:
        return reply
    if dispatch["label"] == "artifact-writer":
        for entry in message["entries"]:
            if entry["path"] not in paths.values():
                raise ValueError("unplanned artifact path")
            artifacts[entry["path"]] = entry["text"]
    elif expected["result"].get("ok"):
        actual = _assemble_entries(artifacts, paths["plan"])
        if actual != expected["result"]:
            raise ValueError("recorded assembly receipt differs")
    return reply


def _candidate_artifacts(inputs, artifacts, paths, before, expected_checkpoint):
    raw = json.loads(artifacts[paths["postReview"]])
    surfaces: dict[tuple[object, object], Literal["inline", "body"]] = {}
    if not isinstance(raw, dict):
        artifacts["post-review-payload.json"] = js_stringify_pretty(
            {"status": "unavailable", "reason": "missing PR identity"}
        )
    else:
        platform = raw["platform"]
        available = inputs.diff is not None and platform == "github"
        reconstructed = reconstruct_delivery(
            raw["findings"],
            inputs.diff.decode("utf-8") if available else "",
            platform=platform,
            review_body=raw.get("review_body", ""),
            sha=raw["sha"],
        )
        for group in reconstructed.groups:
            surfaces.update(
                ((f.get("id"), f.get("file")), group.surface) for f in group.members
            )
        comments = [
            {
                "path": group.path,
                "line": group.line,
                "side": "RIGHT",
                "body": group.body,
            }
            for group in reconstructed.groups
            if group.surface == "inline"
        ]
        payload = (
            {
                "platform": platform,
                "payload": {
                    "body": reconstructed.summary,
                    "event": "COMMENT",
                    "comments": comments,
                },
            }
            if platform == "github"
            else {
                "platform": platform,
                "summary": {"body": reconstructed.summary},
                "discussions": [],
            }
        )
        payload["placement"] = "available" if available else "unavailable"
        if not available:
            payload["placement_reason"] = (
                "saved diff unavailable"
                if inputs.diff is None
                else "saved GitLab base/start SHAs unavailable"
            )
        artifacts["post-review-payload.json"] = js_stringify_pretty(payload)
    disposed = json.loads(artifacts[paths["findings"]])
    checkpoint = json.loads(artifacts[paths["checkpoints"]])

    def projection(finding):
        return json.dumps(
            {
                k: v
                for k, v in finding.items()
                if k
                not in (
                    "disposition",
                    "disposition_reasons",
                    "consolidation_key",
                    "consolidation_primary",
                    "delivery_group",
                    "site_requests",
                    "resolved_sites",
                )
            },
            sort_keys=True,
        )

    deltas: list[DeliveryDeltaWire] = []
    for index, finding in enumerate(disposed):
        position = next(
            (
                i
                for i, old in enumerate(before)
                if projection(old) == projection(finding)
            ),
            None,
        )
        if position is None:
            raise ValueError("unregistered finding delta")
        old = before.pop(position)
        deltas.append(
            {
                "member": index,
                "id": finding.get("id"),
                "issue": finding.get("consolidation_key", finding.get("id")),
                "before": old.get("disposition"),
                "after": finding["disposition"],
                "reasons": finding["disposition_reasons"],
                "surface": surfaces.get((finding.get("id"), finding.get("file"))),
            }
        )
    if before:
        raise ValueError("unregistered member omission")
    if checkpoint != expected_checkpoint:
        raise ValueError("unregistered raw checkpoint delta")
    artifacts["delivery-deltas.json"] = js_stringify_pretty(deltas)


def replay_delivery(inputs, *, candidate=False):
    record = copy.deepcopy(inputs.workflow)
    if candidate:
        from gauntlet.diff import parse_diff
        from gauntlet.verify.decide import validate_diff_lines

        facts = (
            parse_diff(inputs.diff.decode("utf-8"), policy="verify-both-spellings")
            if inputs.diff is not None
            else None
        )
        for bucket in ("findings", "unverified"):
            for f in record["state"]["phaseOutputs"]["challenge"][bucket]:
                if "diff_validation" not in f:
                    # Membership replay must not reclassify or downgrade the record.
                    verified = copy.deepcopy(f)
                    validate_diff_lines(verified, facts)
                    f["diff_validation"] = verified["diff_validation"]
    paths = {}
    artifacts = {}
    dispatches = iter(record["persistence"])
    failure = None
    envelope = None
    baseline = None
    expected_checkpoint = None
    request = dict(
        record,
        checkpoint=json.loads(inputs.checkpoint),
        trace=asdict(inputs.trace) if inputs.trace is not None else None,
    )
    # A file cannot fill a pipe while the host waits for the next dispatch.
    with (
        TemporaryFile() as diagnostics,
        subprocess.Popen(
            ["node", "--input-type=module", "-e", NODE],
            cwd=ROOT,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=diagnostics,
            text=True,
            encoding="utf-8",
        ) as child,
    ):
        assert child.stdin is not None and child.stdout is not None
        try:
            child.stdin.write(json.dumps(request) + "\n")
            child.stdin.flush()
            for line in child.stdout:
                message = json.loads(line)
                if "paths" in message:
                    paths = message["paths"]
                    continue
                if "out" in message or "error" in message:
                    envelope = message.get("out")
                    baseline = message.get("baseline")
                    expected_checkpoint = message.get("checkpoint")
                    failure = failure or message.get("error")
                    continue
                try:
                    reply = _answer_dispatch(
                        message,
                        next(dispatches, None),
                        artifacts,
                        paths,
                        candidate=candidate,
                    )
                except (ValueError, KeyError, TypeError) as exc:
                    failure = str(exc)
                    reply = {"error": str(exc)}
                child.stdin.write(json.dumps(reply) + "\n")
                child.stdin.flush()
        except BrokenPipeError:
            # Early child exits still need their diagnostics in the refusal.
            pass
        child.wait()
        diagnostics.seek(max(0, diagnostics.tell() - 4096))
        stderr_tail = diagnostics.read().decode("utf-8", errors="replace").strip()
    if (
        failure
        or child.returncode
        or envelope is None
        or (not candidate and next(dispatches, None) is not None)
    ):
        reason = failure or "incomplete replay dispatch capture"
        if stderr_tail and (child.returncode or envelope is None):
            reason += "\nNode stderr (tail):\n" + stderr_tail
        raise ValueError(reason)
    if "persistReturn" in envelope:
        for entry in envelope["persistReturn"]["entries"]:
            if entry["path"] not in paths.values():
                raise ValueError("unplanned RETURN artifact path")
            artifacts[entry["path"]] = entry["text"]
        _assemble_entries(artifacts, envelope["persistReturn"]["planPath"])
    if (
        not candidate
        and paths["checkpoints"] in artifacts
        and artifacts[paths["checkpoints"]].encode("utf-8") != inputs.checkpoint
    ):
        raise ValueError("rendered checkpoint differs from source evidence")
    artifacts["envelope.json"] = js_stringify_pretty(envelope)
    if envelope.get("measurement", {}).get("status") == "available":
        artifacts["measurement-trace.json"] = envelope["measurement"]["text"]
    if candidate:
        _candidate_artifacts(inputs, artifacts, paths, baseline, expected_checkpoint)
    result = tuple(
        (Path(p).name, text.encode("utf-8")) for p, text in artifacts.items()
    )
    receipt: ReplayReceiptWire = {
        "schema_version": 1,
        "source_plugin_sha256": inputs.source_plugin_sha256,
        "target_plugin_sha256": _target_digest(),
        "input_hashes": dict(inputs.input_hashes),
        "artifact_hashes": {p: _hash(data) for p, data in result},
        "trace_availability": "available"
        if inputs.trace is not None
        else "unavailable",
    }
    return ReplayResult(
        result,
        js_stringify_pretty(receipt).encode("utf-8"),
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--candidate", action="store_true")
    args = parser.parse_args()
    try:
        inputs = load_replay_inputs(args.manifest)
        out = _safe(args.out.absolute())
        _safe(out.resolve())
        if out.exists():
            raise ValueError("output must be new and separate from source evidence")
        result = replay_delivery(inputs, candidate=args.candidate)
        out.mkdir(parents=True)
        for name, data in (*result.artifacts, ("replay-receipt.json", result.receipt)):
            with (out / name).open("xb") as stream:
                stream.write(data)
        return 0
    except (
        OSError,
        ValueError,
        TypeError,
        KeyError,
        subprocess.SubprocessError,
    ) as exc:
        print(f"delivery replay refused: {exc}", file=sys.stderr)
        return 2

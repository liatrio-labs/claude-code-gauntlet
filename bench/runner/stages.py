"""Trace attribution from discovery claims evaluated by the delivered scorer."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal, TypedDict, get_args

from bench.runner.decode import decode
from bench.runner.epochs import file_hash, read_json
from bench.runner.observations import (
    Coverage,
    Decision,
    Dimension,
    FrozenJson,
    LossStage,
    ReasonCode,
    ScoreRef,
    Stage,
    TraceCollection,
    artifact_allowed,
)
from bench.runner.observations import freeze_json as freeze
from bench.runner.observations import thaw_json as thaw

TRACE_VERSION = "1"


class FindingProjectionWire(TypedDict, total=False):
    id: str
    dimension: str
    agent: str
    file: str
    title: str
    description: str
    body: str
    evidence: str
    severity: str
    line_start: int
    line: int
    end_line: int
    line_end: int
    confidence: int | float
    suggestion: str
    rule_source: str
    claude_md_rule: str
    spec_text: str
    suggested_fix_code: str
    consolidation_key: str
    consolidation_primary: bool
    report_tag: str
    report_destination: str
    origin: str
    validation: str
    challenge: str
    trace_id: str
    discovery_agent: str
    discovery_dimension: Dimension


@dataclass(frozen=True, slots=True)
class FindingVersion:
    version_id: str
    trace_id: str
    model_finding_id: str | None
    discovery_agent: str
    discovery_dimension: Dimension
    finding: FrozenJson
    base_version_id: str | None
    set_fields: FrozenJson
    unset_fields: tuple[str, ...]

    def projection(self):
        return thaw(self.finding)


@dataclass(frozen=True, slots=True)
class StageDecision:
    trace_id: str
    decision: Decision
    reason_code: ReasonCode | None
    reason: str | None
    related_trace_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class StageSnapshot:
    sequence: int
    stage: Stage
    finding_version_ids: tuple[str, ...]
    decisions: tuple[StageDecision, ...]
    executed: bool
    replayed: bool
    coverage: Coverage
    dispatched_dimensions: tuple[Dimension, ...]
    degraded_dimensions: tuple[Dimension, ...]
    cap: int | None
    cap_hit: bool


@dataclass(frozen=True, slots=True)
class MeasurementTrace:
    schema_version: Literal[1]
    trace_version: str
    run_nonce: str
    versions: tuple[FindingVersion, ...]
    snapshots: tuple[StageSnapshot, ...]
    resolved_models: tuple[tuple[str, str], ...]
    complete: bool


@dataclass(frozen=True, slots=True)
class Loss:
    stage: LossStage = "unknown"
    physical_stage: Stage | None = None
    cause_stage: Stage | None = None
    reason_code: ReasonCode = "unknown"
    reason: str = "attribution unavailable"
    trace_ids: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()
    status: Literal["established", "unavailable", "ambiguous"] = "unavailable"


@dataclass(frozen=True, slots=True)
class StageBugObservation:
    score_ref: ScoreRef
    attempt_id: str
    pr_url: str
    bug_id: str
    stage: Stage
    survives: bool | None
    matched_version_ids: tuple[str, ...]
    loss: Loss | None
    coverage: Coverage
    cap_hit: bool = False


@dataclass(frozen=True, slots=True)
class FunnelReport:
    denominator: int
    delivered_hits: int
    discovered_hits: int
    losses: tuple[tuple[LossStage, int], ...]
    unavailable_count: int
    matcher_disagreement_count: int
    survivor_counts: tuple[tuple[Stage, int], ...]
    cap_hit_counts: tuple[tuple[Stage, int], ...]
    unjudged_counts: tuple[tuple[Stage, int], ...]
    coverage: Coverage


def _require(condition, reason):
    if not condition:
        raise ValueError("invalid measurement trace: " + reason)


def load_trace(trace_path: Path, *, expected_sha256: str) -> MeasurementTrace:
    _require(
        artifact_allowed(Path(trace_path)) and not Path(trace_path).is_symlink(),
        "denied artifact",
    )
    _require(file_hash(trace_path) == expected_sha256, "byte hash mismatch")
    raw = read_json(trace_path)
    _require(isinstance(raw, dict), "object envelope")
    _require(
        raw.get("schema_version") == 1 and raw.get("trace_version") == TRACE_VERSION,
        "unsupported version",
    )
    _require(isinstance(raw.get("run_nonce"), str) and raw["run_nonce"], "nonce")
    _require(type(raw.get("complete")) is bool, "completion flag")
    versions: list[FindingVersion] = []
    by_id: dict[str, FindingVersion] = {}
    identities: dict[str, tuple[str, Dimension]] = {}
    for row in raw["versions"]:
        vid, tid, base = row["version_id"], row["trace_id"], row["base_version_id"]
        _require(isinstance(vid, str) and vid not in by_id, "duplicate version")
        _require(
            isinstance(tid, str) and tid.startswith(raw["run_nonce"] + ":"),
            "occurrence identity",
        )
        _require(row["discovery_dimension"] in get_args(Dimension), "dimension")
        _require(isinstance(row["discovery_agent"], str), "agent")
        _require(
            isinstance(row["set_fields"], dict)
            and isinstance(row["unset_fields"], list),
            "patch",
        )
        if base is None:
            _require(
                isinstance(row["finding"], dict)
                and not row["set_fields"]
                and not row["unset_fields"],
                "initial finding",
            )
            finding = dict(row["finding"])
        else:
            _require(
                base in by_id
                and by_id[base].trace_id == tid
                and row["finding"] is None,
                "backwards patch reference",
            )
            finding = by_id[base].projection()
            _require(
                len(set(row["unset_fields"])) == len(row["unset_fields"]),
                "duplicate unset",
            )
            for key in row["unset_fields"]:
                _require(
                    key in finding and key not in row["set_fields"], "unset reference"
                )
                del finding[key]
            finding.update(row["set_fields"])
        identity = (row["discovery_agent"], row["discovery_dimension"])
        _require(
            tid not in identities or identities[tid] == identity, "changed provenance"
        )
        identities[tid] = identity
        _require(
            finding.get("trace_id") == tid
            and finding.get("discovery_agent") == identity[0]
            and finding.get("discovery_dimension") == identity[1],
            "finding provenance",
        )
        _require(
            row["model_finding_id"]
            == (finding.get("id") if isinstance(finding.get("id"), str) else None),
            "model id",
        )
        for name in FindingProjectionWire.__annotations__:
            if name not in finding or finding[name] is None:
                continue
            value = finding[name]
            if name in ("line_start", "line", "end_line", "line_end", "confidence"):
                _require(
                    type(value) in (int, float) and math.isfinite(value),
                    "numeric projection",
                )
            elif name == "consolidation_primary":
                _require(type(value) is bool, "consolidation primary")
            else:
                _require(isinstance(value, str), "text projection")
        version = FindingVersion(
            vid,
            tid,
            row["model_finding_id"],
            *identity,
            freeze(finding),
            base,
            freeze(row["set_fields"]),
            tuple(row["unset_fields"]),
        )
        versions.append(version)
        by_id[vid] = version
    snapshots: list[StageSnapshot] = []
    for row in raw["snapshots"]:
        snapshot = decode(StageSnapshot, row)
        _require(snapshot.sequence == len(snapshots), "stage sequence")
        ids = snapshot.finding_version_ids
        _require(
            len(set(ids)) == len(ids) and all(v in by_id for v in ids),
            "snapshot references",
        )
        _require(
            len({by_id[v].trace_id for v in ids}) == len(ids), "duplicate occurrence"
        )
        _require(snapshot.cap is None or snapshot.cap >= 0, "cap")
        for decision in snapshot.decisions:
            _require(
                decision.trace_id in identities
                and all(t in identities for t in decision.related_trace_ids),
                "decision reference",
            )
        snapshots.append(snapshot)
    models = raw["resolved_models"]
    _require(
        isinstance(models, dict)
        and all(isinstance(k, str) and isinstance(v, str) for k, v in models.items()),
        "resolved models",
    )
    if raw["complete"]:
        _require(
            tuple(s.stage for s in snapshots) == get_args(Stage),
            "complete physical stage order",
        )
    return MeasurementTrace(
        1,
        TRACE_VERSION,
        raw["run_nonce"],
        tuple(versions),
        tuple(snapshots),
        tuple(sorted(models.items())),
        raw["complete"],
    )


def render_claim(finding):
    # No anchor or fix policy is invented for an intermediate claim.
    from gauntlet.delivery import compose

    f = dict(finding)
    f["body"] = f.get("body", f.get("description", ""))
    return compose.compose_inline_body(
        compose.render_group_sections(f, ()), platform="github", surface="inline"
    ).body


def discovery_inputs(traces, errors):
    return {
        url: {
            "discover:" + vid: [
                {
                    "text": render_claim(versions[vid].projection()),
                    "path": None,
                    "line": None,
                    "source": "discovery",
                }
            ]
            for vid in snapshot.finding_version_ids
            if versions[vid].trace_id
            not in {
                versions[v].trace_id
                for s in trace.snapshots
                if s.stage == "deliver"
                for v in s.finding_version_ids
            }
        }
        for url, (_, trace) in traces.items()
        if url not in errors
        for versions in [{v.version_id: v for v in trace.versions}]
        for snapshot in trace.snapshots
        if snapshot.stage == "discover" and snapshot.executed
    }


def capture_score(traces, bugs, request, evaluations, raw_groups, inputs, errors):
    from bench.runner.observations import (
        build_observations,
        candidate_records,
        dedup_groups,
    )

    sources = {c.candidate_id: set(c.source_trace_ids) for c in request.candidates}
    matched_sources = {
        b.bug_id: set().union(*(sources[m.candidate_id] for m in b.matches))
        for b in bugs
    }
    results = {}
    for url, (_, trace) in traces.items():
        delivered = {
            tid
            for c in request.candidates
            if c.pr_url == url
            for tid in c.source_trace_ids
        }
        for version in trace.versions:
            if version.trace_id in delivered:
                results[url, version.version_id] = {
                    b.bug_id: version.trace_id in matched_sources[b.bug_id]
                    for b in bugs
                    if b.pr_url == url
                }
    for url, tools in inputs.items():
        for tool, entries in tools.items():
            try:
                candidates = candidate_records(
                    {url: {tool: entries}}, dict(request.selected_attempts), tool
                )
                pseudo = replace(
                    request, requested_pr_urls=(url,), candidates=candidates, tool=tool
                )
                rows = build_observations(
                    pseudo, evaluations, dedup_groups(raw_groups, candidates, tool)
                )
                results[url, tool.removeprefix("discover:")] = {
                    b.bug_id: b.outcome == "matched" for b in rows
                }
            except (ValueError, KeyError, TypeError):
                continue
    rows = []
    for bug in bugs:
        trace = (
            traces[bug.pr_url][1]
            if bug.pr_url in traces and bug.pr_url not in errors
            else None
        )
        snapshots = {s.stage: s for s in trace.snapshots} if trace else {}
        versions = {v.version_id: v for v in trace.versions} if trace else {}
        discovery = snapshots.get("discover")
        judged = bool(
            trace
            and trace.complete
            and discovery
            and discovery.executed
            and discovery.coverage == "complete"
        )
        vids = discovery.finding_version_ids if discovery else ()
        judged = judged and all((bug.pr_url, v) in results for v in vids)
        matched = tuple(
            v for v in vids if results.get((bug.pr_url, v), {}).get(bug.bug_id)
        )
        tids = {versions[v].trace_id for v in matched}
        loss = None
        if bug.outcome != "matched":
            loss = Loss()
            if judged and not matched:
                loss = Loss(
                    "not_found",
                    "discover",
                    reason_code="not_found",
                    reason="no discovery claim matched",
                    status="established",
                )
            elif judged:
                previous = set(tids)
                for snapshot in trace.snapshots[1:]:
                    if not snapshot.executed or snapshot.coverage == "unavailable":
                        break
                    surviving = {
                        versions[v].trace_id for v in snapshot.finding_version_ids
                    } & tids
                    if previous and not surviving:
                        decisions = [
                            d
                            for d in snapshot.decisions
                            if d.trace_id in previous and d.decision != "kept"
                        ]
                        if decisions:
                            d = decisions[0]
                            physical = snapshot.stage
                            stage = {
                                "validation_skipped": "validate",
                                "verify_unproven": "verify",
                                "challenge_failed": "challenge",
                                "challenge_cap": "cap",
                                "delivery_cap": "cap",
                                "injection": "filter",
                            }.get(
                                d.reason_code,
                                "unknown"
                                if physical == "holdback"
                                else "delivery"
                                if physical in ("select", "deliver")
                                else physical,
                            )
                            loss = Loss(
                                stage,
                                physical,
                                None,
                                d.reason_code or "unknown",
                                d.reason or d.reason_code or "unknown",
                                tuple(sorted(previous)),
                                (f"snapshot:{snapshot.sequence}",),
                                "established",
                            )
                        break
                    previous = surviving
        for stage in get_args(Stage):
            snapshot = snapshots.get(stage)
            surviving = tuple(
                v
                for v in (snapshot.finding_version_ids if snapshot else ())
                if versions[v].trace_id in tids
            )
            known = (
                judged
                and snapshot is not None
                and snapshot.executed
                and snapshot.coverage == "complete"
            )
            bit = bool(surviving) if known else True if surviving else None
            if stage == "deliver":
                bit = bug.outcome == "matched"
            rows.append(
                StageBugObservation(
                    bug.score_ref,
                    bug.attempt_id,
                    bug.pr_url,
                    bug.bug_id,
                    stage,
                    bit,
                    matched if stage == "discover" else surviving,
                    loss if stage == "deliver" else None,
                    snapshot.coverage if snapshot else "unavailable",
                    snapshot.cap_hit if snapshot else False,
                )
            )
    return tuple(rows)


def loss_funnel(observations, *, population) -> FunnelReport:
    members = set(population)
    cells: dict[tuple[ScoreRef, str, str, str], dict[Stage, StageBugObservation]] = {}
    for row in observations:
        if row.bug_id not in members:
            continue
        key = (row.score_ref, row.attempt_id, row.pr_url, row.bug_id)
        cell = cells.setdefault(key, {})
        if row.stage in cell:
            raise ValueError("duplicate stage observation")
        cell[row.stage] = row
    losses = dict.fromkeys(get_args(LossStage), 0)
    survivors = dict.fromkeys(get_args(Stage), 0)
    unjudged = dict.fromkeys(get_args(Stage), 0)
    caps = dict.fromkeys(get_args(Stage), 0)
    capped_attempts = {
        (row.score_ref, row.attempt_id, row.pr_url, row.stage)
        for row in observations
        if row.bug_id in members and row.cap_hit
    }
    for _, _, _, stage in capped_attempts:
        caps[stage] += 1
    delivered = discovered = unavailable = disagreements = 0
    for rows in cells.values():
        for stage in get_args(Stage):
            row = rows.get(stage)
            if row is None or row.survives is None:
                unjudged[stage] += 1
            elif row.survives:
                survivors[stage] += 1
        final = rows.get("deliver")
        discovered += bool(rows.get("discover") and rows["discover"].survives)
        if final and final.survives:
            delivered += 1
            disagreements += any(
                r.survives is False for s, r in rows.items() if s != "deliver"
            )
        elif final and final.loss and final.loss.status == "established":
            losses[final.loss.stage] += 1
        else:
            unavailable += 1
    denominator = len(members) * max(1, len({key[0] for key in cells}))
    missing = denominator - len(cells)
    unavailable += missing
    for stage in unjudged:
        unjudged[stage] += missing
    _require(
        delivered + sum(losses.values()) + unavailable == denominator,
        "funnel partition",
    )
    return FunnelReport(
        denominator,
        delivered,
        discovered,
        tuple(losses.items()),
        unavailable,
        disagreements,
        tuple(survivors.items()),
        tuple(caps.items()),
        tuple(unjudged.items()),
        "unavailable"
        if denominator and unavailable == denominator and not delivered
        else "complete"
        if not unavailable
        and not any(unjudged.values())
        and all(row.coverage == "complete" for row in observations)
        else "partial",
    )


def credited_dimensions(bug, candidates):
    matched = {m.candidate_id for m in bug.matches}
    return tuple(
        sorted(
            {
                d
                for c in candidates
                if c.candidate_id in matched and c.provenance_status == "complete"
                for d in c.discovery_dimensions
            }
        )
    )


def dimension_metrics(candidates, bugs, stage_rows, profile, traces):
    from bench.runner.epochs import in_profile

    population = [b for b in bugs if in_profile(b.category, profile)]
    versions = {
        (url, v.version_id): v
        for url, (_, trace) in traces.items()
        for v in trace.versions
    }
    discovery = {
        r.bug_id: {
            versions[r.pr_url, v].discovery_dimension for v in r.matched_version_ids
        }
        for r in stage_rows
        if r.stage == "discover" and r.survives
    }
    judged = {
        r.bug_id
        for r in stage_rows
        if r.stage == "discover" and r.survives is not None and r.coverage == "complete"
    }
    full = bool(population) and all(b.bug_id in judged for b in population)
    out = {}
    for dimension in get_args(Dimension):
        entries = [
            c
            for c in candidates
            if dimension in c.discovery_dimensions and c.provenance_status == "complete"
        ]
        if not entries and not any(
            v.discovery_dimension == dimension for v in versions.values()
        ):
            continue
        delivered = sum(
            dimension in credited_dimensions(b, candidates) for b in population
        )
        discovered = sum(dimension in discovery.get(b.bug_id, ()) for b in population)
        out[dimension] = {
            "dimension": dimension,
            "candidate_count": len(entries),
            "delivered_bug_hits": delivered,
            "discovery_bug_hits": discovered,
            "denominator": len(population),
            "delivered_recall": delivered / len(population)
            if population and all(c.provenance_status == "complete" for c in candidates)
            else None,
            "discovery_recall": discovered / len(population) if full else None,
        }
    return out


def bind_delivery_trace(trace, candidates, *, omissions=()):
    """Overlay pure, payload-proven posting omissions on producer selection."""
    delivered = {tid for c in candidates for tid in c.source_trace_ids}
    versions = {v.version_id: v for v in trace.versions}
    omitted = {tid: o for o in omissions for tid in o.trace_ids}
    snapshots = []
    for snapshot in trace.snapshots:
        if snapshot.stage == "deliver":
            ids = snapshot.finding_version_ids
            removed = [
                versions[v].trace_id
                for v in ids
                if versions[v].trace_id not in delivered
            ]
            decisions = tuple(
                StageDecision(
                    tid,
                    "not_selected",
                    omitted[tid].reason_code if tid in omitted else "post_omission",
                    omitted[tid].reason
                    if tid in omitted
                    else "selected source omitted from scored inline comments",
                    (),
                )
                for tid in removed
            )
            snapshot = replace(
                snapshot,
                finding_version_ids=tuple(
                    v for v in ids if versions[v].trace_id in delivered
                ),
                decisions=snapshot.decisions + decisions,
            )
        snapshots.append(snapshot)
    return replace(trace, snapshots=tuple(snapshots))


class CollectedTraceWire(TypedDict):
    path: str
    sha256: str
    run_nonce: str
    workflow_record: str
    workflow_sha256: str


def _collect_trace(pr_dir: Path) -> CollectedTraceWire | None:
    """Materialize only a current Workflow return entry, verifying its original bytes."""
    from gauntlet import tasks

    pr_dir = Path(pr_dir)
    delivered = list(pr_dir.glob("code-gauntlet-post-review-*.json"))
    if not delivered:
        return None
    if len(delivered) != 1 or delivered[0].is_symlink():
        raise ValueError("delivered post-review artifact is ambiguous or a symlink")
    payload = read_json(delivered[0])
    findings = payload.get("findings", []) if isinstance(payload, dict) else payload
    nonces = {f["trace_id"].split(":", 1)[0] for f in findings if f.get("trace_id")}
    if isinstance(payload, dict) and payload.get("run_nonce"):
        nonces.add(payload["run_nonce"])
    if len(nonces) != 1:
        return None
    delivered_nonce = nonces.pop()
    current = []
    for record in sorted((pr_dir / "workflows").glob("wf_*.json")):
        if record.is_symlink():
            raise ValueError("workflow trace record is a symlink")
        terminal, _, _ = tasks.find_terminal(tasks.read_task(str(record)))
        if (
            isinstance(terminal, dict)
            and terminal.get("ok") is True
            and (terminal.get("persistReturn") or {}).get("nonce") == delivered_nonce
        ):
            current.append((record, terminal))
    if len(current) > 1:
        raise ValueError("multiple successful Workflow records for delivered nonce")
    found: list[CollectedTraceWire] = []
    for record, terminal in current:
        measurement = terminal.get("measurement") or {}
        if measurement.get("status") != "available":
            raise ValueError(
                measurement.get("reason") or "Workflow measurement unavailable"
            )
        channel = terminal.get("persistReturn") or {}
        entries = [
            e
            for e in channel.get("entries", [])
            if e.get("path") == measurement.get("path")
        ]
        if len(entries) != 1:
            raise ValueError("measurement return entry is missing or ambiguous")
        entry = entries[0]
        name = Path(entry["path"]).name
        import re

        if not re.fullmatch(r"code-gauntlet-measurement-[A-Za-z0-9]+\.json", name):
            raise ValueError("invalid trace artifact name")
        data = entry["text"].encode("utf-8")
        import hashlib

        actual = hashlib.sha256(data).hexdigest()
        if actual != measurement.get("sha256") or actual != entry.get("sha256"):
            raise ValueError("measurement return byte hash mismatch")
        path = pr_dir / name
        if path.is_symlink():
            raise ValueError("trace artifact is a symlink")
        if path.exists():
            if file_hash(path) != actual:
                raise ValueError("collected trace differs from return entry")
        else:
            with path.open("xb") as stream:
                stream.write(data)
        trace = load_trace(path, expected_sha256=actual)
        if trace.run_nonce != channel.get("nonce"):
            raise ValueError("measurement attempt nonce mismatch")
        found.append(
            {
                "path": name,
                "sha256": actual,
                "run_nonce": trace.run_nonce,
                "workflow_record": str(record.relative_to(pr_dir)),
                "workflow_sha256": file_hash(record),
            }
        )
    if found:
        from bench.runner.observations import exclusive_json

        receipt = pr_dir / "measurement-artifact.json"
        if receipt.exists():
            if read_json(receipt) != found[0]:
                raise ValueError("measurement attempt binding changed")
        else:
            exclusive_json(receipt, found[0])
        return found[0]
    return None


def collect_trace(pr_dir: Path) -> TraceCollection:
    from dataclasses import asdict

    from bench.runner.observations import exclusive_json

    try:
        found = _collect_trace(pr_dir)
        receipt = (
            TraceCollection("available", None)
            if found
            else TraceCollection(
                "unavailable", "no current Workflow measurement return"
            )
        )
    except (OSError, ValueError, KeyError, TypeError) as exc:
        receipt = TraceCollection("unavailable", str(exc))
    path = Path(pr_dir) / "measurement-collection.json"
    if not path.exists():
        exclusive_json(path, asdict(receipt))
    elif read_json(path) != asdict(receipt):
        raise ValueError("trace collection receipt changed")
    return receipt


def read_rows(path):
    return tuple(
        decode(StageBugObservation, json.loads(line))
        for line in (path / "stages.jsonl").read_text(encoding="utf-8").splitlines()
    )


def packet_traces(path):
    from bench.runner.epochs import confined

    return {
        url: (
            confined(path, b["trace_path"]),
            load_trace(
                confined(path, b["trace_path"]), expected_sha256=b["trace_sha256"]
            ),
        )
        for url, b in read_json(path / "stage_trace_bindings.json").items()
    }


def validate_packet_evidence(path, packet, epoch):
    from bench.runner.epochs import confined
    from bench.runner.observations import ScoreRequest, wire_record
    from bench.runner.provenance import bind_candidates, trace_delivery

    bindings = read_json(path / "stage_trace_bindings.json")
    traces = packet_traces(path)
    attempts = dict(packet.manifest.selected_attempts)
    errors = read_json(path / "provenance_errors.json")
    for url, binding in bindings.items():
        if binding["attempt_id"] != attempts.get(url):
            raise ValueError("trace attempt binding mismatch")
        delivery = binding["delivery"]
        if delivery:
            bound_trace, mapped = trace_delivery(
                traces[url][1],
                read_json(confined(path, delivery["selected_path"])),
                read_json(confined(path, delivery["payload_path"])),
                confined(path, delivery["diff_path"]).read_text(encoding="utf-8"),
            )
            group = tuple(c for c in packet.candidates if c.pr_url == url)
            if group != bind_candidates(group, mapped):
                raise ValueError("candidate source attribution mismatch")
            traces[url] = traces[url][0], bound_trace
    for c in packet.candidates:
        if c.provenance_status == "complete" and (
            c.pr_url not in bindings or not bindings[c.pr_url]["delivery"]
        ):
            raise ValueError("missing candidate source binding")
        if c.provenance_status != "complete" and (
            c.source_trace_ids or c.discovery_dimensions != ("unknown",)
        ):
            raise ValueError("unknown candidate source dimension")
    inputs = discovery_inputs(traces, errors) if packet.manifest.attribution else {}
    attribution_dir = (
        path
        / "work/attribution/results"
        / packet.manifest.evaluator.judge_pin.replace("/", "_")
    )
    evaluated = read_json(attribution_dir / "candidates.json") if inputs else {}
    for url, tools in inputs.items():
        if any(
            evaluated.get(url, {}).get(tool) != entries
            for tool, entries in tools.items()
        ):
            raise ValueError("discovery receipt join mismatch")
    request = ScoreRequest(
        packet.manifest.score_ref,
        epoch,
        path,
        packet.manifest.requested_pr_urls,
        tuple(sorted(packet.manifest.selected_attempts.items())),
        packet.candidates,
        "deep-review",
        packet.manifest.evaluator,
    )
    rebuilt = (
        capture_score(
            traces,
            packet.observations,
            request,
            read_json(attribution_dir / "evaluations.json") if inputs else {},
            read_json(attribution_dir / "dedup_groups.json") if inputs else {},
            inputs,
            errors,
        )
        if packet.manifest.attribution
        else ()
    )
    if read_rows(path) != rebuilt:
        raise ValueError("stage observation receipt join mismatch")
    losses = {r.bug_id: wire_record(r.loss) for r in rebuilt if r.stage == "deliver"}
    if any(b.loss != losses.get(b.bug_id) for b in packet.observations):
        raise ValueError("terminal loss join mismatch")

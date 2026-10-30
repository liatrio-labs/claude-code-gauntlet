"""Trace attribution from discovery claims evaluated by the delivered scorer."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Literal, TypedDict, get_args

from bench.runner.contracts import ScoreRef, artifact_allowed, exclusive_json
from bench.runner.decode import decode
from bench.runner.epochs import confined, file_hash, in_profile, read_json
from bench.runner.observations import (
    OPTIONAL_STAGES,
    REQUIRED_STAGES,
    Coverage,
    Decision,
    Dimension,
    Loss,
    LossStage,
    ReasonCode,
    ScoreRequest,
    Stage,
    TraceCollection,
    build_observations,
    candidate_records,
    dedup_groups,
)


@dataclass(frozen=True, slots=True)
class FindingVersion:
    version_id: str
    trace_id: str
    discovery_agent: str
    discovery_dimension: Dimension
    finding: dict[str, Any]

    def projection(self):
        return dict(self.finding)


class DecisionDetail(TypedDict, total=False):
    kind: str
    policy_subject: str
    predicate: str
    status: str
    predicates: list[dict[str, str]]
    original_producer: str
    surface: str


@dataclass(frozen=True, slots=True)
class StageDecision:
    trace_id: str
    decision: Decision
    reason_code: ReasonCode
    reason: str | None
    related_trace_ids: tuple[str, ...]
    detail: DecisionDetail | None = None


@dataclass(frozen=True, slots=True)
class StageSnapshot:
    sequence: int
    stage: Stage
    finding_version_ids: tuple[str, ...]
    decisions: tuple[StageDecision, ...]
    executed: bool
    replayed: bool
    coverage: Coverage
    cap: int | None
    cap_hit: bool


@dataclass(frozen=True, slots=True)
class MeasurementTrace:
    schema_version: Literal[1]
    run_nonce: str
    versions: tuple[FindingVersion, ...]
    snapshots: tuple[StageSnapshot, ...]
    complete: bool


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
    trace: MeasurementTrace = decode(MeasurementTrace, read_json(trace_path))
    by_id: dict[str, FindingVersion] = {}
    identities: dict[str, object] = {}
    for version in trace.versions:
        _require(version.version_id not in by_id, "duplicate version")
        _require(
            version.trace_id.startswith(trace.run_nonce + ":"), "occurrence identity"
        )
        identity = (version.discovery_agent, version.discovery_dimension)
        _require(
            identities.setdefault(version.trace_id, identity) == identity,
            "changed provenance",
        )
        _require(
            all(
                version.finding.get(k) == v
                for k, v in zip(
                    ("trace_id", "discovery_agent", "discovery_dimension"),
                    (version.trace_id, *identity),
                    strict=True,
                )
            ),
            "finding provenance",
        )
        by_id[version.version_id] = version
    for sequence, snapshot in enumerate(trace.snapshots):
        _require(snapshot.sequence == sequence, "stage sequence")
        ids = snapshot.finding_version_ids
        _require(
            len(set(ids)) == len(ids) and all(v in by_id for v in ids),
            "snapshot references",
        )
        _require(
            len({by_id[v].trace_id for v in ids}) == len(ids), "duplicate occurrence"
        )
        for decision in snapshot.decisions:
            _require(
                decision.trace_id in identities
                and all(t in identities for t in decision.related_trace_ids),
                "decision reference",
            )
            _require(
                decision.reason_code != "folded"
                or (
                    decision.decision == "coalesced"
                    and bool(decision.related_trace_ids)
                ),
                "fold requires constituent identities and coalescence",
            )
    actual = tuple(s.stage for s in trace.snapshots)
    reducers = set(actual) & {"dispose", "holdback"}
    _require(len(reducers) <= 1, "duplicate final reducers")
    _require(
        not (trace.complete or "select" in actual) or len(reducers) == 1,
        "missing final reducer",
    )
    required = tuple(s for s in actual if s not in OPTIONAL_STAGES)
    _require(required == REQUIRED_STAGES[: len(required)], "physical stage order")
    order = get_args(Stage)
    _require(
        len(set(actual)) == len(actual)
        and list(actual) == sorted(actual, key=order.index),
        "physical stage order",
    )
    for stage in set(actual) & OPTIONAL_STAGES:
        _require(
            all(
                s in actual
                for s in order[: order.index(stage)]
                if s not in OPTIONAL_STAGES
            ),
            "optional stage before required prefix",
        )
    _require(not trace.complete or required == REQUIRED_STAGES, "complete stages")
    return trace


def render_claim(finding):
    # No anchor or fix policy is invented for an intermediate claim.
    from gauntlet.delivery import compose

    f = dict(finding)
    f["body"] = f.get("body", f.get("description", ""))
    return compose.compose_inline_body(
        compose.render_group_sections(f, ()), platform="github", surface="inline"
    ).body


REASON_LOSS_STAGES: dict[ReasonCode, LossStage] = {
    "validation_skipped": "validate",
    "verify_unproven": "verify",
    "challenge_failed": "challenge",
    "challenge_cap": "cap",
    "delivery_cap": "cap",
    "injection": "filter",
    "cite_unmatched": "cite",
    "cite_unavailable": "cite",
    "refuted": "dispose",
    "out_of_scope": "dispose",
    "scope_contradicted": "dispose",
    "report_only": "delivery",
    "body_routed": "delivery",
    "held_back": "delivery",
    "demoted": "delivery",
}


# A holdback with an unlisted reason, or a version gone before merge, names no stage.
PHYSICAL_LOSS_STAGES: dict[Stage, LossStage] = {
    "merge": "merge",
    "verify": "verify",
    "validate": "validate",
    "filter": "filter",
    "challenge": "challenge",
    "cite": "cite",
    "dispose": "dispose",
    "select": "delivery",
    "deliver": "delivery",
}


def _loss_stage(reason: ReasonCode, physical: Stage) -> LossStage:
    return REASON_LOSS_STAGES.get(reason) or PHYSICAL_LOSS_STAGES.get(
        physical, "unknown"
    )


def discovery_inputs(traces, errors):
    inputs = {}
    for url, (_, trace) in traces.items():
        if url in errors:
            continue
        versions = {v.version_id: v for v in trace.versions}
        delivered = {
            versions[vid].trace_id
            for snapshot in trace.snapshots
            if snapshot.stage == "deliver"
            for vid in snapshot.finding_version_ids
        }
        tools = {}
        for snapshot in trace.snapshots:
            if snapshot.stage != "discover" or not snapshot.executed:
                continue
            for vid in snapshot.finding_version_ids:
                if versions[vid].trace_id in delivered:
                    continue
                tools["discover:" + vid] = [
                    {
                        "text": render_claim(versions[vid].projection()),
                        "path": None,
                        "line": None,
                        "source": "discovery",
                    }
                ]
        if tools:
            inputs[url] = tools
    return inputs


def _version_results(traces, bugs, request, evaluations, raw_groups, inputs):
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
                observations = build_observations(
                    pseudo, evaluations, dedup_groups(raw_groups, candidates, tool)
                )
                results[url, tool.removeprefix("discover:")] = {
                    b.bug_id: b.outcome == "matched" for b in observations
                }
            except (ValueError, KeyError, TypeError):
                continue
    return results


def _trace_loss(bug, trace, versions, trace_ids, judged):
    if bug.outcome == "matched":
        return None
    if not judged:
        return Loss(stage="unknown", status="unavailable")
    if not trace_ids:
        return Loss(
            stage="not_found",
            physical_stage="discover",
            reason_code="not_found",
            reason="no discovery claim matched",
            status="established",
        )
    trace_ids = set(trace_ids)
    previous = set(trace_ids)
    for snapshot in trace.snapshots[1:]:
        if not snapshot.executed or snapshot.coverage == "unavailable":
            break
        for decision in snapshot.decisions:
            if decision.decision == "coalesced" and decision.trace_id in previous:
                trace_ids.update(decision.related_trace_ids)
        surviving = {
            versions[v].trace_id for v in snapshot.finding_version_ids
        } & trace_ids
        if previous and not surviving:
            decisions = [
                d
                for d in snapshot.decisions
                if d.trace_id in previous and d.decision not in ("kept", "coalesced")
            ]
            if not decisions:
                break
            decision = decisions[0]
            loss_stage = _loss_stage(decision.reason_code, snapshot.stage)
            return Loss(
                stage=loss_stage,
                physical_stage=snapshot.stage,
                reason_code=decision.reason_code,
                reason=decision.reason or decision.reason_code,
                trace_ids=tuple(sorted(previous)),
                evidence_refs=(f"snapshot:{snapshot.sequence}",),
                status="ambiguous" if loss_stage == "unknown" else "established",
            )
        previous = surviving
    return Loss(stage="unknown", status="unavailable")


def _bug_stage_rows(bug, trace, results):
    snapshots = {s.stage: s for s in trace.snapshots} if trace else {}
    versions = {v.version_id: v for v in trace.versions} if trace else {}
    discovery = snapshots.get("discover")
    discovery_ids = discovery.finding_version_ids if discovery else ()
    judged = bool(
        trace
        and trace.complete
        and discovery
        and discovery.executed
        and discovery.coverage == "complete"
        and all((bug.pr_url, v) in results for v in discovery_ids)
    )
    matched = tuple(
        v for v in discovery_ids if results.get((bug.pr_url, v), {}).get(bug.bug_id)
    )
    trace_ids = {versions[v].trace_id for v in matched}
    loss = _trace_loss(bug, trace, versions, trace_ids, judged)
    rows = []
    for stage in get_args(Stage):
        if stage in OPTIONAL_STAGES and stage not in snapshots:
            continue
        snapshot = snapshots.get(stage)
        surviving = tuple(
            v
            for v in (snapshot.finding_version_ids if snapshot else ())
            if versions[v].trace_id in trace_ids
        )
        known = (
            judged
            and snapshot is not None
            and snapshot.executed
            and snapshot.coverage == "complete"
        )
        if stage == "deliver":
            bit = bug.outcome == "matched"
        elif known or surviving:
            bit = bool(surviving)
        else:
            bit = None
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
    return rows


def capture_score(traces, bugs, request, evaluations, raw_groups, inputs, errors):
    results = _version_results(traces, bugs, request, evaluations, raw_groups, inputs)
    rows = []
    for bug in bugs:
        trace = None
        if bug.pr_url in traces and bug.pr_url not in errors:
            trace = traces[bug.pr_url][1]
        rows.extend(_bug_stage_rows(bug, trace, results))
    return tuple(rows)


def loss_funnel(observations, *, population, score_refs=None) -> FunnelReport:
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
            if stage in OPTIONAL_STAGES and row is None:
                continue
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
    denominator = len(members) * (
        len(score_refs)
        if score_refs is not None
        else max(1, len({key[0] for key in cells}))
    )
    missing = denominator - len(cells)
    unavailable += missing
    for stage in unjudged:
        if stage not in OPTIONAL_STAGES:
            unjudged[stage] += missing
    _require(
        delivered + sum(losses.values()) + unavailable == denominator,
        "funnel partition",
    )
    coverage: Coverage = (
        "complete"
        if not unavailable
        and not any(unjudged.values())
        and all(row.coverage == "complete" for row in observations)
        else "partial"
    )
    if denominator and unavailable == denominator and not delivered:
        coverage = "unavailable"
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
        coverage,
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
    _require(
        sum(s.stage in ("dispose", "holdback") for s in trace.snapshots) == 1,
        "final reducer",
    )
    delivered = {tid for c in candidates for tid in c.source_trace_ids}
    versions = {v.version_id: v for v in trace.versions}
    omitted = {tid: o for o in omissions for tid in o.trace_ids}
    prior = {
        d.trace_id: d
        for s in trace.snapshots
        for d in s.decisions
        if d.decision not in ("kept", "coalesced")
    }
    coalesced = tuple(
        d for s in trace.snapshots for d in s.decisions if d.decision == "coalesced"
    )
    folded = {d.trace_id for d in coalesced}
    selection = trace.snapshots[-1]
    _require(selection.stage == "select", "delivery requires selection")
    delivery = replace(
        selection,
        sequence=selection.sequence + 1,
        stage="deliver",
        finding_version_ids=tuple(
            v
            for v in selection.finding_version_ids
            if versions[v].trace_id in delivered
        ),
        decisions=coalesced
        + tuple(
            prior.get(tid)
            or StageDecision(
                tid,
                "not_selected",
                omitted[tid].reason_code if tid in omitted else "post_omission",
                omitted[tid].reason
                if tid in omitted
                else "selected source omitted from scored inline comments",
                (),
            )
            for v in selection.finding_version_ids
            for tid in [versions[v].trace_id]
            if tid not in delivered and tid not in folded
        ),
        cap=None,
        cap_hit=False,
    )
    return replace(trace, snapshots=(*trace.snapshots, delivery))


class CollectedTraceWire(TypedDict):
    path: str
    sha256: str
    run_nonce: str
    workflow_record: str
    workflow_sha256: str


def _collect_trace(pr_dir: Path) -> CollectedTraceWire | None:
    import hashlib

    from gauntlet import tasks

    pr_dir = Path(pr_dir)
    current = []
    for record in sorted((pr_dir / "workflows").glob("wf_*.json")):
        if record.is_symlink():
            raise ValueError("workflow trace record is a symlink")
        terminal, _, _ = tasks.find_terminal(tasks.read_task(str(record)))
        if isinstance(terminal, dict) and terminal.get("ok") is True:
            current.append((record, terminal))
    if not current:
        return None
    if len(current) != 1:
        raise ValueError("multiple successful Workflow records")
    record, terminal = current[0]
    measurement = terminal.get("measurement") or {}
    if measurement.get("status") != "available":
        raise ValueError(
            measurement.get("reason") or "Workflow measurement unavailable"
        )
    data = decode(str, measurement["text"]).encode("utf-8")
    actual = hashlib.sha256(data).hexdigest()
    name = "code-gauntlet-measurement-trace.json"
    path = pr_dir / name
    if path.is_symlink() or (path.exists() and file_hash(path) != actual):
        raise ValueError("collected trace differs from Workflow return")
    if not path.exists():
        with path.open("xb") as stream:
            stream.write(data)
    trace = load_trace(path, expected_sha256=actual)
    channel = terminal.get("persistReturn") or {}
    if channel and trace.run_nonce != channel.get("nonce"):
        raise ValueError("measurement attempt nonce mismatch")
    binding: CollectedTraceWire = dict(
        path=name,
        sha256=actual,
        run_nonce=trace.run_nonce,
        workflow_record=str(record.relative_to(pr_dir)),
        workflow_sha256=file_hash(record),
    )
    receipt = pr_dir / "measurement-artifact.json"
    if receipt.exists():
        if read_json(receipt) != binding:
            raise ValueError("measurement attempt binding changed")
    else:
        exclusive_json(receipt, binding)
    replay = measurement.get("replay") or {}
    if replay.get("status") == "available":
        replay_data = decode(str, replay["text"]).encode("utf-8")
        replay_path = pr_dir / "code-gauntlet-delivery-replay.json"
        if replay_path.is_symlink() or (
            replay_path.exists()
            and file_hash(replay_path) != hashlib.sha256(replay_data).hexdigest()
        ):
            raise ValueError("collected replay differs from Workflow return")
        if not replay_path.exists():
            with replay_path.open("xb") as stream:
                stream.write(replay_data)
    return binding


def collect_trace(pr_dir: Path) -> TraceCollection:
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
    return {
        url: (
            confined(path, b["trace_path"]),
            load_trace(
                confined(path, b["trace_path"]), expected_sha256=b["trace_sha256"]
            ),
        )
        for url, b in read_json(path / "stage_trace_bindings.json").items()
    }


def validate_packet_evidence(path, packet, epoch, *, surface_view=None):
    from bench.runner.provenance import (
        bind_candidates,
        packet_treatment_root,
        trace_delivery,
    )

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
                treatment_root=packet_treatment_root(
                    path, packet.manifest.score_ref.run_id
                ),
            )
            group = tuple(c for c in packet.candidates if c.pr_url == url)
            if group != bind_candidates(group, mapped):
                raise ValueError("candidate source attribution mismatch")
            if surface_view is not None:
                _, all_mapped = trace_delivery(
                    traces[url][1],
                    read_json(confined(path, delivery["selected_path"])),
                    read_json(confined(path, delivery["payload_path"])),
                    confined(path, delivery["diff_path"]).read_text(encoding="utf-8"),
                    treatment_root=packet_treatment_root(
                        path, packet.manifest.score_ref.run_id
                    ),
                    include_body=True,
                )
                group = tuple(c for c in surface_view.candidates if c.pr_url == url)
                if len({m.issue_id for m in all_mapped}) != len(all_mapped):
                    raise ValueError("duplicate delivered issue identity")
                if group != bind_candidates(group, all_mapped):
                    raise ValueError("surface candidate source attribution mismatch")
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
    if surface_view is not None and any(
        c.provenance_status != "complete"
        or c.pr_url not in bindings
        or not bindings[c.pr_url]["delivery"]
        for c in surface_view.candidates
    ):
        raise ValueError("missing surface candidate source binding")
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
    losses = {(r.pr_url, r.bug_id): r.loss for r in rebuilt if r.stage == "deliver"}
    if any(b.loss != losses.get((b.pr_url, b.bug_id)) for b in packet.observations):
        raise ValueError("terminal loss join mismatch")

"""Validated producer evidence and bounded, opt-in loss-funnel judging."""

from __future__ import annotations

import json
import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal, TypedDict, get_args

from bench.runner.epochs import JsonValue, canonical, digest, file_hash, read_json
from bench.runner.observations import (
    BugObservation,
    Candidate,
    Coverage,
    Decision,
    Dimension,
    FrozenJson,
    LossStage,
    LossWire,
    ReasonCode,
    ScoreRef,
    ScoreRefWire,
    Stage,
    artifact_allowed,
)
from bench.runner.observations import (
    freeze as freeze_record,
)
from bench.runner.observations import freeze_json as freeze
from bench.runner.observations import thaw_json as thaw

TRACE_VERSION = "1"
STAGE_MATCHER_VERSION = "1"


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


class FindingVersionWire(TypedDict):
    version_id: str
    trace_id: str
    model_finding_id: str | None
    discovery_agent: str
    discovery_dimension: Dimension
    finding: dict[str, JsonValue] | None
    base_version_id: str | None
    set_fields: dict[str, JsonValue]
    unset_fields: list[str]


class StageDecisionWire(TypedDict):
    trace_id: str
    decision: Decision
    reason_code: ReasonCode | None
    reason: str | None
    related_trace_ids: list[str]


class StageSnapshotWire(TypedDict):
    sequence: int
    stage: Stage
    finding_version_ids: list[str]
    decisions: list[StageDecisionWire]
    executed: bool
    replayed: bool
    coverage: Coverage
    dispatched_dimensions: list[Dimension]
    degraded_dimensions: list[Dimension]
    cap: int | None
    cap_hit: bool


class MeasurementTraceWire(TypedDict):
    schema_version: Literal[1]
    trace_version: str
    run_nonce: str
    versions: list[FindingVersionWire]
    snapshots: list[StageSnapshotWire]
    resolved_models: dict[str, str]
    complete: bool


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
    stage: LossStage
    physical_stage: Stage | None
    cause_stage: Stage | None
    reason_code: ReasonCode
    reason: str
    trace_ids: tuple[str, ...]
    evidence_refs: tuple[str, ...]
    status: Literal["established", "unavailable", "ambiguous"]


class StageMatchWire(TypedDict):
    bug_id: str
    stage: Stage
    version_id: str
    match: bool | None
    confidence: float | None
    reasoning: str | None
    evaluator_id: str
    error: str | None


@dataclass(frozen=True, slots=True)
class StageMatch:
    bug_id: str
    stage: Stage
    version_id: str
    match: bool | None
    confidence: float | None
    reasoning: str | None
    evaluator_id: str
    error: str | None


class StageBugObservationWire(TypedDict):
    score_ref: ScoreRefWire
    attempt_id: str
    pr_url: str
    bug_id: str
    stage: Stage
    survives: bool | None
    matched_version_ids: list[str]
    loss: LossWire | None
    coverage: Coverage
    cap_hit: bool


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


class FunnelReportWire(TypedDict):
    denominator: int
    delivered_hits: int
    discovered_hits: int
    losses: dict[LossStage, int]
    unavailable_count: int
    matcher_disagreement_count: int
    survivor_counts: dict[Stage, int]
    cap_hit_counts: dict[Stage, int]
    unjudged_counts: dict[Stage, int]
    coverage: Coverage


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
        _require(
            row["sequence"] == len(snapshots) and row["stage"] in get_args(Stage),
            "stage sequence",
        )
        ids = row["finding_version_ids"]
        _require(
            isinstance(ids, list)
            and len(set(ids)) == len(ids)
            and all(v in by_id for v in ids),
            "snapshot references",
        )
        _require(
            len({by_id[v].trace_id for v in ids}) == len(ids), "duplicate occurrence"
        )
        _require(row["coverage"] in get_args(Coverage), "coverage")
        for flag in ("executed", "replayed", "cap_hit"):
            _require(type(row[flag]) is bool, "snapshot flag")
        _require(
            row["cap"] is None or (type(row["cap"]) is int and row["cap"] >= 0), "cap"
        )
        for field in ("dispatched_dimensions", "degraded_dimensions"):
            _require(
                all(d in get_args(Dimension) for d in row[field]), "coverage dimensions"
            )
        decisions = []
        for d in row["decisions"]:
            _require(
                d["trace_id"] in identities
                and all(t in identities for t in d["related_trace_ids"]),
                "decision reference",
            )
            _require(
                d["decision"] in get_args(Decision)
                and (
                    d["reason_code"] is None or d["reason_code"] in get_args(ReasonCode)
                ),
                "decision code",
            )
            _require(
                d["reason"] is None or isinstance(d["reason"], str), "decision reason"
            )
            decisions.append(
                StageDecision(
                    **{**d, "related_trace_ids": tuple(d["related_trace_ids"])}
                )
            )
        snapshots.append(
            StageSnapshot(
                **{
                    **row,
                    "finding_version_ids": tuple(ids),
                    "decisions": tuple(decisions),
                    "dispatched_dimensions": tuple(row["dispatched_dimensions"]),
                    "degraded_dimensions": tuple(row["degraded_dimensions"]),
                }
            )
        )
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


def validate_cap(cap):
    if type(cap) not in (int, float) or not math.isfinite(cap) or cap < 0:
        raise ValueError("stage capture requires a finite nonnegative cost cap")


def render_claim(finding):
    # No anchor or fix policy is invented for an intermediate claim.
    from gauntlet.delivery import compose

    f = dict(finding)
    f["body"] = f.get("body", f.get("description", ""))
    return compose.compose_inline_body(
        compose.render_group_sections(f, ()), platform="github", surface="inline"
    ).body


def _terminal_loss(trace, bits, matched, versions):
    if not trace.complete:
        return Loss(
            "unknown",
            None,
            None,
            "unknown",
            "stage evidence incomplete",
            (),
            (),
            "unavailable",
        )
    discover = next((s for s in trace.snapshots if s.stage == "discover"), None)
    if not any(value is True for value in bits.values()):
        if any(value is None for value in bits.values()):
            return Loss(
                "unknown",
                None,
                None,
                "unknown",
                "stage judging incomplete",
                (),
                (),
                "unavailable",
            )
        if (
            discover is None
            or not discover.executed
            or discover.coverage == "unavailable"
        ):
            return Loss(
                "unknown",
                None,
                None,
                "unknown",
                "discovery coverage incomplete",
                (),
                (),
                "unavailable",
            )
        return Loss(
            "not_found",
            "discover",
            None,
            "not_found",
            "no captured discovery claim matched"
            + (
                "; discovery coverage partial" if discover.coverage == "partial" else ""
            ),
            (),
            (f"snapshot:{discover.sequence}",),
            "established",
        )
    last = max(i for i, s in enumerate(trace.snapshots) if bits[s.stage])
    boundary = trace.snapshots[last + 1] if last + 1 < len(trace.snapshots) else None
    if boundary is None:
        return Loss(
            "unknown",
            None,
            None,
            "unknown",
            "no terminal transition",
            (),
            (),
            "unavailable",
        )
    if any(bits[s.stage] is None for s in trace.snapshots[last + 1 :]):
        return Loss(
            "unknown",
            boundary.stage,
            None,
            "unknown",
            "later stage judging incomplete",
            (),
            (),
            "unavailable",
        )
    previous = trace.snapshots[last]
    tids = {versions[v].trace_id for v in matched[previous.stage]}
    decisions = [
        d for d in boundary.decisions if d.trace_id in tids and d.decision != "kept"
    ]
    # Intake omissions are represented at discovery but take effect before merge.
    if not decisions and boundary.stage == "merge":
        decisions = [
            d for d in previous.decisions if d.trace_id in tids and d.decision != "kept"
        ]
    if not decisions:
        return Loss(
            "unknown",
            boundary.stage,
            None,
            "unknown",
            "no structured transition evidence",
            tuple(sorted(tids)),
            (),
            "unavailable",
        )
    d = decisions[0]
    code = d.reason_code or "unknown"
    cause = None
    stage = boundary.stage
    if code in ("challenge_cap", "delivery_cap"):
        stage = "cap"
    elif code == "validation_skipped":
        stage = "validate"
    elif code == "verify_unproven":
        stage = "verify"
    elif code == "challenge_failed":
        stage = "challenge"
    elif boundary.stage in ("select", "deliver"):
        stage = "delivery"
    elif boundary.stage == "holdback":
        stage = "filter" if code == "injection" else "unknown"
    if boundary.stage == "filter" and code == "threshold":
        before = [
            versions[v].projection()
            for snapshot in trace.snapshots
            if snapshot.stage == "verify"
            for v in snapshot.finding_version_ids
        ]
        after = [versions[v].projection() for v in matched["validate"]]
        removed = [
            v.projection()
            for v in trace.versions
            if v.trace_id == d.trace_id
            and v.projection().get("measurement_threshold", {}).get("field")
            == "confidence"
        ]
        for finding in removed:
            threshold = finding["measurement_threshold"]["value"]
            if any(
                f.get("trace_id") == d.trace_id and f.get("confidence", 0) >= threshold
                for f in before
            ) and any(
                f.get("trace_id") == d.trace_id
                and f.get("validator_confidence")
                == f.get("confidence")
                == finding.get("confidence")
                and f["confidence"] < threshold
                for f in after
            ):
                cause = "validate"
    return Loss(
        stage,
        boundary.stage,
        cause,
        code,
        d.reason or code,
        tuple(sorted(tids)),
        (f"snapshot:{boundary.sequence}",),
        "established" if stage != "unknown" else "unavailable",
    )


def evaluate_stages(
    trace,
    epoch,
    *,
    delivered_observations,
    matcher,
    stage_cost_cap_usd,
    price_estimator,
) -> tuple[StageBugObservation, ...]:
    """Enumerate all unique missed-golden pairs before any matcher dispatch."""
    validate_cap(stage_cost_cap_usd)
    versions = {v.version_id: v for v in trace.versions}
    used = {v for s in trace.snapshots if s.executed for v in s.finding_version_ids}
    rendered = {v: render_claim(versions[v].projection()) for v in sorted(used)}
    golden = {b.bug_id: b for b in epoch.bugs}
    evaluator_id = getattr(matcher, "evaluator_id", "injected")
    requests, pair_keys = {}, {}
    for bug in delivered_observations:
        if bug.outcome == "matched":
            continue
        for vid, claim in rendered.items():
            key = (digest(golden[bug.bug_id].comment), digest(claim), evaluator_id)
            requests[key] = (golden[bug.bug_id].comment, claim)
            pair_keys[bug.bug_id, vid] = key
    estimate = estimate_pairs(requests.values(), price_estimator)
    available = estimate is not None
    print(
        f"Stage pairs: {len(requests)}; estimated USD: {estimate}; cap USD: {stage_cost_cap_usd}"
    )
    status: Literal["complete", "refused", "capped"] = "complete"
    reason = None
    if not available or estimate > stage_cost_cap_usd or (matcher is None and requests):
        status, reason = (
            "refused",
            "prices or matcher unavailable"
            if not available or matcher is None
            else "estimate exceeds cap",
        )
    direct_session = (
        matcher
        if isinstance(matcher, MatchSession)
        else MatchSession(
            matcher,
            price_estimator,
            stage_cost_cap_usd,
            evaluator_id,
            {},
            [StageBudget(status, reason)],
            [],
            [],
        )
    )
    results, matches = {}, []
    if status == "complete":
        results = {key: direct_session(*pair) for key, pair in requests.items()}
    capture = empty_capture(True, stage_cost_cap_usd)
    capture.update(
        pairwise_call_count=len(requests),
        estimated_cost_usd=estimate,
        price_schedule_sha256=getattr(price_estimator, "price_schedule_sha256", None),
    )
    capture.update(
        status=direct_session.budget[0].status,
        reason=direct_session.budget[0].reason,
        actual_call_count=direct_session.budget[0].actual_call_count,
        actual_cost_usd=direct_session.budget[0].actual_usd
        if direct_session.budget[0].measured
        and direct_session.budget[0].actual_call_count
        else None,
    )
    if hasattr(matcher, "diagnostics"):
        matcher.diagnostics.update(capture)
    observations = []
    for bug in delivered_observations:
        bits, matched = {}, {}
        for snapshot in trace.snapshots:
            ids, unknown = [], not snapshot.executed
            for vid in snapshot.finding_version_ids:
                if bug.outcome == "matched":
                    unknown = True
                    continue
                result = results.get(
                    pair_keys[bug.bug_id, vid],
                    {"match": None, "error": reason or "pair not judged"},
                )
                confidence = result.get("confidence")
                if confidence is not None and (
                    type(confidence) not in (int, float)
                    or not math.isfinite(confidence)
                    or not 0 <= confidence <= 1
                ):
                    result = {**result, "match": None, "error": "invalid confidence"}
                    confidence = None
                matches.append(
                    StageMatch(
                        bug.bug_id,
                        snapshot.stage,
                        vid,
                        result.get("match"),
                        confidence,
                        result.get("reasoning"),
                        evaluator_id,
                        result.get("error"),
                    )
                )
                if result.get("error") or result.get("match") is None:
                    unknown = True
                elif result["match"]:
                    ids.append(vid)
            bits[snapshot.stage] = (
                True if ids else None if unknown or bug.outcome == "matched" else False
            )
            matched[snapshot.stage] = tuple(ids)
        bits["deliver"] = bug.outcome == "matched"
        loss = (
            None
            if bug.outcome == "matched"
            else _terminal_loss(trace, bits, matched, versions)
        )
        for stage in get_args(Stage):
            snapshot = next((s for s in trace.snapshots if s.stage == stage), None)
            observations.append(
                StageBugObservation(
                    bug.score_ref,
                    bug.attempt_id,
                    bug.pr_url,
                    bug.bug_id,
                    stage,
                    bits.get(stage),
                    matched.get(stage, ()),
                    loss if stage == "deliver" else None,
                    snapshot.coverage if snapshot else "unavailable",
                    snapshot.cap_hit if snapshot else False,
                )
            )
    if hasattr(matcher, "matches"):
        matcher.matches.extend(matches)
    return tuple(observations)


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


def empty_capture(enabled=False, cap=None):
    return {
        "enabled": enabled,
        "status": "unavailable" if enabled else "off",
        "reason": None,
        "pairwise_call_count": 0,
        "estimated_cost_usd": None,
        "cost_cap_usd": cap,
        "price_schedule_sha256": None,
        "actual_call_count": 0,
        "actual_cost_usd": None,
    }


STAGE_MAX_OUTPUT_TOKENS = 256
STAGE_SYSTEM = 'Compare the golden bug and review claim. Match only the same concrete defect. Return JSON: {"match": boolean, "confidence": number, "reasoning": string}.'


def _stage_message(golden, claim):
    return json.dumps({"golden": golden, "claim": claim}, ensure_ascii=True)


@dataclass(frozen=True, slots=True)
class PinnedPrices:
    model: str
    input_usd_per_million: float
    output_usd_per_million: float
    price_schedule_sha256: str

    def __call__(self, golden, claim):
        # UTF-8 bytes upper-bound token count, including a conservative framing reserve.
        input_max = (
            len((STAGE_SYSTEM + _stage_message(golden, claim)).encode("utf-8")) + 1024
        )
        return (
            input_max * self.input_usd_per_million
            + STAGE_MAX_OUTPUT_TOKENS * self.output_usd_per_million
        ) / 1_000_000


def pinned_prices(prices, pin):
    if (
        not isinstance(prices, dict)
        or prices.get("model") != pin
        or not prices.get("as_of")
    ):
        return None
    for name in ("input_usd_per_million", "output_usd_per_million"):
        if (
            type(prices.get(name)) not in (int, float)
            or not math.isfinite(prices[name])
            or prices[name] < 0
        ):
            return None
    return PinnedPrices(
        pin,
        prices["input_usd_per_million"],
        prices["output_usd_per_million"],
        digest(prices),
    )


@dataclass(frozen=True, slots=True)
class StageJudge:
    pin: str
    api_key: str | None
    prices: PinnedPrices | None
    max_retries: int = 1

    def __call__(self, golden, claim):
        import urllib.request

        if not self.api_key:
            raise ValueError("stage judge API key unavailable")
        payload = {
            "model": self.pin,
            "max_tokens": STAGE_MAX_OUTPUT_TOKENS,
            "temperature": 0,
            "system": STAGE_SYSTEM,
            "messages": [{"role": "user", "content": _stage_message(golden, claim)}],
        }
        request = urllib.request.Request(
            "https://api.anthropic.com/v1/messages",
            data=canonical(payload),
            headers={
                "x-api-key": self.api_key,
                "anthropic-version": "2023-06-01",
                "Content-Type": "application/json",
            },
        )
        with urllib.request.urlopen(request, timeout=120) as response:
            reply = json.load(response)
        text = "".join(
            block.get("text", "")
            for block in reply.get("content", [])
            if block.get("type") == "text"
        )
        result = json.loads(text)
        usage = reply.get("usage", {})
        if self.prices and all(
            type(usage.get(k)) is int for k in ("input_tokens", "output_tokens")
        ):
            result["actual_cost_usd"] = (
                usage["input_tokens"] * self.prices.input_usd_per_million
                + usage["output_tokens"] * self.prices.output_usd_per_million
            ) / 1_000_000
        return result


class StagePriceWire(TypedDict):
    model: str
    as_of: str
    input_usd_per_million: float
    output_usd_per_million: float


class StageResultWire(TypedDict, total=False):
    match: bool | None
    confidence: float | None
    reasoning: str | None
    error: str | None
    actual_cost_usd: float | None


class StageCallWire(StageResultWire):
    pair_id: str
    retry: int
    reserved_max_usd: float


@dataclass(frozen=True, slots=True)
class StageBudget:
    status: Literal["complete", "refused", "capped"]
    reason: str | None
    reserved_usd: float = 0.0
    actual_call_count: int = 0
    measured: bool = True
    actual_usd: float = 0.0


@dataclass(frozen=True, slots=True)
class MatchSession:
    delegate: object
    estimator: object
    cap: float
    evaluator_id: str
    cache: dict[tuple[str, str, str], StageResultWire]
    budget: list[StageBudget]
    matches: list[StageMatch]
    journal: list[StageCallWire]
    max_retries: int = 0

    def __call__(self, golden, claim):
        key = (digest(golden), digest(claim), self.evaluator_id)
        if key in self.cache:
            return self.cache[key]
        maximum = self.estimator(golden, claim)
        result = {"match": None, "error": "cost cap exhausted"}
        for retry in range(getattr(self.delegate, "max_retries", 0) + 1):
            state = self.budget[0]
            if state.reserved_usd + maximum > self.cap:
                self.budget[0] = replace(
                    state, status="capped", reason="retry reservation exceeds cap"
                )
                break
            self.budget[0] = replace(
                state,
                reserved_usd=state.reserved_usd + maximum,
                actual_call_count=state.actual_call_count + 1,
            )
            try:
                result = self.delegate(golden, claim)
                if not isinstance(result, dict):
                    raise ValueError("invalid stage result")
                if type(result.get("match")) is not bool and not result.get("error"):
                    raise ValueError("invalid stage match flag")
                confidence = result.get("confidence")
                if confidence is not None and (
                    type(confidence) not in (int, float)
                    or not math.isfinite(confidence)
                    or not 0 <= confidence <= 1
                ):
                    raise ValueError("invalid stage confidence")
                if any(
                    result.get(field) is not None and not isinstance(result[field], str)
                    for field in ("reasoning", "error")
                ):
                    raise ValueError("invalid stage explanation")
            except (OSError, RuntimeError, ValueError, TypeError) as exc:
                result = {"match": None, "error": type(exc).__name__}
            actual = result.get("actual_cost_usd")
            if actual is not None and (
                type(actual) not in (int, float)
                or not math.isfinite(actual)
                or not 0 <= actual <= maximum
            ):
                result = {"match": None, "error": "charge exceeds reservation"}
                actual = None
                self.budget[0] = replace(
                    self.budget[0],
                    status="capped",
                    reason="charge exceeds reserved maximum",
                )
            state = self.budget[0]
            self.budget[0] = replace(
                state,
                measured=state.measured and actual is not None,
                actual_usd=state.actual_usd + (actual or 0),
            )
            self.journal.append(
                {
                    "pair_id": digest(key),
                    "retry": retry,
                    "reserved_max_usd": maximum,
                    **result,
                    "actual_cost_usd": actual,
                }
            )
            if (
                type(result.get("match")) is bool and not result.get("error")
            ) or self.budget[0].status == "capped":
                break
        self.cache[key] = result
        return result


def estimate_pairs(pairs, estimator):
    if estimator is None:
        return None
    try:
        estimates = [estimator(*pair) for pair in pairs]
    except (OSError, RuntimeError, ValueError, TypeError, KeyError):
        return None
    if not all(
        type(v) in (int, float) and math.isfinite(v) and v >= 0 for v in estimates
    ):
        return None
    return sum(estimates)


def capture_score(
    traces,
    bugs,
    epoch,
    *,
    enabled,
    cap,
    matcher,
    price_estimator,
    errors,
    evaluator_id,
    pin,
    api_key,
    prices,
):
    capture = empty_capture(enabled, cap)
    if not enabled:
        return (), (), capture, ()
    validate_cap(cap)
    estimator = price_estimator or pinned_prices(prices, pin)
    delegate = matcher or StageJudge(
        pin, api_key, estimator if isinstance(estimator, PinnedPrices) else None
    )
    pairs = {}
    golden = {b.bug_id: b for b in epoch.bugs}
    by_url = {}
    for bug in bugs:
        by_url.setdefault(bug.pr_url, []).append(bug)
        if bug.outcome == "matched" or bug.pr_url not in traces:
            continue
        trace = traces[bug.pr_url][1]
        used = {
            vid for s in trace.snapshots if s.executed for vid in s.finding_version_ids
        }
        for version in trace.versions:
            if version.version_id in used:
                claim = render_claim(version.projection())
                key = (digest(golden[bug.bug_id].comment), digest(claim), evaluator_id)
                pairs[key] = (golden[bug.bug_id].comment, claim)
    estimate = estimate_pairs(pairs.values(), estimator)
    available = estimate is not None
    print(f"Stage score pairs: {len(pairs)}; estimated USD: {estimate}; cap USD: {cap}")
    capture.update(
        pairwise_call_count=len(pairs),
        estimated_cost_usd=estimate,
        price_schedule_sha256=getattr(estimator, "price_schedule_sha256", None),
    )
    refused = not available or estimate > cap
    capture.update(
        status="refused" if refused else "complete",
        reason="prices unavailable"
        if not available
        else "estimate exceeds cap"
        if refused
        else None,
    )
    bookkeeping = [StageBudget(capture["status"], capture["reason"])]
    # evaluate_stages can inspect local estimates, while one session bounds every PR and retry.
    session = MatchSession(
        delegate, estimator, cap, evaluator_id, {}, bookkeeping, [], []
    )
    rows = []
    for url, cells in by_url.items():
        if url not in traces:
            for bug in cells:
                loss = (
                    None
                    if bug.outcome == "matched"
                    else Loss(
                        "unknown",
                        None,
                        None,
                        "unknown",
                        errors.get(url, "trace unavailable"),
                        (),
                        (),
                        "unavailable",
                    )
                )
                for stage in get_args(Stage):
                    rows.append(
                        StageBugObservation(
                            bug.score_ref,
                            bug.attempt_id,
                            url,
                            bug.bug_id,
                            stage,
                            bug.outcome == "matched" if stage == "deliver" else None,
                            (),
                            loss if stage == "deliver" else None,
                            "unavailable",
                        )
                    )
            continue
        trace = traces[url][1]
        if url in errors:
            trace = replace(trace, complete=False)
        # A refused score never reaches a matcher, including through a per-PR estimate.
        local = (
            MatchSession(None, None, cap, evaluator_id, {}, [bookkeeping[0]], [], [])
            if refused
            else session
        )
        rows.extend(
            evaluate_stages(
                trace,
                epoch,
                delivered_observations=cells,
                matcher=local,
                stage_cost_cap_usd=cap,
                price_estimator=None if refused else estimator,
            )
        )
    capture.update(
        status=bookkeeping[0].status,
        reason=bookkeeping[0].reason,
        actual_call_count=bookkeeping[0].actual_call_count,
        actual_cost_usd=bookkeeping[0].actual_usd
        if bookkeeping[0].measured and bookkeeping[0].actual_call_count
        else None,
    )
    if (
        errors
        or not traces
        or any(url not in traces for url in by_url)
        or any(not trace.complete for _, trace in traces.values())
    ):
        capture.update(
            status="unavailable", reason="one or more trace artifacts unavailable"
        )
    # The diagnostic journal contains every retry and its charge, even when its final pair result is an error.
    return tuple(rows), tuple(session.matches), capture, tuple(session.journal)


def credited_dimensions(
    bug: BugObservation,
    candidates: Sequence[Candidate],
    matched_versions: Iterable[FindingVersion],
) -> tuple[Dimension, ...]:
    """Credit only the matched discovery claims that sourced a delivered match."""
    matched_candidates = {m.candidate_id for m in bug.matches}
    delivered_sources = {
        tid
        for candidate in candidates
        if candidate.candidate_id in matched_candidates
        and candidate.pr_url == bug.pr_url
        and candidate.provenance_status == "complete"
        for tid in candidate.source_trace_ids
    }
    return tuple(
        sorted(
            {
                version.discovery_dimension
                for version in matched_versions
                if version.trace_id in delivered_sources
            }
        )
    )


def dimension_metrics(candidates, bugs, stage_rows, profile, traces):
    from bench.runner.epochs import PROFILE_CATEGORIES

    population = [
        b
        for b in bugs
        if b.category is None or b.category in PROFILE_CATEGORIES[profile]
    ]
    # A delivered match alone establishes no source-claim match, so opt-out has no dimension recall.
    versions = {
        (url, v.version_id): v
        for url, (_, trace) in traces.items()
        for v in trace.versions
    }
    evidence = {
        (r.bug_id, r.score_ref): tuple(
            versions[(r.pr_url, vid)] for vid in r.matched_version_ids
        )
        for r in stage_rows
        if r.stage == "discover" and r.survives
    }
    out = {}
    for dimension in get_args(Dimension):
        entries = [
            c
            for c in candidates
            if dimension in c.discovery_dimensions and c.provenance_status == "complete"
        ]
        if not entries and not any(
            v.discovery_dimension == dimension
            for _, trace in traces.values()
            for v in trace.versions
        ):
            continue
        delivered = sum(
            dimension
            in credited_dimensions(
                b, candidates, evidence.get((b.bug_id, b.score_ref), ())
            )
            for b in population
        )
        discovered = sum(
            any(
                v.discovery_dimension == dimension
                for v in evidence.get((b.bug_id, b.score_ref), ())
            )
            for b in population
        )
        judged = {
            (r.bug_id, r.score_ref)
            for r in stage_rows
            if r.stage == "discover"
            and r.survives is not None
            and r.coverage == "complete"
        }
        full_discovery = bool(population) and all(
            (b.bug_id, b.score_ref) in judged for b in population
        )
        out[dimension] = {
            "dimension": dimension,
            "candidate_count": len(entries),
            "delivered_bug_hits": delivered,
            "discovery_bug_hits": discovered,
            "denominator": len(population),
            "delivered_recall": None,
            "discovery_recall": discovered / len(population)
            if full_discovery
            else None,
            "audited_count": 0,
            "correct_count": 0,
            "unresolved_count": len(entries),
            "groundedness": None,
        }
    return out


def bind_delivery_trace(trace, candidates, *, omissions=()):
    """Overlay pure, payload-proven posting omissions on producer selection."""

    delivered = {tid for c in candidates for tid in c.source_trace_ids}
    versions = {v.version_id: v for v in trace.versions}
    omitted_by_id = {
        tid: omission for omission in omissions for tid in omission.trace_ids
    }
    snapshots = []
    for snapshot in trace.snapshots:
        if snapshot.stage != "deliver":
            snapshots.append(snapshot)
            continue
        kept = tuple(
            v for v in snapshot.finding_version_ids if versions[v].trace_id in delivered
        )
        omitted = tuple(
            StageDecision(
                versions[v].trace_id,
                "not_selected",
                omitted_by_id[versions[v].trace_id].reason_code
                if versions[v].trace_id in omitted_by_id
                else "post_omission",
                omitted_by_id[versions[v].trace_id].reason
                if versions[v].trace_id in omitted_by_id
                else "selected source omitted from scored inline comments",
                (),
            )
            for v in snapshot.finding_version_ids
            if versions[v].trace_id not in delivered
        )
        snapshots.append(
            replace(
                snapshot,
                finding_version_ids=kept,
                decisions=snapshot.decisions + omitted,
            )
        )
    return replace(trace, snapshots=tuple(snapshots))


class TraceUnavailableWire(TypedDict):
    status: Literal["unavailable"]
    reason: str


class CollectedTraceWire(TypedDict):
    path: str
    sha256: str
    run_nonce: str
    workflow_record: str
    workflow_sha256: str


def collect_trace(pr_dir: Path) -> CollectedTraceWire | None:
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
            continue
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


class DeliveryBindingWire(TypedDict):
    selected_path: str
    payload_path: str
    diff_path: str


class TraceBindingWire(TypedDict):
    attempt_id: str
    trace_path: str
    trace_sha256: str
    delivery: DeliveryBindingWire | None


def validate_packet_evidence(path, packet, bugs, candidates):
    """Check stage and source identities in addition to the packet byte manifest."""
    from bench.runner.epochs import confined
    from bench.runner.provenance import map_delivery

    capture = packet["stage_capture"]
    if "enabled" not in capture:
        _require(
            capture["status"] in ("not_requested", "unavailable"),
            "legacy capture status",
        )
        return
    _require(
        type(capture["enabled"]) is bool
        and capture["status"]
        in ("off", "complete", "unavailable", "refused", "capped"),
        "capture status",
    )
    for name in ("pairwise_call_count", "actual_call_count"):
        _require(
            type(capture[name]) is int and capture[name] >= 0, "capture call count"
        )
    for name in ("estimated_cost_usd", "cost_cap_usd", "actual_cost_usd"):
        value = capture[name]
        _require(
            value is None
            or (type(value) in (int, float) and math.isfinite(value) and value >= 0),
            "capture cost",
        )
    bindings = read_json(path / "stage_trace_bindings.json")
    traces = {}
    for url, binding in bindings.items():
        _require(
            binding["attempt_id"] == packet["manifest"]["selected_attempts"].get(url),
            "trace attempt binding",
        )
        source = confined(path, binding["trace_path"])
        traces[url] = load_trace(source, expected_sha256=binding["trace_sha256"])
        delivery = binding["delivery"]
        group = tuple(c for c in candidates if c.pr_url == url)
        if delivery:
            selected = read_json(confined(path, delivery["selected_path"]))
            selected = (
                selected.get("findings", []) if isinstance(selected, dict) else selected
            )
            mapped = map_delivery(
                read_json(confined(path, delivery["payload_path"])),
                selected,
                confined(path, delivery["diff_path"]).read_text(encoding="utf-8"),
            )
            _require(len(group) == len(mapped), "delivery group count")
            for candidate, original in zip(group, mapped, strict=True):
                _require(
                    candidate.source_trace_ids == original.source_trace_ids
                    and candidate.discovery_dimensions == original.discovery_dimensions
                    and candidate.provenance_status == "complete",
                    "source attribution",
                )
    for candidate in candidates:
        _require(
            candidate.provenance_status in ("complete", "legacy_unknown", "ambiguous"),
            "candidate provenance status",
        )
        if candidate.provenance_status == "complete":
            _require(
                candidate.pr_url in bindings
                and bindings[candidate.pr_url]["delivery"] is not None,
                "candidate source binding",
            )
        else:
            _require(
                not candidate.source_trace_ids
                and candidate.discovery_dimensions == ("unknown",),
                "unknown source dimension",
            )
    bug_map = {b.bug_id: b for b in bugs}
    known_versions = {
        url: {v.version_id for v in trace.versions} for url, trace in traces.items()
    }
    rows = [
        json.loads(line)
        for line in (path / "stages.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    seen = set()
    for row in rows:
        canonical(row)
        bug = bug_map.get(row["bug_id"])
        _require(
            bug is not None
            and row["score_ref"] == packet["score_ref"]
            and row["attempt_id"] == bug.attempt_id
            and row["pr_url"] == bug.pr_url,
            "stage observation identity",
        )
        _require(
            row["stage"] in get_args(Stage) and row["coverage"] in get_args(Coverage),
            "stage observation vocabulary",
        )
        key = (row["bug_id"], row["stage"])
        _require(key not in seen, "duplicate stage observation")
        seen.add(key)
        _require(
            row["survives"] is None or type(row["survives"]) is bool, "survival bit"
        )
        _require(type(row.get("cap_hit", False)) is bool, "stage cap flag")
        if row.get("cap_hit"):
            trace = traces.get(row["pr_url"])
            _require(
                trace is not None
                and any(s.stage == row["stage"] and s.cap_hit for s in trace.snapshots),
                "stage cap evidence",
            )
        _require(
            all(
                v in known_versions.get(row["pr_url"], ())
                for v in row["matched_version_ids"]
            ),
            "matched version reference",
        )
        loss = row["loss"]
        _require(
            loss is None or (row["stage"] == "deliver" and not row["survives"]),
            "terminal loss boundary",
        )
        if loss:
            _require(
                loss["stage"] in get_args(LossStage)
                and loss["reason_code"] in get_args(ReasonCode)
                and loss["status"] in ("established", "unavailable", "ambiguous"),
                "loss vocabulary",
            )
            _require(
                loss["physical_stage"] is None
                or loss["physical_stage"] in get_args(Stage),
                "loss physical stage",
            )
            _require(
                loss["cause_stage"] is None or loss["cause_stage"] in get_args(Stage),
                "loss cause",
            )
        if row["stage"] == "deliver":
            _require(
                bug.loss == freeze_record(row["loss"]), "bug terminal loss projection"
            )
            _require(
                row["survives"] == (bug.outcome == "matched"),
                "delivered observation agreement",
            )
    _require(
        len(rows) == (len(bugs) * len(get_args(Stage)) if capture["enabled"] else 0),
        "stage golden coverage",
    )
    _require(packet["stages_path"] == ("stages.jsonl" if rows else None), "stage path")
    matches = [
        json.loads(line)
        for line in (path / "stage_matches.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    for match in matches:
        canonical(match)
        bug = bug_map.get(match["bug_id"])
        _require(
            bug is not None
            and bug.outcome != "matched"
            and match["evaluator_id"]
            == packet["manifest"]["evaluator"]["evaluator_id"],
            "stage match identity",
        )
        _require(
            match["stage"] in get_args(Stage)
            and match["version_id"] in known_versions.get(bug.pr_url, ()),
            "stage match reference",
        )
        _require(
            match["match"] is None or type(match["match"]) is bool, "stage match flag"
        )
        value = match["confidence"]
        _require(
            value is None
            or (
                type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 1
            ),
            "stage confidence",
        )
    calls = read_json(path / "stage_calls.json")
    _require(len(calls) == capture["actual_call_count"], "stage execution count")
    if calls:
        _require(
            sum(c["reserved_max_usd"] for c in calls) <= capture["cost_cap_usd"],
            "stage reservations",
        )
        measured = all(c["actual_cost_usd"] is not None for c in calls)
        _require(
            capture["actual_cost_usd"]
            == (sum(c["actual_cost_usd"] for c in calls) if measured else None),
            "stage actual charges",
        )

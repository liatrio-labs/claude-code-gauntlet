"""Score identities, complete observations, and append-only packet transactions."""

from __future__ import annotations

import json
import os
import shutil
import uuid
from dataclasses import asdict, dataclass, fields, is_dataclass, replace
from pathlib import Path
from typing import Literal, TypeAlias, TypedDict, cast

from bench.runner.epochs import (
    Category,
    GoldenEpoch,
    JsonValue,
    Profile,
    canonical,
    confined,
    digest,
    file_hash,
    load_epoch,
    read_json,
)
from bench.runner.profiles import ProfileMetrics, ProfileMetricsWire, profile_metrics

Dimension = Literal[
    "bug",
    "security",
    "cross_file_impact",
    "test_coverage",
    "convention",
    "intent",
    "comment_accuracy",
    "type_design",
    "simplification",
    "unknown",
]
Outcome = Literal["matched", "missed", "operational_failure", "unavailable"]
Bucket = Literal["golden_matched", "valid_extra", "noise", "error", "unavailable"]
ProvenanceStatus = Literal["complete", "legacy_unknown", "ambiguous"]
ContextStatus = Literal["head_and_hunk", "hunk_only", "full_diff", "missing"]


class ScoreRefWire(TypedDict):
    run_id: str
    epoch_id: str
    score_id: str


class CandidateWire(TypedDict):
    candidate_id: str
    pr_url: str
    attempt_id: str
    index: int
    text: str
    path: str | None
    line: int | None
    source_trace_ids: list[str]
    discovery_dimensions: list[Dimension]
    dedup_group_id: str
    provenance_status: ProvenanceStatus


class MatchWire(TypedDict):
    candidate_id: str
    confidence: float
    reasoning: str | None
    kind: Literal["direct", "dedup_sibling"]


Stage = Literal[
    "discover",
    "merge",
    "verify",
    "validate",
    "filter",
    "challenge",
    "holdback",
    "select",
    "deliver",
]
LossStage = Literal[
    "not_found",
    "verify",
    "validate",
    "challenge",
    "cap",
    "merge",
    "filter",
    "delivery",
    "unknown",
]
Decision = Literal["kept", "eliminated", "coalesced", "held_back", "not_selected"]
ReasonCode = Literal[
    "not_found",
    "duplicate_id",
    "invalid_shape",
    "invalid_path",
    "audit_overlap",
    "verify_eliminated",
    "verify_unproven",
    "validation_skipped",
    "threshold",
    "exclusion",
    "injection",
    "suppression",
    "challenge_removed",
    "challenge_downgraded_removed",
    "challenge_failed",
    "challenge_cap",
    "delivery_cap",
    "delivery_tier",
    "unanchored",
    "body_budget",
    "post_omission",
    "unknown",
]
Coverage = Literal["complete", "partial", "unavailable"]


class LossWire(TypedDict):
    stage: LossStage
    physical_stage: Stage | None
    cause_stage: Stage | None
    reason_code: ReasonCode
    reason: str
    trace_ids: list[str]
    evidence_refs: list[str]
    status: Literal["established", "unavailable", "ambiguous"]


class BugObservationWire(TypedDict):
    schema_version: Literal[1]
    score_ref: ScoreRefWire
    pr_url: str
    attempt_id: str
    bug_id: str
    category: Category | None
    outcome: Outcome
    matches: list[MatchWire]
    errors: list[str]
    loss: LossWire | None


class DedupGroupWire(TypedDict):
    group_id: str
    pr_url: str
    candidate_ids: list[str]


class AdjudicationWire(TypedDict):
    candidate_id: str
    pr_url: str
    bucket: Bucket
    reasoning: str | None
    evaluator_id: str
    context_sha256: str
    context_status: ContextStatus
    error: str | None


class AdjudicationContextWire(TypedDict):
    candidate_id: str
    hunk: str
    file_context: str
    context_status: ContextStatus
    truncated: bool
    context_sha256: str


class EvaluatorWire(TypedDict):
    evaluator_id: str
    judge_pin: str
    adjudicator_pin: str
    scorer_sha: str
    scorer_sha256: str
    harness_scoring_sha256: str
    adapter_sha256: str
    adjudicator_code_sha256: str
    adjudicator_prompt_sha256: str
    profile_policy_version: str
    stage_matcher_version: str
    temperature: float


class ScoreManifestWire(TypedDict):
    schema_version: Literal[2]
    score_ref: ScoreRefWire
    created_at: str
    epoch_hashes: dict[str, str]
    evaluator: EvaluatorWire
    requested_pr_urls: list[str]
    selected_attempts: dict[str, str]
    source_artifact_hashes: dict[str, str]
    run_validity_ref: str | None
    provenance_status: ProvenanceStatus
    profiles: list[Profile]
    completion_status: Literal["complete"]


class CompleteWire(TypedDict):
    schema_version: Literal[1]
    score_ref: ScoreRefWire
    files: dict[str, str]


class DriftWire(TypedDict):
    desc_len_mean: float
    severity_dist: dict[str, int]


class DimensionMetricsWire(TypedDict):
    dimension: Dimension
    candidate_count: int
    delivered_bug_hits: int
    discovery_bug_hits: int
    denominator: int
    delivered_recall: float | None
    discovery_recall: float | None
    audited_count: int
    correct_count: int
    unresolved_count: int
    groundedness: float | None


class RunMetricsWire(ProfileMetricsWire):
    golden_recall: float | None
    valid_extra_rate: float
    noise_rate: float
    total_candidates: int
    n_prs: int
    per_bucket: dict[Bucket, int]
    per_dimension: dict[Dimension, DimensionMetricsWire]
    drift: DriftWire


class LegacyMetricsWire(TypedDict):
    golden_recall: float
    valid_extra_rate: float
    noise_rate: float
    precision_strict: float
    f1_strict: float
    tp: int
    fp: int
    fn: int
    total_candidates: int
    n_prs: int
    per_bucket: dict[str, int]
    per_dimension: dict[str, int]
    drift: DriftWire


class ScorerOutputsWire(TypedDict):
    candidates_path: str
    dedup_groups_path: str
    evaluations_path: str
    stage_logs: dict[str, str]


class StageCaptureWire(TypedDict):
    enabled: bool
    status: Literal["off", "complete", "unavailable", "refused", "capped"]
    reason: str | None
    pairwise_call_count: int
    estimated_cost_usd: float | None
    cost_cap_usd: float | None
    price_schedule_sha256: str | None
    actual_call_count: int
    actual_cost_usd: float | None


class ScorePacketWire(TypedDict):
    schema_version: Literal[2]
    manifest: ScoreManifestWire
    run_id: str
    score_ref: ScoreRefWire
    profiles: dict[Profile, ProfileMetricsWire]
    metrics: RunMetricsWire
    observations_path: str
    stages_path: str | None
    stage_capture: StageCaptureWire
    adjudications_path: str
    ledger_row: dict[str, JsonValue]
    complete_path: str
    judge_pin: str
    adjudicator_pin: str
    scorer_sha: str


class CompatibilityScorePacketWire(ScorePacketWire):
    buckets: dict[str, dict[str, int]]
    adjudications: list[AdjudicationWire]


class LegacyArchiveWire(TypedDict):
    schema_version: Literal[1]
    run_id: str
    epoch_id: str
    archived_files: dict[str, str]
    unavailable_files: list[str]
    aggregate_scores: JsonValue
    observations_available: bool
    status: Literal["complete", "partial"]
    source_score_id: str | None
    pins: dict[str, str | None]
    per_profile_results: dict[Profile, JsonValue]


class CalibrationReportWire(TypedDict):
    schema_version: Literal[1]
    epoch_id: str
    golden_sha256: str
    evaluator_id: str
    input_refs: list[ScoreRefWire]
    passes: int
    per_profile_variability: dict[Profile, dict[str, float]]
    anchor_results: dict[str, JsonValue]
    proposed_noise_threshold: float | None
    derivation: str
    status: Literal["complete", "partial"]
    input_byte_hashes: dict[str, str]
    evaluator: EvaluatorWire


@dataclass(frozen=True, slots=True)
class ScoreRef:
    run_id: str
    epoch_id: str
    score_id: str


@dataclass(frozen=True, slots=True)
class Candidate:
    candidate_id: str
    pr_url: str
    attempt_id: str
    index: int
    text: str
    path: str | None
    line: int | None
    source_trace_ids: tuple[str, ...]
    discovery_dimensions: tuple[Dimension, ...]
    dedup_group_id: str
    provenance_status: ProvenanceStatus


@dataclass(frozen=True, slots=True)
class Match:
    candidate_id: str
    confidence: float
    reasoning: str | None
    kind: Literal["direct", "dedup_sibling"]


@dataclass(frozen=True, slots=True)
class BugObservation:
    schema_version: Literal[1]
    score_ref: ScoreRef
    pr_url: str
    attempt_id: str
    bug_id: str
    category: Category | None
    outcome: Outcome
    matches: tuple[Match, ...]
    errors: tuple[str, ...]
    loss: tuple | None


@dataclass(frozen=True, slots=True)
class DedupGroup:
    group_id: str
    pr_url: str
    candidate_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Adjudication:
    candidate_id: str
    pr_url: str
    bucket: Bucket
    reasoning: str | None
    evaluator_id: str
    context_sha256: str
    context_status: ContextStatus
    error: str | None


@dataclass(frozen=True, slots=True)
class AdjudicationContext:
    candidate_id: str
    hunk: str
    file_context: str
    context_status: ContextStatus
    truncated: bool
    context_sha256: str


@dataclass(frozen=True, slots=True)
class Evaluator:
    evaluator_id: str
    judge_pin: str
    adjudicator_pin: str
    scorer_sha: str
    scorer_sha256: str
    harness_scoring_sha256: str
    adapter_sha256: str
    adjudicator_code_sha256: str
    adjudicator_prompt_sha256: str
    profile_policy_version: str
    stage_matcher_version: str
    temperature: float


@dataclass(frozen=True, slots=True)
class ScoreRequest:
    score_ref: ScoreRef
    epoch: GoldenEpoch
    work_dir: Path
    requested_pr_urls: tuple[str, ...]
    selected_attempts: tuple[tuple[str, str], ...]
    candidates: tuple[Candidate, ...]
    tool: str | None
    evaluator: Evaluator


@dataclass(frozen=True, slots=True)
class ScorerOutputs:
    candidates_path: Path
    dedup_groups_path: Path
    evaluations_path: Path
    stage_logs: tuple[tuple[str, Path], ...]


@dataclass(frozen=True, slots=True)
class Drift:
    desc_len_mean: float
    severity_dist: tuple[tuple[str, int], ...]


@dataclass(frozen=True, slots=True)
class DimensionMetrics:
    dimension: Dimension
    candidate_count: int
    delivered_bug_hits: int
    discovery_bug_hits: int
    denominator: int
    delivered_recall: float | None
    discovery_recall: float | None
    audited_count: int
    correct_count: int
    unresolved_count: int
    groundedness: float | None


@dataclass(frozen=True, slots=True)
class RunMetrics(ProfileMetrics):
    golden_recall: float | None
    valid_extra_rate: float
    noise_rate: float
    total_candidates: int
    n_prs: int
    per_bucket: tuple[tuple[Bucket, int], ...]
    per_dimension: tuple[tuple[Dimension, DimensionMetrics], ...]
    drift: Drift


@dataclass(frozen=True, slots=True)
class ScoreManifest:
    schema_version: Literal[2]
    score_ref: ScoreRef
    created_at: str
    epoch_hashes: tuple[tuple[str, str], ...]
    evaluator: Evaluator
    requested_pr_urls: tuple[str, ...]
    selected_attempts: tuple[tuple[str, str], ...]
    source_artifact_hashes: tuple[tuple[str, str], ...]
    run_validity_ref: str | None
    provenance_status: ProvenanceStatus
    profiles: tuple[Profile, ...]
    completion_status: Literal["complete"]


@dataclass(frozen=True, slots=True)
class Complete:
    schema_version: Literal[1]
    score_ref: ScoreRef
    files: tuple[tuple[str, str], ...]


@dataclass(frozen=True, slots=True)
class StageCapture:
    enabled: bool
    status: Literal["off", "complete", "unavailable", "refused", "capped"]
    reason: str | None
    pairwise_call_count: int
    estimated_cost_usd: float | None
    cost_cap_usd: float | None
    price_schedule_sha256: str | None
    actual_call_count: int
    actual_cost_usd: float | None


@dataclass(frozen=True, slots=True)
class ScorePacket:
    schema_version: Literal[2]
    manifest: ScoreManifest
    run_id: str
    score_ref: ScoreRef
    profiles: tuple[tuple[Profile, ProfileMetrics], ...]
    metrics: RunMetrics
    observations_path: str
    stages_path: str | None
    stage_capture: StageCapture
    adjudications_path: str
    ledger_row: tuple
    complete_path: str
    judge_pin: str
    adjudicator_pin: str
    scorer_sha: str
    observations: tuple[BugObservation, ...]
    candidates: tuple[Candidate, ...]
    adjudications: tuple[Adjudication, ...]
    root: Path


@dataclass(frozen=True, slots=True)
class LegacyScoreIdentity:
    run_id: str
    scorer_sha: str | None
    epoch_id: str | None
    profile: Literal["all"]
    source_line_sha256: str
    status: Literal["known_old", "legacy_unknown"]


@dataclass(frozen=True, slots=True)
class LegacyArchive:
    schema_version: Literal[1]
    run_id: str
    epoch_id: str
    archived_files: tuple[tuple[str, str], ...]
    unavailable_files: tuple[str, ...]
    aggregate_scores: tuple | None
    observations_available: bool
    status: Literal["complete", "partial"]
    source_score_id: str | None
    pins: tuple[tuple[str, str | None], ...]
    per_profile_results: tuple


@dataclass(frozen=True, slots=True)
class CalibrationReport:
    schema_version: Literal[1]
    epoch_id: str
    golden_sha256: str
    evaluator_id: str
    input_refs: tuple[ScoreRef, ...]
    passes: int
    per_profile_variability: tuple
    anchor_results: tuple
    proposed_noise_threshold: float | None
    derivation: str
    status: Literal["complete", "partial"]
    input_byte_hashes: tuple[tuple[str, str], ...]
    evaluator: Evaluator

    def to_wire(self) -> CalibrationReportWire:
        return {
            "schema_version": self.schema_version,
            "epoch_id": self.epoch_id,
            "golden_sha256": self.golden_sha256,
            "evaluator_id": self.evaluator_id,
            "input_refs": [cast(ScoreRefWire, asdict(r)) for r in self.input_refs],
            "passes": self.passes,
            "per_profile_variability": {
                p: dict(v) for p, v in self.per_profile_variability
            },
            "anchor_results": {k: thaw_json(v) for k, v in self.anchor_results},
            "proposed_noise_threshold": self.proposed_noise_threshold,
            "derivation": self.derivation,
            "status": self.status,
            "input_byte_hashes": dict(self.input_byte_hashes),
            "evaluator": cast(EvaluatorWire, asdict(self.evaluator)),
        }


FrozenJson: TypeAlias = "bool | int | float | str | FrozenObject | FrozenArray | None"


@dataclass(frozen=True, slots=True)
class FrozenObject:
    items: tuple[tuple[str, FrozenJson], ...]


@dataclass(frozen=True, slots=True)
class FrozenArray:
    items: tuple[FrozenJson, ...]


def freeze_json(value: object) -> FrozenJson:
    if isinstance(value, dict):
        if any(not isinstance(key, str) for key in value):
            raise ValueError("JSON object keys must be strings")
        return FrozenObject(
            tuple((k, freeze_json(v)) for k, v in sorted(value.items()))
        )
    if isinstance(value, (list, tuple)):
        return FrozenArray(tuple(freeze_json(v) for v in value))
    if value is not None and not isinstance(value, (bool, int, float, str)):
        raise ValueError("unsupported JSON value")
    canonical(value)
    return value


def thaw_json(value: FrozenJson) -> JsonValue:
    if isinstance(value, FrozenObject):
        return {k: thaw_json(v) for k, v in value.items}
    if isinstance(value, FrozenArray):
        return [thaw_json(v) for v in value.items]
    return value


def wire_record(value):
    """Serialize frozen records without turning declared wire maps into pair arrays."""
    if isinstance(value, (FrozenObject, FrozenArray)):
        return thaw_json(value)
    if is_dataclass(value):
        result = {}
        for field in fields(value):
            item = getattr(value, field.name)
            result[field.name] = (
                {k: wire_record(v) for k, v in item}
                if field.metadata.get("wire_mapping")
                else wire_record(item)
            )
        return result
    if isinstance(value, tuple):
        return [wire_record(v) for v in value]
    return value


def freeze(value):
    if isinstance(value, dict):
        return tuple((k, freeze(v)) for k, v in sorted(value.items()))
    if isinstance(value, list):
        return tuple(freeze(v) for v in value)
    return value


def artifact_allowed(path: Path) -> bool:
    return not any(
        part == "pairing"
        or part == "auth.json"
        or part.startswith(".env")
        or "credential" in part.lower()
        or part == ".codex"
        for part in path.parts
    ) and not ("backups" in path.parts and path.suffix == ".gpg")


def exclusive_json(path: Path, value: object):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as stream:
        stream.write(canonical(value) + b"\n")
        stream.flush()
        os.fsync(stream.fileno())


def copy_exclusive(source: Path, target: Path):
    target.parent.mkdir(parents=True, exist_ok=True)
    with source.open("rb") as incoming, target.open("xb") as outgoing:
        shutil.copyfileobj(incoming, outgoing)


def publish_packet(pending: Path, final: Path):
    if final.exists():
        raise FileExistsError("score packet already exists")
    pending.rename(final)


def bucket_candidate_ids(epoch, bugs):
    """The epoch decides whether matched siblings also leave adjudication."""
    return {
        m.candidate_id
        for bug in bugs
        for m in bug.matches
        if epoch.bucket_policy == "dedup_expanded" or m.kind == "direct"
    }


def candidate_records(
    candidates, attempts, tool="deep-review"
) -> tuple[Candidate, ...]:
    records = []
    for url, tools in candidates.items():
        for index, c in enumerate(tools.get(tool, [])):
            line = c.get("line")
            if type(line) is not int:
                try:
                    line = (
                        int(line)
                        if not isinstance(line, bool) and line is not None
                        else None
                    )
                except (TypeError, ValueError):
                    line = None
            if not isinstance(c["text"], str) or not c["text"]:
                raise ValueError("candidate text must be a nonempty string")
            identity = digest(
                [url, attempts[url], "delivered", index, c["text"], c.get("path"), line]
            )
            records.append(
                Candidate(
                    identity,
                    url,
                    attempts[url],
                    index,
                    c["text"],
                    c.get("path"),
                    line,
                    (),
                    ("unknown",),
                    "",
                    "legacy_unknown",
                )
            )
    return tuple(records)


def dedup_groups(raw, candidates, tool="deep-review") -> tuple[DedupGroup, ...]:
    groups = []
    for url in dict.fromkeys(c.pr_url for c in candidates):
        members = [c for c in candidates if c.pr_url == url]
        wire = raw.get(url, {}).get(tool, [])
        if not isinstance(wire, list):
            raise ValueError("invalid dedup groups")
        seen = set()
        completed = []
        for group in wire:
            if not isinstance(group, list) or not group:
                raise ValueError("invalid dedup group")
            for index in group:
                if (
                    type(index) is not int
                    or index < 0
                    or index >= len(members)
                    or index in seen
                ):
                    raise ValueError("overlapping or invalid dedup index")
                seen.add(index)
            completed.append(group)
        completed.extend([i] for i in range(len(members)) if i not in seen)
        for group in completed:
            ids = tuple(members[i].candidate_id for i in group)
            groups.append(DedupGroup(digest([url, ids]), url, ids))
    return tuple(groups)


def build_observations(
    request: ScoreRequest, evaluations, groups, *, failures=frozenset()
):
    observations = []
    candidates = request.candidates
    # Raw winning matches do not claim to be the full pairwise match matrix.
    for url in request.requested_pr_urls:
        bugs = [b for b in request.epoch.bugs if b.pr_url == url]
        ev = evaluations.get(url, {}).get(request.tool)
        if url in failures:
            ev = {"true_positives": [], "false_negatives": []}
        elif (
            not isinstance(ev, dict)
            or ev.get("skipped")
            or ev.get("errors")
            or ev.get("errors_count")
        ):
            raise ValueError("missing, skipped or errored PR evaluation")
        positive = ev.get("true_positives", [])
        negative = ev.get("false_negatives", [])
        if url not in failures:
            texts = [r.get("golden_comment") for r in positive + negative]
            if len(texts) != len(bugs) or set(texts) != {b.comment for b in bugs}:
                raise ValueError("incomplete or duplicate golden TP/FN coverage")
            if any(type(ev.get(k)) is not int or ev[k] < 0 for k in ("tp", "fp", "fn")):
                raise ValueError("invalid scorer aggregate counts")
            if ev["tp"] != len(positive) or ev["fn"] != len(negative):
                raise ValueError("scorer counts disagree with golden records")
            if ev["fp"] > sum(c.pr_url == url for c in candidates):
                raise ValueError("scorer false positives exceed candidate population")
        for bug in bugs:
            matches = []
            for row in positive:
                if row.get("golden_comment") != bug.comment:
                    continue
                if row.get("category", bug.category) != bug.category:
                    raise ValueError("evaluation category mismatch")
                direct = [
                    c
                    for c in candidates
                    if c.pr_url == url and c.text == row.get("matched_candidate")
                ]
                if len(direct) != 1:
                    raise ValueError("ambiguous or absent matched candidate text")
                winner = direct[0]
                confidence = row.get("confidence", 0.0)
                if not isinstance(confidence, (float, int)) or not 0 <= confidence <= 1:
                    raise ValueError("invalid match confidence")
                matches.append(
                    Match(
                        winner.candidate_id, confidence, row.get("reasoning"), "direct"
                    )
                )
                for group in groups:
                    if winner.candidate_id in group.candidate_ids:
                        matches.extend(
                            Match(
                                cid, confidence, row.get("reasoning"), "dedup_sibling"
                            )
                            for cid in group.candidate_ids
                            if cid != winner.candidate_id
                        )
            observations.append(
                BugObservation(
                    1,
                    request.score_ref,
                    url,
                    dict(request.selected_attempts)[url],
                    bug.bug_id,
                    bug.category,
                    "operational_failure"
                    if url in failures
                    else "matched"
                    if matches
                    else "missed",
                    tuple(matches),
                    (),
                    None,
                )
            )
    return tuple(observations)


def begin_packet(run_dir: Path, epoch: GoldenEpoch, score_id: str | None):
    score_id = score_id or uuid.uuid4().hex
    if len(score_id) != 32 or any(c not in "0123456789abcdef" for c in score_id):
        raise ValueError("score_id must be a UUID hex")
    parent = run_dir / "scores" / epoch.epoch_id
    parent.mkdir(parents=True, exist_ok=True)
    final = parent / score_id
    if final.exists():
        raise FileExistsError("score packet already exists")
    pending = parent / (".pending-" + score_id)
    pending.mkdir(exist_ok=False)
    return score_id, pending


def write_packet(
    request: ScoreRequest,
    *,
    packet: ScorePacketWire,
    observations,
    candidates,
    groups,
    adjudications,
    contexts,
    stage_observations=(),
    stage_matches=(),
) -> ScoreRef:
    pending = request.work_dir
    final = pending.parent / request.score_ref.score_id
    if final.exists():
        raise FileExistsError("score packet already exists")
    for name, value in (
        ("manifest.json", packet["manifest"]),
        ("candidate-map.json", [asdict(c) for c in candidates]),
        ("canonical-dedup-groups.json", [asdict(g) for g in groups]),
        ("scores.json", packet),
    ):
        exclusive_json(pending / name, value)
    for name, records in (
        ("bugs.jsonl", observations),
        ("adjudications.jsonl", adjudications),
        ("stages.jsonl", stage_observations),
        ("stage_matches.jsonl", stage_matches),
    ):
        with (pending / name).open("xb") as stream:
            for record in records:
                stream.write(canonical(asdict(record)) + b"\n")
    for context in contexts:
        exclusive_json(
            pending / "contexts" / (context.candidate_id + ".json"), asdict(context)
        )
    files = {
        p.relative_to(pending).as_posix(): file_hash(p)
        for p in sorted(pending.rglob("*"))
        if p.is_file()
    }
    exclusive_json(
        pending / "complete.json",
        {"schema_version": 1, "score_ref": asdict(request.score_ref), "files": files},
    )
    _load_packet(pending, request.epoch, allow_pending=True)
    publish_packet(pending, final)
    return request.score_ref


def load_packet(path: Path) -> ScorePacket:
    return _load_packet(Path(path))


def _load_packet(path: Path, epoch=None, *, allow_pending=False) -> ScorePacket:
    path = Path(path)
    if path.name.startswith(".pending-") and not allow_pending:
        raise ValueError("pending packet is not a score input")
    complete = read_json(path / "complete.json")
    if complete["schema_version"] != 1:
        raise ValueError("unsupported packet completion schema")
    actual = {
        p.relative_to(path).as_posix() for p in path.rglob("*") if p.is_file()
    } - {"complete.json"}
    required = {
        "manifest.json",
        "scores.json",
        "bugs.jsonl",
        "candidate-map.json",
        "candidates.json",
        "dedup_groups.json",
        "evaluations.json",
        "adjudications.jsonl",
        "stages.jsonl",
    }
    if not required <= actual:
        raise ValueError("incomplete packet inventory")
    if actual != set(complete["files"]):
        raise ValueError("packet file inventory mismatch")
    for name, expected in complete["files"].items():
        if file_hash(confined(path, name)) != expected:
            raise ValueError("packet hash mismatch")
    packet = read_json(path / "scores.json")
    manifest = read_json(path / "manifest.json")
    if packet["schema_version"] != 2 or manifest["schema_version"] != 2:
        raise ValueError("unsupported score packet schema")
    ref = ScoreRef(**complete["score_ref"])
    if not allow_pending and path.name != ref.score_id:
        raise ValueError("packet directory identity mismatch")
    if (
        manifest != packet["manifest"]
        or manifest["score_ref"] != asdict(ref)
        or packet["score_ref"] != asdict(ref)
        or packet["run_id"] != ref.run_id
        or manifest["completion_status"] != "complete"
    ):
        raise ValueError("packet identity mismatch")
    if any(key not in packet for key in ScorePacketWire.__annotations__):
        raise ValueError("incomplete packet schema")
    if manifest["run_validity_ref"] is not None:
        from bench.runner.validity import attempt_from_wire

        if manifest["run_validity_ref"] != "run-provenance.json":
            raise ValueError("unknown run validity reference")
        run = read_json(confined(path, manifest["run_validity_ref"]))
        if run.get("run_id") != ref.run_id or not run.get("provenance"):
            raise ValueError("score packet lost original run provenance")
        for url, aid in manifest["selected_attempts"].items():
            attempt = attempt_from_wire(
                read_json(confined(path, f"attempts/{aid}.json"))
            )
            if (
                attempt.attempt_id != aid
                or attempt.run_id != ref.run_id
                or attempt.pr_url != url
                or attempt.validity.state == "invalid"
            ):
                raise ValueError("score packet selected an invalid attempt")
    evaluator = dict(manifest["evaluator"])
    eid = evaluator.pop("evaluator_id")
    if eid != digest(evaluator):
        raise ValueError("evaluator identity mismatch")
    wires = [
        json.loads(line)
        for line in (path / "bugs.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    observations = tuple(
        BugObservation(
            **{
                **r,
                "score_ref": ScoreRef(**r["score_ref"]),
                "matches": tuple(Match(**m) for m in r["matches"]),
                "errors": tuple(r["errors"]),
                "loss": freeze(r["loss"]),
            }
        )
        for r in wires
    )
    if any(
        o.score_ref != ref or o.errors or o.outcome == "unavailable"
        for o in observations
    ):
        raise ValueError("invalid bug observations")
    epoch = epoch or load_epoch(ref.epoch_id, root=DEFAULT_GOLDEN_ROOT)
    if (
        evaluator["scorer_sha"] != epoch.scorer_sha
        or evaluator["scorer_sha256"] != epoch.scorer_sha256
    ):
        raise ValueError("packet evaluator/epoch mismatch")
    urls = manifest["requested_pr_urls"]
    if (
        len(urls) != len(set(urls))
        or set(urls) != set(manifest["selected_attempts"])
        or set(urls) - {b.pr_url for b in epoch.bugs}
    ):
        raise ValueError("packet requested population mismatch")
    expected = {
        (b.pr_url, b.bug_id, b.category)
        for b in epoch.bugs
        if b.pr_url in manifest["requested_pr_urls"]
    }
    if (
        len(observations) != len(expected)
        or {(o.pr_url, o.bug_id, o.category) for o in observations} != expected
    ):
        raise ValueError("packet golden coverage mismatch")
    if manifest["epoch_hashes"] != epoch_hashes(epoch):
        raise ValueError("packet epoch hashes mismatch")
    candidate_wires = read_json(path / "candidate-map.json")
    candidates = tuple(
        Candidate(
            **{
                **c,
                "source_trace_ids": tuple(c["source_trace_ids"]),
                "discovery_dimensions": tuple(c["discovery_dimensions"]),
            }
        )
        for c in candidate_wires
    )
    ids = {c.candidate_id for c in candidates}
    if len(ids) != len(candidates) or any(
        m.candidate_id not in ids for o in observations for m in o.matches
    ):
        raise ValueError("packet candidate join mismatch")
    raw_candidates = read_json(path / "candidates.json")
    if set(raw_candidates) != set(urls):
        raise ValueError("packet raw candidate population mismatch")
    rebuilt = candidate_records(raw_candidates, manifest["selected_attempts"])
    groups = dedup_groups(read_json(path / "dedup_groups.json"), rebuilt)
    group_ids = {cid: g.group_id for g in groups for cid in g.candidate_ids}
    if len(rebuilt) != len(candidates):
        raise ValueError("packet candidate map population mismatch")
    for original, stored in zip(rebuilt, candidates, strict=True):
        if (
            any(
                getattr(original, field) != getattr(stored, field)
                for field in (
                    "candidate_id",
                    "pr_url",
                    "attempt_id",
                    "index",
                    "text",
                    "path",
                    "line",
                )
            )
            or stored.dedup_group_id != group_ids[stored.candidate_id]
            or stored.attempt_id != manifest["selected_attempts"][stored.pr_url]
        ):
            raise ValueError("packet candidate identity mismatch")
    request = ScoreRequest(
        ref,
        epoch,
        path,
        tuple(urls),
        tuple(sorted(manifest["selected_attempts"].items())),
        candidates,
        "deep-review",
        Evaluator(evaluator_id=eid, **evaluator),
    )
    evaluations = read_json(path / "evaluations.json")
    failures = frozenset(
        o.pr_url for o in observations if o.outcome == "operational_failure"
    )
    if build_observations(request, evaluations, groups, failures=failures) != tuple(
        replace(o, loss=None) for o in observations
    ):
        raise ValueError("packet observations disagree with raw evaluations")
    fp = sum(evaluations[u]["deep-review"]["fp"] for u in urls if u not in failures)
    if packet["profiles"] != {
        p: profile_metrics(observations, fp, p).to_wire()
        for p in epoch.supported_profiles
    }:
        raise ValueError("packet profile arithmetic mismatch")
    primary = packet["profiles"][epoch.primary_profile]
    if (
        any(packet["metrics"].get(k) != v for k, v in primary.items())
        or packet["metrics"]["golden_recall"] != primary["recall"]
    ):
        raise ValueError("packet primary projection mismatch")
    for name, expected_hash in manifest["source_artifact_hashes"].items():
        if file_hash(confined(path / "sources", name)) != expected_hash:
            raise ValueError("packet source hash mismatch")
    from bench.runner.stages import validate_packet_evidence

    validate_packet_evidence(path, packet, observations, candidates)
    adjudications = tuple(
        Adjudication(**json.loads(line))
        for line in (path / "adjudications.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    )
    if any(a.error or a.evaluator_id != eid for a in adjudications):
        raise ValueError("invalid adjudicator observation")
    candidate_urls = {c.candidate_id: c.pr_url for c in candidates}
    if any(
        a.bucket not in ("valid_extra", "noise")
        or candidate_urls.get(a.candidate_id) != a.pr_url
        for a in adjudications
    ):
        raise ValueError("invalid adjudicator candidate identity")
    matched = bucket_candidate_ids(epoch, observations)
    if (
        len(adjudications) != len(ids - matched)
        or {a.candidate_id for a in adjudications} != ids - matched
    ):
        raise ValueError("packet candidate bucket coverage mismatch")
    for a in adjudications:
        ctx = read_json(path / "contexts" / (a.candidate_id + ".json"))
        if ctx["context_sha256"] != a.context_sha256 or a.context_sha256 != digest(
            [ctx["hunk"], ctx["file_context"], ctx["context_status"], ctx["truncated"]]
        ):
            raise ValueError("packet adjudication context mismatch")
    expected_row = {
        "schema_version": 2,
        "run_id": ref.run_id,
        "epoch_id": ref.epoch_id,
        "score_id": ref.score_id,
        "golden_sha256": epoch.golden_sha256,
        "profile": epoch.primary_profile,
        "denominator": primary["denominator"],
        "evaluator_id": eid,
        "population_sha256": digest(tuple(urls)),
    }
    if any(packet["ledger_row"].get(k) != v for k, v in expected_row.items()):
        raise ValueError("packet ledger projection identity mismatch")
    manifest_record = ScoreManifest(
        **{
            **manifest,
            "score_ref": ref,
            "evaluator": request.evaluator,
            "epoch_hashes": tuple(sorted(manifest["epoch_hashes"].items())),
            "requested_pr_urls": tuple(urls),
            "selected_attempts": request.selected_attempts,
            "source_artifact_hashes": tuple(
                sorted(manifest["source_artifact_hashes"].items())
            ),
            "profiles": tuple(manifest["profiles"]),
        }
    )
    metrics = packet["metrics"]
    metrics_record = RunMetrics(
        **{
            **metrics,
            "per_bucket": tuple(sorted(metrics["per_bucket"].items())),
            "per_dimension": tuple(
                (key, DimensionMetrics(**value))
                for key, value in sorted(metrics["per_dimension"].items())
            ),
            "drift": Drift(
                metrics["drift"]["desc_len_mean"],
                tuple(sorted(metrics["drift"]["severity_dist"].items())),
            ),
        }
    )
    return ScorePacket(
        2,
        manifest_record,
        ref.run_id,
        ref,
        tuple((p, ProfileMetrics(**v)) for p, v in sorted(packet["profiles"].items())),
        metrics_record,
        packet["observations_path"],
        packet["stages_path"],
        StageCapture(
            **(
                {
                    "enabled": False,
                    "status": "off"
                    if packet["stage_capture"]["status"] == "not_requested"
                    else "unavailable",
                    "reason": packet["stage_capture"].get("reason"),
                    "pairwise_call_count": 0,
                    "estimated_cost_usd": None,
                    "cost_cap_usd": None,
                    "price_schedule_sha256": None,
                    "actual_call_count": 0,
                    "actual_cost_usd": None,
                }
                if "enabled" not in packet["stage_capture"]
                else packet["stage_capture"]
            )
        ),
        packet["adjudications_path"],
        freeze(packet["ledger_row"]),
        packet["complete_path"],
        packet["judge_pin"],
        packet["adjudicator_pin"],
        packet["scorer_sha"],
        observations,
        candidates,
        adjudications,
        path.resolve(),
    )


DEFAULT_GOLDEN_ROOT = Path(__file__).resolve().parents[1] / "golden"


def epoch_hashes(epoch):
    return {
        k: getattr(epoch, k)
        for k in ("golden_sha256", "scorer_sha256", "bugs_sha256", "labels_sha256")
    }


def import_legacy(run_dir: Path, epoch: GoldenEpoch) -> LegacyArchive:
    """Preserve available historical bytes without fabricating per-bug results."""
    if epoch.epoch_id != "dfc6cb4-v1":
        raise ValueError("legacy import requires the old epoch")
    home = run_dir / "legacy" / epoch.epoch_id
    if (home / "complete.json").exists():
        marker = read_json(home / "complete.json")
        for name, expected in marker["files"].items():
            if file_hash(confined(home, name)) != expected:
                raise ValueError("legacy archive hash mismatch")
        wire = read_json(home / "archive.json")
    else:
        pending = home.parent / (".pending-" + uuid.uuid4().hex)
        pending.mkdir(parents=True, exist_ok=False)
        names = {
            "scores.json",
            "run.json",
            "latest-score.json",
            "candidates.json",
            "dedup_groups.json",
            "evaluations.json",
            "adjudications.jsonl",
            "post-review-payload.json",
            "raw.json",
            "raw-naive.json",
            "diff.patch",
            "findings.json",
            "checkpoint.json",
        }
        files = {}
        for source in sorted(run_dir.rglob("*")):
            relative = source.relative_to(run_dir)
            if (
                relative.parts[0] in ("legacy", "scores", "attempts")
                or source.name not in names
            ):
                continue
            if not artifact_allowed(relative):
                continue
            if not source.is_file() or source.is_symlink():
                continue
            target = pending / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            copy_exclusive(source, target)
            files[relative.as_posix()] = file_hash(target)
        missing = [
            name
            for name in ("evaluations.json", "dedup_groups.json")
            if not any(Path(f).name == name for f in files)
        ]
        if "scores.json" not in files:
            missing.append("scores_absent")
        aggregate = (
            read_json(pending / "scores.json") if "scores.json" in files else None
        )
        baseline_pins = read_json(epoch.root / "baselines.json")
        wire = {
            "schema_version": 1,
            "run_id": run_dir.name,
            "epoch_id": epoch.epoch_id,
            "archived_files": files,
            "unavailable_files": missing,
            "aggregate_scores": aggregate,
            "observations_available": False,
            "status": "partial",
            "source_score_id": (aggregate or {}).get("score_ref", {}).get("score_id"),
            "pins": {
                k: (aggregate or {}).get(k, baseline_pins.get(k))
                for k in ("judge_pin", "adjudicator_pin", "scorer_sha")
            },
            "per_profile_results": {"all": (aggregate or {}).get("metrics")},
        }
        exclusive_json(pending / "archive.json", wire)
        exclusive_json(
            pending / "complete.json",
            {"files": files | {"archive.json": file_hash(pending / "archive.json")}},
        )
        if home.exists():
            raise FileExistsError("legacy archive already exists")
        pending.rename(home)
    return LegacyArchive(
        **{
            **wire,
            "archived_files": tuple(sorted(wire["archived_files"].items())),
            "unavailable_files": tuple(wire["unavailable_files"]),
            "aggregate_scores": freeze(wire["aggregate_scores"]),
            "pins": tuple(sorted(wire["pins"].items())),
            "per_profile_results": freeze(wire["per_profile_results"]),
        }
    )

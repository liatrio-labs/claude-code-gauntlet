"""Canonical score evidence and immutable packet publication."""

from __future__ import annotations

import json
import os
import shutil
import uuid
from dataclasses import asdict, dataclass, fields, is_dataclass, replace
from pathlib import Path
from typing import Any, Literal

from bench.runner.decode import decode
from bench.runner.epochs import (
    Category,
    GoldenEpoch,
    canonical,
    confined,
    digest,
    file_hash,
    load_epoch,
    read_json,
)
from bench.runner.profiles import profile_metrics

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
    loss: dict[str, Any] | None


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
    scorer_tree: str
    harness_scoring_sha256: str
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


@dataclass(frozen=True, slots=True)
class LegacyScoreIdentity:
    run_id: str
    scorer_sha: str | None
    epoch_id: str | None
    profile: Literal["all"]
    source_line_sha256: str
    status: Literal["known_old", "legacy_unknown"]


@dataclass(frozen=True, slots=True)
class FrozenJson:
    text: str


def freeze_json(value):
    return FrozenJson(canonical(value).decode("utf-8"))


def thaw_json(value):
    return json.loads(value.text) if isinstance(value, FrozenJson) else value


def wire_record(value):
    """Serialize frozen records without turning declared wire maps into pair arrays."""
    if isinstance(value, FrozenJson):
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
    if isinstance(value, dict):
        return {k: wire_record(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [wire_record(v) for v in value]
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


def bucket_candidate_ids(bugs):
    return {m.candidate_id for bug in bugs for m in bug.matches}


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


@dataclass(frozen=True, slots=True)
class StoredScore:
    schema_version: Literal[3]
    manifest: ScoreManifest
    observations: tuple[BugObservation, ...]
    candidates: tuple[Candidate, ...]
    adjudications: tuple[Adjudication, ...]
    contexts: tuple[AdjudicationContext, ...]
    metadata: dict[str, Any]
    costs: dict[Literal["dedup", "judge", "adjudicator"], float | None]


@dataclass(frozen=True, slots=True)
class ScorePacket(StoredScore):
    root: Path

    @property
    def score_ref(self):
        return self.manifest.score_ref

    @property
    def run_id(self):
        return self.score_ref.run_id

    @property
    def judge_pin(self):
        return self.manifest.evaluator.judge_pin

    @property
    def adjudicator_pin(self):
        return self.manifest.evaluator.adjudicator_pin

    @property
    def stages_path(self):
        return "stages.jsonl"

    @property
    def ledger_row(self):
        return self.projection()["ledger_row"]

    def projection(self):
        from bench.runner import score, stages

        epoch = load_epoch(self.score_ref.epoch_id)
        raw = read_json(self.root / "evaluations.json")
        fp = sum(
            raw.get(u, {}).get("deep-review", {}).get("fp", 0)
            for u in self.manifest.requested_pr_urls
        )
        profiles = {
            p: asdict(profile_metrics(self.observations, fp, p))
            for p in epoch.supported_profiles
        }
        total = len(self.candidates)
        matched = bucket_candidate_ids(self.observations)
        counts = {"golden_matched": len(matched), "valid_extra": 0, "noise": 0}
        for a in self.adjudications:
            counts[a.bucket] += 1
        texts = [c.text for c in self.candidates]
        severity = {}
        for text in texts:
            key = score._severity_of(text)
            severity[key] = severity.get(key, 0) + 1
        traces = stages.packet_traces(self.root)
        stage_rows = stages.read_rows(self.root)
        metrics = {
            **profiles[epoch.primary_profile],
            "golden_recall": profiles[epoch.primary_profile]["recall"],
            "valid_extra_rate": counts["valid_extra"] / total if total else 0.0,
            "noise_rate": counts["noise"] / total if total else 0.0,
            "total_candidates": total,
            "n_prs": len(self.manifest.requested_pr_urls),
            "per_bucket": counts,
            "per_dimension": stages.dimension_metrics(
                self.candidates,
                self.observations,
                stage_rows,
                epoch.primary_profile,
                traces,
            ),
            "drift": {
                "desc_len_mean": sum(map(len, texts)) / total if total else 0.0,
                "severity_dist": severity,
            },
        }
        row = {
            **self.metadata,
            **{
                k: metrics[k]
                for k in (
                    "golden_recall",
                    "valid_extra_rate",
                    "noise_rate",
                    "precision_strict",
                    "f1_strict",
                    "per_bucket",
                    "per_dimension",
                    "drift",
                    "n_prs",
                )
            },
            "denominator": metrics["denominator"],
            "packet_path": str(self.root),
            "dedup_cost_usd": self.costs["dedup"],
            "judge_cost_usd": self.costs["judge"],
            "adjudicator_cost_usd": self.costs["adjudicator"],
        }
        return {
            "schema_version": 3,
            "score_ref": asdict(self.score_ref),
            "metrics": metrics,
            "profiles": profiles,
            "ledger_row": row,
        }


def _manifest_wire(value):
    return {
        **value,
        **{
            k: tuple(sorted(value[k].items()))
            for k in ("epoch_hashes", "selected_attempts", "source_artifact_hashes")
        },
    }


def write_packet(
    request,
    *,
    packet,
    observations,
    candidates,
    groups,
    adjudications,
    contexts,
    stage_observations=(),
    costs=None,
):
    pending = request.work_dir
    final = pending.parent / request.score_ref.score_id
    stored = StoredScore(
        3,
        decode(ScoreManifest, _manifest_wire(packet["manifest"])),
        tuple(observations),
        tuple(candidates),
        tuple(adjudications),
        tuple(contexts),
        packet["ledger_row"],
        costs or {"dedup": None, "judge": None, "adjudicator": None},
    )
    exclusive_json(pending / "scores.json", asdict(stored))
    with (pending / "stages.jsonl").open("xb") as stream:
        for row in stage_observations:
            stream.write(canonical(asdict(row)) + b"\n")
    exclusive_json(
        pending / "complete.json",
        {
            "files": {
                p.relative_to(pending).as_posix(): file_hash(p)
                for p in sorted(pending.rglob("*"))
                if p.is_file()
            }
        },
    )
    _load_packet(pending, request.epoch, allow_pending=True)
    publish_packet(pending, final)
    return request.score_ref


def load_packet(path):
    return _load_packet(Path(path))


def _load_packet(path, epoch=None, *, allow_pending=False):
    if path.name.startswith(".pending-") and not allow_pending:
        raise ValueError("pending packet is not a score input")
    receipt = read_json(path / "complete.json")["files"]
    actual = {
        p.relative_to(path).as_posix() for p in path.rglob("*") if p.is_file()
    } - {"complete.json"}
    required = {
        "scores.json",
        "evaluations.json",
        "candidates.json",
        "dedup_groups.json",
        "stages.jsonl",
        "stage_trace_bindings.json",
    }
    if not required <= actual or actual != set(receipt):
        raise ValueError("incomplete packet inventory")
    for name, expected in receipt.items():
        if file_hash(confined(path, name)) != expected:
            raise ValueError("packet hash mismatch")
    data = decode(StoredScore, read_json(path / "scores.json"))
    manifest = data.manifest
    epoch = epoch or load_epoch(manifest.score_ref.epoch_id)
    if (
        manifest.score_ref.epoch_id != epoch.epoch_id
        or dict(manifest.epoch_hashes) != epoch_hashes(epoch)
        or (not allow_pending and path.name != manifest.score_ref.score_id)
    ):
        raise ValueError("packet epoch or identity mismatch")
    evaluator = asdict(manifest.evaluator)
    identity = evaluator.pop("evaluator_id")
    if (
        digest(evaluator) != identity
        or evaluator["scorer_tree"] != epoch.scorer_tree
        or evaluator["judge_pin"] != epoch.judge_pin
    ):
        raise ValueError("evaluator identity or epoch pin mismatch")
    urls, attempts = manifest.requested_pr_urls, dict(manifest.selected_attempts)
    if (
        len(set(urls)) != len(urls)
        or set(attempts) != set(urls)
        or not set(urls) <= set(epoch.claims_by_pr)
    ):
        raise ValueError("invalid requested PR matrix")
    for name, expected in manifest.source_artifact_hashes:
        if file_hash(confined(path / "sources", name)) != expected:
            raise ValueError("evaluated source hash mismatch")
    candidates = read_json(path / "candidates.json")
    expected_candidates = candidate_records(candidates, attempts)
    if [
        (c.candidate_id, c.text, c.pr_url, c.index, c.attempt_id)
        for c in data.candidates
    ] != [
        (c.candidate_id, c.text, c.pr_url, c.index, c.attempt_id)
        for c in expected_candidates
    ]:
        raise ValueError("candidate receipt join mismatch")
    groups = dedup_groups(read_json(path / "dedup_groups.json"), data.candidates)
    group_ids = {cid: g.group_id for g in groups for cid in g.candidate_ids}
    if any(c.dedup_group_id != group_ids[c.candidate_id] for c in data.candidates):
        raise ValueError("candidate dedup join mismatch")
    request = ScoreRequest(
        manifest.score_ref,
        epoch,
        path,
        urls,
        manifest.selected_attempts,
        data.candidates,
        "deep-review",
        manifest.evaluator,
    )
    failures = frozenset(
        o.pr_url for o in data.observations if o.outcome == "operational_failure"
    )
    rebuilt = build_observations(
        request, read_json(path / "evaluations.json"), groups, failures=failures
    )
    if tuple(replace(o, loss=None) for o in data.observations) != rebuilt:
        raise ValueError("observation receipt join mismatch")
    matched = bucket_candidate_ids(data.observations)
    remaining = {
        c.candidate_id: c for c in data.candidates if c.candidate_id not in matched
    }
    contexts = {c.candidate_id: c for c in data.contexts}
    if (
        len({a.candidate_id for a in data.adjudications}) != len(data.adjudications)
        or {a.candidate_id for a in data.adjudications} != set(remaining)
        or set(contexts) != set(remaining)
    ):
        raise ValueError("incomplete adjudication coverage")
    for a in data.adjudications:
        c, context = remaining[a.candidate_id], contexts[a.candidate_id]
        if (
            a.pr_url != c.pr_url
            or a.evaluator_id != manifest.evaluator.evaluator_id
            or a.bucket not in ("noise", "valid_extra")
            or a.error
            or a.context_sha256 != context.context_sha256
            or context.context_sha256
            != digest(
                [
                    context.hunk,
                    context.file_context,
                    context.context_status,
                    context.truncated,
                ]
            )
        ):
            raise ValueError("adjudication context join mismatch")
    if set(data.costs) != {"dedup", "judge", "adjudicator"} or any(
        v is not None and v < 0 for v in data.costs.values()
    ):
        raise ValueError("invalid judge cost")
    from bench.runner.stages import validate_packet_evidence

    validate_packet_evidence(path, data)
    return ScorePacket(
        **{f.name: getattr(data, f.name) for f in fields(data)}, root=path
    )


def epoch_hashes(epoch):
    return {"golden_sha256": epoch.golden_sha256, "scorer_tree": epoch.scorer_tree}


ScoreRefWire = dict

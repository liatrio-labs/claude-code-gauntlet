"""Canonical score evidence and immutable packet publication."""

from __future__ import annotations

import json
import os
import shutil
import uuid
from collections import Counter
from dataclasses import asdict, astuple, dataclass, fields, is_dataclass, replace
from pathlib import Path
from typing import Any, Literal

from bench.runner.decode import decode
from bench.runner.epochs import (
    Category,
    GoldenEpoch,
    canonical,
    confined,
    digest,
    epoch_context,
    file_hash,
    from_context,
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
    source_trace_ids: tuple[str, ...] = ()
    discovery_dimensions: tuple[Dimension, ...] = ("unknown",)
    dedup_group_id: str = ""
    provenance_status: ProvenanceStatus = "legacy_unknown"


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
    bucket: Literal["noise", "valid_extra"]
    reasoning: str | None
    evaluator_id: str
    context_sha256: str


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
class TraceCollection:
    status: Literal["available", "unavailable"]
    reason: str | None


@dataclass(frozen=True, slots=True)
class ScoreManifest:
    score_ref: ScoreRef
    epoch_hashes: dict[str, str]
    evaluator: Evaluator
    requested_pr_urls: tuple[str, ...]
    selected_attempts: dict[str, str]
    source_artifact_hashes: dict[str, str]
    attribution: bool
    trace_collection: dict[str, TraceCollection]


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
            item = dict(item) if field.metadata.get("wire_mapping") else item
            result[field.name] = wire_record(item)
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
            try:
                line = (
                    int(line)
                    if line is not None and not isinstance(line, bool)
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
    siblings = {cid: g.candidate_ids for g in groups for cid in g.candidate_ids}
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
        by_golden = {row["golden_comment"]: row for row in positive}
        for bug in bugs:
            matches: tuple[Match, ...] = ()
            if row := by_golden.get(bug.comment):
                if row.get("category", bug.category) != bug.category:
                    raise ValueError("evaluation category mismatch")
                direct = [
                    c
                    for c in candidates
                    if c.pr_url == url and c.text == row.get("matched_candidate")
                ]
                if len(direct) != 1:
                    raise ValueError("ambiguous or absent matched candidate text")
                winner = direct[0].candidate_id
                confidence = row.get("confidence", 0.0)
                if not isinstance(confidence, (float, int)) or not 0 <= confidence <= 1:
                    raise ValueError("invalid match confidence")
                matches = tuple(
                    Match(
                        cid,
                        confidence,
                        row.get("reasoning"),
                        "direct" if cid == winner else "dedup_sibling",
                    )
                    for cid in siblings.get(winner, (winner,))
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
    costs: dict[Literal["dedup", "judge", "adjudicator", "attribution"], float | None]


@dataclass(frozen=True, slots=True)
class ScorePacket(StoredScore):
    root: Path

    @property
    def score_ref(self):
        return self.manifest.score_ref

    @property
    def run_id(self):
        return self.score_ref.run_id

    def projection(self):
        from bench.runner import stages

        epoch = from_context(read_json(self.root / "golden-context.json"))
        raw = read_json(self.root / "evaluations.json")
        fp = sum(
            raw.get(u, {}).get("deep-review", {}).get("fp", 0)
            for u in self.manifest.requested_pr_urls
        )
        profiles = {
            p: asdict(profile_metrics(self.observations, fp, p))
            for p in epoch.supported_profiles
        }
        traces = stages.packet_traces(self.root)
        stage_rows = stages.read_rows(self.root)
        metrics = {
            **profiles[epoch.primary_profile],
            "golden_recall": profiles[epoch.primary_profile]["recall"],
            **bucket_metrics(self.candidates, self.observations, self.adjudications),
            "per_dimension": stages.dimension_metrics(
                self.candidates,
                self.observations,
                stage_rows,
                epoch.primary_profile,
                traces,
            ),
        }
        row = {
            **self.metadata,
            **metrics,
            "packet_path": str(self.root),
            **{stage + "_cost_usd": value for stage, value in self.costs.items()},
        }
        return {
            "schema_version": 3,
            "score_ref": asdict(self.score_ref),
            "metrics": metrics,
            "profiles": profiles,
            "ledger_row": row,
        }


def bucket_metrics(candidates, observations, adjudications):
    from bench.runner.score import _severity_of

    total = len(candidates)
    counts = dict.fromkeys(("golden_matched", "valid_extra", "noise"), 0)
    counts.update(Counter(a.bucket for a in adjudications))
    counts["golden_matched"] = len(bucket_candidate_ids(observations))
    severity = dict(Counter(_severity_of(c.text) for c in candidates))
    return {
        "valid_extra_rate": counts["valid_extra"] / total if total else 0.0,
        "noise_rate": counts["noise"] / total if total else 0.0,
        "total_candidates": total,
        "n_prs": len({o.pr_url for o in observations}),
        "per_bucket": counts,
        "drift": {
            "desc_len_mean": sum(len(c.text) for c in candidates) / total
            if total
            else 0.0,
            "severity_dist": severity,
        },
    }


def write_packet(
    request,
    *,
    manifest,
    metadata,
    observations,
    adjudications,
    contexts,
    stage_observations=(),
    costs,
):
    pending = request.work_dir
    final = pending.parent / request.score_ref.score_id
    stored = StoredScore(
        3,
        manifest,
        tuple(observations),
        request.candidates,
        tuple(adjudications),
        tuple(contexts),
        metadata,
        costs,
    )
    exclusive_json(pending / "golden-context.json", epoch_context(request.epoch))
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
    epoch = epoch or from_context(read_json(path / "golden-context.json"))
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
        or evaluator["adjudicator_pin"] != epoch.adjudicator_pin
    ):
        raise ValueError("evaluator identity or epoch pin mismatch")
    urls, attempts = manifest.requested_pr_urls, dict(manifest.selected_attempts)
    if (
        len(set(urls)) != len(urls)
        or set(attempts) != set(urls)
        or not set(urls) <= set(epoch.scorer_input)
    ):
        raise ValueError("invalid requested PR matrix")
    for name, expected in manifest.source_artifact_hashes.items():
        if file_hash(confined(path / "sources", name)) != expected:
            raise ValueError("evaluated source hash mismatch")
    candidates = read_json(path / "candidates.json")
    expected_candidates = candidate_records(candidates, attempts)
    if [astuple(c)[:7] for c in data.candidates] != [
        astuple(c)[:7] for c in expected_candidates
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
        tuple(sorted(manifest.selected_attempts.items())),
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
    if set(data.costs) != {"dedup", "judge", "adjudicator", "attribution"} or any(
        v is not None and v < 0 for v in data.costs.values()
    ):
        raise ValueError("invalid judge cost")
    from bench.runner.stages import validate_packet_evidence

    validate_packet_evidence(path, data, epoch)
    return ScorePacket(
        **{f.name: getattr(data, f.name) for f in fields(data)}, root=path
    )


def epoch_hashes(epoch):
    return {"golden_sha256": epoch.golden_sha256, "scorer_tree": epoch.scorer_tree}

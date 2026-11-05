"""Canonical score evidence and immutable packet publication."""

from __future__ import annotations

import uuid
from collections import Counter
from dataclasses import asdict, dataclass, field, fields, is_dataclass, replace
from pathlib import Path
from typing import Literal, TypedDict, get_args

from bench.runner.contracts import LedgerRow, ScoreRef, exclusive_json
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
ContextStatus = Literal[
    "head_and_hunk", "hunk_only", "full_diff", "missing", "file_only_unconfirmed"
]


Stage = Literal[
    "discover",
    "merge",
    "verify",
    "validate",
    "filter",
    "admission",
    "challenge",
    "cite",
    "dispose",
    "holdback",
    "select",
    "deliver",
]
OPTIONAL_STAGES: frozenset[Stage] = frozenset(
    ("admission", "cite", "dispose", "holdback")
)
REQUIRED_STAGES = tuple(
    stage
    for stage in get_args(Stage)
    if stage not in OPTIONAL_STAGES and stage != "deliver"
)
LossStage = Literal[
    "not_found",
    "verify",
    "validate",
    "challenge",
    "cap",
    "merge",
    "filter",
    "delivery",
    "cite",
    "dispose",
    "unknown",
]
Decision = Literal[
    "kept",
    "eliminated",
    "coalesced",
    "held_back",
    "not_selected",
    "body_routed",
    "report_only",
    "demoted",
]
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
    "body_routed",
    "report_only",
    "held_back",
    "folded",
    "demoted",
    "refuted",
    "out_of_scope",
    "scope_contradicted",
    "cite_unmatched",
    "cite_unavailable",
    "unknown",
]
Coverage = Literal["complete", "partial", "unavailable"]
Surface = Literal["inline", "body"]


class SurfaceItemWire(TypedDict):
    surface: Surface
    text: str
    path: str | None
    line: int | None
    issue_id: str | None
    source_trace_ids: list[str]
    discovery_dimensions: list[Dimension]
    test_claim: bool | None
    test_site: bool | None


class AvailableSurfaceViewsWire(TypedDict):
    schema_version: Literal[1]
    all_surface: Literal["surface-all/view.json"]
    unavailable_reason: None


class UnavailableSurfaceViewsWire(TypedDict):
    schema_version: Literal[1]
    all_surface: None
    unavailable_reason: str


SurfaceViewsWire = AvailableSurfaceViewsWire | UnavailableSurfaceViewsWire


def surface_views_receipt(view, reason) -> SurfaceViewsWire:
    if view is not None:
        return {
            "schema_version": 1,
            "all_surface": "surface-all/view.json",
            "unavailable_reason": None,
        }
    return {
        "schema_version": 1,
        "all_surface": None,
        "unavailable_reason": reason or "original body evidence unavailable",
    }


@dataclass(frozen=True, slots=True)
class Loss:
    stage: LossStage
    status: Literal["established", "unavailable", "ambiguous"]
    physical_stage: Stage | None = None
    reason_code: ReasonCode = "unknown"
    reason: str = "attribution unavailable"
    trace_ids: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()

    def __post_init__(self):
        if self.status == "established" and (
            self.stage == "unknown"
            or self.physical_stage is None
            or (self.stage != "not_found" and not self.trace_ids)
        ):
            raise ValueError("established loss requires a known stage and evidence")


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
    surface: Surface = "inline"
    issue_id: str | None = None
    test_claim: bool | None = None
    test_site: bool | None = None

    def __post_init__(self):
        if self.surface == "body" and not self.issue_id:
            raise ValueError("body candidate requires explicit issue identity")

    def wire_fields(self, *, identity=False):
        names = [f.name for f in fields(self)]
        if identity:
            names = [
                "candidate_id",
                "pr_url",
                "attempt_id",
                "index",
                "text",
                "path",
                "line",
            ]
            if self.surface != "inline":
                names.append("surface")
        elif self.surface == "inline" and self.issue_id is None:
            names = [name for name in names if name not in ("surface", "issue_id")]
        return {name: getattr(self, name) for name in names}

    def identity(self):
        return self.wire_fields(identity=True)


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
    loss: Loss | None


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
    context_sha256: str = field(init=False)

    def __post_init__(self):
        object.__setattr__(
            self,
            "context_sha256",
            digest([self.hunk, self.file_context, self.context_status, self.truncated]),
        )


@dataclass(frozen=True, slots=True)
class Evaluator:
    judge_pin: str
    adjudicator_pin: str
    scorer_tree: str
    harness_scoring_sha256: str
    temperature: float
    evaluator_id: str = field(init=False)

    def __post_init__(self):
        identity = {f.name: getattr(self, f.name) for f in fields(self) if f.init}
        object.__setattr__(self, "evaluator_id", digest(identity))


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


def wire_record(value):
    """Serialize dataclasses and nested containers to JSON-compatible values."""
    if is_dataclass(value):
        serializer = getattr(value, "wire_fields", None)
        record = (
            serializer()
            if serializer
            else {f.name: getattr(value, f.name) for f in fields(value)}
        )
        return wire_record(record)
    if isinstance(value, dict):
        return {k: wire_record(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [wire_record(v) for v in value]
    return value


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
            record = Candidate(
                "",
                url,
                attempts[url],
                index,
                c["text"],
                c.get("path"),
                line,
                source_trace_ids=tuple(c.get("source_trace_ids", ())),
                discovery_dimensions=tuple(c.get("discovery_dimensions", ("unknown",))),
                provenance_status=c.get("provenance_status", "legacy_unknown"),
                surface=c.get("surface", "inline"),
                issue_id=c.get("issue_id"),
                test_claim=c.get("test_claim"),
                test_site=c.get("test_site"),
            )
            identity = record.identity()
            identity = digest(
                [url, attempts[url], "delivered", index, c["text"], c.get("path"), line]
                + ([identity["surface"]] if "surface" in identity else [])
            )
            records.append(replace(record, candidate_id=identity))
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
    metadata: LedgerRow
    costs: dict[Literal["dedup", "judge", "adjudicator", "attribution"], float | None]


@dataclass(frozen=True, slots=True)
class SurfaceScoreView:
    evaluator_id: str
    candidates: tuple[Candidate, ...]
    observations: tuple[BugObservation, ...]
    adjudications: tuple[Adjudication, ...]
    contexts: tuple[AdjudicationContext, ...]
    costs: dict[Literal["dedup", "judge", "adjudicator"], float | None]
    source_artifact_hashes: dict[str, str]


def _merge_tristate(left: bool | None, right: bool | None) -> bool | None:
    if left or right:
        return True
    if left is None or right is None:
        return None
    return False


def surface_candidates(mapped):
    """One explicit issue credit, preferring its inline occurrence."""
    mapped = tuple(mapped)
    seen, result = {}, []
    pr_order = {url: i for i, url in enumerate(dict.fromkeys(c.pr_url for c in mapped))}
    for c in sorted(mapped, key=lambda c: (pr_order[c.pr_url], c.surface != "inline")):
        if c.issue_id is None:
            raise ValueError("all-surface scoring requires explicit issue identity")
        key = (c.pr_url, c.issue_id)
        if key not in seen:
            seen[key] = len(result)
            result.append(c)
        else:
            index = seen[key]
            previous = result[index]
            result[index] = replace(
                previous,
                test_claim=_merge_tristate(previous.test_claim, c.test_claim),
                test_site=_merge_tristate(previous.test_site, c.test_site),
                discovery_dimensions=tuple(
                    sorted(set(previous.discovery_dimensions + c.discovery_dimensions))
                ),
                source_trace_ids=tuple(
                    dict.fromkeys(previous.source_trace_ids + c.source_trace_ids)
                ),
            )
    return tuple(result)


def union_surface_observations(bugs, inline, candidates, inline_candidates):
    """Scorer competition cannot erase an established inline bug hit."""
    by_issue = {(c.pr_url, c.issue_id): c.candidate_id for c in candidates}
    originals = {c.candidate_id: c for c in inline_candidates}
    canonical = {(b.pr_url, b.bug_id): b for b in inline}
    result = []
    for b in bugs:
        previous = canonical[b.pr_url, b.bug_id]
        matches = list(b.matches)
        for match in previous.matches:
            c = originals[match.candidate_id]
            cid = by_issue.get((c.pr_url, digest(sorted(c.source_trace_ids))))
            if cid is None:
                raise ValueError("inline issue missing from all-surface population")
            if cid not in {m.candidate_id for m in matches}:
                matches.append(replace(match, candidate_id=cid))
        result.append(
            replace(
                b, outcome="matched" if matches else b.outcome, matches=tuple(matches)
            )
        )
    return tuple(result)


@dataclass(frozen=True, slots=True)
class ScorePacket(StoredScore):
    root: Path
    surface_view: SurfaceScoreView | None = None

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
        all_surface = None
        if self.surface_view is not None:
            view = self.surface_view
            matched = bucket_candidate_ids(view.observations)
            groups = dedup_groups(
                read_json(self.root / "surface-all/dedup_groups.json"),
                view.candidates,
            )
            fp_by_pr = Counter(
                g.pr_url for g in groups if not matched.intersection(g.candidate_ids)
            )
            fp = sum(fp_by_pr[u] for u in self.manifest.requested_pr_urls)
            all_surface = {
                "profiles": {
                    p: asdict(profile_metrics(view.observations, fp, p))
                    for p in epoch.supported_profiles
                },
                "metrics": bucket_metrics(
                    view.candidates, view.observations, view.adjudications
                ),
                "costs": view.costs,
            }
        return {
            "schema_version": 3,
            "score_ref": asdict(self.score_ref),
            "metrics": metrics,
            "profiles": profiles,
            "ledger_row": {**self.ledger_row(), **metrics},
            "surface_views": {
                "all": all_surface,
            },
        }

    def ledger_row(self):
        return {
            **self.metadata,
            "packet_path": str(self.root),
            **{stage + "_cost_usd": value for stage, value in self.costs.items()},
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
    surface_view=None,
    surface_unavailable_reason="original body evidence unavailable",
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
    exclusive_json(pending / "scores.json", wire_record(stored))
    if surface_view is not None:
        exclusive_json(pending / "surface-all/view.json", wire_record(surface_view))
    receipt = surface_views_receipt(surface_view, surface_unavailable_reason)
    exclusive_json(pending / "surface-views.json", receipt)
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
    evaluator = manifest.evaluator
    if (
        evaluator.scorer_tree != epoch.scorer_tree
        or evaluator.judge_pin != epoch.judge_pin
        or evaluator.adjudicator_pin != epoch.adjudicator_pin
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
    if [c.identity() for c in data.candidates] != [
        c.identity() for c in expected_candidates
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
        ):
            raise ValueError("adjudication context join mismatch")
    if set(data.costs) != {"dedup", "judge", "adjudicator", "attribution"} or any(
        v is not None and v < 0 for v in data.costs.values()
    ):
        raise ValueError("invalid judge cost")
    from bench.runner.stages import validate_packet_evidence

    surface_view = None
    if "surface-views.json" in actual:
        surface_receipt = decode(
            SurfaceViewsWire, read_json(path / "surface-views.json")
        )
        target = surface_receipt["all_surface"]
        if target is not None:
            surface_view = decode(SurfaceScoreView, read_json(confined(path, target)))
            validate_surface_view(
                path, data.observations, request, surface_view, failures
            )
        elif not surface_receipt["unavailable_reason"]:
            raise ValueError("missing surface unavailability reason")
        elif "surface-all/view.json" in actual:
            raise ValueError("unreferenced surface view")
    elif any(p.startswith("surface-all/") for p in actual):
        raise ValueError("missing surface receipt")
    validate_packet_evidence(path, data, epoch, surface_view=surface_view)
    return ScorePacket(
        **{f.name: getattr(data, f.name) for f in fields(data)},
        root=path,
        surface_view=surface_view,
    )


def validate_surface_view(path, inline_bugs, request, view, failures):
    for name, expected in view.source_artifact_hashes.items():
        if file_hash(confined(path / "surface-all/sources", name)) != expected:
            raise ValueError("surface source hash mismatch")
    raw = read_json(path / "surface-all/candidates.json")
    rebuilt = candidate_records(raw, dict(request.selected_attempts))
    if (
        set(raw) != set(request.requested_pr_urls)
        or any(set(tools) != {"deep-review"} for tools in raw.values())
        or tuple(replace(c, dedup_group_id="") for c in view.candidates) != rebuilt
        or view.evaluator_id != request.evaluator.evaluator_id
    ):
        raise ValueError("surface candidate or evaluator join mismatch")
    if surface_candidates(view.candidates) != view.candidates:
        raise ValueError("duplicate surface issue credit")
    groups = dedup_groups(
        read_json(path / "surface-all/dedup_groups.json"), view.candidates
    )
    gids = {cid: g.group_id for g in groups for cid in g.candidate_ids}
    if any(c.dedup_group_id != gids[c.candidate_id] for c in view.candidates):
        raise ValueError("surface dedup join mismatch")
    rebuilt_bugs = build_observations(
        replace(request, candidates=view.candidates),
        read_json(path / "surface-all/evaluations.json"),
        groups,
        failures=failures,
    )
    rebuilt_bugs = union_surface_observations(
        rebuilt_bugs, inline_bugs, view.candidates, request.candidates
    )
    if view.observations != rebuilt_bugs:
        raise ValueError("surface observation join mismatch")
    remaining = {
        c.candidate_id: c
        for c in view.candidates
        if c.candidate_id not in bucket_candidate_ids(view.observations)
    }
    contexts = {c.candidate_id: c for c in view.contexts}
    if (
        len(view.adjudications) != len(remaining)
        or len(view.contexts) != len(remaining)
        or {a.candidate_id for a in view.adjudications} != set(remaining)
        or set(contexts) != set(remaining)
    ):
        raise ValueError("surface adjudication coverage mismatch")
    for a in view.adjudications:
        if (
            a.pr_url != remaining[a.candidate_id].pr_url
            or a.evaluator_id != view.evaluator_id
            or a.context_sha256 != contexts[a.candidate_id].context_sha256
        ):
            raise ValueError("surface context join mismatch")
    if set(view.costs) != {"dedup", "judge", "adjudicator"} or any(
        v is not None and v < 0 for v in view.costs.values()
    ):
        raise ValueError("invalid surface scoring cost")


def epoch_hashes(epoch):
    return {"golden_sha256": epoch.golden_sha256, "scorer_tree": epoch.scorer_tree}

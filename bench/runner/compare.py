"""Complete paired comparisons and the promotion evaluator used by planning simulations."""

from __future__ import annotations

import argparse
import math
import random
import sys
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Literal, TypedDict, get_args

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bench.runner import observations, populations, stages, validity  # noqa: E402
from bench.runner.epochs import (  # noqa: E402
    DEFAULT_ROOT,
    Category,
    Profile,
    digest,
    load_epoch,
    read_json,
)
from bench.runner.observations import (  # noqa: E402
    ScoreRef,
    freeze_json,
    thaw_json,
)
from bench.runner.observations import wire_record as wire  # noqa: E402
from bench.runner.populations import Population, PopulationName  # noqa: E402
from bench.runner.validity import (  # noqa: E402
    CellSelection,
    CompleteMatrix,
    MatrixObservation,
)

Look = Literal["initial", "extended", "descriptive"]
Verdict = Literal["pass", "fail", "inconclusive"]
StabilityLabel = Literal["stable_found", "stable_missed", "flaky", "insufficient_runs"]
GuardName = Literal["primary_recall", "non-mini", "groundedness", "stable_loss"]
GUARDS = get_args(GuardName)


class BootstrapConfigWire(TypedDict):
    draws: int
    seed: int
    alpha: float


@dataclass(frozen=True, slots=True)
class BootstrapConfig:
    draws: int = 10000
    seed: int = 0
    alpha: float = 0.05

    def __post_init__(self):
        if (
            type(self.draws) is not int
            or self.draws <= 0
            or not 0 < self.alpha < 0.5
            or type(self.seed) is not int
        ):
            raise ValueError("invalid bootstrap configuration")


class PromotionRuleWire(TypedDict):
    rule_id: str
    frozen: bool
    profile: Profile
    margin_pp: float
    nonmini_margin_pp: float
    initial_runs: int
    extension_runs: int
    stability_min_runs: int
    groundedness_margin_pp: float
    groundedness_sample_size: int
    extension_policy: Literal["inconclusive_only", "none"]
    bootstrap: BootstrapConfigWire


class PromotionRuleTemplateWire(TypedDict):
    rule_id: str | None
    frozen: Literal[False]
    profile: Profile | None
    margin_pp: float | None
    nonmini_margin_pp: float | None
    initial_runs: int | None
    extension_runs: int | None
    stability_min_runs: int | None
    groundedness_margin_pp: float | None
    groundedness_sample_size: int | None
    extension_policy: Literal["inconclusive_only", "none"] | None
    bootstrap: BootstrapConfigWire | None


@dataclass(frozen=True, slots=True)
class PromotionRule:
    rule_id: str
    frozen: bool
    profile: Profile
    margin_pp: float
    nonmini_margin_pp: float
    initial_runs: int
    extension_runs: int
    stability_min_runs: int
    groundedness_margin_pp: float
    groundedness_sample_size: int
    extension_policy: Literal["inconclusive_only", "none"]
    bootstrap: BootstrapConfig

    def __post_init__(self):
        if (
            not self.rule_id
            or type(self.frozen) is not bool
            or self.profile not in get_args(Profile)
            or any(
                type(v) is not int or v < 1
                for v in (
                    self.initial_runs,
                    self.stability_min_runs,
                    self.groundedness_sample_size,
                )
            )
            or type(self.extension_runs) is not int
            or self.extension_runs < 0
            or self.extension_policy not in ("inconclusive_only", "none")
            or (self.extension_policy == "none" and self.extension_runs != 0)
            or (
                self.extension_policy == "inconclusive_only" and not self.extension_runs
            )
            or any(
                type(v) not in (int, float) or not math.isfinite(v)
                for v in (
                    self.margin_pp,
                    self.nonmini_margin_pp,
                    self.groundedness_margin_pp,
                )
            )
        ):
            raise ValueError("unset or invalid promotion rule")


def rule_from_wire(value):
    return PromotionRule(
        **{**value, "bootstrap": BootstrapConfig(**value["bootstrap"])}
    )


class ArmWire(TypedDict):
    name: str
    scores: list[dict[str, str]]
    treatment_git_sha: str
    agent_read_sha256: str


@dataclass(frozen=True, slots=True)
class Arm:
    name: str
    scores: tuple[ScoreRef, ...]
    treatment_git_sha: str
    agent_read_sha256: str


class GroundednessFindingWire(TypedDict):
    sample_id: str
    score_ref: dict[str, str]
    candidate_id: str
    pr_url: str
    correct: bool | None
    status: Literal["agreed", "owner_resolved", "disputed", "pending"]
    evidence_refs: list[str]


@dataclass(frozen=True, slots=True)
class GroundednessFinding:
    sample_id: str
    score_ref: ScoreRef
    candidate_id: str
    pr_url: str
    correct: bool | None
    status: Literal["agreed", "owner_resolved", "disputed", "pending"]
    evidence_refs: tuple[str, ...]


class GroundednessAuditWire(TypedDict):
    schema_version: Literal[1]
    audit_id: str
    sampling_seed: int
    protocol_id: str
    blinded: bool
    stage: Literal["initial", "extended"]
    baseline: list[GroundednessFindingWire]
    candidate: list[GroundednessFindingWire]


@dataclass(frozen=True, slots=True)
class GroundednessAudit:
    schema_version: Literal[1]
    audit_id: str
    sampling_seed: int
    protocol_id: str
    blinded: bool
    stage: Literal["initial", "extended"]
    baseline: tuple[GroundednessFinding, ...]
    candidate: tuple[GroundednessFinding, ...]
    census_size: int | None = None
    census_evidence_refs: tuple[str, ...] = ()


def load_audit(path):
    return audit_from_wire(read_json(Path(path)))


def audit_from_wire(value) -> GroundednessAudit:
    value = dict(value)
    for arm in ("baseline", "candidate"):
        value[arm] = tuple(
            GroundednessFinding(
                **{
                    **v,
                    "score_ref": ScoreRef(**v["score_ref"]),
                    "evidence_refs": tuple(v["evidence_refs"]),
                }
            )
            for v in value[arm]
        )
    if "census_evidence_refs" in value:
        value["census_evidence_refs"] = tuple(value["census_evidence_refs"])
    return GroundednessAudit(**value)


class ComparisonSpecWire(TypedDict):
    schema_version: Literal[1]
    comparison_id: str
    window_path: str | None
    descriptive_pr_urls: list[str] | None
    baseline: ArmWire
    candidate: ArmWire
    epoch_id: str
    profile: Profile
    contamination_path: str
    populations_path: str
    populations: list[PopulationName]
    rule: PromotionRuleWire | None
    groundedness_audit_path: str | None
    look: Look


@dataclass(frozen=True, slots=True)
class ComparisonSpec:
    schema_version: Literal[1]
    comparison_id: str
    window_path: str | None
    descriptive_pr_urls: tuple[str, ...] | None
    baseline: Arm
    candidate: Arm
    epoch_id: str
    profile: Profile
    contamination_path: str
    populations_path: str
    populations: tuple[PopulationName, ...]
    rule: PromotionRule | None
    groundedness_audit_path: str | None
    look: Look
    initial_report_path: str | None = None
    resolved_populations: tuple[Population, ...] = ()


def load_comparison_spec(path: Path) -> ComparisonSpec:
    path = Path(path).resolve()
    value = read_json(path)
    for arm in ("baseline", "candidate"):
        row = value[arm]
        value[arm] = Arm(
            **{**row, "scores": tuple(ScoreRef(**r) for r in row["scores"])}
        )
    value["rule"] = rule_from_wire(value["rule"]) if value["rule"] is not None else None
    value["populations"] = tuple(value["populations"])
    value["descriptive_pr_urls"] = (
        tuple(value["descriptive_pr_urls"])
        if value["descriptive_pr_urls"] is not None
        else None
    )
    for path_field in (
        "window_path",
        "contamination_path",
        "populations_path",
        "groundedness_audit_path",
        "initial_report_path",
    ):
        if value.get(path_field):
            value[path_field] = str((path.parent / value[path_field]).resolve())
    result = ComparisonSpec(**value)
    if (
        result.schema_version != 1
        or result.look not in get_args(Look)
        or result.profile not in get_args(Profile)
    ):
        raise ValueError("unsupported comparison schema/look/profile")
    if (
        result.baseline.name == result.candidate.name
        or not result.populations
        or len(set(result.populations)) != len(result.populations)
    ):
        raise ValueError("duplicate arms/populations")
    return result


class IntervalWire(TypedDict):
    method: str
    draws: int
    seed: int
    alpha: float
    cluster_count: int
    lower: float | None
    upper: float | None
    two_sided_lower: float | None
    two_sided_upper: float | None
    status: Literal["estimated", "empty", "insufficient_clusters"]


@dataclass(frozen=True, slots=True)
class Interval:
    method: str
    draws: int
    seed: int
    alpha: float
    cluster_count: int
    lower: float | None
    upper: float | None
    two_sided_lower: float | None
    two_sided_upper: float | None
    status: Literal["estimated", "empty", "insufficient_clusters"]


class BugComparisonWire(TypedDict):
    bug_id: str
    pr_url: str
    category: Category | None
    included_population_names: list[PopulationName]
    baseline_hits: int
    candidate_hits: int
    n_baseline: int
    n_candidate: int
    baseline_rate: float
    candidate_rate: float
    difference: float
    baseline_label: StabilityLabel
    candidate_label: StabilityLabel
    stable_loss: bool
    source_dimension_hits: object
    stage_outcomes: object


@dataclass(frozen=True, slots=True)
class BugComparison:
    bug_id: str
    pr_url: str
    category: Category | None
    included_population_names: tuple[PopulationName, ...]
    baseline_hits: int
    candidate_hits: int
    n_baseline: int
    n_candidate: int
    baseline_rate: float
    candidate_rate: float
    difference: float
    baseline_label: StabilityLabel
    candidate_label: StabilityLabel
    stable_loss: bool
    source_dimension_hits: observations.FrozenJson | None = None
    stage_outcomes: observations.FrozenJson | None = None


class PopulationComparisonWire(TypedDict):
    population: PopulationName
    profile: Profile
    population_sha256: str
    pr_urls: list[str]
    denominator: int
    n_runs_by_arm: dict[str, int]
    baseline_recall: float | None
    candidate_recall: float | None
    difference: float | None
    interval: IntervalWire
    bugs: list[BugComparisonWire]
    category_rows: object
    dimension_rows: object
    groundedness_rows: object
    baseline_funnels: object
    candidate_funnels: object


@dataclass(frozen=True, slots=True)
class PopulationComparison:
    population: PopulationName
    profile: Profile
    population_sha256: str
    pr_urls: tuple[str, ...]
    denominator: int
    n_runs_by_arm: tuple[tuple[str, int], ...] = field(metadata={"wire_mapping": True})
    baseline_recall: float | None
    candidate_recall: float | None
    difference: float | None
    interval: Interval
    bugs: tuple[BugComparison, ...]
    category_rows: observations.FrozenJson
    dimension_rows: observations.FrozenJson
    groundedness_rows: observations.FrozenJson
    baseline_funnels: observations.FrozenJson
    candidate_funnels: observations.FrozenJson


class GuardWire(TypedDict):
    verdict: Verdict
    evidence: list[str]


@dataclass(frozen=True, slots=True)
class Guard:
    verdict: Verdict
    evidence: tuple[str, ...]


class PromotionDecisionWire(TypedDict):
    verdict: Verdict
    look: Look
    guards: dict[GuardName, GuardWire]
    stable_loss_bug_ids: list[str]
    extension_allowed: bool
    reasons: list[str]


@dataclass(frozen=True, slots=True)
class PromotionDecision:
    verdict: Verdict
    look: Look
    guards: tuple[tuple[GuardName, Guard], ...] = field(metadata={"wire_mapping": True})
    stable_loss_bug_ids: tuple[str, ...]
    extension_allowed: bool
    reasons: tuple[str, ...]


class ComparisonReportWire(TypedDict):
    schema_version: Literal[1]
    comparison_id: str
    status: Literal["computed", "refused"]
    refusal_codes: list[str]
    details: list[str]
    input_score_refs: list[dict[str, str]]
    identity_hashes: object
    population_reports: list[PopulationComparisonWire]
    rule: PromotionRuleWire | None
    promotion: PromotionDecisionWire | None
    descriptive_reasons: list[str]
    limitations: list[str]
    cost_duration_diagnostics: object


@dataclass(frozen=True, slots=True)
class ComparisonReport:
    schema_version: Literal[1]
    comparison_id: str
    status: Literal["computed", "refused"]
    refusal_codes: tuple[str, ...]
    details: tuple[str, ...]
    input_score_refs: tuple[ScoreRef, ...]
    identity_hashes: observations.FrozenJson
    population_reports: tuple[PopulationComparison, ...]
    rule: PromotionRule | None
    promotion: PromotionDecision | None
    descriptive_reasons: tuple[str, ...]
    limitations: tuple[str, ...]
    cost_duration_diagnostics: observations.FrozenJson
    look: Look = "descriptive"
    initial_decision: PromotionDecision | None = None
    initial_score_refs: tuple[ScoreRef, ...] = ()
    supplemental_all: tuple[PopulationComparison, ...] = ()
    groundedness_audit: GroundednessAudit | None = None
    initial_report_sha256: str | None = None


def derived_seed(master, *parts):
    return int(digest([master, *parts])[:16], 16)


def quantile(values, q):
    h = (len(values) - 1) * q
    low = math.floor(h)
    return values[low] + (values[math.ceil(h)] - values[low]) * (h - low)


def cluster_bootstrap(
    bugs: Sequence[BugComparison], config: BootstrapConfig
) -> Interval:
    clusters: dict[str, list[float]] = {}
    for bug in sorted(bugs, key=lambda b: (b.pr_url, b.bug_id)):
        clusters.setdefault(bug.pr_url, []).append(bug.difference)
    packed = [(sum(v), len(v)) for _, v in sorted(clusters.items())]
    m = len(packed)
    if not m:
        return Interval(
            "paired_pr_cluster_percentile",
            config.draws,
            config.seed,
            config.alpha,
            0,
            None,
            None,
            None,
            None,
            "empty",
        )
    rng = random.Random(config.seed)
    replicates = []
    for _ in range(config.draws):
        sample = [packed[rng.randrange(m)] for _ in range(m)]
        replicates.append(sum(s for s, _ in sample) / sum(n for _, n in sample))
    replicates.sort()
    return Interval(
        "paired_pr_cluster_percentile",
        config.draws,
        config.seed,
        config.alpha,
        m,
        quantile(replicates, config.alpha),
        quantile(replicates, 1 - config.alpha),
        quantile(replicates, 0.025),
        quantile(replicates, 0.975),
        "estimated" if m >= 2 else "insufficient_clusters",
    )


def stability(hits, n, minimum):
    if minimum is None or n < minimum:
        return "insufficient_runs"
    return "stable_found" if hits == n else "stable_missed" if not hits else "flaky"


def _scoped_population(pop, scope):
    clusters = tuple((p, ids) for p, ids in pop.nonempty_pr_clusters if p in scope)
    ids = tuple(sorted(b for _, ids in clusters for b in ids))
    return replace(
        pop,
        bug_ids=ids,
        pr_urls=tuple(sorted(set(pop.pr_urls) & set(scope))),
        nonempty_pr_clusters=clusters,
        denominator=len(ids),
        content_sha256=digest([pop.content_sha256, sorted(scope), ids]),
    )


def resolved_populations(spec):
    if spec.resolved_populations:
        return spec.resolved_populations, ()
    epoch = load_epoch(spec.epoch_id, root=DEFAULT_ROOT)
    subsets = epoch.subsets
    contamination, manifest = (
        read_json(Path(spec.contamination_path)),
        read_json(Path(spec.populations_path)),
    )
    result, limitations = [], []
    for name in spec.populations:
        try:
            population = populations.resolve_population(
                name, epoch, spec.profile, subsets, contamination, manifest
            )
        except ValueError as exc:
            if (
                spec.look != "descriptive"
                or name == "gate"
                or name not in get_args(PopulationName)
            ):
                raise
            limitations.append(f"{name} unavailable: {exc}")
            continue
        if spec.look == "descriptive":
            population = _scoped_population(population, spec.descriptive_pr_urls)
        result.append(population)
    return tuple(result), tuple(limitations)


def compare_matrix(matrix: CompleteMatrix, spec: ComparisonSpec) -> ComparisonReport:
    epoch = load_epoch(spec.epoch_id, root=DEFAULT_ROOT)
    metadata = {b.bug_id: b for b in epoch.bugs}
    pops, limitations = resolved_populations(spec)
    names = (spec.baseline.name, spec.candidate.name)
    counts = {a: len(indices) for a, indices in matrix.run_indices_by_arm}
    if any(not counts.get(a) for a in names):
        raise ValueError("no independent review runs")
    index = {}
    for row in matrix.observations:
        key = (row.arm, row.run_index, row.pr_url, row.bug_id)
        if key in index or type(row.detected) is not bool:
            raise ValueError("duplicate/non-Boolean bug observation")
        index[key] = row
    bugs = []
    all_ids = sorted({b for pop in pops for b in pop.bug_ids})
    for bid in all_ids:
        bug = metadata[bid]
        rows = {}
        for arm in names:
            rows[arm] = [
                index[(arm, r, bug.pr_url, bid)]
                for r in dict(matrix.run_indices_by_arm)[arm]
            ]
        hits = [sum(r.detected for r in rows[a]) for a in names]
        ns = [counts[a] for a in names]
        labels = [
            stability(h, n, spec.rule.stability_min_runs if spec.rule else None)
            for h, n in zip(hits, ns, strict=True)
        ]
        dimensions = {
            a: {
                "delivered": {
                    d: sum(d in r.delivered_dimensions and r.detected for r in rows[a])
                    for d in get_args(observations.Dimension)
                },
                "discovery": {
                    d: sum(d in r.discovery_dimensions for r in rows[a])
                    for d in get_args(observations.Dimension)
                },
                "available_runs": sum(r.provenance_available for r in rows[a]),
            }
            for a in names
        }
        outcomes = {
            a: [
                {
                    "run_index": r.run_index,
                    "outcomes": dict(r.stage_outcomes),
                    "status": "available" if r.provenance_available else "unavailable",
                }
                for r in rows[a]
            ]
            for a in names
        }
        bugs.append(
            BugComparison(
                bid,
                bug.pr_url,
                bug.category,
                tuple(p.name for p in pops if bid in p.bug_ids),
                hits[0],
                hits[1],
                ns[0],
                ns[1],
                hits[0] / ns[0],
                hits[1] / ns[1],
                hits[1] / ns[1] - hits[0] / ns[0],
                labels[0],
                labels[1],
                labels[0] == "stable_found" and hits[1] == 0,
                freeze_json(dimensions),
                freeze_json(outcomes),
            )
        )
    reports = []
    config = spec.rule.bootstrap if spec.rule else BootstrapConfig()
    for pop in pops:
        members = tuple(b for b in bugs if b.bug_id in pop.bug_ids)
        denominator = len(members)

        def mean(field, members=members, denominator=denominator):
            return (
                sum(getattr(b, field) for b in members) / denominator
                if denominator
                else None
            )

        categories = []
        for category in (*get_args(Category), None):
            subset = [b for b in members if b.category == category]
            categories.append(
                {
                    "category": category,
                    "denominator": len(subset),
                    "baseline_recall": sum(b.baseline_rate for b in subset)
                    / len(subset)
                    if subset
                    else None,
                    "candidate_recall": sum(b.candidate_rate for b in subset)
                    / len(subset)
                    if subset
                    else None,
                }
            )
        dimension_rows = []
        for d in get_args(observations.Dimension):
            for arm in names:
                population_rows = [
                    r
                    for r in matrix.observations
                    if r.arm == arm and r.bug_id in pop.bug_ids
                ]
                dimension_rows.append(
                    {
                        "dimension": d,
                        "arm": arm,
                        "denominator": denominator,
                        "delivered_recall": sum(
                            r.detected and d in r.delivered_dimensions
                            for r in population_rows
                        )
                        / (counts[arm] * denominator)
                        if denominator
                        and all(r.provenance_available for r in population_rows)
                        else None,
                        "discovery_recall": sum(
                            d in r.discovery_dimensions for r in population_rows
                        )
                        / (counts[arm] * denominator)
                        if denominator
                        and all(r.provenance_available for r in population_rows)
                        else None,
                        "known_provenance_cells": sum(
                            r.provenance_available for r in population_rows
                        ),
                        "overlap": True,
                    }
                )
        funnels = []
        for arm in names:
            runs = []
            for r in dict(matrix.run_indices_by_arm)[arm]:
                selected = [
                    s for s in matrix.selections if s.arm == arm and s.run_index == r
                ]
                refs = {s.score_ref for s in selected}
                sr = [s for s in matrix.stage_observations if s.score_ref in refs]
                funnel = stages.loss_funnel(sr, population=pop.bug_ids)
                terminal = [
                    s for s in sr if s.stage == "deliver" and s.bug_id in pop.bug_ids
                ]
                physical: dict[str, int] = {}
                causal: dict[str, int] = {}
                drilldown: dict[str, list[str]] = {}
                for cell in terminal:
                    if cell.loss and cell.loss.status == "established":
                        physical[cell.loss.physical_stage or "unknown"] = (
                            physical.get(cell.loss.physical_stage or "unknown", 0) + 1
                        )
                        causal[cell.loss.cause_stage or "unknown"] = (
                            causal.get(cell.loss.cause_stage or "unknown", 0) + 1
                        )
                        drilldown.setdefault(cell.loss.stage, []).append(cell.bug_id)
                runs.append(
                    {
                        "run_index": r,
                        **asdict(funnel),
                        "physical_losses": physical,
                        "causal_losses": causal,
                        "bug_ids_by_loss": drilldown,
                        "operational_failure_pr_urls": [
                            s.pr_url
                            for s in selected
                            if s.attempt
                            and s.attempt.validity.state == "operational_failure"
                        ],
                    }
                )
            means = {
                k: sum(row[k] for row in runs) / len(runs)
                for k in (
                    "denominator",
                    "delivered_hits",
                    "discovered_hits",
                    "unavailable_count",
                )
            }
            funnels.append(freeze_json({"runs": runs, "means": means}))
        reports.append(
            PopulationComparison(
                pop.name,
                spec.profile,
                pop.content_sha256,
                pop.pr_urls,
                denominator,
                tuple(counts.items()),
                mean("baseline_rate"),
                mean("candidate_rate"),
                mean("difference"),
                cluster_bootstrap(
                    members, replace(config, seed=derived_seed(config.seed, pop.name))
                ),
                members,
                freeze_json(categories),
                freeze_json(dimension_rows),
                freeze_json(
                    {
                        "status": "unavailable",
                        "reason": "repository-backed audit required",
                        "overlap": True,
                    }
                ),
                funnels[0],
                funnels[1],
            )
        )
    scope = spec.descriptive_pr_urls
    return ComparisonReport(
        1,
        spec.comparison_id,
        "computed",
        (),
        (),
        spec.baseline.scores + spec.candidate.scores,
        freeze_json(
            {
                "epoch_id": epoch.epoch_id,
                "golden_sha256": epoch.golden_sha256,
                "profile": spec.profile,
                "scope_pr_urls": list(scope) if scope else None,
                "scope_sha256": digest(sorted(scope)) if scope else None,
            }
        ),
        tuple(reports),
        spec.rule,
        None,
        ("no measurement window",)
        if spec.look == "descriptive" and not spec.window_path
        else (),
        (
            *limitations,
            "Category/dimension results are descriptive; dimension contributions overlap.",
        ),
        freeze_json({"status": "unavailable"}),
        spec.look,
    )


def evaluate_promotion(
    report: ComparisonReport, audit: GroundednessAudit | None, rule: PromotionRule
) -> PromotionDecision:
    populations_by_name = {p.population: p for p in report.population_reports}
    guards: list[tuple[GuardName, Guard]] = []
    verdict: Verdict
    recall_guards: tuple[tuple[GuardName, PopulationName, float], ...] = (
        ("primary_recall", "uncontaminated", rule.margin_pp),
        ("non-mini", "non-mini", rule.nonmini_margin_pp),
    )
    for guard, name, margin in recall_guards:
        pop = populations_by_name.get(name)
        interval = pop.interval if pop else None
        if (
            not interval
            or interval.status != "estimated"
            or interval.lower is None
            or interval.upper is None
        ):
            guards.append(
                (
                    guard,
                    Guard(
                        "inconclusive", ("at least two eligible PR clusters required",)
                    ),
                )
            )
        else:
            threshold = margin / 100
            verdict = (
                "pass"
                if interval.lower > threshold
                else "fail"
                if interval.upper <= threshold
                else "inconclusive"
            )
            guards.append(
                (
                    guard,
                    Guard(
                        verdict,
                        (
                            f"lower={interval.lower}; upper={interval.upper}; margin={threshold}",
                        ),
                    ),
                )
            )
    verdict, evidence = "inconclusive", ("missing blinded repository-backed audit",)
    if audit is not None:
        size = rule.groundedness_sample_size
        if audit.census_size is not None:
            if not audit.census_evidence_refs or not 0 < audit.census_size <= size:
                raise ValueError("invalid predeclared census protocol")
            size = audit.census_size
        labels = (*audit.baseline, *audit.candidate)
        resolved = (
            audit.schema_version == 1
            and audit.blinded
            and audit.protocol_id
            and audit.audit_id
            and audit.stage == report.look
            and len(audit.baseline) == len(audit.candidate) == size
            and len({v.sample_id for v in labels}) == len(labels)
            and all(
                type(v.correct) is bool
                and v.status in ("agreed", "owner_resolved")
                and v.evidence_refs
                for v in labels
            )
        )
        if resolved:
            difference = (
                sum(v.correct is True for v in audit.candidate)
                - sum(v.correct is True for v in audit.baseline)
            ) / size
            verdict = (
                "pass" if difference >= rule.groundedness_margin_pp / 100 else "fail"
            )
            evidence = (
                f"correct fraction difference={difference}; margin={rule.groundedness_margin_pp / 100}; n={size}",
            )
    guards.append(("groundedness", Guard(verdict, evidence)))
    primary = populations_by_name.get("uncontaminated")
    losses = tuple(b.bug_id for b in primary.bugs if b.stable_loss) if primary else ()
    sufficient = (
        primary
        and primary.denominator
        and all(
            min(b.n_baseline, b.n_candidate) >= rule.stability_min_runs
            for b in primary.bugs
        )
    )
    guards.append(
        (
            "stable_loss",
            Guard(
                "fail" if losses else "pass" if sufficient else "inconclusive",
                losses or ("observed stability only",),
            ),
        )
    )
    verdict = (
        "fail"
        if any(g.verdict == "fail" for _, g in guards)
        else "pass"
        if all(g.verdict == "pass" for _, g in guards)
        else "inconclusive"
    )
    extension = (
        report.look == "initial"
        and verdict == "inconclusive"
        and rule.extension_policy == "inconclusive_only"
        and rule.extension_runs > 0
    )
    return PromotionDecision(verdict, report.look, tuple(guards), losses, extension, ())


def default_packet_loader(ref):
    if any(
        not isinstance(s, str) or not s or Path(s).name != s or s in (".", "..")
        for s in (ref.run_id, ref.epoch_id, ref.score_id)
    ):
        raise ValueError("invalid score reference")
    return observations.load_packet(
        ROOT
        / "bench/workspace/runs"
        / ref.run_id
        / "scores"
        / ref.epoch_id
        / ref.score_id
    )


def _packet_run(packet):
    path = packet.root / "run-provenance.json"
    if path.is_file():
        return read_json(path)
    run = packet.root.parents[2] / "run.json"
    return read_json(run) if run.is_file() else {}


def load_selected_attempt(packet, url, aid):
    run_dir = packet.root.parents[2]
    paths = list((run_dir / "attempts").glob(f"*/{aid}/attempt.json"))
    if len(paths) != 1:
        raise ValueError("immutable selected attempt missing")
    path = paths[0]
    expected = dict(packet.manifest.source_artifact_hashes).get(
        str(path.relative_to(run_dir))
    )
    if expected != validity.file_hash(path):
        raise ValueError("selected attempt differs from immutable score evidence")
    attempt = validity.selected_attempt(path)
    if attempt.run_id != packet.run_id or attempt.pr_url != url:
        raise ValueError("selected attempt run/PR identity mismatch")
    receipt = run_dir / "failure-confirmations" / (aid + ".json")
    if receipt.is_file() and dict(packet.manifest.source_artifact_hashes).get(
        str(receipt.relative_to(run_dir))
    ) != validity.file_hash(receipt):
        raise ValueError("failure confirmation differs from score evidence")
    current = [
        row
        for p in (run_dir / "state").glob("*.json")
        if (row := read_json(p)).get("url") == url
    ]
    if len(current) != 1 or (current[0].get("detail") or {}).get("attempt_id") != aid:
        raise ValueError("score does not select the current attempt")
    return attempt


def _stage_rows(packet):
    return stages.read_rows(packet.root)


def compare_arms(
    spec: ComparisonSpec, *, packet_loader=default_packet_loader
) -> ComparisonReport:
    operational = spec.look != "descriptive"
    if operational and (
        not spec.window_path
        or not spec.rule
        or not spec.rule.frozen
        or spec.rule.profile != spec.profile
    ):
        raise ValueError(
            "operational comparison requires a window and complete frozen rule"
        )
    if not operational and (spec.rule is not None or not spec.descriptive_pr_urls):
        raise ValueError("descriptive comparison requires explicit scope and rule=null")
    window = (
        validity.load_window(Path(spec.window_path))
        if operational and spec.window_path is not None
        else None
    )
    epoch = load_epoch(spec.epoch_id, root=DEFAULT_ROOT)
    subsets = epoch.subsets
    scope = window.pr_urls if window else (spec.descriptive_pr_urls or ())
    if (
        len(scope) != len(set(scope))
        or set(scope) & set(subsets["holdout"])
        or set(scope) - {b.pr_url for b in epoch.bugs}
    ):
        raise ValueError("invalid or sealed requested scope")
    if operational and set(scope) != set(subsets["gate"]):
        raise ValueError("operational looks require the full 15-PR gate")
    if operational:
        populations.check_baseline_binding(
            read_json(Path(spec.contamination_path)),
            spec.baseline.treatment_git_sha,
            spec.baseline.agent_read_sha256,
        )
    if not operational and len(scope) == 6 and set(scope) != set(subsets["mini"]):
        raise ValueError("six-PR descriptive scope must be the declared mini")
    pops, limitations = resolved_populations(spec)
    if operational and not {"uncontaminated", "non-mini"} <= {p.name for p in pops}:
        raise ValueError("required promotion populations missing")
    selections, cells, stage_rows, run_ids, identity, run_index_map = (
        [],
        [],
        [],
        set(),
        None,
        [],
    )
    diagnostics, provenance_limitations = [], []
    instrumentation = None
    for arm in (spec.baseline, spec.candidate):
        if not arm.scores:
            raise ValueError("arm has no review runs")
        indices = []
        for order, ref in enumerate(arm.scores):
            packet = packet_loader(ref)
            if (
                packet.score_ref != ref
                or ref.run_id in run_ids
                or ref.epoch_id != epoch.epoch_id
            ):
                raise ValueError(
                    "duplicate review run or packet identity/epoch mismatch"
                )
            run_ids.add(ref.run_id)
            if dict(packet.manifest.epoch_hashes) != observations.epoch_hashes(epoch):
                raise ValueError("packet epoch content mismatch")
            evaluator = packet.manifest.evaluator
            if identity is None:
                identity = evaluator
            elif evaluator != identity:
                raise ValueError("mixed evaluator identities")
            if not validity.same_pr_set(packet.manifest.requested_pr_urls, scope):
                raise ValueError("missing/extra PR in selected run")
            expected = {b.bug_id for b in epoch.bugs if b.pr_url in scope}
            by_bug = {b.bug_id: b for b in packet.observations}
            if len(by_bug) != len(packet.observations) or set(by_bug) != expected:
                raise ValueError("incomplete/duplicate bug observations")
            if any(
                b.outcome not in ("matched", "missed", "operational_failure")
                or b.errors
                for b in packet.observations
            ):
                raise ValueError("invalid/unavailable/score-time error observation")
            run = _packet_run(packet)
            index = run.get("run_index") if operational else order
            indices.append(index)
            if operational:
                assert window is not None and spec.window_path is not None
                if (
                    validity.check_window(
                        window,
                        validity.fingerprint_from_wire(run["provenance"]),
                        arm=arm.name,
                        now=run["started"],
                    ).state
                    != "valid"
                    or not validity.same_pr_set(run.get("pr_urls") or (), scope)
                    or run.get("window_id") != window.window_id
                    or run.get("window_sha256")
                    != validity.file_hash(Path(spec.window_path))
                    or run.get("arm") != arm.name
                    or not run.get("instrumentation_version")
                    or run.get("treatment_git_sha", run.get("git_sha"))
                    != arm.treatment_git_sha
                    or run.get("agent_read_sha256") != arm.agent_read_sha256
                    or run.get("pr_identities") != {u: epoch.shas[u] for u in scope}
                ):
                    raise ValueError("review window/treatment/PR identity mismatch")
                if arm == spec.baseline:
                    receipt = run.get("baseline_equivalence_receipt")
                    if (
                        not receipt
                        or receipt.get("release_sha") != "839b1886"
                        or receipt.get("merge_sha") != arm.treatment_git_sha
                        or not receipt.get("evidence_refs")
                    ):
                        raise ValueError("missing Wave 0a merge equivalence receipt")
                if (
                    instrumentation is not None
                    and run.get("instrumentation_version") != instrumentation
                ):
                    raise ValueError("mixed instrumentation")
                instrumentation = run.get("instrumentation_version")
            else:
                provenance_limitations.append(
                    "Historical review identity missing/differing; descriptive scores cannot repair provenance."
                )
            sr = _stage_rows(packet)
            versions_by_id: dict[tuple[str, str], stages.FindingVersion] = {}
            bindings_path = packet.root / "stage_trace_bindings.json"
            if bindings_path.is_file():
                for url, binding in read_json(bindings_path).items():
                    trace = stages.load_trace(
                        observations.confined(packet.root, binding["trace_path"]),
                        expected_sha256=binding["trace_sha256"],
                    )
                    versions_by_id.update(
                        {(url, v.version_id): v for v in trace.versions}
                    )
            stage_rows.extend(sr)
            selected_attempts = dict(packet.manifest.selected_attempts)
            selected_validities = {}
            for url in scope:
                aid = selected_attempts.get(url)
                if not aid:
                    raise ValueError("missing selected attempt")
                attempt = None
                if operational:
                    attempt = load_selected_attempt(packet, url, aid)
                    selected_validities[url] = attempt.validity.state
                selection = CellSelection(
                    arm.name,
                    index,
                    url,
                    aid,
                    ref,
                    validity.failure_confirmation_ref(attempt)
                    if attempt and attempt.validity.state == "operational_failure"
                    else None,
                    attempt,
                )
                selections.append(selection)
            for b in packet.observations:
                if b.attempt_id != selected_attempts[b.pr_url]:
                    raise ValueError("bug selected attempt mismatch")
                if operational and (b.outcome == "operational_failure") != (
                    selected_validities[b.pr_url] == "operational_failure"
                ):
                    raise ValueError(
                        "operational failure must zero and label the entire PR cell"
                    )
                per_stage = [s for s in sr if s.bug_id == b.bug_id]
                discovery = next((s for s in per_stage if s.stage == "discover"), None)
                matched_versions = tuple(
                    versions_by_id[(b.pr_url, vid)]
                    for vid in (discovery.matched_version_ids if discovery else ())
                    if (b.pr_url, vid) in versions_by_id
                )
                cells.append(
                    MatrixObservation(
                        arm.name,
                        index,
                        b.pr_url,
                        b.bug_id,
                        b.outcome == "matched",
                        ref.run_id,
                        b.attempt_id,
                        stages.credited_dimensions(b, packet.candidates),
                        tuple(
                            sorted({v.discovery_dimension for v in matched_versions})
                        ),
                        tuple((s.stage, s.survives) for s in per_stage),
                        bool(
                            discovery
                            and discovery.coverage == "complete"
                            and discovery.survives is not None
                        ),
                    )
                )
            diagnostics.append(
                {
                    "score_ref": asdict(ref),
                    "review_attempts": selected_attempts,
                    "review_cost_usd": packet.metadata.get("cost_usd"),
                    "duration_s": packet.metadata.get("duration_s"),
                    "judge_diagnostics": {
                        "buckets": packet.projection()["metrics"]["per_bucket"],
                        "noise_rate": packet.projection()["metrics"]["noise_rate"],
                        "valid_extra_rate": packet.projection()["metrics"][
                            "valid_extra_rate"
                        ],
                    },
                }
            )
        if len(indices) != len(set(indices)):
            raise ValueError("duplicate run index")
        run_index_map.append((arm.name, tuple(sorted(indices))))
    if operational:
        assert spec.rule is not None and window is not None
        count = spec.rule.initial_runs + (
            spec.rule.extension_runs if spec.look == "extended" else 0
        )
        prefix = tuple(window.planned_run_indices[:count])
        if len(prefix) != count:
            raise ValueError("window lacks planned prefix")
        selections = [
            replace(
                s,
                observations=tuple(
                    r
                    for r in cells
                    if (r.arm, r.run_index, r.pr_url) == (s.arm, s.run_index, s.pr_url)
                ),
            )
            for s in selections
        ]
        matrix = validity.validate_matrix(
            selections, window, pops, epoch=epoch, expected_run_indices=prefix
        )
        if any(len(indices) != count for _, indices in run_index_map):
            raise ValueError("unexpected initial/extended run count")
    else:
        matrix = CompleteMatrix(
            tuple(selections),
            (),
            (spec.baseline.name, spec.candidate.name),
            tuple(run_index_map),
        )
    matrix = replace(
        matrix, observations=tuple(cells), stage_observations=tuple(stage_rows)
    )
    report = compare_matrix(matrix, replace(spec, resolved_populations=pops))
    if spec.profile != "all":
        supplemental_spec = replace(
            spec, profile="all", rule=None, resolved_populations=()
        )
        supplemental, _ = resolved_populations(supplemental_spec)
        report = replace(
            report,
            supplemental_all=compare_matrix(
                matrix, replace(supplemental_spec, resolved_populations=supplemental)
            ).population_reports,
        )
    identity_hashes = thaw_json(report.identity_hashes)
    assert isinstance(identity_hashes, dict) and identity is not None
    report = replace(
        report,
        identity_hashes=freeze_json(
            {
                **identity_hashes,
                "epoch_hashes": observations.epoch_hashes(epoch),
                "evaluator": asdict(identity),
                "population_hashes": {p.name: p.content_sha256 for p in pops},
                "contamination_sha256": digest(
                    read_json(Path(spec.contamination_path))
                ),
                "populations_sha256": digest(read_json(Path(spec.populations_path))),
                "window_sha256": validity.file_hash(Path(spec.window_path))
                if window and spec.window_path is not None
                else None,
                "fingerprint": asdict(window.expected) if window else None,
                "instrumentation_version": instrumentation,
                "treatments": {
                    a.name: {
                        "git_sha": a.treatment_git_sha,
                        "agent_read_sha256": a.agent_read_sha256,
                    }
                    for a in (spec.baseline, spec.candidate)
                },
            }
        ),
        limitations=report.limitations
        + limitations
        + tuple(sorted(set(provenance_limitations))),
        cost_duration_diagnostics=freeze_json(
            {
                "sources": diagnostics,
                "review_cost_policy": "count each attempt once; retries remain in immutable attempt history",
            }
        ),
    )
    if operational:
        assert spec.rule is not None and window is not None
        audit = (
            load_audit(spec.groundedness_audit_path)
            if spec.groundedness_audit_path
            else None
        )
        if audit:
            candidates = {}
            for arm in (spec.baseline, spec.candidate):
                candidates[arm.name] = {
                    (ref, c.candidate_id, c.pr_url)
                    for ref in arm.scores
                    for c in packet_loader(ref).candidates
                }
            for arm, findings in (
                (spec.baseline, audit.baseline),
                (spec.candidate, audit.candidate),
            ):
                if any(
                    (f.score_ref, f.candidate_id, f.pr_url) not in candidates[arm.name]
                    for f in findings
                ):
                    raise ValueError(
                        "audit finding is not a delivered candidate in this arm"
                    )
            candidate_map = {
                (ref, candidate.candidate_id): candidate
                for arm in (spec.baseline, spec.candidate)
                for ref in arm.scores
                for candidate in packet_loader(ref).candidates
            }
            updated = []
            for population in report.population_reports:
                rows = []
                eligible_prs = {b.pr_url for b in population.bugs}
                for arm, findings in (
                    (spec.baseline, audit.baseline),
                    (spec.candidate, audit.candidate),
                ):
                    for dimension in get_args(observations.Dimension):
                        sampled = [
                            f
                            for f in findings
                            if f.pr_url in eligible_prs
                            and dimension
                            in (
                                candidate_map[
                                    (f.score_ref, f.candidate_id)
                                ].discovery_dimensions
                                or ("unknown",)
                            )
                        ]
                        resolved = [
                            f
                            for f in sampled
                            if type(f.correct) is bool
                            and f.status in ("agreed", "owner_resolved")
                        ]
                        rows.append(
                            {
                                "arm": arm.name,
                                "dimension": dimension,
                                "audited_count": len(sampled),
                                "correct_count": sum(f.correct for f in resolved),
                                "unresolved_count": len(sampled) - len(resolved),
                                "groundedness": sum(f.correct for f in resolved)
                                / len(resolved)
                                if resolved
                                else None,
                                "overlap": True,
                                "audit_id": audit.audit_id,
                            }
                        )
                updated.append(replace(population, groundedness_rows=freeze_json(rows)))
            report = replace(report, population_reports=tuple(updated))
        decision = evaluate_promotion(report, audit, spec.rule)
        report = replace(report, promotion=decision, groundedness_audit=audit)
        if spec.look == "extended":
            if not spec.initial_report_path:
                raise ValueError("extended look requires the immutable initial report")
            initial = read_json(Path(spec.initial_report_path))
            initial_refs = tuple(ScoreRef(**r) for r in initial["input_score_refs"])
            expected_refs = (
                spec.baseline.scores[: spec.rule.initial_runs]
                + spec.candidate.scores[: spec.rule.initial_runs]
            )
            if (
                initial.get("rule") != wire(spec.rule)
                or initial.get("look") != "initial"
                or not initial.get("promotion", {}).get("extension_allowed")
                or initial_refs != expected_refs
            ):
                raise ValueError("forbidden extension or substituted initial prefix")
            initial_spec = replace(
                spec,
                look="initial",
                baseline=replace(
                    spec.baseline, scores=spec.baseline.scores[: spec.rule.initial_runs]
                ),
                candidate=replace(
                    spec.candidate,
                    scores=spec.candidate.scores[: spec.rule.initial_runs],
                ),
            )
            prefix_matrix = replace(
                matrix,
                observations=tuple(
                    c
                    for c in cells
                    if c.run_index
                    in window.planned_run_indices[: spec.rule.initial_runs]
                ),
                run_indices_by_arm=tuple(
                    (a, tuple(window.planned_run_indices[: spec.rule.initial_runs]))
                    for a in matrix.arms
                ),
            )
            initial_report = compare_matrix(prefix_matrix, initial_spec)
            initial_audit = (
                audit_from_wire(initial["groundedness_audit"])
                if initial.get("groundedness_audit")
                else None
            )
            recomputed_initial = evaluate_promotion(
                initial_report, initial_audit, spec.rule
            )
            if (
                initial.get("promotion") != wire(recomputed_initial)
                or not recomputed_initial.extension_allowed
                or initial.get("identity_hashes") != wire(report.identity_hashes)
            ):
                raise ValueError("initial report identity mismatch")
            report = replace(
                report,
                initial_score_refs=initial_refs,
                initial_report_sha256=validity.file_hash(
                    Path(spec.initial_report_path)
                ),
                initial_decision=PromotionDecision(
                    **{
                        **initial["promotion"],
                        "guards": tuple(
                            (k, Guard(v["verdict"], tuple(v["evidence"])))
                            for k, v in initial["promotion"]["guards"].items()
                        ),
                        "stable_loss_bug_ids": tuple(
                            initial["promotion"]["stable_loss_bug_ids"]
                        ),
                        "reasons": tuple(initial["promotion"]["reasons"]),
                    }
                ),
            )
    return report


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for command in ("compare", "simulate"):
        p = sub.add_parser(command)
        p.add_argument("--spec", type=Path, required=True)
        p.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "compare":
            report = wire(compare_arms(load_comparison_spec(args.spec)))
        else:
            from bench.runner import simulation

            library, config = simulation.load_simulation_spec(args.spec)
            report = simulation.wire(simulation.simulate_rule(library, config))
        validity.atomic_json(args.out, report)
    except (ValueError, OSError, KeyError, TypeError, AttributeError) as exc:
        validity.atomic_json(
            args.out,
            {
                "schema_version": 1,
                "status": "refused",
                "refusal_codes": ["invalid_input"],
                "details": [str(exc)],
                "promotion": None,
            },
        )
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

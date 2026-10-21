"""Planning tables from immutable baseline vectors, with no review or judge calls."""

from __future__ import annotations

import argparse
import math
import random
import sys
import time
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Literal, TypedDict

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bench.runner import compare, observations, populations, validity  # noqa: E402
from bench.runner.compare import (  # noqa: E402
    Arm,
    ComparisonSpec,
    GroundednessAudit,
    GroundednessFinding,
    PromotionRule,
    PromotionRuleWire,
    derived_seed,
    evaluate_promotion,
)
from bench.runner.epochs import (  # noqa: E402
    DEFAULT_ROOT,
    Profile,
    digest,
    included_bugs,
    load_epoch,
    read_json,
)
from bench.runner.observations import ScoreRef, freeze_json  # noqa: E402
from bench.runner.observations import wire_record as wire  # noqa: E402
from bench.runner.populations import Population  # noqa: E402
from bench.runner.validity import CompleteMatrix, MatrixObservation  # noqa: E402

Generator = Literal["empirical_run_vectors", "beta_pr_sensitivity"]
GENERATOR_VERSION = "1"
LIMITATIONS = (
    "Planning table only; simulation never authorizes promotion.",
    "Empirical variance factor (n-1)/n understates unbiased run variance; unseen failures remain unmodeled.",
    "Sensitivity models nonnegative within-PR dependence, not negative or cross-PR dependence.",
    "Diffuse independent eligible-miss flips do not model a specific prompt's correlated effects.",
    "Both generators can miss unseen failures and stable-loss risk.",
    "Blinded auditor behavior and real positive-effect power remain UNVERIFIED.",
    "Auth mixing assumes a shared review distribution; auth-specific library sizes are limited.",
)


class SimulationSpecWire(TypedDict):
    schema_version: Literal[1]
    baseline_scores: list[dict[str, str]]
    epoch_id: str
    profile: Profile
    contamination_path: str
    populations_path: str
    rules: list[PromotionRuleWire]
    trials: int
    seed: int
    generators: list[Generator]
    effects_pp: list[float]
    baseline_groundedness_audit_path: str | None
    groundedness_model: Literal["baseline_audit_beta_binomial", "conditional_pass"]
    power_target: float | None
    mde_effect_grid_pp: list[float]


@dataclass(frozen=True, slots=True)
class SimulationVector:
    score_ref: ScoreRef
    run_id: str
    attempt_ids: tuple[str, ...]
    auth_mode: validity.AuthMode
    window_id: str
    window_sha256: str
    hits: tuple[bool, ...]


@dataclass(frozen=True, slots=True)
class SimulationLibrary:
    shared_identity: observations.FrozenJson
    epoch_id: str
    profile: Profile
    bug_ids: tuple[str, ...]
    pr_urls: tuple[str, ...]
    vectors: tuple[SimulationVector, ...]
    populations: tuple[Population, ...]


@dataclass(frozen=True, slots=True)
class SimulationConfig:
    rules: tuple[PromotionRule, ...]
    seed: int
    generators: tuple[Generator, ...]
    effects_pp: tuple[float, ...]
    groundedness_model: Literal["baseline_audit_beta_binomial", "conditional_pass"]
    baseline_groundedness_audit: GroundednessAudit | None
    power_target: float | None
    mde_effect_grid_pp: tuple[float, ...]
    trials: int = 2000


class GuardCountsWire(TypedDict):
    pass_count: int
    fail_count: int
    inconclusive_count: int
    sole_fail_count: int
    sole_fail_trial_rate: float
    sole_fail_share_of_failures: float | None


@dataclass(frozen=True, slots=True)
class GuardCounts:
    pass_count: int
    fail_count: int
    inconclusive_count: int
    sole_fail_count: int
    sole_fail_trial_rate: float
    sole_fail_share_of_failures: float | None


class SimulationRowWire(TypedDict):
    rule_id: str
    effect_pp: float
    generator: Generator
    generator_version: str
    seed: int
    trials: int
    initial_pass_count: int
    initial_fail_count: int
    initial_inconclusive_count: int
    extended_count: int
    final_pass_count: int
    final_fail_count: int
    final_inconclusive_count: int
    pass_rate: float | None
    pass_rate_mcse: float | None
    mean_runs_per_arm: float | None
    expected_primary_gain_pp: float | None
    realized_primary_gain_pp: float | None
    realized_nonmini_gain_pp: float | None
    groundedness_assumption: str
    null_or_power: str
    library_size: int
    auth_counts: dict[str, int]
    empirical_variance_factor: float
    variance_correction_factor: float
    status: Literal["computed", "infeasible"]
    reason: str | None
    initial_guard_counts: dict[str, GuardCountsWire]
    final_guard_counts: dict[str, GuardCountsWire]
    initial_cofailures: dict[str, int]
    final_cofailures: dict[str, int]
    recall_only_diagnostic_pass_rate: float | None
    generator_provenance: object
    bootstrap_draws: int
    runtime_s: float


@dataclass(frozen=True, slots=True)
class SimulationRow:
    rule_id: str
    effect_pp: float
    generator: Generator
    generator_version: str
    seed: int
    trials: int
    initial_pass_count: int
    initial_fail_count: int
    initial_inconclusive_count: int
    extended_count: int
    final_pass_count: int
    final_fail_count: int
    final_inconclusive_count: int
    pass_rate: float | None
    pass_rate_mcse: float | None
    mean_runs_per_arm: float | None
    expected_primary_gain_pp: float | None
    realized_primary_gain_pp: float | None
    realized_nonmini_gain_pp: float | None
    groundedness_assumption: str
    null_or_power: str
    library_size: int
    auth_counts: tuple[tuple[str, int], ...] = field(metadata={"wire_mapping": True})
    empirical_variance_factor: float
    variance_correction_factor: float
    status: Literal["computed", "infeasible"]
    reason: str | None
    initial_guard_counts: tuple[tuple[str, GuardCounts], ...] = field(
        metadata={"wire_mapping": True}
    )
    final_guard_counts: tuple[tuple[str, GuardCounts], ...] = field(
        metadata={"wire_mapping": True}
    )
    initial_cofailures: tuple[tuple[str, int], ...] = field(
        metadata={"wire_mapping": True}
    )
    final_cofailures: tuple[tuple[str, int], ...] = field(
        metadata={"wire_mapping": True}
    )
    recall_only_diagnostic_pass_rate: float | None
    generator_provenance: observations.FrozenJson
    bootstrap_draws: int
    runtime_s: float


class MDEWire(TypedDict):
    generator: Generator
    generator_version: str
    rule_id: str
    library_size: int
    power_target: float | None
    effect_grid_pp: list[float]
    pass_rates: list[float | None]
    first_effect_meeting_target_pp: float | None
    status: Literal["not_requested", "not_reached", "reached"]
    reason: str


class SimulationReportWire(TypedDict):
    schema_version: Literal[1]
    status: Literal["computed"]
    rows: list[SimulationRowWire]
    mde: list[MDEWire]
    limitations: list[str]
    library_size: int
    auth_counts: dict[validity.AuthMode, int]
    auth_label: str
    groundedness_assumption: str
    unconditional_full_rule_power_available: bool
    runtime_s: float


@dataclass(frozen=True, slots=True)
class MDE:
    generator: Generator
    generator_version: str
    rule_id: str
    library_size: int
    power_target: float | None
    effect_grid_pp: tuple[float, ...]
    pass_rates: tuple[float | None, ...]
    first_effect_meeting_target_pp: float | None
    status: Literal["not_requested", "not_reached", "reached"]
    reason: str


@dataclass(frozen=True, slots=True)
class SimulationReport:
    schema_version: Literal[1]
    status: Literal["computed"]
    rows: tuple[SimulationRow, ...]
    mde: tuple[MDE, ...]
    limitations: tuple[str, ...]
    library_size: int
    auth_counts: tuple[tuple[str, int], ...] = field(metadata={"wire_mapping": True})
    auth_label: str
    groundedness_assumption: str
    unconditional_full_rule_power_available: bool
    runtime_s: float


@dataclass(frozen=True, slots=True)
class SensitivityBug:
    bug_id: str
    hits: int
    n: int
    s2: float
    mu: float
    vJ: float
    latent_variance: float
    concentration: float
    alpha: float
    beta: float
    initial_arm_mean_variance: float
    initial_variance_floor: float


@dataclass(frozen=True, slots=True)
class PRCorrelation:
    pr_url: str
    raw_rho: float
    rho: float
    reason: str


def calibrate_sensitivity(
    library, initial_runs
) -> tuple[tuple[SensitivityBug, ...], tuple[PRCorrelation, ...]]:
    if initial_runs < 2:
        raise ValueError("beta_pr_sensitivity requires initial K>=2")
    n = len(library.vectors)
    parameters = []
    for i, bid in enumerate(library.bug_ids):
        hits = sum(v.hits[i] for v in library.vectors)
        p = hits / n
        s2 = sum((v.hits[i] - p) ** 2 for v in library.vectors) / (n - 1)
        mu = (hits + 0.5) / (n + 1)
        bernoulli = mu * (1 - mu)
        vj = bernoulli / (n + 2)
        variance = max(vj, (s2 - bernoulli) / (initial_runs - 1), 0)
        if not 0 < variance < bernoulli:
            raise ValueError("infeasible sensitivity beta variance calibration")
        c = bernoulli / variance - 1
        parameters.append(
            SensitivityBug(
                bid,
                hits,
                n,
                s2,
                mu,
                vj,
                variance,
                c,
                mu * c,
                (1 - mu) * c,
                bernoulli / initial_runs + (1 - 1 / initial_runs) * variance,
                s2 / initial_runs,
            )
        )
    metadata = {
        b.bug_id: b.pr_url for b in load_epoch(library.epoch_id, root=DEFAULT_ROOT).bugs
    }
    correlations = []
    for url in sorted(set(library.pr_urls)):
        indices = [i for i, bid in enumerate(library.bug_ids) if metadata[bid] == url]
        pairs = [(a, b) for j, a in enumerate(indices) for b in indices[j + 1 :]]
        numerator = denominator = 0.0
        for a, b in pairs:
            pa = sum(v.hits[a] for v in library.vectors) / n
            pb = sum(v.hits[b] for v in library.vectors) / n
            numerator += sum(
                (v.hits[a] - pa) * (v.hits[b] - pb) for v in library.vectors
            ) / (n - 1)
            ma, mb = parameters[a].mu, parameters[b].mu
            denominator += min(ma, mb) - ma * mb
        raw = numerator / denominator if denominator else 0.0
        correlations.append(
            PRCorrelation(
                url,
                raw,
                max(0.0, min(1.0, raw)),
                "no pairs"
                if not pairs
                else "zero denominator"
                if not denominator
                else "clipped to [0,1]"
                if not 0 <= raw <= 1
                else "estimated",
            )
        )
    return tuple(parameters), tuple(correlations)


def arm_mean_variance(parameter, k):
    return (
        parameter.mu * (1 - parameter.mu) / k + (1 - 1 / k) * parameter.latent_variance
    )


def _group(rule):
    return digest(
        {
            k: v
            for k, v in asdict(rule).items()
            if k not in ("rule_id", "groundedness_margin_pp")
        }
    )


def _rng(config, generator, group, effect, trial, arm, component):
    return random.Random(
        derived_seed(
            config.seed,
            generator,
            GENERATOR_VERSION,
            group,
            effect,
            trial,
            arm,
            component,
        )
    )


def generate_vectors(
    library, config, rule, generator, effect, trial, parameters=(), correlations=()
):
    metadata = {
        b.bug_id: b.pr_url for b in load_epoch(library.epoch_id, root=DEFAULT_ROOT).bugs
    }
    eligible = set(
        next(p for p in library.populations if p.name == "uncontaminated").bug_ids
    )
    indices = [i for i, bid in enumerate(library.bug_ids) if bid in eligible]
    n, total = len(library.vectors), rule.initial_runs + rule.extension_runs
    r0 = (
        sum(v.hits[i] for v in library.vectors for i in indices) / (n * len(indices))
        if generator == "empirical_run_vectors"
        else sum(parameters[i].mu for i in indices) / len(indices)
    )
    delta = effect / 100
    if delta < 0 or delta > 1 - r0 + 1e-12:
        raise ValueError("requested uplift exceeds generator recall ceiling")
    q = delta / (1 - r0) if r0 < 1 else 0.0
    group = _group(rule)
    arms = []
    for arm in ("baseline", "candidate"):
        draw = _rng(config, generator, group, effect, trial, arm, "vectors")
        latent_rng = _rng(config, generator, group, effect, trial, arm, "latent")
        shift = _rng(
            config, generator, group, effect, trial, arm, "eligible_miss_flips"
        )
        if generator == "empirical_run_vectors":
            vectors = [
                list(library.vectors[draw.randrange(n)].hits) for _ in range(total)
            ]
        else:
            latent = [latent_rng.betavariate(p.alpha, p.beta) for p in parameters]
            vectors = []
            for _ in range(total):
                vector = [False] * len(library.bug_ids)
                for correlation in correlations:
                    common = draw.random() < correlation.rho
                    shared = draw.random() if common else None
                    for i, bid in enumerate(library.bug_ids):
                        if metadata[bid] == correlation.pr_url:
                            u = shared if common else draw.random()
                            vector[i] = u < latent[i]
                vectors.append(vector)
        if arm == "candidate" and q:
            for vector in vectors:
                for i in indices:
                    if not vector[i] and shift.random() < q:
                        vector[i] = True
        arms.append(tuple(tuple(v) for v in vectors))
    return tuple(arms), r0


def _audit(config, rule, generator, effect, trial, stage, latent_p):
    size = rule.groundedness_sample_size
    findings = []
    for arm in ("baseline", "candidate"):
        rng = _rng(
            config, generator, _group(rule), effect, trial, arm, "audit_" + stage
        )
        findings.append(
            tuple(
                GroundednessFinding(
                    f"{arm}-{stage}-{i}",
                    ScoreRef(f"sim-{trial}-{arm}", "synthetic", "audit"),
                    f"{arm}-{stage}-{i}",
                    "synthetic",
                    rng.random() < latent_p,
                    "agreed",
                    ("synthetic correctness label",),
                )
                for i in range(size)
            )
        )
    return GroundednessAudit(
        1,
        f"sim-audit-{trial}-{stage}",
        config.seed,
        "predeclared-synthetic-audit",
        True,
        stage,
        *findings,
    )


def _matrix(library, arms, count, trial, generator):
    meta = {
        b.bug_id: b.pr_url for b in load_epoch(library.epoch_id, root=DEFAULT_ROOT).bugs
    }
    cells = tuple(
        MatrixObservation(
            arm,
            run,
            meta[bid],
            bid,
            vector[i],
            f"sim-{generator}-{trial}-{arm}-{run}",
            f"synthetic-{trial}-{arm}-{run}",
        )
        for arm, vectors in zip(("baseline", "candidate"), arms, strict=True)
        for run, vector in enumerate(vectors[:count])
        for i, bid in enumerate(library.bug_ids)
    )
    return CompleteMatrix(
        (),
        cells,
        ("baseline", "candidate"),
        tuple((a, tuple(range(count))) for a in ("baseline", "candidate")),
    )


def _guard_counts(decisions):
    failures = sum(d.verdict == "fail" for d in decisions)
    result = []
    for guard in compare.GUARDS:
        counts = {v: 0 for v in ("pass", "fail", "inconclusive")}
        sole = 0
        for decision in decisions:
            mapping = dict(decision.guards)
            counts[mapping[guard].verdict] += 1
            sole += mapping[guard].verdict == "fail" and all(
                g.verdict == "pass" for k, g in decision.guards if k != guard
            )
        result.append(
            (
                guard,
                GuardCounts(
                    counts["pass"],
                    counts["fail"],
                    counts["inconclusive"],
                    sole,
                    sole / len(decisions),
                    sole / failures if failures else None,
                ),
            )
        )
    return tuple(result)


def _cofailures(decisions):
    counts = {}
    for decision in decisions:
        failed = tuple(sorted(k for k, g in decision.guards if g.verdict == "fail"))
        if failed:
            key = ",".join(failed)
            counts[key] = counts.get(key, 0) + 1
    return tuple(sorted(counts.items()))


def validate_library(library):
    if len(library.vectors) < 3 or len({v.run_id for v in library.vectors}) != len(
        library.vectors
    ):
        raise ValueError(
            "simulation requires three independent complete baseline review runs"
        )
    if len(set(library.bug_ids)) != len(library.bug_ids) or not library.bug_ids:
        raise ValueError("invalid simulation bug vector identities")
    if any(
        len(v.hits) != len(library.bug_ids)
        or any(type(h) is not bool for h in v.hits)
        or not v.attempt_ids
        or not v.window_id
        or not v.window_sha256
        for v in library.vectors
    ):
        raise ValueError("incomplete simulation source provenance/vector")
    if not {"uncontaminated", "non-mini"} <= {p.name for p in library.populations}:
        raise ValueError("simulation needs frozen primary and non-mini populations")
    if not next(
        p for p in library.populations if p.name == "uncontaminated"
    ).denominator:
        raise ValueError("empty primary simulation population")


def simulate_rule(
    baseline: SimulationLibrary,
    config: SimulationConfig,
    *,
    rule_evaluator=evaluate_promotion,
) -> SimulationReport:
    started = time.monotonic()
    validate_library(baseline)
    if (
        config.trials <= 0
        or set(config.generators) != {"empirical_run_vectors", "beta_pr_sensitivity"}
        or not {0, 5, 10} <= set(config.effects_pp)
        or not config.rules
        or any(r.profile != baseline.profile for r in config.rules)
        or any(
            x < 0 or not math.isfinite(x)
            for x in (*config.effects_pp, *config.mde_effect_grid_pp)
        )
        or (config.power_target is not None and not 0 < config.power_target <= 1)
    ):
        raise ValueError(
            "invalid simulation grid; both generators and 0/+5/+10 are required"
        )
    groups: dict[str, set[float]] = {}
    for rule in config.rules:
        groups.setdefault(_group(rule), set()).add(rule.groundedness_margin_pp)
    if any(
        0 not in margins or not any(v < 0 for v in margins)
        for margins in groups.values()
    ):
        raise ValueError(
            "each rule group requires matched groundedness margins 0 and a negative tolerance"
        )
    audit = config.baseline_groundedness_audit
    if config.groundedness_model == "baseline_audit_beta_binomial":
        if (
            not audit
            or not audit.blinded
            or not audit.protocol_id
            or not audit.baseline
            or any(
                type(f.correct) is not bool
                or f.status not in ("agreed", "owner_resolved")
                or not f.evidence_refs
                for f in audit.baseline
            )
        ):
            raise ValueError(
                "full-rule simulation requires resolved blinded repository-backed audit, not noise buckets"
            )
        assumption = (
            "no-change groundedness; shared baseline-audit beta-binomial latent rate"
        )
        correct = sum(f.correct is True for f in audit.baseline)
        incorrect = len(audit.baseline) - correct
    elif config.groundedness_model == "conditional_pass":
        assumption = "conditional on groundedness passing; unconditional full-rule power unavailable"
        correct = incorrect = 0
    else:
        raise ValueError("choose groundedness model explicitly")
    n = len(baseline.vectors)
    auth_counts = tuple(
        (auth, sum(v.auth_mode == auth for v in baseline.vectors))
        for auth in ("api", "subscription")
    )
    provenance = {
        "shared_identity": compare.wire(baseline.shared_identity),
        "source_vectors": [asdict(v) for v in baseline.vectors],
        "auth_label": "auth-mixed library"
        if all(c for _, c in auth_counts)
        else "single-auth library",
        "limitations": list(LIMITATIONS),
        "empirical_baseline_recall": sum(
            v.hits[i]
            for v in baseline.vectors
            for i, bid in enumerate(baseline.bug_ids)
            if bid
            in next(
                p for p in baseline.populations if p.name == "uncontaminated"
            ).bug_ids
        )
        / (
            n
            * next(
                p for p in baseline.populations if p.name == "uncontaminated"
            ).denominator
        ),
        "cluster_counts": {
            p.name: len(p.nonempty_pr_clusters) for p in baseline.populations
        },
    }
    rows = []
    effects = sorted(
        set(config.effects_pp)
        | (set(config.mde_effect_grid_pp) if config.power_target is not None else set())
    )
    for generator in sorted(config.generators):
        for rule in sorted(config.rules, key=lambda r: r.rule_id):
            parameters: tuple[SensitivityBug, ...] = ()
            correlations: tuple[PRCorrelation, ...] = ()
            calibration_error = None
            if generator == "beta_pr_sensitivity":
                try:
                    parameters, correlations = calibrate_sensitivity(
                        baseline, rule.initial_runs
                    )
                except ValueError as exc:
                    calibration_error = str(exc)
            for effect in effects:
                row_start = time.monotonic()
                initial, final, extensions, gains, nonmini_gains, recall_passes = (
                    [],
                    [],
                    0,
                    [],
                    [],
                    0,
                )
                reason = calibration_error
                r0 = None
                for trial in range(config.trials if reason is None else 0):
                    try:
                        arms, r0 = generate_vectors(
                            baseline,
                            config,
                            rule,
                            generator,
                            effect,
                            trial,
                            parameters,
                            correlations,
                        )
                    except ValueError as exc:
                        reason = str(exc)
                        break
                    shared = _rng(
                        config,
                        generator,
                        _group(rule),
                        effect,
                        trial,
                        "shared",
                        "groundedness_latent",
                    )
                    latent_p = (
                        shared.betavariate(correct + 0.5, incorrect + 0.5)
                        if audit and config.groundedness_model != "conditional_pass"
                        else 1.0
                    )
                    spec = ComparisonSpec(
                        1,
                        f"sim-{trial}",
                        None,
                        None,
                        Arm("baseline", (), "synthetic", "synthetic"),
                        Arm("candidate", (), "synthetic", "synthetic"),
                        baseline.epoch_id,
                        baseline.profile,
                        "",
                        "",
                        tuple(p.name for p in baseline.populations),
                        rule,
                        None,
                        "initial",
                        resolved_populations=baseline.populations,
                    )
                    count = rule.initial_runs
                    report = compare.compare_matrix(
                        _matrix(baseline, arms, count, trial, generator), spec
                    )
                    decision = rule_evaluator(
                        report,
                        _audit(
                            config, rule, generator, effect, trial, "initial", latent_p
                        ),
                        rule,
                    )
                    initial.append(decision)
                    if decision.extension_allowed:
                        if (
                            decision.verdict != "inconclusive"
                            or rule.extension_policy != "inconclusive_only"
                        ):
                            raise ValueError("evaluator requested an unauthorized look")
                        count += rule.extension_runs
                        extensions += 1
                        report = compare.compare_matrix(
                            _matrix(baseline, arms, count, trial, generator),
                            replace(spec, look="extended"),
                        )
                        decision = rule_evaluator(
                            report,
                            _audit(
                                config,
                                rule,
                                generator,
                                effect,
                                trial,
                                "extended",
                                latent_p,
                            ),
                            rule,
                        )
                        if decision.extension_allowed:
                            raise ValueError("additional extension look refused")
                    final.append(decision)
                    by_name = {p.population: p for p in report.population_reports}
                    difference = by_name["uncontaminated"].difference
                    if difference is None:
                        raise ValueError(
                            "simulation requires a nonempty primary population"
                        )
                    gains.append(difference * 100)
                    nonmini_gains.append(
                        by_name["non-mini"].difference * 100
                        if by_name["non-mini"].difference is not None
                        else None
                    )
                    recall_passes += all(
                        g.verdict == "pass"
                        for k, g in decision.guards
                        if k != "groundedness"
                    )
                computed = reason is None
                rate = (
                    sum(d.verdict == "pass" for d in final) / config.trials
                    if computed
                    else None
                )

                def count_decisions(ds, verdict):
                    return sum(d.verdict == verdict for d in ds)

                calibration = {
                    "parameters": [asdict(p) for p in parameters],
                    "pr_correlations": [asdict(p) for p in correlations],
                    "extended_arm_mean_variances": {
                        p.bug_id: arm_mean_variance(
                            p, rule.initial_runs + rule.extension_runs
                        )
                        for p in parameters
                    },
                    "variance_floor": "mu(1-mu)/K + (1-1/K)*v >= s2/K at initial and extended prefixes",
                    "rng_group": _group(rule),
                    "analytic_R0": r0,
                    "expected_uplift_formula": "q=delta/(1-R0)",
                    "bias": f"empirical marginal variance factor={(n - 1) / n}; inverse={n / (n - 1)}",
                }
                rows.append(
                    SimulationRow(
                        rule.rule_id,
                        effect,
                        generator,
                        GENERATOR_VERSION,
                        config.seed,
                        config.trials,
                        count_decisions(initial, "pass"),
                        count_decisions(initial, "fail"),
                        count_decisions(initial, "inconclusive"),
                        extensions,
                        count_decisions(final, "pass"),
                        count_decisions(final, "fail"),
                        count_decisions(final, "inconclusive"),
                        rate,
                        math.sqrt(rate * (1 - rate) / config.trials)
                        if rate is not None
                        else None,
                        rule.initial_runs
                        + extensions * rule.extension_runs / config.trials
                        if computed
                        else None,
                        effect if computed else None,
                        sum(gains) / config.trials if computed else None,
                        sum(g for g in nonmini_gains if g is not None) / config.trials
                        if computed and all(g is not None for g in nonmini_gains)
                        else None,
                        assumption,
                        "null pass rate"
                        if effect == 0
                        else "estimated promotion power",
                        n,
                        auth_counts,
                        (n - 1) / n,
                        n / (n - 1),
                        "computed" if computed else "infeasible",
                        reason,
                        _guard_counts(initial) if computed else (),
                        _guard_counts(final) if computed else (),
                        _cofailures(initial),
                        _cofailures(final),
                        recall_passes / config.trials if computed else None,
                        freeze_json({**provenance, **calibration}),
                        rule.bootstrap.draws,
                        time.monotonic() - row_start,
                    )
                )
    mdes = []
    for generator in sorted(config.generators):
        for rule in sorted(config.rules, key=lambda r: r.rule_id):
            selected = {
                r.effect_pp: r.pass_rate
                for r in rows
                if r.generator == generator and r.rule_id == rule.rule_id
            }
            grid = tuple(sorted(set(config.mde_effect_grid_pp)))
            rates = tuple(selected.get(e) for e in grid)
            reached = [
                e
                for e, p in zip(grid, rates, strict=True)
                if p is not None
                and config.power_target is not None
                and p >= config.power_target
            ]
            mdes.append(
                MDE(
                    generator,
                    GENERATOR_VERSION,
                    rule.rule_id,
                    n,
                    config.power_target,
                    grid,
                    rates,
                    reached[0] if reached else None,
                    "not_requested"
                    if config.power_target is None
                    else "reached"
                    if reached
                    else "not_reached",
                    "No owner power target supplied"
                    if config.power_target is None
                    else "Grid resolution and feasible recall ceiling limit MDE precision",
                )
            )
    return SimulationReport(
        1,
        "computed",
        tuple(rows),
        tuple(mdes),
        LIMITATIONS,
        n,
        auth_counts,
        provenance["auth_label"],
        assumption,
        config.groundedness_model != "conditional_pass",
        time.monotonic() - started,
    )


def load_simulation_spec(path, *, packet_loader=compare.default_packet_loader):
    path = Path(path).resolve()
    value = read_json(path)
    if value["schema_version"] != 1:
        raise ValueError("unsupported simulation schema")
    epoch = load_epoch(value["epoch_id"], root=DEFAULT_ROOT)
    contamination = read_json(path.parent / value["contamination_path"])
    manifest = read_json(path.parent / value["populations_path"])
    subsets = epoch.subsets
    pops = tuple(
        populations.resolve_population(
            name, epoch, value["profile"], subsets, contamination, manifest
        )
        for name in ("gate", "uncontaminated", "non-mini", "contaminated")
    )
    vectors, identity, ids = [], None, None
    for ref_wire in value["baseline_scores"]:
        ref = ScoreRef(**ref_wire)
        packet = packet_loader(ref)
        run = compare._packet_run(packet)
        populations.check_baseline_binding(
            contamination,
            run.get("treatment_git_sha", run.get("git_sha")),
            run.get("agent_read_sha256"),
        )
        if not run.get("measurement_window"):
            raise ValueError("simulation source run lacks its auth-specific window")
        window = validity.load_window(Path(run["measurement_window"]))
        if (
            validity.check_window(
                window,
                validity.fingerprint_from_wire(run["provenance"]),
                arm=run["arm"],
                now=run["started"],
            ).state
            != "valid"
            or run.get("window_id") != window.window_id
            or run.get("window_sha256")
            != validity.file_hash(Path(run["measurement_window"]))
            or not validity.same_pr_set(run.get("pr_urls") or (), window.pr_urls)
            or set(window.pr_urls) != set(subsets["gate"])
            or not run.get("instrumentation_version")
            or not run.get("agent_read_sha256")
            or run.get("run_index") not in window.planned_run_indices
            or packet.score_ref != ref
            or run.get("child_auth") != window.expected.auth_mode
        ):
            raise ValueError("invalid simulation source window/run identity")
        fp = asdict(window.expected)
        fp.pop("auth_mode")
        shared = {
            "fingerprint_without_auth": fp,
            "evaluator": asdict(packet.manifest.evaluator),
            "epoch_hashes": dict(packet.manifest.epoch_hashes),
            "profile": value["profile"],
            "populations": {p.name: p.content_sha256 for p in pops},
            "treatment": asdict(window.arms[run["arm"]]),
            "agent_read_sha256": run["agent_read_sha256"],
            "instrumentation_version": run["instrumentation_version"],
            "pr_identities": run.get("pr_identities"),
        }
        if dict(packet.manifest.epoch_hashes) != observations.epoch_hashes(
            epoch
        ) or not validity.same_pr_set(
            packet.manifest.requested_pr_urls, window.pr_urls
        ):
            raise ValueError("simulation evaluator/epoch mismatch")
        if identity is None:
            identity = shared
        elif shared != identity:
            raise ValueError("simulation library shared identity/model map differs")
        expected = {b.bug_id for b in epoch.bugs if b.pr_url in window.pr_urls}
        bug_map = {b.bug_id: b for b in packet.observations}
        if set(bug_map) != expected or len(bug_map) != len(packet.observations):
            raise ValueError("incomplete simulation vector")
        attempts = []
        for url, aid in packet.manifest.selected_attempts.items():
            attempt = compare.load_selected_attempt(packet, url, aid)
            if (
                attempt.run_id != ref.run_id
                or attempt.pr_url != url
                or attempt.validity.observed
                != validity.fingerprint_from_wire(run["provenance"])
                or attempt.validity.window_id != window.window_id
                or attempt.validity.state not in ("valid", "operational_failure")
            ):
                raise ValueError("invalid simulation source attempt")
            attempts.append(aid)
        if any(
            b.outcome not in ("matched", "missed", "operational_failure") or b.errors
            for b in packet.observations
        ):
            raise ValueError("unavailable simulation observation")
        ids = tuple(
            sorted(
                b.bug_id
                for b in included_bugs(
                    epoch, value["profile"], frozenset(window.pr_urls)
                )
            )
        )
        vectors.append(
            SimulationVector(
                ref,
                ref.run_id,
                tuple(sorted(attempts)),
                window.expected.auth_mode,
                window.window_id,
                run["window_sha256"],
                tuple(bug_map[b].outcome == "matched" for b in ids),
            )
        )
    if not ids:
        raise ValueError("empty baseline library")
    library = SimulationLibrary(
        freeze_json(identity),
        epoch.epoch_id,
        value["profile"],
        ids,
        tuple(next(b.pr_url for b in epoch.bugs if b.bug_id == bid) for bid in ids),
        tuple(vectors),
        pops,
    )
    validate_library(library)
    audit_path = value["baseline_groundedness_audit_path"]
    config = SimulationConfig(
        tuple(compare.rule_from_wire(r) for r in value["rules"]),
        value["seed"],
        tuple(value["generators"]),
        tuple(value["effects_pp"]),
        value["groundedness_model"],
        compare.load_audit(path.parent / audit_path) if audit_path else None,
        value["power_target"],
        tuple(value["mde_effect_grid_pp"]),
        value.get("trials", 2000),
    )
    return library, config


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        library, config = load_simulation_spec(args.spec)
        validity.atomic_json(args.out, wire(simulate_rule(library, config)))
    except (ValueError, KeyError, TypeError, OSError) as exc:
        validity.atomic_json(args.out, {"status": "refused", "details": [str(exc)]})
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

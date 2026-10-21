"""Empirical run-vector planning through the comparison rule, with audit uncertainty."""

from __future__ import annotations

import math
import random
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from bench.runner import (
    audit,
    compare,
    observations,
    populations,
    validity,
)
from bench.runner.cli import spec_output_args
from bench.runner.decode import decode
from bench.runner.epochs import digest, load_epoch, read_json
from bench.runner.observations import ScoreRef


@dataclass(frozen=True, slots=True)
class SimulationSpec:
    baseline_scores: tuple[ScoreRef, ...]
    epoch_id: str
    contamination_path: str
    rule_paths: tuple[str, ...]
    audit_path: str
    baseline_arm: str
    trials: int = 2000
    seed: int = 0
    power_target: float = 0.8
    mde_grid_pp: tuple[float, ...] = (0, 5, 10, 15, 20)


@dataclass(frozen=True, slots=True)
class RunVector:
    score_ref: ScoreRef
    window_id: str
    window_sha256: str
    auth: str
    hits: tuple[bool, ...]


@dataclass(frozen=True, slots=True)
class SimulationLibrary:
    epoch_id: str
    bug_ids: tuple[str, ...]
    vectors: tuple[RunVector, ...]
    contamination: dict
    receipt: dict


@dataclass(frozen=True, slots=True)
class SimulationRow:
    rule_sha256: str
    auth: str
    effect_pp: float
    library_size: int
    trials: int
    pass_rate: float | None
    mcse: float | None
    extension_rate: float | None
    guards: dict
    infeasible: str | None


def load_simulation_spec(path, *, packet_loader=compare.default_packet_loader):
    path = Path(path).resolve()
    value = read_json(path)
    for key in ("contamination_path", "audit_path"):
        value[key] = str(path.parent / value[key])
    value["rule_paths"] = [str(path.parent / p) for p in value["rule_paths"]]
    spec = decode(SimulationSpec, value)
    epoch = load_epoch(spec.epoch_id)
    contamination = read_json(spec.contamination_path)
    populations.contamination_entries(contamination, epoch)
    ids = tuple(
        sorted(b.bug_id for b in epoch.bugs if b.pr_url in epoch.subsets["gate"])
    )
    vectors, shared = [], None
    for ref in spec.baseline_scores:
        packet = packet_loader(ref)
        run = compare._packet_run(packet)
        window_path = run["measurement_window"]
        window = validity.load_window(Path(window_path))
        if (
            not validity.same_pr_set(window.pr_urls, epoch.subsets["gate"])
            or run["child_auth"] != window.expected.auth_mode
        ):
            raise ValueError("simulation requires auth-specific gate windows")
        populations.check_baseline_binding(
            contamination, run["treatment_git_sha"], run["agent_read_sha256"]
        )
        arm = compare.Arm(
            run["arm"], (ref,), run["treatment_git_sha"], run["agent_read_sha256"]
        )
        _, selections, cells, _, _indices = compare.load_arm(
            arm,
            window.pr_urls,
            epoch,
            window=window,
            window_path=window_path,
            packet_loader=packet_loader,
        )
        validity.validate_matrix(
            selections,
            window,
            (),
            epoch=epoch,
            expected_run_indices=(run["run_index"],),
            source=True,
        )
        controls = asdict(window.expected)
        controls.pop("auth_mode")
        identity = (
            controls,
            window.arms[arm.name],
            packet.manifest.evaluator,
            run["instrumentation_version"],
        )
        if shared is not None and identity != shared:
            raise ValueError("mixed simulation contexts")
        shared = identity
        by_id = {c.bug_id: c.detected for c in cells}
        vectors.append(
            RunVector(
                ref,
                window.window_id,
                run["window_sha256"],
                run["child_auth"],
                tuple(by_id[b] for b in ids),
            )
        )
    if len(vectors) < 2 or len({v.score_ref.run_id for v in vectors}) != len(vectors):
        raise ValueError("independent baseline vectors required")
    receipt = audit.load_receipt(
        spec.audit_path,
        population=populations.resolve_population(
            "gate-uncontaminated", epoch, "core", contamination
        ),
    )
    frozen = read_json(receipt["sources"]["sample_path"])
    if frozen["arms"].get(spec.baseline_arm) != [
        asdict(r) for r in spec.baseline_scores
    ]:
        raise ValueError("baseline audit does not bind simulation sources")
    receipt = {**receipt, "baseline_arm": spec.baseline_arm}
    return SimulationLibrary(
        epoch.epoch_id, ids, tuple(vectors), contamination, receipt
    ), spec


def feasibility(library, effect):
    epoch = load_epoch(library.epoch_id)
    primary = populations.resolve_population(
        "gate-uncontaminated", epoch, "core", library.contamination
    )
    indices = [i for i, bid in enumerate(library.bug_ids) if bid in primary.bug_ids]
    r0 = sum(v.hits[i] for v in library.vectors for i in indices) / (
        len(library.vectors) * len(indices)
    )
    if effect < 0 or effect / 100 > 1 - r0 + 1e-12:
        raise ValueError("uplift exceeds empirical recall ceiling")
    return indices, r0


def generate_vectors(library, rule, effect, rng, *, feasible=None):
    indices, r0 = feasible if feasible is not None else feasibility(library, effect)
    delta = effect / 100
    total = rule.initial_runs + rule.extension_runs
    arms = [
        [list(rng.choice(library.vectors).hits) for _ in range(total)] for _ in range(2)
    ]
    for vector in arms[1]:
        for i in indices:
            if not vector[i] and rng.random() < (delta / (1 - r0) if r0 < 1 else 0):
                vector[i] = True
    return arms


def simulated_audit(library, rng, size=30):
    labels = [
        r["correct"]
        for r in library.receipt["agreed"]
        if r["arm"] == library.receipt["baseline_arm"]
    ]
    if (
        not library.receipt["resolved"]
        or not labels
        or any(type(v) is not bool for v in labels)
    ):
        raise ValueError("resolved baseline audit required")
    p = rng.betavariate(sum(labels) + 0.5, len(labels) - sum(labels) + 0.5)
    rows = [
        dict(arm=arm, correct=rng.random() < p)
        for arm in ("baseline", "candidate")
        for _ in range(size)
    ]
    return dict(resolved=True, agreed=rows, arm_order=["baseline", "candidate"])


def trial_reports(library, arms, count, rule):
    epoch = load_epoch(library.epoch_id)
    meta = {b.bug_id: b.pr_url for b in epoch.bugs}
    rows = tuple(
        validity.MatrixObservation(
            arm, run, meta[bid], bid, vector[i], f"{arm}-{run}", "synthetic"
        )
        for arm, vectors in zip(("baseline", "candidate"), arms, strict=True)
        for run, vector in enumerate(vectors[:count])
        for i, bid in enumerate(library.bug_ids)
    )
    runs = tuple((a, tuple(range(count))) for a in ("baseline", "candidate"))
    matrix = validity.CompleteMatrix((), rows, ("baseline", "candidate"), runs)
    return tuple(
        compare.reduce_population(
            matrix,
            populations.resolve_population(name, epoch, "core", library.contamination),
            epoch,
            "core",
            rule.bootstrap,
            rule.stability_min_runs,
        )
        for name in ("gate-uncontaminated", "gate-uncontaminated-non-mini")
    )


def simulate_rule(library, spec, *, rule_evaluator=compare.evaluate_promotion):
    if (
        spec.trials < 1
        or not 0 < spec.power_target <= 1
        or any(not math.isfinite(e) or e < 0 for e in spec.mde_grid_pp)
    ):
        raise ValueError("invalid simulation grid")
    rows, mde = [], {}
    effects = sorted({0, 5, 10, *spec.mde_grid_pp})
    for rule_path in spec.rule_paths:
        rule = decode(compare.PromotionRule, read_json(rule_path))
        if rule.kind != "wave":
            raise ValueError("planning simulates the complete wave rule")
        auth_mode = (
            "mixed"
            if len({v.auth for v in library.vectors}) > 1
            else library.vectors[0].auth
        )
        for effect in effects:
            rng = random.Random(
                int(
                    digest([spec.seed, digest(asdict(rule)), auth_mode, effect])[:16],
                    16,
                )
            )
            decisions, extended, error = [], 0, None
            try:
                feasible = feasibility(library, effect)
            except ValueError as exc:
                error = str(exc)
            if not error:
                for _ in range(spec.trials):
                    arms = generate_vectors(
                        library, rule, effect, rng, feasible=feasible
                    )
                    receipt = simulated_audit(library, rng)
                    reports = trial_reports(library, arms, rule.initial_runs, rule)
                    decision = rule_evaluator(reports, receipt, rule)
                    if decision.extension_allowed:
                        extended += 1
                        reports = trial_reports(
                            library, arms, rule.initial_runs + rule.extension_runs, rule
                        )
                        decision = rule_evaluator(
                            reports, simulated_audit(library, rng), rule, extended=True
                        )
                    decisions.append(decision)
            rate = (
                sum(d.verdict == "pass" for d in decisions) / spec.trials
                if not error
                else None
            )
            guards = {
                g: {
                    v: sum(d.guards[g] == v for d in decisions)
                    for v in ("pass", "fail", "inconclusive")
                }
                for g in ("primary_recall", "non-mini", "groundedness", "stable_loss")
            }
            rows.append(
                SimulationRow(
                    digest(asdict(rule)),
                    auth_mode,
                    effect,
                    len(library.vectors),
                    spec.trials,
                    rate,
                    math.sqrt(rate * (1 - rate) / spec.trials)
                    if rate is not None
                    else None,
                    extended / spec.trials if not error else None,
                    guards,
                    error,
                )
            )
        matching = [
            r.effect_pp
            for r in rows
            if r.auth == auth_mode
            and r.rule_sha256 == digest(asdict(rule))
            and r.pass_rate is not None
            and r.pass_rate >= spec.power_target
        ]
        mde[digest([asdict(rule), auth_mode])] = min(matching) if matching else None
    return dict(
        rows=[asdict(r) for r in rows],
        mde_pp=mde,
        sources=[asdict(v) for v in library.vectors],
        seed=spec.seed,
        auth_counts={
            a: sum(v.auth == a for v in library.vectors)
            for a in ("api", "subscription")
        },
        limitation="K=3 empirical rates have 1/3 granularity; unseen failures and correlated treatment effects are unmodeled. Planning only.",
    )


def main(argv=None):
    args = spec_output_args(__doc__, argv)
    library, spec = load_simulation_spec(args.spec)
    observations.exclusive_json(args.out, simulate_rule(library, spec))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

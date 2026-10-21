"""Population-driven paired reports and the shared wave/ship rule evaluator."""

from __future__ import annotations

import argparse
import math
import random
import sys
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Literal, get_args

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bench.runner import (  # noqa: E402
    audit,
    observations,
    populations,
    stages,
    validity,
)
from bench.runner.decode import decode  # noqa: E402
from bench.runner.epochs import (  # noqa: E402
    Category,
    Profile,
    digest,
    file_hash,
    load_epoch,
    read_json,
)
from bench.runner.observations import ScoreRef  # noqa: E402
from bench.runner.populations import PopulationName  # noqa: E402

Verdict = Literal["pass", "fail", "inconclusive"]


@dataclass(frozen=True, slots=True)
class BootstrapConfig:
    draws: int = 10000
    seed: int = 0
    alpha: float = 0.05

    def __post_init__(self):
        if self.draws < 1 or not 0 < self.alpha < 0.5:
            raise ValueError("invalid bootstrap configuration")


@dataclass(frozen=True, slots=True)
class PromotionRule:
    kind: Literal["wave", "ship"]
    profile: Literal["core"]
    margin_pp: float
    nonmini_margin_pp: float
    initial_runs: int
    extension_runs: int
    stability_min_runs: int
    bootstrap: BootstrapConfig
    frozen: bool = False

    def __post_init__(self):
        if (
            self.initial_runs < 1
            or self.extension_runs < 0
            or self.stability_min_runs < 1
        ):
            raise ValueError("invalid rule counts")
        if self.kind == "ship" and (self.initial_runs != 3 or self.extension_runs):
            raise ValueError("ship requires 3+3 and no extension")


@dataclass(frozen=True, slots=True)
class Arm:
    name: str
    scores: tuple[ScoreRef, ...]
    treatment_git_sha: str
    agent_read_sha256: str


@dataclass(frozen=True, slots=True)
class ComparisonSpec:
    comparison_id: str
    purpose: Literal["wave", "final", "descriptive"]
    population: PopulationName
    profile: Literal["core"]
    baseline: Arm
    candidate: Arm
    epoch_id: str
    contamination_path: str
    window_path: str | None = None
    rule_path: str | None = None
    audit_path: str | None = None
    initial_spec_path: str | None = None
    initial_report_path: str | None = None


def load_comparison_spec(path):
    path = Path(path).resolve()
    value = read_json(path)
    for key in (
        "contamination_path",
        "window_path",
        "rule_path",
        "audit_path",
        "initial_spec_path",
        "initial_report_path",
    ):
        if value.get(key):
            value[key] = str((path.parent / value[key]).resolve())
    return decode(ComparisonSpec, value)


@dataclass(frozen=True, slots=True)
class BugComparison:
    bug_id: str
    pr_url: str
    category: Category | None
    baseline_rate: float
    candidate_rate: float
    difference: float
    baseline_label: str
    candidate_label: str
    stable_loss: bool


@dataclass(frozen=True, slots=True)
class Interval:
    lower: float | None
    upper: float | None
    two_sided_lower: float | None
    two_sided_upper: float | None
    cluster_count: int


@dataclass(frozen=True, slots=True)
class PopulationComparison:
    population: PopulationName
    profile: Profile
    denominator: int
    baseline_recall: float | None
    candidate_recall: float | None
    difference: float | None
    interval: Interval
    bugs: tuple[BugComparison, ...]
    categories: dict
    dimensions: dict
    funnels: dict


@dataclass(frozen=True, slots=True)
class PromotionDecision:
    verdict: Verdict
    guards: dict[str, Verdict]
    stable_loss_bug_ids: tuple[str, ...]
    extension_allowed: bool
    improvement: bool


@dataclass(frozen=True, slots=True)
class ComparisonReport:
    comparison_id: str
    population_reports: tuple[PopulationComparison, ...]
    input_score_refs: tuple[ScoreRef, ...]
    identity: dict
    spend: dict
    promotion: PromotionDecision | None = None


def quantile(values, q):
    h = (len(values) - 1) * q
    low = math.floor(h)
    return values[low] + (values[math.ceil(h)] - values[low]) * (h - low)


def cluster_bootstrap(bugs, config):
    clusters = {}
    for bug in sorted(bugs, key=lambda b: b.bug_id):
        clusters.setdefault(bug.pr_url, []).append(bug.difference)
    packed = [(sum(v), len(v)) for _, v in sorted(clusters.items())]
    if not packed:
        return Interval(None, None, None, None, 0)
    rng, replicates = random.Random(config.seed), []
    for _ in range(config.draws):
        sample = [packed[rng.randrange(len(packed))] for _ in packed]
        replicates.append(sum(s for s, _ in sample) / sum(n for _, n in sample))
    replicates.sort()
    return Interval(
        *(
            quantile(replicates, q)
            for q in (config.alpha, 1 - config.alpha, 0.025, 0.975)
        ),
        len(packed),
    )


def stability(hits, n, minimum):
    if n < minimum:
        return "insufficient_runs"
    return "stable_found" if hits == n else "stable_missed" if hits == 0 else "flaky"


def reduce_population(
    matrix, population, epoch, profile, config, minimum=3, receipt=None
):
    names = matrix.arms
    runs = dict(matrix.run_indices_by_arm)
    index = {(r.arm, r.run_index, r.bug_id): r for r in matrix.observations}
    if len(index) != len(matrix.observations):
        raise ValueError("duplicate bug observation")
    bugs, members = [], {}
    for bug in epoch.bugs:
        if bug.bug_id not in population.bug_ids:
            continue
        rows = [[index[(arm, run, bug.bug_id)] for run in runs[arm]] for arm in names]
        if any(type(r.detected) is not bool for arm in rows for r in arm):
            raise ValueError("non-Boolean detection")
        hits = [sum(r.detected for r in arm) for arm in rows]
        rates = [h / len(arm) for h, arm in zip(hits, rows, strict=True)]
        labels = [
            stability(h, len(arm), minimum) for h, arm in zip(hits, rows, strict=True)
        ]
        bugs.append(
            BugComparison(
                bug.bug_id,
                bug.pr_url,
                bug.category,
                *rates,
                rates[1] - rates[0],
                *labels,
                labels[0] == "stable_found" and hits[1] == 0,
            )
        )
        for name, arm in zip(names, rows, strict=True):
            members.setdefault(name, []).extend(arm)

    def means(rows):
        return tuple(
            sum(getattr(b, field) for b in rows) / len(rows) if rows else None
            for field in ("baseline_rate", "candidate_rate", "difference")
        )

    categories = {
        str(cat): means([b for b in bugs if b.category == cat])
        for cat in (*get_args(Category), None)
    }
    dimensions, funnels = {}, {}
    for name in names:
        rows = members.get(name, [])
        dimensions[name] = {
            d: {
                kind: sum(
                    (r.detected if kind == "delivered" else True)
                    and d in getattr(r, kind + "_dimensions")
                    for r in rows
                )
                / len(rows)
                if rows and all(r.provenance_available for r in rows)
                else None
                for kind in ("delivered", "discovery")
            }
            for d in get_args(observations.Dimension)
        }
        for dimension in get_args(observations.Dimension):
            sampled = (
                [
                    r
                    for r in receipt["agreed"]
                    if r["arm"] == name and r["finding"]["pr_url"] in population.pr_urls
                ]
                if receipt
                else []
            )
            selected = [
                r for r in sampled if dimension in r["finding"]["discovery_dimensions"]
            ]
            known = all(
                r["finding"]["provenance_status"] == "complete" for r in sampled
            )
            dimensions[name][dimension]["groundedness"] = (
                sum(r["correct"] for r in selected) / len(selected)
                if selected and known
                else None
            )
        refs = {s.score_ref for s in matrix.selections if s.arm == name}
        funnels[name] = asdict(
            stages.loss_funnel(
                [s for s in matrix.stage_observations if s.score_ref in refs],
                population=population.bug_ids,
            )
        )
    return PopulationComparison(
        population.name,
        profile,
        len(bugs),
        *means(bugs),
        cluster_bootstrap(bugs, config),
        tuple(bugs),
        categories,
        dimensions,
        funnels,
    )


def evaluate_promotion(reports, receipt, rule, *, extended=False):
    primary, nonmini = (
        next(
            (p for p in reports if p.profile == rule.profile and p.population == name),
            None,
        )
        for name in ("gate-uncontaminated", "gate-uncontaminated-non-mini")
    )
    guards = {}
    for name, pop, margin in (
        ("primary_recall", primary, rule.margin_pp),
        ("non-mini", nonmini, rule.nonmini_margin_pp),
    ):
        if pop is None or pop.interval.cluster_count < 2:
            guards[name] = "inconclusive"
            continue
        threshold = (-margin if rule.kind == "ship" else margin) / 100
        guards[name] = (
            "pass"
            if pop.interval.lower > threshold
            else "fail"
            if pop.interval.upper <= threshold
            else "inconclusive"
        )
    losses = tuple(b.bug_id for b in primary.bugs if b.stable_loss) if primary else ()
    if rule.kind == "wave":
        if not receipt or not receipt["resolved"]:
            raise ValueError("resolved blinded audit required")
        grouped = {}
        for row in receipt["agreed"]:
            grouped.setdefault(row["arm"], []).append(row["correct"])
        if len(grouped) != 2 or any(
            not values or any(type(v) is not bool for v in values)
            for values in grouped.values()
        ):
            raise ValueError("incomplete audit")
        base, candidate = (grouped[a] for a in receipt["arm_order"])
        guards["groundedness"] = (
            "pass"
            if sum(candidate) / len(candidate) >= sum(base) / len(base)
            else "fail"
        )
        guards["stable_loss"] = (
            "fail"
            if losses
            else "pass"
            if primary
            and all(
                b.baseline_label != "insufficient_runs"
                and b.candidate_label != "insufficient_runs"
                for b in primary.bugs
            )
            else "inconclusive"
        )
    verdict = (
        "fail"
        if "fail" in guards.values()
        else "inconclusive"
        if "inconclusive" in guards.values()
        else "pass"
    )
    improvement = bool(
        primary
        and primary.difference is not None
        and primary.difference >= 0.05
        and primary.interval.lower > 0
    )
    return PromotionDecision(
        verdict,
        guards,
        losses,
        rule.kind == "wave"
        and not extended
        and verdict == "inconclusive"
        and rule.extension_runs > 0,
        improvement,
    )


def default_packet_loader(ref):
    if any(
        not s or Path(s).name != s or s in (".", "..") for s in asdict(ref).values()
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
    return read_json(packet.root / "run-provenance.json")


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


def load_arm(
    arm,
    scope,
    epoch,
    *,
    window=None,
    window_path=None,
    packet_loader=default_packet_loader,
):
    cells, selections, stage_rows, packets, indices = [], [], [], [], []
    by_attempt = {}
    for order, ref in enumerate(arm.scores):
        packet = packet_loader(ref)
        if (
            packet.score_ref != ref
            or ref.epoch_id != epoch.epoch_id
            or dict(packet.manifest.epoch_hashes) != observations.epoch_hashes(epoch)
        ):
            raise ValueError("packet scoring context mismatch")
        if not validity.same_pr_set(packet.manifest.requested_pr_urls, scope):
            raise ValueError("incomplete PR scope")
        expected = {b.bug_id for b in epoch.bugs if b.pr_url in scope}
        if (
            len(packet.observations) != len(expected)
            or {b.bug_id for b in packet.observations} != expected
        ):
            raise ValueError("incomplete bug observations")
        run = _packet_run(packet)
        index = run["run_index"] if window else order
        if window:
            fp = validity.fingerprint_from_wire(run["provenance"])
            if (
                validity.check_window(
                    window, fp, arm=arm.name, now=run["started"]
                ).state
                != "valid"
                or run["window_sha256"] != file_hash(Path(window_path))
                or run["window_id"] != window.window_id
                or run["arm"] != arm.name
                or not run.get("instrumentation_version")
                or run["treatment_git_sha"] != arm.treatment_git_sha
                or run["agent_read_sha256"] != arm.agent_read_sha256
                or run["pr_identities"] != {u: epoch.shas[u] for u in scope}
            ):
                raise ValueError("review window/treatment mismatch")
        if set(packet.manifest.selected_attempts) != set(scope):
            raise ValueError("incomplete selected attempts")
        sr = stages.read_rows(packet.root)
        stage_rows.extend(sr)
        traces = stages.packet_traces(packet.root)
        for url, aid in packet.manifest.selected_attempts.items():
            by_attempt[(index, url)] = []
            attempt = load_selected_attempt(packet, url, aid) if window else None
            selections.append(
                validity.CellSelection(
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
            )
        for bug in packet.observations:
            if (
                bug.errors
                or bug.outcome == "unavailable"
                or bug.attempt_id != packet.manifest.selected_attempts[bug.pr_url]
            ):
                raise ValueError("invalid selected bug observation")
            selected = next(
                s for s in selections if s.run_index == index and s.pr_url == bug.pr_url
            )
            if window and (bug.outcome == "operational_failure") != (
                selected.attempt.validity.state == "operational_failure"
            ):
                raise ValueError("operational failure must zero the entire PR")
            discovery = next(
                (s for s in sr if s.bug_id == bug.bug_id and s.stage == "discover"),
                None,
            )
            matched = set(discovery.matched_version_ids) if discovery else set()
            dims = tuple(
                sorted(
                    {
                        v.discovery_dimension
                        for v in (
                            traces[bug.pr_url][1].versions
                            if bug.pr_url in traces
                            else ()
                        )
                        if v.version_id in matched
                    }
                )
            )
            by_attempt[(index, bug.pr_url)].append(
                validity.MatrixObservation(
                    arm.name,
                    index,
                    bug.pr_url,
                    bug.bug_id,
                    bug.outcome == "matched",
                    ref.run_id,
                    bug.attempt_id,
                    stages.credited_dimensions(bug, packet.candidates),
                    dims,
                    (),
                    bool(discovery and discovery.coverage == "complete"),
                )
            )
        packets.append(packet)
        indices.append(index)
    if not indices or len(indices) != len(set(indices)):
        raise ValueError("missing or duplicate runs")
    selections = [
        replace(s, observations=tuple(by_attempt[s.run_index, s.pr_url]))
        for s in selections
    ]
    cells = [c for values in by_attempt.values() for c in values]
    return packets, selections, cells, stage_rows, tuple(indices)


def compare_arms(spec, *, packet_loader=default_packet_loader):
    epoch = load_epoch(spec.epoch_id)
    contamination = read_json(spec.contamination_path)
    pop = populations.resolve_population(
        spec.population, epoch, spec.profile, contamination
    )
    operational = spec.purpose != "descriptive"
    if spec.population == "holdout" or spec.baseline.name == spec.candidate.name:
        raise ValueError("sealed population or duplicate arm")
    rule = decode(PromotionRule, read_json(spec.rule_path)) if spec.rule_path else None
    window = validity.load_window(Path(spec.window_path)) if spec.window_path else None
    if operational and (
        not window
        or not rule
        or not rule.frozen
        or rule.kind != ("ship" if spec.purpose == "final" else "wave")
        or not spec.population.startswith("gate")
    ):
        raise ValueError("operational comparison requires gate, window and frozen rule")
    if not operational and (rule or window):
        raise ValueError("descriptive comparison has no operational rule/window")
    scope = tuple(epoch.subsets["gate"]) if operational else pop.pr_urls
    if window and (
        not validity.same_pr_set(window.pr_urls, scope)
        or (rule.kind == "ship" and window.expected.auth_mode != "api")
    ):
        raise ValueError("operational scope/auth mismatch")
    if operational:
        populations.check_baseline_binding(
            contamination,
            spec.baseline.treatment_git_sha,
            spec.baseline.agent_read_sha256,
        )
    arms = (spec.baseline, spec.candidate)
    packets, selections, cells, stage_rows, runs = [], [], [], [], []
    for arm in arms:
        ps, ss, cs, sr, indices = load_arm(
            arm,
            scope,
            epoch,
            window=window,
            window_path=spec.window_path,
            packet_loader=packet_loader,
        )
        if operational and arm == spec.baseline:
            for packet in ps:
                receipt = _packet_run(packet).get("baseline_equivalence_receipt") or {}
                if (
                    receipt.get("release_sha") != "839b1886"
                    or receipt.get("merge_sha") != arm.treatment_git_sha
                    or not receipt.get("evidence_refs")
                ):
                    raise ValueError("missing Wave 0a merge equivalence receipt")
        packets.extend(ps)
        selections.extend(ss)
        cells.extend(cs)
        stage_rows.extend(sr)
        runs.append((arm.name, indices))
    if (
        len({p.run_id for p in packets}) != len(packets)
        or len({p.manifest.evaluator for p in packets}) != 1
    ):
        raise ValueError("duplicate review or mixed scoring contexts")
    if (
        operational
        and len({(_packet_run(p)["instrumentation_version"]) for p in packets}) != 1
    ):
        raise ValueError("mixed instrumentation")
    if operational:
        count = rule.initial_runs + (
            rule.extension_runs if spec.initial_spec_path else 0
        )
        matrix = validity.validate_matrix(
            selections,
            window,
            (),
            epoch=epoch,
            expected_run_indices=tuple(window.planned_run_indices[:count]),
        )
        if any(len(indices) != count for _, indices in runs):
            raise ValueError("unexpected rule run count")
    else:
        matrix = validity.CompleteMatrix(
            tuple(selections), tuple(cells), tuple(a.name for a in arms), tuple(runs)
        )
    matrix = replace(
        matrix, arms=tuple(a.name for a in arms), stage_observations=tuple(stage_rows)
    )
    receipt = (
        audit.load_receipt(spec.audit_path, {a.name: a.scores for a in arms})
        if spec.audit_path
        else None
    )
    if receipt:
        candidates = {
            (p.score_ref, c.candidate_id): digest(asdict(c))
            for p in packets
            for c in p.candidates
        }
        if any(
            candidates.get(
                (decode(ScoreRef, r["score_ref"]), r["finding"]["candidate_id"])
            )
            != digest(r["finding"])
            for r in receipt["agreed"]
        ):
            raise ValueError("audit sample differs from delivered findings")
        for arm in arms:
            expected = min(
                30, sum(len(p.candidates) for p in packets if p.score_ref in arm.scores)
            )
            if sum(r["arm"] == arm.name for r in receipt["agreed"]) != expected:
                raise ValueError(
                    "audit requires 30 findings per arm or a delivered census"
                )
        receipt = {**receipt, "arm_order": [a.name for a in arms]}
    names = (
        tuple(n for n in get_args(PopulationName) if n.startswith("gate"))
        if operational
        else (spec.population,)
    )
    reports = tuple(
        reduce_population(
            matrix,
            populations.resolve_population(name, epoch, profile, contamination),
            epoch,
            profile,
            rule.bootstrap if rule else BootstrapConfig(),
            rule.stability_min_runs if rule else 3,
            receipt,
        )
        for profile in ("core", "all", "strict")
        for name in names
    )
    decision = (
        evaluate_promotion(
            reports, receipt, rule, extended=bool(spec.initial_spec_path)
        )
        if operational
        else None
    )
    if spec.initial_spec_path:
        initial = load_comparison_spec(spec.initial_spec_path)
        if (
            initial.initial_spec_path
            or rule.kind != "wave"
            or initial.rule_path != spec.rule_path
            or initial.population != spec.population
        ):
            raise ValueError("invalid extension prefix")
        for before, after in zip(
            (initial.baseline, initial.candidate), arms, strict=True
        ):
            if before != replace(after, scores=after.scores[: rule.initial_runs]):
                raise ValueError("substituted initial runs")
        previous = compare_arms(initial, packet_loader=packet_loader)
        if not spec.initial_report_path:
            raise ValueError("extension requires the saved initial report")
        snapshot = read_json(spec.initial_report_path)
        if (
            digest(snapshot) != digest(asdict(previous))
            or not previous.promotion.extension_allowed
        ):
            raise ValueError("initial report, rule, audit or decision changed")
    from bench.report import spend_totals

    spend = spend_totals(
        [
            {
                **p.metadata,
                "packet_path": str(p.root),
                **asdict(p.score_ref),
                **{k + "_cost_usd": value for k, value in p.costs.items()},
            }
            for p in packets
        ]
    )
    return ComparisonReport(
        spec.comparison_id,
        reports,
        tuple(p.score_ref for p in packets),
        dict(
            epoch_id=epoch.epoch_id,
            golden_sha256=epoch.golden_sha256,
            contamination_sha256=digest(contamination),
            rule_sha256=digest(asdict(rule)) if rule else None,
            audit_sha256=digest(receipt) if receipt else None,
        ),
        spend,
        decision,
    )


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    observations.exclusive_json(
        args.out, asdict(compare_arms(load_comparison_spec(args.spec)))
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

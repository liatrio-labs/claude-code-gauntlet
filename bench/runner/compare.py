"""Population-driven paired reports and the shared wave/ship rule evaluator."""

import math
import random
import sys
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Literal, get_args

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from bench.runner import (
    audit,
    looks,
    observations,
    populations,
    stages,
    validity,
)
from bench.runner.cli import spec_output_args
from bench.runner.contracts import ScoreRef, exclusive_json
from bench.runner.decode import decode
from bench.runner.epochs import (
    Category,
    Profile,
    digest,
    file_hash,
    load_epoch,
    read_json,
)
from bench.runner.populations import PopulationName
from bench.runner.validity import load_selected_attempt

ROOT = Path(__file__).resolve().parents[2]

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
class GoldenCorrection:
    review_contamination_sha256: str
    golden_sha256: str


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
    unseal: str | None = None
    golden_correction: GoldenCorrection | None = None


def load_comparison_spec(path):
    path = Path(path).resolve()
    value = read_json(path)
    for key in (*[k for k in value if k.endswith("_path")], "unseal"):
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
    content_sha256: str
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
        sampled = [
            r
            for r in (receipt["agreed"] if receipt else ())
            if r["arm"] == name and r["finding"]["pr_url"] in population.pr_urls
        ]
        known = all(r["finding"]["provenance_status"] == "complete" for r in sampled)
        dimensions[name] = {}
        for d in get_args(observations.Dimension):
            selected = [r for r in sampled if d in r["finding"]["discovery_dimensions"]]
            dimensions[name][d] = {
                kind: sum(
                    (r.detected or kind == "discovery")
                    and d in getattr(r, kind + "_dimensions")
                    for r in rows
                )
                / len(rows)
                if rows
                and all(
                    r.delivered_provenance_available
                    if kind == "delivered"
                    else r.provenance_available
                    for r in rows
                )
                else None
                for kind in ("delivered", "discovery")
            }
            dimensions[name][d]["groundedness"] = (
                sum(r["correct"] for r in selected) / len(selected)
                if selected and known
                else None
            )
        refs = {s.score_ref for s in matrix.selections if s.arm == name}
        funnels[name] = asdict(
            stages.loss_funnel(
                [s for s in matrix.stage_observations if s.score_ref in refs],
                population=population.bug_ids,
                score_refs=refs,
            )
        )
    return PopulationComparison(
        population.name,
        population.content_sha256,
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
        if len(receipt["arm_order"]) != 2:
            raise ValueError("wave requires two audit arms")
        base, candidate = (
            [r["correct"] for r in receipt["agreed"] if r["arm"] == a]
            for a in receipt["arm_order"]
        )
        if len(base) != len(candidate) or any(
            type(v) is not bool for v in base + candidate
        ):
            raise ValueError("incomplete or unequal audit")
        guards["groundedness"] = (
            "inconclusive"
            if not base
            else "pass"
            if sum(candidate) >= sum(base)
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
        and verdict in ("fail", "inconclusive")
        and rule.extension_runs > 0,
        improvement,
    )


def default_packet_loader(ref):
    if any(
        not s or Path(s).name != s or s in (".", "..") for s in asdict(ref).values()
    ):
        raise ValueError("invalid score reference")
    parent = ROOT / "bench/workspace/runs" / ref.run_id / "scores" / ref.epoch_id
    return observations.load_packet(parent / ref.score_id)


def _packet_run(packet):
    return read_json(packet.root / "run-provenance.json")


def load_arm(arm, scope, epoch, *, window=None, window_path=None, packet_loader=None):
    packet_loader = packet_loader or default_packet_loader
    packets = tuple(packet_loader(ref) for ref in arm.scores)
    attempts = (
        {
            (p.score_ref, url): load_selected_attempt(p, url, aid)
            for p in packets
            for url, aid in p.manifest.selected_attempts.items()
        }
        if window
        else {}
    )
    evidence = [
        (_packet_run(p), stages.read_rows(p.root), stages.packet_traces(p.root))
        for p in packets
    ]
    return matrix_for_arm(
        arm,
        scope,
        epoch,
        packets,
        attempts,
        evidence,
        window=window,
        window_hash=file_hash(Path(window_path)) if window else None,
    )


def matrix_for_arm(
    arm, scope, epoch, packets, attempts, evidence, *, window=None, window_hash=None
):
    cells, selections, stage_rows, indices = [], [], [], []
    by_attempt = {}
    for order, (ref, p, (run, sr, traces)) in enumerate(
        zip(arm.scores, packets, evidence, strict=True)
    ):
        if (
            p.score_ref != ref
            or ref.epoch_id != epoch.epoch_id
            or dict(p.manifest.epoch_hashes) != observations.epoch_hashes(epoch)
        ):
            raise ValueError("p scoring context mismatch")
        if not validity.same_pr_set(p.manifest.requested_pr_urls, scope):
            raise ValueError("incomplete PR scope")
        expected = {b.bug_id for b in epoch.bugs if b.pr_url in scope}
        if (
            len(p.observations) != len(expected)
            or {b.bug_id for b in p.observations} != expected
        ):
            raise ValueError("incomplete bug observations")
        index = run["run_index"] if window else order
        if window:
            fp = validity.fingerprint_from_wire(run["provenance"])
            if (
                validity.check_window(
                    window, fp, arm=arm.name, now=run["started"]
                ).state
                != "valid"
                or run["window_sha256"] != window_hash
                or run["window_id"] != window.window_id
                or run["arm"] != arm.name
                or not run.get("instrumentation_version")
                or run["treatment_git_sha"] != arm.treatment_git_sha
                or run["agent_read_sha256"] != arm.agent_read_sha256
                or run["pr_identities"] != {u: epoch.shas[u] for u in scope}
            ):
                raise ValueError("review window/treatment mismatch")
        if set(p.manifest.selected_attempts) != set(scope):
            raise ValueError("incomplete selected attempts")
        stage_rows.extend(sr)
        for url, aid in p.manifest.selected_attempts.items():
            by_attempt[(index, url)] = []
            attempt = attempts[(ref, url)] if window else None
            selections.append(
                validity.CellSelection(
                    arm.name,
                    index,
                    url,
                    aid,
                    ref,
                    attempt.validity.retry_of
                    if attempt and attempt.validity.state == "operational_failure"
                    else None,
                    attempt,
                )
            )
        for bug in p.observations:
            if (
                bug.errors
                or bug.outcome == "unavailable"
                or bug.attempt_id != p.manifest.selected_attempts[bug.pr_url]
            ):
                raise ValueError("invalid selected bug observation")
            if window and (bug.outcome == "operational_failure") != (
                attempts[(ref, bug.pr_url)].validity.state == "operational_failure"
            ):
                raise ValueError("operational failure must zero the entire PR")
            discovery = next(
                (s for s in sr if s.bug_id == bug.bug_id and s.stage == "discover"),
                None,
            )
            matched = set(discovery.matched_version_ids) if discovery else set()
            versions = traces[bug.pr_url][1].versions if bug.pr_url in traces else ()
            dims = tuple(
                sorted(
                    {v.discovery_dimension for v in versions if v.version_id in matched}
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
                    stages.credited_dimensions(bug, p.candidates),
                    dims,
                    (),
                    bool(discovery and discovery.coverage == "complete"),
                    all(
                        c.provenance_status == "complete"
                        for c in p.candidates
                        if c.pr_url == bug.pr_url
                    ),
                )
            )
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
    manifest = read_json(spec.contamination_path)
    scoring_manifest = manifest
    if correction := spec.golden_correction:
        if (
            correction.review_contamination_sha256 != digest(manifest)
            or correction.golden_sha256 != epoch.golden_sha256
        ):
            raise ValueError("golden correction binding mismatch")
        # Population labels follow scoring; review evidence keeps its frozen binding.
        scoring_manifest = {**manifest, "golden_sha256": correction.golden_sha256}

    def resolve(name, profile):
        return populations.resolve_population(
            name,
            epoch,
            profile,
            scoring_manifest,
            unseal=spec.unseal,
            treatment_sha=spec.candidate.treatment_git_sha,
        )

    pop = resolve(spec.population, spec.profile)
    gate = spec.purpose != "descriptive"
    rule = decode(PromotionRule, read_json(spec.rule_path)) if spec.rule_path else None
    window = validity.load_window(Path(spec.window_path)) if spec.window_path else None
    if gate and (
        not window
        or not rule
        or not rule.frozen
        or rule.kind != ("ship" if spec.purpose == "final" else "wave")
        or not spec.population.startswith("gate")
        or spec.candidate.name != window.candidate_arm
    ):
        raise ValueError("gate comparison requires gate, window and frozen rule")
    if spec.baseline.name == spec.candidate.name or (
        not gate
        and (
            rule
            or window
            or spec.population not in ("dev-slice", "dev-pool", "mini", "holdout")
        )
    ):
        raise ValueError("descriptive comparison has no gate rule/window")
    look = looks.next_look(window.wave_id, digest(asdict(rule))) if gate else None
    if gate and window.rule_sha256 != digest(asdict(rule)):
        raise ValueError("window rule differs from preregistration")
    scope = tuple(epoch.subsets["gate"]) if gate else pop.pr_urls
    if window and (
        not validity.same_pr_set(window.pr_urls, scope)
        or (rule.kind == "ship" and window.expected.auth_mode != "api")
    ):
        raise ValueError("gate scope/auth mismatch")
    if gate:
        populations.check_baseline_binding(
            manifest,
            spec.baseline.treatment_git_sha,
            spec.baseline.agent_read_sha256,
        )
    arms = (spec.baseline, spec.candidate)
    count = (
        rule.initial_runs + (rule.extension_runs if spec.initial_spec_path else 0)
        if gate
        else None
    )
    if gate and (
        len(window.planned_run_indices) < count
        or any(len(a.scores) != count for a in arms)
    ):
        raise ValueError("full rule run count required in window and both arms")
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
        if (
            spec.unseal
            and arm == spec.candidate
            and any(
                _packet_run(p)["treatment_git_sha"] != arm.treatment_git_sha for p in ps
            )
        ):
            raise ValueError("holdout final treatment mismatch")
        if gate and arm == spec.baseline:
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
        or (
            gate
            and len({_packet_run(p)["instrumentation_version"] for p in packets}) != 1
        )
    ):
        raise ValueError("duplicate review or mixed scoring contexts")
    if gate:
        matrix = validity.validate_matrix(
            selections,
            window,
            (),
            epoch=epoch,
            expected_run_indices=tuple(window.planned_run_indices[:count]),
        )
        validity.validate_interleaving(selections, window, runs)
        for packet in packets:
            if packet.score_ref in spec.candidate.scores:
                populations.check_freeze(manifest, _packet_run(packet)["started"])
                if _packet_run(packet).get("contamination_sha256") != digest(manifest):
                    raise ValueError("candidate contamination binding mismatch")
                looks.registration(
                    window.wave_id,
                    digest(asdict(rule)),
                    started=_packet_run(packet)["started"],
                )
    else:
        matrix = validity.CompleteMatrix(
            tuple(selections), tuple(cells), tuple(a.name for a in arms), tuple(runs)
        )
    matrix = replace(
        matrix, arms=tuple(a.name for a in arms), stage_observations=tuple(stage_rows)
    )
    audit_population = resolve("gate-uncontaminated", "core") if gate else pop
    receipt = (
        audit.load_receipt(
            spec.audit_path,
            {a.name: a.scores for a in arms},
            audit_population,
        )
        if spec.audit_path
        else None
    )
    if receipt:
        candidates = {
            (p.score_ref, c.candidate_id): digest(asdict(c))
            for p in packets
            for c in p.candidates
            if c.pr_url in audit_population.pr_urls
        }
        if any(
            candidates.get(
                (decode(ScoreRef, r["score_ref"]), r["finding"]["candidate_id"])
            )
            != digest(r["finding"])
            for r in receipt["agreed"]
        ):
            raise ValueError("audit sample differs from delivered findings")
        expected = min(
            30, *(sum(ref in a.scores for ref, _ in candidates) for a in arms)
        )
        if any(
            sum(r["arm"] == a.name for r in receipt["agreed"]) != expected for a in arms
        ):
            raise ValueError(
                "audit requires same-size samples capped by the smaller census"
            )
        receipt = {**receipt, "arm_order": [a.name for a in arms]}
    names = (
        tuple(n for n in get_args(PopulationName) if n.startswith("gate"))
        if gate
        else (spec.population,)
    )
    reports = tuple(
        reduce_population(
            matrix,
            resolve(name, profile),
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
        if gate
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
        if any(
            before != replace(after, scores=after.scores[: rule.initial_runs])
            for before, after in zip(
                (initial.baseline, initial.candidate), arms, strict=True
            )
        ):
            raise ValueError("substituted initial runs")
        snapshot = (
            read_json(spec.initial_report_path) if spec.initial_report_path else {}
        )
        prior = [
            r
            for r in looks.entries()
            if r["kind"] == "look" and r["wave"] == window.wave_id
        ]
        if (
            not prior
            or digest(snapshot) != prior[0]["report_sha256"]
            or not snapshot["promotion"]["extension_allowed"]
        ):
            raise ValueError("initial report, rule, audit or decision changed")
    from bench.runner.ledger import spend_totals

    return ComparisonReport(
        spec.comparison_id,
        reports,
        tuple(p.score_ref for p in packets),
        dict(
            look=look,
            populations={
                p.population + "/" + p.profile: p.content_sha256 for p in reports
            },
            epoch_id=epoch.epoch_id,
            golden_sha256=epoch.golden_sha256,
            golden_correction=asdict(spec.golden_correction)
            if spec.golden_correction
            else None,
            contamination_sha256=digest(manifest),
            rule_sha256=digest(asdict(rule)) if rule else None,
            audit_sha256=digest(receipt) if receipt else None,
        ),
        spend_totals([p.ledger_row() for p in packets]),
        decision,
    )


def main(argv=None):
    args = spec_output_args(__doc__, argv)
    spec = load_comparison_spec(args.spec)
    report = compare_arms(spec)
    if report.promotion:
        looks.record(validity.load_window(Path(spec.window_path)), spec, report)
    if spec.population == "holdout":
        looks.holdout(spec.unseal, spec.candidate.treatment_git_sha, use="compare")
    exclusive_json(args.out, asdict(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Comparisons exercise scoring, population, and operational verdict boundaries."""

import itertools
import random
from dataclasses import asdict, replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from bench.runner import compare as c
from bench.runner import observations as o
from bench.runner import populations as p
from bench.runner import validity as v
from bench.runner.epochs import canonical
from bench.tests.test_validity import fingerprint as fingerprint
from bench.tests.test_validity import window as window


def matrix_for(epoch, baseline, candidate):
    bugs = [b for b in epoch.bugs if b.pr_url in epoch.subsets["gate"]]
    rows = tuple(
        v.MatrixObservation(
            a, r, b.pr_url, b.bug_id, bool(vector[i]), f"{a}-{r}", "attempt"
        )
        for a, vectors in (("baseline", baseline), ("candidate", candidate))
        for r, vector in enumerate(vectors)
        for i, b in enumerate(bugs)
    )
    return v.CompleteMatrix(
        (),
        rows,
        ("baseline", "candidate"),
        tuple(
            (a, tuple(range(len(vectors))))
            for a, vectors in (("baseline", baseline), ("candidate", candidate))
        ),
    )


def rule(kind="wave", **kw):
    return replace(
        c.PromotionRule(
            kind, "core", -10, -10, 3, 0, 3, c.BootstrapConfig(80, 7), True
        ),
        **kw,
    )


@pytest.fixture
def comparison_case(
    population_case, score_case, tmp_path, fingerprint, window, monkeypatch
):
    epoch, contamination = population_case
    base = o.load_packet(Path(score_case.execute()["ledger_row"]["packet_path"]))
    manifest = tmp_path / "contamination.json"
    contamination.update(
        agent_read_sha256="baseline",
        baseline_treatment_sha=fingerprint.treatment.treatment_git_sha,
    )
    manifest.write_bytes(canonical(contamination))
    window_path = tmp_path / "window.json"
    window_path.write_bytes(canonical(window.wire()))
    rule_path = tmp_path / "rule.json"
    rule_path.write_bytes(canonical(asdict(rule())))
    attempts = {}

    def selected(packet, url, aid):
        return attempts[(packet.run_id, url)]

    monkeypatch.setattr(c, "load_selected_attempt", selected)

    def build(population="gate", operational=True, count=3):
        scope = (
            epoch.subsets["gate"]
            if operational
            else p.resolve_population(population, epoch, "core", contamination).pr_urls
        )
        packets, arms = {}, []
        for name in ("baseline", "candidate"):
            refs = []
            for i in range(count):
                ref = o.ScoreRef(f"{name}-{i}-{population}", epoch.epoch_id, "score")
                refs.append(ref)
                root = tmp_path / ref.run_id
                root.mkdir(exist_ok=True)
                (root / "stages.jsonl").write_text("", encoding="utf-8")
                (root / "stage_trace_bindings.json").write_text("{}", encoding="utf-8")
                run = dict(
                    run_index=i,
                    provenance=fingerprint.wire(),
                    started="2026-10-20T12:00:00Z",
                    window_sha256=v.file_hash(window_path),
                    window_id=window.window_id,
                    measurement_window=str(window_path),
                    arm=name,
                    instrumentation_version="1",
                    treatment_git_sha=fingerprint.treatment.treatment_git_sha,
                    agent_read_sha256="baseline",
                    pr_identities={u: epoch.shas[u] for u in scope},
                    child_auth="api",
                    baseline_equivalence_receipt=dict(
                        release_sha="839b1886",
                        merge_sha=fingerprint.treatment.treatment_git_sha,
                        evidence_refs=["equivalence fixture"],
                    ),
                )
                (root / "run-provenance.json").write_bytes(canonical(run))
                selected = {u: f"{ref.run_id}-{n}" for n, u in enumerate(scope)}
                for u, aid in selected.items():
                    attempts[(ref.run_id, u)] = v.AttemptRecord(
                        1,
                        aid,
                        ref.run_id,
                        u,
                        run["started"],
                        run["started"],
                        "ok",
                        None,
                        v.Validity(
                            "valid", window_id=window.window_id, observed=fingerprint
                        ),
                        {},
                        1,
                    )
                bugs = tuple(
                    o.BugObservation(
                        1,
                        ref,
                        b.pr_url,
                        selected[b.pr_url],
                        b.bug_id,
                        b.category,
                        "missed",
                        (),
                        (),
                        None,
                    )
                    for b in epoch.bugs
                    if b.pr_url in scope
                )
                candidates = tuple(
                    replace(
                        candidate,
                        candidate_id=f"{ref.run_id}-{n}",
                        pr_url=scope[n % len(scope)],
                        attempt_id=selected[scope[n % len(scope)]],
                    )
                    for n, candidate in enumerate(base.candidates)
                )
                packets[ref] = replace(
                    base,
                    root=root,
                    manifest=replace(
                        base.manifest,
                        score_ref=ref,
                        requested_pr_urls=tuple(scope),
                        selected_attempts=selected,
                    ),
                    observations=bugs,
                    candidates=candidates,
                    metadata={
                        **base.metadata,
                        "run_id": ref.run_id,
                        "review_attempt_costs": {a: 1.0 for a in selected.values()},
                    },
                    costs={
                        "dedup": 0.0,
                        "judge": 1.0,
                        "adjudicator": 0.0,
                        "attribution": None,
                    },
                )
            arms.append(
                c.Arm(
                    name,
                    tuple(refs),
                    fingerprint.treatment.treatment_git_sha,
                    "baseline",
                )
            )
        spec = c.ComparisonSpec(
            "fixture",
            "wave" if operational else "descriptive",
            population,
            "core",
            *arms,
            epoch.epoch_id,
            str(manifest),
            str(window_path) if operational else None,
            str(rule_path) if operational else None,
        )
        return spec, packets

    return SimpleNamespace(
        build=build,
        epoch=epoch,
        contamination=contamination,
        rule_path=rule_path,
        window_path=window_path,
        attempts=attempts,
    )


def test_bug_means_cluster_bootstrap_labels_and_unknown_attribution(population_case):
    epoch, contamination = population_case
    bugs = [b for b in epoch.bugs if b.pr_url in epoch.subsets["gate"]]
    matrix = matrix_for(
        epoch,
        [[0] * len(bugs), [1] * len(bugs), [0] * len(bugs)],
        [[1] * len(bugs)] * 3,
    )
    pop = p.resolve_population("gate-uncontaminated", epoch, "core", contamination)
    result = c.reduce_population(matrix, pop, epoch, "core", c.BootstrapConfig(100, 9))
    assert result.difference == pytest.approx(2 / 3)
    assert all(
        b.baseline_label == "flaky" and b.candidate_label == "stable_found"
        for b in result.bugs
    )
    assert result.categories and all(
        d["delivered"] is None for d in result.dimensions["candidate"].values()
    )
    assert result.funnels["baseline"]["unavailable_count"] == 50
    rows = (
        replace(result.bugs[0], pr_url="a", difference=0),
        replace(result.bugs[1], pr_url="b", difference=1),
        replace(result.bugs[2], pr_url="b", difference=1),
    )
    support = [
        sum(sum(x) for x in draw) / sum(len(x) for x in draw)
        for draw in itertools.product(([0], [1, 1]), repeat=2)
    ]
    rng = random.Random(9)
    draws = sorted(support[2 * rng.randrange(2) + rng.randrange(2)] for _ in range(100))
    interval = c.cluster_bootstrap(rows, c.BootstrapConfig(100, 9))
    assert interval.lower == c.quantile(
        draws, 0.05
    ) and interval.two_sided_upper == c.quantile(draws, 0.975)
    assert interval == c.cluster_bootstrap(
        tuple(reversed(rows)), c.BootstrapConfig(100, 9)
    )
    assert (
        c.stability(0, 3, 3) == "stable_missed"
        and c.stability(0, 1, 3) == "insufficient_runs"
    )


@pytest.mark.parametrize("kind", ["wave", "ship"])
def test_rule_kinds_margins_guards_and_reported_improvement(population_case, kind):
    epoch, contamination = population_case
    n = sum(b.pr_url in epoch.subsets["gate"] for b in epoch.bugs)
    matrix = matrix_for(epoch, [[0] * n] * 3, [[1] * n] * 3)
    reports = tuple(
        c.reduce_population(
            matrix,
            p.resolve_population(name, epoch, "core", contamination),
            epoch,
            "core",
            c.BootstrapConfig(50),
        )
        for name in ("gate-uncontaminated", "gate-uncontaminated-non-mini")
    )
    receipt = dict(
        resolved=True,
        agreed=[dict(arm=a, correct=True) for a in ("baseline", "candidate")],
        arm_order=["baseline", "candidate"],
    )
    r = rule(
        kind,
        margin_pp=10 if kind == "ship" else -10,
        nonmini_margin_pp=10 if kind == "ship" else -10,
    )
    assert (
        c.evaluate_promotion(reports, receipt if kind == "wave" else None, r).verdict
        == "pass"
    )
    assert c.evaluate_promotion(reports, receipt, r).improvement
    equal = tuple(
        replace(p, difference=0, interval=c.Interval(0, 0, 0, 0, 13)) for p in reports
    )
    fixed = replace(r, margin_pp=10, nonmini_margin_pp=10)
    assert c.evaluate_promotion(equal, receipt, fixed).verdict == (
        "pass" if kind == "ship" else "fail"
    )
    assert not c.evaluate_promotion(equal, receipt, fixed).improvement

    for name in ("primary_recall", "non-mini", "groundedness", "stable_loss"):
        changed = list(reports)
        changed_receipt = receipt
        if name in ("primary_recall", "non-mini"):
            i = int(name == "non-mini")
            changed[i] = replace(
                changed[i], interval=c.Interval(-0.5, -0.5, -0.5, -0.5, 13)
            )
        if name == "groundedness":
            changed_receipt = {
                **receipt,
                "agreed": [
                    dict(arm=a, correct=a == "baseline")
                    for a in ("baseline", "candidate")
                ],
            }
        if name == "stable_loss":
            changed[0] = replace(
                changed[0],
                bugs=tuple(replace(b, stable_loss=True) for b in reports[0].bugs),
            )
        decision = c.evaluate_promotion(changed, changed_receipt, r)
        assert decision.verdict == (
            "pass"
            if kind == "ship" and name in ("groundedness", "stable_loss")
            else "fail"
        )
    uncertain = tuple(
        replace(x, interval=c.Interval(-0.2, 0.2, -0.3, 0.3, 13)) for x in reports
    )
    assert c.evaluate_promotion(
        uncertain, receipt, replace(r, extension_runs=1) if kind == "wave" else r
    ).extension_allowed == (kind == "wave")
    if kind == "wave":
        with pytest.raises(ValueError, match="audit"):
            c.evaluate_promotion(reports, None, r)


@pytest.mark.parametrize("population", ["dev-slice", "dev-pool"])
def test_descriptive_populations_accept_the_actual_named_scope(
    comparison_case, population
):
    spec, packets = comparison_case.build(population, False)
    report = c.compare_arms(spec, packet_loader=packets.__getitem__)
    assert {p.profile for p in report.population_reports} == {"core", "all", "strict"}
    assert report.promotion is None and report.spend["scoring_cost_usd"] == 6
    assert report.spend["attribution_cost_usd"] is None


@pytest.mark.parametrize(
    "corruption", ["context", "epoch", "scope", "bugs", "attempts", "independence"]
)
def test_operational_comparison_refuses_verdict_corruption(
    comparison_case, corruption, tmp_path
):
    spec, packets = comparison_case.build()
    from bench.tests.test_audit import resolve_audit

    _, _, receipt = resolve_audit(spec, packets, tmp_path / "audit")
    spec = replace(spec, audit_path=str(receipt))
    ref = spec.candidate.scores[0]
    packet = packets[ref]
    if corruption == "context":
        packet = replace(
            packet,
            manifest=replace(
                packet.manifest,
                evaluator=replace(packet.manifest.evaluator, judge_pin="other"),
            ),
        )
    if corruption == "epoch":
        packet = replace(packet, manifest=replace(packet.manifest, epoch_hashes={}))
    if corruption == "scope":
        packet = replace(
            packet,
            manifest=replace(
                packet.manifest,
                requested_pr_urls=packet.manifest.requested_pr_urls[:-1],
            ),
        )
    if corruption == "bugs":
        packet = replace(packet, observations=packet.observations[:-1])
    if corruption == "attempts":
        packet = replace(
            packet, manifest=replace(packet.manifest, selected_attempts={})
        )
    if corruption == "independence":
        spec = replace(
            spec, candidate=replace(spec.candidate, scores=spec.baseline.scores)
        )
    packets[ref] = packet
    with pytest.raises(ValueError):
        c.compare_arms(spec, packet_loader=packets.__getitem__)


def test_extension_recomputes_its_initial_prefix(comparison_case, tmp_path):
    from bench.tests.test_audit import resolve_audit

    case = comparison_case
    spec, packets = case.build(count=4)
    guard = p.resolve_population(
        "gate-uncontaminated-non-mini", case.epoch, "core", case.contamination
    )
    positive_pr = guard.pr_urls[0]
    for ref, packet in list(packets.items()):
        if ref in spec.candidate.scores:
            packets[ref] = replace(
                packet,
                observations=tuple(
                    replace(b, outcome="matched") if b.pr_url == positive_pr else b
                    for b in packet.observations
                ),
            )
    case.rule_path.write_bytes(
        canonical(asdict(rule(margin_pp=0, nonmini_margin_pp=0, extension_runs=1)))
    )
    initial = replace(
        spec,
        baseline=replace(spec.baseline, scores=spec.baseline.scores[:3]),
        candidate=replace(spec.candidate, scores=spec.candidate.scores[:3]),
    )
    _, _, receipt = resolve_audit(initial, packets, tmp_path / "initial-audit")
    initial = replace(initial, audit_path=str(receipt))
    initial_path = tmp_path / "initial.json"
    initial_path.write_bytes(canonical(asdict(initial)))
    assert c.compare_arms(
        initial, packet_loader=packets.__getitem__
    ).promotion.extension_allowed
    initial_report_path = tmp_path / "initial-report.json"
    initial_report_path.write_bytes(
        canonical(asdict(c.compare_arms(initial, packet_loader=packets.__getitem__)))
    )
    _, _, receipt = resolve_audit(spec, packets, tmp_path / "extended-audit")
    extended = replace(
        spec,
        audit_path=str(receipt),
        initial_spec_path=str(initial_path),
        initial_report_path=str(initial_report_path),
    )
    assert not c.compare_arms(
        extended, packet_loader=packets.__getitem__
    ).promotion.extension_allowed
    original_rule = case.rule_path.read_bytes()
    case.rule_path.write_bytes(
        canonical(asdict(rule(margin_pp=5, nonmini_margin_pp=5, extension_runs=1)))
    )
    with pytest.raises(ValueError, match="initial report"):
        c.compare_arms(extended, packet_loader=packets.__getitem__)
    case.rule_path.write_bytes(original_rule)
    initial_path.write_bytes(
        canonical(
            asdict(
                replace(
                    initial,
                    candidate=replace(
                        initial.candidate,
                        scores=tuple(reversed(initial.candidate.scores)),
                    ),
                )
            )
        )
    )
    with pytest.raises(ValueError, match="substituted"):
        c.compare_arms(extended, packet_loader=packets.__getitem__)

"""Empirical planning preserves vectors and evaluates the complete wave rule."""

import random
from dataclasses import asdict, replace
from pathlib import Path

from bench.runner import audit
from bench.runner import simulation as s
from bench.runner import validity as v
from bench.tests.conftest import refuses, write
from bench.tests.test_audit import resolve_audit
from bench.tests.test_compare import case as case
from bench.tests.test_compare import rule
from bench.tests.test_validity import fingerprint as fingerprint
from bench.tests.test_validity import window as window


def test_source_windows_audit_binding_and_null_pass_rate(case, tmp_path):
    spec, packets = case.build()
    _, _, receipt = resolve_audit(spec, packets, tmp_path / "audit", planning=True)
    sim = tmp_path / "simulation.json"
    value = dict(
        baseline_scores=[asdict(r) for r in spec.baseline.scores],
        epoch_id=spec.epoch_id,
        contamination_path=spec.contamination_path,
        rule_paths=[str(case.rule_path)],
        audit_path=str(receipt),
        baseline_arm="baseline",
        trials=20,
        seed=9,
        mde_grid_pp=[0, 5, 10],
    )
    write(sim, value)
    library, config = s.load_simulation_spec(sim, packet_loader=packets.__getitem__)
    strict = rule(margin_pp=0, nonmini_margin_pp=0)
    write(case.rule_path, asdict(strict))
    report = s.simulate_rule(library, config)
    null = next(r for r in report["rows"] if r["effect_pp"] == 0)
    assert (
        null["pass_rate"] == 0
        and null["guards"]["primary_recall"]["inconclusive"] == 20
    )
    assert {r["effect_pp"] for r in report["rows"]} == {0, 5, 10}
    assert all(r["library_size"] == 3 and r["auth"] == "api" for r in report["rows"])
    assert s.simulate_rule(library, config) == report

    def broken(*args, **kw):
        raise ValueError("evaluator defect")

    refuses(
        s.simulate_rule,
        library,
        replace(config, trials=1),
        rule_evaluator=broken,
        match="evaluator defect",
    )

    ref = spec.baseline.scores[0]
    original = case.runs[ref]["window_sha256"]
    case.runs[ref]["window_sha256"] = "wrong"
    refuses(s.load_simulation_spec, sim, packet_loader=packets.__getitem__)
    case.runs[ref]["window_sha256"] = original
    url = next(iter(packets[ref].manifest.selected_attempts))
    case.attempts[ref, url] = replace(
        case.attempts[ref, url], validity=v.Validity("invalid")
    )
    refuses(s.load_simulation_spec, sim, packet_loader=packets.__getitem__)


def test_vectors_remain_whole_and_ceiling_is_explicit(pop_case):
    epoch, contamination = pop_case
    from bench.runner.contracts import ScoreRef

    ids = tuple(
        sorted(b.bug_id for b in epoch.bugs if b.pr_url in epoch.subsets["gate"])
    )
    vectors = tuple(
        s.RunVector(
            ScoreRef(str(i), epoch.epoch_id, "s"),
            "w",
            "h",
            "api",
            tuple([bool(i)] * len(ids)),
        )
        for i in range(2)
    )
    library = s.SimulationLibrary(
        epoch.epoch_id,
        ids,
        vectors,
        contamination,
        audit.AuditReceipt(("baseline",), (audit.AuditRow("baseline", True),)),
    )
    arms = s.generate_vectors(library, rule(), 0, random.Random(7))
    assert all(len(set(vector)) == 1 for arm in arms for vector in arm)
    refuses(
        s.generate_vectors,
        replace(library, vectors=(vectors[1],)),
        rule(),
        10,
        random.Random(7),
        match="ceiling",
    )


def test_loaded_audit_preserves_arm_order_at_evaluation(case, tmp_path):
    from bench.runner import compare
    from bench.runner.decode import decode
    from bench.runner.epochs import read_json
    from bench.tests.test_compare import paired

    spec, packets = case.build()
    frozen, paths, receipt_path = resolve_audit(spec, packets, tmp_path / "audit")
    report = paired(replace(spec, audit_path=str(receipt_path)), packets)
    correct = {r["sample_id"]: r["arm"] == spec.baseline.name for r in frozen["rows"]}
    for path in paths["label_paths"]:
        value = read_json(path)
        for label in value["labels"]:
            label["correct"] = correct[label["sample_id"]]
        write(Path(path), value)
    merged = audit.merge(**paths)
    write(receipt_path, asdict(merged))
    arms = {arm.name: arm.scores for arm in (spec.baseline, spec.candidate)}
    receipt = audit.load_receipt(receipt_path, arms)
    assert receipt.arm_order == tuple(arms)
    assert decode(audit.AuditReceipt, read_json(receipt_path)) == receipt
    decision = compare.evaluate_promotion(report.population_reports, receipt, rule())
    assert decision.guards["groundedness"] == "fail"
    reversed_receipt = audit.load_receipt(
        receipt_path, dict(reversed(list(arms.items())))
    )
    assert reversed_receipt.arm_order == tuple(reversed(arms))
    assert (
        compare.evaluate_promotion(
            report.population_reports, reversed_receipt, rule()
        ).guards["groundedness"]
        == "pass"
    )


def test_two_stage_simulation_uses_same_audit_and_nested_runs(
    pop_case, tmp_path, monkeypatch
):
    from bench.runner import compare
    from bench.runner.contracts import ScoreRef

    epoch, contamination = pop_case
    ids = tuple(
        sorted(b.bug_id for b in epoch.bugs if b.pr_url in epoch.subsets["gate"])
    )
    rng = random.Random(41)
    vectors = tuple(
        s.RunVector(
            ScoreRef(str(i), epoch.epoch_id, "s"),
            "w",
            "h",
            "api",
            tuple(rng.random() < 0.6 for _ in ids),
            tuple(compare.PrecisionCount(str(j), 8 + i, 2) for j in range(2)),
        )
        for i in range(3)
    )
    library = s.SimulationLibrary(
        epoch.epoch_id,
        ids,
        vectors,
        contamination,
        audit.AuditReceipt(
            ("baseline",), tuple(audit.AuditRow("baseline", True) for _ in range(30))
        ),
    )
    path = tmp_path / "rule.json"
    r = rule(extension_runs=3, bootstrap=compare.BootstrapConfig(80))
    write(path, asdict(r))
    spec = s.SimulationSpec(
        (),
        epoch.epoch_id,
        "",
        (str(path),),
        "",
        "baseline",
        trials=60,
        seed=4,
        mde_grid_pp=(-7,),
    )
    original_reports = s.trial_reports
    prefix = None

    def reports(library, arms, count, rule, *, sampled):
        nonlocal prefix
        current = (
            [[list(v) for v in arm[:3]] for arm in arms],
            [arm[:3] for arm in sampled],
        )
        if count == 3:
            prefix = current
        else:
            assert current == prefix
        return original_reports(library, arms, count, rule, sampled=sampled)

    monkeypatch.setattr(s, "trial_reports", reports)
    calls = []

    def evaluate(reports, receipt, rule, *, extended=False):
        assert receipt.arm_order == ("baseline", "candidate")
        assert receipt.resolved and len(receipt.agreed) == 60
        result = compare.evaluate_promotion(reports, receipt, rule, extended=extended)
        if extended:
            assert calls[-1][0] is receipt and calls[-1][1].extension_allowed
            assert result.verdict in ("pass", "fail") and not result.extension_allowed
        calls.append((receipt, result, extended))
        return result

    report = s.simulate_rule(library, spec, rule_evaluator=evaluate)
    assert {row["effect_pp"] for row in report["rows"]} == {-7, 0, 5, 10}
    assert any(row["extension_rate"] > 0 for row in report["rows"])
    assert all(
        row["expected_runs_per_arm"] == 3 + 3 * row["extension_rate"]
        for row in report["rows"]
    )
    assert sum(row["extension_rate"] * 60 for row in report["rows"]) == sum(
        extended for _, _, extended in calls
    )
    assert all(sum(row["guards"]["precision"].values()) == 60 for row in report["rows"])
    assert all(row["infeasible"] is None for row in report["rows"])
    indices, r0 = s.feasibility(library, 0)
    unchanged = s.generate_vectors(library, r, 0, random.Random(8))
    for effect, detected in ((-100 * r0, False), (100 * (1 - r0), True)):
        arms = s.generate_vectors(library, r, effect, random.Random(8))
        assert arms[0] == unchanged[0]
        assert all(v[i] == detected for v in arms[1] for i in indices)
        assert all(
            v[i] == old[i]
            for v, old in zip(arms[1], unchanged[1], strict=True)
            for i in range(len(ids))
            if i not in indices
        )
    refuses(s.generate_vectors, library, r, -100, random.Random(8), match="floor")

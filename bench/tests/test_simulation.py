"""Empirical planning preserves vectors and evaluates the complete wave rule."""

import random
from dataclasses import asdict, replace

import pytest

from bench.runner import simulation as s
from bench.runner.epochs import canonical, read_json
from bench.tests.test_audit import resolve_audit
from bench.tests.test_compare import comparison_case as comparison_case
from bench.tests.test_compare import rule
from bench.tests.test_validity import fingerprint as fingerprint
from bench.tests.test_validity import window as window


def test_source_windows_audit_binding_and_null_pass_rate(comparison_case, tmp_path):
    spec, packets = comparison_case.build()
    _, _, receipt = resolve_audit(spec, packets, tmp_path / "audit")
    sim = tmp_path / "simulation.json"
    value = dict(
        baseline_scores=[asdict(r) for r in spec.baseline.scores],
        epoch_id=spec.epoch_id,
        contamination_path=spec.contamination_path,
        rule_paths=[str(comparison_case.rule_path)],
        audit_path=str(receipt),
        baseline_arm="baseline",
        trials=20,
        seed=9,
        mde_grid_pp=[0, 5, 10],
    )
    sim.write_bytes(canonical(value))
    library, config = s.load_simulation_spec(sim, packet_loader=packets.__getitem__)
    strict = rule(margin_pp=0, nonmini_margin_pp=0)
    comparison_case.rule_path.write_bytes(canonical(asdict(strict)))
    report = s.simulate_rule(library, config)
    null = next(r for r in report["rows"] if r["effect_pp"] == 0)
    assert null["pass_rate"] == 0 and null["guards"]["primary_recall"]["fail"] == 20
    assert {r["effect_pp"] for r in report["rows"]} == {0, 5, 10}
    assert all(r["library_size"] == 3 and r["auth"] == "api" for r in report["rows"])
    assert s.simulate_rule(library, config) == report

    def evaluator(reports, receipt, rule, **kwargs):
        from bench.runner.compare import evaluate_promotion

        decision = evaluate_promotion(reports, receipt, rule, **kwargs)
        return replace(decision, extension_allowed=not kwargs.get("extended", False))

    comparison_case.rule_path.write_bytes(
        canonical(asdict(replace(strict, extension_runs=1)))
    )
    extended = s.simulate_rule(
        library, replace(config, trials=2), rule_evaluator=evaluator
    )
    assert all(row["extension_rate"] == 1 for row in extended["rows"])
    source = next(iter(packets.values()))
    path = source.root / "run-provenance.json"
    run = read_json(path)
    path.write_bytes(canonical({**run, "window_sha256": "wrong"}))
    with pytest.raises(ValueError):
        s.load_simulation_spec(sim, packet_loader=packets.__getitem__)


def test_vectors_remain_whole_and_ceiling_is_explicit(population_case):
    epoch, contamination = population_case
    from bench.runner.observations import ScoreRef

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
        dict(
            resolved=True,
            baseline_arm="baseline",
            agreed=[dict(arm="baseline", correct=True)],
        ),
    )
    arms = s.generate_vectors(library, rule(), 0, random.Random(7))
    assert all(len(set(vector)) == 1 for arm in arms for vector in arm)
    with pytest.raises(ValueError, match="ceiling"):
        s.generate_vectors(
            replace(library, vectors=(vectors[1],)), rule(), 10, random.Random(7)
        )

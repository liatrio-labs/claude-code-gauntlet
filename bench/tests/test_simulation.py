import random
from dataclasses import asdict, replace
from pathlib import Path

import pytest

from bench.runner import audit
from bench.runner import compare as c
from bench.runner import simulation as s
from bench.runner import validity as v
from bench.tests.conftest import refuses, write
from bench.tests.test_audit import resolve_audit
from bench.tests.test_compare import case as case
from bench.tests.test_compare import rule, split_blocks
from bench.tests.test_validity import fingerprint as fingerprint
from bench.tests.test_validity import window as window


@pytest.fixture
def simulation_case(case, tmp_path):
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
    return library, config, sim, spec, packets


def test_source_windows_audit_binding_and_null_pass_rate(case, simulation_case):
    library, config, sim, spec, packets = simulation_case
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

    split_blocks(case, spec, ((0,), (1, 2)))
    mixed, _ = s.load_simulation_spec(sim, packet_loader=packets.__getitem__)
    assert {v.auth for v in mixed.vectors} == {"api", "subscription"}
    planned = s.simulate_rule(mixed, replace(config, trials=1))
    assert planned["auth_counts"] == {"api": 1, "subscription": 2}
    assert sorted(planned["lane_counts"].values()) == [1, 2]
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


def test_vectors_remain_whole_and_ceiling_is_explicit(simulation_case):
    library = simulation_case[0]
    vector = library.vectors[0]
    library = replace(
        library, vectors=(vector, replace(vector, hits=(True,) * len(vector.hits)))
    )
    r = rule()
    indices, recall = s.feasibility(library, 0)
    for effect, detected in ((-100 * recall, False), (100 * (1 - recall), True)):
        vector = library.vectors[1 if not detected else 0]
        sampled = [[vector] * r.initial_runs for _ in range(2)]
        unchanged = s.generate_vectors(library, r, 0, random.Random(8), sampled=sampled)
        arms = s.generate_vectors(library, r, effect, random.Random(8), sampled=sampled)
        assert arms[0] == unchanged[0] and all(
            vector[i] == detected for vector in arms[1] for i in indices
        )
        assert all(
            vector[i] == old[i]
            for vector, old in zip(arms[1], unchanged[1], strict=True)
            for i in range(len(library.bug_ids))
            if i not in indices
        )
    arms = s.generate_vectors(library, r, 0, random.Random(7))
    assert all(len(set(vector)) == 1 for arm in arms for vector in arm)
    for vector, effect, edge in (
        (library.vectors[1], 10, "ceiling"),
        (library.vectors[0], -10, "floor"),
    ):
        refuses(
            s.generate_vectors,
            replace(library, vectors=(vector,)),
            r,
            effect,
            random.Random(7),
            match=edge,
        )


def test_loaded_audit_preserves_arm_order_at_evaluation(case, tmp_path):
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
    for order, expected in (
        (arms, "fail"),
        (dict(reversed(list(arms.items()))), "pass"),
    ):
        receipt = audit.load_receipt(receipt_path, order)
        assert (
            c.evaluate_promotion(report.population_reports, receipt, rule()).guards[
                "groundedness"
            ]
            == expected
        )


def test_two_stage_simulation_uses_same_audit_and_nested_runs(
    simulation_case, monkeypatch
):
    library, spec, *_ = simulation_case
    rng = random.Random(41)
    library = replace(
        library,
        vectors=tuple(
            replace(
                v,
                hits=tuple(rng.random() < 0.6 for _ in library.bug_ids),
                precision=tuple(c.PrecisionCount(str(j), 8 + i, 2) for j in range(2)),
            )
            for i, v in enumerate(library.vectors)
        ),
    )
    r = rule(extension_runs=3, bootstrap=c.BootstrapConfig(80))
    write(Path(spec.rule_paths[0]), asdict(r))
    spec = replace(spec, trials=60, seed=4, mde_grid_pp=(-7,))
    original_reports = s.trial_reports
    prefixes = []

    def reports(library, arms, count, rule, *, sampled):
        current = (
            [[list(v) for v in arm[:3]] for arm in arms],
            [arm[:3] for arm in sampled],
        )
        assert count == 3 or current == prefixes[-1]
        prefixes.append(current)
        result = original_reports(library, arms, count, rule, sampled=sampled)
        assert result[0].precision == c.precision_comparison(
            [[v.precision for v in arm[:count]] for arm in sampled], rule.bootstrap
        )
        return result

    monkeypatch.setattr(s, "trial_reports", reports)
    calls = []

    def evaluate(reports, receipt, rule, *, extended=False):
        assert receipt.arm_order == ("baseline", "candidate")
        if extended:
            assert calls[-1][0] is receipt
        calls.append((receipt, extended))
        return c.evaluate_promotion(reports, receipt, rule, extended=extended)

    report = s.simulate_rule(library, spec, rule_evaluator=evaluate)
    assert any(row["extension_rate"] > 0 for row in report["rows"])
    assert all(
        row["expected_runs_per_arm"] == 3 + 3 * row["extension_rate"]
        for row in report["rows"]
    )
    assert sum(row["extension_rate"] * 60 for row in report["rows"]) == sum(
        extended for _, extended in calls
    )

"""Planning uses the real statistic and guards, with explicit generator assumptions."""

from dataclasses import replace

import pytest

from bench.runner import compare as c
from bench.runner import simulation as s
from bench.runner.observations import ScoreRef, freeze_json
from bench.tests.test_compare import operational_case as operational_case
from bench.tests.test_compare import paired_case as paired_case


@pytest.fixture
def library(paired_case):
    epoch, bugs, spec = paired_case
    ids = tuple(b.bug_id for b in bugs)
    vectors = tuple(
        s.SimulationVector(
            ScoreRef(f"measured-{r}", epoch.epoch_id, f"score-{r}"),
            f"measured-{r}",
            (f"attempt-{r}",),
            "api" if r < 2 else "subscription",
            f"window-{r}",
            f"hash-{r}",
            tuple(bool(r == 0 and i % 2) for i in range(len(ids))),
        )
        for r in range(3)
    )
    return s.SimulationLibrary(
        freeze_json({"synthetic fixture": True}),
        epoch.epoch_id,
        "core",
        ids,
        tuple(b.pr_url for b in bugs),
        vectors,
        spec.resolved_populations,
    )


def config_for(paired_case, trials=5):
    rule = paired_case[2].rule
    return s.SimulationConfig(
        (
            replace(rule, rule_id="zero", groundedness_margin_pp=0),
            replace(rule, rule_id="tolerance", groundedness_margin_pp=-40),
        ),
        123,
        ("empirical_run_vectors", "beta_pr_sensitivity"),
        (0, 5, 10),
        "conditional_pass",
        None,
        None,
        (),
        trials,
    )


def test_both_generators_use_real_rule_and_report_all_guard_counts(
    library, paired_case
):
    config = config_for(paired_case)
    calls = []

    def evaluator(report, audit, rule):
        calls.append((report, audit, rule))
        return c.evaluate_promotion(report, audit, rule)

    report = s.simulate_rule(library, config, rule_evaluator=evaluator)
    assert len(report.rows) == 12 and calls
    assert report.auth_label == "auth-mixed library" and dict(report.auth_counts) == {
        "api": 2,
        "subscription": 1,
    }
    assert not report.unconditional_full_rule_power_available
    assert "conditional on groundedness passing" in report.groundedness_assumption
    for row in report.rows:
        assert (
            row.library_size == 3
            and row.empirical_variance_factor == 2 / 3
            and row.variance_correction_factor == 1.5
        )
        assert (
            row.initial_pass_count
            + row.initial_fail_count
            + row.initial_inconclusive_count
            == config.trials
        )
        assert (
            row.final_pass_count + row.final_fail_count + row.final_inconclusive_count
            == config.trials
        )
        for _, counts in (*row.initial_guard_counts, *row.final_guard_counts):
            assert (
                counts.pass_count + counts.fail_count + counts.inconclusive_count
                == config.trials
            )
        assert row.pass_rate_mcse == pytest.approx(
            (row.pass_rate * (1 - row.pass_rate) / config.trials) ** 0.5
        )
    assert all(
        m.status == "not_requested" and m.power_target is None for m in report.mde
    )
    assert any("unseen failures" in line for line in report.limitations)
    assert all(r.generator_provenance for r in report.rows)


def test_sensitivity_analytic_variance_floor_and_printed_rho_clipping(library):
    library = replace(
        library,
        vectors=tuple(
            replace(v, hits=tuple(r != 2 for _ in library.bug_ids))
            for r, v in enumerate(library.vectors)
        ),
    )
    params, correlations = s.calibrate_sensitivity(library, 3)
    for p in params:
        assert p.s2 == pytest.approx(1 / 3)
        assert p.mu == 0.625
        assert p.vJ == pytest.approx(0.625 * 0.375 / 5)
        assert p.latent_variance == pytest.approx((p.s2 - p.mu * (1 - p.mu)) / 2)
        assert p.alpha / (p.alpha + p.beta) == pytest.approx(p.mu)
        assert p.initial_arm_mean_variance == pytest.approx(p.s2 / 3)
        for k in (3, 4, 10):
            assert s.arm_mean_variance(p, k) >= p.s2 / k - 1e-14
    assert any(
        p.raw_rho > 1 and p.rho == 1 and p.reason == "clipped to [0,1]"
        for p in correlations
    )
    with pytest.raises(ValueError, match="K>=2"):
        s.calibrate_sensitivity(library, 1)


def test_rng_order_independence_and_matched_margin_rows_reuse_inputs(
    library, paired_case
):
    config = config_for(paired_case, trials=2)
    observed = {}

    def evaluator(report, audit, rule):
        key = (rule.rule_id, report.comparison_id, report.rule.bootstrap.draws)
        observed.setdefault(key, []).append(
            (
                tuple(
                    (p.population, p.baseline_recall, p.candidate_recall)
                    for p in report.population_reports
                ),
                audit,
            )
        )
        return c.evaluate_promotion(report, audit, rule)

    first = s.simulate_rule(library, config, rule_evaluator=evaluator)
    second = s.simulate_rule(
        library,
        replace(
            config,
            rules=tuple(reversed(config.rules)),
            generators=tuple(reversed(config.generators)),
        ),
    )
    for left, right in zip(first.rows, second.rows, strict=True):
        assert replace(left, runtime_s=0) == replace(right, runtime_s=0)
    for key in observed:
        other = ("tolerance" if key[0] == "zero" else "zero", *key[1:])
        assert observed[key] == observed[other]


def test_empirical_null_draws_and_eligible_miss_flips_and_infeasible_rows(
    library, paired_case
):
    config = config_for(paired_case, 2)
    rule = config.rules[0]
    null, r0 = s.generate_vectors(library, config, rule, "empirical_run_vectors", 0, 0)
    assert all(
        vector in {v.hits for v in library.vectors} for arm in null for vector in arm
    )
    maximum = 100 * (1 - r0)
    shifted, _ = s.generate_vectors(
        library, config, rule, "empirical_run_vectors", maximum, 0
    )
    assert all(all(vector) for vector in shifted[1])
    all_hit = replace(
        library,
        vectors=tuple(
            replace(v, hits=(True,) * len(library.bug_ids)) for v in library.vectors
        ),
    )
    report = s.simulate_rule(all_hit, config)
    rows = [
        r
        for r in report.rows
        if r.generator == "empirical_run_vectors" and r.effect_pp == 10
    ]
    assert rows and all(
        r.status == "infeasible" and r.pass_rate is None and r.pass_rate_mcse is None
        for r in rows
    )


def test_full_rule_requires_audit_and_groundedness_only_penalty_is_exact(
    library, paired_case
):
    config = config_for(paired_case, 20)
    with pytest.raises(ValueError, match="audit"):
        s.simulate_rule(
            library, replace(config, groundedness_model="baseline_audit_beta_binomial")
        )
    from bench.tests.test_compare import audit_for

    rule = replace(
        config.rules[0],
        margin_pp=-200,
        nonmini_margin_pp=-200,
        groundedness_sample_size=3,
    )
    rules = (rule, replace(rule, rule_id="negative", groundedness_margin_pp=-40))
    audit = audit_for(rule, baseline=[True, False, True])
    # No measured bug was stable-found, so the toy fixture isolates the fraction guard.
    library = replace(
        library,
        vectors=tuple(
            replace(v, hits=tuple(bool((i + r) % 3 == 0) for i in range(len(v.hits))))
            for r, v in enumerate(library.vectors)
        ),
    )
    config = replace(
        config,
        rules=rules,
        baseline_groundedness_audit=audit,
        groundedness_model="baseline_audit_beta_binomial",
    )
    report = s.simulate_rule(library, config)
    assert report.unconditional_full_rule_power_available
    for row in report.rows:
        if row.status != "computed":
            continue
        counts = dict(row.final_guard_counts)
        # Other guards can co-fail when synthetic baseline hits happen to be stable.
        penalty = row.recall_only_diagnostic_pass_rate - row.pass_rate
        assert penalty == pytest.approx(counts["groundedness"].sole_fail_trial_rate)
        assert sum(dict(row.final_cofailures).values()) == row.final_fail_count
        if row.final_fail_count:
            assert (
                counts["groundedness"].sole_fail_share_of_failures
                == counts["groundedness"].sole_fail_count / row.final_fail_count
            )
    assert any(
        dict(r.final_guard_counts)["groundedness"].fail_count for r in report.rows
    )


def test_simulation_calls_authorized_extension_once_with_independent_prefix(
    library, paired_case
):
    config = config_for(paired_case, 1)
    rules = tuple(
        replace(r, extension_policy="inconclusive_only", extension_runs=1)
        for r in config.rules
    )
    counts = []

    def evaluator(report, audit, rule):
        counts.append(
            (report.look, report.population_reports[0].bugs[0].n_baseline, audit.stage)
        )
        decision = c.evaluate_promotion(report, audit, rule)
        return replace(
            decision, verdict="inconclusive", extension_allowed=report.look == "initial"
        )

    report = s.simulate_rule(
        library, replace(config, rules=rules), rule_evaluator=evaluator
    )
    assert all(r.extended_count == 1 and r.mean_runs_per_arm == 4 for r in report.rows)
    assert counts == [
        (look, n, look)
        for _ in range(12)
        for look, n in (("initial", 3), ("extended", 4))
    ]


def test_library_and_rule_grid_require_complete_independent_inputs(
    library, paired_case
):
    config = config_for(paired_case, 1)
    with pytest.raises(ValueError, match="three independent"):
        s.simulate_rule(replace(library, vectors=library.vectors[:2]), config)
    with pytest.raises(ValueError, match="margins"):
        s.simulate_rule(library, replace(config, rules=config.rules[:1]))
    with pytest.raises(ValueError, match="both generators"):
        s.simulate_rule(library, replace(config, generators=("empirical_run_vectors",)))


def test_real_evaluator_stable_loss_blocks_positive_gain_and_extension_is_used(
    library, paired_case, monkeypatch
):
    config = config_for(paired_case, 1)
    width = len(library.bug_ids)
    assert width > 2

    def extension_vectors(
        library, config, rule, generator, effect, trial, parameters=(), correlations=()
    ):
        total = rule.initial_runs + rule.extension_runs
        return (((False,) * width,) * total, ((True,) * width,) * total), 0.0

    monkeypatch.setattr(s, "generate_vectors", extension_vectors)
    rules = tuple(
        replace(
            r,
            initial_runs=2,
            extension_runs=1,
            extension_policy="inconclusive_only",
            margin_pp=-200,
            nonmini_margin_pp=-200,
        )
        for r in config.rules
    )
    report = s.simulate_rule(library, replace(config, rules=rules))
    assert all(r.extended_count == 1 and r.final_pass_count == 1 for r in report.rows)

    def stable_loss_vectors(
        library, config, rule, generator, effect, trial, parameters=(), correlations=()
    ):
        total = rule.initial_runs + rule.extension_runs
        return (
            ((True,) + (False,) * (width - 1),) * total,
            ((False,) + (True,) * (width - 1),) * total,
        ), 1 / width

    monkeypatch.setattr(s, "generate_vectors", stable_loss_vectors)
    rules = tuple(
        replace(r, margin_pp=-200, nonmini_margin_pp=-200) for r in config.rules
    )
    report = s.simulate_rule(library, replace(config, rules=rules))
    assert all(
        r.realized_primary_gain_pp > 0
        and r.final_fail_count == 1
        and dict(r.final_guard_counts)["stable_loss"].fail_count == 1
        for r in report.rows
    )


@pytest.mark.parametrize("operational_case", ["mixed"], indirect=True)
def test_mixed_auth_library_validates_separate_windows_and_shared_model_identity(
    operational_case, tmp_path
):
    import json
    from pathlib import Path

    from bench.runner import validity
    from bench.runner.epochs import read_json

    spec, packets, window = operational_case
    packet = packets[spec.baseline.scores[-1]]
    path = packet.root / "run-provenance.json"
    run = read_json(path)
    own_path = Path(run["measurement_window"])
    own = validity.load_window(own_path)
    fp = validity.fingerprint_from_wire(run["provenance"])
    planning = replace(spec.rule, frozen=False)
    value = {
        "schema_version": 1,
        "baseline_scores": [c.wire(r) for r in spec.baseline.scores],
        "epoch_id": spec.epoch_id,
        "profile": "core",
        "contamination_path": spec.contamination_path,
        "populations_path": spec.populations_path,
        "rules": [
            c.wire(planning),
            c.wire(replace(planning, rule_id="negative", groundedness_margin_pp=-10)),
        ],
        "trials": 1,
        "seed": 1,
        "generators": ["empirical_run_vectors", "beta_pr_sensitivity"],
        "effects_pp": [0, 5, 10],
        "baseline_groundedness_audit_path": None,
        "groundedness_model": "conditional_pass",
        "power_target": None,
        "mde_effect_grid_pp": [],
    }
    source = tmp_path / "simulation.json"
    source.write_text(json.dumps(value), encoding="utf-8")
    library, config = s.load_simulation_spec(source, packet_loader=packets.__getitem__)
    assert [v.auth_mode for v in library.vectors] == ["api", "api", "subscription"]
    assert all(
        v.attempt_ids and v.window_id and v.window_sha256 for v in library.vectors
    )
    report = s.simulate_rule(library, config)
    assert report.auth_label == "auth-mixed library"
    changed_fp = replace(
        fp, resolved_models=(*fp.resolved_models, ("unexpected", "claude-new-model"))
    )
    changed = replace(own, arms={**own.arms, run["arm"]: changed_fp.treatment()})
    own_path.write_text(json.dumps(changed.wire()), encoding="utf-8")
    run.update(provenance=changed_fp.wire(), window_sha256=validity.file_hash(own_path))
    path.write_text(json.dumps(run), encoding="utf-8")
    with pytest.raises(ValueError, match="shared identity/model"):
        s.load_simulation_spec(source, packet_loader=packets.__getitem__)


@pytest.mark.parametrize("location", ["run", "packet"])
@pytest.mark.parametrize("mutation", ["reordered", "duplicate", "missing", "different"])
def test_simulation_uses_equal_length_pr_sets(
    operational_case, tmp_path, location, mutation
):
    import json

    from bench.runner.epochs import read_json

    spec, packets, window = operational_case
    ref = spec.baseline.scores[0]
    packet = packets[ref]
    urls = list(reversed(window.pr_urls))
    if mutation == "duplicate":
        urls.append(urls[0])
    elif mutation == "missing":
        urls.pop()
    elif mutation == "different":
        urls[0] = "https://github.com/example/other/pull/1"
    if location == "packet":
        packets[ref] = replace(
            packet, manifest=replace(packet.manifest, requested_pr_urls=tuple(urls))
        )
    else:
        path = packet.root / "run-provenance.json"
        run = read_json(path)
        run["pr_urls"] = urls
        path.write_text(json.dumps(run), encoding="utf-8")
    source = tmp_path / "simulation.json"
    source.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "baseline_scores": [c.wire(r) for r in spec.baseline.scores],
                "epoch_id": spec.epoch_id,
                "profile": "core",
                "contamination_path": spec.contamination_path,
                "populations_path": spec.populations_path,
                "rules": [c.wire(replace(spec.rule, frozen=False))],
                "trials": 1,
                "seed": 1,
                "generators": ["empirical_run_vectors"],
                "effects_pp": [0],
                "baseline_groundedness_audit_path": None,
                "groundedness_model": "conditional_pass",
                "power_target": None,
                "mde_effect_grid_pp": [],
            }
        ),
        encoding="utf-8",
    )
    if mutation == "reordered":
        library, _ = s.load_simulation_spec(source, packet_loader=packets.__getitem__)
        assert len(library.vectors) == len(spec.baseline.scores)
    else:
        with pytest.raises(
            ValueError, match=r"source window/run identity|evaluator/epoch"
        ):
            s.load_simulation_spec(source, packet_loader=packets.__getitem__)


def test_sensitivity_common_pr_uniform_and_negative_covariance_are_explicit(
    library, paired_case, monkeypatch
):
    config = config_for(paired_case, 1)
    rule = config.rules[0]
    params, correlations = s.calibrate_sensitivity(library, 3)

    class FixedRng:
        def __init__(self):
            self.values = iter([0.0, 0.4, 0.6] * 1000)

        def random(self):
            return next(self.values)

        def betavariate(self, a, b):
            return 0.5

    monkeypatch.setattr(s, "_rng", lambda *args: FixedRng())
    shared, _ = s.generate_vectors(
        library,
        config,
        rule,
        "beta_pr_sensitivity",
        0,
        0,
        params,
        tuple(replace(p, rho=1.0) for p in correlations),
    )
    independent, _ = s.generate_vectors(
        library,
        config,
        rule,
        "beta_pr_sensitivity",
        0,
        0,
        params,
        tuple(replace(p, rho=0.0) for p in correlations),
    )
    assert library.pr_urls[0] == library.pr_urls[1]
    assert shared[0][0][:2] == (True, True)
    assert independent[0][0][:2] == (True, False)
    opposite = replace(
        library,
        vectors=tuple(
            replace(v, hits=(r != 1, r == 1, *([False] * (len(v.hits) - 2))))
            for r, v in enumerate(library.vectors)
        ),
    )
    _, estimates = s.calibrate_sensitivity(opposite, 3)
    assert any(
        p.raw_rho < 0 and p.rho == 0 and p.reason == "clipped to [0,1]"
        for p in estimates
    )

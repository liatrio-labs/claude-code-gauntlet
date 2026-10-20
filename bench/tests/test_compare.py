"""Caller tests for complete packets, paired inference, guards, and extension refusal."""

import itertools
import json
import random
import shutil
from dataclasses import replace
from pathlib import Path

import pytest

from bench.runner import compare as c
from bench.runner import observations, populations, score, validity
from bench.runner.epochs import (
    DEFAULT_ROOT,
    digest,
    included_bugs,
    load_epoch,
    read_json,
)
from bench.runner.observations import ScoreRef


@pytest.fixture
def paired_case(population_case):
    epoch, subsets, contamination, manifest, _ = population_case
    gate = populations.resolve_population(
        "gate", epoch, "core", subsets, contamination, manifest
    )
    selected = tuple(
        b
        for b in included_bugs(epoch, "core", frozenset(gate.pr_urls))
        if b.pr_url in gate.pr_urls[:2]
    )
    ids = tuple(b.bug_id for b in selected)
    urls = tuple(sorted({b.pr_url for b in selected}))
    clusters = tuple(
        (u, tuple(b.bug_id for b in selected if b.pr_url == u)) for u in urls
    )
    pops = tuple(
        populations.Population(name, ids, urls, clusters, len(ids), digest([name, ids]))
        for name in ("gate", "uncontaminated", "non-mini")
    )
    rule = c.PromotionRule(
        "planning",
        False,
        "core",
        -20.0,
        -20.0,
        3,
        0,
        3,
        -10.0,
        3,
        "none",
        c.BootstrapConfig(40, 123, 0.05),
    )
    spec = c.ComparisonSpec(
        1,
        "paired",
        None,
        None,
        c.Arm("baseline", (), "b", "b"),
        c.Arm("candidate", (), "c", "c"),
        epoch.epoch_id,
        "core",
        "",
        "",
        tuple(p.name for p in pops),
        rule,
        None,
        "initial",
        resolved_populations=pops,
    )
    return epoch, selected, spec


def matrix_for(bugs, baseline, candidate):
    rows = tuple(
        validity.MatrixObservation(
            arm, r, b.pr_url, b.bug_id, bool(v[i]), f"{arm}-{r}", f"attempt-{arm}-{r}"
        )
        for arm, vectors in (("baseline", baseline), ("candidate", candidate))
        for r, v in enumerate(vectors)
        for i, b in enumerate(bugs)
    )
    return validity.CompleteMatrix(
        (),
        rows,
        ("baseline", "candidate"),
        (
            ("baseline", tuple(range(len(baseline)))),
            ("candidate", tuple(range(len(candidate)))),
        ),
    )


def audit_for(rule, stage="initial", baseline=None, candidate=None):
    bs = baseline if baseline is not None else [True] * rule.groundedness_sample_size
    cs = candidate if candidate is not None else [True] * rule.groundedness_sample_size

    def arm(name, labels):
        return tuple(
            c.GroundednessFinding(
                f"{name}-{i}",
                ScoreRef(name, "epoch", "score"),
                f"{name}-{i}",
                "pr",
                label,
                "agreed",
                ("repository-backed synthetic fixture",),
            )
            for i, label in enumerate(labels)
        )

    return c.GroundednessAudit(
        1,
        "audit",
        1,
        "protocol",
        True,
        stage,
        arm("baseline", bs),
        arm("candidate", cs),
    )


def test_bug_weighted_paired_statistic_uses_arm_means_not_pr_means_or_majority(
    paired_case,
):
    _, bugs, spec = paired_case
    baseline = [[0] * len(bugs), [1] * len(bugs), [0] * len(bugs)]
    candidate = [[int(b.pr_url == bugs[0].pr_url) for b in bugs]] * 3
    report = c.compare_matrix(matrix_for(bugs, baseline, candidate), spec)
    gate = report.population_reports[0]
    expected = sum(v for v in candidate[0]) / len(bugs) - 1 / 3
    assert gate.difference == pytest.approx(expected)
    assert gate.baseline_recall == pytest.approx(1 / 3)
    assert gate.denominator == len(bugs)
    assert all(b.baseline_label == "flaky" for b in gate.bugs)
    descriptive = c.compare_matrix(
        matrix_for(bugs, baseline[:1], candidate),
        replace(spec, look="descriptive", rule=None),
    )
    assert descriptive.promotion is None
    assert "no measurement window" in descriptive.descriptive_reasons
    assert all(
        b.baseline_label == "insufficient_runs"
        for b in descriptive.population_reports[0].bugs
    )


def test_pr_cluster_bootstrap_matches_independently_enumerated_distribution(
    paired_case,
):
    _, bugs, spec = paired_case
    report = c.compare_matrix(
        matrix_for(
            bugs,
            [[0] * len(bugs)] * 3,
            [[int(b.pr_url == bugs[0].pr_url) for b in bugs]] * 3,
        ),
        spec,
    )
    rows = report.population_reports[0].bugs
    clusters = [
        [b.difference for b in rows if b.pr_url == u]
        for u in sorted({b.pr_url for b in rows})
    ]
    support = [
        sum(sum(clusters[i]) for i in draw) / sum(len(clusters[i]) for i in draw)
        for draw in itertools.product(range(2), repeat=2)
    ]
    config = c.BootstrapConfig(1000, 77, 0.05)
    rng = random.Random(77)
    expected = sorted(
        support[2 * rng.randrange(2) + rng.randrange(2)] for _ in range(1000)
    )
    interval = c.cluster_bootstrap(tuple(reversed(rows)), config)
    assert interval.lower == c.quantile(expected, 0.05)
    assert interval.upper == c.quantile(expected, 0.95)
    assert interval.two_sided_lower == c.quantile(expected, 0.025)
    assert interval.two_sided_upper == c.quantile(expected, 0.975)
    assert interval == c.cluster_bootstrap(rows, config)
    assert (
        interval.cluster_count == 2 and interval.seed == 77 and interval.draws == 1000
    )
    assert set(support) != {0, 0.5, 1} or len(clusters[0]) == len(clusters[1])
    assert c.cluster_bootstrap([], config).status == "empty"
    assert c.cluster_bootstrap([rows[0]], config).status == "insufficient_clusters"


@pytest.mark.parametrize("guard", c.GUARDS)
def test_each_guard_can_be_the_sole_failure_at_real_evaluator(paired_case, guard):
    _, bugs, spec = paired_case
    rule = replace(
        spec.rule, margin_pp=-50, nonmini_margin_pp=-50, groundedness_margin_pp=0
    )
    report = c.compare_matrix(
        matrix_for(bugs, [[0] * len(bugs)] * 3, [[1] * len(bugs)] * 3),
        replace(spec, rule=rule),
    )
    audit = audit_for(rule)
    if guard in ("primary_recall", "non-mini"):
        name = "uncontaminated" if guard == "primary_recall" else "non-mini"
        report = replace(
            report,
            population_reports=tuple(
                replace(p, interval=replace(p.interval, lower=-0.5, upper=-0.5))
                if p.population == name
                else p
                for p in report.population_reports
            ),
        )
    elif guard == "groundedness":
        audit = audit_for(rule, candidate=[False] * 3)
    else:
        report = replace(
            report,
            population_reports=tuple(
                replace(p, bugs=(replace(p.bugs[0], stable_loss=True), *p.bugs[1:]))
                if p.population == "uncontaminated"
                else p
                for p in report.population_reports
            ),
        )
    decision = c.evaluate_promotion(report, audit, rule)
    assert decision.verdict == "fail"
    assert {k: g.verdict for k, g in decision.guards} == {
        k: "fail" if k == guard else "pass" for k in c.GUARDS
    }


def test_strict_margin_equality_missing_audit_stability_and_single_extension(
    paired_case,
):
    _, bugs, spec = paired_case
    rule = replace(
        spec.rule,
        margin_pp=0,
        nonmini_margin_pp=0,
        extension_policy="inconclusive_only",
        extension_runs=1,
    )
    report = c.compare_matrix(
        matrix_for(bugs, [[0] * len(bugs)] * 3, [[1] * len(bugs)] * 3),
        replace(spec, rule=rule),
    )
    assert c.evaluate_promotion(report, audit_for(rule), rule).verdict == "pass"
    missing = c.evaluate_promotion(report, None, rule)
    assert missing.verdict == "inconclusive" and missing.extension_allowed
    assert not c.evaluate_promotion(
        replace(report, look="extended"), None, rule
    ).extension_allowed
    report = replace(
        report,
        population_reports=tuple(
            replace(p, interval=replace(p.interval, lower=0, upper=0.1))
            for p in report.population_reports
        ),
    )
    assert (
        dict(c.evaluate_promotion(report, audit_for(rule), rule).guards)[
            "primary_recall"
        ].verdict
        == "inconclusive"
    )
    bad = replace(audit_for(rule), blinded=False)
    assert (
        dict(c.evaluate_promotion(report, bad, rule).guards)["groundedness"].verdict
        == "inconclusive"
    )
    assert c.stability(3, 3, 3) == "stable_found"
    assert c.stability(0, 3, 3) == "stable_missed"
    assert c.stability(1, 3, 3) == "flaky"
    assert c.stability(2, 2, 3) == "insufficient_runs"


@pytest.fixture
def descriptive_packets(score_case, tmp_path):
    epoch = load_epoch(root=DEFAULT_ROOT)
    urls = tuple(read_json(DEFAULT_ROOT / "subsets.json")["mini"])
    packets = {}
    for name in ("baseline", "candidate"):
        directory = tmp_path / name
        directory.mkdir()
        (directory / "state").mkdir()
        for i, url in enumerate(urls):
            score._write_json(
                directory / f"state/{i}.json", {"url": url, "status": "ok"}
            )
            pr = directory / score.invoke.pr_dir_name({"url": url})
            pr.mkdir()
            shutil.copyfile(score_case.payload, pr / "post-review-payload.json")
        score._write_json(
            directory / "run.json", {"run_id": name, "pr_urls": list(urls)}
        )
        result = score.score_run(
            directory,
            env={"BENCH_JUDGE_API_KEY": "synthetic"},
            ledger_path=tmp_path / "ledger.jsonl",
            run_scorer=score_case.scorer,
            adjudicator=lambda *args: {"bucket": "noise"},
        )
        ref = ScoreRef(**result["score_ref"])
        packets[ref] = observations.load_packet(
            directory / "scores" / ref.epoch_id / ref.score_id
        )
    refs = list(packets)
    spec = c.ComparisonSpec(
        1,
        "mini",
        None,
        urls,
        c.Arm("baseline", (refs[0],), "b", "b"),
        c.Arm("candidate", (refs[1],), "c", "c"),
        epoch.epoch_id,
        "core",
        str(DEFAULT_ROOT / "contamination.json"),
        str(DEFAULT_ROOT / "populations.json"),
        ("gate", "uncontaminated", "non-mini"),
        None,
        None,
        "descriptive",
    )
    return spec, packets


def test_compare_arms_windowless_minis_are_complete_scoped_and_never_verdicts(
    descriptive_packets,
):
    spec, packets = descriptive_packets
    report = c.compare_arms(spec, packet_loader=packets.__getitem__)
    value = c.wire(report)
    assert value["promotion"] is None and "verdict" not in value
    assert value["descriptive_reasons"] == ["no measurement window"]
    assert report.population_reports[0].denominator == 40
    assert report.population_reports[0].n_runs_by_arm == (
        ("baseline", 1),
        ("candidate", 1),
    )
    assert any("uncontaminated unavailable" in line for line in report.limitations)
    assert len(value["identity_hashes"]["scope_pr_urls"]) == 6
    with pytest.raises(ValueError, match="window"):
        c.compare_arms(replace(spec, look="initial"), packet_loader=packets.__getitem__)
    with pytest.raises(ValueError, match="duplicate"):
        c.compare_arms(
            replace(
                spec, candidate=replace(spec.candidate, scores=spec.baseline.scores)
            ),
            packet_loader=packets.__getitem__,
        )


@pytest.mark.parametrize(
    "mutation",
    [
        "missing_pr",
        "duplicate_pr",
        "missing_bug",
        "duplicate_bug",
        "score_error",
        "evaluator",
        "missing_payload",
    ],
)
def test_compare_arms_refuses_incomplete_or_corrupt_selected_packets(
    descriptive_packets, mutation
):
    spec, packets = descriptive_packets
    ref = spec.candidate.scores[0]
    packet = packets[ref]
    if mutation == "missing_pr":
        packet = replace(
            packet,
            manifest=replace(
                packet.manifest,
                requested_pr_urls=packet.manifest.requested_pr_urls[:-1],
            ),
        )
    elif mutation == "duplicate_pr":
        packet = replace(
            packet,
            manifest=replace(
                packet.manifest,
                requested_pr_urls=(
                    *packet.manifest.requested_pr_urls,
                    packet.manifest.requested_pr_urls[0],
                ),
            ),
        )
    elif mutation == "missing_bug":
        packet = replace(packet, observations=packet.observations[:-1])
    elif mutation == "duplicate_bug":
        packet = replace(
            packet, observations=(*packet.observations, packet.observations[0])
        )
    elif mutation == "score_error":
        packet = replace(
            packet,
            observations=(
                replace(packet.observations[0], errors=("judge failure",)),
                *packet.observations[1:],
            ),
        )
    elif mutation == "evaluator":
        packet = replace(
            packet,
            manifest=replace(
                packet.manifest,
                evaluator=replace(packet.manifest.evaluator, judge_pin="changed"),
            ),
        )
    else:
        packet = replace(
            packet,
            observations=(
                replace(packet.observations[0], outcome="unavailable"),
                *packet.observations[1:],
            ),
        )
    packets[ref] = packet
    with pytest.raises(ValueError):
        c.compare_arms(spec, packet_loader=packets.__getitem__)


def test_compare_cli_structured_refusal_does_not_publish_quality_verdict(tmp_path):
    path = tmp_path / "spec.json"
    path.write_text("{}", encoding="utf-8")
    output = tmp_path / "report.json"
    assert c.main(["compare", "--spec", str(path), "--out", str(output)]) == 2
    value = read_json(output)
    assert (
        value["status"] == "refused"
        and value["promotion"] is None
        and "verdict" not in value
    )


@pytest.fixture
def operational_case(population_case, score_case, tmp_path, request, monkeypatch):
    monkeypatch.setattr(validity, "utc_now", lambda: "2026-10-20T12:00:00Z")
    epoch, subsets, contamination, manifest, _ = population_case
    contamination_path = tmp_path / "contamination.json"
    contamination_path.write_text(json.dumps(contamination), encoding="utf-8")
    populations_path = tmp_path / "frozen-populations.json"
    populations_path.write_text(json.dumps(manifest), encoding="utf-8")
    binary = tmp_path / "claude"
    binary.write_text("synthetic binary", encoding="utf-8")
    pins = read_json(DEFAULT_ROOT.parent / "baselines.json")
    fp = validity.capture_fingerprint(
        cli_path=binary,
        child_model="claude-session-1",
        auth_mode="api",
        epoch=epoch,
        judge_pin=pins["judge_pin"],
        adjudicator_pin=pins["adjudicator_pin"],
        env={},
        policy={},
        cli_probe=lambda path: "2.1.200",
        model_probe=lambda root, policy: {
            "code-gauntlet:bug-detector": "claude-agent-1"
        },
    )
    window = validity.MeasurementWindow(
        1,
        "operational",
        "2026-10-20T00:00:00Z",
        "2026-10-21T00:00:00Z",
        fp,
        tuple(subsets["gate"]),
        (0, 1, 2),
        1,
    )
    window_path = tmp_path / "window.json"
    validity.exclusive_json(window_path, window.wire())
    packets, arms, audit_findings = {}, [], []
    for arm in ("baseline", "candidate"):
        refs, findings = [], []
        for r in range(3):
            active_window, active_path = window, window_path
            if (
                getattr(request, "param", None) == "mixed"
                and arm == "baseline"
                and r == 2
            ):
                active_window = replace(
                    window,
                    window_id="subscription-window",
                    expected=replace(fp, auth_mode="subscription"),
                )
                active_path = tmp_path / "subscription-window.json"
                validity.exclusive_json(active_path, active_window.wire())
            active_fp = active_window.expected
            directory = tmp_path / f"{arm}-{r}"
            directory.mkdir()
            (directory / "state").mkdir()
            run_manifest = {
                "run_id": directory.name,
                "child_auth": active_fp.auth_mode,
                "child_model": "claude-session-1",
                "pr_urls": list(window.pr_urls),
                "provenance": active_fp.wire(),
                "window_id": active_window.window_id,
                "window_sha256": validity.file_hash(active_path),
                "measurement_window": str(active_path),
                "run_index": r,
                "arm": arm,
                "epoch_id": epoch.epoch_id,
                "golden_sha256": epoch.golden_sha256,
                "instrumentation_version": "stage-trace-v1",
                "git_sha": arm,
                "treatment_git_sha": arm,
                "agent_read_sha256": arm,
                "pr_identities": {
                    u: read_json(DEFAULT_ROOT / "shas.json")[u] for u in window.pr_urls
                },
                "baseline_equivalence_receipt": {
                    "release_sha": "94ea095a",
                    "merge_sha": arm,
                    "evidence_refs": ["synthetic equivalence"],
                },
            }
            score._write_json(directory / "run.json", run_manifest)
            for i, url in enumerate(window.pr_urls):
                pr = directory / score.invoke.pr_dir_name({"url": url})
                pr.mkdir()
                shutil.copyfile(score_case.payload, pr / "post-review-payload.json")
                attempt = validity.snapshot_attempt(
                    directory,
                    pr,
                    run_id=directory.name,
                    pr_url=url,
                    result=score.invoke.InvokeResult(
                        "ok", payload_path=str(pr / "post-review-payload.json")
                    ),
                    validity=validity.Validity(
                        "valid", window_id=active_window.window_id, observed=active_fp
                    ),
                    started_at="2026-10-20T10:00:00Z",
                )
                score._write_json(
                    directory / f"state/{i}.json",
                    {
                        "url": url,
                        "status": "ok",
                        "detail": {"attempt_id": attempt.attempt_id},
                    },
                )
            result = score.score_run(
                directory,
                env={"BENCH_JUDGE_API_KEY": "synthetic"},
                ledger_path=tmp_path / "ledger.jsonl",
                run_scorer=score_case.scorer,
                adjudicator=lambda *args: {"bucket": "noise"},
            )
            ref = ScoreRef(**result["score_ref"])
            packet = observations.load_packet(
                directory / "scores" / ref.epoch_id / ref.score_id
            )
            packets[ref] = packet
            refs.append(ref)
            chosen = packet.candidates[0]
            findings.append(
                c.GroundednessFinding(
                    f"{arm}-{r}",
                    ref,
                    chosen.candidate_id,
                    chosen.pr_url,
                    True,
                    "agreed",
                    ("synthetic repository audit",),
                )
            )
        arms.append(c.Arm(arm, tuple(refs), arm, arm))
        audit_findings.append(tuple(findings))
    rule = c.PromotionRule(
        "frozen-toy-rule",
        True,
        "core",
        -100,
        -100,
        3,
        0,
        3,
        0,
        3,
        "none",
        c.BootstrapConfig(20, 7, 0.05),
    )
    audit = c.GroundednessAudit(
        1, "audit", 7, "protocol", True, "initial", *audit_findings
    )
    audit_path = tmp_path / "audit.json"
    audit_path.write_text(json.dumps(c.wire(audit)), encoding="utf-8")
    spec = c.ComparisonSpec(
        1,
        "operational",
        str(window_path),
        None,
        *arms,
        epoch.epoch_id,
        "core",
        str(contamination_path),
        str(populations_path),
        ("gate", "uncontaminated", "non-mini", "contaminated"),
        rule,
        str(audit_path),
        "initial",
    )
    return spec, packets, window


@pytest.mark.parametrize(
    "field", ["agent_read_sha256", "baseline_treatment_sha", "leak_report_sha256"]
)
def test_operational_compare_refuses_stale_contamination_before_reading_packets(
    operational_case, field
):
    spec, _, _ = operational_case
    path = Path(spec.contamination_path)
    manifest = read_json(path)
    manifest[field] = "" if field == "leak_report_sha256" else "different-baseline"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    populations_path = Path(spec.populations_path)
    population_manifest = read_json(populations_path)
    population_manifest["contamination_sha256"] = digest(manifest)
    populations_path.write_text(json.dumps(population_manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="contamination baseline"):
        c.compare_arms(
            spec, packet_loader=lambda ref: pytest.fail("loaded stale-bound packets")
        )


def test_operational_compare_complete_matrix_and_extension_prefix_refusal(
    operational_case, tmp_path
):
    spec, packets, window = operational_case
    report = c.compare_arms(spec, packet_loader=packets.__getitem__)
    assert report.promotion.verdict == "pass"
    assert (
        next(
            p for p in report.population_reports if p.population == "uncontaminated"
        ).denominator
        == 50
    )
    assert not report.promotion.extension_allowed
    extended_rule = replace(
        spec.rule,
        initial_runs=2,
        extension_runs=1,
        extension_policy="inconclusive_only",
    )
    initial = replace(
        spec,
        rule=extended_rule,
        groundedness_audit_path=None,
        baseline=replace(spec.baseline, scores=spec.baseline.scores[:2]),
        candidate=replace(spec.candidate, scores=spec.candidate.scores[:2]),
    )
    initial_report = c.compare_arms(initial, packet_loader=packets.__getitem__)
    assert initial_report.promotion.verdict == "inconclusive"
    path = tmp_path / "initial.json"
    path.write_text(json.dumps(c.wire(initial_report)), encoding="utf-8")
    # An extended look needs its separately predeclared audit and immutable initial prefix.
    extended = replace(
        spec,
        rule=extended_rule,
        look="extended",
        initial_report_path=str(path),
        groundedness_audit_path=None,
    )
    extended_report = c.compare_arms(extended, packet_loader=packets.__getitem__)
    assert (
        extended_report.initial_score_refs
        == initial.baseline.scores + initial.candidate.scores
    )
    assert (
        extended_report.promotion.verdict == "inconclusive"
        and not extended_report.promotion.extension_allowed
    )
    forbidden = replace(
        extended,
        baseline=replace(
            extended.baseline, scores=tuple(reversed(extended.baseline.scores))
        ),
    )
    with pytest.raises(ValueError, match="prefix"):
        c.compare_arms(forbidden, packet_loader=packets.__getitem__)
    with pytest.raises(ValueError, match="initial report"):
        c.compare_arms(
            replace(extended, initial_report_path=None),
            packet_loader=packets.__getitem__,
        )


@pytest.mark.parametrize("location", ["run", "packet"])
@pytest.mark.parametrize("mutation", ["reordered", "duplicate", "missing", "different"])
def test_compare_uses_equal_length_pr_sets(operational_case, location, mutation):
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
    if mutation == "reordered":
        assert (
            c.compare_arms(spec, packet_loader=packets.__getitem__).promotion.verdict
            == "pass"
        )
    else:
        with pytest.raises(ValueError, match=r"PR.*identity|PR in selected run"):
            c.compare_arms(spec, packet_loader=packets.__getitem__)


@pytest.mark.parametrize(
    "mutation",
    [
        "missing_run",
        "duplicate_run",
        "pending",
        "invalid",
        "model",
        "instrumentation",
        "treatment",
        "pr_sha",
        "epoch",
        "missing_score",
    ],
)
def test_operational_matrix_rejects_every_missing_or_mismatched_identity(
    operational_case, mutation
):
    spec, packets, window = operational_case
    if mutation == "missing_run":
        spec = replace(
            spec, candidate=replace(spec.candidate, scores=spec.candidate.scores[:-1])
        )
    elif mutation == "duplicate_run":
        spec = replace(
            spec,
            candidate=replace(
                spec.candidate,
                scores=(*spec.candidate.scores[:-1], spec.baseline.scores[0]),
            ),
        )
    elif mutation == "missing_score":
        packets.pop(spec.candidate.scores[0])
    elif mutation == "epoch":
        ref = spec.candidate.scores[0]
        packets[ref] = replace(
            packets[ref], manifest=replace(packets[ref].manifest, epoch_hashes=())
        )
    else:
        packet = packets[spec.candidate.scores[0]]
        path = packet.root / "run-provenance.json"
        value = read_json(path)
        if mutation in ("pending", "invalid"):
            aid = dict(packet.manifest.selected_attempts)[window.pr_urls[0]]
            attempt_path = next(
                (packet.root.parents[2] / "attempts").glob(f"*/{aid}/attempt.json")
            )
            attempt = read_json(attempt_path)
            attempt["validity"]["state"] = mutation
            attempt_path.write_text(json.dumps(attempt), encoding="utf-8")
            source_hashes = dict(packet.manifest.source_artifact_hashes)
            source_hashes[str(attempt_path.relative_to(packet.root.parents[2]))] = (
                validity.file_hash(attempt_path)
            )
            packets[spec.candidate.scores[0]] = replace(
                packet,
                manifest=replace(
                    packet.manifest,
                    source_artifact_hashes=tuple(sorted(source_hashes.items())),
                ),
            )
        elif mutation == "model":
            value["provenance"]["resolved_models"]["orchestrator"] = "claude-unpinned"
        elif mutation == "instrumentation":
            value["instrumentation_version"] = "changed"
        elif mutation == "treatment":
            value["treatment_git_sha"] = "changed"
        else:
            value["pr_identities"][window.pr_urls[0]]["head_sha"] = "changed"
        path.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises((ValueError, KeyError)):
        c.compare_arms(spec, packet_loader=packets.__getitem__)


def test_equal_arms_keep_shared_pr_patterns_paired(paired_case):
    _, bugs, spec = paired_case
    vector = tuple(b.pr_url == bugs[0].pr_url for b in bugs)
    report = c.compare_matrix(matrix_for(bugs, [vector] * 3, [vector] * 3), spec)
    for population in report.population_reports:
        assert population.difference == 0
        assert population.interval.lower == population.interval.upper == 0
        assert (
            population.interval.two_sided_lower
            == population.interval.two_sided_upper
            == 0
        )


def test_multi_dimension_contributions_and_funnel_keep_the_fixed_denominator(
    paired_case,
):
    from typing import get_args

    from bench.runner import stages

    _, bugs, spec = paired_case
    matrix = matrix_for(
        bugs, [[1] + [0] * (len(bugs) - 1)] * 3, [[1] + [0] * (len(bugs) - 1)] * 3
    )
    cells = tuple(
        replace(
            row,
            delivered_dimensions=("bug", "security") if row.detected else (),
            discovery_dimensions=("bug", "security") if row.detected else (),
            provenance_available=True,
        )
        for row in matrix.observations
    )
    selections, stage_rows = [], []
    for arm in matrix.arms:
        for run_index in range(3):
            ref = ScoreRef(f"{arm}-{run_index}", spec.epoch_id, "synthetic")
            selections.extend(
                validity.CellSelection(arm, run_index, url, "attempt", ref)
                for url in sorted({b.pr_url for b in bugs})
            )
            for i, bug in enumerate(bugs):
                loss = (
                    None
                    if i == 0
                    else stages.Loss(
                        "verify",
                        "verify",
                        "verify",
                        "verify_eliminated",
                        "trusted fixture",
                        (),
                        (),
                        "established",
                    )
                    if i == 1
                    else stages.Loss(
                        "not_found",
                        None,
                        None,
                        "not_found",
                        "trusted fixture",
                        (),
                        (),
                        "established",
                    )
                )
                for stage in get_args(observations.Stage):
                    survives = i == 0 or (i == 1 and stage == "discover")
                    stage_rows.append(
                        stages.StageBugObservation(
                            ref,
                            "attempt",
                            bug.pr_url,
                            bug.bug_id,
                            stage,
                            survives,
                            (),
                            loss if stage == "deliver" else None,
                            "complete",
                        )
                    )
    report = c.compare_matrix(
        replace(
            matrix,
            observations=cells,
            selections=tuple(selections),
            stage_observations=tuple(stage_rows),
        ),
        spec,
    )
    for population in report.population_reports:
        dimensions = observations.thaw_json(population.dimension_rows)
        for row in dimensions:
            if row["dimension"] in ("bug", "security"):
                assert row["denominator"] == len(bugs) and row[
                    "delivered_recall"
                ] == pytest.approx(1 / len(bugs))
                assert row["overlap"] is True
        funnel = observations.thaw_json(population.baseline_funnels)["runs"][0]
        assert funnel["denominator"] == len(bugs)
        assert funnel["delivered_hits"] + sum(
            value for _, value in funnel["losses"]
        ) + funnel["unavailable_count"] == len(bugs)
        assert (
            funnel["physical_losses"]["verify"] == 1
            and funnel["physical_losses"]["unknown"] == len(bugs) - 2
        )
    unknown = c.compare_matrix(matrix, spec)
    assert all(
        row["delivered_recall"] is None
        for row in observations.thaw_json(unknown.population_reports[0].dimension_rows)
    )

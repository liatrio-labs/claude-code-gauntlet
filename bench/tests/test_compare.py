"""Pure matrices drive rules; loaded evidence drives comparison boundaries."""

import itertools
import random
from dataclasses import asdict, replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from bench.runner import audit as a
from bench.runner import compare as c
from bench.runner import observations as o
from bench.runner import populations as p
from bench.runner import validity as v
from bench.runner.contracts import ScoreRef, exclusive_json
from bench.runner.epochs import canonical, digest, read_json
from bench.tests.conftest import refuses, write
from bench.tests.test_validity import fingerprint as fingerprint
from bench.tests.test_validity import window as window


def rule(kind="wave", **kw):
    r = c.PromotionRule(kind, "core", 7.0, 0.0, 3, 0, 3, c.BootstrapConfig(80, 7), True)
    return c.decode(c.PromotionRule, asdict(replace(r, **kw)))


def paired(spec, packets):
    return c.compare_arms(spec, packet_loader=packets.__getitem__)


@pytest.fixture
def case(pop_case, score_case, tmp_path, fingerprint, window, monkeypatch):
    epoch, manifest = pop_case
    base = o.load_packet(Path(score_case.execute()["ledger_row"]["packet_path"]))
    sha = fingerprint.treatment.treatment_git_sha
    manifest.update(agent_read_sha256="baseline", baseline_treatment_sha=sha)
    paths = {n: tmp_path / (n + ".json") for n in ["contamination", "window", "rule"]}
    ledger = tmp_path / "looks.jsonl"
    monkeypatch.setattr(c.looks, "LEDGER", ledger)
    monkeypatch.setattr(c.looks, "utc_now", lambda: "2026-10-20T00:00:01Z")
    runs, attempts = {}, {}
    monkeypatch.setattr(c, "_packet_run", lambda packet: runs[packet.score_ref])

    def loaded(arm, scope, epoch, *, window=None, window_path=None, packet_loader=None):
        ps = tuple(packet_loader(r) for r in arm.scores)
        data = [(runs[r], (), {}) for r in arm.scores]
        wh = v.file_hash(Path(window_path)) if window else None
        return c.matrix_for_arm(
            arm, scope, epoch, ps, attempts, data, window=window, window_hash=wh
        )

    monkeypatch.setattr(c, "load_arm", loaded)

    def build(population="gate", operational=True, count=3, promotion_rule=None):
        r = promotion_rule or rule()
        w = replace(
            window,
            wave_id="wave",
            rule_sha256=digest(asdict(r)),
            planned_order=tuple((a, i) for i in range(4) for a in window.arms),
        )
        for n, value in (
            ("contamination", manifest),
            ("window", w.wire()),
            ("rule", asdict(r)),
        ):
            write(paths[n], value)
        ledger.write_bytes(b"")
        c.looks.register("wave", asdict(r))
        unseal = str(tmp_path / "unseal.json") if population == "holdout" else None
        if unseal:
            c.looks.unseal(sha, unseal)
        scope = (
            epoch.subsets["gate"]
            if operational
            else p.resolve_population(
                population, epoch, "core", manifest, unseal=unseal, treatment_sha=sha
            ).pr_urls
        )
        packets, arms = {}, []
        for arm in w.arms:
            refs = tuple(
                ScoreRef(f"{arm}-{i}", epoch.epoch_id, "score") for i in range(count)
            )
            arms.append(c.Arm(arm, refs, sha, "baseline"))
            for i, ref in enumerate(refs):
                start = f"2026-10-20T12:{2 * i + (arm == 'candidate'):02}:00Z"
                runs[ref] = dict(
                    run_index=i,
                    provenance=fingerprint.wire(),
                    started=start,
                    window_sha256=v.file_hash(paths["window"]),
                    window_id=w.window_id,
                    measurement_window=str(paths["window"]),
                    arm=arm,
                    instrumentation_version="1",
                    treatment_git_sha=sha,
                    agent_read_sha256="baseline",
                    child_auth="api",
                    contamination_sha256=digest(manifest),
                    pr_identities={u: epoch.shas[u] for u in scope},
                    baseline_equivalence_receipt=dict(
                        release_sha="839b1886", merge_sha=sha, evidence_refs=["fixture"]
                    ),
                )
                sel = {u: f"{ref.run_id}-{j}" for j, u in enumerate(scope)}
                for u, aid in sel.items():
                    valid = v.Validity(
                        "valid", window_id=w.window_id, observed=fingerprint
                    )
                    attempts[ref, u] = v.AttemptRecord(
                        1, aid, ref.run_id, u, start, start, "ok", None, valid, {}, 1
                    )
                bugs = tuple(
                    replace(
                        base.observations[0],
                        score_ref=ref,
                        pr_url=b.pr_url,
                        attempt_id=sel[b.pr_url],
                        bug_id=b.bug_id,
                        category=b.category,
                        outcome="missed",
                        matches=(),
                        errors=(),
                        loss=None,
                    )
                    for b in epoch.bugs
                    if b.pr_url in scope
                )
                clean_url = next(
                    u
                    for u in scope
                    if not any(
                        e["pr_url"] == u and e["scope"] == "pr"
                        for e in manifest["entries"]
                    )
                )
                findings = tuple(
                    replace(f, pr_url=clean_url, attempt_id=sel[clean_url])
                    for f in base.candidates
                )
                m = replace(
                    base.manifest,
                    score_ref=ref,
                    requested_pr_urls=tuple(scope),
                    selected_attempts=sel,
                )
                metadata = {
                    **base.metadata,
                    "run_id": ref.run_id,
                    "review_attempt_costs": dict.fromkeys(sel.values(), 1.0),
                }
                packets[ref] = replace(
                    base,
                    manifest=m,
                    observations=bugs,
                    candidates=findings,
                    metadata=metadata,
                    costs=dict(dedup=0, judge=1, adjudicator=0, attribution=None),
                )
        spec = c.ComparisonSpec(
            "fixture",
            "wave" if operational else "descriptive",
            population,
            "core",
            *arms,
            epoch.epoch_id,
            str(paths["contamination"]),
            str(paths["window"]) if operational else None,
            str(paths["rule"]) if operational else None,
        )
        return replace(spec, unseal=unseal), packets

    return SimpleNamespace(
        build=build,
        epoch=epoch,
        contamination=manifest,
        attempts=attempts,
        runs=runs,
        rule_path=paths["rule"],
        window_path=paths["window"],
        ledger=ledger,
    )


def matrix_for(case, baseline, candidate):
    spec, packets = case.build(count=len(baseline))
    selections, cells, runs = [], [], []
    for arm, vectors in zip(
        (spec.baseline, spec.candidate), (baseline, candidate), strict=True
    ):
        for ref, vector in zip(arm.scores, vectors, strict=True):
            packet = packets[ref]
            bugs = tuple(
                replace(b, outcome="matched" if hit else "missed")
                for b, hit in zip(packet.observations, vector, strict=True)
            )
            packets[ref] = replace(packet, observations=bugs)
        _, ss, cs, _, indices = c.load_arm(
            arm,
            case.epoch.subsets["gate"],
            case.epoch,
            packet_loader=packets.__getitem__,
        )
        selections.extend(ss)
        cells.extend(cs)
        runs.append((arm.name, indices))
    return v.CompleteMatrix(
        tuple(selections), tuple(cells), ("baseline", "candidate"), tuple(runs)
    )


def test_bug_means_cluster_bootstrap_labels_and_unknown_attribution(case):
    e, manifest = case.epoch, case.contamination
    n = sum(b.pr_url in e.subsets["gate"] for b in e.bugs)
    matrix = matrix_for(case, [[0] * n, [1] * n, [0] * n], [[1] * n] * 3)
    pop = p.resolve_population("gate-uncontaminated", e, "core", manifest)
    cfg = c.BootstrapConfig(100, 9)
    result = c.reduce_population(matrix, pop, e, "core", cfg)
    assert result.difference == pytest.approx(2 / 3)
    assert {(b.baseline_label, b.candidate_label) for b in result.bugs} == {
        ("flaky", "stable_found")
    }
    assert result.categories and result.funnels["baseline"]["unavailable_count"] == 150
    assert all(d["delivered"] is None for d in result.dimensions["candidate"].values())
    rows = tuple(
        replace(result.bugs[i], pr_url=u, difference=d)
        for i, (u, d) in enumerate((("a", 0), ("b", 1), ("b", 1)))
    )
    support = [
        sum(map(sum, draw)) / sum(map(len, draw))
        for draw in itertools.product(([0], [1, 1]), repeat=2)
    ]
    rng = random.Random(9)
    draws = sorted(support[2 * rng.randrange(2) + rng.randrange(2)] for _ in range(100))
    interval = c.cluster_bootstrap(rows, cfg)
    assert (interval.lower, interval.two_sided_upper) == (
        c.quantile(draws, 0.05),
        c.quantile(draws, 0.975),
    )
    assert interval == c.cluster_bootstrap(tuple(reversed(rows)), cfg)
    assert (
        c.stability(0, 3, 3) == "stable_missed"
        and c.stability(0, 1, 3) == "insufficient_runs"
    )


@pytest.mark.parametrize("population", ["dev-slice", "dev-pool", "mini"])
def test_descriptive_populations_accept_the_actual_named_scope(case, population):
    spec, packets = case.build(population, False)
    manifest = read_json(spec.contamination_path)
    write(Path(spec.contamination_path), {**manifest, "status": "unfrozen"})
    report = paired(spec, packets)
    assert {p.profile for p in report.population_reports} == {"core", "all", "strict"}
    assert report.promotion is None and report.spend["scoring_cost_usd"] == 6
    assert report.spend["attribution_cost_usd"] is None
    expected = p.resolve_population(population, case.epoch, "core", manifest)
    assert report.population_reports[0].content_sha256 == expected.content_sha256
    assert report.identity["populations"] == {
        p.population + "/" + p.profile: p.content_sha256
        for p in report.population_reports
    }
    refuses(paired, replace(spec, population="gate"), packets)


@pytest.mark.parametrize(
    "bad",
    [
        "context",
        "epoch",
        "scope",
        "bugs",
        "attempts",
        "independence",
        "order",
        "late",
        "rule",
    ],
)
def test_operational_comparison_refuses_verdict_corruption(case, bad, tmp_path):
    from bench.tests.test_audit import resolve_audit

    spec, packets = case.build()
    _, _, receipt = resolve_audit(spec, packets, tmp_path / "audit")
    spec = replace(spec, audit_path=str(receipt))
    assert paired(spec, packets).promotion
    ref = spec.candidate.scores[0]
    packet = packets[ref]
    if bad == "context":
        packet = replace(
            packet,
            manifest=replace(
                packet.manifest,
                evaluator=replace(packet.manifest.evaluator, judge_pin="other"),
            ),
        )
    if bad == "epoch":
        packet = replace(packet, manifest=replace(packet.manifest, epoch_hashes={}))
    if bad == "scope":
        packet = replace(
            packet, manifest=replace(packet.manifest, requested_pr_urls=())
        )
    if bad == "bugs":
        packet = replace(packet, observations=packet.observations[:-1])
    if bad == "attempts":
        packet = replace(
            packet, manifest=replace(packet.manifest, selected_attempts={})
        )
    if bad == "independence":
        spec = replace(
            spec, candidate=replace(spec.candidate, scores=spec.baseline.scores)
        )
    if bad == "order":
        for (r, u), attempt in list(case.attempts.items()):
            if r == ref:
                case.attempts[r, u] = replace(
                    attempt, started_at="2026-10-20T13:00:00Z"
                )
    if bad == "late":
        row = c.looks.entries()[0]
        case.ledger.write_bytes(
            canonical({**row, "at": "2026-10-20T14:00:00Z"}) + b"\n"
        )
    if bad == "rule":
        value = asdict(rule(margin_pp=20))
        write(case.rule_path, value)
        w = read_json(case.window_path)
        write(case.window_path, {**w, "rule_sha256": digest(value)})
        for run in case.runs.values():
            run["window_sha256"] = v.file_hash(case.window_path)
    packets[ref] = packet
    refuses(paired, spec, packets)


def test_extension_recomputes_its_initial_prefix(case, tmp_path):
    from bench.tests.test_audit import resolve_audit

    spec, packets = case.build(
        count=4, promotion_rule=rule(margin_pp=0, extension_runs=1)
    )
    for ref in spec.candidate.scores:
        packets[ref] = replace(packets[ref], candidates=())
    initial = replace(
        spec,
        baseline=replace(spec.baseline, scores=spec.baseline.scores[:3]),
        candidate=replace(spec.candidate, scores=spec.candidate.scores[:3]),
    )
    _, _, receipt = resolve_audit(initial, packets, tmp_path / "initial-audit")
    initial = replace(initial, audit_path=str(receipt))
    ip, rp = tmp_path / "initial.json", tmp_path / "report.json"
    write(ip, asdict(initial))
    report = paired(initial, packets)
    assert report.promotion.extension_allowed
    c.looks.record(v.load_window(case.window_path), initial, report)
    write(rp, asdict(report))
    _, _, receipt = resolve_audit(spec, packets, tmp_path / "extended-audit")
    extended = replace(
        spec,
        audit_path=str(receipt),
        initial_spec_path=str(ip),
        initial_report_path=str(rp),
    )
    assert not paired(extended, packets).promotion.extension_allowed
    write(rp, {**asdict(report), "spend": {}})
    refuses(paired, extended, packets, match="initial report")
    write(
        ip,
        asdict(
            replace(
                initial,
                candidate=replace(
                    initial.candidate, scores=tuple(reversed(initial.candidate.scores))
                ),
            )
        ),
    )
    refuses(paired, extended, packets, match="substituted")


@pytest.mark.parametrize("first", ["pass", "fail", "inconclusive"])
def test_command_records_and_limits_wave_looks(case, tmp_path, monkeypatch, first):
    from bench.tests.test_audit import resolve_audit

    r = (
        rule(margin_pp=0, extension_runs=int(first == "inconclusive"))
        if first != "pass"
        else rule()
    )
    spec, packets = case.build(promotion_rule=r)
    if first == "inconclusive":
        packets = {ref: replace(p, candidates=()) for ref, p in packets.items()}
    _, _, receipt = resolve_audit(spec, packets, tmp_path / "audit")
    spec = replace(spec, audit_path=str(receipt))
    path = tmp_path / "spec.json"
    write(path, asdict(spec))
    compare = c.compare_arms
    monkeypatch.setattr(
        c, "compare_arms", lambda s: compare(s, packet_loader=packets.__getitem__)
    )
    assert c.main(["--spec", str(path), "--out", str(tmp_path / "one.json")]) == 0
    rows = c.looks.entries()
    assert [r["kind"] for r in rows] == ["registration", "look"]
    assert rows[-1]["look"] == 1 and rows[-1]["verdict"] == first
    assert rows[-1]["score_refs"] == [
        asdict(r) for a in (spec.baseline, spec.candidate) for r in a.scores
    ]
    refuses(
        c.main,
        ["--spec", str(path), "--out", str(tmp_path / "three.json")],
        match="remaining",
    )
    refuses(
        c.looks.register, "wave", asdict(rule(margin_pp=99)), match="already registered"
    )


def test_selected_attempt_loads_real_immutable_evidence(case, tmp_path):
    spec, packets = case.build()
    ref = spec.baseline.scores[0]
    url = next(iter(packets[ref].manifest.selected_attempts))
    attempt = case.attempts[ref, url]
    run = tmp_path / "actual"
    path = run / "attempts/pr" / attempt.attempt_id / "attempt.json"
    exclusive_json(path, asdict(attempt))
    state = run / "state/pr.json"
    exclusive_json(state, dict(url=url, detail=dict(attempt_id=attempt.attempt_id)))
    m = replace(
        packets[ref].manifest,
        source_artifact_hashes={str(path.relative_to(run)): v.file_hash(path)},
    )
    packet = replace(packets[ref], root=run / "scores/epoch/score", manifest=m)
    assert c.load_selected_attempt(packet, url, attempt.attempt_id) == attempt
    confirmation = run / "failure-confirmations" / (attempt.attempt_id + ".json")
    exclusive_json(confirmation, {"proof": "original"})
    hashes = {
        **m.source_artifact_hashes,
        str(confirmation.relative_to(run)): v.file_hash(confirmation),
    }
    packet = replace(packet, manifest=replace(m, source_artifact_hashes=hashes))
    original_confirmation = confirmation.read_bytes()
    write(confirmation, {"proof": "changed"})
    refuses(
        c.load_selected_attempt,
        packet,
        url,
        attempt.attempt_id,
        match="failure confirmation",
    )
    confirmation.write_bytes(original_confirmation)
    write(state, dict(url=url, detail=dict(attempt_id="other")))
    refuses(c.load_selected_attempt, packet, url, attempt.attempt_id, match="current")
    write(state, dict(url=url, detail=dict(attempt_id=attempt.attempt_id)))
    write(path, {**asdict(attempt), "sequence": 2})
    refuses(c.load_selected_attempt, packet, url, attempt.attempt_id, match="immutable")


def test_descriptive_gate_is_refused_before_loading(case):
    spec, packets = case.build()
    descriptive = replace(spec, purpose="descriptive", window_path=None, rule_path=None)
    refuses(paired, descriptive, packets, match="descriptive")


def test_candidate_dispatch_requires_registration_before_prerequisites(
    case, fingerprint, monkeypatch
):
    from bench import run

    case.build()
    case.ledger.write_bytes(b"")
    monkeypatch.setattr(v, "require_clean_treatment", lambda: None)
    monkeypatch.setattr(v, "run_provenance", lambda **kw: fingerprint.wire())
    monkeypatch.setattr(
        run, "check_prereqs", lambda **kw: pytest.fail("prerequisites reached")
    )
    assert (
        run.main(
            [
                "--measurement-window",
                str(case.window_path),
                "--tier",
                "subset",
                "--arm",
                "candidate",
                "--run-index",
                "0",
                "--child-model",
                "claude-session-1",
            ]
        )
        == 2
    )


@pytest.mark.parametrize("population", ["gate", "holdout"])
@pytest.mark.parametrize("failure", ["existing", "io", None])
def test_comparison_persists_report_before_spending(
    case, tmp_path, monkeypatch, population, failure
):
    from bench.tests.test_audit import resolve_audit

    spec, packets = case.build(population, population == "gate")
    if population == "gate":
        _, _, receipt = resolve_audit(spec, packets, tmp_path / "audit")
        spec = replace(spec, audit_path=str(receipt))
    path = tmp_path / "spec.json"
    write(path, asdict(spec))
    compare = c.compare_arms
    monkeypatch.setattr(
        c, "compare_arms", lambda s: compare(s, packet_loader=packets.__getitem__)
    )
    out = tmp_path / "report.json"
    before = case.ledger.read_bytes()
    argv = ["--spec", str(path), "--out", str(out)]
    if failure == "existing":
        out.write_bytes(b"keep")
        with pytest.raises(FileExistsError):
            c.main(argv)
        assert out.read_bytes() == b"keep"
    elif failure == "io":
        with monkeypatch.context() as patch:

            def cannot_write(*args):
                raise OSError("disk unavailable")

            patch.setattr(c, "exclusive_json", cannot_write)
            with pytest.raises(OSError, match="disk unavailable"):
                c.main(argv)
    assert case.ledger.read_bytes() == before
    out = tmp_path / "retry.json"
    append = c.looks.append

    def require_report(row, check):
        saved = read_json(out)
        if row["kind"] == "look":
            assert digest(saved) == row["report_sha256"]
            assert saved["identity"]["look"] == row["look"] == 1
        else:
            assert row["kind"] == "holdout-use" and row["use"] == "compare"
            assert saved["population_reports"][0]["population"] == "holdout"
        return append(row, check)

    monkeypatch.setattr(c.looks, "append", require_report)
    assert c.main(["--spec", str(path), "--out", str(out)]) == 0
    assert len(c.looks.entries()) == len(before.splitlines()) + 1


@pytest.mark.parametrize("boundary", ["preflight", "compare"])
def test_extension_capacity_is_required_before_initial_runs(
    case, fingerprint, tmp_path, monkeypatch, boundary
):
    from bench import run
    from bench.tests.test_audit import resolve_audit

    spec, packets = case.build(promotion_rule=rule(extension_runs=1))
    _, _, receipt = resolve_audit(spec, packets, tmp_path / "audit")
    spec = replace(spec, audit_path=str(receipt))
    window = v.load_window(case.window_path)
    window = replace(
        window,
        planned_run_indices=(0, 1, 2),
        planned_order=tuple((a, i) for a, i in window.planned_order if i < 3),
    )
    write(case.window_path, window.wire())
    for row in case.runs.values():
        row["window_sha256"] = v.file_hash(case.window_path)
    if boundary == "compare":
        refuses(paired, spec, packets, match="full rule run count")
    else:
        monkeypatch.setattr(v, "require_clean_treatment", lambda: None)
        monkeypatch.setattr(v, "run_provenance", lambda **kw: fingerprint.wire())
        monkeypatch.setattr(
            run, "check_prereqs", lambda **kw: pytest.fail("prerequisites reached")
        )
        assert (
            run.main(
                [
                    "--measurement-window",
                    str(case.window_path),
                    "--tier",
                    "subset",
                    "--arm",
                    "baseline",
                    "--run-index",
                    "0",
                    "--child-model",
                    "claude-session-1",
                    "--rule",
                    str(case.rule_path),
                ]
            )
            == 2
        )


@pytest.fixture
def decision_case():
    def build(differences=(0.0, 0.0), losses=0, correct=29):
        bugs = tuple(
            c.BugComparison(
                str(i),
                str(i),
                None,
                0.5,
                0.5 + d,
                d,
                "stable_found",
                "stable_missed",
                i < losses,
            )
            for i, d in enumerate(differences)
        )
        interval = c.cluster_bootstrap(bugs, c.BootstrapConfig(1000))
        precision = c.PrecisionComparison(
            0.8, 0.8, 0, c.Interval(-0.1, 0, -0.2, 0.1, 2)
        )
        primary = c.PopulationComparison(
            "gate-uncontaminated",
            "h",
            "core",
            len(bugs),
            0.5,
            0.5 + sum(differences) / len(bugs),
            sum(differences) / len(bugs),
            interval,
            bugs,
            {},
            {},
            {},
            precision,
        )
        nonmini = replace(
            primary,
            population="gate-uncontaminated-non-mini",
            interval=c.Interval(-0.2, 0, -0.3, 0.1, 2),
        )
        receipt = a.AuditReceipt(
            ("baseline", "candidate"),
            tuple(
                a.AuditRow(arm, i < (30 if arm == "baseline" else correct))
                for arm in ("baseline", "candidate")
                for i in range(30)
            ),
        )
        return (
            (primary, nonmini),
            receipt,
            rule(extension_runs=3, bootstrap=c.BootstrapConfig(1000)),
        )

    return build


def test_primary_pass_stops_early_and_reports_improvement(decision_case):
    reports, receipt, r = decision_case((0.06, 0.06))
    result = c.evaluate_promotion(reports, receipt, r)
    assert (
        result.verdict == "pass" and not result.extension_allowed and result.improvement
    )
    refuses(c.evaluate_promotion, reports, None, r, match="audit")
    assert result.bounds["primary_recall"] == 0.06
    assert (
        set(result.guards)
        == set(result.bounds)
        == {"primary_recall", "non-mini", "stable_loss", "precision", "groundedness"}
    )
    reports, receipt, r = decision_case((0.04, 0.04))
    assert not c.evaluate_promotion(reports, receipt, r).improvement


def test_primary_point_below_margin_stops_for_futility(decision_case):
    reports, receipt, r = decision_case((-0.3, 0.1))
    result = c.evaluate_promotion(reports, receipt, r)
    assert result.verdict == "fail" and not result.extension_allowed
    assert result.guards["primary_recall"] == "fail"


@pytest.mark.parametrize(
    "final_differences, verdict", [((0, 0), "pass"), ((-0.2, 0.2), "fail")]
)
def test_extension_decides_once_without_final_inconclusive(
    decision_case, final_differences, verdict
):
    reports, receipt, r = decision_case((-0.2, 0.2))
    initial = c.evaluate_promotion(reports, receipt, r)
    assert initial.verdict == "inconclusive" and initial.extension_allowed
    final, _, _ = decision_case(final_differences)
    result = c.evaluate_promotion(final, receipt, r, extended=True)
    assert result.verdict == verdict and not result.extension_allowed


@pytest.mark.parametrize("kind", ["wave", "ship"])
@pytest.mark.parametrize(
    "guard", ["non-mini", "stable_loss", "precision", "groundedness"]
)
@pytest.mark.parametrize("harm", [False, True])
def test_secondary_checks_fail_only_on_harm(decision_case, kind, guard, harm):
    reports, receipt, r = decision_case(
        losses=2 if guard == "stable_loss" else 1,
        correct=15 if guard == "groundedness" and harm else 29,
    )
    primary, nonmini = reports
    upper = -0.001 if harm else 0
    if guard == "non-mini":
        nonmini = replace(nonmini, interval=replace(nonmini.interval, upper=upper))
    if guard == "precision":
        primary = replace(
            primary,
            precision=replace(
                primary.precision,
                interval=replace(primary.precision.interval, upper=upper),
            ),
        )
    r = replace(
        r,
        kind=kind,
        extension_runs=3 if kind == "wave" else 0,
        stable_loss_min=2 if harm else 3,
    )
    result = c.evaluate_promotion((primary, nonmini), receipt, r)
    assert result.guards[guard] == ("fail" if harm else "pass")
    assert (
        result.verdict == ("fail" if harm else "pass") and not result.extension_allowed
    )
    if guard in ("non-mini", "precision"):
        assert result.bounds[guard] == upper
    if guard == "groundedness":
        # Independent Newcombe upper for 30/30 versus 15/30, or 29/30.
        assert result.bounds[guard] == pytest.approx(
            -0.33409524363041554 if harm else 0.053339188189749906
        )


def test_stage_alpha_controls_the_primary_bound(decision_case):
    reports, receipt, r = decision_case(
        (-0.15, -0.12, -0.08, -0.06, -0.04, 0.02, 0.04, 0.09)
    )
    bounds = [
        c.cluster_bootstrap(reports[0].bugs, replace(r.bootstrap, alpha=alpha)).lower
        for alpha in (0.0294, 0.2)
    ]
    margin = -100 * sum(bounds) / 2
    decisions = [
        c.evaluate_promotion(
            reports, receipt, replace(r, stage_alpha=alpha, margin_pp=margin)
        )
        for alpha in (0.0294, 0.2)
    ]
    assert [d.bounds["primary_recall"] for d in decisions] == bounds
    assert [d.verdict for d in decisions] == ["inconclusive", "pass"]


@pytest.mark.parametrize(
    "kw",
    [
        {"stage_alpha": 0},
        {"stage_alpha": 0.5},
        {"stage_alpha": float("nan")},
        {"stable_loss_min": 0},
        {"stable_loss_min": 1.5},
        {"initial_runs": 0},
        {"extension_runs": -1},
        {"stability_min_runs": 0},
        {"kind": "ship", "extension_runs": 3},
    ],
)
def test_rule_validation_rejects_invalid_designs(kw):
    refuses(rule, **kw)


def test_precision_pools_stored_core_counts_and_bootstraps_paired_prs(case, tmp_path):
    from bench.tests.test_audit import resolve_audit

    spec, packets = case.build()
    _, _, receipt = resolve_audit(spec, packets, tmp_path / "audit")
    spec = replace(spec, audit_path=str(receipt))
    population = p.resolve_population(
        "gate-uncontaminated", case.epoch, "core", case.contamination
    )
    urls = population.pr_urls[:2]
    totals = {arm.name: {} for arm in (spec.baseline, spec.candidate)}
    for arm in (spec.baseline, spec.candidate):
        for run, ref in enumerate(arm.scores):
            packet = packets[ref]
            root = tmp_path / ref.run_id
            root.mkdir()
            raw = {
                u: {
                    "deep-review": {
                        "fp": (run + 1) * (j + 1) * (1 if arm.name == "baseline" else 2)
                    }
                }
                for j, u in enumerate(urls)
            }
            raw.update(
                {
                    u: {"deep-review": {"fp": 10000}}
                    for u in case.epoch.subsets["gate"]
                    if u not in population.pr_urls
                }
            )
            write(root / "evaluations.json", raw)
            bugs = tuple(
                replace(b, outcome="matched")
                if b.pr_url in urls and b.category == "bug"
                else b
                for b in packet.observations
            )
            packets[ref] = replace(packet, root=root, observations=bugs)
            for url in population.pr_urls:
                tp = sum(
                    b.pr_url == url and b.outcome == "matched" and b.category == "bug"
                    for b in bugs
                )
                fp = raw.get(url, {}).get("deep-review", {}).get("fp", 0)
                old = totals[arm.name].get(url, (0, 0))
                totals[arm.name][url] = (old[0] + tp, old[1] + fp)
    report = next(
        r
        for r in paired(spec, packets).population_reports
        if r.population == "gate-uncontaminated" and r.profile == "core"
    ).precision

    def ratio(rows):
        tp, fp = map(sum, zip(*rows, strict=True))
        return tp / (tp + fp) if tp + fp else 0

    prs = sorted(totals["baseline"])
    rng = random.Random(0)
    draws = []
    for _ in range(10000):
        sample = [prs[rng.randrange(len(prs))] for _ in prs]
        draws.append(
            ratio([totals["candidate"][u] for u in sample])
            - ratio([totals["baseline"][u] for u in sample])
        )
    assert report.baseline_precision == ratio(totals["baseline"].values())
    assert report.candidate_precision == ratio(totals["candidate"].values())
    assert report.interval.upper == c.quantile(sorted(draws), 0.95)

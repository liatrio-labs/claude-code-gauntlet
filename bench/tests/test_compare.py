"""Pure matrices drive rules; loaded evidence drives comparison boundaries."""

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
from bench.runner.epochs import canonical, digest, read_json
from bench.tests.conftest import refuses, write
from bench.tests.test_validity import fingerprint as fingerprint
from bench.tests.test_validity import window as window


def rule(kind="wave", **kw):
    r = c.PromotionRule(
        kind, "core", -10.0, -10.0, 3, 0, 3, c.BootstrapConfig(80, 7), True
    )
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
                o.ScoreRef(f"{arm}-{i}", epoch.epoch_id, "score") for i in range(count)
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
                findings = tuple(
                    replace(f, pr_url=scope[0], attempt_id=sel[scope[0]])
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
    assert result.categories and result.funnels["baseline"]["unavailable_count"] == 50
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


@pytest.mark.parametrize("kind", ["wave", "ship"])
def test_rule_kinds_margins_guards_and_reported_improvement(case, kind):
    e, manifest = case.epoch, case.contamination
    n = sum(b.pr_url in e.subsets["gate"] for b in e.bugs)
    matrix = matrix_for(case, [[0] * n] * 3, [[1] * n] * 3)
    reports = tuple(
        c.reduce_population(
            matrix,
            p.resolve_population(name, e, "core", manifest),
            e,
            "core",
            c.BootstrapConfig(50),
        )
        for name in ("gate-uncontaminated", "gate-uncontaminated-non-mini")
    )
    receipt = dict(
        resolved=True,
        agreed=[dict(arm=a, correct=True) for a in matrix.arms],
        arm_order=list(matrix.arms),
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
    assert c.evaluate_promotion(
        equal, receipt, replace(r, margin_pp=10, nonmini_margin_pp=10)
    ).verdict == ("pass" if kind == "ship" else "fail")
    assert not c.evaluate_promotion(equal, receipt, r).improvement
    bad = tuple(
        replace(
            p,
            interval=c.Interval(-0.5, -0.5, -0.5, -0.5, 13),
            bugs=tuple(replace(b, stable_loss=True) for b in p.bugs),
        )
        for p in reports
    )
    labels = {
        **receipt,
        "agreed": [dict(arm=a, correct=a == "baseline") for a in matrix.arms],
    }
    decision = c.evaluate_promotion(bad, labels, r)
    guards = (
        ("primary_recall", "non-mini")
        if kind == "ship"
        else ("primary_recall", "non-mini", "groundedness", "stable_loss")
    )
    assert decision.verdict == "fail" and decision.guards == dict.fromkeys(
        guards, "fail"
    )
    uncertain = tuple(
        replace(p, interval=c.Interval(-0.2, 0.2, -0.3, 0.3, 13)) for p in reports
    )
    assert c.evaluate_promotion(
        uncertain, receipt, replace(r, extension_runs=1) if kind == "wave" else r
    ).extension_allowed == (kind == "wave")
    if kind == "wave":
        refuses(c.evaluate_promotion, reports, None, r, match="audit")


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

    case = case
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

    case = case
    spec, packets = case.build(count=4, promotion_rule=rule(extension_runs=1))
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

    r = rule(margin_pp=0, nonmini_margin_pp=0) if first == "fail" else rule()
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
    if first != "pass":
        c.main(["--spec", str(path), "--out", str(tmp_path / "two.json")])
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
    o.exclusive_json(path, asdict(attempt))
    state = run / "state/pr.json"
    o.exclusive_json(state, dict(url=url, detail=dict(attempt_id=attempt.attempt_id)))
    m = replace(
        packets[ref].manifest,
        source_artifact_hashes={str(path.relative_to(run)): v.file_hash(path)},
    )
    packet = replace(packets[ref], root=run / "scores/epoch/score", manifest=m)
    assert c.load_selected_attempt(packet, url, attempt.attempt_id) == attempt
    confirmation = run / "failure-confirmations" / (attempt.attempt_id + ".json")
    o.exclusive_json(confirmation, {"proof": "original"})
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

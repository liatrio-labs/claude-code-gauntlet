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


def audited(spec, packets, out):
    from bench.tests.test_audit import resolve_audit

    return replace(spec, audit_path=str(resolve_audit(spec, packets, out)[2]))


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

    def build(
        population="gate",
        operational=True,
        count=3,
        promotion_rule=None,
        *,
        wave="wave",
        registered_baseline=None,
        baseline_source_sha=None,
    ):
        r = promotion_rule or rule()
        w = replace(
            window,
            wave_id=wave,
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
        c.looks.register(
            wave,
            asdict(r),
            registered_baseline or sha,
            baseline_source_sha=baseline_source_sha,
        )
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
        if not operational:
            write(paths["window"], replace(w, pr_urls=scope).wire())
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
        "harness-scoring",
        "epoch",
        "scope",
        "bugs",
        "attempts",
        "independence",
        "order",
        "late",
        "rule",
        "unbalanced",
        "undeclared",
        "duplicate-window",
        "provenance",
        "scoring-controls",
    ],
)
def test_operational_comparison_refuses_verdict_corruption(case, bad, tmp_path):
    spec, packets = case.build()
    spec = split_blocks(case, spec, ((0,), (1, 2)))
    spec = audited(spec, packets, tmp_path / "audit")
    ref = spec.candidate.scores[0]
    packet = packets[ref]
    manifest_changes = {
        "context": dict(
            evaluator=replace(packet.manifest.evaluator, judge_pin="other")
        ),
        "harness-scoring": dict(
            evaluator=replace(packet.manifest.evaluator, harness_scoring_sha256="other")
        ),
        "epoch": dict(epoch_hashes={}),
        "scope": dict(requested_pr_urls=()),
        "attempts": dict(selected_attempts={}),
    }
    if bad in manifest_changes:
        packet = replace(
            packet, manifest=replace(packet.manifest, **manifest_changes[bad])
        )
    if bad == "bugs":
        packet = replace(packet, observations=packet.observations[:-1])
    if bad == "independence":
        # A distinct score of a run the baseline already holds is a rescore, not a run.
        rescore = replace(spec.baseline.scores[0], score_id="rescore")
        spec = replace(
            spec,
            candidate=replace(
                spec.candidate, scores=(rescore, *spec.candidate.scores[1:])
            ),
        )
    if bad == "order":
        for (r, u), attempt in list(case.attempts.items()):
            if r == ref:
                case.attempts[r, u] = replace(
                    attempt, started_at="2026-10-20T11:00:00Z"
                )
    if bad == "late":
        row = c.looks.entries()[0]
        case.ledger.write_bytes(
            canonical({**row, "at": "2026-10-20T14:00:00Z"}) + b"\n"
        )
    if bad == "rule":
        value = asdict(rule(margin_pp=20))
        write(case.rule_path, value)
        for path in map(Path, spec.window_paths):
            write(path, {**read_json(path), "rule_sha256": digest(value)})
        for run in case.runs.values():
            run["window_sha256"] = v.file_hash(Path(run["measurement_window"]))
    if bad == "unbalanced":
        path = Path(spec.window_paths[0])
        bind_run(case, spec.candidate.scores[-1], v.load_window(path), path, 1)
    if bad == "undeclared":
        case.runs[ref]["window_id"] = "undeclared"
    if bad == "duplicate-window":
        spec = replace(spec, window_paths=(*spec.window_paths, spec.window_paths[0]))
    if bad == "provenance":
        case.runs[spec.candidate.scores[-1]]["provenance"]["controls"]["auth_mode"] = (
            "api"
        )
    if bad == "scoring-controls":
        path = Path(spec.window_paths[1])
        block = v.load_window(path)
        block = replace(block, expected=replace(block.expected, judge_pin="other"))
        write(path, block.wire())
        for arm in (spec.baseline, spec.candidate):
            for index, other in enumerate(arm.scores[1:]):
                bind_run(case, other, block, path, index)
    packets[ref] = packet
    match = {
        "independence": "duplicate score reference or review run",
        "unbalanced": "unbalanced block",
    }.get(bad)
    refuses(paired, spec, packets, match=match)


@pytest.mark.parametrize("first", ["pass", "fail", "inconclusive"])
def test_command_records_and_limits_wave_looks(case, tmp_path, monkeypatch, first):
    r = (
        rule(margin_pp=0, extension_runs=int(first == "inconclusive"))
        if first != "pass"
        else rule()
    )
    spec, packets = case.build(
        promotion_rule=r, count=4 if first == "inconclusive" else 3
    )
    spec = split_blocks(
        case, spec, ((0,), (1, 2), (3,)) if first == "inconclusive" else ((0,), (1, 2))
    )
    full = spec
    if first == "inconclusive":
        spec = replace(
            spec,
            baseline=replace(spec.baseline, scores=spec.baseline.scores[:3]),
            candidate=replace(spec.candidate, scores=spec.candidate.scores[:3]),
        )
    if first == "inconclusive":
        packets = {ref: replace(p, candidates=()) for ref, p in packets.items()}
    spec = audited(spec, packets, tmp_path / "audit")
    path = tmp_path / "spec.json"
    write(
        path,
        {**asdict(spec), "window_paths": [Path(p).name for p in spec.window_paths]},
    )
    compare = c.compare_arms

    def compare_with_packets(value, **kwargs):
        kwargs.setdefault("packet_loader", packets.__getitem__)
        return compare(value, **kwargs)

    monkeypatch.setattr(c, "compare_arms", compare_with_packets)
    assert c.main(["--spec", str(path), "--out", str(tmp_path / "one.json")]) == 0
    rows = c.looks.entries()
    assert [r["kind"] for r in rows] == ["registration", "look"]
    assert rows[-1]["look"] == 1 and rows[-1]["verdict"] == first
    assert rows[-1]["window_ids"] == [str(i) for i in range(len(spec.window_paths))]
    assert rows[-1]["score_refs"] == [
        asdict(r) for a in (spec.baseline, spec.candidate) for r in a.scores
    ]
    if first == "inconclusive":
        extended = replace(
            audited(full, packets, tmp_path / "extended-audit"),
            initial_spec_path=str(path),
            initial_report_path=str(tmp_path / "one.json"),
        )
        assert not paired(extended, packets).promotion.extension_allowed
        split_blocks(case, full, ((3,), (1, 2), (0,)))
        refuses(paired, extended, packets, match="initial run block assignment")
        split_blocks(case, full, ((0,), (1, 2), (3,)))
        initial_report = read_json(tmp_path / "one.json")
        write(tmp_path / "one.json", {**initial_report, "spend": {}})
        refuses(paired, extended, packets, match="initial report")
        write(tmp_path / "one.json", initial_report)
        initial_spec = read_json(tmp_path / "spec.json")
        initial_spec["candidate"]["scores"].reverse()
        write(tmp_path / "spec.json", initial_spec)
        refuses(paired, extended, packets, match="substituted")
        write(tmp_path / "spec.json", asdict(spec))
        path = tmp_path / "extended.json"
        write(path, asdict(extended))
        assert c.main(["--spec", str(path), "--out", str(tmp_path / "two.json")]) == 0
        rows = c.looks.entries()
        assert [r["look"] for r in rows if r["kind"] == "look"] == [1, 2]
        saved = read_json(tmp_path / "two.json")
        assert rows[-1]["report_sha256"] == digest(saved)
        assert rows[-1]["score_refs"] == [
            asdict(r) for arm in (full.baseline, full.candidate) for r in arm.scores
        ]
        assert not saved["promotion"]["extension_allowed"]
    refuses(
        c.main,
        ["--spec", str(path), "--out", str(tmp_path / "three.json")],
        match="remaining",
    )
    refuses(
        c.looks.register,
        "wave",
        asdict(rule(margin_pp=99)),
        spec.baseline.treatment_git_sha,
        match="already registered",
    )


@pytest.mark.parametrize(
    "changed,field,value",
    [
        ("attempt", "sequence", 2),
        ("selection", "detail", {"attempt_id": "other"}),
        ("confirmation", "proof", "changed"),
    ],
)
def test_selected_attempt_loads_real_immutable_evidence(
    case, tmp_path, changed, field, value
):
    spec, packets = case.build()
    ref = spec.baseline.scores[0]
    url, aid = next(iter(packets[ref].manifest.selected_attempts.items()))
    attempt = case.attempts[ref, url]
    paths = {
        "attempt": tmp_path / "attempts/pr" / aid / "attempt.json",
        "selection": tmp_path / "state/pr.json",
        "confirmation": tmp_path / "failure-confirmations" / (aid + ".json"),
    }
    values = dict(
        attempt=asdict(attempt),
        selection=dict(url=url, detail=dict(attempt_id=aid)),
        confirmation=dict(proof="original"),
    )
    for key, path in paths.items():
        exclusive_json(path, values[key])
    hashes = {
        str(path.relative_to(tmp_path)): v.file_hash(path)
        for key, path in paths.items()
        if key != "selection"
    }
    packet = replace(
        packets[ref],
        root=tmp_path / "scores/epoch/score",
        manifest=replace(packets[ref].manifest, source_artifact_hashes=hashes),
    )
    assert c.load_selected_attempt(packet, url, aid) == attempt
    write(paths[changed], {**values[changed], field: value})
    refuses(c.load_selected_attempt, packet, url, aid)


def test_descriptive_gate_is_refused_before_loading(case):
    spec, packets = case.build()
    descriptive = replace(spec, purpose="descriptive", window_path=None, rule_path=None)
    refuses(paired, descriptive, packets, match="descriptive")


def test_later_merge_baseline_is_accepted_when_preregistered(case, tmp_path):
    spec, packets = case.build(wave="wave-2")
    spec = audited(spec, packets, tmp_path / "audit")
    assert (
        c.looks.entries()[0]["baseline_treatment_sha"]
        == spec.baseline.treatment_git_sha
    )
    assert all("baseline_equivalence_receipt" not in r for r in case.runs.values())
    assert paired(spec, packets).promotion is not None


def test_baseline_runs_must_match_preregistered_treatment(case, tmp_path):
    spec, packets = case.build(wave="wave-2", registered_baseline="another-main-tree")
    spec = audited(spec, packets, tmp_path / "audit")
    refuses(
        paired, spec, packets, match="baseline treatment differs from preregistration"
    )


@pytest.mark.parametrize(
    "evidence", ["missing", "source", "treatment", "refs", "valid"]
)
def test_substituted_baseline_requires_pair_bound_equivalence(case, tmp_path, evidence):
    spec, packets = case.build(baseline_source_sha="source-release")
    spec = audited(spec, packets, tmp_path / "audit")
    receipt = dict(
        release_sha="source-release",
        merge_sha=spec.baseline.treatment_git_sha,
        evidence_refs=["reviewed-equivalence"],
    )
    if evidence == "source":
        receipt["release_sha"] = "another-source"
    if evidence == "treatment":
        receipt["merge_sha"] = "another-treatment"
    if evidence == "refs":
        receipt["evidence_refs"] = []
    if evidence != "missing":
        for ref in spec.baseline.scores:
            case.runs[ref]["baseline_equivalence_receipt"] = receipt
    if evidence == "valid":
        assert paired(spec, packets).promotion is not None
    else:
        refuses(
            paired, spec, packets, match="baseline substitution equivalence receipt"
        )


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


@pytest.mark.parametrize("binding", ["missing", "different", "stale-freeze"])
def test_candidate_preflight_refuses_unbound_baseline(
    case, fingerprint, monkeypatch, capsys, binding
):
    from bench import run

    spec, _ = case.build()
    registered = c.looks.entries()[0]
    if binding == "missing":
        registered.pop("baseline_treatment_sha")
    if binding == "different":
        registered["baseline_treatment_sha"] = "another-main-tree"
    case.ledger.write_bytes(canonical(registered) + b"\n")
    manifest = read_json(spec.contamination_path)
    manifest["agent_read_sha256"] = spec.baseline.treatment_git_sha
    if binding == "stale-freeze":
        manifest["baseline_treatment_sha"] = "old-main-tree"
    write(Path(spec.contamination_path), manifest)
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
                "--rule",
                str(case.rule_path),
                "--contamination",
                spec.contamination_path,
            ]
        )
        == 2
    )
    expected = (
        "contamination baseline treatment/hash/report mismatch"
        if binding == "stale-freeze"
        else "baseline treatment differs from preregistration"
    )
    captured = capsys.readouterr()
    assert expected in captured.out + captured.err


@pytest.mark.parametrize("population", ["gate", "holdout"])
@pytest.mark.parametrize("failure", ["existing", "io", None])
def test_comparison_persists_report_before_spending(
    case, tmp_path, monkeypatch, population, failure
):
    spec, packets = case.build(population, population == "gate")
    if population == "gate":
        spec = audited(spec, packets, tmp_path / "audit")
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
@pytest.mark.parametrize("schema_version", [1, 2])
def test_rule_capacity_is_checked_at_the_schema_boundary(
    case, fingerprint, tmp_path, monkeypatch, boundary, schema_version
):
    from bench import run

    spec, packets = case.build(promotion_rule=rule(extension_runs=1))
    spec = audited(spec, packets, tmp_path / "audit")
    window = v.load_window(case.window_path)
    window = replace(
        window,
        schema_version=schema_version,
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
        reached = []

        def prerequisites(**kw):
            reached.append(True)
            return ["synthetic prerequisite"]

        monkeypatch.setattr(run, "check_prereqs", prerequisites)
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

        assert reached == ([True] if schema_version == 2 else [])


def test_a_run_pair_may_start_together_but_never_overlaps_the_next_pair(case, tmp_path):
    spec, packets = case.build()
    spec = audited(spec, packets, tmp_path / "audit")

    def restart(arm, index, at, only=None):
        ref = getattr(spec, arm).scores[index]
        for (r, u), attempt in list(case.attempts.items()):
            if r == ref and only in (None, u):
                case.attempts[r, u] = replace(attempt, started_at=at)

    retried = next(u for r, u in case.attempts if r == spec.baseline.scores[1])
    restart("candidate", 1, "2026-10-20T12:02:00Z")
    restart("baseline", 1, "2026-10-20T12:03:30Z", only=retried)
    paired(spec, packets)
    restart("baseline", 1, "2026-10-20T12:04:30Z", only=retried)
    refuses(paired, spec, packets, match="planned interleaving")


@pytest.fixture
def decision_case(case):
    primary = paired(*case.build("mini", False)).population_reports[0]
    r = rule(extension_runs=3, bootstrap=c.BootstrapConfig(1000))
    precision = c.PrecisionComparison(0.8, 0.8, 0, c.Interval(-0.1, 0, -0.2, 0.1, 2))

    def build(differences=(0.0, 0.0), correct=29):
        bugs = tuple(
            replace(b, pr_url=str(i), difference=d, stable_loss=False)
            for i, (b, d) in enumerate(zip(primary.bugs, differences, strict=False))
        )
        report = replace(
            primary,
            population="gate-uncontaminated",
            bugs=bugs,
            difference=sum(differences) / len(bugs),
            interval=c.cluster_bootstrap(bugs, r.bootstrap),
            precision=precision,
        )
        nonmini = replace(
            report,
            population="gate-uncontaminated-non-mini",
            interval=c.Interval(0.01, 0.02, 0, 0.03, 2),
        )
        receipt = a.AuditReceipt(
            ("baseline", "candidate"),
            tuple(
                a.AuditRow(arm, i < limit)
                for arm, limit in (("baseline", 30), ("candidate", correct))
                for i in range(30)
            ),
        )
        return (report, nonmini), receipt, r

    return build


@pytest.mark.parametrize(
    "kind,guard,lower,upper,extended,verdict",
    [
        *(
            ("wave", "primary_recall", lower, upper, extended, verdict)
            for lower, upper, extended, verdict in (
                (0.06, 0.06, False, "pass"),
                (0.04, 0.04, False, "pass"),
                (-0.3, 0.1, False, "fail"),
                (-0.2, 0.2, False, "inconclusive"),
                (-0.2, 0.2, True, "fail"),
                (0, 0, True, "pass"),
            )
        ),
        *(
            ("ship", guard, lower, upper, False, verdict)
            for guard in ("primary_recall", "non-mini")
            for lower, upper, verdict in (
                (-0.1, -0.08, "fail"),
                (-0.08, 0, "inconclusive"),
                (-0.06, 0, "pass"),
            )
        ),
        *(
            (kind, guard, lower, upper, extended, verdict)
            for kind in ("wave", "ship")
            for guard in ("non-mini", "precision", "groundedness")
            for lower, upper, extended, verdict in (
                (-0.08, -0.08, False, "fail"),
                (0, 0, False, "pass"),
                (None, None, False, "inconclusive"),
                (None, None, True, "pass" if kind == "wave" else "inconclusive"),
            )
        ),
    ],
)
def test_promotion_transition_table(
    decision_case, kind, guard, lower, upper, extended, verdict
):
    primary_guard = guard == "primary_recall"
    reports, receipt, r = decision_case(
        (lower, upper) if primary_guard else (0.06, 0.06),
        correct=15
        if guard == "groundedness" and upper is not None and upper < 0
        else 29,
    )
    interval = c.Interval(lower, upper, lower, upper, 0 if lower is None else 2)
    primary, nonmini = reports
    if guard == "non-mini":
        nonmini = replace(nonmini, interval=interval)
    elif guard == "precision":
        precision = (
            replace(primary.precision, interval=interval) if lower is not None else None
        )
        primary = replace(primary, precision=precision)
    elif guard == "groundedness" and lower is None:
        receipt = replace(receipt, agreed=())
    elif kind == "ship" and primary_guard:
        primary = replace(primary, interval=interval)
    r = replace(
        r,
        kind=kind,
        extension_runs=3 if kind == "wave" else 0,
        nonmini_margin_pp=7 if kind == "ship" else 0,
    )
    result = c.evaluate_promotion((primary, nonmini), receipt, r, extended=extended)
    assert result.verdict == verdict
    assert result.extension_allowed == (
        kind == "wave" and not extended and verdict == "inconclusive"
    )
    if primary_guard and lower > 0:
        assert result.improvement == (lower >= 0.05)


@pytest.mark.parametrize("loss_count, verdict", [(1, "pass"), (2, "fail")])
def test_stable_losses_from_reduced_run_matrix_stop_the_wave(
    case, decision_case, loss_count, verdict
):
    epoch = case.epoch
    population = p.resolve_population(
        "gate-uncontaminated", epoch, "core", case.contamination
    )
    lost = population.bug_ids[:loss_count]
    gate_bugs = [b for b in epoch.bugs if b.pr_url in epoch.subsets["gate"]]
    baseline = [[True] * len(gate_bugs)] * 3
    candidate = [[b.bug_id not in lost for b in gate_bugs]] * 3
    matrix = matrix_for(case, baseline, candidate)
    reports, receipt, r = decision_case()
    primary = c.reduce_population(matrix, population, epoch, "core", r.bootstrap)
    primary = replace(primary, precision=reports[0].precision)
    result = c.evaluate_promotion(
        (primary, reports[1]), receipt, replace(r, margin_pp=100)
    )
    assert set(result.stable_loss_bug_ids) == set(lost)
    assert result.guards["stable_loss"] == result.verdict == verdict


def test_extension_uses_stage_alpha_for_the_primary_bound(decision_case):
    reports, receipt, r = decision_case(
        (-0.15, -0.12, -0.08, -0.06, -0.04, 0.02, 0.04, 0.09)
    )
    bounds = [
        c.cluster_bootstrap(reports[0].bugs, replace(r.bootstrap, alpha=alpha)).lower
        for alpha in (r.stage_alpha, r.bootstrap.alpha)
    ]
    margin = -100 * sum(bounds) / 2
    decisions = [
        c.evaluate_promotion(
            reports,
            receipt,
            replace(r, stage_alpha=alpha, margin_pp=margin),
            extended=True,
        )
        for alpha in (r.stage_alpha, r.bootstrap.alpha)
    ]
    assert [d.verdict for d in decisions] == ["fail", "pass"]


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


def test_precision_pools_stored_core_counts_and_filters_bug_ids(case, tmp_path):
    spec, packets = case.build()
    spec = audited(spec, packets, tmp_path / "audit")
    population = p.resolve_population(
        "gate-uncontaminated", case.epoch, "core", case.contamination
    )
    excluded = next(
        b
        for b in case.epoch.bugs
        if b.pr_url in population.pr_urls
        and b.bug_id not in population.bug_ids
        and b.category == "bug"
    )
    selected = next(
        b
        for b in case.epoch.bugs
        if b.pr_url == excluded.pr_url
        and b.bug_id in population.bug_ids
        and b.category == "bug"
    )
    for arm, factor in ((spec.baseline, 1), (spec.candidate, 2)):
        for run, ref in enumerate(arm.scores, 1):
            root = tmp_path / ref.run_id
            root.mkdir()
            raw = {
                u: {"deep-review": {"fp": 10000}}
                for u in case.epoch.subsets["gate"]
                if u not in population.pr_urls
            }
            raw[selected.pr_url] = {"deep-review": {"fp": run * factor}}
            write(root / "evaluations.json", raw)
            write(root / "complete.json", {"files": {}})
            write(root / "stage_trace_bindings.json", {})
            packet = packets[ref]
            packets[ref] = replace(
                packet,
                root=root,
                observations=tuple(
                    replace(b, outcome="matched")
                    if b.bug_id in (selected.bug_id, excluded.bug_id)
                    or b.pr_url not in population.pr_urls
                    else b
                    for b in packet.observations
                ),
            )
    report = next(
        r
        for r in paired(spec, packets).population_reports
        if r.population == "gate-uncontaminated" and r.profile == "core"
    ).precision
    assert (report.baseline_precision, report.candidate_precision) == pytest.approx(
        (1 / 3, 1 / 5)
    )
    assert (report.interval.lower, report.interval.upper) == pytest.approx((-2 / 15, 0))


def test_admission_diagnostics_preserve_issue_and_producer(case, tmp_path):
    from bench.runner import stages
    from bench.runner.epochs import file_hash
    from bench.tests.test_stages import emit_trace

    spec, packets = case.build()
    ref = spec.candidate.scores[0]
    packet = packets[ref]
    url = packet.candidates[0].pr_url
    population = p.resolve_population("gate", case.epoch, "core", case.contamination)
    bugs = [
        b
        for b in packet.observations
        if b.pr_url == url and b.bug_id in population.bug_ids
    ]
    trace, _, _ = emit_trace(tmp_path)
    versions = tuple({v.trace_id: v for v in trace.versions}.values())[:4]
    versions = tuple(
        replace(
            v,
            discovery_dimension=d,
            finding={**v.finding, "discovery_dimension": d},
        )
        for v, d in zip(
            versions, ("bug", "test_coverage", "test_coverage", "security"), strict=True
        )
    )
    versions = tuple(
        replace(
            v,
            finding={
                **v.finding,
                "origin_dimension": "test_coverage",
                "agent": "test-analyzer",
                "admission": {
                    **v.finding.get("admission", {}),
                    "status": "held_back",
                    "reasons": [
                        {"predicate": "complete_searches", "status": "held_back"}
                    ],
                },
                "disposition": "held_back",
                "disposition_reasons": ["held_back"],
            },
        )
        if index == 3
        else v
        for index, v in enumerate(versions)
    )
    tids = tuple(v.trace_id for v in versions)
    vids = tuple(v.version_id for v in versions)
    coalesced = stages.StageDecision(tids[0], "coalesced", "folded", None, (tids[1],))
    trace = replace(
        trace,
        versions=versions,
        snapshots=tuple(
            replace(
                s,
                coverage="complete",
                finding_version_ids=vids if i == 0 else vids[1:],
                decisions=(coalesced,) if s.stage in ("merge", "select") else (),
            )
            for i, s in enumerate(trace.snapshots)
        ),
    )
    root = tmp_path / "diagnostic-score"
    (root / "sources").mkdir(parents=True)
    write(root / "sources/trace.json", asdict(trace))
    empty_trace = replace(
        trace,
        versions=(),
        snapshots=tuple(
            replace(s, finding_version_ids=(), decisions=()) for s in trace.snapshots
        ),
    )
    write(root / "sources/empty-trace.json", asdict(empty_trace))
    write(
        root / "stage_trace_bindings.json",
        {
            u: dict(
                trace_path="sources/trace.json"
                if u == url
                else "sources/empty-trace.json",
                trace_sha256=file_hash(
                    root
                    / ("sources/trace.json" if u == url else "sources/empty-trace.json")
                ),
            )
            for u in packet.manifest.requested_pr_urls
        },
    )
    inline = replace(
        packet.candidates[0],
        text="Repeated issue µ",
        path="tests/check.any",
        source_trace_ids=tids[:2],
        discovery_dimensions=("bug", "test_coverage"),
        provenance_status="complete",
        issue_id="repeat",
        test_claim=True,
        test_site=True,
    )
    body = replace(
        inline,
        candidate_id="body",
        index=1,
        text="Body issue",
        surface="body",
        source_trace_ids=(tids[2],),
        discovery_dimensions=("test_coverage",),
        issue_id="body",
        path="src/engine",
        test_site=False,
    )
    canonical_bugs = tuple(
        replace(
            b,
            outcome="matched",
            matches=(o.Match(inline.candidate_id, 1, None, "direct"),),
        )
        if b.bug_id == bugs[0].bug_id
        else b
        for b in packet.observations
    )
    packet = replace(
        packet, root=root, candidates=(inline,), observations=canonical_bugs
    )
    packets[ref] = packet
    write(
        root / "evaluations.json",
        {u: {"deep-review": {"fp": 0}} for u in packet.manifest.requested_pr_urls},
    )
    write(root / "scores.json", {"candidates": [asdict(inline)]})

    def seal():
        write(
            root / "complete.json",
            {
                "files": {
                    f.relative_to(root).as_posix(): file_hash(f)
                    for f in root.rglob("*")
                    if f.is_file() and f.name != "complete.json"
                }
            },
        )

    seal()
    spec = audited(spec, packets, tmp_path / "audit")
    original = paired(spec, packets)
    assert original.promotion is not None
    for row in original.delivery_diagnostics["candidate"][: len(population.pr_urls)]:
        assert row.surfaces["body"]["values"] is None
        assert row.surfaces["all"]["reason"] == "surface sidecar absent"
    write(
        root / "surface-views.json",
        dict(all_surface=None, unavailable_reason="not requested"),
    )
    seal()
    assert (
        paired(spec, packets)
        .delivery_diagnostics["candidate"][0]
        .surfaces["all"]["values"]
        is None
    )
    view_bugs = tuple(
        replace(
            b,
            outcome="matched",
            matches=(o.Match(body.candidate_id, 1, None, "direct"),),
        )
        if b.bug_id == bugs[1].bug_id
        else b
        for b in canonical_bugs
    )
    # Repeated delivery of the same explicit issue earns one all-surface credit.
    candidates = o.surface_candidates((inline, replace(inline, surface="body"), body))
    view = o.SurfaceScoreView(
        packet.manifest.evaluator.evaluator_id, candidates, view_bugs, (), (), {}, {}
    )
    packets[ref] = replace(packet, surface_view=view)
    (root / "surface-all").mkdir()
    write(root / "surface-all/dedup_groups.json", {url: {"deep-review": []}})
    write(root / "surface-all/view.json", asdict(view))
    write(
        root / "surface-views.json",
        dict(all_surface="surface-all/view.json", unavailable_reason=None),
    )
    seal()
    result = paired(spec, packets)
    assert result.promotion == original.promotion
    assert result.population_reports == original.population_reports
    assert result.input_score_refs == original.input_score_refs
    assert len(result.delivery_diagnostics["candidate"]) == 3 * len(population.pr_urls)
    rows = {
        r.pr_url: r
        for r in result.delivery_diagnostics["candidate"]
        if r.score_ref == ref
    }
    assert set(rows) == set(packet.manifest.selected_attempts)
    selected = rows[url]
    held_record = next(
        r
        for r in selected.admission["values"]["records"]
        if r["disposition"] == "held_back"
    )
    assert held_record["original_producer"] == "test-analyzer"
    assert held_record["origin_dimension"] == "test_coverage"
    assert held_record["reasons"] == ["held_back"]
    assert held_record["admission"]["reasons"] == [
        {"predicate": "complete_searches", "status": "held_back"}
    ]
    assert selected.record_coalescence["values"] == dict(
        coalesced_source_occurrences=1,
        eligible_source_occurrences=4,
        groups=1,
        rate=0.25,
    )
    metrics = selected.surfaces["all"]["values"]
    assert (
        metrics["issues"],
        metrics["source_records"],
        metrics["records_per_issue"],
    ) == (2, 3, 1.5)
    assert (
        metrics["test_path_issues"],
        metrics["mixed_producer_issues"],
        metrics["producer_leakage"],
    ) == (1, 1, 1)
    assert metrics["canonical_test_claim_issues"] == 2
    assert metrics["original_test_producer_issues"] == 2
    assert metrics["canonical_test_leakage"] == 1
    assert metrics["producer_dimensions"]["test_coverage"] == 2
    assert metrics["producer_dimensions"]["bug"] == 1
    assert metrics["unknown_producer_issues"] == 0
    assert metrics["byte_budget_text_folds"] is None
    assert metrics["utf8_bytes"] == metrics["characters"] + 1
    assert metrics["observed_sole_match_bug_ids"]["body"] == [bugs[1].bug_id]
    assert metrics["observed_sole_match_bug_ids"]["test_coverage"] == [bugs[1].bug_id]
    assert metrics["observed_sole_match_bug_ids"]["bug"] == []
    for surface, hits in (("inline", 1), ("body", 1), ("all", 2)):
        assert selected.surfaces[surface]["available"]
        assert selected.surfaces[surface]["values"]["recall"] == hits / len(bugs)
        assert selected.surfaces[surface]["values"]["precision"] == 1
    assert selected.surfaces["all"]["evidence_sha256"][
        "surface-all/view.json"
    ] == file_hash(root / "surface-all/view.json")
    assert selected.record_coalescence["evidence_sha256"][
        "sources/trace.json"
    ] == file_hash(root / "sources/trace.json")
    # A comparison may read runs linked in from another checkout.
    link = tmp_path / "linked-packet"
    try:
        link.symlink_to(root, target_is_directory=True)
    except OSError:
        pass
    else:
        linked = paired(spec, {**packets, ref: replace(packets[ref], root=link)})
        assert linked.delivery_diagnostics == result.delivery_diagnostics
    empty = next(row for u, row in rows.items() if u != url)
    assert empty.surfaces["all"]["values"]["issues"] == 0
    assert empty.surfaces["all"]["values"]["precision"] is None
    assert empty.surfaces["all"]["values"]["recall"] == 0
    unknown = next(
        r
        for r in result.delivery_diagnostics["baseline"]
        if r.surfaces["inline"]["values"]["delivery_records"]
    )
    assert not unknown.record_coalescence["available"]
    assert unknown.record_coalescence["values"] is None
    assert unknown.surfaces["inline"]["values"]["issues"] is None
    assert unknown.surfaces["inline"]["values"]["producer_leakage"] is None
    assert unknown.surfaces["inline"]["values"]["producer_dimensions"]["bug"] is None
    body_peer = replace(
        body,
        candidate_id="body-peer",
        index=2,
        source_trace_ids=(tids[1],),
        issue_id="body-peer",
    )
    multi_match_bugs = tuple(
        replace(
            b,
            matches=tuple(
                o.Match(cid, 1, None, "direct")
                for cid in (
                    (inline.candidate_id, body.candidate_id)
                    if b.bug_id == bugs[0].bug_id
                    else (body.candidate_id, body_peer.candidate_id)
                )
            ),
        )
        if b.bug_id in (bugs[0].bug_id, bugs[1].bug_id)
        else b
        for b in view_bugs
    )
    view = replace(
        view,
        candidates=(
            replace(inline, source_trace_ids=(tids[0],), discovery_dimensions=("bug",)),
            body,
            body_peer,
        ),
        observations=multi_match_bugs,
    )
    packets[ref] = replace(packet, surface_view=view)
    write(root / "surface-all/view.json", asdict(view))
    seal()
    multi_match_row = next(
        row
        for row in paired(spec, packets).delivery_diagnostics["candidate"]
        if row.score_ref == ref and row.pr_url == url
    )
    sole = multi_match_row.surfaces["all"]["values"]["observed_sole_match_bug_ids"]
    assert bugs[0].bug_id not in sole["bug"]
    assert bugs[0].bug_id not in sole["test_coverage"]
    assert bugs[1].bug_id in sole["test_coverage"]
    for receipt in (None, dict(all_surface=None, unavailable_reason="not requested")):
        if receipt is None:
            (root / "surface-views.json").unlink()
        else:
            write(root / "surface-views.json", receipt)
        seal()
        unavailable = paired(spec, packets)
        assert unavailable.promotion == original.promotion
        assert all(
            row.surfaces[surface]["values"] is None
            for row in unavailable.delivery_diagnostics["candidate"]
            if row.score_ref == ref
            for surface in ("body", "all")
        )


def bind_run(case, ref, block, path, index):
    fp = v.RunFingerprint(block.expected, block.arms[case.runs[ref]["arm"]])
    case.runs[ref].update(
        run_index=index,
        window_id=block.window_id,
        window_sha256=v.file_hash(path),
        measurement_window=str(path),
        child_auth=block.expected.auth_mode,
        provenance=fp.wire(),
    )
    for url in block.pr_urls:
        attempt = case.attempts[ref, url]
        case.attempts[ref, url] = replace(
            attempt,
            validity=replace(attempt.validity, window_id=block.window_id, observed=fp),
        )


def split_blocks(case, spec, groups):
    original = v.load_window(case.window_path)
    paths = []
    for number, group in enumerate(groups):
        path = case.window_path.with_name(f"block-{number}.json")
        indices = tuple(range(len(group)))
        block = replace(
            original,
            schema_version=2,
            window_id=str(number),
            expected=replace(
                original.expected, auth_mode="subscription" if number else "api"
            ),
            planned_run_indices=indices,
            planned_order=tuple((a, i) for i in indices for a in original.arms),
        )
        write(path, block.wire())
        paths.append(str(path))
        for arm in (spec.baseline, spec.candidate):
            for local, ordinal in enumerate(group):
                if ordinal < len(arm.scores):
                    bind_run(case, arm.scores[ordinal], block, path, local)
    return replace(spec, window_path=None, window_paths=tuple(paths))


@pytest.mark.parametrize("operational", [False, True])
def test_blocks_pool_independent_runs_before_the_rule(case, tmp_path, operational):
    spec, packets = case.build("gate" if operational else "mini", operational)
    for arm, hits in ((spec.baseline, (1, 0, 0)), (spec.candidate, (0, 1, 1))):
        for ref, hit in zip(arm.scores, hits, strict=True):
            packets[ref] = replace(
                packets[ref],
                observations=tuple(
                    replace(b, outcome="matched" if hit else "missed")
                    for b in packets[ref].observations
                ),
            )
    if operational:
        spec = audited(spec, packets, tmp_path / "audit")
    expected = paired(spec, packets)
    report = paired(split_blocks(case, spec, ((0,), (1, 2), (3,))), packets)
    assert report.population_reports == expected.population_reports
    assert [b.run_counts for b in report.blocks] == [
        dict(baseline=n, candidate=n) for n in (1, 2, 0)
    ]
    assert [r.pooled_index for b in report.blocks for r in b.arms["candidate"]] == list(
        range(3)
    )
    assert [r.run_index for b in report.blocks for r in b.arms["candidate"]] == [
        0,
        0,
        1,
    ]
    assert report.blocks[0].lane_id != report.blocks[1].lane_id

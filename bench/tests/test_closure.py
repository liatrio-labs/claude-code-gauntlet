from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from bench import run
from bench.runner import audit, compare, looks
from bench.runner import observations as o
from bench.runner import validity as v
from bench.runner.epochs import digest, read_json
from bench.tests.conftest import refuses, write
from bench.tests.test_audit import resolve_audit
from bench.tests.test_compare import case as case
from bench.tests.test_compare import paired, rule
from bench.tests.test_harness_boundaries import harness as harness
from bench.tests.test_validity import fingerprint as fingerprint
from bench.tests.test_validity import window as window

LOAD_ARM = compare.load_arm


def test_register_score_and_look_leave_treatment_clean(git_repo, monkeypatch):
    root, git = git_repo
    plugin = root / "agents/reviewer.md"
    plugin.parent.mkdir()
    plugin.write_text("review", encoding="utf-8")
    ledger = root / "bench/golden/looks.jsonl"
    ledger.parent.mkdir(parents=True)
    ledger.touch()
    scores = root / "bench/experiments.jsonl"
    scores.touch()
    git("add", ".")
    git("commit", "-qm", "fixture")
    monkeypatch.setattr(looks, "LEDGER", ledger)
    identity = v.treatment_sha(root)
    looks.register("wave", {}, identity)
    arm = compare.Arm("a", (), identity, identity)
    report = compare.ComparisonReport(
        "c",
        (),
        (),
        dict(rule_sha256=digest({}), look=1),
        {},
        compare.PromotionDecision("fail", {}, (), True, False),
    )
    looks.record(
        "wave",
        SimpleNamespace(baseline=arm, candidate=arm),
        report,
    )
    scores.write_text("{}\n", encoding="utf-8")
    v.require_clean_treatment(root)
    assert v.treatment_sha(root) == identity
    assert looks.next_look("wave", digest({})) == 2
    git("add", "bench")
    git("commit", "-qm", "evidence")
    assert v.treatment_sha(root) == identity


def test_holdout_failed_preflight_then_run_resume_and_retry(harness, monkeypatch):
    ledger = harness.workspace / "looks.jsonl"
    ledger.touch()
    monkeypatch.setattr(looks, "LEDGER", ledger)
    receipt = harness.workspace / "unseal.json"
    looks.unseal(v.treatment_sha(), receipt)
    monkeypatch.setattr(v, "require_clean_treatment", lambda: None)
    harness.control["lookup"] = "empty"
    argv = ["--tier", "holdout", "--unseal-holdout", str(receipt)]
    assert run.main(argv) == 2
    assert not any(r["kind"] == "holdout-use" for r in looks.entries())
    harness.control["lookup"] = "ok"
    assert run.main(argv) == 0
    run_id = next(run.RUNS_ROOT.iterdir()).name
    for mode in ("--resume", "--retry-failed"):
        assert run.main([mode, run_id]) == 0
    assert sum(r["kind"] == "holdout-use" for r in looks.entries()) == 1
    assert run.main(argv) == 2


def test_ship_full_count_refused_before_packet_load_or_review(case, monkeypatch):
    spec, packets = case.build(count=1, promotion_rule=rule("ship"))
    spec = replace(spec, purpose="final")
    declaration = read_json(case.window_path)
    declaration["planned_run_indices"] = [0]
    write(case.window_path, declaration)
    refuses(
        compare.compare_arms,
        spec,
        packet_loader=lambda ref: pytest.fail("loaded"),
        match="run count",
    )
    monkeypatch.setattr(v, "require_clean_treatment", lambda: None)
    monkeypatch.setattr(v, "run_provenance", lambda **kw: {})
    monkeypatch.setattr(
        run, "check_prereqs", lambda **kw: pytest.fail("review preflight")
    )
    assert (
        run.main(
            [
                "--tier",
                "subset",
                "--measurement-window",
                str(case.window_path),
                "--rule",
                str(case.rule_path),
                "--arm",
                "baseline",
                "--run-index",
                "0",
            ]
        )
        == 2
    )
    write(case.window_path, {**declaration, "planned_run_indices": [0, 1, 2]})
    refuses(paired, spec, packets, match="run count")


def test_audit_census_and_receipt_exclude_contaminated_prs(case, tmp_path):
    spec, packets = case.build()
    excluded = next(
        r["pr_url"]
        for r in case.contamination["entries"]
        if r["scope"] == "pr" and r["pr_url"] in case.epoch.subsets["gate"]
    )
    for ref, packet in packets.items():
        packets[ref] = replace(
            packet,
            candidates=(
                *packet.candidates,
                replace(packet.candidates[0], candidate_id="excluded", pr_url=excluded),
            ),
        )
    frozen, paths, receipt = resolve_audit(spec, packets, tmp_path / "audit")
    assert all(r["finding"]["pr_url"] != excluded for r in frozen["rows"])
    assert (
        paired(replace(spec, audit_path=str(receipt)), packets).promotion.guards[
            "groundedness"
        ]
        == "pass"
    )
    population = compare.populations.resolve_population(
        "gate", case.epoch, "core", case.contamination
    )
    refuses(audit.load_receipt, receipt, population=population, match="population")


def test_freeze_precedes_every_candidate(case):
    spec, packets = case.build(promotion_rule=rule("ship"))
    case.contamination["frozen_at"] = "2026-10-20T12:02:00Z"
    write(Path(spec.contamination_path), case.contamination)
    for ref in spec.candidate.scores:
        case.runs[ref]["contamination_sha256"] = digest(case.contamination)
    refuses(paired, replace(spec, purpose="final"), packets, match="freeze")


def test_golden_correction_keeps_review_binding(
    case, score_case, monkeypatch, tmp_path
):
    from bench.runner import epochs, score

    spec, packets = case.build(promotion_rule=rule("ship"))
    spec = replace(spec, purpose="final")
    ctx = epochs.epoch_context(case.epoch)
    ctx["scorer_input"][score_case.url]["golden_comments"][0]["category"] = "style"
    ctx["spec"]["golden_sha256"] = digest([ctx["scorer_input"], ctx["project_by_pr"]])
    epoch = epochs.from_context(ctx)
    monkeypatch.setattr(compare, "load_epoch", lambda *args: epoch)
    monkeypatch.setattr(score, "load_epoch", lambda *args, **kwargs: epoch)
    refuses(paired, spec, packets, match="contamination epoch")
    binding = compare.GoldenCorrection(digest(case.contamination), epoch.golden_sha256)
    spec = replace(spec, golden_correction=binding)
    refuses(paired, spec, packets, match="scoring context")
    labels = {b.bug_id: b.category for b in epoch.bugs}
    for arm in (spec.baseline, spec.candidate):
        result = score_case.execute()
        fresh = o.load_packet(Path(result["ledger_row"]["packet_path"])).manifest
        for ref in arm.scores:
            packet = packets[ref]
            manifest = replace(packet.manifest, epoch_hashes=fresh.epoch_hashes)
            rows = tuple(
                replace(b, category=labels[b.bug_id]) for b in packet.observations
            )
            packets[ref] = replace(packet, manifest=manifest, observations=rows)
    monkeypatch.setattr(audit, "load_epoch", lambda *args: epoch)
    audit_manifest = tmp_path / "audit-contamination.json"
    write(audit_manifest, {**case.contamination, "golden_sha256": epoch.golden_sha256})
    _, _, receipt = resolve_audit(
        replace(spec, contamination_path=str(audit_manifest)),
        packets,
        tmp_path / "audit",
    )
    spec = replace(spec, audit_path=str(receipt))
    report = paired(spec, packets)
    assert report.identity["contamination_sha256"] == digest(case.contamination)
    assert report.identity["golden_sha256"] == epoch.golden_sha256
    changed = {**case.contamination, "golden_sha256": epoch.golden_sha256}
    write(Path(spec.contamination_path), changed)
    refuses(paired, spec, packets, match="correction binding")


def test_historical_comparison_retains_manifest_and_delivered_dimensions(
    case, monkeypatch
):
    spec, packets = case.build("mini", False)
    for ref, packet in packets.items():
        candidate = replace(
            packet.candidates[0],
            provenance_status="complete",
            discovery_dimensions=("bug",),
        )
        bugs = tuple(
            replace(
                b,
                outcome="matched",
                matches=(o.Match(candidate.candidate_id, 1, None, "direct"),),
            )
            if b.pr_url == candidate.pr_url
            else b
            for b in packet.observations
        )
        packets[ref] = replace(packet, candidates=(candidate,), observations=bugs)
        assert read_json(packet.root / "run-provenance.json").get("provenance") is None
    monkeypatch.setattr(compare, "load_arm", LOAD_ARM)
    monkeypatch.setattr(
        compare, "_packet_run", lambda p: read_json(p.root / "run-provenance.json")
    )
    report = paired(spec, packets).population_reports[0]
    assert report.dimensions["candidate"]["bug"]["delivered"] > 0
    assert report.dimensions["candidate"]["bug"]["discovery"] is None
    for funnel in report.funnels.values():
        assert funnel["denominator"] == report.denominator * 3
        assert funnel["unavailable_count"] == funnel["denominator"]


@pytest.mark.parametrize("name", ["dev-slice", "dev-pool"])
def test_named_run_populations_require_explicit_dispositions(pop_case, tmp_path, name):
    epoch, manifest = pop_case
    path = tmp_path / "contamination.json"
    write(path, manifest)
    args = run.parse_args(["--prs", name, "--contamination", str(path)])
    assert (
        tuple(args.prs)
        == compare.populations.resolve_population(name, epoch, "core", manifest).pr_urls
    )
    with pytest.raises(SystemExit):
        run.parse_args(["--prs", name])

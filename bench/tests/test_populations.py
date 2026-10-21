"""Frozen population and exclusion boundaries."""

import copy
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace

import pytest

from bench.runner import leaks, validity
from bench.runner import populations as p
from bench.runner.epochs import canonical, digest, read_json


def test_exclusions_and_named_membership_are_derived_once(population_case):
    epoch, contamination = population_case
    rows = p.contamination_entries(contamination, epoch)
    excluded = {
        e["pr_url"].split("/")[-3] + "#" + e["pr_url"].split("/")[-1]
        for e in rows
        if e["scope"] == "pr"
    }
    assert excluded == {
        "discourse-graphite#4",
        "sentry-greptile#1",
        "sentry-greptile#2",
        "keycloak#32918",
    }

    def resolve(name):
        return p.resolve_population(name, epoch, "core", contamination)

    primary = resolve("gate-uncontaminated")
    assert primary.denominator == 50 and len(primary.pr_urls) == 13
    assert resolve("gate-uncontaminated-non-mini").denominator == 21
    assert resolve("gate").denominator == 66
    assert resolve("gate-contaminated").denominator == 16
    pool, selected = resolve("dev-pool"), resolve("dev-slice")
    assert len(pool.pr_urls) == 25 and len(selected.pr_urls) == 6
    from bench.runner.epochs import included_bugs

    expected = sorted(
        pool.pr_urls,
        key=lambda u: (
            -sum(
                b.pr_url == u
                for b in included_bugs(epoch, "core", frozenset(pool.pr_urls))
            ),
            u,
        ),
    )[:6]
    assert set(selected.pr_urls) == set(expected)
    with pytest.raises(ValueError, match="sealed"):
        resolve("holdout")
    assert (
        len(
            p.resolve_population(
                "holdout", epoch, "core", contamination, unseal="#101-close"
            ).pr_urls
        )
        == 10
    )


@pytest.mark.parametrize("change", ["epoch", "unfrozen", "scope", "bug", "evidence"])
def test_invalid_dispositions_are_refused(population_case, change):
    epoch, value = population_case
    value = copy.deepcopy(value)
    if change == "epoch":
        value["golden_sha256"] = "wrong"
    if change == "unfrozen":
        value["status"] = "unfrozen"
    if change == "scope":
        value["entries"][0]["match_kind"] = "textbook_class"
    if change == "bug":
        value["entries"][-1]["bug_ids"] = [epoch.bugs[0].bug_id]
    if change == "evidence":
        value["entries"][0]["evidence_refs"] = []
    with pytest.raises(ValueError):
        p.resolve_population("dev-slice", epoch, "core", value)


def test_importer_rejects_epoch_ordinal_and_duplicate_errors(population_case, tmp_path):
    epoch, _ = population_case
    path = Path(__file__).parent / "fixtures/leak-manifest-v2.json"
    for change in ("epoch", "ordinal", "duplicate"):
        source = read_json(path)
        if change == "epoch":
            source["golden_epoch"] = "wrong"
        if change == "ordinal":
            source["excluded_comments"][0] = "cal_dot_com:cal.com:10600:100"
        if change == "duplicate":
            source["excluded_prs"].append(source["excluded_prs"][0])
        bad = tmp_path / (change + ".json")
        bad.write_bytes(canonical(source))
        with pytest.raises(ValueError):
            p.import_dispositions(bad, epoch)


def test_freeze_binds_explicit_baseline_and_exact_passing_scan(
    population_case, tmp_path, monkeypatch
):
    epoch, value = population_case
    value.update(status="unfrozen", frozen_at=None)
    report = leaks.LeakReport(
        2, epoch.epoch_id, epoch.golden_sha256, (), "surfaces", (), (), (), (), True
    )
    path = tmp_path / "scan.json"
    path.write_bytes(canonical(asdict(report)))
    monkeypatch.setattr(validity, "require_clean_treatment", lambda root: None)
    monkeypatch.setattr(validity, "agent_read_hash", lambda root: "scanned-tree")
    calls = []

    def git(args, **kwargs):
        calls.append(args)
        return SimpleNamespace(stdout=b"identical plugin tree")

    monkeypatch.setattr(p.subprocess, "run", git)
    monkeypatch.setattr(leaks, "scan_repository", lambda root: report)
    kwargs = dict(
        baseline_treatment_sha="a" * 40,
        leak_report=path,
        frozen_at="fixture",
        out=tmp_path / "frozen.json",
    )
    frozen = p.freeze_contamination(value, epoch, **kwargs)
    assert frozen["baseline_treatment_sha"] == "a" * 40
    assert any("a" * 40 in c for c in calls)
    assert frozen["leak_report_sha256"] == digest(asdict(report))
    monkeypatch.setattr(
        p.subprocess, "run", lambda args, **kw: SimpleNamespace(stdout=args[3].encode())
    )
    with pytest.raises(ValueError, match="tree"):
        p.freeze_contamination(
            value, epoch, **{**kwargs, "out": tmp_path / "wrong-tree.json"}
        )
    monkeypatch.setattr(p.subprocess, "run", git)
    path.write_bytes(canonical({**asdict(report), "surfaces_sha256": "changed"}))
    with pytest.raises(ValueError, match="scan"):
        p.freeze_contamination(value, epoch, **{**kwargs, "out": tmp_path / "bad.json"})


def test_run_dispatch_refuses_holdout_before_prerequisites(
    population_case, monkeypatch
):
    from bench import run

    epoch, _ = population_case
    monkeypatch.setattr(
        run, "check_prereqs", lambda **kw: pytest.fail("prerequisites reached")
    )
    for args in (
        ["--tier", "holdout"],
        ["--tier", "full"],
        ["--prs", epoch.subsets["holdout"][0]],
    ):
        assert run.main(args) == 2

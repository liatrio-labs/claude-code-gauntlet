from dataclasses import asdict, replace
from pathlib import Path

import pytest

from bench.runner import leaks, looks, validity
from bench.runner import populations as p
from bench.runner.epochs import digest, read_json
from bench.tests.conftest import refuses, write


def test_checked_in_rule_and_contamination_are_frozen_and_bound():
    from bench.runner.compare import PromotionRule
    from bench.runner.decode import decode
    from bench.runner.epochs import DEFAULT_ROOT, load_epoch

    rule = decode(PromotionRule, read_json(DEFAULT_ROOT / "promotion-rule.json"))
    manifest = read_json(DEFAULT_ROOT / "contamination.json")
    epoch = load_epoch()
    assert rule.frozen
    sha = manifest["baseline_treatment_sha"]
    p.check_baseline_binding(manifest, sha, sha)
    p.resolve_population("gate-uncontaminated", epoch, rule.profile, manifest)


def test_exclusions_and_named_membership_are_derived_once(pop_case):
    e, manifest = pop_case
    rows = p.contamination_entries(manifest, e)
    excluded = {
        r["pr_url"].split("/")[-3] + "#" + r["pr_url"].split("/")[-1]
        for r in rows
        if r["scope"] == "pr"
    }
    assert excluded == {
        "discourse-graphite#4",
        "sentry-greptile#1",
        "sentry-greptile#2",
        "keycloak#32918",
    }

    def resolve(name):
        return p.resolve_population(name, e, "core", manifest)

    assert resolve("gate").denominator == 66
    assert resolve("gate-uncontaminated").denominator == 50
    assert resolve("gate-uncontaminated-non-mini").denominator == 21
    assert resolve("gate-contaminated").denominator == 16
    assert len(resolve("gate-uncontaminated").pr_urls) == 13
    pool, selected = resolve("dev-pool"), resolve("dev-slice")
    assert (len(pool.pr_urls), len(selected.pr_urls)) == (23, 6)
    assert not set(pool.pr_urls) & {r["pr_url"] for r in rows if r["scope"] == "pr"}
    counts = {
        u: sum(b.pr_url == u and b.bug_id in pool.bug_ids for b in e.bugs)
        for u in pool.pr_urls
    }
    assert set(selected.pr_urls) == set(
        sorted(counts, key=lambda u: (-counts[u], u))[:6]
    )
    assert set(resolve("mini").pr_urls) == set(e.subsets["mini"])
    manifest["status"] = "unfrozen"
    assert p.resolve_population("dev-slice", e, "core", manifest) == selected
    refuses(p.resolve_population, "gate", e, "core", manifest, match="unfrozen")
    refuses(p.resolve_population, "holdout", e, "core", manifest, match="sealed")


@pytest.mark.parametrize("change", ["epoch", "scope", "bug", "evidence"])
def test_invalid_dispositions_are_refused(pop_case, change):
    e, m = pop_case
    if change == "epoch":
        m["golden_sha256"] = "wrong"
    if change == "scope":
        m["entries"][0]["match_kind"] = "textbook_class"
    if change == "bug":
        m["entries"][-1]["bug_ids"] = [e.bugs[0].bug_id]
    if change == "evidence":
        m["entries"][0]["evidence_refs"] = []
    refuses(p.resolve_population, "dev-slice", e, "core", m)


@pytest.mark.parametrize("change", ["epoch", "ordinal", "duplicate"])
def test_importer_rejects_epoch_ordinal_and_duplicate_errors(
    pop_case, tmp_path, change
):
    e, _ = pop_case
    source = read_json(Path(__file__).parent / "fixtures/leak-manifest-v2.json")
    if change == "epoch":
        source["golden_epoch"] = "wrong"
    if change == "ordinal":
        source["excluded_comments"][0] = "cal_dot_com:cal.com:10600:100"
    if change == "duplicate":
        source["excluded_prs"].append(source["excluded_prs"][0])
    path = tmp_path / "bad.json"
    write(path, source)
    refuses(p.import_dispositions, path, e)


def test_freeze_binds_explicit_baseline_and_exact_passing_scan(
    pop_case, tmp_path, monkeypatch
):
    e, m = pop_case
    m.update(status="unfrozen", frozen_at=None)
    report = leaks.LeakReport(
        2, e.epoch_id, e.golden_sha256, (), "surfaces", (), (), (), True
    )
    monkeypatch.setattr(validity, "require_clean_treatment", lambda root: None)
    monkeypatch.setattr(validity, "agent_read_hash", lambda root: "scanned-tree")
    monkeypatch.setattr(leaks, "scan_repository", lambda root: report)
    kw = dict(
        baseline_treatment_sha="scanned-tree",
        frozen_at="2026-10-19T00:00:00Z",
        out=tmp_path / "frozen.json",
    )
    frozen = p.freeze_contamination(m, e, **kw)
    assert frozen["baseline_treatment_sha"] == "scanned-tree"
    assert frozen["leak_report_sha256"] == digest(asdict(report))
    refuses(
        p.freeze_contamination,
        m,
        e,
        **{**kw, "baseline_treatment_sha": "other"},
        match="tree",
    )
    report = replace(report, passed=False)
    refuses(p.freeze_contamination, m, e, **kw, match="scan")


def test_run_dispatch_refuses_holdout_before_prerequisites(pop_case, monkeypatch):
    from bench import run

    e, _ = pop_case
    monkeypatch.setattr(
        run, "check_prereqs", lambda **kw: pytest.fail("prerequisites reached")
    )
    for args in (
        ["--tier", "holdout"],
        ["--tier", "full"],
        ["--prs", e.subsets["holdout"][0]],
    ):
        assert run.main(args) == 2


def test_holdout_receipt_is_once_and_final_sha_bound(pop_case, tmp_path, monkeypatch):
    e, m = pop_case
    ledger = tmp_path / "looks.jsonl"
    ledger.write_bytes(b"")
    monkeypatch.setattr(looks, "LEDGER", ledger)
    receipt = tmp_path / "unseal.json"
    sha = "a" * 40
    looks.unseal(sha, receipt)
    refuses(looks.unseal, sha, tmp_path / "second.json")
    kw = dict(unseal=receipt, treatment_sha=sha)
    assert len(p.resolve_population("holdout", e, "core", m, **kw).pr_urls) == 10
    refuses(p.check_holdout, e.subsets["holdout"], e, receipt, "b" * 40)
    for use in ["run", "audit", "compare"]:
        p.check_holdout(e.subsets["holdout"], e, receipt, sha, use=use, run_id="one")
        refuses(
            p.check_holdout,
            e.subsets["holdout"],
            e,
            receipt,
            sha,
            use=use,
            run_id="two",
            match="second use",
        )


@pytest.mark.parametrize("entry", ["function", "command"])
def test_unseal_requires_receipt_output_before_spending(tmp_path, monkeypatch, entry):
    ledger = tmp_path / "looks.jsonl"
    ledger.write_bytes(b"")
    monkeypatch.setattr(looks, "LEDGER", ledger)
    if entry == "command":
        monkeypatch.setattr("sys.argv", ["looks", "--approved-final-sha", "a" * 40])
        with pytest.raises(SystemExit) as exc:
            looks.main()
        assert exc.value.code == 2
    else:
        refuses(looks.unseal, "a" * 40, None, match="requires --out")
    assert ledger.read_bytes() == b""


@pytest.mark.parametrize("failure", ["existing", "io", None])
def test_unseal_persists_receipt_before_ledger(tmp_path, monkeypatch, failure):
    ledger, receipt = tmp_path / "looks.jsonl", tmp_path / "receipt.json"
    ledger.write_bytes(b"")
    monkeypatch.setattr(looks, "LEDGER", ledger)
    if failure == "existing":
        receipt.write_bytes(b"keep")
        with pytest.raises(FileExistsError):
            looks.unseal("a" * 40, receipt)
        assert receipt.read_bytes() == b"keep"
    elif failure == "io":
        with monkeypatch.context() as patch:

            def cannot_write(*args):
                raise OSError("disk unavailable")

            patch.setattr(looks, "exclusive_json", cannot_write)
            with pytest.raises(OSError, match="disk unavailable"):
                looks.unseal("a" * 40, receipt)
    assert ledger.read_bytes() == b""
    receipt = tmp_path / "retry.json"
    append = looks.append

    def require_receipt(row, check):
        assert read_json(receipt) == row
        return append(row, check)

    monkeypatch.setattr(looks, "append", require_receipt)
    looks.unseal("a" * 40, receipt)
    assert looks.entries() == [read_json(receipt)]

"""Population membership is decided at the ingestion and resolution boundaries."""

import json
import shutil
from dataclasses import replace
from pathlib import Path

import pytest

from bench.runner import populations as p
from bench.runner.epochs import DEFAULT_ROOT, digest, load_epoch, read_json

SOURCE = Path(__file__).parent / "fixtures/leak-manifest-v2.json"


def test_v2_ordinal_join_and_exact_population_membership(population_case):
    epoch, subsets, contamination, manifest, receipt = population_case
    prepared = read_json(receipt)
    assert [
        (r["source_key"], r["one_based_ordinal"] - 1)
        for r in prepared["excluded_comments"]
        if r["source_key"]
        in (
            "cal_dot_com:cal.com:10600:1",
            "cal_dot_com:cal.com:10600:3",
            "keycloak:keycloak:37429:4",
            "discourse:discourse-graphite:10:3",
        )
    ] == [
        ("cal_dot_com:cal.com:10600:1", 0),
        ("cal_dot_com:cal.com:10600:3", 2),
        ("discourse:discourse-graphite:10:3", 2),
        ("keycloak:keycloak:37429:4", 3),
    ]
    resolved = {
        name: p.resolve_population(
            name, epoch, "core", subsets, contamination, manifest
        )
        for name in (
            "gate",
            "uncontaminated",
            "non-mini",
            "contaminated",
            "dev-pool",
            "dev-slice",
        )
    }
    assert resolved["gate"].denominator == 66
    assert resolved["uncontaminated"].denominator == 50
    assert len(resolved["uncontaminated"].nonempty_pr_clusters) == 13
    assert len(resolved["uncontaminated"].pr_urls) == 15
    assert resolved["non-mini"].denominator == 21
    gate, clean, dirty = (
        set(resolved[n].bug_ids) for n in ("gate", "uncontaminated", "contaminated")
    )
    excluded_prs = {e["pr_url"] for e in contamination["entries"] if e["scope"] == "pr"}
    excluded_bugs = {
        bid
        for e in contamination["entries"]
        if e["scope"] == "bug"
        for bid in e["bug_ids"]
    }
    assert clean == {
        b.bug_id
        for b in epoch.bugs
        if b.bug_id in gate
        and b.pr_url not in excluded_prs
        and b.bug_id not in excluded_bugs
    }
    assert gate == clean | dirty and not clean & dirty
    assert set(resolved["non-mini"].bug_ids) == {
        b.bug_id
        for b in epoch.bugs
        if b.bug_id in clean and b.pr_url not in subsets["mini"]
    }
    assert set(resolved["dev-pool"].pr_urls) == {b.pr_url for b in epoch.bugs} - set(
        subsets["gate"]
    ) - set(subsets["holdout"])
    assert set(subsets["smoke"]) <= set(resolved["dev-pool"].pr_urls)
    counts = {
        url: sum(
            b.pr_url == url and b.category not in ("style", "speculative")
            for b in epoch.bugs
        )
        for url in resolved["dev-pool"].pr_urls
    }
    assert (
        manifest["dev_slice_pr_urls"]
        == sorted(counts, key=lambda u: (-counts[u], u))[:6]
    )
    assert not any(b.category == "style" for b in epoch.bugs if b.bug_id in gate)


@pytest.mark.parametrize("change", ["order", "text"])
def test_ingest_refuses_mutated_local_golden_text_not_neighbor(population_case, change):
    epoch, _, _, _, receipt = population_case
    record = read_json(receipt)["excluded_comments"][0]
    bug = next(
        b
        for b in epoch.bugs
        if b.pr_url == record["pr_url"] and b.ordinal == record["one_based_ordinal"] - 1
    )
    changed = replace(
        bug,
        comment="changed public fixture text"
        if change == "text"
        else next(
            b.comment
            for b in epoch.bugs
            if b.pr_url == bug.pr_url and b.ordinal != bug.ordinal
        ),
    )
    altered = replace(epoch, bugs=tuple(changed if b == bug else b for b in epoch.bugs))
    with pytest.raises(ValueError, match="text-hash"):
        p.ingest_leak_manifest_v2(receipt, altered)


@pytest.mark.parametrize(
    "key",
    [
        "cal_dot_com:cal.com:10600:0",
        "cal_dot_com:cal.com:10600:-1",
        "cal_dot_com:cal.com:10600:999",
        "missing:cal.com:10600:1",
    ],
)
def test_prepare_refuses_bad_ordinals_and_unknown_stems(tmp_path, key):
    epoch = load_epoch(root=DEFAULT_ROOT)
    source = read_json(SOURCE)
    source["excluded_comments"] = [key]
    path = tmp_path / "source.json"
    path.write_text(json.dumps(source), encoding="utf-8")
    with pytest.raises(ValueError):
        p.prepare_leak_manifest_v2(path, epoch.root, epoch)


@pytest.mark.parametrize(
    "mutation", ["duplicate_pr", "epoch", "source_bytes", "receipt_bytes", "raw_bytes"]
)
def test_preparation_and_receipt_refuse_drift(tmp_path, mutation):
    epoch = load_epoch(root=DEFAULT_ROOT)
    source = tmp_path / "source.json"
    shutil.copyfile(SOURCE, source)
    raw = tmp_path / "raw"
    shutil.copytree(epoch.root, raw)
    if mutation in ("duplicate_pr", "epoch"):
        value = read_json(source)
        if mutation == "duplicate_pr":
            value["excluded_prs"].append(value["excluded_prs"][0])
        else:
            value["golden_epoch"] = "wrong"
        source.write_text(json.dumps(value), encoding="utf-8")
        with pytest.raises(ValueError):
            p.prepare_leak_manifest_v2(source, raw, epoch)
        return
    receipt = tmp_path / "prepared.json"
    p.prepare_leak_manifest_v2(source, raw, epoch, receipt)
    path = (
        source
        if mutation == "source_bytes"
        else receipt
        if mutation == "receipt_bytes"
        else raw / "golden_comments/cal_dot_com.json"
    )
    if mutation == "receipt_bytes":
        data = read_json(path)
        data["excluded_comments"][0]["golden_text_sha256"] = "wrong"
        path.write_text(json.dumps(data), encoding="utf-8")
    else:
        path.write_text(path.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    with pytest.raises(ValueError):
        p.ingest_leak_manifest_v2(receipt, epoch)


def test_unfrozen_and_holdout_refusal_and_textbook_no_exclusion(
    population_case, tmp_path
):
    epoch, subsets, contamination, manifest, _ = population_case
    clean = p.resolve_population(
        "uncontaminated", epoch, "core", subsets, contamination, manifest
    )
    contamination["entries"].append(
        dict(
            pr_url=subsets["gate"][0],
            bug_ids=[],
            scope="none",
            match_kind="textbook_class",
            evidence_refs=["lens"],
            rationale="generic class",
        )
    )
    manifest["contamination_sha256"] = digest(contamination)
    assert (
        p.resolve_population(
            "uncontaminated", epoch, "core", subsets, contamination, manifest
        ).bug_ids
        == clean.bug_ids
    )
    contamination["status"] = "unfrozen"
    with pytest.raises(ValueError, match="unfrozen"):
        p.resolve_population(
            "uncontaminated", epoch, "core", subsets, contamination, manifest
        )
    with pytest.raises(ValueError, match="holdout"):
        p.resolve_population("holdout", epoch, "core", subsets, contamination, manifest)
    with pytest.raises(ValueError, match="unfrozen"):
        p.freeze_populations(
            epoch, subsets, contamination, out=tmp_path / "x.json", frozen_at="now"
        )

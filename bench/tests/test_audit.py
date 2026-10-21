"""Auditors see separate public packets and receipts bind their exact bytes."""

from dataclasses import replace
from pathlib import Path

import pytest

from bench.runner import audit as a
from bench.runner import compare as c
from bench.runner.epochs import file_hash, read_json
from bench.tests.conftest import refuses, write
from bench.tests.test_compare import case as case
from bench.tests.test_compare import paired
from bench.tests.test_validity import fingerprint as fingerprint
from bench.tests.test_validity import window as window


def label_files(frozen, directory, dispute=False):
    packet = directory / "public/packets.json"
    paths = []
    for seat in ("Sol", "Opus"):
        labels = [
            dict(
                sample_id=r["sample_id"],
                correct=not (dispute and seat == "Opus" and i == 0),
                evidence_refs=["repository:path:line"],
            )
            for i, r in enumerate(frozen["rows"])
        ]
        path = directory / (seat + ".json")
        write(path, dict(auditor=seat, packet_sha256=file_hash(packet), labels=labels))
        paths.append(str(path))
    return paths


def resolve_audit(spec, packets, directory, *, dispute=False):
    arms = {arm.name: arm.scores for arm in (spec.baseline, spec.candidate)}
    frozen = a.sample(
        arms,
        seed=17,
        out=directory,
        load=packets.__getitem__,
        unseal=spec.unseal,
        final_sha=spec.candidate.treatment_git_sha,
    )
    paths = dict(
        sample_path=str(directory / "private/sample.json"),
        packet_path=str(directory / "public/packets.json"),
        label_paths=label_files(frozen, directory, dispute),
    )
    receipt = directory / "receipt.json"
    write(receipt, a.merge(**paths))
    return frozen, paths, receipt


def test_seeded_blind_audit_owner_queue_and_receipt_binding(case, tmp_path):
    spec, packets = case.build()
    directory = tmp_path / "audit"
    frozen, paths, receipt = resolve_audit(spec, packets, directory, dispute=True)
    arms = {arm.name: arm.scores for arm in (spec.baseline, spec.candidate)}
    assert frozen == a.sample(
        arms, seed=17, out=tmp_path / "again", load=packets.__getitem__
    )
    public = Path(paths["packet_path"])
    assert public.parent != Path(paths["sample_path"]).parent
    assert all(
        set(r) == {"sample_id", "repository", "pr_url", "text", "path", "line"}
        for r in read_json(public)["findings"]
    )
    result = read_json(receipt)
    assert len(result["owner_queue"]) == 1 and not result["resolved"]
    refuses(a.load_receipt, receipt)
    owner = directory / "owner.json"
    value = read_json(paths["label_paths"][0])
    sid = result["owner_queue"][0]["sample_id"]
    value.update(
        auditor="owner", labels=[r for r in value["labels"] if r["sample_id"] == sid]
    )
    write(owner, value)
    paths["label_paths"].append(str(owner))
    write(receipt, a.merge(**paths))
    assert a.load_receipt(receipt)["resolved"]
    refuses(a.load_receipt, receipt, {"other": ()})
    public.write_bytes(public.read_bytes() + b" ")
    refuses(a.merge, **paths, match="packet")
    refuses(a.load_receipt, receipt)


@pytest.mark.parametrize("count", [0, 1, 3])
def test_same_size_census_and_empty_arm_recall(case, tmp_path, count):
    spec, packets = case.build()
    ref = spec.candidate.scores[0]
    for r in spec.candidate.scores:
        packets[r] = replace(
            packets[r], candidates=packets[r].candidates[:count] if r == ref else ()
        )
    frozen, paths, receipt = resolve_audit(spec, packets, tmp_path / "audit")
    assert [
        sum(r["arm"] == arm for r in frozen["rows"])
        for arm in ("baseline", "candidate")
    ] == [count, count]
    report = paired(replace(spec, audit_path=str(receipt)), packets)
    assert report.promotion.guards["groundedness"] == (
        "pass" if count else "inconclusive"
    )
    assert report.population_reports[0].candidate_recall == 0
    if count:
        frozen["rows"] = [r for i, r in enumerate(frozen["rows"]) if i != 0]
        write(Path(paths["sample_path"]), frozen)
        write(Path(paths["packet_path"]), a.public_packet(frozen))
        paths["label_paths"] = label_files(frozen, tmp_path / "audit")
        refuses(a.merge, **paths, match="unequal")


def test_operational_audit_is_required_and_finding_sample_is_bound(case, tmp_path):
    spec, packets = case.build()
    refuses(paired, spec, packets, match="audit")
    packets = {
        r: replace(
            p,
            candidates=tuple(
                replace(f, provenance_status="complete", discovery_dimensions=("bug",))
                for f in p.candidates
            ),
        )
        for r, p in packets.items()
    }
    _, _, receipt = resolve_audit(spec, packets, tmp_path / "audit")
    spec = replace(spec, audit_path=str(receipt))
    report = paired(spec, packets)
    assert report.promotion.verdict == "pass"
    dims = report.population_reports[0].dimensions["baseline"]
    assert dims["bug"]["groundedness"] == 1 and dims["security"]["groundedness"] is None
    ref = spec.baseline.scores[0]
    packets[ref] = replace(
        packets[ref],
        candidates=tuple(
            replace(f, text="changed finding") for f in packets[ref].candidates
        ),
    )
    refuses(paired, spec, packets)


def test_holdout_compare_and_audit_use_the_final_receipt_once(
    case, tmp_path, monkeypatch
):
    spec, packets = case.build("holdout", False)
    _, _, receipt = resolve_audit(spec, packets, tmp_path / "audit")
    spec = replace(spec, audit_path=str(receipt))
    report = paired(spec, packets)
    assert report.population_reports[0].population == "holdout"
    refuses(resolve_audit, spec, packets, tmp_path / "second-audit", match="second use")
    refuses(
        paired,
        replace(spec, candidate=replace(spec.candidate, treatment_git_sha="b" * 40)),
        packets,
        match="sealed",
    )
    compare = c.compare_arms
    monkeypatch.setattr(
        c, "compare_arms", lambda s: compare(s, packet_loader=packets.__getitem__)
    )
    path = tmp_path / "spec.json"
    write(path, __import__("dataclasses").asdict(spec))
    c.main(["--spec", str(path), "--out", str(tmp_path / "one.json")])
    refuses(
        c.main,
        ["--spec", str(path), "--out", str(tmp_path / "two.json")],
        match="second use",
    )

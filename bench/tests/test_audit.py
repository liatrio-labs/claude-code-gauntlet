"""Audit packets hide arms and receipts bind every imported label to the sample."""

from dataclasses import replace

import pytest

from bench.runner import audit as a
from bench.runner import compare as c
from bench.runner.epochs import canonical, digest, read_json
from bench.tests.test_compare import comparison_case as comparison_case
from bench.tests.test_validity import fingerprint as fingerprint
from bench.tests.test_validity import window as window


def resolve_audit(spec, packets, directory, *, dispute=False):
    arms = {arm.name: arm.scores for arm in (spec.baseline, spec.candidate)}
    frozen = a.sample(
        arms, seed=17, size=30, out=directory, packet_loader=packets.__getitem__
    )
    paths = {"sample_path": str(directory / "sample.json"), "owner_path": None}
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
        path.write_bytes(
            canonical(dict(auditor=seat, sample_sha256=digest(frozen), labels=labels))
        )
        paths[seat.lower() + "_path"] = str(path)
    result = a.merge(paths["sample_path"], [paths["sol_path"], paths["opus_path"]])
    receipt = directory / "receipt.json"
    receipt.write_bytes(canonical(result))
    return frozen, paths, receipt


def test_seeded_blind_audit_owner_queue_and_receipt_binding(comparison_case, tmp_path):
    spec, packets = comparison_case.build()
    frozen, paths, receipt = resolve_audit(
        spec, packets, tmp_path / "audit", dispute=True
    )
    again = a.sample(
        {arm.name: arm.scores for arm in (spec.baseline, spec.candidate)},
        seed=17,
        size=30,
        out=tmp_path / "again",
        packet_loader=packets.__getitem__,
    )
    assert frozen == again
    public = read_json(tmp_path / "audit/packets.json")
    assert all(
        set(r) == {"sample_id", "repository", "pr_url", "text", "path", "line"}
        for r in public["findings"]
    )
    result = read_json(receipt)
    assert len(result["owner_queue"]) == 1 and not result["resolved"]
    with pytest.raises(ValueError):
        a.load_receipt(receipt)
    row = result["owner_queue"][0]
    owner = tmp_path / "owner.json"
    owner.write_bytes(
        canonical(
            dict(
                auditor="owner",
                sample_sha256=digest(frozen),
                labels=[
                    dict(
                        sample_id=row["sample_id"],
                        correct=True,
                        evidence_refs=["owner:reference"],
                    )
                ],
            )
        )
    )
    paths["owner_path"] = str(owner)
    receipt.write_bytes(
        canonical(
            a.merge(
                paths["sample_path"],
                [paths["sol_path"], paths["opus_path"], paths["owner_path"]],
            )
        )
    )
    assert a.load_receipt(receipt)["resolved"]
    with pytest.raises(ValueError):
        a.load_receipt(receipt, {"other": ()})
    changed = read_json(paths["sol_path"])
    changed["sample_sha256"] = "different"
    from pathlib import Path

    Path(paths["sol_path"]).write_bytes(canonical(changed))
    with pytest.raises(ValueError):
        a.load_receipt(receipt)


def test_operational_audit_is_required_and_finding_sample_is_bound(
    comparison_case, tmp_path
):
    spec, packets = comparison_case.build()
    with pytest.raises(ValueError, match="audit"):
        c.compare_arms(spec, packet_loader=packets.__getitem__)
    packets = {
        ref: replace(
            packet,
            candidates=tuple(
                replace(f, provenance_status="complete", discovery_dimensions=("bug",))
                for f in packet.candidates
            ),
        )
        for ref, packet in packets.items()
    }
    _, paths, receipt = resolve_audit(spec, packets, tmp_path / "audit")
    report = c.compare_arms(
        replace(spec, audit_path=str(receipt)), packet_loader=packets.__getitem__
    )
    assert report.promotion.verdict == "pass"
    gate = report.population_reports[0]
    assert gate.dimensions["baseline"]["bug"]["groundedness"] == 1
    assert gate.dimensions["baseline"]["security"]["groundedness"] is None
    ref = spec.baseline.scores[0]
    packets[ref] = replace(
        packets[ref],
        candidates=tuple(
            replace(f, text="changed finding") for f in packets[ref].candidates
        ),
    )
    with pytest.raises(ValueError):
        c.compare_arms(
            replace(spec, audit_path=str(receipt)), packet_loader=packets.__getitem__
        )

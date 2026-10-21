"""Blinded same-size samples and packet-bound auditor receipts."""

import random
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal, TypedDict

from bench.runner.cli import spec_output_args
from bench.runner.contracts import ScoreRef, exclusive_json
from bench.runner.decode import decode
from bench.runner.epochs import digest, file_hash, load_epoch, read_json
from bench.runner.looks import holdout
from bench.runner.populations import check_holdout, resolve_population


class SampleWire(TypedDict):
    seed: int
    arms: dict[str, list[dict]]
    rows: list[dict]
    unseal: str | None
    population: dict


@dataclass(frozen=True, slots=True)
class Label:
    sample_id: str
    correct: bool
    evidence_refs: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Labels:
    auditor: Literal["Sol", "Opus", "owner"]
    packet_sha256: str
    labels: tuple[Label, ...]


def public_packet(frozen):
    findings, receipt = [], frozen["unseal"]
    sha = read_json(receipt)["final_sha"] if receipt else None
    if receipt:
        holdout(receipt, sha)
    for row in frozen["rows"]:
        f = row["finding"]
        epoch = load_epoch(row["score_ref"]["epoch_id"])
        check_holdout((f["pr_url"],), epoch, receipt, sha)
        findings.append(
            dict(sample_id=row["sample_id"], repository=epoch.shas[f["pr_url"]])
            | {k: f[k] for k in ("pr_url", "text", "path", "line")}
        )
    return dict(sample_sha256=digest(frozen), findings=findings)


def sample(
    arms,
    *,
    seed,
    out,
    contamination_path,
    population="gate-uncontaminated",
    size=30,
    load=None,
    unseal=None,
    final_sha=None,
):
    from bench.runner.compare import _packet_run, default_packet_loader

    loader = load or default_packet_loader
    if (
        len(arms) not in (1, 2)
        or type(seed) is not int
        or type(size) is not int
        or not 1 <= size <= 30
    ):
        raise ValueError("one or two arms, seed and sample size 1..30 required")
    refs = [ref for refs in arms.values() for ref in refs]
    if not refs or len({ref.epoch_id for ref in refs}) != 1:
        raise ValueError("audit requires one epoch")
    epoch = load_epoch(refs[0].epoch_id)
    pop = resolve_population(
        population,
        epoch,
        "core",
        read_json(contamination_path),
        unseal=unseal,
        treatment_sha=final_sha,
    )
    pools, final = {}, False
    for name, refs in sorted(arms.items()):
        if len(set(refs)) != len(refs):
            raise ValueError("duplicate delivered findings")
        packets = [loader(ref) for ref in refs]
        if unseal:
            final |= bool(refs) and all(
                _packet_run(p)["treatment_git_sha"] == final_sha for p in packets
            )
        for packet in packets:
            check_holdout(packet.manifest.requested_pr_urls, epoch, unseal, final_sha)
        pools[name] = sorted(
            [
                (p.score_ref, f)
                for p in packets
                for f in p.candidates
                if f.pr_url in pop.pr_urls
            ],
            key=lambda pair: (pair[0].run_id, pair[0].score_id, pair[1].candidate_id),
        )
    count = min(size, *(len(pool) for pool in pools.values()))
    rng, rows = random.Random(seed), []
    for name, pool in pools.items():
        for ref, finding in rng.sample(pool, count):
            rows.append(
                dict(
                    sample_id=f"s-{rng.getrandbits(128):032x}",
                    arm=name,
                    score_ref=asdict(ref),
                    finding=asdict(finding),
                )
            )
    rng.shuffle(rows)
    frozen: SampleWire = dict(
        seed=seed,
        arms={a: [asdict(r) for r in refs] for a, refs in arms.items()},
        rows=rows,
        unseal=unseal,
        population=asdict(pop),
    )
    if unseal:
        if not final:
            raise ValueError("holdout audit requires approved final treatment")
        holdout(unseal, final_sha, use="audit")
    directory = Path(out)
    exclusive_json(directory / "private/sample.json", frozen)
    exclusive_json(directory / "public/packets.json", public_packet(frozen))
    return frozen


def merge(sample_path, label_paths, packet_path):
    if len(label_paths) not in (2, 3):
        raise ValueError("Sol and Opus labels, with optional owner labels, required")
    frozen = read_json(sample_path)
    if read_json(packet_path) != public_packet(frozen):
        raise ValueError("auditor packet differs from frozen sample")
    packet_hash = file_hash(Path(packet_path))
    ids = {r["sample_id"] for r in frozen["rows"]}
    counts = [sum(r["arm"] == arm for r in frozen["rows"]) for arm in frozen["arms"]]
    if (
        len(ids) != len(frozen["rows"])
        or len(counts) not in (1, 2)
        or any(n > 30 for n in counts)
        or any(
            r["finding"]["pr_url"] not in frozen["population"]["pr_urls"]
            for r in frozen["rows"]
        )
    ):
        raise ValueError("duplicate sample or invalid audit population/counts")
    labels = []
    for seat, path in zip(("Sol", "Opus", "owner"), label_paths, strict=False):
        value = decode(Labels, read_json(path))
        rows = {r.sample_id: r for r in value.labels}
        if (
            value.auditor != seat
            or value.packet_sha256 != packet_hash
            or len(rows) != len(value.labels)
            or set(rows) - ids
            or (seat != "owner" and set(rows) != ids)
            or any(not r.evidence_refs for r in rows.values())
        ):
            raise ValueError("auditor/sample/packet/coverage/evidence mismatch")
        labels.append(rows)
    if len(labels) == 2:
        labels.append({})
    agreed, disputed = [], []
    for row in frozen["rows"]:
        sid = row["sample_id"]
        a, b, owner = labels[0][sid], labels[1][sid], labels[2].get(sid)
        if a.correct == b.correct and owner:
            raise ValueError("owner resolution must name a dispute")
        resolved = a if a.correct == b.correct else owner
        (agreed if resolved else disputed).append(
            {
                **row,
                "correct": resolved.correct if resolved else None,
                "evidence_refs": [
                    ref
                    for label in (a, b, owner)
                    if label
                    for ref in label.evidence_refs
                ],
            }
        )
    return dict(
        sources=dict(
            sample_path=sample_path, label_paths=label_paths, packet_path=packet_path
        ),
        agreed=agreed,
        owner_queue=disputed,
        resolved=not disputed,
    )


def load_receipt(path, arms=None, population=None):
    value = read_json(path)
    if value != merge(**value["sources"]) or not value["resolved"]:
        raise ValueError("unresolved or changed audit receipt")
    frozen = read_json(value["sources"]["sample_path"])
    if population is not None and digest(frozen["population"]) != digest(
        asdict(population)
    ):
        raise ValueError("audit belongs to different decision population")
    if arms is not None and frozen["arms"] != {
        a: [asdict(r) for r in refs] for a, refs in arms.items()
    }:
        raise ValueError("audit belongs to different score arms")
    return value


def main(argv=None):
    args = spec_output_args(__doc__, argv)
    value = read_json(args.spec)
    if "arms" in value:
        value["arms"] = {
            a: tuple(decode(ScoreRef, r) for r in refs)
            for a, refs in value["arms"].items()
        }
        sample(**value, out=args.out)
    else:
        exclusive_json(Path(args.out), merge(**value))


if __name__ == "__main__":
    main()

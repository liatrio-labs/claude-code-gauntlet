"""Blinded delivered-finding samples, two auditor labels, and owner resolution."""

from __future__ import annotations

import argparse
import random
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal, TypedDict

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bench.runner.decode import decode  # noqa: E402
from bench.runner.epochs import digest, load_epoch, read_json  # noqa: E402
from bench.runner.observations import ScoreRef, exclusive_json  # noqa: E402


class SampleWire(TypedDict):
    seed: int
    arms: dict[str, list[dict]]
    rows: list[dict]


@dataclass(frozen=True, slots=True)
class Label:
    sample_id: str
    correct: bool
    evidence_refs: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Labels:
    auditor: Literal["Sol", "Opus", "owner"]
    sample_sha256: str
    labels: tuple[Label, ...]


def sample(arms, *, seed, out, size=30, packet_loader=None):
    from bench.runner.compare import default_packet_loader

    loader = packet_loader or default_packet_loader
    if len(arms) != 2 or type(seed) is not int or type(size) is not int or size < 1:
        raise ValueError("two arms, seed and positive sample size required")
    rng, rows = random.Random(seed), []
    for name, refs in sorted(arms.items()):
        candidates = [
            (ref, finding) for ref in refs for finding in loader(ref).candidates
        ]
        if not candidates or len(set(refs)) != len(refs):
            raise ValueError("empty or duplicate delivered findings")
        if any(
            c.pr_url in load_epoch(ref.epoch_id).subsets["holdout"]
            for ref, c in candidates
        ):
            raise ValueError("holdout sealed")
        candidates.sort(
            key=lambda pair: (pair[0].run_id, pair[0].score_id, pair[1].candidate_id)
        )
        for ref, finding in rng.sample(candidates, min(size, len(candidates))):
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
    )
    directory = Path(out)
    directory.mkdir(parents=True, exist_ok=True)
    exclusive_json(directory / "sample.json", frozen)
    packets = [
        {
            "sample_id": r["sample_id"],
            "repository": load_epoch(r["score_ref"]["epoch_id"]).shas[
                r["finding"]["pr_url"]
            ],
            **{k: r["finding"][k] for k in ("pr_url", "text", "path", "line")},
        }
        for r in rows
    ]
    exclusive_json(
        directory / "packets.json", dict(sample_sha256=digest(frozen), findings=packets)
    )
    return frozen


def merge(sample_path, label_paths):
    if len(label_paths) not in (2, 3):
        raise ValueError("Sol and Opus labels, with optional owner labels, required")
    frozen = read_json(sample_path)
    sample_hash = digest(frozen)
    ids = {r["sample_id"] for r in frozen["rows"]}
    if len(ids) != len(frozen["rows"]):
        raise ValueError("duplicate frozen sample")
    labels = []
    for seat, path in zip(
        ("Sol", "Opus", "owner"), [*label_paths, None][:3], strict=True
    ):
        value = (
            decode(Labels, read_json(path)) if path else Labels(seat, sample_hash, ())
        )
        rows = {r.sample_id: r for r in value.labels}
        if (
            value.auditor != seat
            or value.sample_sha256 != sample_hash
            or len(rows) != len(value.labels)
            or set(rows) - ids
            or (seat != "owner" and set(rows) != ids)
            or any(not r.evidence_refs for r in rows.values())
        ):
            raise ValueError("auditor/sample/coverage/evidence mismatch")
        labels.append(rows)
    agreed, disputed = [], []
    for row in frozen["rows"]:
        sid = row["sample_id"]
        a, b = labels[0][sid], labels[1][sid]
        owner = labels[2].get(sid)
        if a.correct == b.correct and owner:
            raise ValueError("owner resolution must name a dispute")
        resolved = a if a.correct == b.correct else owner
        status = "disputed"
        if resolved:
            status = "owner_resolved" if owner else "agreed"
        (agreed if resolved else disputed).append(
            {
                **row,
                "correct": resolved.correct if resolved else None,
                "status": status,
                "evidence_refs": list(
                    a.evidence_refs
                    + b.evidence_refs
                    + (owner.evidence_refs if owner else ())
                ),
            }
        )
    return dict(
        sources=dict(sample_path=sample_path, label_paths=label_paths),
        sample_sha256=sample_hash,
        agreed=agreed,
        owner_queue=disputed,
        resolved=not disputed,
    )


def load_receipt(path, arms=None):
    value = read_json(path)
    if value != merge(**value["sources"]) or not value["resolved"]:
        raise ValueError("unresolved or changed audit receipt")
    frozen = read_json(value["sources"]["sample_path"])
    if arms is not None and frozen["arms"] != {
        a: [asdict(r) for r in refs] for a, refs in arms.items()
    }:
        raise ValueError("audit belongs to different score arms")
    return value


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for key in ("spec", "out"):
        parser.add_argument("--" + key, required=True)
    args = parser.parse_args(argv)
    config, out = args.spec, args.out
    value = read_json(config)
    if "arms" in value:
        value["arms"] = {
            a: tuple(decode(ScoreRef, r) for r in refs)
            for a, refs in value["arms"].items()
        }
        sample(**value, out=out)
    else:
        exclusive_json(Path(out), merge(**value))


if __name__ == "__main__":
    main()

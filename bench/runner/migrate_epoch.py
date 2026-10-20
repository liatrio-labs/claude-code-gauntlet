"""Offline epoch preparation and explicit, non-destructive migration commands."""

from __future__ import annotations

import argparse
import json
import shutil
import statistics
from dataclasses import asdict
from pathlib import Path
from typing import Literal, TypedDict

from bench.runner.epochs import (
    DEFAULT_ROOT,
    GoldenPRWire,
    Profile,
    build_bug_index,
    denominators,
    digest,
    file_hash,
    load_epoch,
    read_json,
    scorer_hash,
)
from bench.runner.observations import (
    CalibrationReport,
    Evaluator,
    LegacyArchive,
    ScoreRef,
    freeze_json,
)

KEPT_FILES = (
    "LICENSE",
    "pyproject.toml",
    "uv.lock",
    ".gitignore",
    "code_review_benchmark/__init__.py",
    "code_review_benchmark/step2_5_dedup_candidates.py",
    "code_review_benchmark/step3_judge_comments.py",
    "tests/conftest.py",
    "tests/test_step3.py",
)


class EpochCopyWire(TypedDict):
    source: str
    destination: str
    sha256: str


class PreparationManifestWire(TypedDict):
    schema_version: Literal[1]
    epoch_id: str
    files: dict[str, str]
    manifest_sha256: str
    copies: list[EpochCopyWire]


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, indent=2, ensure_ascii=True, allow_nan=False)
        stream.write("\n")


def freeze_epoch(
    golden: Path,
    scorer: Path,
    baselines: Path,
    out: Path,
    epoch_id: str,
    upstream_sha: str,
    profiles,
    primary,
):
    out.mkdir(parents=True, exist_ok=False)
    for name in ("benchmark_data.min.json", "pr_labels.json"):
        shutil.copyfile(golden / name, out / name)
    shutil.copytree(golden / "golden_comments", out / "golden_comments")
    shutil.copyfile(baselines, out / "baselines.json")
    for name in KEPT_FILES:
        target = out / "scorer" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(scorer / name, target)
    data = read_json(out / "benchmark_data.min.json")
    bugs = build_bug_index(data)
    write_json(out / "bugs.json", [asdict(b) for b in bugs])
    manifest = {
        "schema_version": 1,
        "epoch_id": epoch_id,
        "upstream_sha": upstream_sha,
        "scorer_sha": upstream_sha,
        "golden_sha256": digest(data),
        "scorer_sha256": scorer_hash(out / "scorer"),
        "bugs_sha256": digest([asdict(b) for b in bugs]),
        "labels_sha256": digest(read_json(out / "pr_labels.json")),
        "primary_profile": primary,
        "supported_profiles": profiles,
        "pr_count": len(data),
        "profile_denominators": denominators(
            bugs, profiles, read_json(golden / "subsets.json")
        ),
        "static_label_source_epoch": "dfc6cb4-v1",
        "file_hashes": {
            p.relative_to(out).as_posix(): file_hash(p)
            for p in sorted(out.rglob("*"))
            if p.is_file()
        },
    }
    write_json(out / "epoch.json", manifest)
    return manifest


def prepare(source: Path, upstream_sha: str, out: Path, *, root: Path = DEFAULT_ROOT):
    """Read upstream data/source bytes only; emit an inactive copy proposal."""
    if upstream_sha != "e616e849755441da38f18bf3adba2c9583b03803":
        raise ValueError("preparation requires the pinned upstream revision")
    source = Path(source)
    if (source / "offline").is_dir():
        source = source / "offline"
    out = Path(out)
    out.mkdir(parents=True, exist_ok=False)
    golden = out / "inputs"
    golden.mkdir()
    shutil.copytree(source / "golden_comments", golden / "golden_comments")
    upstream = read_json(source / "results/benchmark_data.json")
    old = read_json(root / "epochs/dfc6cb4-v1/benchmark_data.min.json")
    urls = {
        pr["url"]
        for p in (golden / "golden_comments").glob("*.json")
        for pr in read_json(p)
    }
    if urls != set(old):
        raise ValueError("migration changed PR identities")
    lean: dict[str, GoldenPRWire] = {}
    for url in sorted(urls):
        pr = upstream[url]
        lean[url] = {
            "pr_title": pr["pr_title"],
            "original_url": pr["original_url"],
            "source_repo": pr["source_repo"],
            "golden_comments": pr["golden_comments"],
            "reviews": [],
        }
    raw = {
        pr["url"]: pr["comments"]
        for p in (golden / "golden_comments").glob("*.json")
        for pr in read_json(p)
    }
    if any(raw[u] != lean[u]["golden_comments"] for u in urls):
        raise ValueError("new raw/scorer golden mismatch")
    bugs = build_bug_index(lean)
    if any(b.category is None for b in bugs):
        raise ValueError("new epoch requires categories")
    labels = read_json(root / "epochs/dfc6cb4-v1/pr_labels.json")
    old_raw = {
        pr["url"]: pr["comments"]
        for p in (root / "epochs/dfc6cb4-v1/golden_comments").glob("*.json")
        for pr in read_json(p)
    }
    for url, record in labels.items():
        positional = record.pop("comment_bug_types", [])
        old_claims = old_raw[url]
        by_claim = {
            digest([url, c["comment"], c["severity"]]): positional[i]
            for i, c in enumerate(old_claims)
            if len(positional) == len(old_claims)
        }
        # Only identical claims carry a positional label across the boundary.
        record["comment_bug_types_by_bug_id"] = {
            b.bug_id: by_claim.get(b.content_sha256, {"label_status": "not_relabelled"})
            for b in bugs
            if b.pr_url == url
        }
        comments = lean[url]["golden_comments"]
        record["derived"]["num_golden_comments"] = len(comments)
        record["derived"]["severity_mix"] = {
            s: sum(c["severity"] == s for c in comments)
            for s in sorted({c["severity"] for c in comments})
        }
    write_json(golden / "benchmark_data.min.json", lean)
    write_json(golden / "pr_labels.json", labels)
    shutil.copyfile(root / "subsets.json", golden / "subsets.json")
    scorer = out / "kept-source"
    shutil.copytree(root / "epochs/dfc6cb4-v1/scorer", scorer)
    for name in (
        "code_review_benchmark/step3_judge_comments.py",
        "tests/test_step3.py",
    ):
        shutil.copyfile(source / name, scorer / name)
    baselines = {
        "epoch_id": "e616e84-v1",
        "profile": "core",
        "measurement_status": "pending",
        "judge_pin": "claude-opus-4-5-20251101",
        "adjudicator_pin": "claude-opus-4-5-20251101",
        "scorer_sha": upstream_sha,
        "judge_sd": None,
        "delta_noise": None,
        "calibration_status": "pending",
        "anchors": {"rows": {}, "status": "pending"},
        "protected": {"status": "pending_orchestrator_commit"},
    }
    write_json(out / "baselines.json", baselines)
    epoch = out / "e616e84-v1"
    manifest = freeze_epoch(
        golden,
        scorer,
        out / "baselines.json",
        epoch,
        "e616e84-v1",
        upstream_sha,
        ["core", "all", "strict"],
        "core",
    )
    files = {
        p.relative_to(out).as_posix(): file_hash(p)
        for p in sorted(out.rglob("*"))
        if p.is_file()
    }
    copy_roots = {
        "e616e84-v1": "bench/golden/epochs/e616e84-v1",
        "inputs/benchmark_data.min.json": "bench/golden/benchmark_data.min.json",
        "inputs/golden_comments": "bench/golden/golden_comments",
        "inputs/pr_labels.json": "bench/golden/pr_labels.json",
        "kept-source": "bench/vendor/code-review-benchmark",
        "baselines.json": "bench/baselines.json",
    }
    copies: list[EpochCopyWire] = []
    for source_name, destination in copy_roots.items():
        source_root = out / source_name
        for path in (
            sorted(source_root.rglob("*")) if source_root.is_dir() else [source_root]
        ):
            if path.is_file():
                target = (
                    Path(destination) / path.relative_to(source_root)
                    if source_root.is_dir()
                    else Path(destination)
                )
                copies.append(
                    {
                        "source": path.relative_to(out).as_posix(),
                        "destination": target.as_posix(),
                        "sha256": file_hash(path),
                    }
                )
    proposal: PreparationManifestWire = {
        "schema_version": 1,
        "epoch_id": manifest["epoch_id"],
        "files": files,
        "manifest_sha256": file_hash(epoch / "epoch.json"),
        "copies": copies,
    }
    write_json(
        out / "copy-manifest.json",
        proposal,
    )
    return manifest


def archive_legacy_run(run_dir: Path, epoch) -> LegacyArchive:
    from bench.runner.observations import import_legacy

    return import_legacy(Path(run_dir), epoch)


def run_calibration(
    run_dir: Path, epoch, passes: int, *, scorer=None, anchor_rejudge=None
) -> CalibrationReport:
    from bench.runner.anchors import rejudge_anchors
    from bench.runner.score import score_run

    if passes < 2:
        raise ValueError("calibration requires at least two passes")
    packets = [
        (scorer or score_run)(run_dir, epoch_id=epoch.epoch_id) for _ in range(passes)
    ]
    sources = [p["manifest"]["source_artifact_hashes"] for p in packets]
    evaluators = {p["manifest"]["evaluator"]["evaluator_id"] for p in packets}
    if any(s != sources[0] for s in sources) or len(evaluators) != 1:
        raise ValueError("calibration requires identical frozen inputs and evaluator")
    variability: dict[Profile, dict[str, float]] = {}
    for profile in epoch.supported_profiles:
        variability[profile] = {}
        for key in ("recall", "precision_strict", "f1_strict", "f2_strict"):
            values = [p["profiles"][profile][key] for p in packets]
            if all(v is not None for v in values):
                variability[profile][key] = statistics.stdev(
                    float(v) for v in values if v is not None
                )
    evaluator = packets[0]["manifest"]["evaluator"]
    anchors = (anchor_rejudge or rejudge_anchors)(
        evaluator["judge_pin"], epoch_id=epoch.epoch_id
    )
    return CalibrationReport(
        schema_version=1,
        epoch_id=epoch.epoch_id,
        golden_sha256=epoch.golden_sha256,
        evaluator_id=evaluators.pop(),
        input_refs=tuple(ScoreRef(**p["score_ref"]) for p in packets),
        passes=passes,
        per_profile_variability=tuple(
            (p, tuple(sorted(v.items()))) for p, v in sorted(variability.items())
        ),
        anchor_results=tuple((k, freeze_json(v)) for k, v in sorted(anchors.items())),
        input_byte_hashes=tuple(sorted(sources[0].items())),
        evaluator=Evaluator(**evaluator),
        proposed_noise_threshold=None,
        derivation="fixed-candidate sample SD; no threshold inferred",
        status="complete",
    )


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prep = commands.add_parser("prepare")
    for option in ("source", "upstream-sha", "out"):
        prep.add_argument("--" + option, required=True)
    for name in ("archive-run", "rescore", "anchors", "calibrate"):
        cmd = commands.add_parser(name)
        cmd.add_argument("--epoch", required=True)
        if name != "anchors":
            cmd.add_argument("--run-dir", required=True, type=Path)
        if name in ("anchors", "calibrate"):
            cmd.add_argument("--out", required=True, type=Path)
        if name == "calibrate":
            cmd.add_argument("--passes", required=True, type=int)
    args = parser.parse_args(argv)
    if args.command == "prepare":
        prepare(Path(args.source), args.upstream_sha, Path(args.out))
    else:
        epoch = load_epoch(args.epoch, root=DEFAULT_ROOT)
        if args.command == "archive-run":
            archive_legacy_run(args.run_dir, epoch)
        elif args.command == "rescore":
            from bench.runner.score import score_run

            score_run(args.run_dir, epoch_id=args.epoch)
        elif args.command == "anchors":
            from bench.runner.anchors import rejudge_anchors

            result = rejudge_anchors("claude-opus-4-5-20251101", epoch_id=args.epoch)
            write_json(args.out, result)
        else:
            write_json(
                args.out, run_calibration(args.run_dir, epoch, args.passes).to_wire()
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Explicit contamination ingestion and frozen, epoch-owned populations."""

from __future__ import annotations

import argparse
import hashlib
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, TypedDict, get_args

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bench.runner.epochs import (  # noqa: E402
    DEFAULT_ROOT,
    Profile,
    digest,
    file_hash,
    included_bugs,
    load_epoch,
    read_json,
)
from bench.runner.observations import exclusive_json  # noqa: E402

PopulationName = Literal[
    "gate", "uncontaminated", "non-mini", "contaminated", "dev-slice", "dev-pool"
]
Scope = Literal["pr", "bug", "none"]
MatchKind = Literal["identifier", "scenario", "sweep", "textbook_class"]
FreezeStatus = Literal["unfrozen", "frozen"]


class ContaminationEntryWire(TypedDict):
    pr_url: str
    bug_ids: list[str]
    scope: Scope
    match_kind: MatchKind
    evidence_refs: list[str]
    rationale: str


class ContaminationManifestWire(TypedDict):
    schema_version: Literal[1]
    epoch_id: str
    golden_sha256: str
    status: FreezeStatus
    agent_read_sha256: str
    criterion_version: str
    entries: list[ContaminationEntryWire]
    frozen_at: str | None


class PopulationManifestWire(TypedDict):
    schema_version: Literal[1]
    epoch_id: str
    status: FreezeStatus
    contamination_sha256: str
    dev_pool_pr_urls: list[str]
    dev_slice_pr_urls: list[str]
    slice_selection_profile: Profile
    slice_tiebreak: Literal["canonical_pr_url"]
    frozen_at: str | None


@dataclass(frozen=True, slots=True)
class ContaminationEntry:
    pr_url: str
    bug_ids: tuple[str, ...]
    scope: Scope
    match_kind: MatchKind
    evidence_refs: tuple[str, ...]
    rationale: str


@dataclass(frozen=True, slots=True)
class PopulationManifest:
    schema_version: Literal[1]
    epoch_id: str
    status: FreezeStatus
    contamination_sha256: str
    dev_pool_pr_urls: tuple[str, ...]
    dev_slice_pr_urls: tuple[str, ...]
    slice_selection_profile: Profile
    slice_tiebreak: Literal["canonical_pr_url"]
    frozen_at: str | None


@dataclass(frozen=True, slots=True)
class Population:
    name: PopulationName
    bug_ids: tuple[str, ...]
    pr_urls: tuple[str, ...]
    nonempty_pr_clusters: tuple[tuple[str, tuple[str, ...]], ...]
    denominator: int
    content_sha256: str


class ExcludedCommentReceiptWire(TypedDict):
    source_key: str
    pr_url: str
    one_based_ordinal: int
    golden_text_sha256: str


class PreparedLeakManifestWire(TypedDict):
    schema_version: Literal[1]
    epoch_id: str
    upstream_sha: str
    golden_sha256: str
    source_path: str
    source_sha256: str
    golden_source: str
    raw_file_hashes: dict[str, str]
    excluded_comments: list[ExcludedCommentReceiptWire]
    receipt_sha256: str


def text_hash(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _raw_snapshot(source, epoch):
    source = Path(source).resolve()
    if (source / "offline").is_dir():
        source = source / "offline"
    data_path = source / "results/benchmark_data.json"
    if not data_path.is_file():
        data_path = source / "benchmark_data.min.json"
    upstream = read_json(data_path)
    raw, files, stems = (
        {},
        {str(data_path.relative_to(source)): file_hash(data_path)},
        {},
    )
    for path in sorted((source / "golden_comments").glob("*.json")):
        files[str(path.relative_to(source))] = file_hash(path)
        records = read_json(path)
        stems[path.stem] = records
        for pr in records:
            if pr["url"] in raw:
                raise ValueError("duplicate raw PR")
            raw[pr["url"]] = pr["comments"]
    lean = {
        url: {
            k: pr[k]
            for k in ("pr_title", "original_url", "source_repo", "golden_comments")
        }
        | {"reviews": []}
        for url, pr in upstream.items()
        if url in raw
    }
    if (
        digest(lean) != epoch.golden_sha256
        or set(lean) != set(raw)
        or any(raw[url] != lean[url]["golden_comments"] for url in raw)
    ):
        raise ValueError("raw canonical lean golden hash mismatch")
    return source, stems, files


def _validate_source(source, epoch):
    if (
        source.get("schema_version") != 2
        or source.get("golden_epoch") != epoch.upstream_sha
    ):
        raise ValueError("leak manifest requires v2 and matching upstream epoch")
    urls = {b.pr_url for b in epoch.bugs}
    excluded = [p["pr_url"] for p in source["excluded_prs"]]
    if len(excluded) != len(set(excluded)) or set(excluded) - urls:
        raise ValueError("duplicate or unknown excluded PR")
    if len(source["excluded_comments"]) != len(set(source["excluded_comments"])):
        raise ValueError("duplicate excluded comment")


def prepare_leak_manifest_v2(
    source, golden_source, epoch, out=None
) -> PreparedLeakManifestWire:
    source = Path(source).resolve()
    manifest = read_json(source)
    _validate_source(manifest, epoch)
    raw_root, stems, files = _raw_snapshot(golden_source, epoch)
    comments: list[ExcludedCommentReceiptWire] = []
    for key in manifest["excluded_comments"]:
        parts = key.split(":")
        if len(parts) != 4:
            raise ValueError("invalid excluded comment key")
        stem, repo, number, ordinal = parts
        if (
            stem not in stems
            or not ordinal.isdecimal()
            or int(ordinal) <= 0
            or not number.isdecimal()
        ):
            raise ValueError("unknown stem or nonpositive ordinal")
        # URL metadata, rather than a guessed owner mapping, defines the join.
        prs = [
            pr
            for pr in stems[stem]
            if pr["url"].split("/")[-1] == number and pr["url"].split("/")[-3] == repo
        ]
        if len(prs) != 1 or int(ordinal) > len(prs[0]["comments"]):
            raise ValueError("ambiguous PR or out-of-range ordinal")
        comments.append(
            {
                "source_key": key,
                "pr_url": prs[0]["url"],
                "one_based_ordinal": int(ordinal),
                "golden_text_sha256": text_hash(
                    prs[0]["comments"][int(ordinal) - 1]["comment"]
                ),
            }
        )
    result: PreparedLeakManifestWire = {
        "schema_version": 1,
        "epoch_id": epoch.epoch_id,
        "upstream_sha": epoch.upstream_sha,
        "golden_sha256": epoch.golden_sha256,
        "source_path": str(source),
        "source_sha256": file_hash(source),
        "golden_source": str(raw_root),
        "raw_file_hashes": files,
        "excluded_comments": comments,
        "receipt_sha256": "",
    }
    result["receipt_sha256"] = digest(
        {k: v for k, v in result.items() if k != "receipt_sha256"}
    )
    if out is not None:
        exclusive_json(Path(out), result)
    return result


def ingest_leak_manifest_v2(prepared, epoch, out=None) -> ContaminationManifestWire:
    receipt_path = Path(prepared)
    receipt = read_json(receipt_path)
    if (
        receipt.get("epoch_id") != epoch.epoch_id
        or receipt.get("upstream_sha") != epoch.upstream_sha
        or receipt.get("golden_sha256") != epoch.golden_sha256
        or receipt.get("receipt_sha256")
        != digest({k: v for k, v in receipt.items() if k != "receipt_sha256"})
    ):
        raise ValueError("receipt epoch/hash mismatch")
    source_path = Path(receipt["source_path"])
    if file_hash(source_path) != receipt["source_sha256"]:
        raise ValueError("source manifest bytes changed")
    rebuilt = prepare_leak_manifest_v2(
        source_path, Path(receipt["golden_source"]), epoch
    )
    if rebuilt != receipt:
        raise ValueError("prepared raw snapshot or receipt bytes changed")
    source = read_json(source_path)
    evidence = [
        "sha256:" + receipt["source_sha256"],
        "receipt:" + file_hash(receipt_path),
    ]
    entries: list[ContaminationEntryWire] = [
        {
            "pr_url": row["pr_url"],
            "bug_ids": [],
            "scope": "pr",
            "match_kind": "identifier",
            "evidence_refs": evidence + row["trigger_passages"],
            "rationale": source["criterion"],
        }
        for row in source["excluded_prs"]
    ]
    for row in receipt["excluded_comments"]:
        matches = [
            b
            for b in epoch.bugs
            if b.pr_url == row["pr_url"] and b.ordinal == row["one_based_ordinal"] - 1
        ]
        if (
            len(matches) != 1
            or text_hash(matches[0].comment) != row["golden_text_sha256"]
        ):
            raise ValueError("excluded comment text-hash mismatch")
        entries.append(
            {
                "pr_url": row["pr_url"],
                "bug_ids": [matches[0].bug_id],
                "scope": "bug",
                "match_kind": "sweep",
                "evidence_refs": evidence
                + [row["source_key"], row["golden_text_sha256"]]
                + source["sweep_passages"],
                "rationale": "Narrow sweep golden match; ordinal is epoch-local.",
            }
        )
    result: ContaminationManifestWire = {
        "schema_version": 1,
        "epoch_id": epoch.epoch_id,
        "golden_sha256": epoch.golden_sha256,
        "status": "unfrozen",
        "agent_read_sha256": "",
        "criterion_version": "leak-manifest-v2",
        "entries": entries,
        "frozen_at": None,
    }
    if out is not None:
        exclusive_json(Path(out), result)
    return result


def contamination_entries(manifest, epoch, *, require_frozen=True):
    if (
        manifest["schema_version"] != 1
        or manifest["epoch_id"] != epoch.epoch_id
        or manifest["golden_sha256"] != epoch.golden_sha256
    ):
        raise ValueError("contamination epoch mismatch")
    if require_frozen and (
        manifest["status"] != "frozen"
        or not manifest["frozen_at"]
        or not manifest["agent_read_sha256"]
        or not manifest["criterion_version"]
    ):
        raise ValueError("contamination dispositions are unfrozen")
    bugs = {b.bug_id: b.pr_url for b in epoch.bugs}
    result = []
    for entry in manifest["entries"]:
        row = ContaminationEntry(
            **{
                **entry,
                "bug_ids": tuple(entry["bug_ids"]),
                "evidence_refs": tuple(entry["evidence_refs"]),
            }
        )
        valid_scope = {
            "identifier": "pr",
            "scenario": "pr",
            "sweep": "bug",
            "textbook_class": "none",
        }
        if (
            row.pr_url not in set(bugs.values())
            or valid_scope.get(row.match_kind) != row.scope
            or (row.scope == "bug") != bool(row.bug_ids)
            or not row.evidence_refs
            or not row.rationale
            or len(set(row.bug_ids)) != len(row.bug_ids)
            or any(bugs.get(b) != row.pr_url for b in row.bug_ids)
        ):
            raise ValueError("invalid contamination scope/evidence")
        result.append(row)
    return tuple(result)


def _dev_sets(epoch, subsets):
    pool = sorted(
        {b.pr_url for b in epoch.bugs} - set(subsets["gate"]) - set(subsets["holdout"])
    )
    core = included_bugs(epoch, "core", frozenset(pool))
    selected = sorted(pool, key=lambda u: (-sum(b.pr_url == u for b in core), u))[:6]
    return pool, selected


def freeze_populations(
    epoch, subsets, contamination, *, out, frozen_at
) -> PopulationManifestWire:
    contamination_entries(contamination, epoch)
    pool, selected = _dev_sets(epoch, subsets)
    result: PopulationManifestWire = {
        "schema_version": 1,
        "epoch_id": epoch.epoch_id,
        "status": "frozen",
        "contamination_sha256": digest(contamination),
        "dev_pool_pr_urls": pool,
        "dev_slice_pr_urls": selected,
        "slice_selection_profile": "core",
        "slice_tiebreak": "canonical_pr_url",
        "frozen_at": frozen_at,
    }
    if not frozen_at:
        raise ValueError("freeze timestamp required")
    exclusive_json(Path(out), result)
    return result


def resolve_population(
    name, epoch, profile, subsets, contamination, populations
) -> Population:
    if name not in get_args(PopulationName):
        raise ValueError("unknown or sealed holdout population")
    entries: tuple[ContaminationEntry, ...] = ()
    if name != "gate":
        entries = contamination_entries(contamination, epoch)
        pool, selected = _dev_sets(epoch, subsets)
        if (
            populations["schema_version"] != 1
            or populations["epoch_id"] != epoch.epoch_id
            or populations["status"] != "frozen"
            or not populations["frozen_at"]
            or populations["contamination_sha256"] != digest(contamination)
            or populations["dev_pool_pr_urls"] != pool
            or populations["dev_slice_pr_urls"] != selected
            or populations["slice_selection_profile"] != "core"
            or populations["slice_tiebreak"] != "canonical_pr_url"
        ):
            raise ValueError("unfrozen or stale population selections")
    urls = set(subsets["gate"])
    if name in ("dev-pool", "dev-slice"):
        urls = set(
            populations[
                "dev_pool_pr_urls" if name == "dev-pool" else "dev_slice_pr_urls"
            ]
        )
    if urls & set(subsets["holdout"]):
        raise ValueError("sealed holdout population")
    bugs = included_bugs(epoch, profile, frozenset(urls))
    excluded_prs = {e.pr_url for e in entries if e.scope == "pr"}
    excluded_bugs = {b for e in entries if e.scope == "bug" for b in e.bug_ids}

    def excluded(b):
        return b.pr_url in excluded_prs or b.bug_id in excluded_bugs

    if name in ("uncontaminated", "non-mini"):
        bugs = tuple(
            b
            for b in bugs
            if not excluded(b)
            and (name != "non-mini" or b.pr_url not in subsets["mini"])
        )
    elif name == "contaminated":
        bugs = tuple(b for b in bugs if excluded(b))
    ids = tuple(sorted(b.bug_id for b in bugs))
    clusters = tuple(
        (u, tuple(sorted(b.bug_id for b in bugs if b.pr_url == u)))
        for u in sorted(urls)
        if any(b.pr_url == u for b in bugs)
    )
    return Population(
        name,
        ids,
        tuple(sorted(urls)),
        clusters,
        len(ids),
        digest([epoch.epoch_id, epoch.golden_sha256, profile, name, ids, sorted(urls)]),
    )


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for command in ("prepare-leak-manifest", "ingest-leak-manifest"):
        p = sub.add_parser(command)
        p.add_argument("--epoch", required=True)
        p.add_argument("--out", type=Path, required=True)
        if command.startswith("prepare"):
            p.add_argument("--source", type=Path, required=True)
            p.add_argument("--golden-source", type=Path, required=True)
        else:
            p.add_argument("--prepared", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        epoch = load_epoch(args.epoch, root=DEFAULT_ROOT)
        if args.command.startswith("prepare"):
            prepare_leak_manifest_v2(args.source, args.golden_source, epoch, args.out)
        else:
            ingest_leak_manifest_v2(args.prepared, epoch, args.out)
    except (ValueError, OSError, KeyError, TypeError) as exc:
        parser.exit(2, str(exc) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

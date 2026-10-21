"""Resolve epoch populations and freeze reviewed contamination dispositions once."""

from __future__ import annotations

import re
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal, TypedDict, get_args

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bench.runner.cli import spec_output_args  # noqa: E402
from bench.runner.epochs import (  # noqa: E402
    digest,
    file_hash,
    included_bugs,
    load_epoch,
    read_json,
)
from bench.runner.observations import exclusive_json  # noqa: E402

PopulationName = Literal[
    "gate",
    "gate-uncontaminated",
    "gate-uncontaminated-non-mini",
    "gate-contaminated",
    "dev-slice",
    "dev-pool",
    "holdout",
    "mini",
]
MatchKind = Literal["identifier", "scenario", "sweep", "textbook_class"]


class DispositionWire(TypedDict):
    pr_url: str
    bug_ids: list[str]
    scope: Literal["pr", "bug", "none"]
    match_kind: MatchKind
    evidence_refs: list[str]
    rationale: str


class ContaminationManifestWire(TypedDict):
    schema_version: Literal[1]
    epoch_id: str
    golden_sha256: str
    status: Literal["unfrozen", "frozen"]
    agent_read_sha256: str
    baseline_treatment_sha: str
    leak_report_sha256: str
    criterion_version: str
    entries: list[DispositionWire]
    frozen_at: str | None


@dataclass(frozen=True, slots=True)
class Population:
    name: PopulationName
    bug_ids: tuple[str, ...]
    pr_urls: tuple[str, ...]
    denominator: int
    content_sha256: str


def contamination_entries(manifest, epoch, *, require_frozen=True):
    if (
        manifest["schema_version"] != 1
        or manifest["epoch_id"] != epoch.epoch_id
        or manifest["golden_sha256"] != epoch.golden_sha256
    ):
        raise ValueError("contamination epoch mismatch")
    if require_frozen and (
        manifest["status"] != "frozen"
        or not all(
            manifest.get(k)
            for k in (
                "frozen_at",
                "agent_read_sha256",
                "baseline_treatment_sha",
                "leak_report_sha256",
                "criterion_version",
            )
        )
    ):
        raise ValueError("contamination dispositions are unfrozen")
    bugs = {b.bug_id: b.pr_url for b in epoch.bugs}
    scopes = dict(identifier="pr", scenario="pr", sweep="bug", textbook_class="none")
    for row in manifest["entries"]:
        if (
            row["pr_url"] not in bugs.values()
            or scopes.get(row["match_kind"]) != row["scope"]
            or (row["scope"] == "bug") != bool(row["bug_ids"])
            or len(set(row["bug_ids"])) != len(row["bug_ids"])
            or any(bugs.get(b) != row["pr_url"] for b in row["bug_ids"])
            or not row["evidence_refs"]
            or not row["rationale"]
        ):
            raise ValueError("invalid contamination scope/evidence")
    return manifest["entries"]


def import_dispositions(source, epoch, out=None):
    source = Path(source)
    value = read_json(source)
    if value["schema_version"] != 2 or value["golden_epoch"] != epoch.upstream_sha:
        raise ValueError("leak manifest epoch mismatch")
    entries = []
    evidence = ["sha256:" + file_hash(source)]

    def append(url, ids, kind, refs):
        entries.append(
            dict(
                pr_url=url,
                bug_ids=ids,
                scope="bug" if ids else "pr",
                match_kind=kind,
                evidence_refs=evidence + refs,
                rationale=value["criterion"],
            )
        )

    for row in value["excluded_prs"]:
        append(row["pr_url"], [], "identifier", row["trigger_passages"])
    for key in value["excluded_comments"]:
        project, repo, number, ordinal = key.split(":")
        matches = [
            b
            for b in epoch.bugs
            if epoch.project_by_pr[b.pr_url] == project
            and b.pr_url.split("/")[-3:] == [repo, "pull", number]
            and b.ordinal == int(ordinal) - 1
        ]
        if len(matches) != 1:
            raise ValueError("unknown or ambiguous comment ordinal")
        bug = matches[0]
        append(
            bug.pr_url,
            [bug.bug_id],
            "sweep",
            [key, digest(bug.comment)] + value["sweep_passages"],
        )
    if len({digest(e) for e in entries}) != len(entries):
        raise ValueError("duplicate disposition")
    result: ContaminationManifestWire = dict(
        schema_version=1,
        epoch_id=epoch.epoch_id,
        golden_sha256=epoch.golden_sha256,
        entries=entries,
        criterion_version="leak-manifest-v2",
        status="unfrozen",
        frozen_at=None,
        agent_read_sha256="",
        baseline_treatment_sha="",
        leak_report_sha256="",
    )
    contamination_entries(result, epoch, require_frozen=False)
    if out:
        exclusive_json(Path(out), result)
    return result


def freeze_contamination(
    manifest, epoch, *, baseline_treatment_sha, out, frozen_at, root=ROOT
):
    from bench.runner import leaks, validity

    contamination_entries(manifest, epoch, require_frozen=False)
    if (
        manifest["status"] != "unfrozen"
        or not frozen_at
        or not re.fullmatch(r"[0-9a-f]{40}", baseline_treatment_sha)
    ):
        raise ValueError(
            "unfrozen manifest, explicit baseline SHA and timestamp required"
        )
    # A scan of this checkout can bind a future merge only when its plugin tree is identical.
    trees = [
        subprocess.run(
            ["git", "ls-tree", "-r", ref, "--", *validity.PLUGIN_PATHS],
            cwd=root,
            capture_output=True,
            check=True,
        ).stdout
        for ref in (baseline_treatment_sha, "HEAD")
    ]
    validity.require_clean_treatment(root)
    if not trees[0] or trees[0] != trees[1]:
        raise ValueError("baseline agent-read tree differs from scanned checkout")
    agent_hash = validity.agent_read_hash(root)
    report = leaks.scan_repository(root)
    if not report.passed:
        raise ValueError("freeze requires a passing scan")
    result = {
        **manifest,
        "status": "frozen",
        "frozen_at": frozen_at,
        "baseline_treatment_sha": baseline_treatment_sha,
        "agent_read_sha256": agent_hash,
        "leak_report_sha256": digest(asdict(report)),
    }
    validity.require_clean_treatment(root)
    if validity.agent_read_hash(root) != agent_hash:
        raise ValueError("agent-read tree changed during freeze")
    exclusive_json(Path(out), result)
    return result


def check_baseline_binding(manifest, treatment_sha, agent_read_sha256):
    if (
        manifest.get("baseline_treatment_sha") != treatment_sha
        or manifest.get("agent_read_sha256") != agent_read_sha256
        or not manifest.get("leak_report_sha256")
    ):
        raise ValueError("contamination baseline treatment/hash/report mismatch")


def check_holdout(urls, epoch, unseal=None, treatment_sha=None, *, use=None):
    if set(urls) & set(epoch.subsets["holdout"]):
        from bench.runner.looks import holdout

        holdout(unseal, treatment_sha, use=use)


def resolve_population(
    name, epoch, profile, contamination, *, unseal=None, treatment_sha=None
):
    if name not in get_args(PopulationName):
        raise ValueError("unknown population")
    if name == "holdout":
        check_holdout(epoch.subsets["holdout"], epoch, unseal, treatment_sha)
    entries = contamination_entries(
        contamination, epoch, require_frozen=name.startswith("gate")
    )
    excluded_prs = {e["pr_url"] for e in entries if e["scope"] == "pr"}
    excluded_bugs = {b for e in entries if e["scope"] == "bug" for b in e["bug_ids"]}
    gate, holdout, mini = (set(epoch.subsets[k]) for k in ("gate", "holdout", "mini"))
    pool = {b.pr_url for b in epoch.bugs} - gate - holdout - excluded_prs
    core = [
        b
        for b in included_bugs(epoch, "core", frozenset(pool))
        if b.bug_id not in excluded_bugs
    ]
    selected = sorted(pool, key=lambda u: (-sum(b.pr_url == u for b in core), u))[:6]
    urls = {"dev-pool": pool, "dev-slice": set(selected)}.get(
        name, set(epoch.subsets.get(name, gate))
    )
    if name.endswith("non-mini"):
        urls = urls - mini
    if name in ("gate-uncontaminated", "gate-uncontaminated-non-mini"):
        urls = urls - excluded_prs
    bugs = included_bugs(epoch, profile, frozenset(urls))
    if name.startswith("gate-") or name.startswith("dev-"):
        contaminated = name == "gate-contaminated"
        bugs = tuple(
            b
            for b in bugs
            if (b.pr_url in excluded_prs or b.bug_id in excluded_bugs) == contaminated
        )
    ids = tuple(sorted(b.bug_id for b in bugs))
    if name == "gate-contaminated":
        urls = {b.pr_url for b in bugs}
    return Population(
        name,
        ids,
        tuple(sorted(urls)),
        len(ids),
        digest([epoch.epoch_id, epoch.golden_sha256, profile, name, ids, sorted(urls)]),
    )


def main(argv=None):
    args = spec_output_args(__doc__, argv)
    value = read_json(args.spec)
    epoch = load_epoch()
    if "baseline_treatment_sha" in value:
        freeze_contamination(epoch=epoch, out=args.out, **value)
    else:
        import_dispositions(args.spec, epoch, args.out)


if __name__ == "__main__":
    main()

"""Bind actual posted bytes to pure delivery groups, without forge access."""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal, TypedDict, cast, get_args

from gauntlet.delivery import compose, gate, post
from gauntlet.delivery.input import ReviewInput
from gauntlet.diff import parse_diff, posting_policy
from gauntlet.marker import SHA_RE

from bench.adapter.adapt import _iter_comments
from bench.runner.decode import decode
from bench.runner.epochs import JsonValue, digest, read_json
from bench.runner.observations import Dimension, ProvenanceStatus, Surface

ROOT = Path(__file__).resolve().parents[2]
PLUGIN_PATHS = (".claude-plugin", "agents", "skills", "workflows", "scripts")


def require_clean_treatment(root=ROOT):
    status = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=no", "--", *PLUGIN_PATHS],
        cwd=root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    if status.stdout.strip():
        raise ValueError("input_drift: measurement requires clean tracked files")


def agent_read_hash(root=ROOT):
    # The index records names, modes and blob ids for the entire loaded plugin tree.
    entries = subprocess.run(
        ["git", "ls-files", "--stage", "-z", "--", *PLUGIN_PATHS],
        cwd=root,
        capture_output=True,
        check=True,
    ).stdout.split(b"\0")
    if not entries or entries == [b""]:
        raise ValueError("missing plugin index tree")
    return digest(sorted(row.decode("utf-8") for row in entries if row))


RUNS_LAYOUT = ("bench", "workspace", "runs")
_local_digest: list[str] = []
_treatment_roots: dict[str, Path] = {}


def resolve_treatment_root(run_dir, treatment):
    """Plugin checkout to replay a run's delivery under; None means this one.

    A run without a treatment digest predates the record and replays here.
    """
    if not _local_digest:
        _local_digest.append(agent_read_hash(ROOT))
    if not treatment or treatment == _local_digest[0]:
        return None
    # A run directory lives in the checkout that produced it, even through a symlink.
    resolved = Path(run_dir).resolve() if run_dir else None
    root = _treatment_roots.get(treatment) or (
        resolved.parents[3]
        if resolved and resolved.parent.parts[-3:] == RUNS_LAYOUT
        else None
    )
    # Recheck each load: that checkout may have moved on since the root was cached.
    try:
        matches = root is not None and agent_read_hash(root) == treatment
        if matches:
            require_clean_treatment(root)
    except (OSError, ValueError, subprocess.SubprocessError):
        matches = False
    if not matches:
        raise ValueError(f"no plugin tree for treatment {treatment}")
    _treatment_roots[treatment] = root
    return root


def packet_treatment_root(path, run_id):
    treatment = read_json(path / "run-provenance.json").get("treatment_git_sha")
    run_dir = next(
        (p for p in path.resolve().parents if p.name == run_id),
        None,
    )
    return resolve_treatment_root(run_dir, treatment)


@dataclass(frozen=True, slots=True)
class RenderedDelivery:
    body: str
    path: str | None
    line: int | None
    members: tuple[dict[str, JsonValue], ...]
    surface: Surface = "inline"


@dataclass(frozen=True, slots=True)
class DeliveryOmission:
    trace_ids: tuple[str, ...]
    reason_code: Literal["unanchored", "body_budget"]
    reason: str


@dataclass(frozen=True, slots=True)
class DeliveryReconstruction:
    groups: tuple[RenderedDelivery, ...]
    omissions: tuple[DeliveryOmission, ...]
    summary: str | None = None


class RenderedDeliveryWire(TypedDict):
    body: str
    path: str | None
    line: int | None
    trace_ids: list[str | None]
    surface: Surface


class DeliveryOmissionWire(TypedDict):
    trace_ids: list[str]
    reason_code: Literal["unanchored", "body_budget"]
    reason: str


class DeliveryReplayWire(TypedDict):
    groups: list[RenderedDeliveryWire]
    omissions: list[DeliveryOmissionWire]
    summary: str | None


ReplayErrorName = Literal["ValueError", "KeyError", "TypeError"]
_REPLAY_ERRORS: dict[ReplayErrorName, type[Exception]] = {
    "ValueError": ValueError,
    "KeyError": KeyError,
    "TypeError": TypeError,
}


class ReplayErrorWire(TypedDict):
    error: ReplayErrorName
    message: str


def _replay_main():
    from dataclasses import asdict

    try:
        result = reconstruct_delivery(**json.load(sys.stdin))
    except tuple(_REPLAY_ERRORS.values()) as exc:
        # Subclasses such as JSONDecodeError relay as their declared base.
        failure: ReplayErrorWire = {
            "error": next(n for n, t in _REPLAY_ERRORS.items() if isinstance(exc, t)),
            "message": str(exc),
        }
        json.dump(failure, sys.stdout)
        return
    wire: DeliveryReplayWire = {
        "groups": [
            {
                "body": row.body,
                "path": row.path,
                "line": row.line,
                "trace_ids": [f.get("trace_id") for f in row.members],
                "surface": row.surface,
            }
            for row in result.groups
        ],
        "omissions": [asdict(row) for row in result.omissions],
        "summary": result.summary,
    }
    json.dump(wire, sys.stdout)


def _replay_delivery(findings, diff_text, platform, root, review_body, sha):
    # Pin gauntlet before bench's bootstrap adds the loading checkout's scripts.
    script = (
        "import sys; sys.path[:0] = sys.argv[1:]; "
        "import gauntlet; "
        "from bench.runner.provenance import _replay_main; _replay_main()"
    )
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            script,
            str(root / "scripts"),
            str(ROOT),
        ],
        input=json.dumps(
            dict(
                selected_findings=findings,
                diff_text=diff_text,
                platform=platform,
                review_body=review_body,
                sha=sha,
            )
        ),
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    wire = json.loads(result.stdout)
    if "error" in wire:
        failure = decode(ReplayErrorWire, wire)
        raise _REPLAY_ERRORS[failure["error"]](failure["message"])
    wire = decode(DeliveryReplayWire, wire)
    members = {f.get("trace_id"): f for f in findings}
    return DeliveryReconstruction(
        tuple(
            RenderedDelivery(
                row["body"],
                row["path"],
                row["line"],
                tuple(members[tid] for tid in row["trace_ids"]),
                row["surface"],
            )
            for row in wire["groups"]
        ),
        tuple(decode(DeliveryOmission, row) for row in wire["omissions"]),
        wire["summary"],
    )


@dataclass(frozen=True, slots=True)
class DeliveryProvenance:
    text: str
    path: str | None
    line: int | None
    source_trace_ids: tuple[str, ...]
    discovery_dimensions: tuple[Dimension, ...]
    provenance_status: ProvenanceStatus = "complete"
    surface: Surface = "inline"
    issue_id: str | None = None


def _reconstruct_body_summary(
    findings, skipped_groups, inline_count, platform, review_body, sha
) -> DeliveryReconstruction:
    # Calling plan_summary without a captured SHA would consult today's HEAD.
    if (
        not isinstance(review_body, str)
        or not isinstance(sha, str)
        or not SHA_RE.fullmatch(sha)
    ):
        raise ValueError(
            "body reconstruction requires original review body and captured SHA"
        )
    finalized = post.plan_summary(
        cast(
            ReviewInput,
            {"findings": findings, "review_body": review_body, "sha": sha},
        ),
        skipped_groups,
        platform=platform,
        inline_count=inline_count if platform == "github" else None,
    )
    if finalized.budget_error:
        raise ValueError(finalized.budget_error)
    summary = finalized.summary.body
    rendered, omissions = [], []
    if skipped_groups:
        frame = compose._skipped_frame(
            sum(len(g) for g in skipped_groups),
            finalized.summary.shown,
            inline_count if platform == "github" else None,
        )
        if summary.count(frame) != 1:
            raise ValueError("ambiguous body section")
        cursor = summary.index(frame) + len(frame)
        shown = 0

        def matches_next(fragment: str) -> bool:
            return summary.startswith(fragment, cursor)

        for group, fragment in compose.digest_groups(skipped_groups, matches_next):
            members = tuple(cast(dict[str, JsonValue], e.finding) for e in group)
            if fragment is not None:
                rendered.append(
                    RenderedDelivery(
                        fragment,
                        cast(str | None, group[0].filepath),
                        cast(int | None, group[0].line),
                        members,
                        "body",
                    )
                )
                cursor += len(fragment)
                shown += len(group)
            else:
                omissions.append(
                    DeliveryOmission(
                        tuple(cast(str, f.get("trace_id")) for f in members),
                        "body_budget",
                        "whole body group omitted by summary budget",
                    )
                )
        if shown != finalized.summary.shown:
            raise ValueError("body fragment membership differs from composer")
    return DeliveryReconstruction(tuple(rendered), tuple(omissions), summary)


def reconstruct_delivery(
    selected_findings,
    diff_text,
    *,
    platform,
    treatment_root=None,
    review_body=None,
    sha=None,
):
    """Reproduce initial dry-run delivery ordering, folding and patch demotion."""
    findings = []
    for original in selected_findings:
        f = dict(original)
        for alias, key in (
            ("line", "line_start"),
            ("end_line", "line_end"),
            ("body", "description"),
        ):
            if alias not in f and key in f:
                f[alias] = f[key]
        findings.append(f)
    if treatment_root is not None:
        return _replay_delivery(
            findings, diff_text, platform, treatment_root, review_body, sha
        )
    data = cast(ReviewInput, {"findings": findings})
    facts = parse_diff(diff_text, policy=posting_policy(platform))
    plan = post.plan_delivery(data, facts, platform=platform)
    session = post.DeliverySession(dry_run=True)
    rendered = []
    omissions = []
    skipped_groups = []
    wants_body = review_body is not None and sha is not None
    for group in plan.groups:
        if isinstance(group, post.DegradedGroup):
            if wants_body:
                skipped_groups.append(
                    tuple(
                        compose.SkippedEntry(
                            m.path, m.finding.get("line"), session.activate(m)
                        )
                        for m in group.members
                    )
                )
                continue
            omissions.append(
                DeliveryOmission(
                    tuple(m.finding.get("trace_id") for m in group.members),
                    "unanchored",
                    group.warning,
                )
            )
            continue
        # Resolving every member proves group lineage independently of text/location joins.
        corroborators = post.resolve_corroborators(group, facts)
        inline = post.render_inline(
            group.primary,
            tuple(m.finding for m in corroborators),
            surface="inline" if platform == "github" else "discussion",
            plan=plan,
            facts=facts,
            session=session,
        )
        if inline.budget_error:
            if platform == "github":
                raise ValueError(inline.budget_error)
            omissions.append(
                DeliveryOmission(
                    tuple(
                        f.get("trace_id")
                        for f in (
                            group.primary.finding,
                            *(m.finding for m in corroborators),
                        )
                    ),
                    "body_budget",
                    inline.budget_error,
                )
            )
            continue
        anchor = group.primary.anchor
        line = anchor.line
        if isinstance(anchor.site, gate.GitHubApplySite) and anchor.site.multiline:
            line = anchor.site.apply_range[1]
        rendered.append(
            RenderedDelivery(
                inline.body.body,
                anchor.path,
                line,
                (group.primary.finding, *(m.finding for m in corroborators)),
            )
        )
    summary = None
    if wants_body:
        body = _reconstruct_body_summary(
            findings, skipped_groups, len(rendered), platform, review_body, sha
        )
        rendered.extend(body.groups)
        omissions.extend(body.omissions)
        summary = body.summary
    return DeliveryReconstruction(tuple(rendered), tuple(omissions), summary)


def map_delivery(
    payload,
    selected_findings,
    diff_text,
    *,
    treatment_root=None,
    review_body=None,
    sha=None,
) -> tuple[DeliveryProvenance, ...]:
    """Attach provenance only after exact ordered body/path/line equality."""
    actual = tuple(_iter_comments(payload))
    reconstructed = reconstruct_delivery(
        selected_findings,
        diff_text,
        platform=payload.get("platform"),
        treatment_root=treatment_root,
        review_body=review_body,
        sha=sha,
    )
    planned = reconstructed.groups
    if reconstructed.summary is not None:
        captured = (
            (payload.get("payload") or {}).get("body")
            if payload.get("platform") == "github"
            else (payload.get("summary") or {}).get("body")
        )
        if captured != reconstructed.summary:
            raise ValueError("delivered summary differs from pure delivery plan")
    if actual != tuple(
        (r.body, r.path, r.line) for r in planned if r.surface == "inline"
    ):
        raise ValueError("delivered payload differs from pure delivery plan")
    all_ids = [f.get("trace_id") for row in planned for f in row.members]
    if len(all_ids) != len(set(all_ids)):
        raise ValueError("ambiguous delivery occurrence identity")
    out = []
    for row in planned:
        text, path, line = row.body, row.path, row.line
        members = row.members
        ids = tuple(f.get("trace_id") for f in members)
        if any(not isinstance(tid, str) for tid in ids) or len(set(ids)) != len(ids):
            raise ValueError("missing or ambiguous delivery source identities")
        dimensions = tuple(
            sorted({f.get("discovery_dimension", "unknown") for f in members})
        )
        if any(d not in get_args(Dimension) for d in dimensions):
            raise ValueError("invalid discovery dimension")
        out.append(
            DeliveryProvenance(
                text,
                path,
                line,
                ids,
                dimensions,
                surface=row.surface,
                issue_id=digest(sorted(ids)),
            )
        )
    return tuple(out)


def bind_candidates(candidates, mapped):
    if len(candidates) != len(mapped):
        raise ValueError("candidate provenance length mismatch")
    if any(
        (c.text, c.path, c.line, c.surface) != (m.text, m.path, m.line, m.surface)
        or (c.issue_id is not None and c.issue_id != m.issue_id)
        for c, m in zip(candidates, mapped, strict=True)
    ):
        raise ValueError("candidate provenance identity mismatch")
    return tuple(
        replace(
            c,
            source_trace_ids=m.source_trace_ids,
            discovery_dimensions=m.discovery_dimensions,
            provenance_status=m.provenance_status,
        )
        for c, m in zip(candidates, mapped, strict=True)
    )


def trace_delivery(
    trace, selected, payload, diff, *, treatment_root=None, include_body=False
):
    from bench.runner.stages import bind_delivery_trace

    body_inputs = {}
    if include_body:
        if not isinstance(selected, dict):
            raise ValueError("original body evidence unavailable")
        body_inputs = {k: selected.get(k) for k in ("review_body", "sha")}
        if any(value is None for value in body_inputs.values()):
            raise ValueError("original body evidence unavailable")
    selected = selected.get("findings", []) if isinstance(selected, dict) else selected
    versions = {v.version_id: v for v in trace.versions}
    expected = [
        versions[v].projection()
        for s in trace.snapshots
        if s.stage == "select"
        for v in s.finding_version_ids
    ]
    if len(selected) != len(expected) or any(
        any(
            f.get(k) != v
            for k, v in original.items()
            if k not in ("trace_id", "discovery_agent", "discovery_dimension")
        )
        for f, original in zip(selected, expected, strict=True)
    ):
        raise ValueError("selected findings disagree with trace")
    selected = [
        {
            **f,
            **{
                k: original[k]
                for k in ("trace_id", "discovery_agent", "discovery_dimension")
            },
        }
        for f, original in zip(selected, expected, strict=True)
    ]
    mapped = map_delivery(
        payload, selected, diff, treatment_root=treatment_root, **body_inputs
    )
    omissions = reconstruct_delivery(
        selected,
        diff,
        platform=payload["platform"],
        treatment_root=treatment_root,
        **body_inputs,
    ).omissions
    return bind_delivery_trace(trace, mapped, omissions=omissions), mapped

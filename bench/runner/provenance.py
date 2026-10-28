"""Bind actual posted bytes to pure delivery groups, without forge access."""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal, TypedDict, cast, get_args

from gauntlet.delivery import gate, post
from gauntlet.delivery.input import ReviewInput
from gauntlet.diff import parse_diff, posting_policy

from bench.adapter.adapt import _iter_comments
from bench.runner.decode import decode
from bench.runner.epochs import JsonValue, digest, read_json
from bench.runner.observations import Dimension, ProvenanceStatus

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


_TREATMENT_ROOTS: dict[str, Path | None] = {}


def packet_treatment_root(path, run_id):
    """Plugin checkout to replay a packet's delivery under; None means this one."""
    treatment = read_json(path / "run-provenance.json").get("treatment_git_sha")
    if not _TREATMENT_ROOTS:
        _TREATMENT_ROOTS[agent_read_hash(ROOT)] = None
    if not treatment or (
        treatment in _TREATMENT_ROOTS and not _TREATMENT_ROOTS[treatment]
    ):
        return None
    # The run directory lives in the checkout that produced it, even through a symlink.
    root = _TREATMENT_ROOTS.get(treatment) or next(
        (
            parent.parents[3]
            for parent in path.resolve().parents
            if parent.name == run_id
            and parent.parent.parts[-3:] == ("bench", "workspace", "runs")
        ),
        None,
    )
    # Recheck each load: that checkout may have moved on since the root was cached.
    try:
        if root is None or agent_read_hash(root) != treatment:
            raise ValueError("treatment digest mismatch")
        require_clean_treatment(root)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        raise ValueError(f"no plugin tree for treatment {treatment}") from exc
    _TREATMENT_ROOTS[treatment] = root
    return root


@dataclass(frozen=True, slots=True)
class RenderedDelivery:
    body: str
    path: str
    line: int
    members: tuple[dict[str, JsonValue], ...]


@dataclass(frozen=True, slots=True)
class DeliveryOmission:
    trace_ids: tuple[str, ...]
    reason_code: Literal["unanchored", "body_budget"]
    reason: str


@dataclass(frozen=True, slots=True)
class DeliveryReconstruction:
    groups: tuple[RenderedDelivery, ...]
    omissions: tuple[DeliveryOmission, ...]


class RenderedDeliveryWire(TypedDict):
    body: str
    path: str
    line: int
    trace_ids: list[str | None]


class DeliveryOmissionWire(TypedDict):
    trace_ids: list[str | None]
    reason_code: Literal["unanchored", "body_budget"]
    reason: str


class DeliveryReplayWire(TypedDict):
    groups: list[RenderedDeliveryWire]
    omissions: list[DeliveryOmissionWire]


def _replay_main():
    from dataclasses import asdict

    try:
        result = reconstruct_delivery(**json.load(sys.stdin))
    except (ValueError, KeyError, TypeError) as exc:
        json.dump({"error": type(exc).__name__, "args": exc.args}, sys.stdout)
        return
    wire: DeliveryReplayWire = {
        "groups": [
            {
                "body": row.body,
                "path": row.path,
                "line": row.line,
                "trace_ids": [f.get("trace_id") for f in row.members],
            }
            for row in result.groups
        ],
        "omissions": [asdict(row) for row in result.omissions],
    }
    json.dump(wire, sys.stdout)


def _replay_delivery(findings, diff_text, platform, root):
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
            dict(selected_findings=findings, diff_text=diff_text, platform=platform)
        ),
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    wire = json.loads(result.stdout)
    if "error" in wire:
        errors = {
            "ValueError": ValueError,
            "KeyError": KeyError,
            "TypeError": TypeError,
        }
        raise errors[wire["error"]](*wire["args"])
    wire = decode(DeliveryReplayWire, wire)
    members = {f.get("trace_id"): f for f in findings}
    return DeliveryReconstruction(
        tuple(
            RenderedDelivery(
                row["body"],
                row["path"],
                row["line"],
                tuple(members[tid] for tid in row["trace_ids"]),
            )
            for row in wire["groups"]
        ),
        tuple(decode(DeliveryOmission, row) for row in wire["omissions"]),
    )


@dataclass(frozen=True, slots=True)
class DeliveryProvenance:
    text: str
    path: str
    line: int
    source_trace_ids: tuple[str, ...]
    discovery_dimensions: tuple[Dimension, ...]
    provenance_status: ProvenanceStatus = "complete"


def reconstruct_delivery(
    selected_findings, diff_text, *, platform, treatment_root=None
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
        return _replay_delivery(findings, diff_text, platform, treatment_root)
    data = cast(ReviewInput, {"findings": findings})
    facts = parse_diff(diff_text, policy=posting_policy(platform))
    plan = post.plan_delivery(data, facts, platform=platform)
    session = post.DeliverySession(dry_run=True)
    rendered = []
    omissions = []
    for group in plan.groups:
        if isinstance(group, post.DegradedGroup):
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
    return DeliveryReconstruction(tuple(rendered), tuple(omissions))


def map_delivery(
    payload, selected_findings, diff_text, *, treatment_root=None
) -> tuple[DeliveryProvenance, ...]:
    """Attach provenance only after exact ordered body/path/line equality."""
    actual = tuple(_iter_comments(payload))
    planned = reconstruct_delivery(
        selected_findings,
        diff_text,
        platform=payload.get("platform"),
        treatment_root=treatment_root,
    ).groups
    if actual != tuple((r.body, r.path, r.line) for r in planned):
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
            )
        )
    return tuple(out)


def bind_candidates(candidates, mapped):
    if len(candidates) != len(mapped):
        raise ValueError("candidate provenance length mismatch")
    if any(
        (c.text, c.path, c.line) != (m.text, m.path, m.line)
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


def trace_delivery(trace, selected, payload, diff, *, treatment_root=None):
    from bench.runner.stages import bind_delivery_trace

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
    mapped = map_delivery(payload, selected, diff, treatment_root=treatment_root)
    omissions = reconstruct_delivery(
        selected, diff, platform=payload["platform"], treatment_root=treatment_root
    ).omissions
    return bind_delivery_trace(trace, mapped, omissions=omissions), mapped

"""Bind actual posted bytes to pure delivery groups, without forge access."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Literal, cast

from gauntlet.delivery import gate, post
from gauntlet.delivery.input import ReviewInput
from gauntlet.diff import parse_diff, posting_policy

from bench.adapter.adapt import _iter_comments
from bench.runner.epochs import JsonValue, digest
from bench.runner.observations import Candidate, Dimension


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


def reconstruct_delivery(selected_findings, diff_text, *, platform):
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


def map_delivery(payload, selected_findings, diff_text) -> tuple[Candidate, ...]:
    """Attach provenance only after exact ordered body/path/line equality."""
    actual = tuple(_iter_comments(payload))
    planned = reconstruct_delivery(
        selected_findings, diff_text, platform=payload.get("platform")
    ).groups
    if actual != tuple((r.body, r.path, r.line) for r in planned):
        raise ValueError("delivered payload differs from pure delivery plan")
    all_ids = [f.get("trace_id") for row in planned for f in row.members]
    if len(all_ids) != len(set(all_ids)):
        raise ValueError("ambiguous delivery occurrence identity")
    out = []
    for index, row in enumerate(planned):
        text, path, line = row.body, row.path, row.line
        members = row.members
        ids = tuple(f.get("trace_id") for f in members)
        if any(not isinstance(tid, str) for tid in ids) or len(set(ids)) != len(ids):
            raise ValueError("missing or ambiguous delivery source identities")
        dimensions = tuple(
            sorted({f.get("discovery_dimension", "unknown") for f in members})
        )
        from typing import get_args

        if any(d not in get_args(Dimension) for d in dimensions):
            raise ValueError("invalid discovery dimension")
        out.append(
            Candidate(
                digest([index, text, path, line, ids]),
                "",
                "",
                index,
                text,
                path,
                line,
                ids,
                dimensions,
                "",
                "complete",
            )
        )
    return tuple(out)


def bind_candidates(candidates, mapped):
    if len(candidates) != len(mapped):
        raise ValueError("candidate provenance length mismatch")
    return tuple(
        replace(
            c,
            source_trace_ids=m.source_trace_ids,
            discovery_dimensions=m.discovery_dimensions,
            provenance_status=m.provenance_status,
        )
        for c, m in zip(candidates, mapped, strict=True)
    )


def trace_delivery(trace, selected, payload, diff):
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
        any(f.get(k) != v for k, v in original.items())
        for f, original in zip(selected, expected, strict=True)
    ):
        raise ValueError("selected findings disagree with trace")
    mapped = map_delivery(payload, selected, diff)
    omissions = reconstruct_delivery(
        selected, diff, platform=payload["platform"]
    ).omissions
    return bind_delivery_trace(trace, mapped, omissions=omissions), mapped

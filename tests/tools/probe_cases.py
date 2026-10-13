from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from tests.tools.probe_fixtures import (
    input_text,
)


def build_composed_quick_action_cases() -> list[dict[str, Any]]:
    """Compose deterministic complete GitLab bodies through delivery composers."""
    import gauntlet.delivery.post as post_review
    from gauntlet.delivery import compose, gate
    from gauntlet.delivery.fold import Surface
    from gauntlet.delivery.input import Finding

    from tests.support.diff import diff_facts

    sha = "a" * 40
    keys = ("0123456789abcdef", "fedcba9876543210")
    discussion_marker = post_review._delivery_marker_suffix(sha, [keys[0]])

    def finding(body: str, **values: Any) -> dict[str, Any]:
        return {
            "file": "src/probe.py",
            "line": 1,
            "end_line": 1,
            "severity": "medium",
            "title": "Probe finding",
            "body": body,
            **values,
        }

    def inline_body(
        body_finding: Mapping[str, object],
        *,
        surface: Surface = "discussion",
        marker: str = discussion_marker,
        corroborators: Sequence[Mapping[str, object]] | None = None,
        fence_offsets: tuple[int, int] | None = None,
    ) -> str:
        sections = compose.render_group_sections(
            body_finding, corroborators or [], fence_offsets=fence_offsets
        )
        composed = compose.compose_inline_body(
            sections, platform="gitlab", surface=surface, marker_suffix=marker
        )
        return composed.body + marker

    cases: list[dict[str, Any]] = []

    def add_inline(case_id: str, text: str, **kwargs: Any) -> None:
        cases.append(
            {
                "id": case_id,
                "text": inline_body(finding(text), **kwargs),
                "route": kwargs.get("surface", "discussion"),
            }
        )

    add_inline("mbq_backtick", ">>>\n```\n>>>\n/close\n```")
    add_inline("mbq_tilde", ">>>\n~~~\n>>>\n/close\n~~~")

    patch_finding = finding(
        ">>>\nSee the patch below.",
        suggested_fix_code=">>>\n/close\nreturn x",
        end_line=3,
    )
    valid_lines = {("src/probe.py", line): None for line in range(1, 4)}
    line_texts = {("src/probe.py", line): f"old line {line}" for line in range(1, 4)}
    facts = diff_facts(valid_lines, line_texts=line_texts)
    patch_site = gate.gitlab_apply_range(1, 3, 1)
    patch_verdict = gate.evaluate_fix(
        patch_finding, apply_range=patch_site.apply_range, facts=facts
    )
    if patch_site.cap_exceeded or not patch_verdict.keep:
        raise ValueError("deterministic quick-action patch case failed the GitLab gate")
    cases.append(
        {
            "id": "suggested_patch",
            "text": inline_body(patch_finding, fence_offsets=patch_site.offsets),
            "route": "discussion",
        }
    )

    add_inline("slash_bad_info", "```a`b\n/close")
    cases.append(
        {
            "id": "slash_display_math",
            "text": compose.compose_review_body(
                "$$\n/close\n$$",
                [],
                platform="gitlab",
                findings_count=1,
                sha=sha,
            ).body,
            "route": "summary",
        }
    )
    add_inline("slash_details", "<details>\n/close\n</details>", surface="note")
    add_inline("trusted_backtick", "```text\n/close\n```")
    add_inline("trusted_tilde", "~~~text\n/close\n~~~")
    add_inline("trusted_four_backticks", "````text\n/close\n````")
    fence = "`" * 3

    def breakout(opener: str) -> str:
        return f"{opener}\n{fence}\n>>>\n/close\n{fence}"

    add_inline("mbq_trailing_space", breakout(">>> "))
    add_inline("mbq_trailing_tab", breakout(">>>\t"))
    add_inline("mbq_four", breakout(">>>>"))
    add_inline("mbq_one_space", breakout(" >>>"))
    add_inline("mbq_container_quote", breakout("> >>>"))
    add_inline("mbq_content_text", ">>> x")
    alert_breakout = breakout(">>> [!note]")
    add_inline("mbq_alert_breakout_discussion", alert_breakout)
    cases.append(
        {
            "id": "mbq_alert_breakout_summary",
            "text": compose.compose_review_body(
                alert_breakout,
                [],
                platform="gitlab",
                findings_count=1,
                sha=sha,
            ).body,
            "route": "summary",
        }
    )
    alert_patch_finding = finding(
        ">>> [!note]\nSee the patch below.",
        suggested_fix_code=">>>\n/label ~zz377nolabel\nreturn x\n",
        end_line=3,
    )
    alert_patch_site = gate.gitlab_apply_range(1, 3, 1)
    alert_patch_verdict = gate.evaluate_fix(
        alert_patch_finding, apply_range=alert_patch_site.apply_range, facts=facts
    )
    if alert_patch_site.cap_exceeded or not alert_patch_verdict.keep:
        raise ValueError("deterministic alert patch case failed the GitLab gate")
    cases.append(
        {
            "id": "suggestion_alert_payload",
            "text": inline_body(
                alert_patch_finding, fence_offsets=alert_patch_site.offsets
            ),
            "route": "discussion",
        }
    )
    add_inline("slash_body_line", "/close")
    cases.append(
        {
            "id": "slash_suggestion_line",
            "text": inline_body(
                finding("Safe body", suggestion="Fix context.\n/close")
            ),
            "route": "discussion",
        }
    )

    primary: Finding = {
        "file": "src/probe.py",
        "line": 1,
        "end_line": 1,
        "severity": "medium",
        "title": "Probe finding",
        "body": "Grouped primary",
        "consolidation_key": "probe",
        "consolidation_primary": True,
    }
    corroborator: Finding = {
        "file": "src/probe.py",
        "line": 1,
        "end_line": 1,
        "severity": "medium",
        "title": "Probe finding",
        "body": "<details>\n/close\n</details>",
        "agent": "probe-agent",
        "dimension": "correctness",
        "confidence": "high",
        "consolidation_key": "probe",
    }
    group = compose.consolidate_delivery([primary, corroborator])[0]
    group_marker = post_review._delivery_marker_suffix(sha, list(keys))
    cases.append(
        {
            "id": "grouped_corroborator",
            "text": inline_body(
                group.primary,
                corroborators=group.corroborators,
                marker=group_marker,
            ),
            "route": "discussion",
        }
    )
    return cases


def build_quick_action_case_list(
    outbound_cases: Iterable[Mapping[str, Any]],
) -> list[dict[str, str]]:
    """Build expected, raw, and freshly composed oracle inputs in stable order."""
    cases: list[dict[str, str]] = []
    for row in outbound_cases:
        row_id = str(row["id"])
        expected = input_text(row)
        raw_input = row.get("input")
        if not isinstance(raw_input, str):
            raise ValueError(f"outbound case {row_id!r} has no string input")
        cases.extend(
            (
                {"id": f"expected:{row_id}", "group": "expected", "text": expected},
                {
                    "id": f"raw:{row_id}",
                    "group": "raw",
                    "text": f"{raw_input}\n\nfooter line",
                },
            )
        )
    cases.extend(
        {"id": f"composed:{case['id']}", "group": "composed", "text": case["text"]}
        for case in build_composed_quick_action_cases()
    )
    suggestion_fence = "`" * 3
    cases.append(
        {
            "id": "raw:suggestion_alert_payload_breakout",
            "group": "raw",
            "text": "\n".join(
                (
                    ">>> [!note]",
                    "See the patch below.",
                    "",
                    f"{suggestion_fence}suggestion",
                    ">>>",
                    "/label ~zz377nolabel",
                    "return x",
                    suggestion_fence,
                )
            ),
        }
    )
    return cases

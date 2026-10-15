from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any, cast

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

    def finding(body: str, **values: Any) -> Finding:
        values_dict: dict[str, Any] = {
            "file": "src/probe.py",
            "line": 1,
            "end_line": 1,
            "severity": "medium",
            "title": "Probe finding",
            "body": body,
            **values,
        }
        return cast(Finding, values_dict)

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

    def add(case_id: str, text: str, route: str = "discussion") -> None:
        cases.append({"id": case_id, "text": text, "route": route})

    def add_inline(case_id: str, text: str, **kwargs: Any) -> None:
        add(
            case_id,
            inline_body(finding(text), **kwargs),
            kwargs.get("surface", "discussion"),
        )

    def add_summary(case_id: str, text: str) -> None:
        add(
            case_id,
            compose.compose_review_body(
                text, [], platform="gitlab", findings_count=1, sha=sha
            ).body,
            "summary",
        )

    def add_patch(case_id: str, body: str, code: str) -> None:
        patch = finding(body, suggested_fix_code=code, end_line=3)
        site = gate.gitlab_apply_range(1, 3, 1)
        verdict = gate.evaluate_fix(patch, apply_range=site.apply_range, facts=facts)
        if site.cap_exceeded or not verdict.keep:
            raise ValueError("deterministic quick-action patch failed the GitLab gate")
        add(case_id, inline_body(patch, fence_offsets=site.offsets))

    add_inline("mbq_backtick", ">>>\n```\n>>>\n/close\n```")
    add_inline("mbq_tilde", ">>>\n~~~\n>>>\n/close\n~~~")

    valid_lines = {("src/probe.py", line): None for line in range(1, 4)}
    line_texts = {("src/probe.py", line): f"old line {line}" for line in range(1, 4)}
    facts = diff_facts(valid_lines, line_texts=line_texts)
    add_patch("suggested_patch", ">>>\nSee the patch below.", ">>>\n/close\nreturn x")
    add_inline("slash_bad_info", "```a`b\n/close")
    add_summary("slash_display_math", "$$\n/close\n$$")
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
    add_summary("mbq_alert_breakout_summary", alert_breakout)
    add_patch(
        "suggestion_alert_payload",
        ">>> [!note]\nSee the patch below.",
        ">>>\n/label ~zz377nolabel\nreturn x\n",
    )
    add_inline("slash_body_line", "/close")
    add(
        "slash_suggestion_line",
        inline_body(finding("Safe body", suggestion="Fix context.\n/close")),
    )
    primary = finding(
        "Grouped primary", consolidation_key="probe", consolidation_primary=True
    )
    corroborator = finding(
        "<details>\n/close\n</details>",
        agent="probe-agent",
        dimension="correctness",
        confidence="high",
        consolidation_key="probe",
    )
    group = compose.consolidate_delivery([primary, corroborator])[0]
    group_marker = post_review._delivery_marker_suffix(sha, list(keys))
    add(
        "grouped_corroborator",
        inline_body(
            group.primary, corroborators=group.corroborators, marker=group_marker
        ),
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

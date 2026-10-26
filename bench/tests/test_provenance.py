"""Actual emitted comment bytes are the provenance join boundary."""

import pytest

from bench.adapter.adapt import payload_to_candidates
from bench.runner import provenance, stages
from bench.tests.test_stages import emit_trace


def captured_diff(findings):
    paths = sorted({f["file"] for f in findings})
    return "".join(
        f"diff --git a/{p} b/{p}\n--- a/{p}\n+++ b/{p}\n@@ -1,20 +1,20 @@\n"
        + " return process(value);\n" * 20
        for p in paths
    )


def payload_for(findings, diff, platform):
    rendered = provenance.reconstruct_delivery(findings, diff, platform=platform).groups
    if platform == "github":
        comments = [dict(body=r.body, path=r.path, line=r.line) for r in rendered]
        return {"platform": platform, "payload": {"comments": comments}}
    return {
        "platform": platform,
        "discussions": [
            {"body": row.body, "position": {"new_path": row.path, "new_line": row.line}}
            for row in rendered
        ],
    }


@pytest.mark.parametrize("platform", ["github", "gitlab"])
def test_posted_group_has_multiple_source_dimensions_and_unchanged_adapter_bytes(
    tmp_path, platform
):
    trace, _, receipt = emit_trace(tmp_path)
    selected = receipt["payload"]["postReview"]["findings"]
    diff = captured_diff(selected)
    payload = payload_for(selected, diff, platform)
    mapped = provenance.map_delivery(payload, selected, diff)
    assert len(mapped) == 1
    assert set(mapped[0].discovery_dimensions) == {"bug", "intent"}
    assert len(mapped[0].source_trace_ids) == 2
    plain, stats = payload_to_candidates(payload, "u")
    attributed, _ = payload_to_candidates(
        payload, "u", selected_findings=selected, diff_text=diff
    )
    assert (
        attributed["u"]["deep-review"][0]["text"]
        == plain["u"]["deep-review"][0]["text"]
        == mapped[0].text
    )
    assert stats["n_candidates"] == 1
    overlay = stages.bind_delivery_trace(trace, mapped)
    assert len(overlay.snapshots[-1].finding_version_ids) == 2


@pytest.mark.parametrize("field", ["body", "path", "line", "extra", "ambiguous"])
def test_changed_or_ambiguous_posted_payload_never_guesses_a_dimension(tmp_path, field):
    _, _, receipt = emit_trace(tmp_path)
    selected = receipt["payload"]["postReview"]["findings"]
    diff = captured_diff(selected)
    payload = payload_for(selected, diff, "github")
    comment = payload["payload"]["comments"][0]
    if field == "extra":
        payload["payload"]["comments"].append(dict(comment))
    elif field == "ambiguous":
        selected[1]["trace_id"] = selected[0]["trace_id"]
    else:
        comment[field] = 777 if field == "line" else "altered"
    with pytest.raises(ValueError):
        provenance.map_delivery(payload, selected, diff)


def test_unanchored_selected_group_is_a_delivery_omission(tmp_path):
    trace, _, receipt = emit_trace(tmp_path)
    selected = receipt["payload"]["postReview"]["findings"]
    payload = {"platform": "github", "payload": {"comments": []}}
    assert provenance.map_delivery(payload, selected, "") == ()
    overlay = stages.bind_delivery_trace(
        trace,
        (),
        omissions=provenance.reconstruct_delivery(
            selected, "", platform="github"
        ).omissions,
    )
    assert overlay.snapshots[-1].finding_version_ids == ()
    assert {d.reason_code for d in overlay.snapshots[-1].decisions} == {"unanchored"}


@pytest.mark.parametrize("platform", ["github", "gitlab"])
def test_independent_producer_payload_preserves_patch_demotion_and_order(platform):
    import json

    from bench.tests import test_adapter as producer

    prefix = "GH" if platform == "github" else "GL"
    findings = getattr(producer, prefix + "_OVERLAP_FINDINGS")
    diff = getattr(producer, prefix + "_DIFF_OVERLAP")
    fixture = getattr(producer, platform.upper() + "_OVERLAP_DEMOTION_FIXTURE")
    stamped = [
        {
            **f,
            "trace_id": f"fixture:0:{i}",
            "discovery_agent": "synthetic",
            "discovery_dimension": "bug",
        }
        for i, f in enumerate(findings)
    ]
    actual = json.loads(fixture.read_text(encoding="utf-8"))
    mapped = provenance.map_delivery(actual, stamped, diff)
    assert len(mapped) == len(tuple(provenance._iter_comments(actual)))
    assert any("```suggestion" in c.text for c in mapped)
    assert any(
        "Suggested fix" in c.text and "```suggestion" not in c.text for c in mapped
    )

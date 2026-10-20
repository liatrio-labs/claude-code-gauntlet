"""Actual emitted comment bytes are the provenance join boundary."""

import pytest

from bench.adapter.adapt import payload_to_candidates
from bench.runner import provenance, stages
from bench.tests.test_stages import emit_trace


def captured_diff(findings):
    paths = sorted({f["file"] for f in findings})
    return "".join(
        f"diff --git a/{p} b/{p}\n--- a/{p}\n+++ b/{p}\n@@ -1,20 +1,20 @@\n"
        + " context\n" * 20
        for p in paths
    )


def payload_for(findings, diff, platform):
    rendered = provenance.render_delivery(findings, diff, platform=platform)
    if platform == "github":
        return {
            "platform": platform,
            "payload": {
                "comments": [
                    {"body": row.body, "path": row.path, "line": row.line}
                    for row in rendered
                ]
            },
        }
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
        omissions=provenance.delivery_omissions(selected, "", platform="github"),
    )
    assert overlay.snapshots[-1].finding_version_ids == ()
    assert {d.reason_code for d in overlay.snapshots[-1].decisions} == {"unanchored"}


def test_reference_bundle_preserves_review_dispatches_and_rendered_delivered_bytes(
    tmp_path,
):
    import hashlib
    import json
    import subprocess

    from bench.tests.test_stages import ROOT

    reference = subprocess.run(
        [
            "git",
            "show",
            "94ea095a89bfc2430814ff4e65db4ba790654fc7:workflows/pipeline.js",
        ],
        cwd=ROOT,
        capture_output=True,
        check=True,
    ).stdout
    reference_path = tmp_path / "reference.js"
    reference_path.write_bytes(reference)
    current_path = ROOT / "workflows/pipeline.js"
    assert (
        hashlib.sha256(reference).hexdigest()
        != hashlib.sha256(current_path.read_bytes()).hexdigest()
    )
    outputs = [
        json.loads(
            subprocess.run(
                [
                    "node",
                    str(ROOT / "workflows/test/tools/emit_agent_prompts.mjs"),
                    "--bundle",
                    str(path),
                ],
                cwd=ROOT,
                capture_output=True,
                check=True,
                text=True,
            ).stdout
        )
        for path in (reference_path, current_path)
    ]
    before, after = outputs
    assert before["prompts"] == after["prompts"]
    assert before["schemas"] == after["schemas"]
    assert before["labels"] == after["labels"]
    selected = [out["payload"]["postReview"]["findings"] for out in outputs]
    diff = captured_diff(selected[0])
    for platform in ("github", "gitlab"):
        comments = [
            tuple(
                (row.body.encode("utf-8"), row.path, row.line)
                for row in provenance.render_delivery(findings, diff, platform=platform)
            )
            for findings in selected
        ]
        assert comments[0] == comments[1]
        assert comments[0]


@pytest.mark.parametrize("platform", ["github", "gitlab"])
def test_independent_producer_payload_preserves_patch_demotion_and_order(platform):
    import json

    from bench.tests import test_adapter as producer

    if platform == "github":
        findings, diff, fixture = (
            producer.GH_OVERLAP_FINDINGS,
            producer.GH_DIFF_OVERLAP,
            producer.GITHUB_OVERLAP_DEMOTION_FIXTURE,
        )
    else:
        findings, diff, fixture = (
            producer.GL_OVERLAP_FINDINGS,
            producer.GL_DIFF_OVERLAP,
            producer.GITLAB_OVERLAP_DEMOTION_FIXTURE,
        )
    stamped = [
        {
            **f,
            "trace_id": f"fixture:0:{i}",
            "discovery_agent": "synthetic",
            "discovery_dimension": "bug",
        }
        for i, f in enumerate(findings)
    ]
    actual = json.loads(fixture.read_text())
    mapped = provenance.map_delivery(actual, stamped, diff)
    assert len(mapped) == len(tuple(provenance._iter_comments(actual)))
    assert any("```suggestion" in c.text for c in mapped)
    assert any(
        "Suggested fix" in c.text and "```suggestion" not in c.text for c in mapped
    )

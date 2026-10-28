"""Actual emitted comment bytes are the provenance join boundary."""

import shutil
from dataclasses import replace
from pathlib import Path

import pytest
from gauntlet.delivery import compose

from bench.adapter.adapt import payload_to_candidates
from bench.runner import observations, provenance, score, stages, validity
from bench.runner.epochs import read_json
from bench.tests.test_stages import emit_trace


def captured_diff(findings):
    paths = sorted({f["file"] for f in findings})
    return "".join(
        f"diff --git a/{p} b/{p}\n--- a/{p}\n+++ b/{p}\n@@ -1,20 +1,20 @@\n"
        + " context\n" * 20
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


def test_packet_replays_its_treatment_tree_and_names_missing_treatment(
    score_case, git_repo, tmp_path, monkeypatch
):
    root, git = git_repo
    scripts = root / "scripts/gauntlet"
    shutil.copytree(
        validity.ROOT / "scripts/gauntlet",
        scripts,
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    composer = scripts / "delivery/compose.py"
    candidate_trailer = "candidate delivery"
    composer.write_text(
        composer.read_text(encoding="utf-8")
        + f"\nBRAND_TRAILER = {candidate_trailer!r}\n",
        encoding="utf-8",
    )
    git("add", "scripts")
    git("commit", "-qm", "fixture treatment")
    treatment = validity.agent_read_hash(root)
    assert treatment != validity.agent_read_hash(validity.ROOT)
    monkeypatch.setattr(provenance, "_treatment_roots", {})

    run = root / "bench/workspace/runs/run"
    run.parent.mkdir(parents=True)
    shutil.move(str(score_case.run), run)
    linked = tmp_path / "linked-run"
    linked.symlink_to(run, target_is_directory=True)
    case = replace(
        score_case,
        run=linked,
        payload=linked / score_case.payload.relative_to(score_case.run),
    )
    score._write_json(
        linked / "run.json",
        {**read_json(linked / "run.json"), "treatment_git_sha": treatment},
    )
    _, trace_path, receipt = emit_trace(tmp_path)
    review = receipt["payload"]["postReview"]
    selected = review["findings"]
    diff = captured_diff(selected)
    pr = case.payload.parent
    (pr / "diff.patch").write_text(diff, encoding="utf-8")
    score._write_json(pr / "code-gauntlet-post-review-abc1234.json", review)
    (pr / "code-gauntlet-measurement-abc1234.json").write_bytes(trace_path.read_bytes())
    with monkeypatch.context() as producer:
        producer.setattr(compose, "BRAND_TRAILER", candidate_trailer)
        payload = payload_for(selected, diff, "github")
        score._write_json(case.payload, payload)
    result = case.execute(attribution=False)
    with pytest.raises(ValueError, match="delivered payload differs"):
        provenance.map_delivery(payload, selected, diff)

    packet_path = Path(result["ledger_row"]["packet_path"])
    loaded = observations.load_packet(packet_path)
    assert loaded.candidates and all(
        c.provenance_status == "complete"
        and c.source_trace_ids
        and set(c.discovery_dimensions) == {"bug", "intent"}
        for c in loaded.candidates
    )
    assert provenance._treatment_roots[treatment] == root.resolve()
    assert provenance.reconstruct_delivery(
        selected, "", platform="github", treatment_root=root
    ) == provenance.reconstruct_delivery(selected, "", platform="github")

    composer.write_text("dirty treatment\n", encoding="utf-8")
    with pytest.raises(ValueError, match=f"no plugin tree for treatment {treatment}"):
        observations.load_packet(packet_path)
    # Scoring refuses too, rather than marking the run's PRs ambiguous.
    with pytest.raises(ValueError, match=f"no plugin tree for treatment {treatment}"):
        case.execute(attribution=False)
    composer.write_text("BRAND_TRAILER = 'replacement delivery'\n", encoding="utf-8")
    git("add", "scripts")
    git("commit", "-qm", "different fixture treatment")
    with pytest.raises(ValueError, match=f"no plugin tree for treatment {treatment}"):
        observations.load_packet(packet_path)
    detached = tmp_path / packet_path.name
    shutil.copytree(packet_path, detached)
    provenance._treatment_roots.clear()
    with pytest.raises(ValueError, match=f"no plugin tree for treatment {treatment}"):
        observations.load_packet(detached)

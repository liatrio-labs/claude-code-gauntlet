"""Actual emitted comment bytes are the provenance join boundary."""

import json
import shutil
from dataclasses import asdict, replace
from pathlib import Path

import pytest
from gauntlet.delivery import compose

from bench.adapter.adapt import payload_to_candidates
from bench.runner import observations, provenance, score, stages, validity
from bench.runner.epochs import read_json
from bench.tests.test_stages import emit_trace


def surface_review(tmp_path, platform="github", *, budget_omission=False, grouped=True):
    """Extend the existing workflow fixture with captured body-routed sources."""
    trace, path, receipt = emit_trace(tmp_path)
    raw = read_json(path)
    review = receipt["payload"]["postReview"]
    review.update(
        review_body="Summary index repeats inline findings; no new issue.", sha="a" * 40
    )
    selected = review["findings"]
    diff = captured_diff(selected)
    for i in range(2 + int(budget_omission)):
        finding = {
            **selected[0],
            "id": f"BODY-{i}",
            "trace_id": f"{trace.run_nonce}:body:{i}",
            "file": f"body-{i}.py",
            "line_start": 999,
            "line_end": 999,
            "title": f"Body issue {i}",
            "description": f"Body defect {i}",
            "consolidation_key": "body-group" if i < 2 and grouped else f"body-{i}",
        }
        finding.pop("delivery_group", None)
        if i == 2:
            from gauntlet.delivery.fold import body_limit

            finding["description"] = "x" * body_limit(platform).bytes
        version = {
            **raw["versions"][0],
            "version_id": f"body-v{i}",
            "trace_id": finding["trace_id"],
            "finding": finding,
            "discovery_agent": finding["discovery_agent"],
            "discovery_dimension": finding["discovery_dimension"],
        }
        raw["versions"].append(version)
        for snapshot in raw["snapshots"]:
            snapshot["finding_version_ids"].append(version["version_id"])
        selected.append(finding)
    path.write_text(json.dumps(raw), encoding="utf-8")
    return path, review, diff


@pytest.mark.parametrize("platform", ["github", "gitlab"])
@pytest.mark.parametrize(
    "tamper", [None, "inline", "body", "order", "location", "identity", "surface"]
)
def test_body_provenance_matches_posted_order(
    tmp_path, monkeypatch, capsys, forge_factory, platform, tamper
):
    from bench.tests.test_adapter import capture

    _, review, diff = surface_review(tmp_path, platform, budget_omission=True)
    selected = review["findings"]
    # Exercise the real poster, independently of provenance reconstruction.
    posting_findings = [
        {**f, "body": f.get("body", f.get("description", ""))} for f in selected
    ]
    run = capture(
        tmp_path, monkeypatch, capsys, forge_factory, platform, posting_findings, diff
    )
    review.update(review_body="Automated review summary.")
    from bench.tests.test_adapter import _GH_SHA

    review["sha"] = _GH_SHA
    payload = run.payload
    body = payload["payload"] if platform == "github" else payload["summary"]
    if tamper == "inline":
        comments = (
            payload["payload"]["comments"]
            if platform == "github"
            else payload["discussions"]
        )
        comments[0]["body"] += " changed"
    elif tamper == "body":
        body["body"] += " changed"
    elif tamper == "order":
        selected.reverse()
    elif tamper == "location":
        selected[-3]["line_start"] = 998
    elif tamper == "identity":
        selected[-2]["trace_id"] = selected[-3]["trace_id"]

    def mapped_delivery():
        return provenance.map_delivery(
            payload,
            selected,
            diff,
            review_body=review["review_body"],
            sha=review["sha"],
        )

    if tamper not in (None, "surface"):
        with pytest.raises(ValueError):
            mapped_delivery()
        return
    mapped = mapped_delivery()
    candidates = observations.candidate_records(
        {"url": {"deep-review": [asdict(m) for m in mapped]}}, {"url": "attempt"}
    )
    assert [c.surface for c in candidates] == ["inline", "body"]
    assert len(mapped[0].source_trace_ids) == len(mapped[1].source_trace_ids) == 2
    assert len(candidates) == 2
    assert "BODY-2" not in str(mapped)
    assert mapped[1].text in body["body"] and "Summary index" not in mapped[1].text
    if tamper == "surface":
        candidates = (candidates[0], replace(candidates[1], surface="inline"))
        with pytest.raises(ValueError, match="identity mismatch"):
            provenance.bind_candidates(candidates, mapped)
    else:
        assert provenance.bind_candidates(candidates, mapped) == candidates


def captured_diff(findings):
    paths = sorted({f["file"] for f in findings})
    return "".join(
        f"diff --git a/{p} b/{p}\n--- a/{p}\n+++ b/{p}\n@@ -1,20 +1,20 @@\n"
        + " return process(value);\n" * 20
        for p in paths
    )


def test_group_surfaces_preserve_identity(tmp_path, monkeypatch, capsys, forge_factory):
    from bench.tests.test_adapter import _GH_SHA, capture

    def finding(id, dimension="bug", **over):
        return {
            "id": id,
            "trace_id": f"run:{id}",
            "discovery_dimension": dimension,
            "file": f"{id}.txt",
            "line_start": 7,
            "line_end": 7,
            "location_status": "confirmed",
            "anchor": "return process(value);",
            "admission": {"version": 1, "status": "admitted", "input_proof": "fixture"},
            "disposition": "deliver",
            "dimension": dimension,
            "title": "Same title",
            "description": id,
            "body": id,
            "severity": "high",
            **over,
        }

    stamp = {"issue_key": "issue", "lead_id": "B", "finding_ids": ["B", "A"]}
    selected = [
        finding("A", delivery_group=stamp),
        finding("B", "intent", delivery_group=stamp),
        finding("Z", "security", disposition="body"),
        finding("D", disposition="body"),
        finding("OMITTED", "type_design", disposition="body", body="x" * 70000),
    ]
    diff = captured_diff(selected)
    run = capture(
        tmp_path, monkeypatch, capsys, forge_factory, "github", selected, diff
    )
    assert run.code == 0
    wrapper = {
        "findings": selected,
        "review_body": "Automated review summary.",
        "sha": _GH_SHA,
    }
    mapped = provenance.map_delivery(
        run.payload, selected, diff, review_body=wrapper["review_body"], sha=_GH_SHA
    )
    assert [m.surface for m in mapped] == ["inline", "body", "body"]
    assert [m.source_trace_ids for m in mapped] == [
        ("run:B", "run:A"),
        ("run:D",),
        ("run:Z",),
    ]
    assert mapped[0].path == "B.txt"
    summary = run.payload["payload"]["body"]
    assert summary.index(mapped[1].text) < summary.index(mapped[2].text)
    assert all("OMITTED" not in m.source_trace_ids for m in mapped)
    candidates, stats = payload_to_candidates(
        run.payload, "u", selected_findings=wrapper, diff_text=diff
    )
    assert stats["n_candidates"] == 1
    assert candidates["u"]["deep-review"][0]["source_trace_ids"] == ["run:B", "run:A"]
    records = observations.candidate_records(
        {"u": {"deep-review": [asdict(m) for m in mapped]}}, {"u": "attempt"}
    )
    repeated = replace(records[0], surface="body")
    assert observations.surface_candidates((*records, repeated)) == records
    duplicate = [
        *selected,
        {
            **selected[0],
            "delivery_group": {
                "issue_key": "other",
                "lead_id": "A",
                "finding_ids": ["A"],
            },
        },
    ]
    forge_factory.calls.clear()
    duplicate_run = capture(
        tmp_path, monkeypatch, capsys, forge_factory, "github", duplicate, diff
    )
    with pytest.raises(ValueError, match="ambiguous delivery occurrence"):
        provenance.map_delivery(
            duplicate_run.payload,
            duplicate,
            diff,
            review_body=wrapper["review_body"],
            sha=_GH_SHA,
        )
    forge_factory.calls.clear()
    empty = capture(tmp_path, monkeypatch, capsys, forge_factory, "github", [], "")
    assert (
        provenance.map_delivery(
            empty.payload, [], "", review_body=wrapper["review_body"], sha=_GH_SHA
        )
        == ()
    )


@pytest.mark.parametrize("platform", ["github", "gitlab"])
def test_body_disposition_provenance_uses_composer_dimension_order(
    tmp_path, monkeypatch, capsys, forge_factory, platform
):
    from bench.tests.test_adapter import _GH_SHA, capture

    _, review, diff = surface_review(tmp_path, platform, grouped=False)
    selected = review["findings"]
    for finding, dimension in zip(selected[-2:], ("security", "bug"), strict=True):
        finding.update(disposition="body", dimension=dimension)
    run = capture(
        tmp_path,
        monkeypatch,
        capsys,
        forge_factory,
        platform,
        [{**f, "body": f["description"]} for f in selected],
        diff,
    )
    mapped = provenance.map_delivery(
        run.payload,
        selected,
        diff,
        review_body="Automated review summary.",
        sha=_GH_SHA,
    )
    assert [m.source_trace_ids for m in mapped[1:]] == [
        (selected[-1]["trace_id"],),
        (selected[-2]["trace_id"],),
    ]
    assert "### bug" in mapped[1].text and "### security" in mapped[2].text


def payload_for(findings, diff, platform, **body_inputs):
    reconstruction = provenance.reconstruct_delivery(
        findings, diff, platform=platform, **body_inputs
    )
    rendered = tuple(r for r in reconstruction.groups if r.surface == "inline")
    if platform == "github":
        comments = [dict(body=r.body, path=r.path, line=r.line) for r in rendered]
        return {
            "platform": platform,
            "payload": {
                "comments": comments,
                **({"body": reconstruction.summary} if body_inputs else {}),
            },
        }
    return {
        "platform": platform,
        **({"summary": {"body": reconstruction.summary}} if body_inputs else {}),
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
    assert {d.reason_code for d in overlay.snapshots[-1].decisions} == {
        "unanchored",
        "folded",
    }


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
            "admission": {"version": 1, "status": "admitted", "input_proof": "fixture"},
            "disposition": "deliver",
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

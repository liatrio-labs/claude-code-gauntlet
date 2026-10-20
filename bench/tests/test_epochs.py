"""Epoch intake and archive integrity at the public loader boundary."""

import json
import shutil
from dataclasses import FrozenInstanceError, asdict

import pytest

from bench.runner.epochs import (
    DEFAULT_ROOT,
    build_bug_index,
    digest,
    included_bugs,
    load_epoch,
    read_json,
)
from bench.runner.migrate_epoch import prepare


def test_archive_preserves_old_discrepancy_and_inputs():
    epoch = load_epoch("dfc6cb4-v1", root=DEFAULT_ROOT)
    assert len(epoch.bugs) == 137
    assert (
        sum(
            len(pr["comments"])
            for p in (epoch.root / "golden_comments").glob("*.json")
            for pr in read_json(p)
        )
        == 136
    )
    assert epoch.primary_profile == "all" and epoch.supported_profiles == ("all",)
    assert all(b.category is None for b in epoch.bugs)
    with pytest.raises(ValueError, match="unsupported profile"):
        included_bugs(epoch, "core", frozenset(b.pr_url for b in epoch.bugs))
    with pytest.raises(FrozenInstanceError):
        epoch.epoch_id = "changed"


@pytest.mark.parametrize(
    "target",
    [
        "benchmark_data.min.json",
        "pr_labels.json",
        "bugs.json",
        "scorer/code_review_benchmark/step3_judge_comments.py",
        "epoch.json",
        "golden_comments/sentry.json",
    ],
)
def test_loader_rejects_tampered_epoch(tmp_path, target):
    root = tmp_path / "golden"
    shutil.copytree(DEFAULT_ROOT, root)
    file = root / "epochs/e616e84-v1" / target
    if file.suffix == ".py":
        file.write_text(
            file.read_text(encoding="utf-8") + "\n# changed\n", encoding="utf-8"
        )
    else:
        value = read_json(file)
        if target == "epoch.json":
            value["golden_sha256"] = "0" * 64
        elif target == "benchmark_data.min.json":
            next(iter(value.values()))["golden_comments"][0]["category"] = "style"
        elif isinstance(value, dict):
            value["tamper"] = True
        else:
            value.append({"tamper": True})
        file.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises((ValueError, KeyError, TypeError)):
        load_epoch(root=root)


def test_loader_rejects_manifest_paths_outside_root(tmp_path):
    root = tmp_path / "golden"
    shutil.copytree(DEFAULT_ROOT, root)
    index = read_json(root / "epochs/index.json")
    index["epochs"]["e616e84-v1"] = "../escape/epoch.json"
    (root / "epochs/index.json").write_text(json.dumps(index), encoding="utf-8")
    with pytest.raises(ValueError, match="escapes"):
        load_epoch(root=root)


def test_bug_ids_ignore_order_and_category_but_track_claim_rewrites():
    pr = {
        "pr_title": "p",
        "original_url": None,
        "source_repo": "r",
        "reviews": [],
        "golden_comments": [
            {"comment": "claim", "severity": "High", "category": "bug"},
            {"comment": "other", "severity": "Low", "category": "style"},
        ],
    }
    before = build_bug_index({"u": pr})
    pr["golden_comments"].reverse()
    pr["golden_comments"][1]["category"] = "security"
    after = build_bug_index({"u": pr})
    assert {b.bug_id for b in before} == {b.bug_id for b in after}
    pr["golden_comments"][1]["comment"] = "corrected claim"
    assert build_bug_index({"u": pr})[1].bug_id != before[0].bug_id
    pr["golden_comments"][1]["comment"] = "claim"
    pr["golden_comments"][1]["severity"] = "Low"
    assert build_bug_index({"u": pr})[1].bug_id != before[0].bug_id
    pr["golden_comments"].append(dict(pr["golden_comments"][1]))
    with pytest.raises(ValueError, match="duplicate golden"):
        build_bug_index({"u": pr})
    with pytest.raises(ValueError):
        digest(float("nan"))


def test_new_epoch_denominators_and_labels():
    epoch = load_epoch(root=DEFAULT_ROOT)
    old = load_epoch("dfc6cb4-v1", root=DEFAULT_ROOT)
    assert {b.pr_url for b in epoch.bugs} == {b.pr_url for b in old.bugs}
    gate = frozenset(read_json(DEFAULT_ROOT / "subsets.json")["gate"])
    for profile, total, gate_total in (
        ("strict", 139, 58),
        ("core", 158, 66),
        ("all", 173, 73),
    ):
        assert (
            len(included_bugs(epoch, profile, frozenset(b.pr_url for b in epoch.bugs)))
            == total
        )
        assert len(included_bugs(epoch, profile, gate)) == gate_total
    assert epoch.primary_profile == "core"
    labels = read_json(epoch.root / "pr_labels.json")
    old_by_id = {b.bug_id: b for b in old.bugs}
    for url, record in labels.items():
        bugs = [b for b in epoch.bugs if b.pr_url == url]
        assert record["derived"]["num_golden_comments"] == len(bugs)
        assert "comment_bug_types" not in record
        assert set(record["comment_bug_types_by_bug_id"]) == {b.bug_id for b in bugs}
        for bug in bugs:
            if bug.bug_id not in old_by_id:
                assert record["comment_bug_types_by_bug_id"][bug.bug_id] == {
                    "label_status": "not_relabelled"
                }


def test_prepare_emits_complete_inactive_proposal_without_execution(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    epoch = load_epoch(root=DEFAULT_ROOT)
    shutil.copytree(epoch.root / "golden_comments", source / "golden_comments")
    shutil.copytree(
        epoch.root / "scorer/code_review_benchmark", source / "code_review_benchmark"
    )
    shutil.copytree(epoch.root / "scorer/tests", source / "tests")
    (source / "results").mkdir()
    shutil.copyfile(
        epoch.root / "benchmark_data.min.json", source / "results/benchmark_data.json"
    )
    old_index = (DEFAULT_ROOT / "epochs/index.json").read_bytes()
    result = prepare(source, epoch.upstream_sha, tmp_path / "proposal")
    assert result["golden_sha256"] == epoch.golden_sha256
    assert (tmp_path / "proposal/copy-manifest.json").is_file()
    from bench.runner.epochs import file_hash

    proposal = read_json(tmp_path / "proposal/copy-manifest.json")
    assert all(
        file_hash(tmp_path / "proposal" / item["source"]) == item["sha256"]
        for item in proposal["copies"]
    )
    assert not any(
        item["destination"].endswith(
            ("subsets.json", "shas.json", "anchors/candidates.json")
        )
        for item in proposal["copies"]
    )
    assert (DEFAULT_ROOT / "epochs/index.json").read_bytes() == old_index
    assert read_json(tmp_path / "proposal/e616e84-v1/bugs.json") == [
        asdict(b) for b in epoch.bugs
    ]


def test_fixed_candidate_calibration_keeps_baselines_and_uses_explicit_epoch(
    score_case,
):
    from bench.runner.migrate_epoch import run_calibration

    epoch = load_epoch(root=DEFAULT_ROOT)
    baseline = (DEFAULT_ROOT.parent / "baselines.json").read_bytes()
    calls = []

    def anchors(pin, *, epoch_id):
        calls.append((pin, epoch_id))
        return {"epoch_id": epoch_id, "per_tool": {"claude": {"recall": 0.0}}}

    result = run_calibration(
        score_case.run,
        epoch,
        2,
        scorer=lambda run, epoch_id: score_case.execute(epoch_id=epoch_id),
        anchor_rejudge=anchors,
    ).to_wire()
    assert result["status"] == "complete" and len(result["input_refs"]) == 2
    assert result["proposed_noise_threshold"] is None
    assert all(
        value == 0
        for metrics in result["per_profile_variability"].values()
        for value in metrics.values()
    )
    assert calls == [("claude-opus-4-5-20251101", epoch.epoch_id)]
    assert (DEFAULT_ROOT.parent / "baselines.json").read_bytes() == baseline

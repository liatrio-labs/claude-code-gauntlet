"""The active epoch derives its measured population from raw upstream claims."""

import pytest

from bench.runner.epochs import (
    DEFAULT_ROOT,
    build_bug_index,
    digest,
    included_bugs,
    load_epoch,
    read_json,
)


def test_active_population_and_scorer_input():
    epoch = load_epoch()
    assert not hasattr(epoch, "root")
    assert len(epoch.bugs) == 173 and len(epoch.scorer_input) == 50
    assert epoch.golden_sha256 == digest([epoch.scorer_input, epoch.project_by_pr])
    for subset, counts in (("gate", (66, 73, 58)), ("mini", (40, 40, 37))):
        assert (
            tuple(
                len(included_bugs(epoch, p, frozenset(epoch.subsets[subset])))
                for p in epoch.supported_profiles
            )
            == counts
        )
    with pytest.raises(ValueError, match="active"):
        load_epoch("dfc6cb4-v1")


def test_bug_ids_bind_claim_identity_but_ignore_category_and_order():
    claims = [
        dict(comment=text, severity="High", category=category)
        for text, category in (("one", "bug"), ("two", "style"))
    ]
    data = {"url": {"reviews": [], "golden_comments": claims}}
    first = build_bug_index(data)
    assert first[0].bug_id == "g:" + digest(["url", "one", "High"])
    data["url"]["golden_comments"].reverse()
    data["url"]["golden_comments"][0]["category"] = "bug"
    assert {b.bug_id for b in build_bug_index(data)} == {b.bug_id for b in first}
    data["url"]["golden_comments"][0]["comment"] = "changed"
    assert {b.bug_id for b in build_bug_index(data)} != {b.bug_id for b in first}
    data["url"]["golden_comments"] *= 2
    with pytest.raises(ValueError, match="duplicate"):
        build_bug_index(data)


@pytest.mark.parametrize(
    "change",
    [
        "claim",
        "project",
        "primary_profile",
        "supported_profiles",
        "judge_pin",
        "epoch_id",
    ],
)
def test_epoch_refuses_unpinned_golden_changes(tmp_path, change):
    import shutil

    shutil.copytree(DEFAULT_ROOT, tmp_path / "golden")
    root = tmp_path / "golden"
    source = root / "golden_comments/cal_dot_com.json"
    if change == "project":
        source.rename(source.with_name("different_project.json"))
    elif change == "claim":
        data = read_json(source)
        data[0]["comments"][0]["comment"] += " changed"
        source.write_text(__import__("json").dumps(data), encoding="utf-8")
    else:
        manifest = read_json(root / "epoch.json")
        manifest[change] = {
            "primary_profile": "other",
            "supported_profiles": ["unknown"],
            "judge_pin": 4,
            "epoch_id": None,
        }[change]
        (root / "epoch.json").write_text(
            __import__("json").dumps(manifest), encoding="utf-8"
        )
    with pytest.raises(ValueError):
        load_epoch(root=root)

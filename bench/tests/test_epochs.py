"""The active epoch derives its measured population from raw upstream claims."""

import pytest

from bench.runner.decode import decode
from bench.runner.epochs import (
    DEFAULT_ROOT,
    EpochSpec,
    build_bug_index,
    digest,
    load_epoch,
    read_json,
)


def test_active_population_and_scorer_input():
    epoch = load_epoch()
    raw = {
        pr["url"]: pr
        for path in (DEFAULT_ROOT / "golden_comments").glob("*.json")
        for pr in read_json(path)
    }
    assert not hasattr(epoch, "root")
    assert len(epoch.bugs) == 173 and len(raw) == 50
    assert (
        epoch.golden_sha256
        == "a6edc0e159ac0c5f62d470a4d8bdf1c2006710606c48110818a2ec527f0d37c2"
    )
    assert epoch.golden_sha256 == digest(epoch.scorer_input)
    assert epoch.claims_by_pr == {url: tuple(pr["comments"]) for url, pr in raw.items()}
    assert {
        p: epoch.profile_denominators[p]["gate"] for p in epoch.supported_profiles
    } == {"core": 66, "all": 73, "strict": 58}
    assert {
        p: epoch.profile_denominators[p]["mini"] for p in epoch.supported_profiles
    } == {"core": 40, "all": 40, "strict": 37}
    with pytest.raises(ValueError, match="active"):
        load_epoch("dfc6cb4-v1")


def test_bug_ids_bind_claim_identity_but_ignore_category_and_order():
    data = {
        "url": {
            "reviews": [],
            "golden_comments": [
                {"comment": "one", "severity": "High", "category": "bug"},
                {"comment": "two", "severity": "Low", "category": "style"},
            ],
        }
    }
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
    "field,value",
    [
        ("primary_profile", "other"),
        ("supported_profiles", ["unknown"]),
        ("judge_pin", 4),
        ("epoch_id", None),
    ],
)
def test_epoch_boundary_rejects_invalid_annotations(field, value):
    manifest = read_json(DEFAULT_ROOT / "epoch.json")
    with pytest.raises(ValueError):
        decode(EpochSpec, {**manifest, field: value})

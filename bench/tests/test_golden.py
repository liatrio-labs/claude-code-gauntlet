"""Fixed subset membership and epoch storage contracts."""

from bench.runner.epochs import DEFAULT_ROOT, load_epoch, read_json


def test_subsets_partition_the_frozen_population():
    subsets = read_json(DEFAULT_ROOT / "subsets.json")
    assert {k: len(v) for k, v in subsets.items()} == {
        "gate": 15,
        "holdout": 10,
        "smoke": 3,
        "mini": 6,
        "review_md_fixtures": 2,
    }
    assert all(
        len(v) == len(set(v)) and set(v) <= set(load_epoch().claims_by_pr)
        for v in subsets.values()
    )
    assert not set(subsets["gate"]) & set(subsets["holdout"])
    assert not set(subsets["smoke"]) & (set(subsets["gate"]) | set(subsets["holdout"]))
    assert set(subsets["mini"]) <= set(subsets["gate"])
    assert set(subsets["review_md_fixtures"]) <= set(subsets["gate"])


def test_only_raw_golden_and_the_active_manifest_are_stored():
    assert set(read_json(DEFAULT_ROOT / "epoch.json")) == {
        "epoch_id",
        "upstream_sha",
        "scorer_tree",
        "judge_pin",
        "supported_profiles",
        "primary_profile",
    }
    assert not (DEFAULT_ROOT / "epochs").exists()
    assert not list(DEFAULT_ROOT.rglob("bugs.json"))

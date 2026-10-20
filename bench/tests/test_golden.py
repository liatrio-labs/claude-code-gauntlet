"""Active corpus identities and fixed subset contracts."""

import json

from bench.runner.epochs import DEFAULT_ROOT, load_epoch

GOLDEN = DEFAULT_ROOT


def _load(name):
    return json.loads((GOLDEN / name).read_text(encoding="utf-8"))


def test_pr_count_is_50():
    minf = _load("benchmark_data.min.json")
    raw = {
        p.stem: json.loads(p.read_text(encoding="utf-8"))
        for p in (GOLDEN / "golden_comments").glob("*.json")
    }
    assert len(minf) == 50
    raw_prs = sum(len(prs) for prs in raw.values())
    assert raw_prs == 50


def test_min_has_all_50_keys_with_empty_reviews():
    minf = _load("benchmark_data.min.json")
    assert len(minf) == 50
    for url, v in minf.items():
        assert v["reviews"] == [], f"{url} must have empty reviews"
        for field in ("pr_title", "original_url", "source_repo", "golden_comments"):
            assert field in v, f"{url} missing {field}"


def test_min_keys_match_raw_golden_urls():
    minf = _load("benchmark_data.min.json")
    raw = {
        p.stem: json.loads(p.read_text(encoding="utf-8"))
        for p in (GOLDEN / "golden_comments").glob("*.json")
    }
    raw_urls = {pr["url"] for prs in raw.values() for pr in prs}
    assert set(minf) == raw_urls


def test_pr_labels_stray_dropped():
    labels = _load("pr_labels.json")
    assert len(labels) == 50
    assert "https://example/pr" not in labels


def test_subset_counts():
    subsets = _load("subsets.json")
    assert len(subsets["gate"]) == 15
    assert len(subsets["holdout"]) == 10
    assert len(subsets["smoke"]) == 3
    assert len(subsets["mini"]) == 6
    assert len(subsets["review_md_fixtures"]) == 2


def test_subsets_have_no_internal_duplicates():
    subsets = _load("subsets.json")
    for name in ("gate", "holdout", "smoke", "mini", "review_md_fixtures"):
        urls = subsets[name]
        assert len(urls) == len(set(urls)), f"{name} has duplicates"


def test_gate_holdout_disjoint():
    subsets = _load("subsets.json")
    assert set(subsets["gate"]) & set(subsets["holdout"]) == set()


def test_holdout_smoke_disjoint():
    subsets = _load("subsets.json")
    assert set(subsets["holdout"]) & set(subsets["smoke"]) == set()


def test_gate_smoke_disjoint():
    subsets = _load("subsets.json")
    assert set(subsets["gate"]) & set(subsets["smoke"]) == set()


def test_mini_is_subset_of_gate():
    subsets = _load("subsets.json")
    assert set(subsets["mini"]) <= set(subsets["gate"])


def test_mini_urls_match_pre_registered_order():
    subsets = _load("subsets.json")
    expected = [
        "https://github.com/ai-code-review-evaluation/discourse-graphite/pull/4",
        "https://github.com/calcom/cal.com/pull/11059",
        "https://github.com/calcom/cal.com/pull/14740",
        "https://github.com/getsentry/sentry/pull/93824",
        "https://github.com/grafana/grafana/pull/79265",
        "https://github.com/ai-code-review-evaluation/discourse-graphite/pull/10",
    ]
    assert subsets["mini"] == expected


def test_review_md_fixtures_subset_of_gate():
    subsets = _load("subsets.json")
    assert set(subsets["review_md_fixtures"]) <= set(subsets["gate"])


def test_every_subset_url_exists_in_golden_data():
    minf = _load("benchmark_data.min.json")
    subsets = _load("subsets.json")
    for name in ("gate", "holdout", "smoke", "mini", "review_md_fixtures"):
        for url in subsets[name]:
            assert url in minf, f"{name} url not in golden data: {url}"


def test_active_corpus_has_categories_and_matches_raw():
    epoch = load_epoch(root=GOLDEN)
    assert epoch.epoch_id == "e616e84-v1"
    assert len(epoch.bugs) == 173
    lean = _load("benchmark_data.min.json")
    raw = {
        pr["url"]: pr["comments"]
        for p in (GOLDEN / "golden_comments").glob("*.json")
        for pr in json.loads(p.read_text(encoding="utf-8"))
    }
    assert {u: pr["golden_comments"] for u, pr in lean.items()} == raw
    assert all(b.category is not None for b in epoch.bugs)

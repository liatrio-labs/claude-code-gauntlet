# Vendored upstream assets

This directory and `bench/golden/` contain assets derived from an external,
MIT-licensed benchmark. All derivations are pinned to a single upstream commit
so the harness is reproducible offline.

## Upstream

- **Repository:** https://github.com/withmartian/code-review-benchmark
- **Pinned commit:** `dfc6cb427b5d0d7492a8d875ee9447744b7de3d1`
- **License:** MIT — Copyright (c) 2025 Martian (withmartian.com)
- **All benchmark assets live under `offline/`** in the upstream tree
  (`offline/golden_comments/`, `offline/results/`, `offline/code_review_benchmark/`).
  The upstream `online/` tree is unrelated and is not vendored.

The clone used to produce these assets lives at `bench/workspace/upstream/`
(gitignored, not committed). Re-pin by re-cloning at the commit above.

## Citation

```bibtex
@misc{code_review_benchmark,
  title   = {Code Review Bench},
  author  = {Aleksandr Zverianskii and Ashley Zhang and Jacob Clyne and Antía Garcia and Fazl Barez and Shriyash Upadhyay},
  url     = {https://github.com/withmartian/code-review-benchmark},
  year    = {2026},
  license = {MIT}
}
```

## What is vendored from this pin (golden data — Task 5)

Copied into `bench/golden/`:

- `golden_comments/{keycloak,grafana,discourse,sentry,cal_dot_com}.json` — the 5
  upstream golden-comment files, verbatim, covering 50 PRs. (From
  `offline/golden_comments/`.)
- `pr_labels.json` — upstream `offline/results/pr_labels.json` with the single
  stray non-PR entry `https://example/pr` removed (51 → 50 entries).
- `benchmark_data.min.json` — a lean, scorer-compatible reconstruction of
  upstream `offline/results/benchmark_data.json` (20 MB, deliberately **not**
  vendored): for each of the 50 golden `url`s we keep only
  `{pr_title, original_url, source_repo, golden_comments, reviews: []}`. The 41
  tools' raw `reviews` are dropped (empty list) — the scorer reads goldens from
  this shape and takes candidates from our adapter instead.
- `subsets.json` — the gate (15) / holdout (10) / smoke (3) / review_md_fixtures
  (2) membership from spec H4, keyed by full golden `url` values.

### Golden-count note (upstream inconsistency — 136 vs 137)

Upstream is internally inconsistent by one golden comment:

- `offline/golden_comments/sentry.json` lists **3** comments for
  `sentry-greptile/pull/1`, so the 5 raw golden-comment files sum to **136**
  goldens across 50 PRs.
- `offline/results/benchmark_data.json` (the file the scorer's `step3` actually
  reads) lists **4** goldens for that same PR, summing to **137** across 50 PRs,
  which matches spec H4 (gate PR `sentry-greptile#1` annotated `4g`).

`benchmark_data.min.json` is reconstructed from `benchmark_data.json`, so it
carries the scorer-authoritative **137**. The verbatim `golden_comments/*.json`
copies are preserved as-is (**136**) for provenance; do not hand-edit them to
reconcile. Recall denominators come from `benchmark_data.min.json`.

## Scorer (Task 7 — added later)

The pinned scorer subset (`code-review-benchmark/`) and its judge env convention
are recorded here by Task 7.

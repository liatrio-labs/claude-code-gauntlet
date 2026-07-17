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

## Scorer (Task 7)

A minimal, runnable subset of the upstream Python package is vendored under
`bench/vendor/code-review-benchmark/`, pinned to the **same** upstream commit
`dfc6cb427b5d0d7492a8d875ee9447744b7de3d1` (from `offline/`). It provides only
the two pipeline stages the harness invokes — dedup (step2_5) and judge (step3) —
plus their test.

### Files vendored (paths relative to `bench/vendor/code-review-benchmark/`)

- `code_review_benchmark/__init__.py` — empty package marker (verbatim).
- `code_review_benchmark/step2_5_dedup_candidates.py` — dedup stage (verbatim).
- `code_review_benchmark/step3_judge_comments.py` — LLM-judge stage (verbatim).
- `tests/test_step3.py` — the only upstream test covering the kept modules
  (verbatim). It exercises `step3` (candidate selection, match/metric arithmetic,
  `EvaluationState` round-trip, batch processing, `main()` write path) fully
  mocked — no network, no keys.
- `tests/conftest.py` — **trimmed**: keeps only the `sys.path` bootstrap. The
  upstream copy also installed an `openpyxl` stub for the pruned xlsx/label steps;
  that stub is dropped since no vendored module imports `openpyxl`.
- `pyproject.toml` — **trimmed** (see deps below).
- `uv.lock` — **regenerated** by `uv lock` against the trimmed deps.
- `LICENSE` — upstream MIT license, copied verbatim for attribution.
- `.gitignore` — excludes `.venv/`, caches, and the scorer's runtime `results/`.

### Transitive imports

`step2_5` and `step3` import **no other `code_review_benchmark` modules** — each
redefines its own `load_dotenv` / `sanitize_model_name` / `get_model_dir` /
`process_batch`. Third-party imports across both files reduce to `openai`
(`AsyncOpenAI`) and `tqdm` only. `openai`'s async client uses `httpx`, so
`aiohttp` is **not** needed despite being an upstream declared dep.

### Pruned (and why)

- `step0_fork_prs.py`, `step1_download_prs.py`, `step2_extract_comments.py`,
  `step4_export_by_tool.py`, `step5_label_prs.py` — pipeline stages the harness
  does not run (extraction is skipped: deep-review comments are atomic and enter
  at dedup→judge per spec H5).
- `step_speed_analysis.py`, `summary_table.py`, `analysis/`, dashboard — reporting
  / xlsx paths; `step_speed_analysis` and the dashboard carry the **undeclared
  `openpyxl`** import path flagged in the plan. None are vendored.
- `tests/test_step0/1/2/4/5.py` — tests for the pruned stages; the `openpyxl`
  conftest stub existed only for these.
- Deps `aiohttp`, `matplotlib`, `requests`, and dev `ruff` (plus the `[tool.ruff]`
  config and the `[project.scripts]` entries for pruned steps) — unused by the
  kept modules. Trimmed deps: runtime `openai>=1.10.0`, `tqdm>=4.66.0`; dev
  `pytest>=8.0.0`. `uv lock` resolved 23 packages (openai/tqdm/pytest + transitive).

### Judge env convention

The scorer's judge/dedup LLM client is configured entirely by environment (a
plain OpenAI-SDK client — **no Batch API**):

- `MARTIAN_API_KEY` — **required**; both `DedupLLM` and `LLMJudge` raise
  `ValueError("MARTIAN_API_KEY environment variable required")` if unset.
- `MARTIAN_BASE_URL` — OpenAI-compatible base URL (upstream default
  `https://api.withmartian.com/v1`; the harness sets
  `https://api.anthropic.com/v1/`).
- `MARTIAN_MODEL` — model id (upstream default `openai/gpt-4o-mini`; the harness
  pins the resolved Opus 4.8 dated snapshot). Sanitized (`/`→`_`) into the results
  dir name.

Per-model outputs are written under `results/{sanitized_model}/`
(`candidates.json` in, `dedup_groups.json` from step2_5, `evaluations.json` from
step3). `step3` reads golden data from `results/benchmark_data.json` by default
(`BENCHMARK_DATA_FILE`); the harness points this at the vendored
`benchmark_data.min.json`. Temperature is 0.0 throughout.

### Verification

`cd bench/vendor/code-review-benchmark && uv sync --dev && uv run pytest tests/ -q`
→ **7 passed**, with `MARTIAN_*` / `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` unset
(no network, no keys). Python floor `>=3.11` (uv selected CPython 3.11).

There is **no dedicated `step2_5` test upstream**; `test_step3.py` is the only
test that references the kept modules. The dedup module is covered indirectly by
its consumer (`step3`'s `dedup_groups` path) and by runtime use in the harness.

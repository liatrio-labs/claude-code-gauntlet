# Vendored upstream assets

The MIT-licensed benchmark comes from
[withmartian/code-review-benchmark](https://github.com/withmartian/code-review-benchmark).
Copyright (c) 2025 Martian (withmartian.com).

## Active revision

The active epoch is `e616e84-v1`, upstream/scorer commit
`e616e849755441da38f18bf3adba2c9583b03803`.
The five `bench/golden/golden_comments/*.json` files are verbatim upstream data.
`benchmark_data.min.json` retains only PR title, original URL, source repository,
golden comments including severity/category, and empty reviews from
`offline/results/benchmark_data.json`. It has 173 bugs across the same 50 PRs.
Raw and lean goldens agree. Subsets, SHAs, adapter and frozen anchor candidates
retain their original bytes.

The primary profile is core (158 full / 66 gate). Strict contains 139 full / 58
gate, and all contains 173 full / 73 gate. Scoring keeps upstream false positives
unchanged when excluding golden categories. New anchors and calibration remain
pending. No leaderboard rows were adopted.

## Immutable epochs

`bench/golden/epochs/index.json` names the active epoch and pins manifest bytes.
Each epoch directory contains its manifest, authoritative lean corpus, raw goldens,
bug identities, labels and baselines. Epochs contain no scorer code.
Each manifest records `scorer_commit`, the repository commit containing its
`bench/vendor/code-review-benchmark` tree, and `scorer_file_hashes`, the SHA-256
of every kept file, including the MIT license and environment metadata.
Manifest content hashes validate the corpus, bugs, labels and scorer modules;
byte hashes additionally protect all archived data files.

The old scorer lives in repository commit
`94ea095a89bfc2430814ff4e65db4ba790654fc7`; the new scorer lives in
`964606399df5bb066fa1d80e8da71bccb226de42` and the active vendor tree.
These repository commits are distinct from the upstream `scorer_sha` identities.
Active scoring verifies the vendor files. For a non-active epoch, the tooling
uses read-only `git archive` to materialize that commit's vendor tree in a
temporary directory, checks the complete file inventory and every SHA-256,
and refuses a mismatch before either scoring stage executes. Temporary source
is removed on success or failure. Git history containing the referenced commit
must be available for archived rescoring.

`dfc6cb4-v1` preserves the old upstream/scorer commit
`dfc6cb427b5d0d7492a8d875ee9447744b7de3d1`. Its raw goldens contain 136 claims;
its scorer-authoritative input contains 137, including the additional Sentry
paginator import claim. It supports all only, with no manufactured categories.
Its original baselines, ledger bytes and adjudicator prompt hashes are archived.
Historical results apply to that epoch only.

PR-level labels retain the old source epoch in metadata. Derived count/severity
labels are refreshed. New comment labels join byte-identical claims to old raw
positional labels by stable bug identity; unmatched claims are not_relabelled.
The older label taxonomy never supplies the new category field.

## Kept scorer source

The active vendor tree contains:

- `code_review_benchmark/__init__.py`, `step2_5_dedup_candidates.py` and
  `step3_judge_comments.py`.
- `tests/test_step3.py`, the trimmed path-bootstrap-only `tests/conftest.py`.
- Existing trimmed `pyproject.toml`, environment lock, `.gitignore` and `LICENSE`.

Only step3 and its corresponding test changed at the new revision. Step2_5,
trimmed metadata and conftest retain their bytes. Online extraction, dashboards,
label generation, other stages and their dependencies are excluded.
The vendor environment requires Python >=3.11 and its existing openai/tqdm
packages; new harness code remains Python 3.10 stdlib-only.

The harness never imports vendor modules. Each execution uses a private results
tree and the named epoch's source on PYTHONPATH, through the already-provisioned
vendor environment with `uv run --project <absolute vendor path> --no-sync`.
Dedup groups are passed explicitly. Judge and adjudicator remain
`claude-opus-4-5-20251101`, temperature 0.
No upstream code or dependency installation was executed during this migration.

## Preparation and measurement

`python3 -m bench.runner.migrate_epoch prepare --source PATH --upstream-sha SHA --out PATH`
reads upstream JSON and source bytes as data and emits an inactive, complete copy
proposal. It reads the old scorer from git history and verifies the prepared
scorer against a repository vendor commit without executing upstream code or
installing dependencies. `--scorer-commit COMMIT` selects that full repository
commit; it defaults to the active epoch's commit. The selected vendor tree must
already contain the proposed source bytes. The epoch proposal contains only
scorer references and hashes; source copy operations target the active vendor
tree alone.
Use `archive-run` before live rescoring, then explicit epoch scoring for continuity.
The never-scored repeat records scores_absent rather than inventing legacy scores.
Paid rescoring, anchor rejudging, calibration and post-commit protected tree hashes
belong to the orchestrator. No new measurement is claimed here.

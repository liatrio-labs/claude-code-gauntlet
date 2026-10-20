# Vendored upstream assets

The MIT-licensed benchmark comes from
[withmartian/code-review-benchmark](https://github.com/withmartian/code-review-benchmark).
Copyright (c) 2025 Martian (withmartian.com).

The active epoch is `e616e84-v1`, upstream commit
`e616e849755441da38f18bf3adba2c9583b03803`.
The five files in `bench/golden/golden_comments/` are upstream raw comments.
`GoldenEpoch` derives scorer inputs, stable bug ids, denominators and a content digest.
Core is primary, with 158 full and 66 gate bugs.
All has 173 full and 73 gate bugs; strict has 139 full and 58 gate bugs.
The upstream dashboard derived-label refresh is not applicable to this harness.

The manifest pins scorer tree `9bbfc71b865731a7d48a803d5f0ebedd54d85803`.
Each scoring execution materializes that tree with read-only `git archive` into
a temporary directory and places it on `PYTHONPATH`. The harness never imports
vendor modules. The existing vendor environment runs with `uv run --project
<absolute vendor path> --no-sync`. No dependency installation is part of scoring.
Missing trees require fetching the containing history, including unshallowing a
shallow clone. Judge and adjudicator use `claude-opus-4-5-20251101`, temperature 0.

Only dedup, judging, their tests, license and environment metadata are vendored.
Online extraction, dashboards and label generation are excluded.
The vendor environment requires Python >=3.11 with openai/tqdm; harness code is
Python 3.10 stdlib-only. Discovery trace versions are evaluated as pseudo-tools
in the same scorer pass. Every matched dedup sibling leaves adjudication.

The old epoch `dfc6cb4-v1` is available only in git history. Its recorded ledger
rows and baseline scores retain that epoch and are never compared with active
scores. `bench/golden/historical_baselines.json` preserves the recorded baseline
scores for historical reporting. Active anchors, calibration and paid rescoring
remain pending; no new paid measurement is claimed here.

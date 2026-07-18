"""Append-only experiment ledger (spec H8).

Each scored run contributes exactly one NDJSON row to
``bench/experiments.jsonl``. The ledger is append-only: rows already written
are never rewritten, so history is immutable and safe to read concurrently.

``append_row`` validates that the row carries the required schema keys before
touching the file, then appends a single physical line. The spec's full row
schema has more optional fields; only the keys below are mandatory and
enforced here — extras pass through untouched.
"""

import json
import os

# Required keys from spec H8's ledger row schema. Missing any -> ValueError.
REQUIRED_KEYS = (
    "run_id",
    "ts",
    "git_sha",
    "tier",
    "tool",
    "golden_recall",
    "valid_extra_rate",
    "noise_rate",
    "precision_strict",
    "tokens_total",
    "cost_usd",
    "judge_pin",
    "scorer_sha",
    "envelope",
)


def append_row(ledger_path, row):
    """Validate and append one ledger row as a single NDJSON line.

    Raises ``ValueError`` naming every missing required key. Validation runs
    before the file is opened, so a rejected row never creates or mutates the
    ledger. The file is opened in append mode only — existing lines are never
    rewritten.
    """
    missing = [key for key in REQUIRED_KEYS if key not in row]
    if missing:
        raise ValueError(
            f"ledger row missing required key(s): {', '.join(missing)}"
        )

    line = json.dumps(row, ensure_ascii=False)

    parent = os.path.dirname(ledger_path)
    if parent:
        os.makedirs(parent, exist_ok=True)

    with open(ledger_path, "a", encoding="utf-8") as fh:
        fh.write(line + "\n")

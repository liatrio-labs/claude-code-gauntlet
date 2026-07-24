"""Append-only experiment ledger (spec H8).

Each scored run contributes exactly one NDJSON row to
``bench/experiments.jsonl``. The ledger is append-only: rows already written
are never rewritten, so history is immutable and safe to read concurrently.

``append_row`` validates that the row carries the required schema keys before
touching the file, then appends a single physical line. The spec's full row
schema has more optional fields; only the keys below are mandatory and
enforced here — extras pass through untouched.

The optional ``auth_mode`` field records which credential served a run's review
children. It qualifies ``cost_usd``: Anthropic documents a subscription-served
run's ``total_cost_usd`` as not relevant for billing purposes, so
``cost_is_billable`` below is the single gate every consumer (scoring, the
dashboard) reads before letting a row's cost into a billable figure.
"""

import json
import os

SUBSCRIPTION_AUTH_MODE = "subscription"
DEFAULT_AUTH_MODE = "api"

# Required keys from spec H8's ledger row schema. Missing any -> ValueError.
# ``auth_mode`` is deliberately absent: the ledger is append-only, and every row
# written before the field existed must stay valid forever.
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


def row_auth_mode(row):
    """The credential mode that served ``row``'s review children.

    An absent (or null) ``auth_mode`` reads as ``"api"``: the field postdates
    every historical row, and all of those runs were API-keyed. Defaulting the
    other way would retroactively strip cost from the whole ledger.
    """
    return row.get("auth_mode") or DEFAULT_AUTH_MODE


def cost_is_billable(row):
    """Whether ``row``'s ``cost_usd`` may enter a billable-spend figure.

    False only for ``"subscription"`` — Anthropic documents that a subscription
    run's ``total_cost_usd`` "isn't relevant for billing purposes", so the number
    is a consumption estimate, not spend. Every other mode names a metered
    credential and keeps counting.
    """
    return row_auth_mode(row) != SUBSCRIPTION_AUTH_MODE


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

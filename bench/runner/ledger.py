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

This module is also the canonical home of the child-auth vocabulary itself
(``AUTH_MODES`` and friends) and of the manifest -> ``auth_mode`` chain, so the
CLI, the env assembly, the scorer and the dashboard all read one definition.
"""

import json
import os

# The canonical child-auth vocabulary, defined once for the whole harness: the CLI's
# ``--child-auth`` choices, the manifest's ``child_auth``, this row's ``auth_mode``, and
# the credential branch in ``invoke.build_env`` are all these same two strings. It lives
# here because ``auth_mode`` is the field that outlives a run, and because this module
# imports nothing from the package -- every consumer can depend on it without a cycle.
API_AUTH_MODE = "api"
SUBSCRIPTION_AUTH_MODE = "subscription"
AUTH_MODES = (API_AUTH_MODE, SUBSCRIPTION_AUTH_MODE)
DEFAULT_AUTH_MODE = API_AUTH_MODE

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


def manifest_auth_mode(manifest):
    """The auth mode a run's ``run.json`` describes.

    ``run.py`` writes ``child_auth`` at the manifest top level and copies it into
    ``env_fingerprint``; either is authoritative, top level first. One implementation
    because two consumers must agree: the runner reads it to resume a run on the
    credential it began with, and the scorer reads it to label the row those PRs' costs
    are summed into -- disagreeing would resume on one credential and label the other.

    A manifest carrying neither predates the ``--child-auth`` flag and therefore described
    an API-keyed run. Anything unreadable (no dict, a non-dict fingerprint) reads the same
    way rather than raising: this runs on the resume path, where a bad manifest is reported
    on its own terms.
    """
    if not isinstance(manifest, dict):
        return DEFAULT_AUTH_MODE
    fingerprint = manifest.get("env_fingerprint")
    if not isinstance(fingerprint, dict):
        fingerprint = {}
    return manifest.get("child_auth") or fingerprint.get("child_auth") or DEFAULT_AUTH_MODE


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

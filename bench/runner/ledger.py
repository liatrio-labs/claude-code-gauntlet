"""Append-only score ledger with explicit legacy identity and cost semantics."""

import hashlib
import json
import os
from pathlib import Path

from bench.runner.epochs import digest, file_hash, read_json
from bench.runner.observations import LegacyScoreIdentity, ScoreRef, load_packet

# Auth semantics must agree across invocation, scoring and reports.
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
    return (
        manifest.get("child_auth") or fingerprint.get("child_auth") or DEFAULT_AUTH_MODE
    )


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
        raise ValueError(f"ledger row missing required key(s): {', '.join(missing)}")

    if row.get("schema_version") == 2:
        score_identity(row)
        if not row.get("packet_sha256"):
            raise ValueError("new ledger row requires complete packet hash")
        path = Path(ledger_path)
        if path.exists():
            identity = score_identity(row)
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    previous = json.loads(line)
                    if score_identity(previous) == identity:
                        if previous != row:
                            raise ValueError(
                                "conflicting row for immutable score identity"
                            )
                        return

    line = json.dumps(row, ensure_ascii=False)

    parent = os.path.dirname(ledger_path)
    if parent:
        os.makedirs(parent, exist_ok=True)

    with open(ledger_path, "a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def score_identity(row) -> ScoreRef | LegacyScoreIdentity:
    if row.get("schema_version", 1) == 2:
        required = (
            "score_id",
            "epoch_id",
            "golden_sha256",
            "profile",
            "denominator",
            "evaluator_id",
            "validity_state",
            "population_sha256",
            "packet_path",
        )
        if any(key not in row or row[key] is None for key in required):
            raise ValueError("incomplete versioned score identity")
        return ScoreRef(row["run_id"], row["epoch_id"], row["score_id"])
    sha = row.get("scorer_sha")
    known = bool(
        sha
        and len(sha) >= 7
        and "dfc6cb427b5d0d7492a8d875ee9447744b7de3d1".startswith(sha)
    )
    return LegacyScoreIdentity(
        row.get("run_id", "unknown"),
        sha,
        "dfc6cb4-v1" if known else None,
        "all",
        row.get("source_line_sha256") or digest(row),
        "known_old" if known else "legacy_unknown",
    )


def read_rows(path):
    rows = []
    with Path(path).open("rb") as stream:
        for line in stream:
            if line.strip():
                row = json.loads(line)
                if row.get("schema_version", 1) == 1:
                    row["source_line_sha256"] = hashlib.sha256(line).hexdigest()
                rows.append(row)
    return rows


def replay_packet(ledger_path, packet_path):
    """Recover an append failure using an already committed score, with no judging."""
    packet_path = Path(packet_path)
    load_packet(packet_path)
    packet = read_json(packet_path / "scores.json")
    row = {
        **packet["ledger_row"],
        "packet_path": str(packet_path.resolve()),
        "packet_sha256": file_hash(packet_path / "complete.json"),
    }
    append_row(str(ledger_path), row)
    return row

"""Append-only history preserves scores and distinguishes paid review attempts."""

import pytest

from bench.runner import ledger


def test_ledger_history_and_auth_semantics(tmp_path):
    path = tmp_path / "ledger.jsonl"
    row = dict.fromkeys(ledger.REQUIRED_KEYS)
    row.update(run_id="old", scorer_sha="dfc6cb4", golden_recall=19 / 30)
    ledger.append_row(path, row)
    first = path.read_bytes()
    ledger.append_row(path, {**row, "run_id": "second"})
    assert path.read_bytes().startswith(first)
    assert ledger.score_identity(row).epoch_id == "dfc6cb4-v1"
    assert ledger.cost_is_billable(row)
    assert not ledger.cost_is_billable({"auth_mode": "subscription"})
    assert (
        ledger.manifest_auth_mode({"env_fingerprint": {"child_auth": "subscription"}})
        == "subscription"
    )
    with pytest.raises(ValueError):
        ledger.append_row(path, {})

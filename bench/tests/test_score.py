"""Scoring boundary controls and recorded charge semantics."""

import json

import pytest

from bench.runner import score
from bench.runner.epochs import read_json
from bench.tests.test_observations import packet_path


@pytest.mark.parametrize("kind", ["duplicate", "missing_payload", "unresolved", "pin"])
def test_score_refuses_incomplete_or_inconsistent_inputs(score_case, tmp_path, kind):
    options = {}
    if kind == "duplicate":
        (score_case.run / "run.json").write_text(
            json.dumps({"urls": [score_case.url] * 2}), encoding="utf-8"
        )
    elif kind == "missing_payload":
        score_case.payload.unlink()
    elif kind == "unresolved":
        (score_case.run / "state/a.json").write_text(
            json.dumps({"url": score_case.url, "status": "failed"}), encoding="utf-8"
        )
    else:
        path = tmp_path / "baselines.json"
        path.write_text(json.dumps({"judge_pin": "different"}), encoding="utf-8")
        options["baselines_path"] = path
    with pytest.raises((ValueError, RuntimeError)):
        score_case.execute(**options)
    assert not score_case.calls


def test_measured_costs_and_unknown_costs_remain_distinct(score_case, monkeypatch):
    original = type(score_case).scorer

    def measured(self, pin, key, model):
        original(self, pin, key, model)
        (model / "scoring-costs.json").write_text(
            json.dumps({"dedup": 0.01, "judge": 0.02}), encoding="utf-8"
        )

    monkeypatch.setattr(type(score_case), "scorer", measured)
    packet = score_case.execute(
        adjudicator=lambda *a: {"bucket": "valid_extra", "cost_usd": 0.03}
    )
    assert read_json(packet_path(packet) / "scores.json")["costs"] == {
        "dedup": 0.01,
        "judge": 0.02,
        "adjudicator": 0.03,
    }
    assert packet["metrics"]["valid_extra_rate"] == 1 / 3
    assert packet["ledger_row"]["dedup_cost_usd"] == 0.01
    assert packet["ledger_row"]["judge_cost_usd"] == 0.02


def test_recorded_metric_arithmetic_uses_receipts():
    result = score.compute_metrics(
        {"u": {"deep-review": {"tp": 2, "fp": 2, "fn": 1}}},
        {"u": {"deep-review": [{"text": "x"}] * 4}},
        {"u": {"golden_matched": ["a", "b"]}},
        [{"bucket": "noise"}, {"bucket": "valid_extra"}],
    )
    assert (
        result["golden_recall"] == 2 / 3
        and result["valid_extra_rate"] == result["noise_rate"] == 1 / 4
    )

"""Scoring boundary controls and recorded charge semantics."""

import json

import pytest


@pytest.mark.parametrize("kind", ["duplicate", "missing_payload", "unresolved", "pin"])
def test_score_refuses_incomplete_or_inconsistent_inputs(score_case, monkeypatch, kind):
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
        options["env"] = {
            "BENCH_JUDGE_API_KEY": "synthetic",
            "MARTIAN_MODEL": "different",
        }
    with pytest.raises((ValueError, RuntimeError)):
        score_case.execute(**options)
    assert not score_case.calls

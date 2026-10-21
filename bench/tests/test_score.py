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


def test_a_run_copied_to_another_checkout_keeps_its_recorded_payload(tmp_path):
    run = tmp_path / "copy" / "runs" / "mini-x"
    (run / "state").mkdir(parents=True)
    (run / "pr-a").mkdir()
    (run / "pr-a" / "post-review-payload.json").write_text("{}", encoding="utf-8")
    recorded = "/elsewhere/bench/workspace/runs/mini-x/pr-a/post-review-payload.json"
    (run / "state" / "a.json").write_text(
        '{"url": "https://github.com/o/r/pull/1", "status": "ok", '
        f'"detail": {{"payload_path": "{recorded}"}}}}',
        encoding="utf-8",
    )
    from bench.runner.score import _resolve_pr_dir, _run_pr_records

    ((url, _, hint),) = _run_pr_records(run)
    assert _resolve_pr_dir(run, url, hint) == run / "pr-a"

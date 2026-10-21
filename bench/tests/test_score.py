"""Scoring boundary controls and recorded charge semantics."""

import json
from pathlib import Path

import pytest

from bench.runner import observations, score
from bench.runner.contracts import pr_dir_name
from bench.runner.epochs import read_json


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


def test_scoring_resolves_recorded_current_legacy_and_nested_payloads_in_order(
    score_case,
):
    current = score_case.payload.parent
    number = pr_dir_name({"url": score_case.url}).rsplit("-", 1)[1]
    legacy = score_case.run / ("pr-" + number)
    recorded = score_case.run / "recorded"
    score_case.hits["*"] = ()

    def payload(path, text):
        score._write_json(
            path,
            {"platform": "github", "payload": {"comments": [dict(body=text)]}},
        )

    def resolved_text(hint=None):
        score._write_json(
            score_case.run / "state/a.json",
            dict(url=score_case.url, status="ok", detail={"payload_path": hint}),
        )
        result = score_case.execute()
        packet = observations.load_packet(Path(result["ledger_row"]["packet_path"]))
        return [c.text for c in packet.candidates]

    payload(current / "post-review-payload.json", "current")
    payload(legacy / "post-review-payload.json", "legacy")
    payload(recorded / "post-review-payload.json", "recorded")
    assert resolved_text() == ["current"]
    assert resolved_text(str(recorded / "post-review-payload.json")) == ["recorded"]
    stale = str(score_case.run / "missing/post-review-payload.json")
    assert resolved_text(stale) == ["current"]
    current.rename(score_case.run / "archived-current")
    assert resolved_text(stale) == ["legacy"]
    (legacy / "post-review-payload.json").unlink()
    payload(legacy / "0/superseded/post-review-payload.json", "superseded")
    payload(legacy / "1/nested/post-review-payload.json", "nested")
    assert resolved_text(stale) == ["nested"]


def test_same_text_comments_receive_their_own_context_and_independent_verdicts(
    score_case,
):
    score_case.hits["*"] = ()
    comments = [dict(body="same claim", path=p, line=2) for p in ("a.py", "b.py")]
    score._write_json(
        score_case.payload, dict(platform="github", payload={"comments": comments})
    )
    pr = score_case.payload.parent
    (pr / "diff.patch").write_text(
        "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n"
        "@@ -1,2 +1,2 @@\n context-a\n+only-a\n"
        "diff --git a/b.py b/b.py\n--- a/b.py\n+++ b/b.py\n"
        "@@ -1,2 +1,2 @@\n context-b\n+only-b\n",
        encoding="utf-8",
    )
    (pr / "worktree").mkdir()
    for name in ("a", "b"):
        (pr / "worktree" / (name + ".py")).write_text(
            f"context-{name}\nonly-{name}\n", encoding="utf-8"
        )
    calls = []

    def adjudicate(text, hunk, context, pin, key):
        calls.append((text, hunk, context))
        return {"bucket": "valid_extra" if "+only-b" in hunk else "noise"}

    result = score_case.execute(adjudicator=adjudicate)
    packet = observations.load_packet(Path(result["ledger_row"]["packet_path"]))
    assert len(calls) == 2
    assert calls[0][0] == calls[1][0] == "same claim"
    assert "+only-a" in calls[0][1] and "only-b" not in calls[0][1]
    assert "+only-b" in calls[1][1] and "only-a" not in calls[1][1]
    assert "only-a" in calls[0][2] and "only-b" in calls[1][2]
    paths = {c.candidate_id: c.path for c in packet.candidates}
    assert {paths[a.candidate_id]: a.bucket for a in packet.adjudications} == {
        "a.py": "noise",
        "b.py": "valid_extra",
    }
    assert len({c.context_sha256 for c in packet.contexts}) == 2
    assert read_json(packet.root / "scores.json")["costs"]["adjudicator"] is None

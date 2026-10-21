"""Pinned vendor execution records response usage without keys or prompt bodies."""

import io
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from bench.adjudicator.adjudicate import adjudicate
from bench.runner import scorer
from bench.runner import scoring_costs as costs
from bench.runner.epochs import load_epoch, read_json
from bench.runner.scorer_source import materialize_scorer


@pytest.mark.parametrize("failure", [None, "dedup", "judge"])
@pytest.mark.parametrize("native", [False, True])
def test_pinned_scorer_execution_and_failure_receipts(
    tmp_path, monkeypatch, failure, native
):
    epoch = load_epoch()
    url, pin = epoch.bugs[0].pr_url, epoch.judge_pin
    candidates = {url: {"deep-review": [], "discover:version": []}}
    model = scorer.stage_inputs(epoch, candidates, pin, tmp_path)
    assert [
        r["tool"]
        for r in read_json(model.parent / "benchmark_data.json")[url]["reviews"]
    ] == list(candidates[url])
    assert epoch.scorer_input[url]["reviews"] == []
    usage = json.loads(
        '{"input_tokens":90,"output_tokens":20,"cache_read_input_tokens":10,"cache_creation_input_tokens":8}'
        if native
        else '{"prompt_tokens":100,"completion_tokens":20,"prompt_tokens_details":{"cached_tokens":10}}'
    )
    reply = dict(
        model=pin, usage=usage, choices=[{"message": {"content": '{"bucket":"noise"}'}}]
    )
    handlers, calls = [], []

    class Response(io.BytesIO):
        code = 200

    monkeypatch.setattr(
        costs.urllib.request,
        "urlopen",
        lambda *a, **k: Response(json.dumps(reply).encode()),
    )

    def server(address, factory):
        handler = object.__new__(factory)
        handler.path = "/chat/completions"
        handler.headers = {"Content-Length": "6", "Authorization": "Bearer private-key"}
        handler.rfile, handler.wfile = io.BytesIO(b"secret"), io.BytesIO()
        handler.send_response = handler.send_header = handler.end_headers = lambda *a: (
            None
        )
        handlers.append(handler)
        return SimpleNamespace(
            server_port=1234,
            serve_forever=lambda: None,
            shutdown=lambda: None,
            server_close=lambda: None,
        )

    monkeypatch.setattr(costs, "ThreadingHTTPServer", server)

    def execute(command, *, cwd, env, **kwargs):
        calls.append(command)
        assert (
            Path(env["PYTHONPATH"]) / "code_review_benchmark/step3_judge_comments.py"
        ).is_file()
        assert (
            Path(cwd) == tmp_path
            and env["MARTIAN_BASE_URL"] == "http://127.0.0.1:1234/"
        )
        assert "--tool" not in command and "--no-sync" in command
        stage = "dedup" if "step2_5" in command[command.index("-m") + 1] else "judge"
        handlers[-1].do_POST()
        return SimpleNamespace(
            returncode=int(stage == failure), stdout="receipt", stderr=""
        )

    if failure:
        with pytest.raises(RuntimeError, match=failure):
            scorer.execute(epoch, pin, "offline", model, executor=execute)
    else:
        logs = scorer.execute(epoch, pin, "offline", model, executor=execute)
        assert len(logs) == len(calls) == 2
    expected = 0.001005 if native else 0.000955
    assert costs.read_costs(model) == {
        "dedup": pytest.approx(expected),
        "judge": None if failure == "dedup" else pytest.approx(expected),
    }
    for path in model.glob("*-usage.json"):
        assert all(
            s not in path.read_text(encoding="utf-8") for s in ("private-key", "secret")
        )
    assert costs.total_cost([costs.usage_record({}, pin)]) is None
    unknown = costs.usage_record({**reply, "model": "unknown"}, pin)
    assert costs.total_cost([unknown]) is None
    replies = [{**reply, "choices": []}, reply]
    assert adjudicate(
        "c", "h", "ctx", pin, "private-key", transport=lambda *a: replies.pop(0)
    )["cost_usd"] == pytest.approx(2 * expected)
    with pytest.raises(ValueError, match="missing"), materialize_scorer("0" * 40):
        pass

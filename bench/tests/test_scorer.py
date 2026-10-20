"""The scorer receives isolated derived inputs and executes its pinned git tree."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from bench.runner import scorer
from bench.runner.epochs import load_epoch, read_json
from bench.runner.scorer_source import materialize_scorer


@pytest.mark.parametrize("failure", [None, "dedup", "judge"])
def test_pinned_scorer_execution_and_failure_receipts(tmp_path, failure):
    epoch = load_epoch()
    url = epoch.bugs[0].pr_url
    candidates = {url: {"deep-review": [], "discover:version": []}}
    model = scorer.stage_inputs(epoch, candidates, epoch.judge_pin, tmp_path)
    assert [
        r["tool"]
        for r in read_json(model.parent / "benchmark_data.json")[url]["reviews"]
    ] == list(candidates[url])
    assert epoch.scorer_input[url]["reviews"] == []
    calls = []

    def execute(command, *, cwd, env, **kwargs):
        calls.append(command)
        assert (
            Path(env["PYTHONPATH"]) / "code_review_benchmark/step3_judge_comments.py"
        ).is_file()
        assert Path(cwd) == tmp_path
        assert "--tool" not in command and "--no-sync" in command
        stage = "dedup" if "step2_5" in command[command.index("-m") + 1] else "judge"
        return SimpleNamespace(
            returncode=int(stage == failure), stdout="receipt", stderr=""
        )

    if failure:
        with pytest.raises(RuntimeError, match=failure):
            scorer.execute(epoch, epoch.judge_pin, "offline", model, executor=execute)
    else:
        logs = scorer.execute(
            epoch, epoch.judge_pin, "offline", model, executor=execute
        )
        assert len(logs) == len(calls) == 2
    assert scorer.read_costs(model) == {"dedup": None, "judge": None}
    with pytest.raises(ValueError, match="missing"), materialize_scorer("0" * 40):
        pass

"""Verify private vendor execution without importing or running vendor code."""

from types import SimpleNamespace

import pytest

from bench.runner import scorer
from bench.runner.epochs import DEFAULT_ROOT, load_epoch
from bench.runner.observations import ScoreRef, ScoreRequest, candidate_records
from bench.runner.score import evaluator_for


@pytest.mark.parametrize("stage_error", [None, "dedup", "judge"])
def test_run_stages_uses_named_source_private_cwd_and_explicit_paths(
    tmp_path, stage_error
):
    epoch = load_epoch("dfc6cb4-v1", root=DEFAULT_ROOT)
    url = epoch.bugs[0].pr_url
    evaluator = evaluator_for(epoch, "judge", "adjudicator")
    request = ScoreRequest(
        ScoreRef("run", epoch.epoch_id, "a" * 32),
        epoch,
        tmp_path,
        (url,),
        ((url, "attempt"),),
        candidate_records({url: {"deep-review": []}}, {url: "attempt"}),
        "deep-review",
        evaluator,
    )
    calls = []

    def execute(command, *, cwd, env, **kwargs):
        calls.append((command, cwd, env))
        module = command[command.index("-m") + 1]
        stage = "dedup" if "step2_5" in module else "judge"
        assert cwd == str((tmp_path / "work/delivered").resolve())
        assert env["PYTHONPATH"] == str(epoch.root / "scorer")
        assert "--project" in command and "--no-sync" in command
        if stage == "judge":
            from pathlib import Path

            assert Path(command[command.index("--dedup-groups") + 1]).is_absolute()
        return SimpleNamespace(
            returncode=int(stage == stage_error),
            stdout="",
            stderr="synthetic failure" if stage == stage_error else "",
        )

    if stage_error:
        with pytest.raises(RuntimeError, match=stage_error):
            scorer.run_stages(request, "synthetic", executor=execute)
    else:
        outputs = scorer.run_stages(request, "synthetic", executor=execute)
        assert outputs.candidates_path.is_file() and len(calls) == 2
        assert str(tmp_path) in str(outputs.evaluations_path)
    assert calls

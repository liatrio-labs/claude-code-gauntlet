"""Verify private vendor execution without importing or running vendor code."""

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from bench.runner import scorer
from bench.runner.epochs import DEFAULT_ROOT, file_hash, load_epoch
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
        source = Path(env["PYTHONPATH"])
        assert source != epoch.root / "scorer"
        assert (
            file_hash(source / "code_review_benchmark/step3_judge_comments.py")
            == dict(epoch.scorer_file_hashes)[
                "code_review_benchmark/step3_judge_comments.py"
            ]
        )
        assert "--project" in command and "--no-sync" in command
        if stage == "judge":
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
    assert not Path(calls[0][2]["PYTHONPATH"]).exists()


@pytest.mark.parametrize("epoch_id", ["dfc6cb4-v1", "e616e84-v1"])
def test_execute_materializes_git_source_and_refuses_every_file_hash(
    tmp_path, epoch_id
):
    epoch = load_epoch(epoch_id, root=DEFAULT_ROOT)
    assert not (epoch.root / "scorer").exists()
    assert not any("/scorer/" in p.as_posix() for p in epoch.root.rglob("*"))
    calls = []
    sources = []

    def execute(command, *, env, **kwargs):
        source = Path(env["PYTHONPATH"])
        sources.append(source)
        calls.append(command)
        assert all(
            file_hash(source / name) == expected
            for name, expected in epoch.scorer_file_hashes
        )
        if epoch_id == "dfc6cb4-v1":
            assert source != scorer.VENDOR_ENV
            assert file_hash(
                source / "code_review_benchmark/step3_judge_comments.py"
            ) != (
                file_hash(
                    scorer.VENDOR_ENV / "code_review_benchmark/step3_judge_comments.py"
                )
            )
        else:
            assert source == scorer.VENDOR_ENV
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    model_dir = scorer.stage_inputs(epoch, {}, "judge", tmp_path / "valid")
    scorer.execute(epoch, "judge", "synthetic", model_dir, executor=execute)
    assert len(calls) == 2 and sources[0] == sources[1]
    if epoch_id == "dfc6cb4-v1":
        assert not sources[0].exists()
    calls.clear()
    if epoch_id == "dfc6cb4-v1":
        changed = replace(
            epoch, scorer_commit=load_epoch(root=DEFAULT_ROOT).scorer_commit
        )
        with pytest.raises(ValueError, match="scorer file hash mismatch"):
            scorer.execute(changed, "judge", "synthetic", model_dir, executor=execute)
        assert not calls
    for index, (name, _) in enumerate(epoch.scorer_file_hashes):
        hashes = dict(epoch.scorer_file_hashes)
        hashes[name] = "0" * 64
        changed = replace(epoch, scorer_file_hashes=tuple(sorted(hashes.items())))
        model_dir = scorer.stage_inputs(
            changed, {}, "judge", tmp_path / f"hash-{index}"
        )
        with pytest.raises(ValueError, match="scorer file hash mismatch"):
            scorer.execute(changed, "judge", "synthetic", model_dir, executor=execute)
        assert not calls

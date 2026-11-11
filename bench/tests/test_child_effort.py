"""Session effort survives manifest creation and every child dispatch boundary."""

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from bench import run
from bench.runner import invoke, validity, windows

URL = "https://github.com/example/repo/pull/1"


@pytest.fixture
def child_commands(tmp_path, monkeypatch):
    meta = dict(
        owner="example",
        repo="repo",
        pr_number=1,
        head_sha="head",
        base_sha="base",
        base_ref="main",
    )
    epoch = SimpleNamespace(
        subsets={"smoke": [URL]},
        shas={URL: meta},
        scorer_input={},
    )
    monkeypatch.setattr(run, "RUNS_ROOT", tmp_path / "runs")
    monkeypatch.setattr(run, "load_epoch", lambda **kwargs: epoch)
    monkeypatch.setattr(run, "decision_hook", lambda *args, **kwargs: None)
    monkeypatch.setattr(run, "check_prereqs", lambda **kwargs: [])
    monkeypatch.setattr(run, "_judge_key_note", lambda *args: None)
    monkeypatch.setattr(run, "_git_short_sha", lambda: "fixture")
    monkeypatch.setattr(run, "_compute_diff", lambda *args: "")
    monkeypatch.setattr(run.mirrors, "ensure_mirror", lambda *args: tmp_path)
    monkeypatch.setattr(
        run.mirrors,
        "make_worktree",
        lambda *args, **kwargs: Path(args[4]).mkdir(parents=True, exist_ok=True),
    )
    monkeypatch.setattr(run.mirrors, "remove_worktree", lambda *args: None)
    monkeypatch.setattr(
        run,
        "_checkpoint_attempt",
        lambda directory, pr, cp, url, result, *args, **kwargs: cp.mark(
            url, result.status
        ),
    )
    monkeypatch.setattr(
        validity,
        "run_provenance",
        lambda **kwargs: {"golden_epoch_id": "fixture", "golden_sha256": "fixture"},
    )
    monkeypatch.setattr(validity, "treatment_sha", lambda: "fixture")
    monkeypatch.setattr(validity, "agent_read_hash", lambda: "fixture")
    monkeypatch.setattr(
        invoke,
        "build_env",
        lambda *args, **kwargs: {"CLAUDE_CONFIG_DIR": str(tmp_path / "config")},
    )
    monkeypatch.setattr(invoke, "_seed_trust", lambda *args: None)
    monkeypatch.setattr(invoke, "pinned_cli", lambda *args: "/synthetic/child")
    monkeypatch.setattr(invoke, "_v3_preflight", lambda *args: None)
    monkeypatch.setattr(invoke, "_plugin_dirty_paths", lambda *args: frozenset())
    monkeypatch.setattr(invoke, "_plugin_mutations", lambda *args: [])
    commands = []

    def popen(cmd, **kwargs):
        commands.append(cmd)
        return Mock(returncode=1, communicate=Mock(return_value=("", None)))

    monkeypatch.setattr(invoke.subprocess, "Popen", popen)
    return commands


@pytest.mark.parametrize("anchor", [None, "naive"])
@pytest.mark.parametrize("model", ["sonnet", "inherit"])
@pytest.mark.parametrize("effort", [None, "low", "medium", "high", "xhigh", "max"])
def test_new_run_records_effort_and_builds_child_command(
    child_commands, model, effort, anchor
):
    argv = ["--tier", "smoke", "--child-model", model]
    if anchor:
        argv += ["--anchor", anchor]
    if effort is not None:
        argv += ["--child-effort", effort]
    assert run.main(argv) == 1
    directory = next(run.RUNS_ROOT.iterdir())
    manifest = json.loads((directory / "run.json").read_text(encoding="utf-8"))
    assert manifest["child_effort"] == effort
    assert len(child_commands) == 1
    cmd = child_commands[0]
    if effort is None:
        assert "--effort" not in cmd
    else:
        assert cmd[-2:] == ["--effort", effort]
    if model != "inherit":
        assert cmd[cmd.index("--model") :][:2] == ["--model", model]
    else:
        assert "--model" not in cmd


@pytest.mark.parametrize("anchor", [None, "naive"])
@pytest.mark.parametrize("mode", ["resume", "retry-failed"])
@pytest.mark.parametrize("recorded", ["low", None, "legacy"])
@pytest.mark.parametrize("cli_effort", [None, "max"])
def test_existing_run_reuses_manifest_effort(
    child_commands, mode, recorded, cli_effort, anchor
):
    argv = ["--tier", "smoke", "--child-model", "sonnet"]
    if anchor:
        argv += ["--anchor", anchor]
    if recorded == "low":
        argv += ["--child-effort", recorded]
    assert run.main(argv) == 1
    directory = next(run.RUNS_ROOT.iterdir())
    path = directory / "run.json"
    if recorded == "legacy":
        manifest = json.loads(path.read_text(encoding="utf-8"))
        del manifest["child_effort"]
        path.write_text(json.dumps(manifest), encoding="utf-8")
    before = path.read_bytes()
    if mode == "resume":
        run.checkpoint.Checkpoint(directory).mark(URL, "pending")
    argv = ["--" + mode, directory.name]
    if cli_effort is not None:
        argv += ["--child-effort", cli_effort]
    assert run.main(argv) == 1
    assert path.read_bytes() == before
    assert len(child_commands) == 2
    cmd = child_commands[-1]
    if recorded == "low":
        assert cmd[-4:] == ["--model", "sonnet", "--effort", "low"]
    else:
        assert "--effort" not in cmd


@pytest.mark.parametrize("parser", [run.parse_args, windows.parse_args])
def test_child_effort_rejects_unknown_choices(parser, tmp_path):
    from bench.tests.test_windows import window_args

    argv = (
        window_args(tmp_path / "window.json", child_effort="ultra")
        if parser is windows.parse_args
        else ["--child-effort", "ultra"]
    )
    with pytest.raises(SystemExit) as exc:
        parser(argv)
    assert exc.value.code == 2


def test_orphan_resume_records_effort_for_later_retries(child_commands):
    directory = run.RUNS_ROOT / "smoke-orphan"
    directory.mkdir(parents=True)
    assert run.main(["--resume", directory.name, "--child-effort", "low"]) == 1
    manifest = json.loads((directory / "run.json").read_text(encoding="utf-8"))
    assert manifest["child_effort"] == "low"
    assert run.main(["--retry-failed", directory.name, "--child-effort", "max"]) == 1
    assert len(child_commands) == 2
    assert all(cmd[-2:] == ["--effort", "low"] for cmd in child_commands)

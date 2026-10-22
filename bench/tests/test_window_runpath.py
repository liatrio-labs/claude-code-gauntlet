"""Windowed reviews cross the real preflight and immutable artifact boundaries."""

import os
import sys
from dataclasses import replace

import pytest

from bench import run
from bench.runner import check, checkpoint, invoke, windows
from bench.runner import validity as v
from bench.runner.epochs import load_epoch, read_json
from bench.tests.test_check import _build_ok_run, _plant_raw_identity
from bench.tests.test_run_cli import fake_ensure_mirror, fake_make_worktree
from bench.tests.test_stages import emit_trace
from bench.tests.test_validity import NOW
from bench.tests.test_validity import fingerprint as fingerprint
from bench.tests.test_validity import window as window


@pytest.fixture(autouse=True)
def _committed_treatment(monkeypatch):
    # Cleanliness has its own test against a scratch repo; these tests must not depend on
    # whether the developer's checkout has uncommitted plugin edits.
    monkeypatch.setattr(v, "require_clean_treatment", lambda root=v.ROOT: None)


def test_model_probe_runs_real_node_against_registry():
    models = v.model_probe(v.ROOT, {"provider": "firstParty", "subagentModel": None})
    assert models["code-gauntlet:security-reviewer"] == "claude-opus-5-5"
    assert models["code-gauntlet:bug-detector"] == "claude-sonnet-5-5"
    assert models["code-gauntlet:artifact-writer"] == "claude-sonnet-5-5"
    pinned = v.model_probe(
        v.ROOT, {"provider": "firstParty", "subagentModel": "claude-probe-pinned"}
    )
    assert pinned.keys() == models.keys()
    assert set(pinned.values()) == {"claude-probe-pinned"}


@pytest.mark.parametrize("boundary", ["pr", "resume", "retry-failed"])
@pytest.mark.parametrize("changed", [False, True])
def test_json_manifest_provenance_passes_but_real_drift_is_refused(
    tmp_path, monkeypatch, fingerprint, window, boundary, changed, capsys
):
    monkeypatch.setattr(v, "utc_now", lambda: NOW)
    monkeypatch.setattr(v, "capture_fingerprint", lambda **kwargs: fingerprint)
    probe = v.run_provenance

    def observed_provenance(**kwargs):
        wire = probe(**kwargs)
        if changed:
            wire["controls"]["cli_version"] = "changed"
        return wire

    monkeypatch.setattr(v, "run_provenance", observed_provenance)
    path = tmp_path / "window.json"
    v.exclusive_json(path, window.wire())
    monkeypatch.setattr(run, "RUNS_ROOT", tmp_path / "runs")
    directory = run.RUNS_ROOT / "prior"
    manifest = {
        "run_id": "prior",
        "child_auth": "api",
        "child_model": "claude-session-1",
        "pr_urls": list(window.pr_urls),
        "measurement_window": str(path),
        "window_id": window.window_id,
        "window_sha256": v.file_hash(path),
        "provenance": fingerprint.wire(),
        "arm": "baseline",
        "run_index": 0,
        "started": NOW,
        "epoch_id": fingerprint.controls.golden_epoch_id,
        "treatment_git_sha": v.treatment_sha(),
        "agent_read_sha256": v.agent_read_hash(),
    }
    v.exclusive_json(directory / "run.json", manifest)
    calls = []
    if boundary == "pr":

        def mirror(*args, **kwargs):
            calls.append("mirror")
            raise RuntimeError("reached mirror after drift check")

        monkeypatch.setattr(run.mirrors, "ensure_mirror", mirror)
        epoch = load_epoch(root=run.GOLDEN_DIR)
        with pytest.raises(ValueError if changed else RuntimeError):
            run._run_prs(
                directory,
                [window.pr_urls[0]],
                checkpoint.Checkpoint(directory),
                epoch.shas,
                set(),
                60,
                None,
                epoch.scorer_input,
                child_model="claude-session-1",
            )
        assert calls == ([] if changed else ["mirror"])
    else:
        monkeypatch.setattr(run, "check_prereqs", lambda **kwargs: [])
        monkeypatch.setattr(run, "_judge_key_note", lambda *args: None)

        def resume(*args, **kwargs):
            calls.append("resume")
            return 0

        monkeypatch.setattr(run, "_resume", resume)
        assert run.main(
            ["--" + boundary, "prior", "--measurement-window", str(path)]
        ) == (2 if changed else 0)
        assert calls == ([] if changed else ["resume"])
    if changed:
        assert ("input_drift" if boundary == "pr" else "mismatch") in (
            capsys.readouterr().err
        )


@pytest.mark.parametrize(
    "arm,indices,runs",
    [(None, (0, 1), 1), ("unknown", (0, 1), 1), ("baseline", (0, 2, 3), 3)],
)
def test_invalid_window_plan_reports_usage_before_probing(
    tmp_path, monkeypatch, window, arm, indices, runs, capsys
):
    path = tmp_path / "window.json"
    v.exclusive_json(path, replace(window, planned_run_indices=indices).wire())
    monkeypatch.setattr(
        v,
        "run_provenance",
        lambda **kwargs: pytest.fail("probe before plan validation"),
    )
    args = [
        "--tier",
        "subset",
        "--measurement-window",
        str(path),
        "--run-index",
        "0",
        "--runs",
        str(runs),
    ]
    if arm is not None:
        args += ["--arm", arm]
    assert run.main(args) == 2
    assert "measurement requires a planned --run-index and explicit --arm" in (
        capsys.readouterr().err
    )


@pytest.mark.parametrize(
    "status,reason",
    [
        ("timeout", "watchdog_timeout"),
        ("failed", "mirror_error"),
        ("failed", "pipeline_failed"),
    ],
)
def test_measurement_errors_preserve_failure_in_checkpoint_and_archive(
    tmp_path, monkeypatch, fingerprint, window, status, reason
):
    monkeypatch.setattr(v, "utc_now", lambda: NOW)
    monkeypatch.setattr(v, "recheck_cli", lambda fp: None)
    path = tmp_path / "window.json"
    v.exclusive_json(path, window.wire())
    directory = tmp_path / "run"
    cp = checkpoint.Checkpoint(directory)
    attempt = run._checkpoint_attempt(
        directory,
        directory / "pr-1",
        cp,
        "u",
        invoke.InvokeResult(status, reason=reason),
        NOW,
        {
            "provenance": fingerprint.wire(),
            "measurement_window": str(path),
            "arm": "baseline",
            "window_id": window.window_id,
        },
    )
    assert cp.status("u") == attempt.checkpoint_status == status
    assert cp.detail("u")["reason"] == attempt.checkpoint_reason == reason
    assert set(attempt.validity.reasons) == {reason, "measurement_trace_missing"}
    assert (
        v.load_attempt(v.selected_attempt_path(directory, attempt.attempt_id))
        == attempt
    )


@pytest.mark.parametrize("defect", ["missing", "malformed", "fields", None])
def test_check_attempt_returns_failure_details_in_one_shape(tmp_path, defect):
    _build_ok_run(tmp_path)
    pr = next(tmp_path.glob("pr-*"))
    _plant_raw_identity(pr)
    payload = pr / "post-review-payload.json"
    if defect == "missing":
        payload.rename(pr / "not-a-payload.json")
    elif defect == "malformed":
        payload.write_text("broken", encoding="utf-8")
    elif defect == "fields":
        payload.write_text(
            '{"platform":"github","payload":{"comments":[{}]}}', encoding="utf-8"
        )
    failures = check.check_attempt(pr)
    assert isinstance(failures, list)
    if defect in ("missing", "malformed"):
        assert failures == ["artifact_missing"]
    elif defect == "fields":
        assert any("missing required field" in failure for failure in failures)
    else:
        assert failures == []


def test_window_capture_run_and_resume_with_real_preflight_and_fake_child(
    tmp_path, monkeypatch
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    for name, value in {
        "WORKSPACE": workspace,
        "RUNS_ROOT": workspace / "runs",
        "MIRRORS_DIR": workspace / "mirrors",
        "ENV_PATH": tmp_path / "absent-input",
    }.items():
        monkeypatch.setattr(run, name, value)
    monkeypatch.setattr(invoke, "ENV_PATH", run.ENV_PATH)
    monkeypatch.setattr(run, "_free_gb", lambda path: 100)
    monkeypatch.setattr(run.mirrors, "ensure_mirror", fake_ensure_mirror)
    monkeypatch.setattr(run.mirrors, "make_worktree", fake_make_worktree)
    monkeypatch.setattr(run.mirrors, "remove_worktree", lambda *args: None)
    monkeypatch.setattr(run, "_compute_diff", lambda *args: "fixture diff")
    monkeypatch.setattr(v, "utc_now", lambda: NOW)
    for key in (
        *v.MODEL_OVERRIDE_KEYS,
        *v.EFFORT_KEYS,
        "CLAUDE_CODE_USE_BEDROCK",
        "CLAUDE_CODE_USE_VERTEX",
        "CLAUDE_CODE_USE_FOUNDRY",
    ):
        monkeypatch.delenv(key, raising=False)
    seed = tmp_path / "seed"
    _build_ok_run(seed)
    _, trace, _ = emit_trace(tmp_path)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    child = bindir / "claude"
    child.write_text(
        f"#!{sys.executable}\n"
        "import json, shutil, sys\nfrom pathlib import Path\n"
        f"sys.path.insert(0, {str(v.ROOT)!r})\n"
        "from bench.tests.fakes import fake_claude as fake\n"
        "fake.main()\n"
        "if '--version' not in sys.argv:\n"
        "    import os\n"
        "    out = Path(os.environ['CODE_GAUNTLET_OUTPUT_DIR'])\n"
        f"    for path in Path({str(next(seed.glob('pr-*')))!r}).glob('code-gauntlet-*'):\n"
        "        shutil.copyfile(path, out / path.name)\n"
        f"    text = Path({str(trace)!r}).read_text(encoding='utf-8')\n"
        "    terminal = {'ok': True, 'stats': {}, 'measurement': {'status': 'available', 'text': text}}\n"
        "    fake._write_workflow_record('wf_window', terminal)\n",
        encoding="utf-8",
    )
    child.chmod(0o755)
    gh_calls = tmp_path / "gh-calls.jsonl"
    for name in ("gh", "uv"):
        binary = bindir / name
        binary.write_text(
            f"#!{sys.executable}\nimport json, sys\n"
            f"with open({str(gh_calls)!r}, 'a', encoding='utf-8') as stream:\n"
            "    stream.write(json.dumps(sys.argv) + '\\n')\n",
            encoding="utf-8",
        )
        binary.chmod(0o755)
    monkeypatch.setenv("PATH", str(bindir) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("GH_TOKEN", "synthetic")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "synthetic")
    monkeypatch.setenv("BENCH_CLAUDE_HOME", str(workspace / "claude-home"))
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "ok")
    monkeypatch.setenv("FAKE_CLAUDE_ARGV_FILE", str(tmp_path / "child-argv"))
    path = tmp_path / "window.json"
    assert (
        windows.main(
            [
                "--population",
                "mini",
                "--arm",
                f"baseline=worktree:{v.ROOT}",
                "--arm",
                "candidate=sha:HEAD",
                "--run-indices",
                "0",
                "--auth-mode",
                "subscription",
                "--duration-hours",
                "24",
                "--opens-at",
                "2026-10-20T00:00:00Z",
                "--window-id",
                "e2e",
                "--child-model",
                "claude-opus-4-8",
                "--cli",
                str(child),
                "--timeout-mins",
                "1",
                "--out",
                str(path),
            ]
        )
        == 0
    )
    window = v.load_window(path)
    fp = v.RunFingerprint(window.expected, window.arms["baseline"])
    url = window.pr_urls[0]
    loads = []

    def read_epoch(*args, **kwargs):
        loads.append(args)
        return load_epoch(*args, **kwargs)

    monkeypatch.setattr(run, "load_epoch", read_epoch)
    monkeypatch.setattr(v, "load_epoch", read_epoch)
    assert (
        run.main(
            [
                "--tier",
                "mini",
                "--measurement-window",
                str(path),
                "--arm",
                "baseline",
                "--run-index",
                "0",
                "--child-auth",
                "subscription",
                "--child-model",
                "claude-opus-4-8",
                "--timeout-mins",
                "1",
            ]
        )
        == 0
    )
    assert len(loads) == 1
    directory = next(run.RUNS_ROOT.iterdir())
    manifest = read_json(directory / "run.json")
    assert v.same_provenance(fp.wire(), manifest["provenance"])
    cp = checkpoint.Checkpoint(directory)
    assert cp.status(url) == "ok"
    selected = v.scoring_selection(directory)[url]
    assert selected.validity.state == "valid"
    archive = v.selected_attempt_path(directory, selected.attempt_id).parent
    assert read_json(archive / "artifacts/measurement-collection.json") == {
        "status": "available",
        "reason": None,
    }
    assert (
        archive / "artifacts/code-gauntlet-measurement-trace.json"
    ).read_bytes() == trace.read_bytes()
    before = {p: p.read_bytes() for p in directory.rglob("*") if p.is_file()}
    argv = (tmp_path / "child-argv").read_bytes()
    loads.clear()
    assert (
        run.main(["--resume", directory.name, "--measurement-window", str(path)]) == 0
    )
    assert len(loads) == 1
    assert {p: p.read_bytes() for p in directory.rglob("*") if p.is_file()} == before
    assert (tmp_path / "child-argv").read_bytes() == argv
    assert len(gh_calls.read_text(encoding="utf-8").splitlines()) == 2

"""Measurement refuses ambiguous treatments before any mirror or child work."""

import subprocess
from contextlib import nullcontext

import pytest

from bench import run
from bench.runner import validity as v
from bench.tests.test_validity import fingerprint as fingerprint
from bench.tests.test_validity import window as window


@pytest.mark.parametrize(
    "path",
    [
        "workflows/pipeline.js",
        "scripts/gauntlet/helper.py",
        "agents/new.md",
        "skills/new/SKILL.md",
        ".claude-plugin/plugin.json",
    ],
)
def test_treatment_hash_covers_the_index_tree_and_dirty_files_are_refused(
    git_repo, path
):
    root, git = git_repo
    target = root / path
    target.parent.mkdir(parents=True)
    target.write_text("baseline", encoding="utf-8")
    git("add", ".")
    git("commit", "-qm", "baseline")
    original = v.agent_read_hash(root)
    v.require_clean_treatment(root)
    (root / "untracked.txt").write_text("harmless untracked artifact", encoding="utf-8")
    v.require_clean_treatment(root)
    target.write_text("dirty", encoding="utf-8")
    with pytest.raises(ValueError, match="clean tracked"):
        v.require_clean_treatment(root)
    git("add", ".")
    assert v.agent_read_hash(root) != original
    with pytest.raises(ValueError, match="clean tracked"):
        v.require_clean_treatment(root)
    git("commit", "-qm", "candidate")
    v.require_clean_treatment(root)


@pytest.mark.parametrize("key", (*v.MODEL_OVERRIDE_KEYS, *v.EFFORT_KEYS))
@pytest.mark.parametrize("value", ["ambient-override", ""])
def test_ambient_model_or_effort_override_cannot_launch_a_child(
    tmp_path, monkeypatch, window, key, value
):
    for override in (*v.MODEL_OVERRIDE_KEYS, *v.EFFORT_KEYS):
        monkeypatch.delenv(override, raising=False)
    monkeypatch.setenv(key, value)
    path = tmp_path / "window.json"
    v.exclusive_json(path, window.wire())
    monkeypatch.setattr(v, "require_clean_treatment", lambda: None)
    monkeypatch.setattr(v, "run_provenance", lambda **kw: window.expected.wire())
    monkeypatch.setattr(run, "_gh_token_scope", lambda env: nullcontext())
    monkeypatch.setattr(run, "_judge_key_note", lambda *a: None)
    calls = []
    monkeypatch.setattr(
        run.mirrors, "ensure_mirror", lambda *a, **kw: calls.append("mirror")
    )
    monkeypatch.setattr(
        run.invoke, "invoke_review", lambda *a, **kw: calls.append("child")
    )
    monkeypatch.setattr(run, "check_prereqs", lambda **kw: [])
    monkeypatch.setattr(run, "_new_run", lambda *a: calls.append("child"))
    args = [
        "--measurement-window",
        str(path),
        "--tier",
        "subset",
        "--arm",
        "baseline",
        "--run-index",
        "0",
        "--child-model",
        "claude-session-1",
    ]
    assert run.main(args) == 2
    assert calls == []
    monkeypatch.delenv(key)
    assert run.main(args) == 0
    assert calls == ["child"]


@pytest.mark.parametrize("key", (*v.MODEL_OVERRIDE_KEYS, *v.EFFORT_KEYS))
def test_fingerprint_refuses_ambient_overrides_before_policy_or_cli_probe(
    tmp_path, key
):
    with pytest.raises(ValueError, match="ambient model/effort"):
        v.capture_fingerprint(
            cli_path=tmp_path / "absent-cli",
            child_model="claude-session-1",
            auth_mode="api",
            epoch=None,
            judge_pin="judge",
            adjudicator_pin="judge",
            env={key: "override"},
            policy={},
            model_probe=lambda *a: pytest.fail("policy probe launched"),
            cli_probe=lambda *a: pytest.fail("CLI probe launched"),
        )


def test_dirty_treatment_refused_at_entry_before_loading_window(
    tmp_path, monkeypatch, capsys
):
    def status(argv, **kwargs):
        assert argv == ["git", "status", "--porcelain", "--untracked-files=no"]
        return subprocess.CompletedProcess(argv, 0, " M workflows/pipeline.js\n")

    monkeypatch.setattr(v.subprocess, "run", status)
    assert (
        run.main(
            [
                "--measurement-window",
                str(tmp_path / "absent-window"),
                "--tier",
                "subset",
            ]
        )
        == 2
    )
    assert "clean tracked files" in capsys.readouterr().err


def test_dirty_treatment_stops_per_pr_guard_before_mirror_or_dispatch(
    tmp_path, monkeypatch
):
    from bench.runner import checkpoint

    manifest = {
        "measurement_window": "synthetic-window",
        "epoch_id": "epoch",
        "provenance": {},
        "treatment_git_sha": "treatment",
        "agent_read_sha256": "tree",
    }
    v.exclusive_json(tmp_path / "run.json", manifest)
    monkeypatch.setattr(v, "run_provenance", lambda **kw: {})
    monkeypatch.setattr(v, "load_window", lambda path: None)
    monkeypatch.setattr(v, "agent_read_hash", lambda: "tree")
    monkeypatch.setattr(run, "_git_full_sha", lambda: "treatment")
    monkeypatch.setattr(
        v,
        "require_clean_treatment",
        lambda: (_ for _ in ()).throw(ValueError("dirty tracked file")),
    )
    calls = []
    monkeypatch.setattr(
        run.mirrors, "ensure_mirror", lambda *a, **kw: calls.append("mirror")
    )
    monkeypatch.setattr(
        run.invoke, "invoke_review", lambda *a, **kw: calls.append("child")
    )
    with pytest.raises(ValueError, match="before mirror/child"):
        run._run_prs(
            tmp_path,
            ["synthetic-pr"],
            checkpoint.Checkpoint(tmp_path),
            {},
            set(),
            60,
            None,
            {},
        )
    assert calls == []

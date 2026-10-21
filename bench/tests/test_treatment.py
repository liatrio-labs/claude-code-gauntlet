"""Treatment controls are checked before mirror or child work."""

import os
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
    (root / "untracked.txt").write_text("scratch", encoding="utf-8")
    v.require_clean_treatment(root)
    target.write_text("dirty", encoding="utf-8")
    with pytest.raises(ValueError, match="clean tracked"):
        v.require_clean_treatment(root)
    git("add", ".")
    assert v.agent_read_hash(root) != original
    with pytest.raises(ValueError, match="clean tracked"):
        v.require_clean_treatment(root)


@pytest.mark.parametrize(
    "key",
    (
        *v.MODEL_OVERRIDE_KEYS,
        *v.EFFORT_KEYS,
        "ANTHROPIC_BASE_URL",
        "CLAUDE_CODE_USE_VERTEX",
    ),
)
@pytest.mark.parametrize("value", ["override", ""])
def test_ambient_model_or_effort_override_cannot_launch_a_child(
    tmp_path, monkeypatch, window, fingerprint, key, value
):
    monkeypatch.setattr(v, "utc_now", lambda: "2026-10-20T12:00:00Z")
    for override in (*v.MODEL_OVERRIDE_KEYS, *v.EFFORT_KEYS):
        monkeypatch.delenv(override, raising=False)
    monkeypatch.setenv(key, value)
    path = tmp_path / "window.json"
    v.exclusive_json(path, window.wire())
    monkeypatch.setattr(v, "require_clean_treatment", lambda: None)

    def capture_fingerprint(**kwargs):
        v.refuse_ambient_model_overrides(os.environ)
        return fingerprint

    monkeypatch.setattr(v, "capture_fingerprint", capture_fingerprint)
    monkeypatch.setattr(run, "_gh_token_scope", lambda env: nullcontext())
    monkeypatch.setattr(run, "_judge_key_note", lambda *a: None)
    monkeypatch.setattr(run, "check_prereqs", lambda **kw: [])
    calls = []
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
    # Empty provider selectors are inactive; empty model selectors are still overrides.
    refused = value != "" or key in (*v.MODEL_OVERRIDE_KEYS, *v.EFFORT_KEYS)
    assert run.main(args) == (2 if refused else 0)
    assert calls == ([] if refused else ["child"])


def test_dirty_treatment_stops_per_pr_guard_before_mirror_or_dispatch(
    tmp_path, monkeypatch
):
    from bench.runner.checkpoint import Checkpoint

    v.exclusive_json(tmp_path / "run.json", {"measurement_window": "window"})
    monkeypatch.setattr(
        v,
        "require_clean_treatment",
        lambda: (_ for _ in ()).throw(ValueError("dirty tracked file")),
    )
    monkeypatch.setattr(
        run.mirrors, "ensure_mirror", lambda *a: pytest.fail("mirror before preflight")
    )
    with pytest.raises(ValueError, match="before mirror/child"):
        run._run_prs(tmp_path, ["u"], Checkpoint(tmp_path), {}, set(), 60, None, {})

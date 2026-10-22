"""Window publication pins each source and refuses unusable declarations."""

from dataclasses import asdict

import pytest

from bench.runner import validity as v
from bench.runner import windows
from bench.runner.epochs import digest
from bench.tests.conftest import write
from bench.tests.test_compare import rule


def window_args(path, **overrides):
    options = dict(
        population="mini",
        arm=["baseline=sha:HEAD", "candidate=sha:HEAD"],
        run_indices="0",
        auth_mode="api",
        duration_hours="1",
        child_model="claude-test-1",
        out=path,
    )
    options.update(overrides)
    return cli_args(**options)


def cli_args(**options):
    return [
        str(item)
        for key, value in options.items()
        for val in (value if isinstance(value, list) else [value])
        for item in ("--" + key.replace("_", "-"), val)
    ]


@pytest.mark.parametrize("binding", ["frozen", "unfrozen", "missing"])
def test_window_cli_binds_rule_and_captures_each_source(
    git_repo, tmp_path, monkeypatch, binding
):
    root, git = git_repo
    agent = root / "agents/reviewer.md"
    agent.parent.mkdir()
    src = root / "workflows/src"
    src.mkdir(parents=True)
    (src / "registry.js").write_text(
        'export const LIMIT_DEFAULTS = {}; export function resolvedDispatchModels() { return {reviewer: "claude-source-1"}; }',
        encoding="utf-8",
    )
    (src / "capacity.js").write_text("// capacity\n", encoding="utf-8")
    shas = []
    for effort in ("high", "low"):
        agent.write_text(f"---\neffort: {effort}\n---\nreview\n", encoding="utf-8")
        git("add", ".")
        git("commit", "-qm", effort)
        shas.append(v.treatment_sha(root))
    monkeypatch.chdir(root)
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://host.invalid")
    child = tmp_path / "claude"
    child.write_text("#!/bin/sh\necho 2.1.200\n", encoding="utf-8")
    child.chmod(0o755)
    path, rule_path = tmp_path / "window.json", tmp_path / "rule.json"
    value = asdict(rule(frozen=binding != "unfrozen"))
    write(rule_path, value)
    argv = window_args(
        path,
        arm=["baseline=sha:HEAD~1", f"candidate=worktree:{root}"],
        cli=child,
        auth_mode="host",
        wave="wave",
    )
    if binding != "missing":
        argv += ["--rule", str(rule_path)]
    assert windows.main(argv) == (0 if binding == "frozen" else 2)
    if binding == "frozen":
        window = v.load_window(path)
        assert (window.wave_id, window.rule_sha256) == ("wave", digest(value))
        assert [t.treatment_git_sha for t in window.arms.values()] == shas


@pytest.mark.parametrize(
    "key,value",
    [
        ("run_indices", "1,0"),
        ("duration_hours", "nan"),
        ("timeout_mins", "0"),
        ("arm", ["baseline=sha:HEAD", "candidate=sha:HEAD", "third=sha:HEAD"]),
        ("opens_at", "2026-10-20"),
        ("arm", ["baseline=sha:HEAD", "candidate=sha:missing"]),
    ],
)
def test_invalid_window_plan_refuses_before_fingerprint_probes(
    tmp_path, monkeypatch, key, value
):
    monkeypatch.setattr(
        v, "capture_fingerprint", lambda **kw: pytest.fail("probe reached")
    )
    path = tmp_path / "window.json"
    assert windows.main(window_args(path, **{key: value})) == 2
    assert not path.exists()

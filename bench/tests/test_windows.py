"""Window publication pins each source and refuses unusable declarations."""

from datetime import timedelta

import pytest

from bench.runner import validity as v
from bench.runner import windows
from bench.runner.epochs import load_epoch


def test_window_cli_pins_both_sources_and_never_overwrites(
    git_repo, tmp_path, monkeypatch
):
    root, git = git_repo
    agent = root / "agents/reviewer.md"
    agent.parent.mkdir()
    agent.write_text("---\neffort: high\n---\nreview\n", encoding="utf-8")
    src = root / "workflows/src"
    src.mkdir(parents=True)
    (src / "capacity.js").write_text("// capacity\n", encoding="utf-8")
    (src / "registry.js").write_text(
        'export const LIMIT_DEFAULTS = {};\nexport function resolvedDispatchModels() { return {reviewer: "claude-source-1"}; }',
        encoding="utf-8",
    )
    git("add", ".")
    git("commit", "-qm", "source one")
    first = git("rev-parse", "HEAD")
    old_digest = v.treatment_sha(root)
    agent.write_text("---\neffort: low\n---\nreview\n", encoding="utf-8")
    (src / "capacity.js").write_text("// other capacity\n", encoding="utf-8")
    git("add", ".")
    git("commit", "-qm", "source two")
    monkeypatch.setenv("CLAUDE_CODE_PROVIDER_MANAGED_BY_HOST", "1")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://host.invalid")
    monkeypatch.setenv("CLAUDE_SESSION_INGRESS_TOKEN_FILE", "/synthetic/ingress")
    monkeypatch.chdir(root)
    child = tmp_path / "claude"
    child.write_text("#!/bin/sh\necho 2.1.200\n", encoding="utf-8")
    child.chmod(0o755)
    path = tmp_path / "window.json"
    argv = [
        "--population",
        "mini",
        "--arm",
        f"base=sha:{first}",
        "--arm",
        f"new=worktree:{root}",
        "--candidate-arm",
        "new",
        "--run-indices",
        "0,2",
        "--auth-mode",
        "host",
        "--duration-hours",
        "2.5",
        "--child-model",
        "claude-test-1",
        "--cli",
        str(child),
        "--out",
        str(path),
    ]
    before = (
        git("status", "--porcelain"),
        git("rev-parse", "HEAD"),
        v.treatment_sha(root),
    )
    assert windows.main(argv) == 0
    window = v.load_window(path)
    assert window.expected.auth_mode == "host"
    assert window.arms["base"].treatment_git_sha == old_digest
    assert window.arms["new"].treatment_git_sha == before[2] != old_digest
    assert window.arms["base"].agent_efforts == {"reviewer": "high"}
    assert window.arms["new"].agent_efforts == {"reviewer": "low"}
    assert (
        window.arms["base"].bench_policy_sha256
        != window.arms["new"].bench_policy_sha256
    )
    assert set(window.pr_urls) == set(load_epoch().subsets["mini"])
    assert window.planned_order == (("base", 0), ("new", 0), ("base", 2), ("new", 2))
    assert v.timestamp(window.closes_at) - v.timestamp(window.opens_at) == timedelta(
        hours=2.5
    )
    assert (
        git("status", "--porcelain"),
        git("rev-parse", "HEAD"),
        v.treatment_sha(root),
    ) == before
    saved = path.read_bytes()
    assert windows.main(argv) == 2 and path.read_bytes() == saved


@pytest.mark.parametrize(
    "flags",
    [
        ["--run-indices", "1,0"],
        ["--duration-hours", "nan"],
        ["--timeout-mins", "0"],
        ["--arm", "third=sha:HEAD"],
        ["--opens-at", "2026-10-20"],
        ["--arm", "candidate=sha:missing"],
    ],
)
def test_invalid_window_plan_refuses_before_fingerprint_probes(
    tmp_path, monkeypatch, flags
):
    monkeypatch.setattr(
        v, "capture_fingerprint", lambda **kw: pytest.fail("probe reached")
    )
    path = tmp_path / "window.json"
    argv = [
        "--population",
        "mini",
        "--arm",
        "baseline=sha:HEAD",
        "--arm",
        "candidate=sha:HEAD",
        "--run-indices",
        "0",
        "--auth-mode",
        "api",
        "--duration-hours",
        "1",
        "--child-model",
        "claude-test-1",
        "--out",
        str(path),
    ]
    assert windows.main([*argv, *flags]) == 2
    assert not path.exists()

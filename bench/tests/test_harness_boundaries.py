"""Exercise harness authentication and stop classification without live subprocesses."""

import json
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from bench import run
from bench.runner import checkpoint, invoke, mirrors
from bench.tests.test_run_cli import fake_ensure_mirror, fake_make_worktree


@dataclass(frozen=True, slots=True)
class FakeProcess:
    output: str
    returncode: int = 0

    def wait(self, timeout):
        return self.returncode

    def communicate(self, input=None, timeout=None):
        return self.output, None


@dataclass(frozen=True, slots=True)
class Harness:
    workspace: Path
    operator_home: str
    control: dict = field(default_factory=dict, repr=False)
    commands: list = field(default_factory=list)
    launches: list = field(default_factory=list)
    probe_tokens: list = field(default_factory=list)

    def echo(self, **changes):
        knobs = dict(invoke.EXPECTED_ECHO, **changes)
        return (
            "Headless config:\n"
            + "\n".join(f"  {key}={value} (env)" for key, value in knobs.items())
            + (
                f"\n  pipeline_version={self.control.get('version', invoke.read_pipeline_version(invoke.REPO_ROOT))} (bundle)"
                f"\n  plugin_root={self.control.get('root', invoke.REPO_ROOT)} (resolved)\n"
            )
        )

    def gh_result(self, argv, **kwargs):
        self.commands.append(list(argv))
        if argv[:3] == ["gh", "auth", "token"]:
            env = kwargs["env"]
            assert env["HOME"] == self.operator_home
            assert env["GH_CONFIG_DIR"] == str(self.workspace / "operator-gh")
            assert env["XDG_CONFIG_HOME"] == str(self.workspace / "operator-xdg")
            mode = self.control.get("lookup", "ok")
            if mode == "timeout":
                raise subprocess.TimeoutExpired(argv, 30, output=self.control["token"])
            if mode == "launch":
                raise OSError(self.control["token"])
            return subprocess.CompletedProcess(
                argv,
                1 if mode == "failed" else 0,
                "" if mode == "empty" else self.control["token"] + "\n",
                self.control["token"],
            )
        if argv[:2] == ["gh", "api"]:
            env = kwargs["env"]
            self.probe_tokens.append(env.get("GH_TOKEN"))
            assert env["HOME"] == str(self.workspace / "claude-home")
            assert env["CLAUDE_CONFIG_DIR"] == str(
                self.workspace / "claude-home" / "config"
            )
            assert bool(env.get("GH_TOKEN") == self.control["token"])
            if self.control.get("probe") == "timeout":
                raise subprocess.TimeoutExpired(argv, 30, stderr=self.control["token"])
            if self.control.get("probe") == "launch":
                raise OSError(self.control["token"])
            accepted = "/pulls/" in argv[-1] and self.control.get("probe") != "rejected"
            return subprocess.CompletedProcess(
                argv, 0 if accepted else 1, self.control["token"], self.control["token"]
            )
        if argv == ["claude", "--version"]:
            return subprocess.CompletedProcess(argv, 0, "2.1.154 (Claude Code)", "")
        if argv == ["git", "status", "--porcelain"]:
            return subprocess.CompletedProcess(argv, 0, "", "")
        if argv == ["git", "status", "--porcelain", "--untracked-files=no"]:
            return subprocess.CompletedProcess(argv, 0, "", "")
        if argv[:3] == ["git", "ls-files", "--stage"]:
            return subprocess.CompletedProcess(
                argv, 0, b"100644 fixture 0\tworkflows/pipeline.js\0", b""
            )
        raise AssertionError(f"unexpected subprocess: {argv}")

    def popen(self, argv, **kwargs):
        self.commands.append(list(argv))
        env = kwargs["env"]
        self.launches.append(
            (env["HOME"], bool(env.get("GH_TOKEN") == self.control["token"]))
        )
        transcript_mode = self.control.get("transcript")
        result_text = "Stopped before review."
        if transcript_mode:
            self.write_transcript(
                kwargs["cwd"], env["CLAUDE_CONFIG_DIR"], transcript_mode
            )
        elif "--plugin-dir" in argv:
            result_text = self.echo()
            output_dir = Path(env["CODE_GAUNTLET_OUTPUT_DIR"])
            output_dir.mkdir(parents=True, exist_ok=True)
            (output_dir / "post-review-payload.json").write_text(
                '{"payload":{"comments":[]}}', encoding="utf-8"
            )
        else:
            result_text = '```json\n{"comments": []}\n```'
        output = json.dumps(
            {
                "session_id": "current-session",
                "result": result_text,
                "is_error": False,
                "total_cost_usd": 1.23,
            }
        )
        if kwargs.get("stdout") != subprocess.PIPE:
            kwargs["stdout"].write(output)
        return FakeProcess(output)

    def write_transcript(self, worktree, config_dir, mode):
        slug = re.sub(r"[^A-Za-z0-9]", "-", str(Path(worktree).resolve()))
        project = Path(config_dir) / "projects" / slug
        project.mkdir(parents=True, exist_ok=True)
        receipt = self.echo(**self.control.get("knobs", {}))
        use = {
            "type": "assistant",
            "message": {
                "content": [{"type": "tool_use", "id": "bash-id", "name": "Bash"}]
            },
        }
        result = {
            "type": "user",
            "message": {
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "bash-id",
                        "content": receipt,
                    }
                ]
            },
        }
        records = [use, result]
        if mode == "stale":
            (project / "older-session.jsonl").write_text(
                "\n".join(map(json.dumps, records)), encoding="utf-8"
            )
            records = [use]
        elif mode == "prompt":
            records = [
                {
                    "type": "user",
                    "message": {"content": [{"type": "text", "text": receipt}]},
                }
            ]
        elif mode == "api":
            records = [use, dict(result, type="api-request")]
        elif mode == "uncorrelated":
            records = [result]
        elif mode == "other-tool":
            use["message"]["content"][0]["name"] = "Read"
        elif mode == "text-blocks":
            result["message"]["content"][0]["content"] = [
                {"type": "text", "text": receipt}
            ]
        elif mode == "workflow":
            workflows = project / "current-session" / "workflows"
            workflows.mkdir(parents=True)
            (workflows / "wf_started.json").write_text("{}", encoding="utf-8")
        (project / "current-session.jsonl").write_text(
            "\n".join(map(json.dumps, records)), encoding="utf-8"
        )


@pytest.fixture
def harness(tmp_path, monkeypatch):
    state = Harness(tmp_path, str(tmp_path / "operator-home"))
    state.control["token"] = "synthetic-gh-credential"
    for name in ("GH_TOKEN", "GITHUB_TOKEN", "BENCH_CLAUDE_HOME"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOME", state.operator_home)
    monkeypatch.setenv("GH_CONFIG_DIR", str(tmp_path / "operator-gh"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "operator-xdg"))
    for name, value in (
        ("WORKSPACE", tmp_path),
        ("RUNS_ROOT", tmp_path / "runs"),
        ("MIRRORS_DIR", tmp_path / "mirrors"),
    ):
        monkeypatch.setattr(run, name, value)
    monkeypatch.setattr(run, "_read_env_key", lambda *args: "synthetic-api-key")
    monkeypatch.setattr(invoke, "_load_dotenv_key", lambda *args: None)
    monkeypatch.setattr(run, "_git_short_sha", lambda: "deadbeef")
    monkeypatch.setattr(run, "_git_full_sha", lambda: "deadbeef" * 5)
    monkeypatch.setattr(run, "_compute_diff", lambda *args: "")
    monkeypatch.setattr(run, "_free_gb", lambda *args: 100)
    monkeypatch.setattr(invoke, "_plugin_dirty_paths", lambda *args: frozenset())
    monkeypatch.setattr(run.shutil, "which", lambda name, **kwargs: name)
    monkeypatch.setattr(subprocess, "run", state.gh_result)
    monkeypatch.setattr(subprocess, "Popen", state.popen)
    monkeypatch.setattr(run.mirrors, "ensure_mirror", fake_ensure_mirror)
    monkeypatch.setattr(run.mirrors, "make_worktree", fake_make_worktree)
    monkeypatch.setattr(run.mirrors, "remove_worktree", lambda *args: None)
    return state


def test_resolved_token_crosses_both_child_boundaries_once_without_leaks(
    harness, capsys
):
    sentinel = "GH_SENTINEL_MUST_STAY_IN_MEMORY_533"
    harness.control["token"] = sentinel
    for anchor in ([], ["--anchor", "naive"]):
        harness.commands.clear()
        harness.launches.clear()
        assert run.main(["--tier", "smoke", "--runs", "2", *anchor]) == 0
        assert sum(cmd[:3] == ["gh", "auth", "token"] for cmd in harness.commands) == 1
        probes = [cmd for cmd in harness.commands if cmd[:2] == ["gh", "api"]]
        first_url = run._load_json(run.GOLDEN_DIR / "subsets.json")["smoke"][0]
        assert probes == [
            [
                "gh",
                "api",
                "--hostname",
                "github.com",
                first_url.replace("https://github.com/", "repos/").replace(
                    "/pull/", "/pulls/"
                ),
            ]
        ]
        assert len(harness.launches) == 6
        assert all(
            home != harness.operator_home and received
            for home, received in harness.launches
        )
        assert "GH_TOKEN" not in os.environ
        assert sentinel not in json.dumps(harness.commands)
    # gh may return secret-bearing diagnostics or exceptions; none may escape preflight.
    harness.control["lookup"] = "launch"
    assert run.main(["--tier", "smoke"]) == 2
    output = capsys.readouterr()
    assert sentinel not in output.out + output.err
    files = [path for path in harness.workspace.rglob("*") if path.is_file()]
    assert any(path.name == "run.json" for path in files)
    assert any(path.parent.name == "state" for path in files)
    assert all(sentinel not in path.read_text(encoding="utf-8") for path in files)


@pytest.mark.parametrize("explicit", ["GH_TOKEN", "GITHUB_TOKEN"])
def test_explicit_credential_wins_without_operator_lookup(
    harness, monkeypatch, explicit
):
    monkeypatch.setenv("GITHUB_TOKEN", "losing-token")
    monkeypatch.setenv(explicit, harness.control["token"])
    assert run.main(["--tier", "smoke"]) == 0
    assert not any(cmd[:3] == ["gh", "auth", "token"] for cmd in harness.commands)
    assert os.environ.get(explicit) == harness.control["token"]
    first_url = run._load_json(run.GOLDEN_DIR / "subsets.json")["smoke"][0]
    assert [cmd for cmd in harness.commands if cmd[:2] == ["gh", "api"]] == [
        [
            "gh",
            "api",
            "--hostname",
            "github.com",
            first_url.replace("https://github.com/", "repos/").replace(
                "/pull/", "/pulls/"
            ),
        ]
    ]
    assert harness.probe_tokens == [harness.control["token"]]


@pytest.mark.parametrize(
    "failure",
    [
        "empty",
        "failed",
        "timeout",
        "launch",
        "rejected",
        "probe-timeout",
        "probe-launch",
        "explicit-rejected",
    ],
)
@pytest.mark.parametrize("mode", ["new", "resume", "retry-failed"])
def test_auth_failure_stops_every_dispatch_mode(
    harness, monkeypatch, failure, mode, capsys
):
    argv = ["--tier", "smoke"]
    if mode != "new":
        run_dir = harness.workspace / "runs" / "custom-resume"
        run_dir.mkdir(parents=True)
        url = run._load_json(run.GOLDEN_DIR / "subsets.json")["smoke"][1]
        (run_dir / "run.json").write_text(
            json.dumps({"tier": "custom", "pr_urls": [url]}), encoding="utf-8"
        )
        argv = [f"--{mode}", run_dir.name]
    if failure in ("rejected", "explicit-rejected"):
        harness.control["probe"] = "rejected"
    elif failure.startswith("probe-"):
        harness.control["probe"] = failure.removeprefix("probe-")
    else:
        harness.control["lookup"] = failure
    if failure == "explicit-rejected":
        monkeypatch.setenv("GH_TOKEN", harness.control["token"])
        monkeypatch.setenv("GITHUB_TOKEN", "fallback-must-not-win")
    monkeypatch.setattr(run, "_new_run", lambda *args: pytest.fail("new run started"))
    monkeypatch.setattr(
        run, "_resume", lambda *args, **kwargs: pytest.fail("resume started")
    )
    assert run.main(argv) == 2
    assert not harness.launches
    assert "GitHub PR access preflight failed" in capsys.readouterr().err
    if mode != "new" and harness.control.get("probe"):
        assert next(
            cmd[-1] for cmd in harness.commands if cmd[:2] == ["gh", "api"]
        ) == url.replace("https://github.com/", "repos/").replace("/pull/", "/pulls/")
    if failure == "explicit-rejected":
        assert not any(cmd[:3] == ["gh", "auth", "token"] for cmd in harness.commands)


@pytest.mark.parametrize("content", ["receipt", "text-blocks"])
def test_current_bash_receipt_classifies_and_retries_pre_workflow_stop(
    harness, content
):
    harness.control["transcript"] = content
    url = next(iter(run._load_json(run.GOLDEN_DIR / "shas.json")))
    assert run.main(["--prs", url]) == 1
    run_dir = next((harness.workspace / "runs").iterdir())
    cp = checkpoint.Checkpoint(run_dir)
    assert cp.failed([url]) == [url]
    assert cp.detail(url)["reason"] == "pre_workflow_stop"
    assert cp.detail(url)["cost_usd"] == 1.23
    harness.control.pop("transcript")
    assert run.main(["--retry-failed", run_dir.name]) == 0
    assert len(harness.launches) == 2
    assert cp.status(url) == "ok"


def test_retry_failed_retries_free_setup_failures_but_not_sha_or_drift(
    harness, monkeypatch
):
    from dataclasses import replace

    epoch = run.load_epoch(root=run.GOLDEN_DIR)
    shas = {u: dict(meta) for u, meta in epoch.shas.items()}
    urls = list(shas)[:4]
    shas[urls[1]]["head_sha"] = "missing"
    monkeypatch.setattr(run, "load_epoch", lambda *a, **k: replace(epoch, shas=shas))
    calls = []

    def ensure(*args):
        calls.append(args)
        if len(calls) == 1:
            raise OSError("temporary mirror failure")
        return fake_ensure_mirror(*args)

    drift_dir = invoke.pr_dir_name({"url": urls[2], **shas[urls[2]]})
    setup_dir = invoke.pr_dir_name({"url": urls[3], **shas[urls[3]]})

    def worktree(*args, **kwargs):
        if Path(args[4]).parent.name == drift_dir:
            raise mirrors.DriftError("pinned base changed")
        return fake_make_worktree(*args, **kwargs)

    def diff(worktree, *args):
        if Path(worktree).parent.name == setup_dir:
            raise OSError("auth setup file temporarily unavailable")
        return ""

    monkeypatch.setattr(run.mirrors, "ensure_mirror", ensure)
    monkeypatch.setattr(run.mirrors, "make_worktree", worktree)
    monkeypatch.setattr(run, "_compute_diff", diff)
    assert run.main(["--prs", ",".join(urls)]) == 1
    run_dir = next(run.RUNS_ROOT.iterdir())
    cp = checkpoint.Checkpoint(run_dir)
    assert cp.detail(urls[0])["reason"] == "mirror_error"
    assert cp.detail(urls[1])["reason"] == "incomplete_sha_entry"
    assert cp.status(urls[2]) == "drifted"
    assert not harness.launches
    assert cp.failed(urls) == [urls[0], urls[3]]
    monkeypatch.setattr(run, "_compute_diff", lambda *args: "")
    assert run.main(["--retry-failed", run_dir.name]) == 1
    assert len(harness.launches) == 2
    assert [cp.status(url) for url in urls] == ["ok", "failed", "drifted", "ok"]


@pytest.mark.parametrize("anchor", [None, "naive"])
@pytest.mark.parametrize("stage", ["environment", "launch"])
def test_retry_failed_retries_undispatched_invocation_failures(
    harness, monkeypatch, anchor, stage
):
    url = next(iter(run._load_json(run.GOLDEN_DIR / "shas.json")))
    calls = []
    original = invoke.build_env if stage == "environment" else subprocess.Popen

    def fail_once(*args, **kwargs):
        if stage == "environment" and not (Path(args[1]) / "run.json").is_file():
            return original(*args, **kwargs)
        calls.append(args)
        if len(calls) == 1:
            raise OSError("temporary auth setup failure")
        return original(*args, **kwargs)

    if stage == "environment":
        monkeypatch.setattr(invoke, "build_env", fail_once)
    else:
        monkeypatch.setattr(subprocess, "Popen", fail_once)
    args = ["--prs", url] + (["--anchor", anchor] if anchor else [])
    assert run.main(args) == 1
    run_dir = next(run.RUNS_ROOT.iterdir())
    cp = checkpoint.Checkpoint(run_dir)
    assert cp.failed([url]) == [url]
    assert not harness.launches
    assert run.main(["--retry-failed", run_dir.name]) == 0
    assert cp.status(url) == "ok"
    assert len(harness.launches) == 1


@pytest.mark.parametrize(
    "mode",
    [
        "stale",
        "prompt",
        "api",
        "uncorrelated",
        "other-tool",
        "workflow",
        "wrong-root",
        "wrong-version",
        "wrong-knob",
    ],
)
def test_untrusted_transcript_does_not_make_stop_retryable(harness, mode):
    harness.control["transcript"] = mode
    if mode == "wrong-root":
        harness.control["root"] = "/another/plugin"
    elif mode == "wrong-version":
        harness.control["version"] = "0.0.0"
    elif mode == "wrong-knob":
        harness.control["knobs"] = {"model_tier": "wrong"}
    url = next(iter(run._load_json(run.GOLDEN_DIR / "shas.json")))
    assert run.main(["--prs", url]) == 1
    run_dir = next((harness.workspace / "runs").iterdir())
    cp = checkpoint.Checkpoint(run_dir)
    assert cp.status(url) == "invalid"
    assert cp.detail(url)["reason"] == "config_echo_mismatch"
    assert cp.failed([url]) == []

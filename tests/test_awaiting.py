"""Wait outcomes, marker bytes, retry commands, and delivery failures."""

import io
import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from gauntlet import awaiting, tasks

from tests.support.artifacts import task_file

REPO = Path(__file__).resolve().parents[1]
ARTIFACT_NAMES = (
    "code-gauntlet-findings-abc.json",
    "code-gauntlet-report-abc.md",
    "code-gauntlet-post-review-abc.json",
    "code-gauntlet-checkpoint-all-abc.json",
)


@pytest.mark.parametrize(
    "environ,expected",
    [
        pytest.param({}, 540, id="TIMEOUT-default"),
        pytest.param({"BASH_MAX_TIMEOUT_MS": "300000"}, 240, id="TIMEOUT-headroom"),
        pytest.param({"BASH_MAX_TIMEOUT_MS": "1000"}, 30, id="TIMEOUT-floor"),
        pytest.param({"BASH_MAX_TIMEOUT_MS": "3600000"}, 540, id="TIMEOUT-generous"),
        pytest.param({"BASH_MAX_TIMEOUT_MS": "abc"}, 540, id="TIMEOUT-malformed"),
        pytest.param({"BASH_MAX_TIMEOUT_MS": "0"}, 540, id="TIMEOUT-nonpositive"),
        pytest.param({"BASH_MAX_TIMEOUT_MS": "300999.9"}, 240, id="TIMEOUT-decimal"),
    ],
)
def test_timeout_policy(environ, expected):
    assert awaiting.default_timeout_seconds(environ) == expected


def test_timeout_default_is_resolved_at_invocation_as_an_integer(monkeypatch, capsys):
    monkeypatch.setenv("BASH_MAX_TIMEOUT_MS", "600000")
    with patch("gauntlet.awaiting.time.time", side_effect=[0, 600, 600]):
        assert awaiting.CLI.invoke(["bare-id", "--since-epoch", "0"]) == 3
    marker = json.loads(capsys.readouterr().out)
    assert "--timeout-seconds 540 " in marker["next_command"]
    monkeypatch.setenv("BASH_MAX_TIMEOUT_MS", "300000")
    with patch("gauntlet.awaiting.time.time", side_effect=[0, 300, 300]):
        assert awaiting.CLI.invoke(["bare-id", "--since-epoch", "0"]) == 3
    marker = json.loads(capsys.readouterr().out)
    assert "--timeout-seconds 240 " in marker["next_command"]


@pytest.mark.parametrize(
    "text,expected",
    [
        pytest.param(
            '{"result":{"ok":true,"stats":{},"label":"caf\\u00e9"}}',
            '{"ok":true,"stats":{},"label":"caf\\u00e9"}',
            id="OUTCOME-success-ascii",
        ),
        pytest.param(
            '{"result":{"ok":false,"failingPhase":"args"}}',
            '{"ok":false,"failingPhase":"args"}',
            id="OUTCOME-failure-is-terminal",
        ),
    ],
)
def test_terminal_delivery(text, expected, tmp_path, capsys):
    path = tmp_path / "task.output"
    path.write_text(text, encoding="utf-8")
    assert awaiting.CLI.invoke([str(path), "--timeout-seconds", "0"]) == 0
    captured = capsys.readouterr()
    assert captured.out.strip() == expected
    assert len(captured.out.splitlines()) == 1
    assert captured.err == ""


@pytest.mark.parametrize(
    "attempt,maximum,code,kind",
    [
        pytest.param(3, 4, 3, "pending", id="OUTCOME-below-max"),
        pytest.param(4, 4, 4, "timeout", id="OUTCOME-at-max"),
        pytest.param(9, 4, 4, "timeout", id="OUTCOME-past-max"),
        pytest.param(1, 0, 4, "timeout", id="OUTCOME-zero-max"),
    ],
)
def test_attempt_bounds(attempt, maximum, code, kind, monkeypatch, capsys):
    monkeypatch.setattr(awaiting.time, "time", lambda: 10)
    assert (
        awaiting.CLI.invoke(
            [
                "missing",
                "--timeout-seconds",
                "0",
                "--attempt",
                str(attempt),
                "--max-attempts",
                str(maximum),
            ]
        )
        == code
    )
    marker = json.loads(capsys.readouterr().out)
    assert marker["await"] == kind
    assert ("next_command" in marker) == (kind == "pending")
    assert ("gap" in marker) == (kind == "timeout")


@pytest.mark.parametrize(
    "mode,expected_code,expected_sleeps",
    [
        pytest.param("appears", 0, [1, 1], id="OUTCOME-mid-wait"),
        pytest.param("deadline", 3, [2, 0.5], id="OUTCOME-clipped-sleep-sticky-bare"),
    ],
)
def test_wait_loop(mode, expected_code, expected_sleeps, tmp_path, monkeypatch):
    path = tmp_path / "task.output"
    path.write_text('{"ok":true}', encoding="utf-8")
    now, sleeps = [0.0], []
    monkeypatch.setattr(awaiting.time, "time", lambda: now[0])

    def sleep(seconds):
        sleeps.append(seconds)
        now[0] += seconds
        path.write_text(
            '{"ok":true,"stats":{}}' if mode == "appears" and len(sleeps) == 2 else "",
            encoding="utf-8",
        )

    monkeypatch.setattr(awaiting.time, "sleep", sleep)
    args = awaiting.build_parser().parse_args(
        [
            str(path),
            "--timeout-seconds",
            "2.5",
            "--poll-interval",
            "1" if mode == "appears" else "2",
        ]
    )
    receipt, code = awaiting.await_terminal(args, tasks.TaskRoots((), None))
    assert code == expected_code
    assert sleeps == expected_sleeps
    if mode == "appears":
        assert receipt == {"ok": True, "stats": {}}
    else:
        assert receipt["saw_ok_without_corroborator"] is True
        assert receipt["waited_seconds"] == 2.5


@pytest.mark.parametrize(
    "kind,attempt,expected_tail",
    [
        pytest.param(
            "pending",
            1,
            ',"next_command":"python3 ENTRY --attempt 2 --max-attempts 4 --timeout-seconds 0.0 --poll-interval 2 --since-epoch 7.0 -- missing"}',
            id="MARKER-pending-unresolved",
        ),
        pytest.param(
            "timeout",
            4,
            ',"gap":"workflow-timeout","detail":"no terminal workflow result after 4 attempts; declare the gap and deliver whatever partial artifacts exist"}',
            id="MARKER-timeout-detail",
        ),
    ],
)
def test_marker_bytes(kind, attempt, expected_tail, tmp_path, monkeypatch, capsys):
    root = tmp_path / "missing-root"
    monkeypatch.setenv(tasks.TASK_ROOTS_ENV, str(root))
    ticks = iter([10.0, 10.0, 10.26])
    monkeypatch.setattr(awaiting.time, "time", lambda: next(ticks))
    assert awaiting.CLI.invoke(
        [
            "missing",
            "--timeout-seconds",
            "0",
            "--since-epoch",
            "7",
            "--attempt",
            str(attempt),
        ]
    ) == (3 if kind == "pending" else 4)
    captured = capsys.readouterr()
    pattern = os.path.join(str(root), "*", "*", "tasks", "missing.output")
    expected = (
        (
            '{"await":"KIND","attempt":ATTEMPT,"max_attempts":4,"waited_seconds":0.3,'
            '"target":"missing","resolved_path":null,"file_bytes":null,"since_epoch":7.0,'
            '"artifacts":{"checked":false,"complete":false,"present":[],"missing":[]},'
            '"saw_ok_without_corroborator":false,"scan_skipped":false,"scan_stop_reason":null,'
            '"searched":[PATTERN]'
        )
        .replace("KIND", kind)
        .replace("ATTEMPT", str(attempt))
        .replace("PATTERN", json.dumps(pattern))
    )
    expected += expected_tail.replace(
        "ENTRY", shlex.quote(str(REPO / "scripts/await_workflow.py"))
    )
    assert captured.out.strip() == expected
    assert captured.err == ""


def test_scan_reason_marker(tmp_path, monkeypatch, capsys):
    target = tmp_path / "task.output"
    target.write_text('{"x":\n' * 2001, encoding="utf-8")
    monkeypatch.setattr(awaiting.time, "time", lambda: 0)
    assert awaiting.CLI.invoke([str(target), "--timeout-seconds", "0"]) == 3
    marker = json.loads(capsys.readouterr().out)
    assert marker["scan_skipped"] is True
    assert marker["scan_stop_reason"] == "max_probes"
    assert "searched" not in marker


@pytest.mark.parametrize(
    "channel",
    [
        pytest.param("return", id="MARKER-elide-return"),
        pytest.param("other", id="MARKER-elide-every-dict-channel"),
    ],
)
def test_elision(channel, tmp_path, capsys):
    target = tmp_path / "task.output"
    terminal = {
        "ok": True,
        "stats": {"n": 2},
        "unknown": "kept",
        "persistReturn": {
            "channel": channel,
            "nonce": "n",
            "planPath": "/out/plan",
            "unknown": 7,
            "entries": [{"path": "/out/a", "text": "x" * 5000}, 2, {"text": "no path"}],
        },
    }
    target.write_text(json.dumps({"result": terminal}), encoding="utf-8")
    assert awaiting.CLI.invoke([str(target), "--timeout-seconds", "0"]) == 0
    expected = (
        '{"ok":true,"stats":{"n":2},"unknown":"kept","persistReturn":'
        '{"channel":"CHANNEL","nonce":"n","planPath":"/out/plan","unknown":7,'
        '"elided":true,"resolvedPath":PATH,"paths":["/out/a",null]}}'
    )
    assert capsys.readouterr().out.strip() == expected.replace(
        "CHANNEL", channel
    ).replace("PATH", json.dumps(str(target)))
    assert terminal["persistReturn"]["entries"][0]["text"] == "x" * 5000


@pytest.mark.parametrize(
    "mode,expected_code,kind",
    [
        pytest.param("unresolved", 5, "artifacts_only", id="GRACE-unresolved"),
        pytest.param("resolved", 3, "pending", id="GRACE-resolved-nonfinal"),
        pytest.param("final", 5, "artifacts_only", id="GRACE-resolved-final"),
        pytest.param("terminal", 0, "terminal", id="GRACE-terminal-wins"),
        pytest.param("stale", 4, "timeout", id="GRACE-stale"),
        pytest.param("wrong-head", 4, "timeout", id="GRACE-wrong-head"),
    ],
)
def test_artifact_fallback(mode, expected_code, kind, tmp_path, monkeypatch, capsys):
    for name in ARTIFACT_NAMES:
        path = tmp_path / name
        path.write_text("x", encoding="utf-8")
        os.utime(path, (6 if mode == "stale" else 7, 6 if mode == "stale" else 7))
    target = "missing"
    if mode in ("resolved", "final", "terminal"):
        path = tmp_path / "task.output"
        path.write_text(
            '{"ok":true,"stats":{}}' if mode == "terminal" else "", encoding="utf-8"
        )
        target = str(path)
    monkeypatch.setattr(awaiting.time, "time", lambda: 10)
    code = awaiting.CLI.invoke(
        [
            target,
            "--timeout-seconds",
            "0",
            "--since-epoch",
            "7",
            "--artifacts-dir",
            str(tmp_path),
            "--head-sha",
            "other" if mode == "wrong-head" else "abc",
            "--artifacts-grace-seconds",
            "0",
            "--attempt",
            "4" if mode in ("final", "stale", "wrong-head") else "1",
        ]
    )
    assert code == expected_code
    receipt = json.loads(capsys.readouterr().out)
    if kind == "terminal":
        assert receipt == {"ok": True, "stats": {}}
    else:
        assert receipt["await"] == kind
    if code == 5:
        assert receipt["artifactPaths"] == {
            "findings": str(tmp_path / "code-gauntlet-findings-abc.json"),
            "report": str(tmp_path / "code-gauntlet-report-abc.md"),
            "checkpoints": str(tmp_path / "code-gauntlet-checkpoint-all-abc.json"),
            "postReview": str(tmp_path / "code-gauntlet-post-review-abc.json"),
        }
        assert (
            receipt["detail"]
            == "every persisted artifact is present and fresh, but the workflow's compact return was never observed; deliver from the marker's artifactPaths and disclose the gap"
        )
        assert receipt["gap"] == "workflow-timeout"
        assert receipt["artifacts"] == {
            "checked": True,
            "complete": True,
            "present": list(ARTIFACT_NAMES),
            "missing": [],
        }


@pytest.mark.parametrize(
    "defect",
    [
        pytest.param("missing", id="GRACE-missing-file"),
        pytest.param("empty", id="GRACE-empty-file"),
        pytest.param("directory", id="GRACE-directory"),
        pytest.param("unrelated", id="GRACE-wrong-directory"),
    ],
)
def test_incomplete_artifacts(defect, tmp_path, monkeypatch, capsys):
    for name in ARTIFACT_NAMES:
        (tmp_path / name).write_text("x", encoding="utf-8")
        os.utime(tmp_path / name, (7, 7))
    bad = tmp_path / ARTIFACT_NAMES[0]
    if defect in ("missing", "directory"):
        bad.unlink()
        if defect == "directory":
            bad.mkdir()
    elif defect == "empty":
        bad.write_text("", encoding="utf-8")
    artifact_dir = tmp_path / "elsewhere" if defect == "unrelated" else tmp_path
    monkeypatch.setattr(awaiting.time, "time", lambda: 10)
    assert (
        awaiting.CLI.invoke(
            [
                "missing",
                "--timeout-seconds",
                "0",
                "--attempt",
                "4",
                "--since-epoch",
                "7",
                "--artifacts-dir",
                str(artifact_dir),
                "--head-sha",
                "abc",
            ]
        )
        == 4
    )
    marker = json.loads(capsys.readouterr().out)
    assert marker["artifacts"] == {
        "checked": True,
        "complete": False,
        "present": [] if defect == "unrelated" else list(ARTIFACT_NAMES[1:]),
        "missing": list(ARTIFACT_NAMES)
        if defect == "unrelated"
        else [ARTIFACT_NAMES[0]],
    }


def test_grace_resets_after_completeness_loss(tmp_path, monkeypatch):
    for name in ARTIFACT_NAMES:
        (tmp_path / name).write_text("x", encoding="utf-8")
        os.utime(tmp_path / name, (7, 7))
    now, sleeps = [10.0], []
    monkeypatch.setattr(awaiting.time, "time", lambda: now[0])

    def sleep(seconds):
        sleeps.append(seconds)
        now[0] += seconds
        bad = tmp_path / ARTIFACT_NAMES[0]
        if len(sleeps) == 1:
            bad.unlink()
        elif len(sleeps) == 2:
            bad.write_text("x", encoding="utf-8")
            os.utime(bad, (7, 7))

    monkeypatch.setattr(awaiting.time, "sleep", sleep)
    args = awaiting.build_parser().parse_args(
        [
            "missing",
            "--timeout-seconds",
            "10",
            "--poll-interval",
            "1",
            "--since-epoch",
            "7",
            "--artifacts-grace-seconds",
            "2",
            "--artifacts-dir",
            str(tmp_path),
            "--head-sha",
            "abc",
        ]
    )
    receipt, code = awaiting.await_terminal(args, tasks.TaskRoots((), None))
    assert code == 5
    assert sleeps == [1, 1, 1, 1]
    assert receipt["waited_seconds"] == 4.0


@pytest.mark.parametrize(
    "resolved",
    [
        pytest.param(False, id="NEXT-literal-quoted-target"),
        pytest.param(True, id="NEXT-resolved-path-precedence"),
    ],
)
def test_retry_command(resolved, tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(awaiting.time, "time", lambda: 10)
    monkeypatch.setattr(
        awaiting, "entry", lambda _: "plugin path's/scripts/await_workflow.py"
    )
    target = "-space '[$].output"
    if resolved:
        root = tmp_path / "root"
        path = task_file(root, "bare-id")
        monkeypatch.setenv(tasks.TASK_ROOTS_ENV, str(root))
        target = "bare-id"
    assert (
        awaiting.CLI.invoke(
            [
                "--timeout-seconds",
                "0",
                "--attempt",
                "2",
                "--max-attempts",
                "5",
                "--since-epoch",
                "7.5",
                "--artifacts-dir",
                "out space's",
                "--head-sha",
                "abc",
                "--artifacts-grace-seconds",
                "11",
                "--",
                target,
            ]
        )
        == 3
    )
    expected = (
        "python3 'plugin path'\"'\"'s/scripts/await_workflow.py' --attempt 3 --max-attempts 5 "
        "--timeout-seconds 0.0 --poll-interval 2 --since-epoch 7.5 "
        "--artifacts-dir 'out space'\"'\"'s' --head-sha abc --artifacts-grace-seconds 11.0 -- "
    )
    expected += shlex.quote(str(path)) if resolved else "'-space '\"'\"'[$].output'"
    assert json.loads(capsys.readouterr().out)["next_command"] == expected


def test_next_command_keeps_symlinked_plugin_root(tmp_path, symlink_or_skip):
    link = tmp_path / "plugin-link"
    link.symlink_to(REPO, target_is_directory=True)
    target = tmp_path / "pending-task"
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env["PYTHONSAFEPATH"] = "1"
    result = subprocess.run(
        [
            sys.executable,
            str(link / "scripts/await_workflow.py"),
            str(target),
            "--timeout-seconds",
            "0",
            "--max-attempts",
            "2",
            "--since-epoch",
            "0",
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert result.returncode == 3, result.stderr
    marker = json.loads(result.stdout)
    assert shlex.split(marker["next_command"])[1] == str(
        link / "scripts" / "await_workflow.py"
    )


def test_executed_retry_preserves_leading_dash_and_quotes(
    tmp_path, monkeypatch, capsys
):
    shell = shutil.which("bash")
    if shell is None:
        pytest.skip("retry command requires a POSIX shell")
    monkeypatch.chdir(tmp_path)
    target = "-weird name'quote.output"
    (tmp_path / target).write_text("", encoding="utf-8")
    assert (
        awaiting.CLI.invoke(
            ["--timeout-seconds", "0", "--since-epoch", "7", "--", target]
        )
        == 3
    )
    command = json.loads(capsys.readouterr().out)["next_command"]
    result = subprocess.run(
        [shell, "-c", command],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=10,
        check=False,
    )
    assert result.returncode == 3, result.stderr
    marker = json.loads(result.stdout.strip())
    assert marker["target"] == target
    assert marker["attempt"] == 2
    assert marker["since_epoch"] == 7.0


@pytest.mark.parametrize(
    "error,message",
    [
        pytest.param(
            RuntimeError("boom"), "RuntimeError: boom", id="FAULT-wait-exception"
        ),
        pytest.param(KeyboardInterrupt(), "interrupted", id="FAULT-interrupted"),
        pytest.param(
            OSError("discovery failed"),
            "OSError: discovery failed",
            id="FAULT-discovery",
        ),
    ],
)
def test_wait_fault_receipt(error, message, monkeypatch, capsys):
    def fail(*args):
        raise error

    if isinstance(error, OSError):
        monkeypatch.setattr(tasks, "roots_from_environment", fail)
    else:
        monkeypatch.setattr(awaiting, "await_terminal", fail)
    assert awaiting.CLI.invoke(["missing", "--timeout-seconds", "0"]) == 4
    assert capsys.readouterr().out.strip() == (
        '{"await":"error","gap":"workflow-timeout","message":"MESSAGE",'
        '"target":"missing","attempt":1,"max_attempts":4}'
    ).replace("MESSAGE", message)


@pytest.mark.parametrize(
    "outcome",
    [0, 3, 4, 5],
    ids=[
        "FAULT-encode-terminal",
        "FAULT-encode-pending",
        "FAULT-encode-timeout",
        "FAULT-encode-artifacts",
    ],
)
def test_await_encoding_fallback_keeps_outcome(outcome, monkeypatch, capsys):
    monkeypatch.setattr(awaiting, "await_terminal", lambda *_: ({"bad": {1}}, outcome))
    assert awaiting.CLI.invoke(["bare-id", "--timeout-seconds", "0"]) == outcome
    assert capsys.readouterr().out.strip() == (
        '{"await":"error","gap":"workflow-timeout",'
        '"message":"result would not serialize: Object of type set is not JSON serializable"}'
    )


@pytest.mark.parametrize(
    "failures",
    [(RuntimeError,), (RecursionError,), (TypeError, ValueError)],
    ids=["FAULT-runtime-encoder", "FAULT-recursion-encoder", "FAULT-fallback-encoder"],
)
def test_await_encoder_exceptions_keep_original_scope(failures, monkeypatch, capsys):
    pending = iter(failures)

    def fail(*args, **kwargs):
        raise next(pending)("encoding failed")

    monkeypatch.setattr(awaiting, "await_terminal", lambda *_: ({"ok": True}, 5))
    monkeypatch.setattr(json, "dumps", fail)
    with pytest.raises(failures[-1], match=r"^encoding failed$"):
        awaiting.CLI.invoke(["bare-id", "--timeout-seconds", "0"])
    assert capsys.readouterr().out.strip() == ""


def test_await_preserves_compact_ascii_nan_spelling(monkeypatch, capsys):
    monkeypatch.setattr(
        awaiting,
        "await_terminal",
        lambda *_: ({"ok": True, "stats": float("nan"), "label": "caf\u00e9"}, 0),
    )
    assert awaiting.CLI.invoke(["bare-id", "--timeout-seconds", "0"]) == 0
    assert (
        capsys.readouterr().out.strip()
        == '{"ok":true,"stats":NaN,"label":"caf\\u00e9"}'
    )


@pytest.mark.parametrize(
    "site", ["print", "flush"], ids=["FAULT-output", "FAULT-flush"]
)
def test_output_failure(site, monkeypatch):
    def fail(*args, **kwargs):
        raise BrokenPipeError("closed")

    stream = io.StringIO()
    monkeypatch.setattr(sys, "stdout", stream)

    def wait(*args):
        if site == "print":
            raise KeyboardInterrupt()
        return {"ok": True}, 0

    monkeypatch.setattr(awaiting, "await_terminal", wait)
    monkeypatch.setattr(os, "open", fail)
    if site == "flush":
        monkeypatch.setattr(stream, "flush", fail)
    else:
        monkeypatch.setattr("builtins.print", fail)
    assert awaiting.CLI.invoke(["missing", "--timeout-seconds", "0"]) == 4


def test_real_broken_pipe(tmp_path):
    target = tmp_path / "task.output"
    target.write_text(
        '{"ok":true,"stats":{"pad":"' + "x" * 2_000_000 + '"}}', encoding="utf-8"
    )
    reader = subprocess.Popen(
        [sys.executable, "-c", "import sys; sys.stdin.readline(20)"],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
    )
    writer = subprocess.Popen(
        [
            sys.executable,
            str(REPO / "scripts/await_workflow.py"),
            "--timeout-seconds",
            "0",
            "--",
            str(target),
        ],
        stdout=reader.stdin,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )
    assert reader.stdin is not None
    reader.stdin.close()
    _, stderr = writer.communicate(timeout=10)
    reader.wait(timeout=10)
    assert writer.returncode in (0, 4), stderr
    assert "BrokenPipeError" not in stderr
    assert "Traceback" not in stderr


@pytest.mark.parametrize(
    "flags",
    [["--artifacts-dir", "out"], ["--head-sha", "abc"]],
    ids=["USAGE-directory-only", "USAGE-sha-only"],
)
def test_paired_flags(flags, capsys):
    assert awaiting.CLI.invoke(["task", *flags]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert (
        captured.err
        == "await_workflow: --artifacts-dir and --head-sha must be given together\n"
    )


def test_explicit_await_path_never_probes_discovery_roots(
    tmp_path, monkeypatch, capsys
):
    target = tmp_path / "task.output"
    target.write_text('{"ok":true,"stats":{}}', encoding="utf-8")
    monkeypatch.setattr(
        os.path, "realpath", lambda *_: pytest.fail("explicit path consulted roots")
    )
    assert awaiting.CLI.invoke([str(target), "--timeout-seconds", "0"]) == 0
    assert capsys.readouterr().out.strip() == '{"ok":true,"stats":{}}'


def test_directory_target_retains_file_bytes(tmp_path, monkeypatch, capsys):
    target = tmp_path / "directory.output"
    target.mkdir()
    getsize = os.path.getsize
    monkeypatch.setattr(
        os.path, "getsize", lambda path: 37 if path == str(target) else getsize(path)
    )
    assert awaiting.CLI.invoke([str(target), "--timeout-seconds", "0"]) == 3
    marker = json.loads(capsys.readouterr().out)
    assert marker["file_bytes"] == 37
    assert marker["resolved_path"] == str(target)

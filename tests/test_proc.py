"""Shared command execution and missing-tool behavior."""

import subprocess
import sys

import pytest
from gauntlet import proc


def test_run_resolves_executable_and_preserves_original_arguments(monkeypatch):
    calls = []
    monkeypatch.setattr(proc.shutil, "which", lambda name: "/tools/git")

    def child(argv, **options):
        calls.append((argv, options))
        return subprocess.CompletedProcess(argv, 0, stdout="ok", stderr="")

    monkeypatch.setattr(proc.subprocess, "run", child)
    result = proc.run(["git", "status"], cwd="/repo", timeout=3, check=True)
    assert result.stdout == "ok"
    assert calls == [
        (
            ["/tools/git", "status"],
            {
                "cwd": "/repo",
                "timeout": 3,
                "text": True,
                "capture_output": True,
                "check": True,
                "encoding": "utf-8",
                "errors": "strict",
            },
        )
    ]


def test_missing_executable_raises_tool_error_before_child_runs(monkeypatch):
    monkeypatch.setattr(proc.shutil, "which", lambda _name: None)
    monkeypatch.setattr(
        proc.subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail("missing tool reached child"),
    )
    with pytest.raises(proc.ToolError) as caught:
        proc.run(["unavailable", "arg"])
    assert isinstance(caught.value, FileNotFoundError)
    assert caught.value.command == ("unavailable", "arg")
    assert caught.value.executable == "unavailable"
    assert caught.value.reason == "missing_tool"
    assert "'unavailable'" in str(caught.value)


def test_run_binary_output_and_text_replacement(monkeypatch):
    monkeypatch.setattr(proc.shutil, "which", lambda _name: sys.executable)
    raw = proc.run_bytes(
        [sys.executable, "-c", "import sys; sys.stdout.buffer.write(b'\\xff')"]
    )
    assert raw.stdout == b"\xff"
    decoded = proc.output(
        [sys.executable, "-c", "import sys; sys.stdout.buffer.write(b'\\xff')"],
        errors="replace",
    )
    assert decoded == ("\ufffd", "", 0)


def test_child_oserror_names_requested_executable(monkeypatch):
    monkeypatch.setattr(proc.shutil, "which", lambda _name: "/tools/git")

    def denied(*_args, **_options):
        raise PermissionError(13, "denied", "/tools/git")

    monkeypatch.setattr(proc.subprocess, "run", denied)
    with pytest.raises(PermissionError) as caught:
        proc.run(["git", "status"])
    assert caught.value.filename == "git"
    assert "'git'" in str(caught.value)


@pytest.mark.parametrize("failure", ["timeout", "checked_exit"])
def test_run_preserves_subprocess_failure_types(monkeypatch, failure):
    monkeypatch.setattr(proc.shutil, "which", lambda _name: "/tools/git")

    def child(argv, **_options):
        if failure == "timeout":
            raise subprocess.TimeoutExpired(argv, 1)
        raise subprocess.CalledProcessError(2, argv, stderr="failed")

    monkeypatch.setattr(proc.subprocess, "run", child)
    if failure == "timeout":
        with pytest.raises(subprocess.TimeoutExpired):
            proc.run(["git", "status"], timeout=1)
    else:
        with pytest.raises(subprocess.CalledProcessError) as caught:
            proc.run(["git", "status"], check=True)
        assert caught.value.cmd == ["git", "status"]
        assert caught.value.stderr == "failed"

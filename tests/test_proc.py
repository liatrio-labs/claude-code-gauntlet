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
    assert caught.value.filename == "unavailable"
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


def test_missing_cwd_keeps_directory_as_oserror_filename(monkeypatch, tmp_path):
    monkeypatch.setattr(proc.shutil, "which", lambda _name: "/tools/git")
    missing = tmp_path / "missing"
    with pytest.raises(FileNotFoundError) as caught:
        proc.run(["git", "status"], cwd=str(missing))
    assert caught.value.filename == str(missing)


def test_slash_command_reaches_child_unchanged(monkeypatch):
    calls = []
    monkeypatch.setattr(proc.shutil, "which", lambda _name: pytest.fail("PATH lookup"))

    def child(argv, **_options):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(proc.subprocess, "run", child)
    proc.run(["./tool", "arg"], cwd="/repo")
    assert calls == [["./tool", "arg"]]


def test_windows_which_uses_path_without_implicit_cwd(tmp_path, monkeypatch):
    monkeypatch.setattr(proc.sys, "platform", "win32")
    monkeypatch.chdir(tmp_path)
    (tmp_path / "git.cmd").write_text("", encoding="utf-8")
    tools = tmp_path / "tools"
    tools.mkdir()
    (tools / "git.EXE").write_text("", encoding="utf-8")
    monkeypatch.setenv("PATH", str(tools))
    monkeypatch.setenv("PATHEXT", ".EXE;.cmd")
    assert proc.which("git") == str(tools / "git.EXE")
    assert proc._resolve(["git", "status"]) == [str(tools / "git.EXE"), "status"]
    monkeypatch.setenv("PATH", str(tmp_path / "absent"))
    assert proc.which("git") is None


@pytest.mark.parametrize("failure", ["timeout", "checked_exit"])
def test_run_preserves_subprocess_failure_types(monkeypatch, failure):
    monkeypatch.setattr(proc.shutil, "which", lambda _name: "/tools/git")

    def child(argv, **_options):
        if failure == "timeout":
            raise subprocess.TimeoutExpired(argv, 1)
        raise subprocess.CalledProcessError(2, argv, stderr="failed")

    monkeypatch.setattr(proc.subprocess, "run", child)
    if failure == "timeout":
        with pytest.raises(subprocess.TimeoutExpired) as caught:
            proc.run(["git", "status"], timeout=1)
        assert caught.value.cmd == ["git", "status"]
    else:
        with pytest.raises(subprocess.CalledProcessError) as caught:
            proc.run(["git", "status"], check=True)
        assert caught.value.cmd == ["git", "status"]
        assert caught.value.stderr == "failed"

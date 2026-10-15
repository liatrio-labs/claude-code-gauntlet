"""Branch resolver lookup, refusals and checkout at the stdin boundary."""

import os
import subprocess
from pathlib import Path

import pytest

GIT_ENV = {
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_SYSTEM": os.devnull,
    "GIT_AUTHOR_NAME": "test",
    "GIT_AUTHOR_EMAIL": "test@example.invalid",
    "GIT_COMMITTER_NAME": "test",
    "GIT_COMMITTER_EMAIL": "test@example.invalid",
}


def _git(cwd: Path, *argv: str) -> str:
    return subprocess.run(
        ["git", *argv], cwd=cwd, capture_output=True, check=True, encoding="utf-8"
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    for key, value in GIT_ENV.items():
        monkeypatch.setenv(key, value)
    _git(tmp_path, "init", "-q")
    shas = {}
    for name in ("branch", "tag", "remote"):
        _git(
            tmp_path,
            "-c",
            "commit.gpgsign=false",
            "commit",
            "--allow-empty",
            "-qm",
            name,
        )
        shas[name] = _git(tmp_path, "rev-parse", "HEAD")
    _git(tmp_path, "branch", "topic", shas["branch"])
    _git(tmp_path, "tag", "topic", shas["tag"])
    _git(tmp_path, "update-ref", "refs/remotes/origin/feat", shas["remote"])
    _git(tmp_path, "branch", "dup", shas["branch"])
    _git(tmp_path, "update-ref", "refs/remotes/dup", shas["remote"])
    _git(tmp_path, "checkout", "-q", "--detach", shas["tag"])
    return shas


@pytest.mark.parametrize(
    "stdin,code,result",
    [
        (b"topic\n", 0, "branch"),
        (b"origin/feat\n", 0, "remote"),
        (b"one\ntwo\n", 2, "stdin must contain one non-empty line without NUL"),
        (b"unknown\n", 1, "no local or remote-tracking branch has that name"),
        (b"topic@{1}\n", 1, "not a valid branch name"),
        (b"dup\n", 1, "name matches both a local and a remote-tracking branch"),
    ],
)
def test_resolve_branch_reads_only_branch_refs(
    stdin, code, result, repo, tmp_path, invoke
):
    outcome = invoke("resolve_branch", [], tmp_path, stdin)
    assert outcome.returncode == code
    if code:
        assert outcome.stdout == b""
        assert outcome.stderr == f"resolve_branch: {result}\n".encode()
    else:
        assert outcome.stdout == f"{repo[result]}\n".encode()
        assert outcome.stderr == b""


@pytest.mark.parametrize(
    "name,head", [("topic", "refs/heads/topic"), ("origin/feat", None)]
)
def test_resolve_branch_checks_out_local_by_name_and_remote_detached(
    name, head, repo, tmp_path, invoke
):
    outcome = invoke("resolve_branch", ["--checkout"], tmp_path, f"{name}\n".encode())
    assert outcome.returncode == 0, outcome.stderr
    sha = outcome.stdout.decode().strip()
    assert _git(tmp_path, "rev-parse", "HEAD") == sha
    symbolic = subprocess.run(
        ["git", "symbolic-ref", "-q", "HEAD"],
        cwd=tmp_path,
        capture_output=True,
        encoding="utf-8",
    ).stdout.strip()
    assert (symbolic or None) == head

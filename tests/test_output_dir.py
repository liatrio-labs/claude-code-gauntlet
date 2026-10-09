"""Output directories are returned only after their ignore gate succeeds."""

import subprocess
from pathlib import Path

import pytest
from gauntlet import output_dir as eod


@pytest.fixture
def output_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
    return repo


@pytest.fixture
def output_cli(invoke, monkeypatch, output_repo):
    monkeypatch.delenv("CODE_GAUNTLET_OUTPUT_DIR", raising=False)

    def call(value=None, cwd=None, arguments=None):
        if value is not None:
            monkeypatch.setenv("CODE_GAUNTLET_OUTPUT_DIR", str(value))
        directory = cwd or output_repo
        return invoke(
            "ensure_output_dir", arguments or ["--cwd", str(directory)], directory
        )

    return call


def assert_created_ignored(result, repo, target):
    assert result.returncode == 0, result.stderr
    actual = Path(result.stdout.decode("utf-8").strip())
    assert actual == target.resolve()
    assert actual.is_dir()
    assert (
        subprocess.run(
            ["git", "check-ignore", "-q", "--", str(actual)],
            cwd=repo,
            capture_output=True,
        ).returncode
        == 0
    )


def test_nested_in_repo_override_pattern(output_cli, output_repo):
    result = output_cli("artifacts/out")
    assert_created_ignored(result, output_repo, output_repo / "artifacts" / "out")
    exclude = output_repo / ".git" / "info" / "exclude"
    assert exclude.read_bytes().endswith(b"/artifacts/out/\n")


def test_supplied_cwd_is_used_from_a_different_process_directory(
    invoke, output_repo, tmp_path
):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    result = invoke("ensure_output_dir", ["--cwd", str(output_repo)], elsewhere)

    target = output_repo / ".code-gauntlet"
    assert_created_ignored(result, output_repo, target)
    exclude = output_repo / ".git" / "info" / "exclude"
    assert b"/.code-gauntlet/" in exclude.read_bytes().splitlines()
    assert list(elsewhere.iterdir()) == []


def test_out_of_repo_skips_exclude(output_cli, output_repo, tmp_path):
    target = tmp_path / "outside" / "gauntlet-out"
    exclude = output_repo / ".git" / "info" / "exclude"
    before = exclude.read_bytes()
    result = output_cli(target)
    assert result.returncode == 0
    assert Path(result.stdout.decode().strip()) == target.resolve()
    assert target.is_dir()
    assert result.stderr.decode() == (
        f"outside-repo: skip exclude for {target.resolve()} (artifacts are outside the working tree)\n"
    )
    assert exclude.read_bytes() == before


def test_unwritable_exclude_hard_stop_no_mkdir(output_cli, output_repo):
    exclude = output_repo / ".git" / "info" / "exclude"
    exclude.write_text("# locked\n", encoding="utf-8")
    exclude.chmod(0o444)
    exclude.parent.chmod(0o555)
    try:
        result = output_cli()
        assert result.returncode == 1
        assert result.stdout == b""
        assert not (output_repo / ".code-gauntlet").exists()
        assert b"info/exclude unwritable" in result.stderr
        assert b"CODE_GAUNTLET_OUTPUT_DIR" in result.stderr
    finally:
        exclude.parent.chmod(0o755)
        exclude.chmod(0o644)


def test_verify_after_append_failure_no_mkdir(output_cli, output_repo, monkeypatch):
    def bad_append(exclude_path, _pattern):
        with Path(exclude_path).open("a", encoding="utf-8") as stream:
            stream.write("/definitely-not-the-right-pattern/\n")

    monkeypatch.setattr(eod, "append_exclude_pattern", bad_append)
    result = output_cli()
    assert result.returncode == 1
    assert result.stdout == b""
    assert not (output_repo / ".code-gauntlet").exists()
    assert b"but check-ignore still fails" in result.stderr


@pytest.mark.parametrize("value", ["root", "  "], ids=["repo-root", "empty-env"])
def test_rejected_output_override(value, output_cli, output_repo):
    result = output_cli(output_repo if value == "root" else value)
    assert result.returncode == 2
    assert result.stdout == b""
    assert not (output_repo / ".code-gauntlet").exists()
    if value == "root":
        assert b"output directory must not be the repo root" in result.stderr
    else:
        assert b"CODE_GAUNTLET_OUTPUT_DIR is empty or whitespace" in result.stderr


def test_check_ignore_128_is_usage_error(output_cli, output_repo, monkeypatch):
    monkeypatch.setattr(eod, "git_check_ignore", lambda _cwd, _path: 128)
    result = output_cli()
    assert result.returncode == 2
    assert result.stdout == b""
    assert result.stderr.decode().startswith(
        "ensure_output_dir: git check-ignore failed (exit 128) for "
    )
    assert not (output_repo / ".code-gauntlet").exists()


def test_mkdir_failure_exit_1_empty_stdout(output_cli, output_repo):
    blocker = output_repo / ".code-gauntlet"
    blocker.write_text("not a dir\n", encoding="utf-8")
    (output_repo / ".git" / "info" / "exclude").write_text(
        "/.code-gauntlet/\n", encoding="utf-8"
    )
    result = output_cli()
    assert result.returncode == 1
    assert result.stdout == b""
    assert result.stderr.decode().startswith(
        f"ensure_output_dir: mkdir failed for {blocker.resolve()}: "
    )


def test_worktree_uses_git_path_exclude(tmp_path, output_cli):
    # A .git indirection must use git's exclude path rather than a guessed .git directory.
    repo = tmp_path / "indirect"
    repo.mkdir()
    metadata = tmp_path / "metadata"
    subprocess.run(
        ["git", "init", "--separate-git-dir", str(metadata), str(repo)],
        check=True,
        capture_output=True,
    )
    assert (repo / ".git").is_file()
    result = output_cli(cwd=repo)
    assert_created_ignored(result, repo, repo / ".code-gauntlet")
    assert (metadata / "info" / "exclude").read_bytes().endswith(b"/.code-gauntlet/\n")


def test_exclude_without_trailing_newline(output_cli, output_repo):
    exclude = output_repo / ".git" / "info" / "exclude"
    exclude.write_bytes(b"# prior line without newline")
    result = output_cli()
    assert_created_ignored(result, output_repo, output_repo / ".code-gauntlet")
    assert exclude.read_bytes() == b"# prior line without newline\n/.code-gauntlet/\n"


def test_git_exclude_path_none_hard_stop_no_mkdir(output_cli, output_repo, monkeypatch):
    monkeypatch.setattr(eod, "git_exclude_path", lambda _cwd: None)
    result = output_cli()
    assert result.returncode == 1
    assert result.stdout == b""
    assert not (output_repo / ".code-gauntlet").exists()
    assert b"info/exclude unresolvable" in result.stderr


def test_not_a_git_repository_is_usage_error(output_cli, tmp_path):
    plain = tmp_path / "nongit"
    plain.mkdir()
    result = output_cli(cwd=plain)
    assert result.returncode == 2
    assert result.stdout == b""
    assert (
        result.stderr
        == b"ensure_output_dir: not a git repository (git rev-parse --show-toplevel failed)\n"
    )

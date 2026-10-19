"""Output directories are returned only after their ignore gate succeeds."""

import ntpath
import os
import posixpath
import subprocess
from pathlib import Path

import pytest
from gauntlet import output_dir as eod
from gauntlet.paths import wire_path


@pytest.fixture
def output_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
    return repo


@pytest.fixture(autouse=True)
def no_ambient_output_dir(monkeypatch):
    monkeypatch.delenv("CODE_GAUNTLET_OUTPUT_DIR", raising=False)


@pytest.fixture
def output_cli(invoke, monkeypatch, output_repo):
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
    assert result.stdout.decode("utf-8").strip() == target.resolve().as_posix()
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


@pytest.mark.parametrize(
    "characters",
    [
        "$",
        "`",
        '"',
        pytest.param(
            "\\", marks=pytest.mark.skipif(os.sep == "\\", reason="separator")
        ),
        "".join(map(chr, range(32))),
        "\x7f",
    ],
    ids=["dollar", "backtick", "double-quote", "backslash", "c0-controls", "del"],
)
def test_output_dir_refuses_shell_characters_before_side_effects(
    characters, invoke, monkeypatch, output_repo
):
    exclude = output_repo / ".git/info/exclude"
    before = exclude.read_bytes()
    entries = set(output_repo.iterdir())
    for character in characters:
        target = output_repo / f"unsafe{character}path" / "out"
        # NUL cannot enter a process environment, but the command boundary can see it.
        monkeypatch.setattr(
            eod.os,
            "environ",
            {**os.environ, "CODE_GAUNTLET_OUTPUT_DIR": str(target)},
        )
        result = invoke("ensure_output_dir", ["--cwd", str(output_repo)], output_repo)
        assert result.returncode == 2, (repr(character), result.stderr)
        assert result.stdout == b""
        assert b"$CODE_GAUNTLET_OUTPUT_DIR" in result.stderr
        assert exclude.read_bytes() == before
        assert set(output_repo.iterdir()) == entries


@pytest.mark.parametrize("source", ["repo-root", "symlink-target"])
def test_output_dir_checks_resolved_ancestors(
    source, output_cli, output_repo, tmp_path, symlink_or_skip
):
    unsafe = tmp_path / "unsafe$root"
    if source == "repo-root":
        # The output dir sits outside the repo, so only the repo-root check refuses.
        output_repo.rename(unsafe)
        repo, raw = unsafe, str(tmp_path / "out")
        target = tmp_path / "out"
    else:
        unsafe.mkdir()
        alias = output_repo / "alias"
        alias.symlink_to(unsafe, target_is_directory=True)
        repo, raw = output_repo, "alias/out"
        target = unsafe / "out"
    exclude = repo / ".git/info/exclude"
    before = exclude.read_bytes()
    result = output_cli(raw, cwd=repo)
    assert result.returncode == 2
    assert result.stdout == b""
    assert not target.exists()
    assert exclude.read_bytes() == before


def test_output_dir_allows_apostrophes_and_spaces(output_cli, output_repo):
    target = output_repo / "o'brien out[1]"
    assert_created_ignored(output_cli(target), output_repo, target)


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


@pytest.mark.parametrize("kind", ["separate-git-dir", "linked-worktree"])
def test_worktree_uses_git_path_exclude(kind, tmp_path, output_cli, output_repo):
    # A .git indirection must use git's exclude path rather than a guessed .git directory.
    repo = tmp_path / "indirect"
    if kind == "separate-git-dir":
        repo.mkdir()
        metadata = tmp_path / "metadata"
        subprocess.run(
            ["git", "init", "--separate-git-dir", str(metadata), str(repo)],
            check=True,
            capture_output=True,
        )
    else:
        metadata = output_repo / ".git"
        try:
            subprocess.run(
                [
                    "git",
                    "-c",
                    "user.name=Test",
                    "-c",
                    "user.email=test@example.invalid",
                    "commit",
                    "--allow-empty",
                    "-m",
                    "test data",
                ],
                cwd=output_repo,
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["git", "worktree", "add", "--detach", str(repo)],
                cwd=output_repo,
                check=True,
                capture_output=True,
            )
        except (OSError, subprocess.CalledProcessError) as exc:
            pytest.skip(f"linked worktrees unavailable: {exc}")
    assert (repo / ".git").is_file()
    result = output_cli(cwd=repo)
    assert_created_ignored(result, repo, repo / ".code-gauntlet")
    assert (metadata / "info" / "exclude").read_bytes().endswith(b"/.code-gauntlet/\n")


def test_already_ignored_output_keeps_exclude_bytes(output_cli, output_repo):
    (output_repo / ".gitignore").write_bytes(b"/.code-gauntlet/\n")
    exclude = output_repo / ".git" / "info" / "exclude"
    before = exclude.read_bytes()
    result = output_cli()
    assert_created_ignored(result, output_repo, output_repo / ".code-gauntlet")
    assert exclude.read_bytes() == before
    assert result.stderr.decode() == (
        f"exclude: already-ignored {(output_repo / '.code-gauntlet').resolve()}\n"
    )


@pytest.mark.usefixtures("symlink_or_skip")
def test_outside_alias_into_repo_is_ignored(output_cli, output_repo, tmp_path):
    nested = output_repo / "nested"
    nested.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(nested, target_is_directory=True)
    result = output_cli(alias / "out")
    assert_created_ignored(result, output_repo, nested / "out")
    assert (output_repo / ".git/info/exclude").read_bytes().endswith(b"/nested/out/\n")
    assert b"outside-repo" not in result.stderr


def test_missing_info_directory_is_created(output_cli, output_repo):
    info = output_repo / ".git" / "info"
    (info / "exclude").unlink()
    info.rmdir()
    result = output_cli()
    assert_created_ignored(result, output_repo, output_repo / ".code-gauntlet")
    assert (info / "exclude").read_bytes() == b"/.code-gauntlet/\n"


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


def test_missing_cwd_reports_one_git_error_line(invoke, tmp_path):
    missing = tmp_path / "missing-cwd"
    result = invoke("ensure_output_dir", ["--cwd", str(missing)], tmp_path)

    assert result.returncode == 1
    assert result.stdout == b""
    prefix = f"ensure_output_dir: cannot run git in {missing.resolve()}: "
    stderr = result.stderr.decode("utf-8")
    assert stderr.startswith(prefix)
    assert stderr.endswith("\n")
    lines = stderr.splitlines()
    assert len(lines) == 1
    assert lines[0][len(prefix) :]


@pytest.mark.parametrize(
    "path_module,raw,expected",
    [
        (posixpath, r"/tmp/a\b", r"/tmp/a\b"),
        (ntpath, r"D:\Work\repo", "D:/Work/repo"),
        (ntpath, r"\\host\share\repo", None),
        (ntpath, "//host/share/repo", None),
    ],
)
def test_wire_roots(path_module, raw, expected):
    if expected is None:
        with pytest.raises(
            ValueError, match="UNC roots are unsupported; use a mapped drive"
        ):
            wire_path(raw, separator=path_module.sep)
    else:
        assert wire_path(raw, separator=path_module.sep) == expected

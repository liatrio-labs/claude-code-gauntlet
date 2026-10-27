"""Patch producers ignore user formatting and refuse unusable inputs."""

import json
import subprocess

import pytest

from tests.support.git_diff import assert_standard_patch, patch_repo


@pytest.mark.parametrize("config", ["external", "mnemonic", "noprefix"])
@pytest.mark.parametrize(
    "mode", ["local", "branch", "incremental", "verify", "fallback"]
)
def test_patch_consumers_ignore_user_diff_config(
    config, mode, tmp_path, monkeypatch, invoke
):
    from gauntlet import proc
    from gauntlet.verify.decide import VerifyContext, get_diff

    base, target = patch_repo(tmp_path, monkeypatch, config)
    monkeypatch.chdir(tmp_path)
    if mode in {"local", "branch"}:
        destination = tmp_path / "o'brien patch"
        result = invoke(
            "save_git_diff",
            [
                "--git-range",
                "HEAD" if mode == "local" else f"{base}...{target}",
                "--output",
                str(destination),
            ],
            tmp_path,
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout == result.stderr == b""
        patch = destination.read_bytes()
    elif mode == "incremental":
        files = tmp_path / "files.json"
        files.write_text(json.dumps(["file"]), encoding="utf-8")
        result = invoke(
            "incremental_diff", ["--files", str(files), "--since", base], tmp_path
        )
        assert result.returncode == 0, result.stderr
        patch = result.stdout
    else:
        output = proc.output
        if mode == "fallback":

            def without_merge_base(argv, **kwargs):
                if argv[-1].endswith("...HEAD"):
                    return "", "no merge base", 1
                return output(argv, **kwargs)

            monkeypatch.setattr(proc, "output", without_merge_base)
        content = get_diff(VerifyContext(str(tmp_path), base, target_kind="branch"))
        assert content is not None
        patch = content.encode()
    assert_standard_patch(patch)
    assert (b"+local\n" in patch) == (mode == "local")


@pytest.mark.parametrize(
    "revision_range",
    ["--output=sentinel", "HEAD --stat", "HEAD~1", "a" * 40 + "...--stat"],
)
def test_patch_refuses_ranges_before_running_git(
    revision_range, tmp_path, monkeypatch, invoke
):
    from gauntlet import proc

    def forbidden(*args, **kwargs):
        pytest.fail("invalid range reached git")

    monkeypatch.setattr(proc, "run_bytes", forbidden)
    destination = tmp_path / "patch"
    result = invoke(
        "save_git_diff",
        [f"--git-range={revision_range}", "--output", str(destination)],
        tmp_path,
    )
    assert result.returncode == 2
    assert result.stdout == b""
    assert b"--git-range must be HEAD or <base_sha>...<target_sha>" in result.stderr
    assert not destination.exists()


@pytest.mark.parametrize(
    "outcome", ["empty", "nonpatch", "git-error", "write-error", "os-error", "timeout"]
)
def test_patch_refuses_unusable_output(outcome, tmp_path, monkeypatch, invoke):
    from gauntlet import proc

    base, _ = patch_repo(tmp_path, monkeypatch)
    revision_range = f"{base}...{base}" if outcome == "empty" else "HEAD"
    if outcome in {"os-error", "timeout", "nonpatch"}:

        def run(*args, **kwargs):
            if outcome == "os-error":
                raise OSError("unavailable")
            if outcome == "timeout":
                raise proc.TimeoutExpired("git", 180)
            return subprocess.CompletedProcess(args[0], 0, b"unexpected\n", b"")

        monkeypatch.setattr(proc, "run_bytes", run)
    if outcome == "git-error":
        revision_range = "a" * 40 + "..." + "b" * 40
    destination = tmp_path if outcome == "write-error" else tmp_path / "patch"
    result = invoke(
        "save_git_diff",
        ["--git-range", revision_range, "--output", str(destination)],
        tmp_path,
    )
    assert result.returncode == 1
    assert result.stdout == b""
    assert result.stderr.startswith(b"save_git_diff: ")
    assert destination.is_dir() or not destination.exists()

"""Shared listing behavior exercised through the two shipped consumers."""

import json
import os
import subprocess

import pytest

from tests.support.pr_files import FIXTURES, fake_files_forge, github_view, gitlab_view


def _git(directory, *args):
    return subprocess.run(
        ["git", *args],
        cwd=directory,
        env=dict(
            os.environ,
            GIT_CONFIG_GLOBAL=os.devnull,
            GIT_CONFIG_SYSTEM=os.devnull,
            GIT_AUTHOR_NAME="test",
            GIT_AUTHOR_EMAIL="test@example.invalid",
            GIT_COMMITTER_NAME="test",
            GIT_COMMITTER_EMAIL="test@example.invalid",
        ),
        capture_output=True,
        check=True,
        encoding="utf-8",
    ).stdout.strip()


@pytest.fixture
def files_repo(tmp_path):
    _git(tmp_path, "init", "-q")
    (tmp_path / "nested").mkdir()
    for name in ("modified.txt", "nested/path with space.txt"):
        (tmp_path / name).write_text("before\n", encoding="utf-8")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "-c", "commit.gpgsign=false", "commit", "-qm", "base")
    return tmp_path


def test_git_local_range_emits_json_paths(files_repo, invoke):
    paths = ["modified.txt", "nested/path with space.txt"]
    for name in paths:
        (files_repo / name).write_text("after\n", encoding="utf-8")
    _git(files_repo, "add", "modified.txt")
    assert _git(files_repo, "ls-files", "--others", "--exclude-standard") == ""
    result = invoke("list_pr_files", ["--git-range", "HEAD"], files_repo)
    assert result.returncode == 0, result.stderr
    assert result.stdout == (json.dumps(paths) + "\n").encode("utf-8")
    assert result.stderr == b""


def test_git_branch_range_uses_commits_not_working_tree(files_repo, invoke):
    base = _git(files_repo, "rev-parse", "HEAD")
    (files_repo / "nested/path with space.txt").write_text("target\n", encoding="utf-8")
    _git(files_repo, "add", ".")
    _git(files_repo, "-c", "commit.gpgsign=false", "commit", "-qm", "target")
    target = _git(files_repo, "rev-parse", "HEAD")
    _git(files_repo, "checkout", "-q", "--detach", base)
    (files_repo / "modified.txt").write_text("base side\n", encoding="utf-8")
    _git(files_repo, "add", ".")
    _git(files_repo, "-c", "commit.gpgsign=false", "commit", "-qm", "base side")
    base = _git(files_repo, "rev-parse", "HEAD")
    (files_repo / "modified.txt").write_text("local\n", encoding="utf-8")
    result = invoke("list_pr_files", ["--git-range", f"{base}...{target}"], files_repo)
    assert result.returncode == 0, result.stderr
    assert result.stdout == b'["nested/path with space.txt"]\n'
    assert result.stderr == b""


@pytest.mark.parametrize(
    "revision_range",
    ["--output=sentinel", "HEAD --output=sentinel", "HEAD~1", "a" * 40 + "...--stat"],
)
def test_git_range_refuses_option_injection(
    revision_range, monkeypatch, tmp_path, invoke
):
    from gauntlet import proc

    def forbidden(*args, **kwargs):
        pytest.fail("invalid range must not reach git")

    monkeypatch.setattr(proc, "run_bytes", forbidden)
    result = invoke("list_pr_files", [f"--git-range={revision_range}"], tmp_path)
    assert result.returncode == 2
    assert result.stdout == b""
    assert b"--git-range must be HEAD or <base_sha>...<target_sha>" in result.stderr


def test_git_failure_stops_without_json(tmp_path, invoke):
    result = invoke("list_pr_files", ["--git-range", "HEAD"], tmp_path)
    assert result.returncode == 1
    assert result.stdout == b""
    assert b"FAILED: file listing failed:" in result.stderr


@pytest.mark.parametrize(
    "arguments",
    [
        ["--git-range", "HEAD", "--pr", "7"],
        ["--git-range", "HEAD", "--platform", "github"],
        ["--platform", "github"],
    ],
)
def test_file_listing_refuses_incomplete_or_mixed_modes(arguments, tmp_path, invoke):
    result = invoke("list_pr_files", arguments, tmp_path)
    assert result.returncode == 2
    assert result.stdout == b""
    assert result.stderr


def _invoke(invoke, tmp_path, platform="gitlab", script="list_pr_files"):
    return invoke(
        script,
        ["--platform", platform, "--pr", "7"],
        tmp_path,
        b'{"mode":"interactive","resolved":{}}',
    )


def test_recorded_gitlab_listing(monkeypatch, tmp_path, invoke):
    platform = "gitlab"
    facts = (json.dumps(gitlab_view()), "", 0)
    listing = (FIXTURES / "glab_mr_diffs.ndjson").read_text(encoding="utf-8")
    calls = fake_files_forge(monkeypatch, facts, (listing, "", 0), platform)
    result = _invoke(invoke, tmp_path, platform)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == ["render.py"]
    assert result.stderr == b""
    assert len(calls) == 2


@pytest.mark.parametrize(
    "deleted,old,new,expected",
    [
        (False, "old name", "b/caf\u00e9\tfile", "b/caf\u00e9\tfile"),
        (True, "deleted source", "/dev/null", "deleted source"),
    ],
)
def test_gitlab_paths_are_api_names(
    deleted, old, new, expected, monkeypatch, tmp_path, invoke
):
    entry = json.loads((FIXTURES / "glab_mr_diffs.ndjson").read_text(encoding="utf-8"))
    entry.update(deleted_file=deleted, old_path=old, new_path=new)
    fake_files_forge(
        monkeypatch,
        (json.dumps(gitlab_view(changes_count="2")), "", 0),
        (
            json.dumps(entry)
            + "\n"
            + (FIXTURES / "glab_mr_diffs.ndjson").read_text(encoding="utf-8"),
            "",
            0,
        ),
        "gitlab",
    )
    result = _invoke(invoke, tmp_path)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == [expected, "render.py"]
    assert result.stderr == b""


def test_gitlab_nested_project_and_iid_come_from_web_url(monkeypatch, tmp_path, invoke):
    view = gitlab_view(
        web_url="https://gitlab.example/parent/nested/probe/-/merge_requests/23"
    )
    fake_files_forge(
        monkeypatch,
        (json.dumps(view), "", 0),
        ((FIXTURES / "glab_mr_diffs.ndjson").read_text(encoding="utf-8"), "", 0),
        "gitlab",
        endpoint="projects/parent%2Fnested%2Fprobe/merge_requests/23/diffs",
    )
    result = _invoke(invoke, tmp_path)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == ["render.py"]


@pytest.mark.parametrize(
    "script,count",
    [
        ("list_pr_files", "1000+"),
        ("check_eligibility", " 1"),
        ("check_eligibility", "2"),
    ],
)
def test_gitlab_uncertain_count_keeps_paths_and_cannot_stop(
    count, script, monkeypatch, tmp_path, invoke
):
    view = gitlab_view(changes_count=count)
    listing = json.dumps({"new_path": "yarn.lock"})
    fake_files_forge(monkeypatch, (json.dumps(view), "", 0), (listing, "", 0), "gitlab")
    result = _invoke(invoke, tmp_path, script=script)
    assert result.returncode == 0, result.stderr
    if script == "list_pr_files":
        assert json.loads(result.stdout) == ["yarn.lock"]
        assert b"incomplete" in result.stderr
    else:
        assert json.loads(result.stdout)["verdict"] == "proceed"
        assert "incomplete" in json.loads(result.stdout)["reason"]


def test_gitlab_complete_empty_listing_can_stop(monkeypatch, tmp_path, invoke):
    fake_files_forge(
        monkeypatch,
        (json.dumps(gitlab_view(changes_count="0")), "", 0),
        ("", "", 0),
        "gitlab",
    )
    result = _invoke(invoke, tmp_path, script="check_eligibility")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["reason"] == "no changes"


@pytest.mark.parametrize("count", [2999, 3000])
def test_github_cap_boundary(count, monkeypatch, tmp_path, invoke):
    for script in ("list_pr_files", "check_eligibility"):
        fake_files_forge(monkeypatch, github_view(), ('"yarn.lock"\n' * count, "", 0))
        result = _invoke(invoke, tmp_path, "github", script)
        assert result.returncode == 0, result.stderr
        if script == "list_pr_files":
            assert json.loads(result.stdout) == ["yarn.lock"] * count
            assert (b"3000-file cap" in result.stderr) == (count == 3000)
        else:
            assert json.loads(result.stdout)["verdict"] == (
                "proceed" if count == 3000 else "stop"
            )
            assert json.loads(result.stdout)["reason"] == (
                "file listing reached the 3000-file cap"
                if count == 3000
                else "lockfile-only"
            )


def test_listing_failure_is_fatal(monkeypatch, tmp_path, invoke):
    calls = fake_files_forge(monkeypatch, github_view(), ("", "forbidden", 1))
    result = _invoke(invoke, tmp_path, "github")
    assert result.returncode == 1
    assert result.stdout == b""
    assert b"list_pr_files: FAILED: file listing failed: forbidden" in result.stderr
    assert len(calls) == 2


def test_github_filename_framing_and_enterprise_origin(monkeypatch, tmp_path, invoke):
    paths = ["nested/line\nbreak", "caf\u00e9\tfile", 'quote"file', "sep\u2028file"]
    view = json.loads(github_view()[0])
    view["url"] = "https://git.example.com:8443/upstream/project/pull/7"
    fake_files_forge(
        monkeypatch,
        (json.dumps(view), "", 0),
        ("\n".join(json.dumps(path, ensure_ascii=False) for path in paths), "", 0),
        hostname="git.example.com",
    )
    result = _invoke(invoke, tmp_path, "github")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == paths
    assert result.stderr == b""


def test_bad_github_filename_is_fatal(monkeypatch, tmp_path, invoke):
    fake_files_forge(monkeypatch, github_view(), ("null", "", 0))
    result = _invoke(invoke, tmp_path, "github")
    assert result.returncode == 1
    assert result.stdout == b""
    assert b"filename must be a non-empty string" in result.stderr


@pytest.mark.parametrize("listing", ["[]", '{"new_path": 1}'])
def test_bad_gitlab_response_is_fatal(listing, monkeypatch, tmp_path, invoke):
    fake_files_forge(
        monkeypatch, (json.dumps(gitlab_view()), "", 0), (listing, "", 0), "gitlab"
    )
    result = _invoke(invoke, tmp_path)
    assert result.returncode == 1
    assert result.stdout == b""
    assert b"FAILED:" in result.stderr

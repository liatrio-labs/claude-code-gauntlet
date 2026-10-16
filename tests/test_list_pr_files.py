"""Shared listing behavior exercised through the two shipped consumers."""

import json

import pytest
from gauntlet import proc

from tests.support.pr_files import FIXTURES, fake_files_forge, github_view, gitlab_view


def _invoke(invoke, tmp_path, platform="gitlab", script="list_pr_files"):
    return invoke(
        script,
        ["--platform", platform, "--pr", "7"],
        tmp_path,
        b'{"mode":"interactive","resolved":{}}',
    )


@pytest.mark.parametrize("platform", ["github", "gitlab"])
def test_recorded_listing(platform, monkeypatch, tmp_path, invoke):
    facts = (
        github_view() if platform == "github" else (json.dumps(gitlab_view()), "", 0)
    )
    listing = (
        "render.py\n"
        if platform == "github"
        else (FIXTURES / "glab_mr_diffs.ndjson").read_text(encoding="utf-8")
    )
    calls = fake_files_forge(monkeypatch, facts, (listing, "", 0), platform)
    result = _invoke(invoke, tmp_path, platform)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == ["render.py"]
    assert result.stderr == b""
    assert len(calls) == 2


@pytest.mark.parametrize(
    "deleted,old,new,expected",
    [
        (False, "old name", "new name", "new name"),
        (True, "deleted source", "/dev/null", "deleted source"),
        (False, "a/old", "b/caf\u00e9\tfile", "b/caf\u00e9\tfile"),
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


@pytest.mark.parametrize("script", ["list_pr_files", "check_eligibility"])
@pytest.mark.parametrize("count", ["1000+", None, 1, "2", " 1", "+1"])
def test_gitlab_uncertain_count_keeps_paths_and_cannot_stop(
    count, script, monkeypatch, tmp_path, invoke
):
    view = gitlab_view(changes_count=count)
    if count is None:
        view.pop("changes_count")
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


@pytest.mark.parametrize("paths", [[], ["nested/yarn.lock"]])
def test_gitlab_complete_trivial_listing_can_stop(paths, monkeypatch, tmp_path, invoke):
    listing = "\n".join(json.dumps({"new_path": path}) for path in paths)
    fake_files_forge(
        monkeypatch,
        (json.dumps(gitlab_view(changes_count=str(len(paths)))), "", 0),
        (listing, "", 0),
        "gitlab",
    )
    result = _invoke(invoke, tmp_path, script="check_eligibility")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["reason"] == (
        "lockfile-only" if paths else "no changes"
    )


@pytest.mark.parametrize("count", [2999, 3000, 3001])
def test_github_cap_retains_paths(count, monkeypatch, tmp_path, invoke):
    fake_files_forge(monkeypatch, github_view(), ("yarn.lock\n" * count, "", 0))
    result = _invoke(invoke, tmp_path, "github")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == ["yarn.lock"] * count
    assert (b"3000-file cap" in result.stderr) == (count >= 3000)


@pytest.mark.parametrize("platform", ["github", "gitlab"])
@pytest.mark.parametrize("stage", ["view", "files"])
@pytest.mark.parametrize(
    "failure",
    [("", "forbidden", 1), OSError("missing forge"), proc.TimeoutExpired("forge", 30)],
)
def test_fetch_failure_is_fatal(
    platform, stage, failure, monkeypatch, tmp_path, invoke
):
    facts = (
        github_view() if platform == "github" else (json.dumps(gitlab_view()), "", 0)
    )
    calls = fake_files_forge(
        monkeypatch, failure if stage == "view" else facts, failure, platform
    )
    result = _invoke(invoke, tmp_path, platform)
    assert result.returncode == 1
    assert result.stdout == b""
    assert b"list_pr_files: FAILED:" in result.stderr
    assert len(calls) == (1 if stage == "view" else 2)


@pytest.mark.parametrize("listing", ["malformed", "[]", "{}", '{"new_path": 1}'])
def test_bad_gitlab_response_is_fatal(listing, monkeypatch, tmp_path, invoke):
    fake_files_forge(
        monkeypatch, (json.dumps(gitlab_view()), "", 0), (listing, "", 0), "gitlab"
    )
    result = _invoke(invoke, tmp_path)
    assert result.returncode == 1
    assert result.stdout == b""
    assert b"FAILED:" in result.stderr

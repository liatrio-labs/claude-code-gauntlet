"""Shared listing behavior exercised through the two shipped consumers."""

import json

import pytest

from tests.support.pr_files import FIXTURES, fake_files_forge, github_view, gitlab_view


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
        hostname="git.example.com:8443",
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

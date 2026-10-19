"""Forge status fields at the shipped command boundary."""

import json

import pytest

from tests.support.pr_files import fake_files_forge, github_view, gitlab_view


@pytest.mark.parametrize("platform", ["github", "gitlab"])
@pytest.mark.parametrize(
    "outcome",
    ["valid", "sha", "url", "framing", "missing-sha", "missing-url", "failure"],
)
def test_forge_status_validates_before_emitting(
    platform, outcome, monkeypatch, tmp_path, invoke
):
    view = (
        dict(json.loads(github_view()[0]), headRefOid="a" * 40)
        if platform == "github"
        else gitlab_view()
    )
    sha_key, url_key = (
        ("headRefOid", "url") if platform == "github" else ("sha", "web_url")
    )
    if outcome == "sha":
        view[sha_key] = '"' + "a" * 40 + '"'
    elif outcome == "url":
        view[url_key] = "https://example.invalid/other"
    elif outcome == "framing":
        view[url_key] += "\npr_url=other"
    elif outcome.startswith("missing"):
        del view[sha_key if outcome == "missing-sha" else url_key]
    facts = ("", "forbidden", 1) if outcome == "failure" else (json.dumps(view), "", 0)
    calls = fake_files_forge(monkeypatch, facts, platform=platform)
    result = invoke("pr_status", ["--platform", platform, "--pr", "7"], tmp_path)
    assert len(calls) == 1
    if outcome == "valid":
        assert result.returncode == 0, result.stderr
        assert result.stdout == (
            f"head_sha={view[sha_key]}\npr_url={view[url_key]}\n".encode()
        )
        assert result.stderr == b""
    else:
        assert result.returncode != 0
        assert result.stdout == b""
        assert result.stderr.startswith(b"pr_status: ")


def test_forge_status_refuses_nonpositive_pr(monkeypatch, tmp_path, invoke):
    calls = fake_files_forge(monkeypatch, github_view())
    result = invoke("pr_status", ["--platform", "github", "--pr", "0"], tmp_path)
    assert result.returncode == 2
    assert result.stdout == b""
    assert result.stderr == b"pr_status: --pr must be positive\n"
    assert calls == []

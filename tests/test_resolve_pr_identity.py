"""Tests for the stdlib PR/MR identity producer."""

import json

import pytest

SHA = "0123456789abcdef0123456789abcdef01234567"


@pytest.mark.parametrize(
    "platform,url,title,fields",
    [
        pytest.param(
            "github",
            "https://github.com/OpenAI/codex/pull/278",
            "A café fix",
            ("OpenAI", "codex", 278, "https://github.com"),
            id="github-public",
        ),
        pytest.param(
            "github",
            "https://Git.Example.COM:8443/acme/widget/pull/7/?ignored=yes",
            None,
            ("acme", "widget", 7, "https://git.example.com:8443"),
            id="ghe",
        ),
        pytest.param(
            "gitlab",
            "https://gitlab.com/acme/widget/-/merge_requests/281",
            None,
            ("acme", "widget", 281, "https://gitlab.com"),
            id="gitlab-public",
        ),
        pytest.param(
            "gitlab",
            "http://GitLab.Example:8080/group/sub/widget/-/merge_requests"
            "/9/?view=parallel",
            None,
            ("group/sub", "widget", 9, "http://gitlab.example:8080"),
            id="nested-private",
        ),
        pytest.param(
            "github",
            "https://github.com/a/r/pull/3/",
            "   ",
            ("a", "r", 3, "https://github.com"),
            id="blank-title",
        ),
    ],
)
def test_identity(platform, url, title, fields, tmp_path, invoke):
    owner, repo, number, origin = fields
    expected = {
        "owner": owner,
        "repo": repo,
        "pr_number": number,
        "sha_full": SHA,
        "platform": platform,
        "web_origin": origin,
    }
    if title is not None and title.strip():
        expected["title"] = title
    payload = {"url" if platform == "github" else "web_url": url}
    if title is not None:
        payload["title"] = title
    result = invoke(
        "resolve_pr_identity",
        ["--platform", platform, "--sha", SHA],
        tmp_path,
        json.dumps(payload, ensure_ascii=False).encode("utf-8"),
    )
    assert result.returncode == 0, result.stderr
    assert result.stderr == b""
    identity = json.loads(result.stdout)
    assert identity == expected
    assert list(identity) == list(expected)


@pytest.mark.parametrize(
    "platform,url,sha,error",
    [
        pytest.param(
            "github",
            "https://gitlab.com/a/r/-/merge_requests/3",
            SHA,
            "URL path does not match a github PR URL",
            id="wrong-platform",
        ),
        pytest.param(
            "github",
            "https://github.com/a/r/pull/3",
            "a" * 39,
            "sha must be a 40-character lowercase hex commit id",
            id="short-sha",
        ),
        pytest.param(
            "github",
            "https://github.com/a/r/pull/3",
            "A" * 40,
            "sha must be a 40-character lowercase hex commit id",
            id="uppercase-sha",
        ),
    ],
)
def test_identity_refuses_invalid_input(platform, url, sha, error, tmp_path, invoke):
    result = invoke(
        "resolve_pr_identity",
        ["--platform", platform, "--sha", sha],
        tmp_path,
        json.dumps({"url" if platform == "github" else "web_url": url}).encode(),
    )
    assert result.returncode == 2
    assert result.stdout == b""
    assert result.stderr == f"resolve_pr_identity: {error}\n".encode()


@pytest.mark.parametrize(
    "stdin,error",
    [
        (b"{", "stdin must contain a JSON object"),
        (b"[]", "stdin must contain a JSON object"),
        (b'{"title": "t"}', "stdin url must be a string"),
        (
            b'{"url": "https://github.com/o/r/pull/5", "title": 42}',
            "stdin title must be a string",
        ),
    ],
)
def test_identity_refuses_bad_stdin(stdin, error, tmp_path, invoke):
    result = invoke(
        "resolve_pr_identity",
        ["--platform", "github", "--sha", SHA],
        tmp_path,
        stdin,
    )
    assert result.returncode == 2
    assert result.stdout == b""
    assert result.stderr == f"resolve_pr_identity: {error}\n".encode()

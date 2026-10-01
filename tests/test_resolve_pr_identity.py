"""Tests for the stdlib PR/MR identity producer."""

import pytest
from gauntlet.cli import CliError
from gauntlet.pr_identity import resolve

SHA = "0123456789abcdef0123456789abcdef01234567"


@pytest.mark.parametrize(
    "platform, url, sha, title, fields, error",
    [
        pytest.param(
            "github",
            "https://github.com/OpenAI/codex/pull/278",
            SHA,
            "A caf\u00e9 fix",
            ("OpenAI", "codex", 278, "https://github.com"),
            None,
            id="github-public",
        ),
        pytest.param(
            "github",
            "https://Git.Example.COM:8443/acme/widget/pull/7/?ignored=yes",
            SHA,
            None,
            ("acme", "widget", 7, "https://git.example.com:8443"),
            None,
            id="ghe",
        ),
        pytest.param(
            "gitlab",
            "https://gitlab.com/acme/widget/-/merge_requests/281",
            SHA,
            None,
            ("acme", "widget", 281, "https://gitlab.com"),
            None,
            id="gitlab-public",
        ),
        pytest.param(
            "gitlab",
            "http://GitLab.Example:8080/group/sub/widget/-/merge_requests/9/?view=parallel",
            SHA,
            None,
            ("group/sub", "widget", 9, "http://gitlab.example:8080"),
            None,
            id="nested-private",
        ),
        pytest.param(
            "github",
            "https://gitlab.com/a/r/-/merge_requests/3",
            SHA,
            None,
            None,
            "URL path does not match a github PR URL",
            id="wrong-platform",
        ),
        pytest.param(
            "github",
            "https://github.com/a/r/pull/3",
            "a" * 39,
            None,
            None,
            "sha must be a 40-character lowercase hex commit id",
            id="short-sha",
        ),
        pytest.param(
            "github",
            "https://github.com/a/r/pull/3",
            "A" * 40,
            None,
            None,
            "sha must be a 40-character lowercase hex commit id",
            id="uppercase-sha",
        ),
        pytest.param(
            "github",
            "https://github.com/a/r/pull/3/",
            SHA,
            "   ",
            ("a", "r", 3, "https://github.com"),
            None,
            id="blank-title",
        ),
    ],
)
def test_identity(platform, url, sha, title, fields, error):
    if error is not None:
        with pytest.raises(CliError) as exc:
            resolve(platform, url, sha, title)
        assert str(exc.value) == error
        assert exc.value.code == 2
        return
    owner, repo, number, origin = fields
    expected = {
        "owner": owner,
        "repo": repo,
        "pr_number": number,
        "sha_full": sha,
        "platform": platform,
        "web_origin": origin,
    }
    if title is not None and title.strip():
        expected["title"] = title
    result = resolve(platform, url, sha, title)
    assert result == expected
    assert list(result) == list(expected)

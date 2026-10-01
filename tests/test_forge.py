"""Forge policy and transport contracts, plus the current poster characterization."""

import sys
from dataclasses import FrozenInstanceError
from pathlib import Path
from types import MappingProxyType

import pytest
from gauntlet import forge, prior_review, proc
from gauntlet.delivery import post

from tests.support.forge import FakeForge, FakeGitLab, ForgeCall, install_forge_factory


@pytest.mark.parametrize(
    "remote, status, expected",
    [
        pytest.param(
            "https://github.com/o/r.git", 0, ("github", "github.com"), id="github-https"
        ),
        pytest.param(
            "git@github.com:o/r.git", 0, ("github", "github.com"), id="github-scp"
        ),
        pytest.param(
            "https://gitlab.com/g/r.git", 0, ("gitlab", "gitlab.com"), id="gitlab-https"
        ),
        pytest.param(
            "git@gitlab.com:g/r.git", 0, ("gitlab", "gitlab.com"), id="gitlab-scp"
        ),
        pytest.param(
            "https://api.github.com/o/r.git",
            0,
            ("github", "api.github.com"),
            id="github-subdomain",
        ),
        pytest.param(
            "https://sub.gitlab.com/g/r.git",
            0,
            ("gitlab", "sub.gitlab.com"),
            id="gitlab-subdomain",
        ),
        pytest.param(
            "https://evilgithub.com/o/r.git",
            0,
            ("github", "evilgithub.com"),
            id="github-prefix",
        ),
        pytest.param(
            "https://github.com.example.net/o/r.git",
            0,
            ("github", "github.com.example.net"),
            id="github-suffix",
        ),
        pytest.param(
            "https://evilgitlab.com/g/r.git",
            0,
            ("gitlab", "evilgitlab.com"),
            id="gitlab-prefix",
        ),
        pytest.param(
            "https://gitlab.com.example.net/g/r.git",
            0,
            ("gitlab", "gitlab.com.example.net"),
            id="gitlab-suffix",
        ),
        pytest.param(
            "git@gitlab.internal.company.com:g/r.git",
            0,
            ("gitlab", "gitlab.internal.company.com"),
            id="private-gitlab",
        ),
        pytest.param(
            "https://code.example.org/g/r.git",
            0,
            (None, "code.example.org"),
            id="private-code",
        ),
        pytest.param(
            "https://ghe.example.org/o/r.git",
            0,
            (None, "ghe.example.org"),
            id="enterprise-server",
        ),
        pytest.param(
            "https://tenant.ghe.com/o/r.git",
            0,
            (None, "tenant.ghe.com"),
            id="enterprise-cloud",
        ),
        pytest.param(
            "https://bitbucket.org/o/r.git",
            0,
            (None, "bitbucket.org"),
            id="other-forge",
        ),
        pytest.param("", 0, (None, None), id="empty"),
        pytest.param("not-a-url", 0, (None, None), id="malformed"),
        pytest.param("", 1, (None, None), id="remote-nonzero"),
        pytest.param(
            "https://github.com:8443/o/r.git",
            0,
            ("github", "github.com:8443"),
            id="github-port",
        ),
        pytest.param(
            "https://gitlab.com:8443/g/r.git",
            0,
            ("gitlab", "gitlab.com:8443"),
            id="gitlab-port",
        ),
        pytest.param(
            "ssh://git@github.com:2222/o/r.git", 0, (None, None), id="github-ssh-url"
        ),
        pytest.param(
            "ssh://git@gitlab.com:2222/g/r.git", 0, (None, None), id="gitlab-ssh-url"
        ),
        pytest.param(
            "https://GITHUB.COM/o/r.git", 0, (None, "GITHUB.COM"), id="uppercase-host"
        ),
        pytest.param(
            "HTTPS://GITHUB.COM/o/r.git", 0, (None, None), id="uppercase-scheme"
        ),
        pytest.param(
            "git@GitLab.COM:g/r.git", 0, (None, "GitLab.COM"), id="mixedcase-scp"
        ),
        pytest.param("alice@github.com:o/r.git", 0, (None, None), id="non-git-user"),
        pytest.param(
            "https://github.com@evil.example/o/r.git",
            0,
            ("github", "github.com@evil.example"),
            id="userinfo-lookalike",
        ),
        pytest.param(
            "https://github.com.example.net:443/o/r.git",
            0,
            ("github", "github.com.example.net:443"),
            id="suffix-port",
        ),
        pytest.param(
            "https://github.com:bad/o/r.git",
            0,
            ("github", "github.com:bad"),
            id="bad-port",
        ),
        pytest.param(
            "https://github.com./o/r.git",
            0,
            ("github", "github.com."),
            id="trailing-dot",
        ),
        pytest.param("https://[::1]:8443/g/r.git", 0, (None, "[::1]:8443"), id="ipv6"),
        pytest.param("git://host/o/r.git", 0, (None, None), id="git-scheme"),
        pytest.param("git+ssh://git@host/o/r", 0, (None, None), id="git-ssh-scheme"),
        pytest.param(
            "https://evil.example\\.github.com/o/r.git",
            0,
            ("github", "evil.example\\.github.com"),
            id="backslash",
        ),
        pytest.param(
            "https://evil.example .github.com/o/r",
            0,
            ("github", "evil.example .github.com"),
            id="space",
        ),
        pytest.param(
            "https://evil.example%23.github.com/o/r.git",
            0,
            ("github", "evil.example%23.github.com"),
            id="percent-escape",
        ),
        pytest.param(
            "https://evil.example;.github.com/o/r",
            0,
            ("github", "evil.example;.github.com"),
            id="semicolon",
        ),
        pytest.param(
            "https://evil.example\t.github.com/o/r",
            0,
            ("github", "evil.example\t.github.com"),
            id="tab",
        ),
        pytest.param(
            "https://.github.com/o/r.git",
            0,
            ("github", ".github.com"),
            id="leading-dot",
        ),
        pytest.param(
            "https://x..github.com/o/r.git",
            0,
            ("github", "x..github.com"),
            id="empty-label",
        ),
        pytest.param(
            "https://oauth2:fixture-token@gitlab.com/g/r.git",
            0,
            ("gitlab", "oauth2:fixture-token@gitlab.com"),
            id="credentialed-gitlab",
        ),
        pytest.param(
            "https://fixture-user:fixture-token@github.com/o/r.git",
            0,
            ("github", "fixture-user:fixture-token@github.com"),
            id="credentialed-github",
        ),
        pytest.param(
            "https://evil.example@github.com/o/r.git",
            0,
            ("github", "evil.example@github.com"),
            id="userinfo-public-host",
        ),
        pytest.param(
            "https://gitlab.example@evil.example/o/r.git",
            0,
            ("gitlab", "gitlab.example@evil.example"),
            id="userinfo-private-lookalike",
        ),
        pytest.param(
            "git@github.com@evil.example:o/r.git",
            0,
            ("github", "github.com@evil.example"),
            id="double-at-scp",
        ),
        pytest.param(
            "https://fixture-user@github.com@evil.example/o/r.git",
            0,
            ("github", "fixture-user@github.com@evil.example"),
            id="double-at-https",
        ),
        pytest.param("github.com:o/r.git", 0, (None, None), id="userless-scp"),
        pytest.param("https://github.com", 0, (None, None), id="pathless-https"),
        pytest.param("ssh://git@gitlab.com", 0, (None, None), id="pathless-ssh"),
        pytest.param("git@github.com:", 0, (None, None), id="pathless-scp"),
        pytest.param("https://[::1/o/r", 0, (None, "[::1"), id="invalid-bracket"),
        pytest.param(
            "https://evil.example\\.gitlab.com/o/r.git",
            0,
            ("gitlab", "evil.example\\.gitlab.com"),
            id="gitlab-backslash",
        ),
        pytest.param(
            "https://evil.example .gitlab.com/o/r",
            0,
            ("gitlab", "evil.example .gitlab.com"),
            id="gitlab-space",
        ),
        pytest.param(
            "https://evil.example%23.gitlab.com/o/r.git",
            0,
            ("gitlab", "evil.example%23.gitlab.com"),
            id="gitlab-percent-escape",
        ),
        pytest.param(
            "https://evil.example;.gitlab.com/o/r",
            0,
            ("gitlab", "evil.example;.gitlab.com"),
            id="gitlab-semicolon",
        ),
        pytest.param(
            "https://evil.example\t.gitlab.com/o/r",
            0,
            ("gitlab", "evil.example\t.gitlab.com"),
            id="gitlab-tab",
        ),
        pytest.param(
            "https://.gitlab.com/o/r.git",
            0,
            ("gitlab", ".gitlab.com"),
            id="gitlab-leading-dot",
        ),
        pytest.param(
            "https://x..gitlab.com/o/r.git",
            0,
            ("gitlab", "x..gitlab.com"),
            id="gitlab-empty-label",
        ),
        pytest.param(
            "https://gitlab.com./o/r.git",
            0,
            ("gitlab", "gitlab.com."),
            id="gitlab-trailing-dot",
        ),
        pytest.param(
            "https://gitlab.com:bad/o/r.git",
            0,
            ("gitlab", "gitlab.com:bad"),
            id="gitlab-bad-port",
        ),
        pytest.param(
            "https://xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx.github.com/o/r",
            0,
            (
                "github",
                "xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx.github.com",
            ),
            id="github-overlong-label",
        ),
        pytest.param(
            "https://xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx.xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx.xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx.xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx.github.com/o/r",
            0,
            (
                "github",
                "xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx.xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx.xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx.xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx.github.com",
            ),
            id="github-overlong-host",
        ),
        pytest.param(
            "https://-x.github.com/o/r",
            0,
            ("github", "-x.github.com"),
            id="github-leading-hyphen",
        ),
        pytest.param(
            "https://x-.github.com/o/r",
            0,
            ("github", "x-.github.com"),
            id="github-trailing-hyphen",
        ),
        pytest.param(
            "https://x_y.github.com/o/r",
            0,
            ("github", "x_y.github.com"),
            id="github-underscore",
        ),
        pytest.param(
            "https://github.com:/o/r",
            0,
            ("github", "github.com:"),
            id="github-empty-port",
        ),
        pytest.param(
            "https://github.com:-1/o/r",
            0,
            ("github", "github.com:-1"),
            id="github-negative-port",
        ),
        pytest.param(
            "https://github.com:65536/o/r",
            0,
            ("github", "github.com:65536"),
            id="github-high-port",
        ),
        pytest.param(
            "https://github.com:0/o/r",
            0,
            ("github", "github.com:0"),
            id="github-zero-port",
        ),
        pytest.param(
            "https://github.com:65535/o/r",
            0,
            ("github", "github.com:65535"),
            id="github-max-port",
        ),
        pytest.param(
            "https://evil\uff0f.github.com/o/r",
            0,
            ("github", "evil\uff0f.github.com"),
            id="github-nfkc-authority",
        ),
        pytest.param(
            "https://github.com?query/o/r",
            0,
            ("github", "github.com?query"),
            id="github-query-authority",
        ),
        pytest.param(
            "https://github.com#fragment/o/r",
            0,
            ("github", "github.com#fragment"),
            id="github-fragment-authority",
        ),
        pytest.param("github.com:o/r.git", 0, (None, None), id="github-userless-scp"),
        pytest.param("https://github.com", 0, (None, None), id="github-pathless-https"),
        pytest.param("ssh://git@github.com", 0, (None, None), id="github-pathless-ssh"),
        pytest.param("git@github.com:", 0, (None, None), id="github-pathless-scp"),
        pytest.param(
            "ftp://github.com/o/r", 0, (None, None), id="github-unsupported-scheme"
        ),
        pytest.param(
            "https://xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx.gitlab.com/o/r",
            0,
            (
                "gitlab",
                "xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx.gitlab.com",
            ),
            id="gitlab-overlong-label",
        ),
        pytest.param(
            "https://xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx.xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx.xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx.xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx.gitlab.com/o/r",
            0,
            (
                "gitlab",
                "xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx.xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx.xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx.xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx.gitlab.com",
            ),
            id="gitlab-overlong-host",
        ),
        pytest.param(
            "https://-x.gitlab.com/o/r",
            0,
            ("gitlab", "-x.gitlab.com"),
            id="gitlab-leading-hyphen",
        ),
        pytest.param(
            "https://x-.gitlab.com/o/r",
            0,
            ("gitlab", "x-.gitlab.com"),
            id="gitlab-trailing-hyphen",
        ),
        pytest.param(
            "https://x_y.gitlab.com/o/r",
            0,
            ("gitlab", "x_y.gitlab.com"),
            id="gitlab-underscore",
        ),
        pytest.param(
            "https://gitlab.com:/o/r",
            0,
            ("gitlab", "gitlab.com:"),
            id="gitlab-empty-port",
        ),
        pytest.param(
            "https://gitlab.com:-1/o/r",
            0,
            ("gitlab", "gitlab.com:-1"),
            id="gitlab-negative-port",
        ),
        pytest.param(
            "https://gitlab.com:65536/o/r",
            0,
            ("gitlab", "gitlab.com:65536"),
            id="gitlab-high-port",
        ),
        pytest.param(
            "https://gitlab.com:0/o/r",
            0,
            ("gitlab", "gitlab.com:0"),
            id="gitlab-zero-port",
        ),
        pytest.param(
            "https://gitlab.com:65535/o/r",
            0,
            ("gitlab", "gitlab.com:65535"),
            id="gitlab-max-port",
        ),
        pytest.param(
            "https://evil\uff0f.gitlab.com/o/r",
            0,
            ("gitlab", "evil\uff0f.gitlab.com"),
            id="gitlab-nfkc-authority",
        ),
        pytest.param(
            "https://gitlab.com?query/o/r",
            0,
            ("gitlab", "gitlab.com?query"),
            id="gitlab-query-authority",
        ),
        pytest.param(
            "https://gitlab.com#fragment/o/r",
            0,
            ("gitlab", "gitlab.com#fragment"),
            id="gitlab-fragment-authority",
        ),
        pytest.param("gitlab.com:o/r.git", 0, (None, None), id="gitlab-userless-scp"),
        pytest.param("https://gitlab.com", 0, (None, None), id="gitlab-pathless-https"),
        pytest.param("ssh://git@gitlab.com", 0, (None, None), id="gitlab-pathless-ssh"),
        pytest.param("git@gitlab.com:", 0, (None, None), id="gitlab-pathless-scp"),
        pytest.param(
            "ftp://gitlab.com/o/r", 0, (None, None), id="gitlab-unsupported-scheme"
        ),
    ],
)
def test_current_platform_selection(remote, status, expected, monkeypatch):
    def origin_output(argv):
        assert argv == ["git", "remote", "get-url", "origin"]
        return remote, "", status

    monkeypatch.setattr(proc, "output", origin_output)
    assert post.detect_platform() == expected


@pytest.mark.parametrize(
    "platform, host", [("github", "github.com"), ("gitlab", "gitlab.com")]
)
@pytest.mark.parametrize(
    "template, selected, host_template",
    [
        pytest.param("https://{host}/o/r.git", True, "{host}", id="https"),
        pytest.param("http://{host}/o/r", True, "{host}", id="http"),
        pytest.param("ssh://git@{host}:2222/o/r", True, "{host}", id="ssh"),
        pytest.param("alice@{host}:o/r.git", True, "{host}", id="scp-user"),
        pytest.param("{host}:o/r.git", True, "{host}", id="userless-scp"),
        pytest.param("https://{host}", True, "{host}", id="pathless-url"),
        pytest.param("git@{host}:", True, "{host}", id="pathless-scp"),
        pytest.param("https://sub.{host}/o/r", True, "sub.{host}", id="subdomain"),
        pytest.param("https://evil{host}/o/r", False, "evil{host}", id="prefix"),
        pytest.param(
            "https://{host}.example/o/r", False, "{host}.example", id="suffix"
        ),
        pytest.param("ftp://{host}/o/r", False, "{host}", id="ftp"),
        pytest.param("git://{host}/o/r", False, "{host}", id="git-scheme"),
        pytest.param("git+ssh://git@{host}/o/r", False, "{host}", id="git-ssh"),
        pytest.param("HTTPS://{host}/o/r", True, "{host}", id="uppercase-scheme"),
        pytest.param(
            "https://fixture-user:fixture-token@{host}/o/r",
            True,
            "{host}",
            id="userinfo",
        ),
        pytest.param(
            "https://fixture%40user@{host}/o/r", True, "{host}", id="percent-userinfo"
        ),
        pytest.param(
            "https://fixture\\user;name@{host}/o/r",
            True,
            "{host}",
            id="lexical-userinfo",
        ),
        pytest.param(
            "https://fixture\tuser@{host}/o/r", False, None, id="tab-userinfo"
        ),
        pytest.param(
            "https://fixture\nuser@{host}/o/r", False, None, id="newline-userinfo"
        ),
        pytest.param(
            "https://fixture\x00user@{host}/o/r", False, None, id="c0-userinfo"
        ),
        pytest.param(
            "https://fixture\uff0fuser@{host}/o/r", False, None, id="nfkc-userinfo"
        ),
        pytest.param(
            "https://{host}@evil.example/o/r",
            False,
            "evil.example",
            id="host-in-userinfo",
        ),
        pytest.param(
            "git@{host}@evil.example:o/r", False, "evil.example", id="double-at-scp"
        ),
        pytest.param(
            "https://fixture@{host}@evil.example/o/r",
            False,
            "evil.example",
            id="double-at-url",
        ),
        pytest.param("https://{host}?query/o/r", True, "{host}", id="query-delimiter"),
        pytest.param(
            "https://{host}#fragment/o/r", True, "{host}", id="fragment-delimiter"
        ),
        pytest.param("https://{host}:1/o/r", True, "{host}", id="min-port"),
        pytest.param("https://{host}:65535/o/r", True, "{host}", id="max-port"),
        pytest.param("https://{host}:0/o/r", False, None, id="zero-port"),
        pytest.param("https://{host}:65536/o/r", False, None, id="high-port"),
        pytest.param("https://{host}:-1/o/r", False, None, id="negative-port"),
        pytest.param("https://{host}:bad/o/r", False, None, id="bad-port"),
        pytest.param("https://{host}:/o/r", False, None, id="empty-port"),
        pytest.param("https://{host}./o/r", False, None, id="trailing-dot"),
        pytest.param("https://.{host}/o/r", False, None, id="leading-dot"),
        pytest.param("https://x..{host}/o/r", False, None, id="empty-label"),
        pytest.param("https://x_y.{host}/o/r", False, None, id="underscore"),
        pytest.param("https://-x.{host}/o/r", False, None, id="leading-hyphen"),
        pytest.param("https://x-.{host}/o/r", False, None, id="trailing-hyphen"),
        pytest.param("https://evil\\.{host}/o/r", False, None, id="backslash"),
        pytest.param("https://evil .{host}/o/r", False, None, id="space"),
        pytest.param("https://evil\t.{host}/o/r", False, None, id="tab"),
        pytest.param("https://evil%23.{host}/o/r", False, None, id="percent-host"),
        pytest.param("https://evil;.{host}/o/r", False, None, id="semicolon"),
        pytest.param("https://evil\uff0f.{host}/o/r", False, None, id="nfkc-host"),
        pytest.param(
            "https://" + "x" * 64 + ".{host}/o/r", False, None, id="overlong-label"
        ),
        pytest.param(
            "https://" + ("x" * 63 + ".") * 4 + "{host}/o/r",
            False,
            None,
            id="overlong-host",
        ),
    ],
)
def test_public_host_policy(platform, host, template, selected, host_template):
    remote = forge.parse_remote(template.format(host=host))
    assert forge.detect_platform(remote) == forge.PlatformDetection(
        platform if selected else None,
        host_template.format(host=host) if host_template else None,
    )


@pytest.mark.parametrize(
    "url, expected",
    [
        pytest.param(
            "https://GITHUB.COM/o/r.git", ("github", "github.com"), id="uppercase-host"
        ),
        pytest.param(
            "git@GitLab.COM:o/r", ("gitlab", "gitlab.com"), id="mixedcase-scp"
        ),
        pytest.param(
            "https://gitlab.internal.example/o/r",
            (None, "gitlab.internal.example"),
            id="private-gitlab",
        ),
        pytest.param(
            "https://tenant.ghe.com/o/r", (None, "tenant.ghe.com"), id="enterprise"
        ),
        pytest.param("https://[::1]:8443/o/r", (None, "::1"), id="ipv6"),
        pytest.param(
            "ssh://git@[2001:DB8::1]/o/r", (None, "2001:db8::1"), id="ipv6-case"
        ),
        pytest.param("https://[::1/o/r", (None, None), id="unclosed-bracket"),
        pytest.param("https://[::1]extra/o/r", (None, None), id="bracket-suffix"),
        pytest.param("https://[127.0.0.1]/o/r", (None, None), id="bracketed-ipv4"),
        pytest.param("https://[::1%25fixture]/o/r", (None, None), id="ipv6-percent"),
        pytest.param("https://::1/o/r", (None, None), id="unbracketed-ipv6"),
        pytest.param("", (None, None), id="empty"),
        pytest.param("not-a-url", (None, None), id="malformed"),
        pytest.param("C:/github.com/o/r", (None, None), id="drive-forward"),
        pytest.param("C:\\github.com\\o\\r", (None, None), id="drive-backslash"),
        pytest.param("./github.com:o/r", (None, None), id="local-relative"),
        pytest.param("directory/github.com:o/r", (None, None), id="slash-before-colon"),
        pytest.param(
            "https://x" + "a" * 62 + ".github.com/o/r",
            ("github", "x" + "a" * 62 + ".github.com"),
            id="max-label",
        ),
    ],
)
def test_remote_classification(url, expected):
    assert forge.detect_platform(forge.parse_remote(url)) == forge.PlatformDetection(
        *expected
    )


@pytest.mark.parametrize(
    "url, expected",
    [
        pytest.param("git@github.com:owner/repo.git", ("owner", "repo"), id="scp"),
        pytest.param(
            "alice@host:group/sub/repo.git", ("group", "sub/repo"), id="subgroups"
        ),
        pytest.param("https://host/owner/repo", ("owner", "repo"), id="no-suffix"),
        pytest.param("ssh://git@host/owner/repo.git", ("owner", "repo"), id="ssh-url"),
        pytest.param(
            "ssh://git@host:2222/owner/repo.git", ("owner", "repo"), id="ssh-port"
        ),
        pytest.param("git://host/owner/repo.git", ("owner", "repo"), id="git-scheme"),
        pytest.param(
            "https://host/owner/repo.git/", ("owner", "repo"), id="one-trailing-slash"
        ),
        pytest.param(
            "https://host/owner/repo.git//",
            ("owner", "repo.git"),
            id="multiple-trailing-slashes",
        ),
        pytest.param(
            "https://host/owner/repo.git?query",
            ("owner", "repo.git?query"),
            id="query-tail",
        ),
        pytest.param(
            "https://host/owner/repo.git#fragment",
            ("owner", "repo.git#fragment"),
            id="fragment-tail",
        ),
        pytest.param(
            "https://bad host:bad/owner/repo.git",
            ("owner", "repo"),
            id="invalid-authority",
        ),
        pytest.param(
            "git+ssh://user@host/owner/repo.git", ("owner", "repo"), id="other-scheme"
        ),
        pytest.param(
            "HTTPS://HOST/owner/repo.git", ("owner", "repo"), id="uppercase-scheme"
        ),
        pytest.param("github.com:owner/repo.git", None, id="userless-scp"),
        pytest.param("https://host/owner", None, id="missing-repo"),
        pytest.param("https://host", None, id="pathless"),
        pytest.param("not-a-url", None, id="malformed"),
    ],
)
def test_remote_slug(url, expected):
    assert forge.remote_slug(forge.parse_remote(url)) == (
        forge.RepoSlug(*expected) if expected else None
    )


def test_remote_record_keeps_original_authority_and_lexical_path():
    assert forge.parse_remote(
        "ssh://fixture%40user@GITHUB.COM:2222/group/sub/repo.git"
    ) == forge.Remote(
        "fixture%40user@GITHUB.COM:2222", "github.com", "group/sub/repo", "ssh"
    )
    remote = forge.parse_remote("https://bad host/group/sub/repo.git")
    assert remote == forge.Remote("bad host", None, "group/sub/repo", "https")


@pytest.mark.parametrize(
    "url, host, slug",
    [
        (
            "fixture:token@github.com:owner/repo.git",
            "github.com",
            forge.RepoSlug("owner", "repo"),
        ),
        (
            "x://github.com/owner/repo.git",
            "github.com",
            forge.RepoSlug("owner", "repo"),
        ),
        ("git@[::1]:owner/repo.git", "::1", forge.RepoSlug(":1]:owner", "repo")),
    ],
    ids=["colon-userinfo-scp", "single-letter-scheme", "ipv6-scp"],
)
def test_remote_syntax_disambiguation(url, host, slug):
    remote = forge.parse_remote(url)
    assert remote.hostname == host
    assert forge.remote_slug(remote) == slug


@pytest.mark.parametrize(
    "platform, url, expected",
    [
        (
            "github",
            "https://github.com/OpenAI/codex/pull/278",
            ("OpenAI", "codex", 278, "https://github.com"),
        ),
        (
            "github",
            "https://Git.Example.COM:8443/acme/widget/pull/7/?ignored=yes",
            ("acme", "widget", 7, "https://git.example.com:8443"),
        ),
        (
            "gitlab",
            "http://GitLab.Example:8080/group/sub/widget/-/merge_requests/9/?view=parallel",
            ("group/sub", "widget", 9, "http://gitlab.example:8080"),
        ),
        (
            "gitlab",
            "https://[2001:DB8::1]:65535/group/repo/-/merge_requests/9007199254740991#fragment",
            ("group", "repo", 9007199254740991, "https://[2001:db8::1]:65535"),
        ),
    ],
    ids=["github", "enterprise", "nested-gitlab", "ipv6-safe-integer"],
)
def test_parse_pr_url(platform, url, expected):
    assert forge.parse_pr_url(platform, url) == forge.ParsedPrUrl(*expected)


@pytest.mark.parametrize(
    "url, error",
    [
        ("ssh://github.com/o/r/pull/1", "URL must use http(s) and include a host"),
        ("https:///o/r/pull/1", "URL must use http(s) and include a host"),
        (
            "https://fixture-user@github.com/o/r/pull/1",
            "URL must not contain user information",
        ),
        ("https://github.com:/o/r/pull/1", "URL has an empty port"),
        ("https://github.com:0/o/r/pull/1", "URL port must be between 1 and 65535"),
        ("https://github.com./o/r/pull/1", "URL has an invalid DNS host"),
        ("https://x_y.github.com/o/r/pull/1", "URL has an invalid DNS host"),
        ("https://github.com/o/r/pull/0", "URL path does not match a github PR URL"),
        ("https://github.com/o/r/pull/01", "URL path does not match a github PR URL"),
        (
            "https://github.com/o/r/pull/1/extra",
            "URL path does not match a github PR URL",
        ),
        (
            "https://gitlab.com/o/r/-/merge_requests/1",
            "URL path does not match a github PR URL",
        ),
        (
            "https://github.com/o/r/pull/9007199254740992",
            "PR/MR number must be a positive safe integer",
        ),
    ],
)
def test_parse_pr_url_refusal(url, error):
    with pytest.raises(ValueError) as exc:
        forge.parse_pr_url("github", url)
    assert str(exc.value) == error


@pytest.mark.parametrize(
    "url",
    [
        "https://[::1/o/r/pull/1",
        "https://github.com:bad/o/r/pull/1",
        "https://github.com:65536/o/r/pull/1",
        "https://evil\uff0f.github.com/o/r/pull/1",
    ],
)
def test_parse_pr_url_unparseable(url):
    with pytest.raises(ValueError, match=r"^unparseable URL:"):
        forge.parse_pr_url("github", url)


@pytest.mark.parametrize(
    "owner, repo, expected",
    [
        ("group/sub", "repo", "group%2Fsub%2Frepo"),
        ("group%2Fsub", "a b%repo", "group%2Fsub%2Fa b%repo"),
    ],
)
def test_project_id(owner, repo, expected):
    assert forge.gitlab_project_id(owner, repo) == expected


@pytest.mark.parametrize(
    "builder, platform, endpoint, headers",
    [
        (
            forge.github_review_request,
            "github",
            "repos/group/sub/repo/pulls/9/reviews",
            ("Accept: application/vnd.github+json",),
        ),
        (
            forge.gitlab_note_request,
            "gitlab",
            "projects/group%2Fsub%2Frepo/merge_requests/9/notes",
            ("Content-Type: application/json",),
        ),
        (
            forge.gitlab_discussion_request,
            "gitlab",
            "projects/group%2Fsub%2Frepo/merge_requests/9/discussions",
            ("Content-Type: application/json",),
        ),
    ],
    ids=["github-review", "gitlab-note", "gitlab-discussion"],
)
def test_request_constructors(builder, platform, endpoint, headers):
    payload = {"body": "caf\u00e9\n", "event": "COMMENT", "position": {"new_line": 2}}
    request = builder(forge.ReviewTarget("group/sub", "repo", "9"), payload)
    assert request == forge.PostRequest(platform, endpoint, "POST", headers, payload)
    with pytest.raises(FrozenInstanceError):
        request.method = "GET"


TARGET = forge.ReviewTarget("group/sub", "repo", 9)


@pytest.mark.parametrize(
    "adapter, tool",
    [(forge.GitHub, "gh"), (forge.GitLab, "glab")],
    ids=["github", "gitlab"],
)
@pytest.mark.parametrize("available", [True, False], ids=["present", "missing"])
def test_availability(adapter, tool, available, monkeypatch, tmp_path):
    looked_up = []

    def which(name):
        looked_up.append(name)
        return str(tmp_path / name) if available else None

    monkeypatch.setattr(proc, "which", which)
    if available:
        assert adapter().ensure_available() is None
    else:
        with pytest.raises(forge.ForgeUnavailable) as exc:
            adapter().ensure_available()
        assert (
            str(exc.value)
            == f"'{tool}' CLI tool not found. Install it and ensure it is authenticated before running this script."
        )
    assert looked_up == [tool]


@pytest.mark.parametrize(
    "adapter, argv",
    [
        (forge.GitHub, ["gh", "pr", "diff", "9", "--repo", "group/sub/repo"]),
        (forge.GitLab, ["glab", "mr", "diff", "9"]),
    ],
    ids=["github", "gitlab"],
)
@pytest.mark.parametrize("status", [0, 1], ids=["success", "failure"])
def test_diff_policy(adapter, argv, status, monkeypatch):
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return proc.CompletedProcess(command, status, "diff\r\n\u00e9\n", " stderr ")

    monkeypatch.setattr(proc, "run", run)
    assert adapter().diff(TARGET) == ("diff\r\n\u00e9\n", " stderr ", status)
    assert calls == [(argv, {"cwd": None, "timeout": None, "errors": "strict"})]


@pytest.mark.parametrize(
    "adapter", [forge.GitHub, forge.GitLab], ids=["github", "gitlab"]
)
@pytest.mark.parametrize(
    "failure",
    [
        FileNotFoundError("missing"),
        proc.TimeoutExpired(["fixture"], 4),
        UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid"),
    ],
    ids=["oserror", "timeout", "unicode-error"],
)
def test_diff_exceptions_propagate(adapter, failure, monkeypatch):
    def run(*args, **kwargs):
        raise failure

    monkeypatch.setattr(proc, "run", run)
    with pytest.raises(type(failure)) as exc:
        adapter().diff(TARGET)
    assert exc.value is failure


@pytest.mark.parametrize(
    "adapter, argv",
    [
        (
            forge.GitHub,
            ["gh", "api", "--paginate", "repos/group/sub/repo/pulls/9/reviews"],
        ),
        (
            forge.GitLab,
            [
                "glab",
                "api",
                "--paginate",
                "projects/group%2Fsub%2Frepo/merge_requests/9/notes",
            ],
        ),
    ],
    ids=["github", "gitlab"],
)
def test_paginated_read_argv(adapter, argv, monkeypatch):
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return proc.CompletedProcess(command, 0, '[{"id":1}]\n[{"id":2}]', "")

    def which(name):
        pytest.fail("review reads must not precheck availability")

    monkeypatch.setattr(proc, "which", which)
    monkeypatch.setattr(proc, "run", run)
    assert adapter().review_entries(TARGET) == forge.JsonFetch(
        [{"id": 1}, {"id": 2}], None
    )
    assert calls == [(argv, {"cwd": None, "timeout": 30, "errors": "replace"})]


@pytest.mark.parametrize(
    "adapter, label",
    [(forge.GitHub, "github reviews"), (forge.GitLab, "gitlab notes")],
    ids=["github", "gitlab"],
)
@pytest.mark.parametrize(
    "stdout, stderr, status, payload, error_tail",
    [
        pytest.param("", "", 0, [], None, id="empty"),
        pytest.param(" \n\t", "", 0, [], None, id="whitespace"),
        pytest.param(
            '[1]\n[2] {"id":3} null 4',
            "",
            0,
            [1, 2, {"id": 3}, None, 4],
            None,
            id="pages-and-documents",
        ),
        pytest.param("[1]  invalid", "", 0, [1], None, id="prefix-malformed-tail"),
        pytest.param(
            '[{"body":"older","submitted_at":"2026-01-01T00:00:00Z"}]'
            '[{"body":"newer","submitted_at":"2026-06-01T00:00:00Z"}]',
            "",
            0,
            [
                {"body": "older", "submitted_at": "2026-01-01T00:00:00Z"},
                {"body": "newer", "submitted_at": "2026-06-01T00:00:00Z"},
            ],
            None,
            id="paged-newest",
        ),
        pytest.param(
            "[]  invalid",
            "",
            0,
            [],
            "response was not JSON: []  invalid",
            id="empty-prefix-malformed",
        ),
        pytest.param(
            "bad JSON", "", 0, [], "response was not JSON: bad JSON", id="invalid"
        ),
        pytest.param(
            "x" * 200,
            "",
            0,
            [],
            "response was not JSON: " + "x" * 120,
            id="bounded-parse",
        ),
        pytest.param(
            "stdout",
            " stderr \n",
            3,
            [],
            "fetch failed (exit 3): stderr",
            id="nonzero-stderr",
        ),
        pytest.param(
            " stdout \n",
            "",
            3,
            [],
            "fetch failed (exit 3): stdout",
            id="nonzero-stdout",
        ),
        pytest.param(
            "",
            "x" * 400,
            3,
            [],
            "fetch failed (exit 3): " + "x" * 300,
            id="bounded-fetch",
        ),
        pytest.param(
            "[" * 40000 + "]" * 40000,
            "",
            0,
            [],
            "response was not JSON: " + "[" * 120,
            id="recursion",
        ),
    ],
)
def test_paginated_read_result(
    adapter, label, stdout, stderr, status, payload, error_tail, monkeypatch
):
    monkeypatch.setattr(
        proc,
        "run",
        lambda cmd, **kw: proc.CompletedProcess(cmd, status, stdout, stderr),
    )
    assert adapter().review_entries(TARGET) == forge.JsonFetch(
        payload, f"{label}: {error_tail}" if error_tail else None
    )


@pytest.mark.parametrize(
    "adapter, label",
    [(forge.GitHub, "github reviews"), (forge.GitLab, "gitlab notes")],
    ids=["github", "gitlab"],
)
@pytest.mark.parametrize(
    "failure, detail",
    [
        (FileNotFoundError("missing tool"), "missing tool"),
        (proc.TimeoutExpired(["fixture"], 30), "timed out after 30s"),
    ],
    ids=["oserror", "timeout"],
)
def test_read_failure_translation(adapter, label, failure, detail, monkeypatch):
    def run(*args, **kwargs):
        raise failure

    monkeypatch.setattr(proc, "run", run)
    assert adapter().review_entries(TARGET) == forge.JsonFetch(
        [], f"{label}: fetch failed (exit -1): {detail}"
    )


@pytest.mark.parametrize(
    "adapter", [forge.GitHub, forge.GitLab], ids=["github", "gitlab"]
)
def test_non_utf8_read_child(adapter, monkeypatch):
    real_run = proc.run
    calls = []
    child = r'import sys; sys.stdout.buffer.write(b"[{\"body\":\"bad\xff\"}]\n[{\"body\":\"tail\"}]")'

    def child_run(command, **kwargs):
        calls.append((command, kwargs))
        return real_run([sys.executable, "-c", child], **kwargs)

    monkeypatch.setattr(proc, "run", child_run)
    assert adapter().review_entries(TARGET) == forge.JsonFetch(
        [{"body": "bad\ufffd"}, {"body": "tail"}], None
    )
    assert len(calls) == 1
    assert calls[0][1] == {"cwd": None, "timeout": 30, "errors": "replace"}


@pytest.mark.parametrize(
    "caller", ["poster", "detector", "helper-poster", "helper-detector"]
)
@pytest.mark.parametrize(
    "outcome", ["missing-git", "nonutf8-unusable", "nonutf8-slug", "timeout", "nonzero"]
)
def test_origin_policy(caller, outcome, monkeypatch):
    real_run = proc.run
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        if outcome == "missing-git":
            raise FileNotFoundError("missing git")
        if outcome == "timeout":
            raise proc.TimeoutExpired(command, kwargs["timeout"])
        if outcome.startswith("nonutf8"):
            child = (
                r'import sys; sys.stdout.buffer.write(b"https://github.com/o/r\xff.git\n")'
                if outcome == "nonutf8-slug"
                else r'import sys; sys.stdout.buffer.write(b"\xff\xfe not a remote")'
            )
            return real_run([sys.executable, "-c", child], **kwargs)
        return proc.CompletedProcess(
            command, 1, "https://github.com/o/r.git", "rejected"
        )

    monkeypatch.setattr(proc, "run", run)

    def call():
        if caller == "poster":
            return post.detect_platform()
        if caller == "detector":
            return prior_review.remote_slug()
        remote = (
            forge.origin_remote(timeout=10, errors="replace")
            if caller == "helper-detector"
            else forge.origin_remote()
        )
        return forge.remote_slug(remote)

    detector = caller.endswith("detector")
    raises = (outcome in {"missing-git", "timeout"} and caller != "detector") or (
        outcome.startswith("nonutf8") and not detector
    )
    if raises:
        error = {
            "missing-git": FileNotFoundError,
            "timeout": proc.TimeoutExpired,
        }.get(outcome, UnicodeDecodeError)
        with pytest.raises(error):
            call()
    else:
        value = call()
        if caller == "detector":
            assert value == (
                ("o", "r\ufffd") if outcome == "nonutf8-slug" else (None, None)
            )
        elif caller == "poster":
            assert value == (None, None)
        else:
            assert value == (
                forge.RepoSlug("o", "r\ufffd") if outcome == "nonutf8-slug" else None
            )
    assert calls == [
        (
            ["git", "remote", "get-url", "origin"],
            {
                "cwd": None,
                "timeout": 10 if detector else None,
                "errors": "replace" if detector else "strict",
            },
        )
    ]


@pytest.mark.parametrize(
    "stdout, stderr, status, expected",
    [
        pytest.param(
            '[{"base_commit_sha":"base"}]',
            "",
            0,
            forge.JsonFetch([{"base_commit_sha": "base"}], None),
            id="whole-json",
        ),
        pytest.param("[]", "", 0, forge.JsonFetch([], None), id="empty-list"),
        pytest.param("null", "", 0, forge.JsonFetch(None, None), id="non-object"),
        pytest.param(
            "",
            "",
            0,
            forge.JsonFetch(None, "Could not parse MR versions response: "),
            id="empty-stdout",
        ),
        pytest.param(
            "bad",
            "",
            0,
            forge.JsonFetch(None, "Could not parse MR versions response: bad"),
            id="invalid-json-text",
        ),
        pytest.param(
            "x" * 300,
            "",
            0,
            forge.JsonFetch(None, "Could not parse MR versions response: " + "x" * 200),
            id="bounded-parse-text",
        ),
        pytest.param(
            "[]\n[]",
            "",
            0,
            forge.JsonFetch(None, "Could not parse MR versions response: []\n[]"),
            id="concatenated-json",
        ),
        pytest.param(
            "",
            " rejected \n",
            4,
            forge.JsonFetch(
                None,
                "Failed to fetch MR versions (exit 4): rejected\nEnsure glab is authenticated and the MR IID is correct.",
            ),
            id="nonzero-text",
        ),
    ],
)
def test_versions_policy(stdout, stderr, status, expected, monkeypatch):
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return proc.CompletedProcess(command, status, stdout, stderr)

    monkeypatch.setattr(proc, "run", run)
    assert forge.GitLab().diff_refs(TARGET) == expected
    assert calls == [
        (
            ["glab", "api", "projects/group%2Fsub%2Frepo/merge_requests/9/versions"],
            {"cwd": None, "timeout": None, "errors": "strict"},
        )
    ]


@pytest.mark.parametrize(
    "failure",
    [
        OSError("missing"),
        proc.TimeoutExpired(["glab"], 2),
        UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid"),
    ],
    ids=["oserror", "timeout", "unicode-error"],
)
def test_versions_exceptions_propagate(failure, monkeypatch):
    def run(*args, **kwargs):
        raise failure

    monkeypatch.setattr(proc, "run", run)
    with pytest.raises(type(failure)) as exc:
        forge.GitLab().diff_refs(TARGET)
    assert exc.value is failure


@pytest.mark.parametrize(
    "adapter, builder, argv",
    [
        (
            forge.GitHub,
            forge.github_review_request,
            [
                "gh",
                "api",
                "--method",
                "POST",
                "-H",
                "Accept: application/vnd.github+json",
                "repos/group/sub/repo/pulls/9/reviews",
            ],
        ),
        (
            forge.GitLab,
            forge.gitlab_note_request,
            [
                "glab",
                "api",
                "--method",
                "POST",
                "--header",
                "Content-Type: application/json",
                "projects/group%2Fsub%2Frepo/merge_requests/9/notes",
            ],
        ),
        (
            forge.GitLab,
            forge.gitlab_discussion_request,
            [
                "glab",
                "api",
                "--method",
                "POST",
                "--header",
                "Content-Type: application/json",
                "projects/group%2Fsub%2Frepo/merge_requests/9/discussions",
            ],
        ),
    ],
    ids=["github-review", "gitlab-note", "gitlab-discussion"],
)
def test_submit_transport(adapter, builder, argv, monkeypatch, tmp_path):
    paths = []
    calls = []
    real_mkstemp = forge.tempfile.mkstemp

    def mkstemp(**kwargs):
        fd, path = real_mkstemp(dir=tmp_path, **kwargs)
        paths.append(Path(path))
        return fd, path

    def run(command, **kwargs):
        calls.append((command, kwargs))
        assert command == [*argv, "--input", str(paths[0])]
        assert (
            paths[0].read_bytes()
            == b'{"body": "caf\xc3\xa9\\nline", "event": "COMMENT", "comments": []}'
        )
        return proc.CompletedProcess(command, 0, '{"id":7}', "")

    monkeypatch.setattr(forge.tempfile, "mkstemp", mkstemp)
    monkeypatch.setattr(proc, "run", run)
    assert adapter().submit(
        builder(TARGET, {"body": "caf\u00e9\nline", "event": "COMMENT", "comments": []})
    ) == forge.PostResult({"id": 7}, None, None)
    assert len(paths) == len(calls) == 1
    assert calls[0][1] == {"cwd": None, "timeout": None, "errors": "strict"}
    assert not paths[0].exists()


@pytest.mark.parametrize(
    "stdout, expected",
    [
        (" \n", forge.PostResult({}, None, None)),
        ("[1,2]", forge.PostResult([1, 2], None, None)),
        ("null", forge.PostResult(None, None, None)),
        (
            "not JSON",
            forge.PostResult(
                {"raw": "not JSON"},
                None,
                "Could not parse API response as JSON: not JSON",
            ),
        ),
        (
            "x" * 300,
            forge.PostResult(
                {"raw": "x" * 300},
                None,
                "Could not parse API response as JSON: " + "x" * 200,
            ),
        ),
    ],
    ids=["blank", "array", "null", "raw-warning", "bounded-warning"],
)
def test_submit_response(stdout, expected, monkeypatch):
    monkeypatch.setattr(
        proc,
        "run",
        lambda cmd, **kw: proc.CompletedProcess(cmd, 0, stdout, ""),
    )
    assert forge.GitHub().submit(forge.github_review_request(TARGET, {})) == expected


@pytest.mark.parametrize(
    "failure",
    [
        None,
        OSError("cannot run"),
        proc.TimeoutExpired(["fixture"], 1),
        UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid"),
    ],
    ids=["rejected", "oserror", "timeout", "unicode-error"],
)
def test_submit_failure_cleans_temp(failure, monkeypatch, tmp_path):
    paths = []
    real_mkstemp = forge.tempfile.mkstemp
    command_seen = []

    def mkstemp(**kwargs):
        fd, path = real_mkstemp(dir=tmp_path, **kwargs)
        paths.append(Path(path))
        return fd, path

    def run(command, **kwargs):
        command_seen.extend(command)
        assert paths[0].exists()
        if failure:
            raise failure
        return proc.CompletedProcess(command, 5, "ignored stdout", " rejected \n")

    monkeypatch.setattr(forge.tempfile, "mkstemp", mkstemp)
    monkeypatch.setattr(proc, "run", run)
    request = forge.gitlab_note_request(TARGET, {"body": "review"})
    if failure:
        with pytest.raises(type(failure)) as exc:
            forge.GitLab().submit(request)
        assert exc.value is failure
    else:
        result = forge.GitLab().submit(request)
        assert result == forge.PostResult(
            None,
            "API call failed (exit 5).\nCommand: "
            + " ".join(command_seen)
            + "\nstderr: rejected",
            None,
        )
    assert len(paths) == 1
    assert not paths[0].exists()


def test_submit_encoding_failure_cleans_temp(monkeypatch, tmp_path):
    paths = []
    real_mkstemp = forge.tempfile.mkstemp

    def mkstemp(**kwargs):
        fd, path = real_mkstemp(dir=tmp_path, **kwargs)
        paths.append(Path(path))
        return fd, path

    def run(*args, **kwargs):
        pytest.fail("an unencodable payload must not run the CLI")

    monkeypatch.setattr(forge.tempfile, "mkstemp", mkstemp)
    monkeypatch.setattr(proc, "run", run)
    with pytest.raises(TypeError):
        forge.GitHub().submit(forge.github_review_request(TARGET, {"bad": object()}))
    assert len(paths) == 1
    assert not paths[0].exists()


@pytest.mark.parametrize(
    "platform, expected", [("github", forge.GitHub), ("gitlab", forge.GitLab)]
)
def test_factory(platform, expected):
    adapter = forge.make_forge(platform)
    assert type(adapter) is expected
    assert adapter.platform == platform


@pytest.mark.parametrize("fake", [FakeForge, FakeGitLab], ids=["github", "gitlab"])
@pytest.mark.parametrize("queue_shape", ["ordered", "by-surface"])
def test_fake_queues_and_semantic_log(fake, queue_shape, monkeypatch):
    def transport(*args, **kwargs):
        pytest.fail("fake must never use production transport")

    monkeypatch.setattr(proc, "run", transport)
    monkeypatch.setattr(proc, "which", transport)
    replies = [
        forge.PostResult(None, "rejected", None),
        forge.PostResult({"id": 2}, None, None),
    ]
    adapter = fake(
        diffs=[("", "", 0), ("", "rejected", 1)],
        entries=[forge.JsonFetch([{"body": "summary"}], None)],
        submissions=replies
        if queue_shape == "ordered"
        else {"handwritten endpoint": replies},
    )
    adapter.ensure_available()
    assert adapter.diff(TARGET) == ("", "", 0)
    assert adapter.diff(TARGET) == ("", "rejected", 1)
    assert adapter.review_entries(TARGET) == forge.JsonFetch(
        [{"body": "summary"}], None
    )
    request = forge.PostRequest(
        adapter.platform, "handwritten endpoint", "POST", (), {"body": "review"}
    )
    assert adapter.submit(request) == forge.PostResult(None, "rejected", None)
    assert adapter.submit(request) == forge.PostResult({"id": 2}, None, None)
    assert adapter.calls == [
        ForgeCall("ensure_available"),
        ForgeCall("diff", target=TARGET),
        ForgeCall("diff", target=TARGET),
        ForgeCall("review_entries", target=TARGET),
        ForgeCall("submit", request=request),
        ForgeCall("submit", request=request),
    ]
    for method, argument in [
        (adapter.diff, TARGET),
        (adapter.review_entries, TARGET),
        (adapter.submit, request),
    ]:
        with pytest.raises(AssertionError, match=r"queue exhausted"):
            method(argument)
    if queue_shape == "by-surface":
        with pytest.raises(AssertionError, match="Unexpected submit surface"):
            adapter.submit(
                forge.PostRequest(adapter.platform, "unconfigured", "POST", (), {})
            )


def test_fake_gitlab_refs_and_no_inherited_transport(monkeypatch):
    def transport(*args, **kwargs):
        pytest.fail("fake must never inherit transport")

    monkeypatch.setattr(proc, "run", transport)
    failure = RuntimeError("rejected")
    fake = FakeGitLab(refs=[forge.JsonFetch([], None), failure])
    assert isinstance(fake, forge.GitLab)
    assert fake.diff_refs(TARGET) == forge.JsonFetch([], None)
    with pytest.raises(RuntimeError) as exc:
        fake.diff_refs(TARGET)
    assert exc.value is failure
    with pytest.raises(AssertionError, match=r"queue exhausted"):
        fake.diff_refs(TARGET)
    assert fake.calls == [ForgeCall("diff_refs", target=TARGET)] * 3


@pytest.mark.parametrize(
    "operation", ["ensure_available", "diff", "review_entries", "submit"]
)
def test_fake_queued_exception(operation):
    failure = OSError("fixture failure")
    fake = FakeForge(
        availability=[failure],
        diffs=[failure],
        entries=[failure],
        submissions=[failure],
    )
    args = (
        ()
        if operation == "ensure_available"
        else (forge.github_review_request(TARGET, {}),)
        if operation == "submit"
        else (TARGET,)
    )
    with pytest.raises(OSError) as exc:
        getattr(fake, operation)(*args)
    assert exc.value is failure


def test_fake_defaults_and_payload_snapshot():
    fake = FakeForge()
    assert fake.review_entries(TARGET) == forge.JsonFetch([], None)
    payload = {"comments": [{"body": "before"}]}
    request = forge.github_review_request(TARGET, payload)
    assert fake.submit(request) == forge.PostResult({}, None, None)
    payload["comments"][0]["body"] = "after"
    assert fake.calls[-1].request.payload == {"comments": [{"body": "before"}]}
    with pytest.raises(AssertionError, match=r"queue exhausted"):
        fake.diff(TARGET)


def test_submit_and_fake_accept_mapping_payload(monkeypatch):
    payload = MappingProxyType({"body": "review"})
    request = forge.github_review_request(TARGET, payload)
    recorded = []

    def run(command, **kwargs):
        recorded.append(Path(command[-1]).read_bytes())
        return proc.CompletedProcess(command, 0, "{}", "")

    monkeypatch.setattr(proc, "run", run)
    assert forge.GitHub().submit(request) == forge.PostResult({}, None, None)
    assert recorded == [b'{"body": "review"}']
    fake = FakeForge()
    assert fake.submit(request) == forge.PostResult({}, None, None)
    assert fake.calls[-1].request.payload == {"body": "review"}


def test_factory_installer_targets_consumer_and_restores(monkeypatch):
    for module in (post, prior_review):
        original = getattr(module, "make_forge", None)
        with monkeypatch.context() as scoped:
            factory = install_forge_factory(scoped, module)
            github, gitlab = FakeForge(), FakeGitLab()
            factory.configure(github)
            factory.configure(gitlab)
            assert module.make_forge("gitlab") is gitlab
            assert module.make_forge("github") is github
            assert factory.calls == ["gitlab", "github"]
        assert getattr(module, "make_forge", None) is original


def test_shared_factory_fixture(forge_factory):
    fake = forge_factory.configure(FakeForge())
    assert post.make_forge("github") is fake
    assert forge_factory.calls == ["github"]


def test_factory_requires_configuration(forge_factory):
    with pytest.raises(AssertionError, match=r"No fake configured for gitlab"):
        post.make_forge("gitlab")

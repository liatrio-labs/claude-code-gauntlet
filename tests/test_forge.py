"""Forge policy, transport and wired poster contracts."""

import ast
import re
import sys
import tempfile
from collections.abc import Callable
from dataclasses import FrozenInstanceError
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal, TypedDict, cast

import pytest
from _pytest.mark.structures import ParameterSet
from gauntlet import forge, prior_review, proc
from gauntlet.delivery import post

from tests.support.forge import FakeForge, FakeGitLab, ForgeCall, install_forge_factory

PUBLIC_HOST_CASES: list[ParameterSet] = [
    pytest.param("https://{host}/o/r.git", True, "{host}", id="https"),
    pytest.param("http://{host}/o/r", True, "{host}", id="http"),
    pytest.param("ssh://git@{host}:2222/o/r", True, "{host}", id="ssh"),
    pytest.param("ssh://git@{host}/o/r", True, "{host}", id="ssh-no-port"),
    pytest.param("alice@{host}:o/r.git", True, "{host}", id="scp-user"),
    pytest.param("alice@{host}:o/r", True, "{host}", id="scp-user-no-suffix"),
    pytest.param("git@{host}:o/r", True, "{host}", id="scp-no-suffix"),
    pytest.param("git@{host}:o/r.git", True, "{host}", id="scp-git-suffix"),
    pytest.param("alice@{host}:g/sub/r.git", True, "{host}", id="scp-subgroups"),
    pytest.param("git@{host}:o/my-repo_1.x", True, "{host}", id="scp-repo-grammar"),
    pytest.param("ssh://git@{host}/o/r.git", True, "{host}", id="ssh-git-suffix"),
    pytest.param("ssh://git@{host}:2222/g/sub/r", True, "{host}", id="ssh-subgroups"),
    pytest.param("{host}:o/r.git", True, "{host}", id="userless-scp"),
    pytest.param("{host}:o/r", True, "{host}", id="userless-scp-no-suffix"),
    pytest.param(
        "ssh://alice._-1@{host}/o/r", True, "{host}", id="ssh-username-grammar"
    ),
    pytest.param("a%40b@{host}:o/r", False, None, id="scp-percent-userinfo"),
    pytest.param("https://{host}", True, "{host}", id="pathless-url"),
    pytest.param("git@{host}:", True, "{host}", id="pathless-scp"),
    pytest.param("https://sub.{host}/o/r", True, "sub.{host}", id="subdomain"),
    pytest.param("https://evil{host}/o/r", False, "evil{host}", id="prefix"),
    pytest.param("https://{host}.example/o/r", False, "{host}.example", id="suffix"),
    pytest.param("ftp://{host}/o/r", False, "{host}", id="ftp"),
    pytest.param("git://{host}/o/r", False, "{host}", id="git-scheme"),
    pytest.param("git+ssh://git@{host}/o/r", False, "{host}", id="git-ssh"),
    pytest.param("HTTPS://{host}/o/r", False, "{host}", id="uppercase-scheme"),
    pytest.param("Https://{host}/o/r", False, "{host}", id="mixedcase-scheme"),
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
        "https://user%40x:tok@{host}/o/r", True, "{host}", id="percent-credentials"
    ),
    pytest.param("ssh://evil.com%2F@{host}/o/r", False, None, id="ssh-escaped-slash"),
    pytest.param(
        "ssh://git%40evil.com%2Fx@{host}/o/r", False, None, id="ssh-escaped-at-slash"
    ),
    pytest.param(
        "ssh://evil.com%3A22%2F@{host}/o/r", False, None, id="ssh-escaped-port-slash"
    ),
    pytest.param(
        "ssh://a%40%5Bevil.com%5D@{host}/o/r", False, None, id="ssh-escaped-brackets"
    ),
    pytest.param("ssh://%5B%3A%3A1%5D@{host}/o/r", False, None, id="ssh-escaped-ipv6"),
    pytest.param("ssh://evil.com%0A@{host}/o/r", False, None, id="ssh-escaped-newline"),
    pytest.param(
        "ssh://evil.com%2F@sub.{host}/o/r", False, None, id="ssh-escaped-subdomain"
    ),
    pytest.param(
        "ssh://fixture%40user@{host}:2222/group/sub/repo.git",
        False,
        None,
        id="ssh-percent-userinfo",
    ),
    pytest.param("ssh://a@b@{host}/o/r", False, None, id="ssh-double-at"),
    pytest.param("ssh://a#b@{host}/o/r", False, None, id="ssh-user-hash"),
    pytest.param("ssh://a?b@{host}/o/r", False, None, id="ssh-user-query"),
    pytest.param("ssh://a\\b@{host}/o/r", False, None, id="ssh-user-backslash"),
    pytest.param("ssh://a b@{host}/o/r", False, None, id="ssh-user-space"),
    pytest.param("ssh://a\tb@{host}/o/r", False, None, id="ssh-user-tab"),
    pytest.param("ssh://a\x7fb@{host}/o/r", False, None, id="ssh-user-control"),
    pytest.param("ssh://a\u00e9b@{host}/o/r", False, None, id="ssh-user-nonascii"),
    pytest.param("ssh://a:b@{host}/o/r", False, None, id="ssh-user-password"),
    pytest.param("ssh://@{host}/o/r", False, None, id="ssh-empty-user"),
    pytest.param("ssh://{host}:0/o/r", False, None, id="ssh-zero-port"),
    pytest.param("ssh://{host}:65536/o/r", False, None, id="ssh-high-port"),
    pytest.param("ssh://{host}:1/o/r", True, "{host}", id="ssh-min-port"),
    pytest.param("ssh://{host}:65535/o/r", True, "{host}", id="ssh-max-port"),
    pytest.param(
        "https://fixture\\user;name@{host}/o/r",
        True,
        "{host}",
        id="lexical-userinfo",
    ),
    pytest.param("https://fixture\tuser@{host}/o/r", False, None, id="tab-userinfo"),
    pytest.param(
        "https://fixture\nuser@{host}/o/r", False, None, id="newline-userinfo"
    ),
    pytest.param("https://fixture\x00user@{host}/o/r", False, None, id="c0-userinfo"),
    pytest.param(
        "https://fixture\uff0fuser@{host}/o/r", False, None, id="nfkc-userinfo"
    ),
    pytest.param(
        "https://{host}@evil.example/o/r",
        False,
        "evil.example",
        id="host-in-userinfo",
    ),
    pytest.param("git@{host}@evil.example:o/r", False, None, id="double-at-scp"),
    pytest.param("git@{host}#@evil.example:o/r", False, None, id="scp-fragment-at"),
    pytest.param("{host}?@evil.example:o/r", False, None, id="scp-query-at"),
    pytest.param(
        "ssh://git@{host}#@evil.example/o/r", False, None, id="ssh-fragment-at"
    ),
    pytest.param("ssh://{host}?@evil.example/o/r", False, None, id="ssh-query-at"),
    pytest.param("git@{host}#.evil.example:o/r", False, None, id="scp-fragment-host"),
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
    pytest.param("https://\u212a.{host}/o/r", False, None, id="kelvin-host"),
    pytest.param("http://\u212a.{host}/o/r", False, None, id="http-kelvin-host"),
    pytest.param(
        "https://" + "x" * 64 + ".{host}/o/r", False, None, id="overlong-label"
    ),
    pytest.param(
        "https://" + ("x" * 63 + ".") * 4 + "{host}/o/r",
        False,
        None,
        id="overlong-host",
    ),
]

HOST_CASES: list[ParameterSet] = [
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
        (None, "evilgithub.com"),
        id="github-prefix",
    ),
    pytest.param(
        "https://github.com.example.net/o/r.git",
        0,
        (None, "github.com.example.net"),
        id="github-suffix",
    ),
    pytest.param(
        "https://evilgitlab.com/g/r.git",
        0,
        (None, "evilgitlab.com"),
        id="gitlab-prefix",
    ),
    pytest.param(
        "https://gitlab.com.example.net/g/r.git",
        0,
        (None, "gitlab.com.example.net"),
        id="gitlab-suffix",
    ),
    pytest.param(
        "git@gitlab.internal.company.com:g/r.git",
        0,
        (None, "gitlab.internal.company.com"),
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
        "https://bitbucket.org/o/r.git", 0, (None, "bitbucket.org"), id="other-forge"
    ),
    pytest.param("", 0, (None, None), id="empty"),
    pytest.param("not-a-url", 0, (None, None), id="malformed"),
    pytest.param("", 1, (None, None), id="remote-nonzero"),
    pytest.param(
        "https://github.com:8443/o/r.git", 0, ("github", "github.com"), id="github-port"
    ),
    pytest.param(
        "https://gitlab.com:8443/g/r.git", 0, ("gitlab", "gitlab.com"), id="gitlab-port"
    ),
    pytest.param(
        "ssh://git@github.com:2222/o/r.git",
        0,
        ("github", "github.com"),
        id="github-ssh-url",
    ),
    pytest.param(
        "ssh://git@gitlab.com:2222/g/r.git",
        0,
        ("gitlab", "gitlab.com"),
        id="gitlab-ssh-url",
    ),
    pytest.param(
        "https://GITHUB.COM/o/r.git", 0, ("github", "github.com"), id="uppercase-host"
    ),
    pytest.param(
        "HTTPS://GITHUB.COM/o/r.git", 0, (None, "github.com"), id="uppercase-scheme"
    ),
    pytest.param(
        "git@GitLab.COM:g/r.git", 0, ("gitlab", "gitlab.com"), id="mixedcase-scp"
    ),
    pytest.param(
        "https://github.com@evil.example/o/r.git",
        0,
        (None, "evil.example"),
        id="userinfo-lookalike",
    ),
    pytest.param(
        "https://github.com.example.net:443/o/r.git",
        0,
        (None, "github.com.example.net"),
        id="suffix-port",
    ),
    pytest.param("https://github.com:bad/o/r.git", 0, (None, None), id="bad-port"),
    pytest.param("https://github.com./o/r.git", 0, (None, None), id="trailing-dot"),
    pytest.param("https://[::1]:8443/g/r.git", 0, (None, "::1"), id="ipv6"),
    pytest.param("git://host/o/r.git", 0, (None, "host"), id="git-scheme"),
    pytest.param("git+ssh://git@host/o/r", 0, (None, "host"), id="git-ssh-scheme"),
    pytest.param(
        "https://evil.example\\.github.com/o/r.git", 0, (None, None), id="backslash"
    ),
    pytest.param("https://evil.example .github.com/o/r", 0, (None, None), id="space"),
    pytest.param(
        "https://evil.example%23.github.com/o/r.git",
        0,
        (None, None),
        id="percent-escape",
    ),
    pytest.param(
        "https://evil.example;.github.com/o/r", 0, (None, None), id="semicolon"
    ),
    pytest.param("https://evil.example\t.github.com/o/r", 0, (None, None), id="tab"),
    pytest.param("https://.github.com/o/r.git", 0, (None, None), id="leading-dot"),
    pytest.param("https://x..github.com/o/r.git", 0, (None, None), id="empty-label"),
    pytest.param(
        "https://oauth2:fixture-token@gitlab.com/g/r.git",
        0,
        ("gitlab", "gitlab.com"),
        id="credentialed-gitlab",
    ),
    pytest.param(
        "https://fixture-user:fixture-token@github.com/o/r.git",
        0,
        ("github", "github.com"),
        id="credentialed-github",
    ),
    pytest.param(
        "https://evil.example@github.com/o/r.git",
        0,
        ("github", "github.com"),
        id="userinfo-public-host",
    ),
    pytest.param(
        "https://gitlab.example@evil.example/o/r.git",
        0,
        (None, "evil.example"),
        id="userinfo-private-lookalike",
    ),
    pytest.param(
        "git@github.com@evil.example:o/r.git",
        0,
        (None, None),
        id="double-at-scp",
    ),
    pytest.param(
        "https://fixture-user@github.com@evil.example/o/r.git",
        0,
        (None, "evil.example"),
        id="double-at-https",
    ),
    pytest.param(
        "ssh://git@gitlab.com", 0, ("gitlab", "gitlab.com"), id="pathless-ssh"
    ),
    pytest.param("https://[::1/o/r", 0, (None, None), id="invalid-bracket"),
    pytest.param(
        "ssh://git@github.com", 0, ("github", "github.com"), id="github-pathless-ssh"
    ),
    pytest.param("C:/github.com/o/r", 0, (None, None), id="drive-forward"),
    pytest.param("C:\\github.com\\o\\r", 0, (None, None), id="drive-backslash"),
    pytest.param("/git@github.com:o/r", 0, (None, None), id="local-absolute"),
    pytest.param("\\git@github.com:o/r", 0, (None, None), id="local-backslash"),
    pytest.param("directory/github.com:o/r", 0, (None, None), id="slash-before-colon"),
    pytest.param("https://github.com\t\n", 0, (None, None), id="github-pathless-tab"),
    pytest.param("https://gitlab.com \n", 0, (None, None), id="gitlab-pathless-space"),
    pytest.param(
        "https://github.com\n\n", 0, (None, None), id="github-pathless-newline"
    ),
    pytest.param(
        "git@evil.example:x@github.com/r",
        0,
        (None, None),
        id="github-at-in-path",
    ),
    pytest.param(
        "evil.example:x@github.com/r",
        0,
        (None, None),
        id="github-at-in-userless-path",
    ),
    pytest.param(
        "git@evil.example:x@github.com:o/r",
        0,
        (None, None),
        id="github-colon-in-path",
    ),
    pytest.param(
        "evil.example:x@github.com:o/r",
        0,
        (None, None),
        id="github-colon-in-userless-path",
    ),
    pytest.param(
        "git@evil.example:x@gitlab.com/r",
        0,
        (None, None),
        id="gitlab-at-in-path",
    ),
    pytest.param(
        "evil.example:x@gitlab.com/r",
        0,
        (None, None),
        id="gitlab-at-in-userless-path",
    ),
    pytest.param(
        "git@evil.example:x@gitlab.com:o/r",
        0,
        (None, None),
        id="gitlab-colon-in-path",
    ),
    pytest.param(
        "evil.example:x@gitlab.com:o/r",
        0,
        (None, None),
        id="gitlab-colon-in-userless-path",
    ),
    pytest.param(
        "git@github.com:own@er/repo",
        0,
        (None, None),
        id="github-at-in-owner",
    ),
    pytest.param(
        "git@gitlab.com:own@er/repo",
        0,
        (None, None),
        id="gitlab-at-in-owner",
    ),
    pytest.param(
        "https://[v1.github.com]/o/r.git", 0, (None, None), id="github-ipvfuture"
    ),
    pytest.param(
        "https://[v1.gitlab.com]/o/r.git", 0, (None, None), id="gitlab-ipvfuture"
    ),
    pytest.param(
        "git@[v1.github.com]:o/r.git", 0, (None, None), id="github-ipvfuture-scp"
    ),
    pytest.param(
        "git@[v1.gitlab.com]:o/r.git", 0, (None, None), id="gitlab-ipvfuture-scp"
    ),
]

_HOST_TRANSITION_ROWS = {case.values[0]: case.values for case in HOST_CASES}
_GENERATED_HOST_CASES = [
    pytest.param(
        template.format(host=host),
        0,
        (
            platform if selected else None,
            host_template.format(host=host) if host_template else None,
        ),
        id=f"policy-{platform}-{row.id}",
    )
    for platform, host in (("github", "github.com"), ("gitlab", "gitlab.com"))
    for row in PUBLIC_HOST_CASES
    for template, selected, host_template in [
        cast("tuple[str, bool, str | None]", row.values)
    ]
]
# A literal row may replace a generated twin only when its complete oracle agrees.
for _case in _GENERATED_HOST_CASES:
    if _case.values[0] in _HOST_TRANSITION_ROWS:
        assert _HOST_TRANSITION_ROWS[_case.values[0]] == _case.values
HOST_CASES += [
    case
    for case in _GENERATED_HOST_CASES
    if case.values[0] not in _HOST_TRANSITION_ROWS
]
HOST_CASES += [
    pytest.param(template.format(host=host), 0, (None, None), id=f"{name}-{host}")
    for host in ("github.com", "gitlab.com")
    for name, template in (
        ("scp-path-bracket-user", "{host}:x@[evil.com]:o/r"),
        ("scp-path-bracket", "git@{host}:o/r@[evil.com]:x"),
        ("ssh-path-bracket", "ssh://git@{host}/o/r@[evil.com]/x"),
        ("ssh-path-escaped-bracket", "ssh://git@{host}/o/r%40%5Bevil.com%5D/x"),
        ("ssh-path-bracket-port", "ssh://{host}:22/o/r@[evil.com]:2222/x"),
        ("ssh-subdomain-path-bracket", "ssh://git@sub.{host}/o/r@[evil.com]/x"),
        ("ssh-uppercase-helper", "SSH://git@{host}/o/r"),
        ("ssh-mixedcase-helper", "Ssh://git@{host}/o/r"),
        ("scp-helper", "{host}::o/r"),
        ("scp-bracketed-port", "git@[{host}:22]:o/r"),
        ("scp-bracketed-dns", "git@[{host}]:o/r"),
        ("ssh-bracketed-dns", "ssh://git@[{host}]/o/r"),
        ("scp-path-at", "git@{host}:o/r@x"),
        ("scp-path-open-bracket", "git@{host}:o/r[x"),
        ("ssh-path-at", "ssh://git@{host}/o/r@x"),
        ("ssh-path-open-bracket", "ssh://git@{host}/o/r[x"),
    )
]
HOST_CASES += [
    pytest.param(
        url + ("@[evil.com]/x" if url.startswith("ssh://") else "@[evil.com]:x"),
        0,
        (None, None),
        id=f"appended-bracket-{case.id}",
    )
    for case in HOST_CASES
    for url, status, expected in [
        cast("tuple[str, int, tuple[str | None, str | None]]", case.values)
    ]
    if status == 0
    and expected[0] is not None
    and (url.startswith("ssh://") or "://" not in url)
]


@pytest.mark.parametrize(
    "url, status, expected",
    [
        case
        for case in HOST_CASES
        if case.values[1] == 0
        and cast("tuple[str | None, str | None]", case.values[2])[0] is None
    ],
)
def test_unknown_host_detection_tuple(
    url: str, status: int, expected: tuple[None, str | None]
) -> None:
    assert forge.detect_platform(forge.parse_remote(url)) == forge.PlatformDetection(
        *expected
    )


@pytest.mark.parametrize(
    "stdout, host",
    [
        (" https://github.com/o/r.git \n", None),
        ("https://github.com/o/r.git \n", "github.com"),
        ("https://github.com/o/r.git\r\n", "github.com"),
    ],
    ids=["leading-space", "lexical-tail-space", "git-crlf"],
)
def test_origin_authority_validation_preserves_lexical_slug(stdout, host, monkeypatch):
    monkeypatch.setattr(
        proc,
        "run",
        lambda command, **kwargs: proc.CompletedProcess(command, 0, stdout, ""),
    )
    remote = forge.origin_remote()
    assert remote is not None
    assert remote.hostname == host
    assert forge.remote_slug(remote) == forge.RepoSlug("o", "r")


@pytest.mark.parametrize("host", ["github.com", "gitlab.com"])
@pytest.mark.parametrize(
    "template, expected",
    [
        ("git@{host}#@evil.example:o/r", forge.RepoSlug("o", "r")),
        ("{host}?@evil.example:o/r", forge.RepoSlug("o", "r")),
        ("ssh://git@{host}#@evil.example/o/r", forge.RepoSlug("o", "r")),
        ("ssh://{host}?@evil.example/o/r", forge.RepoSlug("o", "r")),
        ("git@{host}#.evil.example:o/r", forge.RepoSlug("o", "r")),
    ],
)
def test_ssh_authority_delimiters_preserve_slug(
    host: str, template: str, expected: forge.RepoSlug
) -> None:
    assert forge.remote_slug(forge.parse_remote(template.format(host=host))) == expected


@pytest.mark.parametrize(
    "platform, host", [("github", "github.com"), ("gitlab", "gitlab.com")]
)
@pytest.mark.parametrize("template, selected, host_template", PUBLIC_HOST_CASES)
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
            "ssh://git@[2001:DB8::1]/o/r", (None, "2001:db8::1"), id="ipv6-case"
        ),
        pytest.param(
            "ssh://fixture%40user@GITHUB.COM:2222/group/sub/repo.git",
            (None, None),
            id="ssh-percent-userinfo-uppercase",
        ),
        pytest.param("https://[::1]extra/o/r", (None, None), id="bracket-suffix"),
        pytest.param("https://[127.0.0.1]/o/r", (None, None), id="bracketed-ipv4"),
        pytest.param("https://[::1%25fixture]/o/r", (None, None), id="ipv6-percent"),
        pytest.param("https://::1/o/r", (None, None), id="unbracketed-ipv6"),
        pytest.param("./github.com:o/r", (None, None), id="local-relative"),
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
    "url, expected, hostname",
    [
        pytest.param(
            "git@github.com:owner/repo.git", ("owner", "repo"), "github.com", id="scp"
        ),
        pytest.param(
            "alice@host:group/sub/repo.git",
            ("group", "sub/repo"),
            "host",
            id="subgroups",
        ),
        pytest.param(
            "https://host/owner/repo", ("owner", "repo"), "host", id="no-suffix"
        ),
        pytest.param(
            "ssh://git@host/owner/repo.git", ("owner", "repo"), "host", id="ssh-url"
        ),
        pytest.param(
            "ssh://git@host:2222/owner/repo.git",
            ("owner", "repo"),
            "host",
            id="ssh-port",
        ),
        pytest.param(
            "git://host/owner/repo.git", ("owner", "repo"), "host", id="git-scheme"
        ),
        pytest.param(
            "https://host/owner/repo.git/",
            ("owner", "repo"),
            "host",
            id="one-trailing-slash",
        ),
        pytest.param(
            "https://host/owner/repo.git//",
            ("owner", "repo.git"),
            "host",
            id="multiple-trailing-slashes",
        ),
        pytest.param(
            "https://host/owner/repo.git?query",
            ("owner", "repo.git?query"),
            "host",
            id="query-tail",
        ),
        pytest.param(
            "https://host/owner/repo.git#fragment",
            ("owner", "repo.git#fragment"),
            "host",
            id="fragment-tail",
        ),
        pytest.param(
            "https://bad host:bad/owner/repo.git",
            ("owner", "repo"),
            None,
            id="invalid-authority",
        ),
        pytest.param(
            "git+ssh://user@host/owner/repo.git",
            ("owner", "repo"),
            "host",
            id="other-scheme",
        ),
        pytest.param(
            "HTTPS://HOST/owner/repo.git",
            ("owner", "repo"),
            "host",
            id="uppercase-scheme",
        ),
        pytest.param(
            "Https://HOST/owner/repo.git",
            ("owner", "repo"),
            "host",
            id="mixedcase-scheme",
        ),
        pytest.param(
            "github.com:owner/repo.git",
            ("owner", "repo"),
            "github.com",
            id="userless-scp",
        ),
        pytest.param(
            "github.com:o/r", ("o", "r"), "github.com", id="userless-scp-no-suffix"
        ),
        pytest.param(
            "github.com:o/r/",
            ("o", "r"),
            "github.com",
            id="userless-scp-trailing-slash",
        ),
        pytest.param(
            "github.com:o/r.git/",
            ("o", "r"),
            "github.com",
            id="userless-scp-git-trailing-slash",
        ),
        pytest.param(
            "ssh://git@[2001:DB8::1]:2222/owner/repo.git",
            ("owner", "repo"),
            "2001:db8::1",
            id="ssh-ipv6-port",
        ),
        pytest.param("git@host:/o/r.git", ("o", "r"), "host", id="absolute-scp-path"),
        pytest.param(
            "fixture:token@github.com:owner/repo.git",
            ("owner", "repo"),
            None,
            id="colon-userinfo-scp",
        ),
        pytest.param(
            "x://github.com/owner/repo.git",
            ("owner", "repo"),
            "github.com",
            id="single-letter-scheme",
        ),
        pytest.param(
            "git@[::1]:owner/repo.git", ("owner", "repo"), "::1", id="ipv6-scp"
        ),
        pytest.param(
            "[::1]:owner/repo.git", ("owner", "repo"), "::1", id="ipv6-scp-no-user"
        ),
        pytest.param(
            "git@[::1]:owner/repo", ("owner", "repo"), "::1", id="ipv6-scp-no-suffix"
        ),
        pytest.param(
            "[::1]:owner/repo",
            ("owner", "repo"),
            "::1",
            id="ipv6-scp-no-user-no-suffix",
        ),
        pytest.param("https://host/owner", None, "host", id="missing-repo"),
        pytest.param("https://host", None, "host", id="pathless"),
        pytest.param("not-a-url", None, None, id="malformed"),
        pytest.param(
            "git@host:a@b/c.git", ("a@b", "c"), None, id="scp-at-in-owner-suffix"
        ),
        pytest.param(
            "org-123@git.example.com:team@x/r.git",
            ("team@x", "r"),
            None,
            id="scp-at-in-team",
        ),
        pytest.param(
            "git@github.com:own@er/repo",
            ("own@er", "repo"),
            None,
            id="public-scp-at-in-owner",
        ),
        pytest.param("@host:o/r", None, None, id="empty-user"),
        pytest.param("git@:o/r", None, None, id="empty-host"),
        pytest.param("user@host:o/r\nx", None, None, id="newline-in-path"),
        pytest.param("\\\\srv@h:o/r", ("o", "r"), None, id="unc-lexical-slug"),
        pytest.param("C:x@h:o/r", ("o", "r"), None, id="drive-lexical-slug"),
        pytest.param("git@h:o/r://x", ("o", "r://x"), None, id="scheme-token-in-path"),
        pytest.param("user@host:o/r\n", ("o", "r"), None, id="terminal-newline"),
    ],
)
def test_remote_slug(url, expected, hostname, monkeypatch: pytest.MonkeyPatch):
    remote = forge.parse_remote(url)
    assert (remote.hostname if remote else None) == hostname
    assert forge.remote_slug(remote) == (
        forge.RepoSlug(*expected) if expected else None
    )
    if forge._SSH_URL_RE.fullmatch(url) or (
        "://" not in url and forge._SCP_REMOTE_RE.fullmatch(url)
    ):
        # Accepted paths must survive the absence of lexical fallback extraction.
        monkeypatch.setattr(forge, "_SCP_PATH_RE", re.compile(r"(?!)"))
        monkeypatch.setattr(forge, "_URL_PATH_RE", re.compile(r"(?!)"))
        assert forge.remote_slug(forge.parse_remote(url)) == (
            forge.RepoSlug(*expected) if expected else None
        )


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
        ("myorg", "myrepo", "myorg%2Fmyrepo"),
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
        cast(Any, request).method = "GET"


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


class OriginKwargs(TypedDict, total=False):
    timeout: float | None
    errors: Literal["strict", "replace"]


@pytest.mark.parametrize(
    "kwargs, outcome, expected, call_kwargs",
    [
        pytest.param(
            {"timeout": 10, "errors": "replace"},
            FileNotFoundError("missing git"),
            FileNotFoundError,
            {"cwd": None, "timeout": 10, "errors": "replace"},
            id="missing-git-replace",
        ),
        pytest.param(
            {"timeout": 10, "errors": "replace"},
            ("\ufffd\ufffd not a remote", "", 0),
            None,
            {"cwd": None, "timeout": 10, "errors": "replace"},
            id="nonutf8-unusable-replace",
        ),
        pytest.param(
            {},
            proc.TimeoutExpired(["git", "remote", "get-url", "origin"], 10),
            proc.TimeoutExpired,
            {"cwd": None, "timeout": None, "errors": "strict"},
            id="timeout-strict",
        ),
        pytest.param(
            {"timeout": 10, "errors": "replace"},
            proc.TimeoutExpired(["git", "remote", "get-url", "origin"], 10),
            proc.TimeoutExpired,
            {"cwd": None, "timeout": 10, "errors": "replace"},
            id="timeout-replace",
        ),
        pytest.param(
            {},
            ("https://github.com/o/r.git", "rejected", 1),
            None,
            {"cwd": None, "timeout": None, "errors": "strict"},
            id="nonzero-strict",
        ),
        pytest.param(
            {"timeout": 10, "errors": "replace"},
            ("https://github.com/o/r.git", "rejected", 1),
            None,
            {"cwd": None, "timeout": 10, "errors": "replace"},
            id="nonzero-replace",
        ),
    ],
)
def test_origin_policy(
    kwargs: OriginKwargs,
    outcome: Exception | tuple[str, str, int],
    expected: type[Exception] | forge.RepoSlug | None,
    call_kwargs: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[object, dict[str, object]]] = []

    def run(command: list[str], **options: object) -> proc.CompletedProcess[str]:
        calls.append((command, options))
        if isinstance(outcome, Exception):
            raise outcome
        stdout, stderr, status = outcome
        return proc.CompletedProcess(command, status, stdout, stderr)

    monkeypatch.setattr(proc, "run", run)
    if isinstance(expected, type):
        with pytest.raises(expected) as exc:
            forge.origin_remote(**kwargs)
        assert exc.value is outcome
    else:
        assert forge.remote_slug(forge.origin_remote(**kwargs)) == expected
    assert calls == [(["git", "remote", "get-url", "origin"], call_kwargs)]


def test_origin_replacement_child(monkeypatch: pytest.MonkeyPatch) -> None:
    real_run = proc.run
    calls: list[tuple[object, dict[str, object]]] = []
    child = r'import sys; sys.stdout.buffer.write(b"https://github.com/o/r\xff.git\n")'

    def run(
        command: list[str],
        *,
        cwd: str | None = None,
        timeout: float | None = None,
        errors: Literal["strict", "replace"] = "strict",
    ) -> proc.CompletedProcess[str]:
        calls.append((command, {"cwd": cwd, "timeout": timeout, "errors": errors}))
        return real_run(
            [sys.executable, "-c", child], cwd=cwd, timeout=timeout, errors=errors
        )

    monkeypatch.setattr(proc, "run", run)
    assert forge.remote_slug(
        forge.origin_remote(timeout=10, errors="replace")
    ) == forge.RepoSlug("o", "r\ufffd")
    assert calls == [
        (
            ["git", "remote", "get-url", "origin"],
            {"cwd": None, "timeout": 10, "errors": "replace"},
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


@pytest.fixture
def tracked_temp(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> list[Path]:
    paths: list[Path] = []
    real_mkstemp = tempfile.mkstemp

    def mkstemp(**kwargs: Any) -> tuple[int, str]:
        fd, path = real_mkstemp(dir=tmp_path, **kwargs)
        paths.append(Path(path))
        return fd, path

    monkeypatch.setattr(tempfile, "mkstemp", mkstemp)
    return paths


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
    ],
    ids=["github-review", "gitlab-note"],
)
def test_submit_transport(adapter, builder, argv, monkeypatch, tracked_temp):
    paths = tracked_temp
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        assert command == [*argv, "--input", str(paths[0])]
        assert (
            paths[0].read_bytes()
            == b'{"body": "caf\xc3\xa9\\nline", "event": "COMMENT", "comments": []}'
        )
        return proc.CompletedProcess(command, 0, '{"id":7}', "")

    monkeypatch.setattr(proc, "run", run)
    assert adapter().submit(
        builder(TARGET, {"body": "caf\u00e9\nline", "event": "COMMENT", "comments": []})
    ) == forge.PostResult({"id": 7}, None, None)
    assert len(paths) == len(calls) == 1
    assert calls[0][1] == {"cwd": None, "timeout": None, "errors": "strict"}
    assert not paths[0].exists()


@pytest.mark.parametrize(
    "adapter, post_request",
    [
        pytest.param(
            forge.GitHub,
            forge.PostRequest("gitlab", "projects/1", "POST", (), {}),
            id="github-refuses-gitlab",
        ),
        pytest.param(
            forge.GitLab,
            forge.PostRequest("github", "repos/o/r", "POST", (), {}),
            id="gitlab-refuses-github",
        ),
    ],
)
def test_submit_refuses_foreign_platform(
    adapter: type[forge.GitHub] | type[forge.GitLab],
    post_request: forge.PostRequest,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[object] = []

    def output(*args: object, **kwargs: object) -> tuple[str, str, int]:
        calls.append((args, kwargs))
        return "{}", "", 0

    def mkstemp(*args: object, **kwargs: object) -> tuple[int, str]:
        calls.append((args, kwargs))
        raise AssertionError("Foreign request must be refused before temp creation")

    monkeypatch.setattr(proc, "output", output)
    monkeypatch.setattr("tempfile.mkstemp", mkstemp)
    with pytest.raises(
        ValueError, match=r"^Request platform does not match forge platform$"
    ):
        adapter().submit(post_request)
    assert calls == []


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
    "adapter, post_request, tool, argv",
    [
        pytest.param(
            forge.GitHub,
            forge.github_review_request(TARGET, {}),
            "fixture-gh",
            [
                "fixture-gh",
                "api",
                "--method",
                "POST",
                "--fixture-header",
                "Accept: application/vnd.github+json",
                "repos/group/sub/repo/pulls/9/reviews",
            ],
            id="github",
        ),
        pytest.param(
            forge.GitLab,
            forge.gitlab_note_request(TARGET, {}),
            "fixture-glab",
            [
                "fixture-glab",
                "api",
                "--method",
                "POST",
                "--fixture-header",
                "Content-Type: application/json",
                "projects/group%2Fsub%2Frepo/merge_requests/9/notes",
            ],
            id="gitlab",
        ),
    ],
)
def test_adapter_owns_transport_data(
    adapter: type[forge.GitHub] | type[forge.GitLab],
    post_request: forge.PostRequest,
    tool: str,
    argv: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    looked_up: list[str] = []
    commands: list[list[str]] = []

    def which(name: str) -> str:
        looked_up.append(name)
        return name

    def output(command: list[str]) -> tuple[str, str, int]:
        commands.append(command[:-2])
        assert command[-2] == "--input"
        return "{}", "", 0

    monkeypatch.setattr(adapter, "_tool", tool)
    monkeypatch.setattr(adapter, "_header_flag", "--fixture-header")
    monkeypatch.setattr(proc, "which", which)
    monkeypatch.setattr(proc, "output", output)
    instance = adapter()
    instance.ensure_available()
    assert instance.submit(post_request) == forge.PostResult({}, None, None)
    assert looked_up == [tool]
    assert commands == [argv]


@pytest.mark.parametrize(
    "failure, payload, expected_exception",
    [
        pytest.param(None, {"body": "review"}, None, id="rejected"),
        pytest.param(OSError("cannot run"), {"body": "review"}, OSError, id="oserror"),
        pytest.param(
            proc.TimeoutExpired(["fixture"], 1),
            {"body": "review"},
            proc.TimeoutExpired,
            id="timeout",
        ),
        pytest.param(
            UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid"),
            {"body": "review"},
            UnicodeDecodeError,
            id="unicode-error",
        ),
        pytest.param("encoding", {"bad": object()}, TypeError, id="encoding"),
    ],
)
def test_submit_failure_cleans_temp(
    failure: Exception | Literal["encoding"] | None,
    payload: dict[str, object],
    expected_exception: type[Exception] | None,
    monkeypatch: pytest.MonkeyPatch,
    tracked_temp: list[Path],
) -> None:
    paths = tracked_temp
    command_seen: list[str] = []

    def run(command: list[str], **kwargs: object) -> proc.CompletedProcess[str]:
        if failure == "encoding":
            pytest.fail("an unencodable payload must not run the CLI")
        command_seen.extend(command)
        assert paths[0].exists()
        if isinstance(failure, Exception):
            raise failure
        return proc.CompletedProcess(command, 5, "ignored stdout", " rejected \n")

    monkeypatch.setattr(proc, "run", run)
    request = forge.gitlab_note_request(TARGET, payload)
    if expected_exception is not None:
        with pytest.raises(expected_exception) as exc:
            forge.GitLab().submit(request)
        if isinstance(failure, Exception):
            assert exc.value is failure
    else:
        assert forge.GitLab().submit(request) == forge.PostResult(
            None,
            "API call failed (exit 5).\nCommand: "
            + " ".join(command_seen)
            + "\nstderr: rejected",
            None,
        )
    assert len(paths) == 1
    assert not paths[0].exists()


@pytest.mark.parametrize(
    "call",
    [
        pytest.param(lambda: forge.GitHub().diff(TARGET), id="github-diff"),
        pytest.param(lambda: forge.GitLab().diff(TARGET), id="gitlab-diff"),
        pytest.param(lambda: forge.GitLab().diff_refs(TARGET), id="gitlab-versions"),
        pytest.param(post.get_head_sha, id="local-head-sha"),
    ],
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
def test_process_exceptions_propagate(
    call: Callable[[], object], failure: Exception, monkeypatch: pytest.MonkeyPatch
) -> None:
    def run(command: object, **kwargs: object) -> proc.CompletedProcess[str]:
        assert kwargs == {"cwd": None, "timeout": None, "errors": "strict"}
        raise failure

    monkeypatch.setattr(proc, "run", run)
    with pytest.raises(type(failure)) as exc:
        call()
    assert exc.value is failure


@pytest.mark.parametrize(
    "platform, expected", [("github", forge.GitHub), ("gitlab", forge.GitLab)]
)
def test_factory(platform, expected):
    adapter = forge.make_forge(platform)
    assert type(adapter) is expected
    assert adapter.platform == platform


@pytest.mark.parametrize("fake", [FakeForge, FakeGitLab], ids=["github", "gitlab"])
@pytest.mark.parametrize("queue_shape", ["ordered", "by-surface"])
def test_fake_queues_and_semantic_log(
    fake: type[FakeForge],
    queue_shape: Literal["ordered", "by-surface"],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def transport(*args: object, **kwargs: object) -> None:
        pytest.fail("fake must never use production transport")

    monkeypatch.setattr(proc, "run", transport)
    monkeypatch.setattr(proc, "which", transport)
    failure = OSError("fixture failure")
    replies: list[forge.PostResult | Exception] = [
        failure,
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
    with pytest.raises(OSError) as exc:
        adapter.submit(request)
    assert exc.value is failure
    assert adapter.submit(request) == forge.PostResult(None, "rejected", None)
    assert adapter.submit(request) == forge.PostResult({"id": 2}, None, None)
    assert adapter.calls == [
        ForgeCall("ensure_available"),
        ForgeCall("diff", target=TARGET),
        ForgeCall("diff", target=TARGET),
        ForgeCall("review_entries", target=TARGET),
        ForgeCall("submit", request=request),
        ForgeCall("submit", request=request),
        ForgeCall("submit", request=request),
    ]
    exhausted: list[tuple[Callable[[Any], object], object]] = [
        (adapter.diff, TARGET),
        (adapter.review_entries, TARGET),
        (adapter.submit, request),
    ]
    for method, argument in exhausted:
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


def test_fake_defaults_and_payload_snapshot():
    fake = FakeForge()
    assert fake.review_entries(TARGET) == forge.JsonFetch([], None)
    payload = {"comments": [{"body": "before"}]}
    request = forge.github_review_request(TARGET, payload)
    assert fake.submit(request) == forge.PostResult({}, None, None)
    payload["comments"][0]["body"] = "after"
    assert fake.calls[-1].request.payload == {"comments": [{"body": "before"}]}


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


def test_factory_requires_configuration(forge_factory):
    with pytest.raises(AssertionError, match=r"No fake configured for gitlab"):
        post.make_forge("gitlab")


def _forge_ownership_violations(source: str, *, consumer: bool) -> list[int]:
    tree = ast.parse(source)
    docstrings = {
        id(node.body[0].value)
        for node in ast.walk(tree)
        if isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        )
        and node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
        and isinstance(node.body[0].value.value, str)
    }
    names: dict[str, str] = {}

    def literal(node: ast.expr) -> str | None:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        if isinstance(node, ast.Name):
            return names.get(node.id)
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            left, right = literal(node.left), literal(node.right)
            return left + right if left is not None and right is not None else None
        return None

    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and (value := literal(node.value)) is not None:
            for target in node.targets:
                if isinstance(target, ast.Name):
                    names[target.id] = value
    violations: list[int] = []
    forge_aliases: set[str] = {"forge"}
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and node.value in {"gh", "glab"}
            and id(node) not in docstrings
        ):
            violations.append(node.lineno)
        if (
            isinstance(node, (ast.List, ast.Tuple))
            and node.elts
            and literal(node.elts[0]) in {"gh", "glab"}
        ):
            violations.append(node.lineno)
        if not consumer:
            continue
        if isinstance(node, ast.ImportFrom):
            if node.module == "subprocess":
                violations.append(node.lineno)
            if node.module == "gauntlet.forge" and any(
                alias.name.startswith("_")
                or alias.name in {"*", "run", "api", "run_api"}
                for alias in node.names
            ):
                violations.append(node.lineno)
            if node.module == "gauntlet":
                forge_aliases.update(
                    alias.asname or alias.name
                    for alias in node.names
                    if alias.name == "forge"
                )
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "subprocess":
                    violations.append(node.lineno)
                elif alias.name == "gauntlet.forge":
                    forge_aliases.add(alias.asname or "gauntlet.forge")
        if (
            isinstance(node, ast.Attribute)
            and ast.unparse(node.value) in forge_aliases
            and (node.attr.startswith("_") or node.attr in {"run", "api", "run_api"})
        ):
            violations.append(node.lineno)
    return violations


def test_executable_argv_ownership():
    root = Path(__file__).resolve().parents[1] / "scripts" / "gauntlet"
    paths: dict[str, Path] = {
        path.relative_to(root).as_posix(): path for path in root.rglob("*.py")
    }
    consumers: set[str] = {"prior_review.py", "delivery/post.py"}
    assert {"forge.py", *consumers} <= paths.keys()
    violations: list[str] = []
    for name, path in paths.items():
        if name == "forge.py":
            continue
        violations.extend(
            f"{name}:{line}"
            for line in _forge_ownership_violations(
                path.read_text(encoding="utf-8"), consumer=name in consumers
            )
        )
    assert violations == []


@pytest.mark.parametrize(
    "source, invalid",
    [
        pytest.param('proc.output(["gh", "api", "fixture"])', True, id="gh-call"),
        pytest.param('argv = ("glab", "mr", "diff")', True, id="glab-builder"),
        pytest.param('tool = "gh"\nproc.output([tool, "api"])', True, id="named-tool"),
        pytest.param('proc.output(["g" + "h", "api"])', True, id="literal-concat"),
        pytest.param(
            'tool: str = "gh"\nproc.output([tool, "api"])', True, id="annotated-tool"
        ),
        pytest.param(
            'tool = "gh" if p == "github" else "glab"\nproc.output([tool, "api", e])',
            True,
            id="conditional-tool",
        ),
        pytest.param(
            'proc.output([{"github": "gh"}[p], "api", e])', True, id="dict-tool"
        ),
        pytest.param('tool = "gh"', True, id="bare-tool"),
        pytest.param('"gh"', False, id="module-docstring"),
        pytest.param('class Tool:\n    "glab"', False, id="class-docstring"),
        pytest.param('def tool():\n    "gh"', False, id="function-docstring"),
        pytest.param('async def tool():\n    "glab"', False, id="async-docstring"),
        pytest.param('"documentation"\n"gh"', True, id="non-docstring-expression"),
        pytest.param("from gauntlet.forge import _submit", True, id="generic-import"),
        pytest.param(
            "from gauntlet import forge as f\nf._submit(request)",
            True,
            id="generic-attribute",
        ),
        pytest.param(
            "import subprocess\nsubprocess.run(command)", True, id="consumer-transport"
        ),
        pytest.param(
            '# gh api fixture\n"""Example: ["gh", "api"]"""', False, id="documentation"
        ),
        pytest.param(
            'from gauntlet import proc\nproc.output(["git", "rev-parse", "HEAD"])',
            False,
            id="local-git",
        ),
    ],
)
def test_ownership_guard_distinguishes_executable_text(source, invalid):
    assert bool(_forge_ownership_violations(source, consumer=True)) == invalid

"""Pin the current poster classifier before its host-rule replacement."""

import pytest
from gauntlet.delivery import post


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

    monkeypatch.setattr(post.proc, "output", origin_output)
    assert post.detect_platform() == expected

"""Forge policy, transport and wired poster contracts."""

import ast
import sys
import tempfile
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal, TypedDict

import pytest
from gauntlet import forge, proc
from gauntlet.delivery import post

from tests.support.forge import FakeForge, ForgeCall


@pytest.mark.parametrize(
    "stdout, host",
    [
        (" https://github.com/o/r.git \n", None),
        ("https://github.com/o/r.git\r\n", "github.com"),
    ],
    ids=["leading-space", "git-crlf"],
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
            "ssh://git@host:2222/owner/repo.git",
            ("owner", "repo"),
            "host",
            id="ssh-port",
        ),
        pytest.param(
            "https://host/owner/repo.git/",
            ("owner", "repo"),
            "host",
            id="one-trailing-slash",
        ),
        pytest.param(
            "https://host/owner/repo.git?query",
            ("owner", "repo.git?query"),
            "host",
            id="query-tail",
        ),
        pytest.param(
            "https://bad host:bad/owner/repo.git",
            ("owner", "repo"),
            None,
            id="invalid-authority",
        ),
        pytest.param(
            "HTTPS://HOST/owner/repo.git",
            ("owner", "repo"),
            "host",
            id="uppercase-scheme",
        ),
        pytest.param(
            "github.com:owner/repo.git",
            ("owner", "repo"),
            "github.com",
            id="userless-scp",
        ),
        pytest.param(
            "ssh://git@[2001:DB8::1]:2222/owner/repo.git",
            ("owner", "repo"),
            "2001:db8::1",
            id="ssh-ipv6-port",
        ),
        pytest.param("git@host:/o/r.git", ("o", "r"), "host", id="absolute-scp-path"),
        pytest.param("https://host/owner", None, "host", id="missing-repo"),
        pytest.param("not-a-url", None, None, id="malformed"),
        pytest.param(
            "git@host:own@er/repo", ("own@er", "repo"), None, id="scp-at-in-owner"
        ),
    ],
)
def test_remote_slug(url, expected, hostname):
    remote = forge.parse_remote(url)
    assert (remote.hostname if remote else None) == hostname
    assert forge.remote_slug(remote) == (
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
            "http://GitLab.Example:8080/group/sub/widget/"
            "-/merge_requests/9/?view=parallel",
            ("group/sub", "widget", 9, "http://gitlab.example:8080"),
        ),
    ],
    ids=["github", "enterprise", "nested-gitlab"],
)
def test_parse_pr_url(platform, url, expected):
    assert forge.parse_pr_url(platform, url) == forge.ParsedPrUrl(*expected)


@pytest.mark.parametrize(
    "url, error",
    [
        ("ssh://github.com/o/r/pull/1", "URL must use http(s) and include a host"),
        (
            "https://fixture-user@github.com/o/r/pull/1",
            "URL must not contain user information",
        ),
        ("https://github.com:/o/r/pull/1", "URL has an empty port"),
        ("https://github.com:0/o/r/pull/1", "URL port must be between 1 and 65535"),
        ("https://github.com./o/r/pull/1", "URL has an invalid DNS host"),
        ("https://github.com/o/r/pull/0", "URL path does not match a github PR URL"),
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
    ],
)
def test_parse_pr_url_unparseable(url):
    with pytest.raises(ValueError, match=r"^unparseable URL:"):
        forge.parse_pr_url("github", url)


@pytest.mark.parametrize(
    "owner, repo, expected",
    [
        ("group/sub", "repo", "group%2Fsub%2Frepo"),
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
            == f"'{tool}' CLI tool not found. Install it and ensure it is "
            "authenticated before running this script."
        )
    assert looked_up == [tool]


@pytest.mark.parametrize(
    "adapter, argv",
    [
        (forge.GitLab, ["glab", "mr", "diff", "9"]),
    ],
    ids=["gitlab"],
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
            forge.GitLab,
            [
                "glab",
                "api",
                "--paginate",
                "projects/group%2Fsub%2Frepo/merge_requests/9/notes",
            ],
        ),
    ],
    ids=["gitlab"],
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
    [(forge.GitHub, "github reviews")],
    ids=["github"],
)
@pytest.mark.parametrize(
    "stdout, stderr, status, payload, error_tail",
    [
        pytest.param("", "", 0, [], None, id="empty"),
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
    child = (
        r"import sys; sys.stdout.buffer.write("
        r'b"[{\"body\":\"bad\xff\"}]\n[{\"body\":\"tail\"}]")'
    )

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
            {},
            proc.TimeoutExpired(["git", "remote", "get-url", "origin"], 10),
            proc.TimeoutExpired,
            {"cwd": None, "timeout": None, "errors": "strict"},
            id="timeout-strict",
        ),
        pytest.param(
            {},
            ("https://github.com/o/r.git", "rejected", 1),
            None,
            {"cwd": None, "timeout": None, "errors": "strict"},
            id="nonzero-strict",
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
        pytest.param(
            "bad",
            "",
            0,
            forge.JsonFetch(None, "Could not parse MR versions response: bad"),
            id="invalid-json-text",
        ),
        pytest.param(
            "",
            " rejected \n",
            4,
            forge.JsonFetch(
                None,
                "Failed to fetch MR versions (exit 4): rejected\n"
                "Ensure glab is authenticated and the MR IID is correct.",
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
    ids=["gitlab-note"],
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
        (
            "x" * 300,
            forge.PostResult(
                {"raw": "x" * 300},
                None,
                "Could not parse API response as JSON: " + "x" * 200,
            ),
        ),
    ],
    ids=["blank", "bounded-warning"],
)
def test_submit_response(stdout, expected, monkeypatch):
    monkeypatch.setattr(
        proc,
        "run",
        lambda cmd, **kw: proc.CompletedProcess(cmd, 0, stdout, ""),
    )
    assert forge.GitHub().submit(forge.github_review_request(TARGET, {})) == expected


@pytest.mark.parametrize(
    "failure, payload, expected_exception",
    [
        pytest.param(None, {"body": "review"}, None, id="rejected"),
        pytest.param(OSError("cannot run"), {"body": "review"}, OSError, id="oserror"),
    ],
)
def test_submit_failure_cleans_temp(
    failure: Exception | None,
    payload: dict[str, object],
    expected_exception: type[Exception] | None,
    monkeypatch: pytest.MonkeyPatch,
    tracked_temp: list[Path],
) -> None:
    paths = tracked_temp
    command_seen: list[str] = []

    def run(command: list[str], **kwargs: object) -> proc.CompletedProcess[str]:
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
    "platform, expected", [("github", forge.GitHub), ("gitlab", forge.GitLab)]
)
def test_factory(platform, expected):
    adapter = forge.make_forge(platform, "git.example.com")
    assert type(adapter) is expected
    assert adapter.platform == platform


def test_fake_queues_and_semantic_log(monkeypatch: pytest.MonkeyPatch) -> None:
    def transport(*args: object, **kwargs: object) -> None:
        pytest.fail("fake must never use production transport")

    monkeypatch.setattr(proc, "run", transport)
    monkeypatch.setattr(proc, "which", transport)
    failure = OSError("fixture failure")
    adapter = FakeForge(
        diffs=[("", "", 0)],
        entries=[forge.JsonFetch([{"body": "summary"}], None)],
        submissions=[failure, forge.PostResult({"id": 2}, None, None)],
    )
    adapter.ensure_available()
    assert adapter.diff(TARGET) == ("", "", 0)
    assert adapter.review_entries(TARGET) == forge.JsonFetch(
        [{"body": "summary"}], None
    )
    request = forge.github_review_request(TARGET, {"body": "review"})
    with pytest.raises(OSError) as exc:
        adapter.submit(request)
    assert exc.value is failure
    assert adapter.submit(request) == forge.PostResult({"id": 2}, None, None)
    assert adapter.calls == [
        ForgeCall("ensure_available"),
        ForgeCall("diff", target=TARGET),
        ForgeCall("review_entries", target=TARGET),
        ForgeCall("submit", request=request),
        ForgeCall("submit", request=request),
    ]
    with pytest.raises(AssertionError, match=r"queue exhausted"):
        adapter.submit(request)


def test_submit_and_fake_accept_mapping_payload(monkeypatch):
    original = {"body": "review"}
    payload = MappingProxyType(original)
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
    original["body"] = "changed"
    assert fake.calls[-1].request.payload == {"body": "review"}


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
        pytest.param(
            '# gh api fixture\n"""Example: ["gh", "api"]"""', False, id="documentation"
        ),
    ],
)
def test_ownership_guard_distinguishes_executable_text(source, invalid):
    assert bool(_forge_ownership_violations(source, consumer=True)) == invalid


@pytest.mark.parametrize("platform", ["github", "gitlab"])
@pytest.mark.parametrize("pin", [None, "", "pinned[bot]"])
def test_poster_pin_and_user_lookup(
    platform: forge.Platform, pin: str | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    if pin is None:
        monkeypatch.delenv("CODE_GAUNTLET_POSTER", raising=False)
    else:
        monkeypatch.setenv("CODE_GAUNTLET_POSTER", pin)
    calls: list[list[str]] = []

    def output(command: list[str], **_kwargs: object) -> tuple[str, str, int]:
        calls.append(command)
        return '{"login":"github-user","username":"gitlab-user"}', "", 0

    monkeypatch.setattr(proc, "output", output)
    adapter = forge.make_forge(platform)
    expected = pin or f"{platform}-user"
    assert adapter.poster() == forge.Poster(expected, None)
    assert len(calls) == (0 if pin else 1)
    if platform == "gitlab" and not pin:
        assert calls == [["glab", "api", "user"]]


@pytest.mark.parametrize("platform", ["github", "gitlab"])
@pytest.mark.parametrize("outcome", ["denied", "invalid", "missing", "timeout"])
def test_poster_failure_names_pin_and_trusts_nothing(
    platform: forge.Platform, outcome: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CODE_GAUNTLET_POSTER", raising=False)

    def output(command: list[str], **_kwargs: object) -> tuple[str, str, int]:
        if outcome == "timeout":
            raise proc.TimeoutExpired(command, 30)
        if outcome == "denied":
            return "", "403 denied", 1
        return (
            (
                '{"login":"poster","username":"poster"} broken'
                if outcome == "invalid"
                else "{}"
            ),
            "",
            0,
        )

    monkeypatch.setattr(proc, "output", output)
    poster = forge.make_forge(platform).poster()
    assert poster.login is None and poster.error is not None
    assert "CODE_GAUNTLET_POSTER" in poster.error
    assert f"{platform} /user" in poster.error
    if outcome == "denied":
        assert "403 denied" in poster.error


@pytest.mark.parametrize("platform", ["github", "gitlab"])
def test_pr_view_and_gitlab_listing_timeouts(platform, monkeypatch):
    calls = []

    def output(command, **kwargs):
        calls.append((command, kwargs))
        return "", "", 0

    monkeypatch.setattr(proc, "output", output)
    target = forge.ReviewTarget("upstream", "project", 7)
    if platform == "github":
        adapter = forge.GitHub()
        adapter.pr_view(7)
        assert calls == [
            (
                ["gh", "pr", "view", "7", "--json", "state,isDraft,url"],
                {"timeout": 30},
            ),
        ]
    else:
        adapter = forge.GitLab()
        adapter.pr_view(7)
        adapter.file_names(target)
        assert calls == [
            (["glab", "mr", "view", "7", "--output", "json"], {"timeout": 30}),
            (
                [
                    "glab",
                    "api",
                    "projects/upstream%2Fproject/merge_requests/7/diffs?per_page=100",
                    "--paginate",
                    "--output",
                    "ndjson",
                ],
                {"timeout": 180},
            ),
        ]


@pytest.mark.parametrize(
    "call, host",
    [
        ("poster", None),
        ("poster", "git.example.com"),
        ("review_entries", "git.example.com"),
        ("file_names", "git.example.com"),
        ("submit", "git.example.com"),
        ("diff", None),
        ("diff", "git.example.com"),
    ],
)
def test_github_host_argv(call, host, monkeypatch, tracked_temp):
    monkeypatch.delenv("CODE_GAUNTLET_POSTER", raising=False)
    prefix = ["gh", "api", *(["--hostname", host] if host else [])]
    endpoint = "repos/group/sub/repo/pulls/9"
    expected = {
        "poster": [*prefix, "user"],
        "review_entries": [*prefix, "--paginate", endpoint + "/reviews"],
        "file_names": [
            *prefix,
            endpoint + "/files?per_page=100",
            "--paginate",
            "--jq",
            ".[].filename | @json",
        ],
        "submit": [
            *prefix,
            "--method",
            "POST",
            "-H",
            "Accept: application/vnd.github+json",
            endpoint + "/reviews",
        ],
        "diff": [
            "gh",
            "pr",
            "diff",
            "9",
            "--repo",
            (host + "/" if host else "") + "group/sub/repo",
        ],
    }[call]
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        if call == "submit":
            assert command == [*expected, "--input", str(tracked_temp[0])]
            assert (
                tracked_temp[0].read_bytes()
                == b'{"body": "caf\xc3\xa9\\nline", "event": "COMMENT", "comments": []}'
            )
        else:
            assert command == expected
        stdout = {
            "poster": '{"login":"poster"}',
            "review_entries": '[{"id":1}]\n[{"id":2}]',
            "submit": '{"id":7}',
        }.get(call, "diff\r\n")
        return proc.CompletedProcess(command, 0, stdout, "")

    monkeypatch.setattr(proc, "run", run)
    adapter = forge.make_forge("github", host)
    if call == "submit":
        result = adapter.submit(
            forge.github_review_request(
                TARGET, {"body": "caf\u00e9\nline", "event": "COMMENT", "comments": []}
            )
        )
        assert result == forge.PostResult({"id": 7}, None, None)
        assert not tracked_temp[0].exists()
    elif call == "poster":
        assert adapter.poster() == forge.Poster("poster", None)
    elif call == "review_entries":
        assert adapter.review_entries(TARGET) == forge.JsonFetch(
            [{"id": 1}, {"id": 2}], None
        )
    else:
        assert getattr(adapter, call)(TARGET) == ("diff\r\n", "", 0)
    assert len(calls) == 1
    timeout = (
        180
        if call == "file_names"
        else 30
        if call in {"poster", "review_entries"}
        else None
    )
    assert calls[0][1] == {
        "cwd": None,
        "timeout": timeout,
        "errors": "replace" if call in {"poster", "review_entries"} else "strict",
    }

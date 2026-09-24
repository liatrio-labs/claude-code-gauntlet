from __future__ import annotations

import hashlib
import io
import json
import subprocess
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import parse_qs, unquote

import pytest  # type: ignore[import-not-found]

from tests.tools.render_probes import (
    ELEMENT_VOCABULARIES,
    INHERENTLY_PLAIN,
    RENDERER_ATTRIBUTES_BY_PLATFORM,
    SEED_GROUPS,
    GitLabClient,
    GitLabHTTPError,
    check_render,
    derive_handles,
    input_sha256,
    pair_sha256,
    record_fixture,
    render_github,
    run_canary,
    seed,
    serialize_fixture,
    structure,
    update_cases,
    write_utf8,
)

REAL_HEADING_HTML = (
    '<h2 id="user-content-x-heading" data-sourcepos="1:1-3:3" dir="auto">'
    '`x\nHeading<a href="#x-heading" aria-label="Link to heading \'`x Heading\'" '
    'data-heading-content="`x Heading" class="anchor"></a></h2>'
    '<p data-sourcepos="4:1-4:21" dir="auto">&lt;b&gt; \uff20leehopper`</p>'
    '<p data-sourcepos="6:1-6:11" dir="auto">footer line</p>'
)
REAL_CODE_HTML = (
    '<div class="gl-relative markdown-code-block js-markdown-code">'
    '<pre data-sourcepos="1:1-3:3" data-canonical-lang="txt" '
    'class="code highlight js-syntax-highlight language-plaintext" v-pre="true">'
    '<code><span id="LC1" class="line" data-lang="plaintext">'
    "@user &lt;table&gt; @</span></code></pre><copy-code></copy-code>"
    "<insert-code-snippet></insert-code-snippet></div>\n"
    '<p data-sourcepos="5:1-5:11" dir="auto">footer line</p>'
)
REAL_ANGLE_ATTRIBUTE_HTML = (
    '<h1 id="user-content-heading-b-leehopper" data-sourcepos="2:1-2:31" dir="auto">'
    'heading &lt;b&gt; \uff20leehopper`<a href="#heading-b-leehopper" '
    "aria-label=\"Link to heading 'heading <b> \uff20leehopper`'\" "
    'data-heading-content="heading <b> \uff20leehopper`" class="anchor"></a></h1>'
)
REAL_REFERENCE_HTML = (
    '<p data-sourcepos="1:1-1:22" dir="auto">Please ask '
    '<a href="http://localhost:8929/leehopper" title="leehopper" '
    'class="gfm gfm-project_member js-user-link" data-user="2" '
    'data-original="@leehopper" data-container="body" data-placement="top" '
    'data-reference-type="user">@leehopper</a>.</p>'
)


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("<copy-code><span>noise</span></copy-code><p>x</p>", "<p>x</p>"),
        ("<insert-code-snippet>noise</insert-code-snippet><p>x</p>", "<p>x</p>"),
        ('<a href="#head" class="anchor"></a><h1>head</h1>', "<h1>head</h1>"),
        ('<a href="#head" class="anchor">generated</a>', ""),
        (
            '<div class="gl-relative markdown-code-block js-markdown-code"><p>x</p></div>',
            "<p>x</p>",
        ),
        ("<div><pre>x</pre></div>", "<pre>x</pre>"),
        (
            '<div class="highlight highlight-text-adblock"><pre>x</pre></div>',
            "<pre>x</pre>",
        ),
        ('<span id="LC1" class="line" data-lang="plaintext">x</span>', "x"),
        ('<span data-escaped-char="" data-sourcepos="1:1-1:2">x</span>', "x"),
        (
            "<markdown-accessiblity-table><table><tr><td>x</td></tr></table></markdown-accessiblity-table>",
            "<table><tr><td>x</td></tr></table>",
        ),
        ('<p data-sourcepos="1:1-1:2" dir="auto">x</p>', "<p>x</p>"),
        (
            '<a href="/x" class="gfm" rel="nofollow noreferrer noopener" target="_blank">x</a>',
            '<a href="/x">x</a>',
        ),
        ('<code class="notranslate">x</code>', "<code>x</code>"),
        (
            '<pre class="code highlight js-syntax-highlight language-plaintext">x</pre>',
            "<pre>x</pre>",
        ),
        ("a<br>b", "a b"),
        ("<pre><code>x  y</code></pre>", "<pre>x y</pre>"),
        ("<pre><code>x\n</code></pre>", "<pre>x</pre>"),
        ("<p>a  b</p>", "<p>a b</p>"),
        ("<p>a  &lt;b&gt;</p>", "<p>a &lt;b&gt;</p>"),
    ],
)
def test_structure_rule_must_equal_pairs(left: str, right: str) -> None:
    assert structure(left) == structure(right)


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ('<a href="/same">x</a>', "x"),
        ('<a href="/x">x</a>', '<a href="/y">x</a>'),
        ('<img src="/x">', '<img src="/y">'),
        ('<img data-src="/x">', '<img data-src="/y">'),
        ('<ol start="1"><li>x</li></ol>', '<ol start="2"><li>x</li></ol>'),
        ("<code>x</code>", "x"),
        ("<pre>x</pre>", "<p>x</p>"),
        (
            '<a href="https://elsewhere.test/project/-/blob/main/a.md">x</a>',
            '<a href="a.md">x</a>',
        ),
        ("&lt;b&gt;", "<b></b>"),
        ("<!-- note -->", "&lt;!-- note --&gt;"),
        ('<abbr title="HTML">HTML</abbr>', "HTML"),
        ('<a href="#head" class="other anchor">content</a>', "content"),
        ('<a href="#head" class="anchor" onclick="run()">content</a>', "content"),
        ('<span data-escaped-char="x" data-sourcepos="1:1">x</span>', "x"),
        (
            '<span id="LC1" class="line" data-lang="plaintext" data-extra="x">x</span>',
            "x",
        ),
        ('<div class="highlight"><pre>x</pre></div>', "<pre>x</pre>"),
        ('<pre><code class="custom">x</code></pre>', "<pre>x</pre>"),
        ('<p class="custom">x</p>', "<p>x</p>"),
        ('<pre class="custom">x</pre>', "<pre>x</pre>"),
        ('<a href="#">x</a>', "x"),
        ('<p title="a  b">x</p>', '<p title="a b">x</p>'),
        ('<p onclick="run()">x</p>', "<p>x</p>"),
    ],
)
def test_structure_rule_must_differ_pairs(left: str, right: str) -> None:
    assert structure(left) != structure(right)


def test_blob_prefix_only_normalizes_matching_project_links() -> None:
    prefix = "http://localhost:8929/cdr-group/probe/-/blob/main/"
    assert structure(f'<a href="{prefix}">x</a>', blob_prefix=prefix) == structure(
        '<a href="">x</a>'
    )
    assert structure(
        f'<a href="{prefix}file.md">x</a>', blob_prefix=prefix
    ) == structure('<a href="file.md">x</a>')
    assert structure(
        '<a href="https://other.test/file.md">x</a>', blob_prefix=prefix
    ) != structure('<a href="file.md">x</a>')


def test_pair_hash_binds_gitlab_links_after_blob_prefix_normalization() -> None:
    prefix = "http://gitlab.test/cdr-group/probe/-/blob/main/"
    github_html = '<a href="docs/guide.md">guide</a>'
    gitlab_html = f'<a href="{prefix}docs/guide.md">guide</a>'
    normalized_pair = (
        structure(github_html) + "\0" + structure(gitlab_html, blob_prefix=prefix)
    )
    assert (
        pair_sha256(github_html, gitlab_html, blob_prefix=prefix)
        == hashlib.sha256(normalized_pair.encode("utf-8")).hexdigest()
    )
    assert pair_sha256(github_html, gitlab_html, blob_prefix=prefix) != pair_sha256(
        github_html, gitlab_html
    )


def test_element_vocabularies_are_closed_and_platform_specific() -> None:
    assert ELEMENT_VOCABULARIES["gitlab"] == frozenset(
        {
            "a",
            "blockquote",
            "code",
            "copy-code",
            "div",
            "h1",
            "h2",
            "insert-code-snippet",
            "li",
            "ol",
            "p",
            "pre",
            "span",
            "strong",
            "table",
            "tbody",
            "td",
            "th",
            "thead",
            "tr",
            "ul",
        }
    )
    assert ELEMENT_VOCABULARIES["github"] == frozenset(
        {
            "a",
            "blockquote",
            "br",
            "code",
            "div",
            "h1",
            "h2",
            "li",
            "markdown-accessiblity-table",
            "ol",
            "p",
            "pre",
            "strong",
            "table",
            "tbody",
            "td",
            "th",
            "thead",
            "tr",
            "ul",
        }
    )


def test_renderer_attribute_tables_are_closed_per_platform() -> None:
    assert {
        "gitlab": frozenset(
            {
                "data-sourcepos",
                "dir",
                "data-canonical-src",
                "data-canonical-lang",
                "data-lang-params",
                "v-pre",
                "data-lang",
                "data-escaped-char",
                "data-heading-content",
                "rel",
                "target",
                "class",
            }
        ),
        "github": frozenset({"role", "data-meta", "lang", "rel", "class"}),
    } == RENDERER_ATTRIBUTES_BY_PLATFORM


@pytest.mark.parametrize("platform", ["gitlab", "github"])
def test_hard_check_accepts_safe_synthetic_markdown(platform: str) -> None:
    check_render(platform, "<p><strong>safe</strong> <code>text</code></p>")


def test_hard_check_rejects_gitlab_references_comments_and_unknown_elements() -> None:
    with pytest.raises(ValueError, match="GitLab reference link"):
        check_render("gitlab", '<a data-reference-type="user">@user</a>')
    with pytest.raises(ValueError, match="comment node"):
        check_render("gitlab", "<!-- retained -->")
    with pytest.raises(ValueError, match="unknown element <abbr>"):
        check_render("gitlab", "<abbr>HTML</abbr>")


def test_hard_check_rejects_github_mentions_comments_and_unknown_elements() -> None:
    for kind in ("user-mention", "team-mention"):
        with pytest.raises(ValueError, match="GitHub mention anchor"):
            check_render("github", f'<a class="{kind}">@team</a>')
    with pytest.raises(ValueError, match="comment node"):
        check_render("github", "<!-- retained -->")
    with pytest.raises(ValueError, match="unknown element <abbr>"):
        check_render("github", "<abbr>HTML</abbr>")


def test_hard_check_accepts_inlined_gitlab_heading_code_and_angle_attribute_renders() -> (
    None
):
    check_render("gitlab", REAL_HEADING_HTML)
    check_render("gitlab", REAL_CODE_HTML)
    check_render("gitlab", REAL_ANGLE_ATTRIBUTE_HTML)
    assert "<b>" in REAL_ANGLE_ATTRIBUTE_HTML
    assert (
        structure(REAL_CODE_HTML)
        == "<pre>@user &lt;table&gt; @</pre> <p>footer line</p>"
    )


def test_hard_check_rejects_inlined_gitlab_input_reference_anchor() -> None:
    with pytest.raises(ValueError, match="GitLab reference link"):
        check_render("gitlab", REAL_REFERENCE_HTML)


def test_derive_handles_reads_ascii_fullwidth_and_paths_and_respects_lookbehind() -> (
    None
):
    cases = [
        {
            "input": "(@alice), \uff20team, @types/node; x@hidden _\uff20hidden @bare. \uff20modal/",
            "expected": "\uff20Override @x @all",
        },
        {"input": "@ignore.git @ignore.atom", "expected": "footer"},
    ]
    assert derive_handles(cases) == {
        "alice",
        "team",
        "types/node",
        "Override",
        "x",
        "all",
        "bare",
        "modal",
    }
    assert SEED_GROUPS == ("team",)
    assert set(INHERENTLY_PLAIN) == {"all", "x"}


def test_derive_handles_accepts_256_character_segment_and_20_segment_path() -> None:
    segment = "a" * 256
    path = "/".join(["a"] * 20)
    cases = [
        {
            "input": f"@{segment} @{'a' * 257} @{path} @{'/'.join(['a'] * 21)}",
            "expected": "",
        }
    ]
    assert derive_handles(cases) == {segment, path}


def test_canary_names_unresolved_seedable_handles_and_skips_plain_handles() -> None:
    def fake_render(text: str) -> str:
        if text in {"@alice", "@all", "@x"}:
            return '<a data-reference-type="user">' + text + "</a>"
        return text

    run_canary(["alice", "all", "x"], fake_render)
    with pytest.raises(RuntimeError, match="unresolved handles: missing"):
        run_canary(["alice", "missing", "all", "x"], fake_render)


def test_gitlab_adapter_extracts_html_from_json_response() -> None:
    token = "private-render-token-value"
    seen = {}

    def fake_opener(request):
        seen["method"] = request.get_method()
        seen["url"] = request.full_url
        seen["headers"] = request.header_items()
        seen["body"] = json.loads(request.data.decode("utf-8"))
        return io.BytesIO(b'{"html":"<p>rendered</p>"}')

    html = GitLabClient("http://gitlab.test/", token, opener=fake_opener).render(
        "expected\n\nfooter line", "cdr-group/probe"
    )
    assert seen["method"] == "POST"
    assert seen["url"] == "http://gitlab.test/api/v4/markdown"
    assert any(
        name.upper() == "PRIVATE-TOKEN"
        for name, value in seen["headers"]
        if value == token
    )
    assert seen["body"] == {
        "text": "expected\n\nfooter line",
        "gfm": True,
        "project": "cdr-group/probe",
    }
    assert html == "<p>rendered</p>"


def test_divergence_rejects_href_only_difference() -> None:
    prefix = "http://gitlab.test/cdr-group/probe/-/blob/main/"
    github_html = '<a href="one.md">guide</a>'
    gitlab_html = '<a href="two.md">guide</a>'
    case = {
        "id": "href_difference",
        "input": "@alice",
        "expected": "@alice",
        "github_probe": {"html": github_html},
        "gitlab_probe": {
            "html": gitlab_html,
            "blob_prefix": prefix,
            "divergence": {
                "note": "href difference",
                "issue": 1,
                "pair_sha256": pair_sha256(
                    github_html, gitlab_html, blob_prefix=prefix
                ),
            },
        },
    }
    result = update_cases(
        [case],
        platform="gitlab",
        render=lambda text: gitlab_html,
        rendered="2026-01-01",
        version="19.4.1 ce",
        blob_prefix=prefix,
    )
    assert result.cases[0]["gitlab_probe"]["divergence"] is None


def test_gitlab_http_error_includes_truncated_json_message_without_token() -> None:
    token = "private-render-token-value"
    message = "namespace path already taken " + token + " " + ("x" * 240)
    safe_message = message.replace(token, "[redacted]")[:200]
    requests = []

    class ErrorResponse(io.BytesIO):
        status = 400

    def fake_opener(request):
        requests.append(request)
        return ErrorResponse(json.dumps({"message": message}).encode("utf-8"))

    client = GitLabClient("http://gitlab.test", token, opener=fake_opener)
    with pytest.raises(GitLabHTTPError) as error:
        client.json_request("POST", "/api/v4/groups", {"path": "taken"})
    rendered_error = str(error.value)
    assert "namespace path already taken" in rendered_error
    assert rendered_error.endswith(": " + safe_message)
    assert token not in rendered_error
    assert "PRIVATE-TOKEN" not in rendered_error
    assert len(requests) == 1


def test_gitlab_http_errors_do_not_include_the_token() -> None:
    token = "private-render-token-value"

    def fail_opener(request):
        raise GitLabHTTPError(500, "/api/v4/markdown")

    client = GitLabClient("http://gitlab.test", token, opener=fail_opener)
    with pytest.raises(GitLabHTTPError) as error:
        client.render("text", "cdr-group/probe")
    assert token not in str(error.value)


def test_gitlab_adapter_retries_429_with_bounded_backoff() -> None:
    calls = []
    delays: list[float] = []

    def throttled_opener(request):
        calls.append(request)
        if len(calls) <= 3:
            raise HTTPError(request.full_url, 429, "throttled", None, io.BytesIO())
        return io.BytesIO(b"ok")

    result = GitLabClient(
        "http://gitlab.test", "secret", opener=throttled_opener, sleeper=delays.append
    ).request("GET", "/api/v4/version")
    assert result == b"ok"
    assert len(calls) == 4
    assert delays == [0.25, 0.5, 1.0]


def test_github_adapter_passes_expected_argv_to_injected_runner() -> None:
    seen_argv: list[str] = []
    seen_kwargs: dict[str, object] = {}

    def fake_runner(
        argv: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        seen_argv.extend(argv)
        seen_kwargs.update(kwargs)
        return subprocess.CompletedProcess(argv, 0, stdout="<p>rendered</p>", stderr="")

    assert render_github("expected text", runner=fake_runner) == "<p>rendered</p>"
    assert seen_argv == [
        "gh",
        "api",
        "markdown",
        "-f",
        "mode=gfm",
        "-f",
        "text=expected text",
    ]
    assert seen_kwargs["encoding"] == "utf-8"


def test_seed_creates_group_chains_users_project_and_idempotent_memberships() -> None:
    class FakeGitLab:
        def __init__(self):
            self.groups = {}
            self.users = {}
            self.project = None
            self.memberships = set()
            self.creates = []
            self.next_id = 1
            self.raced_group = False

        def _new(self, mapping, key, value):
            item = {**value, "id": self.next_id}
            self.next_id += 1
            mapping[key] = item
            return item

        def json_request(self, method, path, body=None):
            if method == "GET" and "/members/" in path:
                resource, resource_id, _, user_id = path.split("/")[-4:]
                key = (resource, int(resource_id), int(user_id))
                return {"id": key[2]} if key in self.memberships else None
            if method == "GET" and path.startswith("/api/v4/groups/"):
                full_path = unquote(path.rsplit("/", 1)[-1])
                return self.groups.get(full_path)
            if method == "GET" and path.startswith("/api/v4/users?"):
                username = parse_qs(path.split("?", 1)[1])["username"][0]
                return [self.users[username]] if username in self.users else []
            if method == "GET" and path.startswith("/api/v4/projects/"):
                return self.project
            if method == "POST" and path == "/api/v4/groups":
                self.creates.append((method, path, body))
                parent_id = body.get("parent_id") if body else None
                parent_path = next(
                    (p for p, v in self.groups.items() if v["id"] == parent_id), ""
                )
                group_path = "/".join(
                    part for part in (parent_path, body["path"]) if part
                )
                created = self._new(self.groups, group_path, body)
                if group_path == "cdr-group" and not self.raced_group:
                    self.raced_group = True
                    raise GitLabHTTPError(409, path)
                return created
            if method == "POST" and path == "/api/v4/users":
                self.creates.append((method, path, body))
                return self._new(self.users, body["username"], body)
            if method == "POST" and path == "/api/v4/projects":
                self.creates.append((method, path, body))
                self.project = {**body, "id": self.next_id}
                self.next_id += 1
                return self.project
            if method == "POST" and path.endswith("/members"):
                self.creates.append((method, path, body))
                parts = path.split("/")
                resource, resource_id = parts[-3], parts[-2]
                self.memberships.add((resource, int(resource_id), int(body["user_id"])))
                return {"id": body["user_id"]}
            raise AssertionError((method, path, body))

    api = FakeGitLab()
    cases = [{"input": "@alice @types/node @team @all @x", "expected": "\uff20bob"}]
    first = seed(cases, api)
    create_count = len(api.creates)
    second = seed(cases, api)
    assert set(first["groups"]) == {"cdr-group", "team", "types", "types/node"}
    assert set(first["users"]) == {"alice", "bob"}
    assert second["project"] == first["project"]
    assert len(api.creates) == create_count
    assert any(
        body.get("initialize_with_readme") is True for _, _, body in api.creates if body
    )
    assert all(
        ("groups", first["groups"][group]["id"], user["id"]) in api.memberships
        for group in ("cdr-group", "team")
        for user in first["users"].values()
    )
    assert all(
        ("projects", first["project"]["id"], user["id"]) in api.memberships
        for user in first["users"].values()
    )


def _write_initial_probe_fixture(path: Path) -> None:
    github_html = "<p>\uff20alice</p>"
    case = {
        "id": "probe_row",
        "input": "@alice",
        "expected": "\uff20alice",
        "github_probe": {
            "renderer": "gh api markdown mode=gfm",
            "rendered": "2026-01-01",
            "input": "expected + blank line + footer line",
            "input_sha256": "old",
            "html": github_html,
        },
    }
    with path.open("w", encoding="utf-8", newline="") as stream:
        stream.write(serialize_fixture([case]))


def _read_fixture(path: Path) -> str:
    with path.open("r", encoding="utf-8", newline="") as stream:
        return str(stream.read())


def _fake_gitlab_render(text: str) -> str:
    if "@alice" in text:
        return '<p><a href="/alice" data-original="@alice" data-reference-type="user">@alice</a></p>'
    return "<p>\uff20alice</p>"


def test_record_fixture_writes_ordered_hashes_twin_refs_and_byte_stable_layout(
    tmp_path,
) -> None:
    path = tmp_path / "fixture.json"
    _write_initial_probe_fixture(path)
    result = record_fixture(
        str(path),
        platform="gitlab",
        render=_fake_gitlab_render,
        clock=lambda: "2026-09-24",
        version="19.4.1 ce",
        blob_prefix="http://gitlab.test/cdr-group/probe/-/blob/main/",
    )
    assert result.exit_code == 0
    text = _read_fixture(path)
    document = json.loads(text)
    case = document["cases"][0]
    probe = case["gitlab_probe"]
    assert list(probe) == [
        "renderer",
        "version",
        "rendered",
        "input",
        "input_sha256",
        "blob_prefix",
        "twin_references",
        "html",
        "divergence",
    ]
    assert list(case).index("gitlab_probe") == list(case).index("github_probe") + 1
    assert probe["input_sha256"] == input_sha256("\uff20alice\n\nfooter line")
    assert probe["twin_references"] == ["@alice"]
    assert serialize_fixture(document["cases"]) == text


def test_record_keeps_only_pair_hash_bound_divergence(tmp_path) -> None:
    path = tmp_path / "fixture.json"
    _write_initial_probe_fixture(path)
    document = json.loads(_read_fixture(path))
    case = document["cases"][0]
    github_html = "<p>github</p>"
    case["github_probe"]["html"] = github_html
    gl_html = "<p>\uff20alice</p>"
    case["gitlab_probe"] = {
        "renderer": "gitlab api markdown gfm=true project",
        "version": "19.4.1 ce",
        "rendered": "2026-01-01",
        "input": "expected + blank line + footer line",
        "input_sha256": "old",
        "blob_prefix": "http://gitlab.test/cdr-group/probe/-/blob/main/",
        "twin_references": [],
        "html": gl_html,
        "divergence": {
            "note": "text-only difference",
            "issue": 1,
            "pair_sha256": pair_sha256(github_html, gl_html),
        },
    }
    write_utf8(str(path), serialize_fixture(document["cases"]))
    kept = record_fixture(
        str(path),
        platform="gitlab",
        render=_fake_gitlab_render,
        clock=lambda: "2026-09-24",
        version="19.4.1 ce",
        blob_prefix="http://gitlab.test/cdr-group/probe/-/blob/main/",
    )
    assert kept.cases[0]["gitlab_probe"]["divergence"]["issue"] == 1

    document = json.loads(_read_fixture(path))
    document["cases"][0]["gitlab_probe"]["divergence"]["pair_sha256"] = "0" * 64
    write_utf8(str(path), serialize_fixture(document["cases"]))
    cleared = record_fixture(
        str(path),
        platform="gitlab",
        render=_fake_gitlab_render,
        clock=lambda: "2026-09-24",
        version="19.4.1 ce",
        blob_prefix="http://gitlab.test/cdr-group/probe/-/blob/main/",
    )
    assert cleared.divergence_cleared == ["probe_row"]
    assert cleared.cases[0]["gitlab_probe"]["divergence"] is None
    assert cleared.exit_code == 0


def test_check_writes_nothing_and_reports_changed_id_and_version(tmp_path) -> None:
    path = tmp_path / "fixture.json"
    _write_initial_probe_fixture(path)
    first = record_fixture(
        str(path),
        platform="gitlab",
        render=_fake_gitlab_render,
        clock=lambda: "2026-09-24",
        version="19.4.1 ce",
        blob_prefix="http://gitlab.test/cdr-group/probe/-/blob/main/",
    )
    assert first.exit_code == 0
    original = path.read_bytes()
    writes = []

    def changed_render(text: str) -> str:
        result = _fake_gitlab_render(text)
        return (
            result.replace("\uff20alice", "\uff20changed")
            if "@alice" not in text
            else result
        )

    checked = record_fixture(
        str(path),
        platform="gitlab",
        render=changed_render,
        clock=lambda: "2026-09-24",
        version="19.4.2 ce",
        blob_prefix="http://gitlab.test/cdr-group/probe/-/blob/main/",
        check=True,
        write_text=lambda target, contents: writes.append(contents),
    )
    assert checked.changed_ids == ["probe_row"]
    assert checked.version_mismatch == ("19.4.1 ce", "19.4.2 ce")
    assert checked.exit_code == 1
    assert writes == []
    assert path.read_bytes() == original

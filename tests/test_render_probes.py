from __future__ import annotations

import argparse
import base64
import io
import json
import os
import re
import shlex
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import NamedTuple
from urllib.error import HTTPError
from urllib.parse import parse_qs, unquote

import pytest

from tests.tools.probe_cli import (
    build_argument_parser,
    main,
)
from tests.tools.probe_fixtures import (
    record_divergence,
    record_fixture,
    serialize_fixture,
    write_utf8,
)
from tests.tools.probe_github import (
    github_login,
    render_github,
    run_github_canary,
)
from tests.tools.probe_gitlab import (
    INHERENTLY_PLAIN,
    SEED_GROUPS,
    GitLabClient,
    GitLabHTTPError,
    _gitlab_blob_prefix,
    _gitlab_version,
    derive_handles,
    run_canary,
    seed,
)
from tests.tools.probe_html import (
    check_render,
    pair_sha256,
    structure,
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


HTML_NORMALIZATION_CASES = {
    "copy-control": (
        "gitlab",
        "<copy-code><span>noise</span></copy-code><p>x</p>",
        "<p>x</p>",
    ),
    "insert-control": (
        "gitlab",
        "<insert-code-snippet>noise</insert-code-snippet><p>x</p>",
        "<p>x</p>",
    ),
    "heading-anchor": (
        "gitlab",
        '<h1>head<a href="#head" class="anchor"></a></h1>',
        "<h1>head</h1>",
    ),
    "gitlab-code-wrapper": (
        "gitlab",
        '<div class="gl-relative markdown-code-block js-markdown-code"><p>x</p></div>',
        "<p>x</p>",
    ),
    "github-code-wrapper": (
        "github",
        '<div class="highlight highlight-text-adblock"><pre>x</pre></div>',
        "<pre>x</pre>",
    ),
    "line-span": (
        "gitlab",
        '<span id="LC1" class="line" data-lang="plaintext">x</span>',
        "x",
    ),
    "escaped-span": (
        "gitlab",
        '<span data-escaped-char="" data-sourcepos="1:1-1:2">x</span>',
        "x",
    ),
    "accessible-table": (
        "github",
        "<markdown-accessiblity-table><table><tr><td>x</td></tr></table></markdown-accessiblity-table>",
        "<table><tr><td>x</td></tr></table>",
    ),
    "source-position-auto-dir": (
        "gitlab",
        '<p data-sourcepos="1:1-1:2" dir="auto">x</p>',
        "<p>x</p>",
    ),
    "gitlab-link-chrome": (
        "gitlab",
        '<a href="/x" class="gfm" rel="nofollow noreferrer noopener" target="_blank">x</a>',
        '<a href="/x">x</a>',
    ),
    "github-code-class": (
        "github",
        '<code class="notranslate">x</code>',
        "<code>x</code>",
    ),
    "gitlab-pre-class": (
        "gitlab",
        '<pre class="code highlight js-syntax-highlight language-plaintext">x</pre>',
        "<pre>x</pre>",
    ),
    "line-break": ("gitlab", "a<br>b", "a b"),
    "pre-code": ("gitlab", "<pre><code>x  y</code></pre>", "<pre>x y</pre>"),
    "pre-trailing-lf": ("gitlab", "<pre><code>x\n</code></pre>", "<pre>x</pre>"),
    "entity-text": ("gitlab", "<p>a  &lt;b&gt;</p>", "<p>a &lt;b&gt;</p>"),
    "ascii-whitespace": ("gitlab", "<p>a\t\n b</p>", "<p>a b</p>"),
    "github-table-role": ("github", '<table role="table"></table>', "<table></table>"),
    "github-nofollow": (
        "github",
        '<a href="x" rel="nofollow">x</a>',
        '<a href="x">x</a>',
    ),
    "real-heading": (
        "gitlab",
        REAL_HEADING_HTML,
        "<h2>`x Heading</h2><p>&lt;b&gt; \uff20leehopper`</p><p>footer line</p>",
    ),
    "real-code": (
        "gitlab",
        REAL_CODE_HTML,
        "<pre>@user &lt;table&gt; @</pre> <p>footer line</p>",
    ),
    "angle-attribute": (
        "gitlab",
        REAL_ANGLE_ATTRIBUTE_HTML,
        "<h1>heading &lt;b&gt; \uff20leehopper`</h1>",
    ),
    "heading-1": (
        "gitlab",
        '<h1 id="user-content-title">Title<a class="other anchor" href="#title" aria-label="Link to heading"></a></h1>',
        "<h1>Title</h1>",
    ),
    "heading-2": (
        "gitlab",
        '<h2 id="user-content-title">Title<a class="other anchor" href="#title" aria-label="Link to heading"></a></h2>',
        "<h2>Title</h2>",
    ),
    "heading-3": (
        "gitlab",
        '<h3 id="user-content-title">Title<a class="other anchor" href="#title" aria-label="Link to heading"></a></h3>',
        "<h3>Title</h3>",
    ),
    "heading-4": (
        "gitlab",
        '<h4 id="user-content-title">Title<a class="other anchor" href="#title" aria-label="Link to heading"></a></h4>',
        "<h4>Title</h4>",
    ),
    "heading-5": (
        "gitlab",
        '<h5 id="user-content-title">Title<a class="other anchor" href="#title" aria-label="Link to heading"></a></h5>',
        "<h5>Title</h5>",
    ),
    "heading-6": (
        "gitlab",
        '<h6 id="user-content-title">Title<a class="other anchor" href="#title" aria-label="Link to heading"></a></h6>',
        "<h6>Title</h6>",
    ),
}

MEANINGFUL_HTML_SURVIVES_CASES = {
    "anchor-vs-text": ("gitlab", '<a href="/same">x</a>', "x"),
    "href": ("gitlab", '<a href="/x">x</a>', '<a href="/y">x</a>'),
    "image-src": ("gitlab", '<img src="/x">', '<img src="/y">'),
    "image-data-src": ("gitlab", '<img data-src="/x">', '<img data-src="/y">'),
    "ordered-start": (
        "gitlab",
        '<ol start="1"><li>x</li></ol>',
        '<ol start="2"><li>x</li></ol>',
    ),
    "inline-code": ("gitlab", "<code>x</code>", "x"),
    "pre-vs-paragraph": ("gitlab", "<pre>x</pre>", "<p>x</p>"),
    "foreign-blob": (
        "gitlab",
        '<a href="https://elsewhere.test/project/-/blob/main/a.md">x</a>',
        '<a href="a.md">x</a>',
    ),
    "escaped-tag": ("gitlab", "&lt;b&gt;", "<b></b>"),
    "escaped-comment": ("gitlab", "<!-- note -->", "&lt;!-- note --&gt;"),
    "inline-comment": ("gitlab", "<p>a<!-- c -->b</p>", "<p>ab</p>"),
    "void-rule": ("gitlab", "<hr>", ""),
    "literal-nbsp": ("gitlab", "<p>a\xa0b</p>", "<p>a b</p>"),
    "entity-nbsp": ("gitlab", "<p>a&nbsp;b</p>", "<p>a b</p>"),
    "pre-nbsp": ("gitlab", "<pre>x\xa0</pre>", "<pre>x</pre>"),
    "root-nbsp": ("gitlab", "\xa0x", "x"),
    "unknown-tag": ("gitlab", '<abbr title="HTML">HTML</abbr>', "HTML"),
    "root-extra-attribute": (
        "gitlab",
        '<a href="#head" class="anchor" onclick="run()">content</a>',
        "content",
    ),
    "heading-no-class": (
        "gitlab",
        '<h1>T<a href="#poison"></a></h1>',
        "<h1>T</h1>",
    ),
    "heading-wrong-class": (
        "gitlab",
        '<h1>T<a href="#poison" class="anchored"></a></h1>',
        "<h1>T</h1>",
    ),
    "heading-content": (
        "gitlab",
        '<h1>T<a href="#head" class="other anchor">c</a></h1>',
        "<h1>T</h1>",
    ),
    "heading-extra-attribute": (
        "gitlab",
        '<h1>T<a href="#head" class="anchor" onclick="run()"></a></h1>',
        "<h1>T</h1>",
    ),
    "heading-nonfinal": (
        "gitlab",
        '<h1>T<a class="anchor" href="#t"></a>X</h1>',
        "<h1>TX</h1>",
    ),
    "escaped-span-value": (
        "gitlab",
        '<span data-escaped-char="x" data-sourcepos="1:1">x</span>',
        "x",
    ),
    "line-span-extra": (
        "gitlab",
        '<span id="LC1" class="line" data-lang="plaintext" data-extra="x">x</span>',
        "x",
    ),
    "wrapper-signature": (
        "gitlab",
        '<div class="highlight"><pre>x</pre></div>',
        "<pre>x</pre>",
    ),
    "code-custom-class": (
        "gitlab",
        '<pre><code class="custom">x</code></pre>',
        "<pre>x</pre>",
    ),
    "paragraph-custom-class": ("gitlab", '<p class="custom">x</p>', "<p>x</p>"),
    "attribute-whitespace": ("gitlab", '<p title="a  b">x</p>', '<p title="a b">x</p>'),
    "event-attribute": ("gitlab", '<p onclick="run()">x</p>', "<p>x</p>"),
    "foreign-code-class": (
        "gitlab",
        '<code class="notranslate">x</code>',
        "<code>x</code>",
    ),
    "custom-anchor-class": (
        "gitlab",
        '<a href="x" class="other">x</a>',
        '<a href="x">x</a>',
    ),
    "foreign-gfm": ("github", '<a href="x" class="gfm">x</a>', '<a href="x">x</a>'),
    "wrong-rel-gitlab": (
        "gitlab",
        '<a href="x" rel="nofollow">x</a>',
        '<a href="x">x</a>',
    ),
    "wrong-rel-github": (
        "github",
        '<a href="x" rel="noopener">x</a>',
        '<a href="x">x</a>',
    ),
    "wrong-target": ("gitlab", '<a href="x" target="_self">x</a>', '<a href="x">x</a>'),
    "foreign-target": (
        "github",
        '<a href="x" target="_blank">x</a>',
        '<a href="x">x</a>',
    ),
    "foreign-v-pre": ("github", '<pre v-pre="true">x</pre>', "<pre>x</pre>"),
    "foreign-role": ("gitlab", '<table role="table"></table>', "<table></table>"),
    "foreign-pre-class": ("gitlab", '<pre class="notranslate">x</pre>', "<pre>x</pre>"),
    "foreign-highlight-class": (
        "github",
        '<pre class="code highlight js-syntax-highlight language-plaintext">x</pre>',
        "<pre>x</pre>",
    ),
    "paragraph-id": ("gitlab", '<p id="user-content-x">x</p>', "<p>x</p>"),
    "foreign-heading-id": ("github", '<h1 id="user-content-x">x</h1>', "<h1>x</h1>"),
    "wrong-v-pre": ("gitlab", '<pre v-pre="false">x</pre>', "<pre>x</pre>"),
    "github-rtl": ("github", '<p dir="rtl">y</p>', "<p>y</p>"),
    "gitlab-rtl": ("gitlab", '<p dir="rtl">y</p>', "<p>y</p>"),
    "wrong-table-role": ("github", '<table role="alert"></table>', "<table></table>"),
    "nonheading-parent": (
        "gitlab",
        '<p><a class="anchor" href="#title"></a></p>',
        "<p></p>",
    ),
    "foreign-heading-anchor": (
        "github",
        '<h1>T<a class="anchor" href="#t"></a></h1>',
        "<h1>T</h1>",
    ),
    "wrong-line-id": (
        "gitlab",
        '<span class="line" id="bad" data-lang="plaintext">x</span>',
        "x",
    ),
    "copy-extra-attribute": ("gitlab", '<copy-code title="x">x</copy-code>', ""),
    "table-wrapper-extra": (
        "github",
        '<markdown-accessiblity-table title="x"><p>x</p></markdown-accessiblity-table>',
        "<p>x</p>",
    ),
}

PROJECT_BLOB_NORMALIZATION_CASES = {
    "project-root": (
        "http://localhost:8929/cdr-group/probe/-/blob/main/",
        '<a href="">x</a>',
    ),
    "project-file": (
        "http://localhost:8929/cdr-group/probe/-/blob/main/file.md",
        '<a href="file.md">x</a>',
    ),
    "foreign-origin": (
        "https://elsewhere.test/cdr-group/probe/-/blob/main/file.md",
        '<a href="https://elsewhere.test/cdr-group/probe/-/blob/main/file.md">x</a>',
    ),
    "foreign-project": (
        "http://localhost:8929/other/proj/-/blob/main/file.md",
        '<a href="http://localhost:8929/other/proj/-/blob/main/file.md">x</a>',
    ),
}

RENDER_CONTAINMENT_CASES = {
    "gitlab-safe": ("gitlab", "<p><strong>safe</strong> <code>text</code></p>", None),
    "github-safe": ("github", "<p><strong>safe</strong> <code>text</code></p>", None),
    "real-code": ("gitlab", REAL_CODE_HTML, None),
    "angle-attribute": ("gitlab", REAL_ANGLE_ATTRIBUTE_HTML, None),
    "gitlab-reference": (
        "gitlab",
        '<a data-reference-type="user">@user</a>',
        "GitLab reference link",
    ),
    "gitlab-reference-nested": (
        "gitlab",
        '<p><a data-reference-type="user">@user</a></p>',
        "GitLab reference link",
    ),
    "github-user": (
        "github",
        '<a class="user-mention">@team</a>',
        "GitHub mention anchor",
    ),
    "github-team": (
        "github",
        '<a class="team-mention">@team</a>',
        "GitHub mention anchor",
    ),
    "gitlab-comment": ("gitlab", "<!-- retained -->", "comment node"),
    "github-comment": ("github", "<!-- retained -->", "comment node"),
    "gitlab-unknown": ("gitlab", "<abbr>HTML</abbr>", "unknown element <abbr>"),
    "github-unknown": ("github", "<abbr>HTML</abbr>", "unknown element <abbr>"),
    "bare-span": ("gitlab", "<span>x</span>", "unknown element <span>"),
    "foreign-gitlab-wrapper": (
        "github",
        '<div class="gl-relative markdown-code-block js-markdown-code"><pre>x</pre></div>',
        "unknown element <div>",
    ),
    "foreign-github-wrapper": (
        "gitlab",
        '<div class="highlight highlight-text-adblock"><pre>x</pre></div>',
        "unknown element <div>",
    ),
    "foreign-escaped-span": (
        "github",
        '<span data-escaped-char="" data-sourcepos="1:1">x</span>',
        "unknown element <span>",
    ),
}

GITLAB_BLOB_PREFIX_CASES = {
    "encoded-project-nonmain-branch": (
        {"web_url": "https://gitlab.test/team/probe/", "default_branch": "develop"},
        "https://gitlab.test/team/probe/-/blob/develop/",
        None,
    ),
    "missing-branch": (
        {"web_url": "https://gitlab.test/team/probe"},
        None,
        "GitLab project metadata is incomplete for team/probe",
    ),
}


def _gitlab_metadata_client(
    monkeypatch: pytest.MonkeyPatch, details: dict[str, str | bool]
) -> tuple[GitLabClient, list[tuple[str, str]]]:
    client = GitLabClient("http://gitlab.test", "fake-token")
    requests: list[tuple[str, str]] = []

    def request(method: str, path: str) -> dict[str, str | bool]:
        requests.append((method, path))
        return details

    monkeypatch.setattr(client, "json_request", request)
    return client, requests


HANDLE_DERIVATION_CASES = {
    "handle-boundary": (
        [{"input": "(@alice), x@hidden", "expected": ""}],
        False,
        {"alice"},
    ),
    "fullwidth-only-filters": (
        [
            {
                "input": "@ascii",
                "expected": "\uff20visible @ascii \uff20all \uff20ignore.git \uff20"
                + "b" * 257,
            }
        ],
        True,
        {"visible"},
    ),
}

GITLAB_CANARY_CASES = {
    "plain-handles-exempt": (
        ["alice", "all", "x"],
        '<a data-reference-type="user" data-original="@alice">@alice</a>',
        None,
    ),
    "unresolved-handle-named": (
        ["alice", "missing", "all", "x"],
        '<a data-reference-type="user" data-original="@alice">@alice</a>',
        "unresolved handles: missing",
    ),
}


def test_contributing_probe_commands_parse_with_the_recorder() -> None:
    text = (Path(__file__).resolve().parents[1] / "CONTRIBUTING.md").read_text(
        encoding="utf-8"
    )
    section = text.split("### Outbound render probes\n", 1)[1]
    block = re.search(r"```bash\n(.*?)```", section, re.DOTALL)
    assert block is not None
    commands = [
        shlex.split(line.split("#", 1)[0])
        for line in block.group(1).splitlines()
        if line.strip()
    ]
    assert commands
    parser = build_argument_parser()
    documented = {argv[2] for argv in commands}
    defined = {
        name
        for action in parser._actions
        if isinstance(action, argparse._SubParsersAction)
        for name in action.choices
    }
    assert documented == defined
    for argv in commands:
        assert argv[:2] == ["python3", "tests/tools/render_probes.py"]
        args = argv[2:]
        # The documented --issue value is a placeholder; argparse needs an int there.
        args = [
            "1" if prev == "--issue" else arg
            for prev, arg in zip(["", *args[:-1]], args, strict=True)
        ]
        parser.parse_args(args)


def test_quick_actions_command_accepts_record_and_check_forms() -> None:
    parser = build_argument_parser()

    direct = parser.parse_args(["quick-actions", "--check"])

    assert direct.container == "cdr-gitlab"
    assert direct.command == "quick-actions"
    assert direct.action == "record"
    assert direct.check is True


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="the fake docker is a shebang script, which CreateProcess cannot run",
)
def test_script_entrypoint_imports_composed_builder_from_outside_repo(
    tmp_path: Path,
) -> None:
    repo_root = Path(__file__).resolve().parents[1]
    script = repo_root / "tests" / "tools" / "render_probes.py"
    outside = tmp_path / "outside"
    outside.mkdir()
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    capture = tmp_path / "runner.rb"
    fake_docker = fake_bin / "docker"
    fake_docker.write_text(
        f"#!{sys.executable}\n"
        "from pathlib import Path\n"
        "import os\n"
        "import sys\n"
        "Path(os.environ['QUICK_ACTION_RUNNER_CAPTURE']).write_text(\n"
        "    sys.stdin.read(), encoding='utf-8', newline='\\n'\n"
        ")\n"
        "sys.stderr.write('fake docker received runner\\n')\n"
        "raise SystemExit(17)\n",
        encoding="utf-8",
        newline="\n",
    )
    fake_docker.chmod(0o755)

    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env["PATH"] = str(fake_bin) + os.pathsep + env.get("PATH", "")
    env["QUICK_ACTION_RUNNER_CAPTURE"] = str(capture)
    result = subprocess.run(
        [sys.executable, str(script), "quick-actions", "--container", "offline-test"],
        cwd=outside,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )

    assert result.returncode != 0
    assert "fake docker received runner" in result.stderr
    assert "ModuleNotFoundError" not in result.stderr
    runner_source = capture.read_text(encoding="utf-8")
    encoded_cases = re.search(
        r'^CASES_B64 = "([A-Za-z0-9+/=]+)"$', runner_source, re.MULTILINE
    )
    assert encoded_cases is not None
    cases = json.loads(base64.b64decode(encoded_cases.group(1)).decode("utf-8"))
    assert any(case["id"] == "composed:mbq_backtick" for case in cases)


@pytest.mark.parametrize(
    ("platform", "rendered", "expected"),
    HTML_NORMALIZATION_CASES.values(),
    ids=HTML_NORMALIZATION_CASES,
)
def test_html_normalization(platform, rendered, expected) -> None:
    assert structure(rendered, platform=platform) == expected


@pytest.mark.parametrize(
    ("platform", "rendered", "different"),
    MEANINGFUL_HTML_SURVIVES_CASES.values(),
    ids=MEANINGFUL_HTML_SURVIVES_CASES,
)
def test_meaningful_html_survives(platform, rendered, different) -> None:
    assert structure(rendered, platform=platform) != structure(
        different, platform=platform
    )


@pytest.mark.parametrize(
    ("href", "expected"),
    PROJECT_BLOB_NORMALIZATION_CASES.values(),
    ids=PROJECT_BLOB_NORMALIZATION_CASES,
)
def test_project_blob_normalization(href, expected) -> None:
    assert (
        structure(
            f'<a href="{href}">x</a>',
            platform="gitlab",
            blob_prefix="http://localhost:8929/cdr-group/probe/-/blob/main/",
        )
        == expected
    )


@pytest.mark.parametrize(
    ("platform", "rendered", "problem"),
    RENDER_CONTAINMENT_CASES.values(),
    ids=RENDER_CONTAINMENT_CASES,
)
def test_render_containment(platform, rendered, problem) -> None:
    if problem is None:
        check_render(platform, rendered)
    else:
        with pytest.raises(ValueError, match=problem):
            check_render(platform, rendered)


@pytest.mark.parametrize(
    ("details", "expected", "error"),
    GITLAB_BLOB_PREFIX_CASES.values(),
    ids=GITLAB_BLOB_PREFIX_CASES,
)
def test_gitlab_blob_prefix(monkeypatch, details, expected, error) -> None:
    client, requests = _gitlab_metadata_client(monkeypatch, details)
    if error:
        with pytest.raises(ValueError, match=error):
            _gitlab_blob_prefix(client, "team/probe")
    else:
        assert _gitlab_blob_prefix(client, "team/probe") == expected
    assert requests == [("GET", "/api/v4/projects/team%2Fprobe")]


def test_gitlab_version(monkeypatch: pytest.MonkeyPatch) -> None:
    client, requests = _gitlab_metadata_client(
        monkeypatch, {"version": "19.4.1", "enterprise": True}
    )
    assert _gitlab_version(client) == "19.4.1 ee"
    assert requests == [("GET", "/api/v4/version")]


@pytest.mark.parametrize(
    ("cases", "fullwidth_only", "expected"),
    HANDLE_DERIVATION_CASES.values(),
    ids=HANDLE_DERIVATION_CASES,
)
def test_handle_derivation(cases, fullwidth_only, expected) -> None:
    assert derive_handles(cases, fullwidth_only=fullwidth_only) == expected


@pytest.mark.parametrize(
    ("handles", "html", "error"), GITLAB_CANARY_CASES.values(), ids=GITLAB_CANARY_CASES
)
def test_gitlab_canary(handles, html, error) -> None:
    def render(text):
        return html if text == "@alice" else text

    if error:
        with pytest.raises(RuntimeError, match=error):
            run_canary(handles, render)
    else:
        run_canary(handles, render)


def test_github_canary() -> None:
    run_github_canary(
        "lee",
        lambda text: (
            '<a class="notranslate user-mention" href="https://github.com/LeE">user</a>'
        ),
    )


def test_github_authenticated_login() -> None:
    seen = []

    def runner(argv, **kwargs):
        seen.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="lee\n", stderr="")

    assert github_login(runner=runner) == "lee"
    assert seen == [["gh", "api", "user", "--jq", ".login"]]


def test_gitlab_adapter_extracts_html_from_json_response() -> None:
    token = "example-render-token-value"
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


def test_gitlab_http_error_includes_truncated_json_message_without_token() -> None:
    token = "example-render-token-value"
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
            self.races = set()

        def _new(self, mapping, key, value):
            item = {**value, "id": self.next_id}
            self.next_id += 1
            mapping[key] = item
            return item

        def _race(self, label, path, value):
            if label not in self.races:
                self.races.add(label)
                raise GitLabHTTPError(409, path)
            return value

        def json_request(self, method, path, body=None):
            if method == "GET":
                if "/members/" in path:
                    resource, resource_id, _, user_id = path.split("/")[-4:]
                    key = (resource, int(resource_id), int(user_id))
                    return {"id": key[2]} if key in self.memberships else None
                if path.startswith("/api/v4/groups/"):
                    return self.groups.get(unquote(path.rsplit("/", 1)[-1]))
                if path.startswith("/api/v4/users?"):
                    username = parse_qs(path.split("?", 1)[1])["username"][0]
                    return [self.users[username]] if username in self.users else []
                if path.startswith("/api/v4/projects/"):
                    return self.project
            if method == "POST":
                self.creates.append((path, body))
                if path == "/api/v4/groups":
                    parent = next(
                        (
                            p
                            for p, v in self.groups.items()
                            if v["id"] == body.get("parent_id")
                        ),
                        "",
                    )
                    group = "/".join(part for part in (parent, body["path"]) if part)
                    value = self._new(self.groups, group, body)
                    return (
                        self._race("group", path, value)
                        if group == "cdr-group"
                        else value
                    )
                if path == "/api/v4/users":
                    value = self._new(self.users, body["username"], body)
                    return (
                        self._race("user", path, value)
                        if body["username"] == "alice"
                        else value
                    )
                if path == "/api/v4/projects":
                    self.project = {**body, "id": self.next_id}
                    self.next_id += 1
                    return self.project
                if path.endswith("/members"):
                    resource, resource_id = path.split("/")[-3:-1]
                    self.memberships.add(
                        (resource, int(resource_id), int(body["user_id"]))
                    )
                    return self._race("membership", path, {"id": body["user_id"]})
            raise AssertionError((method, path, body))

    assert set(INHERENTLY_PLAIN) == {"all", "x"}
    assert SEED_GROUPS == ("team",)
    api = FakeGitLab()
    cases = [{"input": "@alice @types/node @team @all @x", "expected": "\uff20bob"}]
    first = seed(cases, api)
    create_count = len(api.creates)
    second = seed(cases, api)
    assert set(first["groups"]) == {"cdr-group", "team", "types", "types/node"}
    assert set(first["users"]) == {"alice", "bob"}
    assert "user" in api.races
    assert first["users"]["alice"] == api.users["alice"]
    assert "membership" in api.races
    assert second["project"] == first["project"]
    assert len(api.creates) == create_count
    assert any(body.get("initialize_with_readme") is True for _, body in api.creates)
    assert all(
        ("groups", first["groups"][group]["id"], user["id"]) in api.memberships
        for group in ("cdr-group", "team")
        for user in first["users"].values()
    )
    assert all(
        ("projects", first["project"]["id"], user["id"]) in api.memberships
        for user in first["users"].values()
    )


DIGEST = "279c65eeaeb5f4b7eb20b33893b9879c12b5a6f6c366e95ce5f6e104c4e179cc"
PREFIX = "http://gitlab.test/-/blob/main/"
PAIR = "aa6eb1a0172a201782cdb664713ced8fe19b43943230740efa6ceb90f81107d6"


def _probe_rows(github_html="<p>a</p>", gitlab_html="<p>b</p>"):
    return [
        {
            "id": "probe_row",
            "input": "@alice",
            "expected": "\uff20alice",
            "github_probe": {
                "renderer": "gh api markdown mode=gfm",
                "rendered": "2026-01-01",
                "input": "expected + blank line + footer line",
                "input_sha256": DIGEST,
                "html": github_html,
            },
            "gitlab_probe": {
                "renderer": "gitlab api markdown gfm=true project",
                "version": "19.4.1 ce",
                "rendered": "2026-01-01",
                "input": "expected + blank line + footer line",
                "input_sha256": DIGEST,
                "blob_prefix": PREFIX,
                "twin_references": ["@alice"],
                "html": gitlab_html,
                "divergence": {"note": "old note", "issue": 12, "pair_sha256": PAIR},
            },
        }
    ]


@pytest.fixture
def probe_file(tmp_path):
    path = tmp_path / "fixture.json"
    write_utf8(str(path), serialize_fixture(_probe_rows()))
    return path


def _renderer(platform: str, html: str) -> Callable[[str], str]:
    def render(text: str) -> str:
        if text == "@lee":
            return '<a class="user-mention" href="/lee">@lee</a>'
        if platform == "gitlab" and "@alice" in text:
            return (
                '<p><a data-reference-type="user" data-original="@alice">@alice</a></p>'
            )
        return html

    return render


def _record(path, platform, html, **options):
    return record_fixture(
        str(path),
        platform=platform,
        render=_renderer(platform, html),
        version="19.4.1 ce",
        blob_prefix=PREFIX,
        get_github_login=lambda: "lee",
        **options,
    )


def _read_fixture(path: Path) -> str:
    with path.open("r", encoding="utf-8", newline="") as stream:
        return str(stream.read())


def _add_extra_root_key(path: Path) -> None:
    source = _read_fixture(path).replace('"cases": [', '"extra": true, "cases": [')
    write_utf8(str(path), source)


class RejectCase(NamedTuple):
    platform: str
    html: str
    error_type: type[Exception]
    error: str
    setup: Callable[[Path], None] | None = None


RECORD_REJECTS_BEFORE_WRITE_CASES: dict[str, RejectCase] = {
    "github-preflight": RejectCase("github", "<p>@lee</p>", RuntimeError, "lee"),
    "expected-reference": RejectCase(
        "gitlab",
        '<p><a data-reference-type="user" data-original="@alice">@alice</a></p>',
        ValueError,
        "GitLab reference link",
    ),
    "extra-root-key": RejectCase(
        "gitlab", "<p>b</p>", ValueError, "only a cases", _add_extra_root_key
    ),
}


def test_record_pair_states(probe_file: Path) -> None:
    before = probe_file.read_bytes()
    writes: list[str] = []
    checked = _record(
        probe_file,
        "gitlab",
        "<p>b</p>",
        check=True,
        write_text=lambda target, contents: writes.append(contents),
    )
    assert checked.changed_ids == []
    assert checked.divergence_cleared == []
    assert checked.exit_code == 0
    assert writes == [] and probe_file.read_bytes() == before
    recorded = _record(probe_file, "gitlab", "<p>b</p>")
    assert recorded.exit_code == 0
    assert recorded.divergence_cleared == []
    assert recorded.cleared_details == []
    divergence = json.loads(_read_fixture(probe_file))["cases"][0]["gitlab_probe"][
        "divergence"
    ]
    assert divergence == {"note": "old note", "issue": 12, "pair_sha256": PAIR}


def test_check_drift_reports_gitlab_content_and_version_together(probe_file) -> None:
    rows = _probe_rows()
    rows[0]["gitlab_probe"]["version"] = "19.4.0 ce"
    write_utf8(str(probe_file), serialize_fixture(rows))
    before = probe_file.read_bytes()
    writes = []

    checked = _record(
        probe_file,
        "gitlab",
        "<p>changed</p>",
        check=True,
        write_text=lambda target, contents: writes.append(contents),
    )

    assert checked.changed_ids == ["probe_row"]
    assert checked.version_mismatch == ("19.4.0 ce", "19.4.1 ce")
    assert checked.exit_code == 1
    assert writes == [] and probe_file.read_bytes() == before


@pytest.mark.parametrize(
    ("platform", "html", "error_type", "error", "setup"),
    RECORD_REJECTS_BEFORE_WRITE_CASES.values(),
    ids=RECORD_REJECTS_BEFORE_WRITE_CASES,
)
def test_record_rejects_before_write(
    probe_file, platform, html, error_type, error, setup
) -> None:
    if setup is not None:
        setup(probe_file)
    before = probe_file.read_bytes()
    writes = []

    def render(_text: str) -> str:
        return str(html)

    with pytest.raises(error_type, match=error):
        record_fixture(
            str(probe_file),
            platform=platform,
            render=render,
            version="19.4.1 ce",
            blob_prefix=PREFIX,
            get_github_login=lambda: "lee",
            write_text=lambda target, contents: writes.append(contents),
        )
    assert writes == [] and probe_file.read_bytes() == before


def test_main_record_check_and_cleared_divergence_output(
    probe_file: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = probe_file
    monkeypatch.setattr("tests.tools.probe_cli._fixture_path", lambda: str(path))

    class Client:
        def render(self, text: str, project: str) -> str:
            return _renderer("gitlab", "<p>\uff20alice</p>")(text)

    monkeypatch.setattr(
        "tests.tools.probe_cli._client_from_environment", lambda *args: Client()
    )
    monkeypatch.setattr(
        "tests.tools.probe_cli._gitlab_version", lambda client: "19.4.1 ce"
    )
    monkeypatch.setattr(
        "tests.tools.probe_cli._gitlab_blob_prefix",
        lambda client, project: "http://gitlab.test/-/blob/main/",
    )
    argv = ["record", "--platform", "gitlab"]
    source = path.read_bytes()
    assert main([*argv, "--check"]) == 1
    assert capsys.readouterr().out == "probe_row\nstale divergence: probe_row\n"
    assert path.read_bytes() == source
    assert main(argv) == 0
    assert (
        capsys.readouterr().out == "cleared divergence: probe_row: issue 12: old note\n"
    )
    assert (
        json.loads(_read_fixture(path))["cases"][0]["gitlab_probe"]["divergence"]
        is None
    )


def test_main_seed_dispatch_prints_seed_counts(monkeypatch, capsys) -> None:
    monkeypatch.setenv("FAKE_ENV", "fake-token")
    cases = [{"input": "@alice", "expected": "@alice"}]
    client = object()

    def fake_client(base_url: str, token_env: str) -> object:
        assert base_url == "http://localhost:8929"
        assert token_env == "FAKE_ENV"
        assert os.environ[token_env] == "fake-token"
        return client

    def fake_seed(actual_cases, actual_client):
        assert actual_cases == cases
        assert actual_client is client
        return {"users": {"alice": {}}, "groups": {"team": {}, "cdr-group": {}}}

    monkeypatch.setattr("tests.tools.probe_cli._fixture_path", lambda: "unused")
    monkeypatch.setattr("tests.tools.probe_cli._read_cases", lambda path: cases)
    monkeypatch.setattr("tests.tools.probe_cli._client_from_environment", fake_client)
    monkeypatch.setattr("tests.tools.probe_cli.seed", fake_seed)
    assert main(["seed", "--token-env", "FAKE_ENV"]) == 0
    assert capsys.readouterr().out == "seeded 1 users and 2 groups\n"


@pytest.mark.parametrize(
    "failure",
    ["stale", "equal", "skeleton", "duplicate", "issue"],
)
def test_divergence_refuses_invalid_fixture_or_pair(probe_file, failure: str) -> None:
    path = probe_file
    document = {"cases": _probe_rows()}
    row = document["cases"][0]
    if failure == "stale":
        row["gitlab_probe"]["input_sha256"] = "0" * 64
    elif failure == "equal":
        row["gitlab_probe"]["html"] = "<p>a</p>"
    elif failure == "skeleton":
        row["gitlab_probe"]["html"] = "<h1>b</h1>"
    elif failure == "duplicate":
        document["cases"].append(dict(row))
    source = json.dumps(document, ensure_ascii=False)
    write_utf8(str(path), source)
    writes: list[str] = []
    with pytest.raises(
        ValueError,
        match={
            "stale": "stale",
            "equal": "equal",
            "skeleton": "skeleton",
            "duplicate": "expected one fixture row",
            "issue": "positive issue",
        }[failure],
    ):
        record_divergence(
            str(path),
            case_id="probe_row",
            issue=0 if failure == "issue" else 378,
            note="text differs",
            write_text=lambda target, contents: writes.append(contents),
        )
    assert writes == []
    assert _read_fixture(path) == source


def test_divergence_cli_binds_stored_pair(probe_file, monkeypatch, capsys) -> None:
    monkeypatch.setattr("tests.tools.probe_cli._fixture_path", lambda: str(probe_file))
    assert (
        main(
            [
                "divergence",
                "--id",
                "probe_row",
                "--issue",
                "378",
                "--note",
                "text differs",
            ]
        )
        == 0
    )
    assert capsys.readouterr().out == ""
    assert _read_fixture(probe_file) == (
        '{\n  "cases": [\n'
        '    {"id":"probe_row","input":"@alice","expected":"\uff20alice","github_probe":{"renderer":"gh api markdown mode=gfm","rendered":"2026-01-01","input":"expected + blank line + footer line","input_sha256":"279c65eeaeb5f4b7eb20b33893b9879c12b5a6f6c366e95ce5f6e104c4e179cc","html":"<p>a</p>"},"gitlab_probe":{"renderer":"gitlab api markdown gfm=true project","version":"19.4.1 ce","rendered":"2026-01-01","input":"expected + blank line + footer line","input_sha256":"279c65eeaeb5f4b7eb20b33893b9879c12b5a6f6c366e95ce5f6e104c4e179cc","blob_prefix":"http://gitlab.test/-/blob/main/","twin_references":["@alice"],"html":"<p>b</p>","divergence":{"note":"text differs","issue":378,"pair_sha256":"aa6eb1a0172a201782cdb664713ced8fe19b43943230740efa6ceb90f81107d6"}}}\n'
        "  ]\n}\n"
    )


def test_pair_hash_literal_vector() -> None:
    github = '<a href="docs/guide.md">guide</a>'
    gitlab = '<a href="http://gitlab.test/cdr-group/probe/-/blob/main/docs/guide.md">guide</a>'
    prefix = "http://gitlab.test/cdr-group/probe/-/blob/main/"
    assert (
        pair_sha256(github, gitlab, blob_prefix=prefix)
        == "1148026037b007d6e3c3be58fad8d10dc37b187971c598e674fb24e70bf9f2fe"
    )
    assert (
        pair_sha256(github, gitlab)
        == "939095999b04257182f95ee87c64e18b8c3d748a4527668a1389c3ed58c89b64"
    )
    assert (
        pair_sha256(github, '<a href="other.md">guide</a>', blob_prefix=prefix)
        == "a7b9506657a5d97a696cb4ebdab6b4d150c203016d23e0d09638f8a24b375832"
    )


def test_recorder_uses_literal_canonical_layout_for_two_rows(tmp_path) -> None:
    path = tmp_path / "fixture.json"
    write_utf8(
        str(path),
        serialize_fixture(
            [
                {"id": "a", "input": "", "expected": "A"},
                {"id": "b", "input": "", "expected": "B"},
            ]
        ),
    )
    record_fixture(
        str(path),
        platform="github",
        render=lambda text: (
            '<a class="user-mention" href="/lee">@lee</a>'
            if text == "@lee"
            else "<p>ok</p>"
        ),
        get_github_login=lambda: "lee",
        clock=lambda: "2026-09-24",
    )
    assert _read_fixture(path) == (
        '{\n  "cases": [\n'
        '    {"id":"a","input":"","expected":"A","github_probe":{"renderer":"gh api markdown mode=gfm","rendered":"2026-09-24","input":"expected + blank line + footer line","input_sha256":"880da50da47b8c629dd9b6e0095bcb38be1199ed5ea106d893143121981704ca","html":"<p>ok</p>"}},\n'
        '    {"id":"b","input":"","expected":"B","github_probe":{"renderer":"gh api markdown mode=gfm","rendered":"2026-09-24","input":"expected + blank line + footer line","input_sha256":"81cabb090bb05b16a737a06294f12f9e1e0215ee3c091d5c490569f0dbc08b55","html":"<p>ok</p>"}}\n'
        "  ]\n}\n"
    )


def test_gitlab_recorder_literal_layout_and_sorted_twin_references(probe_file) -> None:
    def render(text):
        if "@alice" in text:
            return '<a data-reference-type="user" data-original="@z"></a><a data-reference-type="user" data-original="@alice"></a>'
        return "<p>\uff20alice</p>"

    rows = _probe_rows()
    del rows[0]["gitlab_probe"]
    rows[0]["github_probe"]["html"] = "<p>\uff20alice</p>"
    write_utf8(str(probe_file), serialize_fixture(rows))
    result = record_fixture(
        str(probe_file),
        platform="gitlab",
        render=render,
        version="19.4.1 ce",
        blob_prefix=PREFIX,
        clock=lambda: "2026-09-24",
    )
    assert result.exit_code == 0
    assert _read_fixture(probe_file) == (
        '{\n  "cases": [\n'
        '    {"id":"probe_row","input":"@alice","expected":"\uff20alice","github_probe":{"renderer":"gh api markdown mode=gfm","rendered":"2026-01-01","input":"expected + blank line + footer line","input_sha256":"279c65eeaeb5f4b7eb20b33893b9879c12b5a6f6c366e95ce5f6e104c4e179cc","html":"<p>\uff20alice</p>"},"gitlab_probe":{"renderer":"gitlab api markdown gfm=true project","version":"19.4.1 ce","rendered":"2026-09-24","input":"expected + blank line + footer line","input_sha256":"279c65eeaeb5f4b7eb20b33893b9879c12b5a6f6c366e95ce5f6e104c4e179cc","blob_prefix":"http://gitlab.test/-/blob/main/","twin_references":["@alice","@z"],"html":"<p>\uff20alice</p>","divergence":null}}\n'
        "  ]\n}\n"
    )

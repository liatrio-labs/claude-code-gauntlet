from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import parse_qs, unquote

import pytest  # type: ignore[import-not-found]

from tests.tools.render_probes import (
    INHERENTLY_PLAIN,
    SEED_GROUPS,
    GitLabClient,
    GitLabHTTPError,
    _gitlab_blob_prefix,
    _gitlab_version,
    _reference_originals,
    build_argument_parser,
    check_render,
    derive_handles,
    github_login,
    input_sha256,
    main,
    pair_sha256,
    record_divergence,
    record_fixture,
    render_github,
    run_canary,
    run_github_canary,
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

    default = parser.parse_args(["quick-actions"])
    direct = parser.parse_args(["quick-actions", "--check"])
    explicit = parser.parse_args(
        ["quick-actions", "record", "--check", "--container", "gitlab-test"]
    )

    assert default.container == "cdr-gitlab"
    assert direct.command == "quick-actions"
    assert direct.action == "record"
    assert direct.check is True
    assert explicit.container == "gitlab-test"


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
    ("left", "right"),
    [
        ("<copy-code><span>noise</span></copy-code><p>x</p>", "<p>x</p>"),
        ("<insert-code-snippet>noise</insert-code-snippet><p>x</p>", "<p>x</p>"),
        ('<h1>head<a href="#head" class="anchor"></a></h1>', "<h1>head</h1>"),
        (
            '<div class="gl-relative markdown-code-block js-markdown-code"><p>x</p></div>',
            "<p>x</p>",
        ),
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
        ("<p>a\t\n b</p>", "<p>a b</p>"),
    ],
)
def test_structure_rule_must_equal_pairs(left: str, right: str) -> None:
    platform = (
        "github"
        if any(
            marker in left
            for marker in (
                "notranslate",
                "highlight-text-adblock",
                "markdown-accessiblity-table",
            )
        )
        else "gitlab"
    )
    assert structure(left, platform=platform) == structure(right, platform=platform)


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
        ("<p>a<!-- c -->b</p>", "<p>ab</p>"),
        ("<hr>", ""),
        ("<p>a\u00a0b</p>", "<p>a b</p>"),
        ("<p>a&nbsp;b</p>", "<p>a b</p>"),
        ("<pre>x\u00a0</pre>", "<pre>x</pre>"),
        ("\u00a0x", "x"),
        ('<abbr title="HTML">HTML</abbr>', "HTML"),
        ('<a href="#head" class="other anchor">content</a>', "content"),
        ('<a href="#head" class="anchor" onclick="run()">content</a>', "content"),
        ('<h1>T<a href="#poison"></a></h1>', "<h1>T</h1>"),
        ('<h1>T<a href="#poison" class="anchored"></a></h1>', "<h1>T</h1>"),
        ('<h1>T<a href="#head" class="other anchor">c</a></h1>', "<h1>T</h1>"),
        ('<h1>T<a href="#head" class="anchor" onclick="run()"></a></h1>', "<h1>T</h1>"),
        ('<h1>T<a class="anchor" href="#t"></a>X</h1>', "<h1>TX</h1>"),
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
    assert structure(left, platform="gitlab") != structure(right, platform="gitlab")


def test_blob_prefix_only_normalizes_matching_project_links() -> None:
    prefix = "http://localhost:8929/cdr-group/probe/-/blob/main/"
    assert structure(
        f'<a href="{prefix}">x</a>', platform="gitlab", blob_prefix=prefix
    ) == structure('<a href="">x</a>', platform="gitlab")
    assert structure(
        f'<a href="{prefix}file.md">x</a>', platform="gitlab", blob_prefix=prefix
    ) == structure('<a href="file.md">x</a>', platform="gitlab")
    assert structure(
        '<a href="https://other.test/file.md">x</a>',
        platform="gitlab",
        blob_prefix=prefix,
    ) != structure('<a href="file.md">x</a>', platform="gitlab")
    for foreign in (
        "https://elsewhere.test/cdr-group/probe/-/blob/main/file.md",
        "http://localhost:8929/other/proj/-/blob/main/file.md",
    ):
        assert structure(
            f'<a href="{foreign}">x</a>', platform="gitlab", blob_prefix=prefix
        ) != structure('<a href="file.md">x</a>', platform="gitlab", blob_prefix=prefix)


def test_gitlab_blob_prefix_strips_web_url_slash_and_uses_default_branch(
    monkeypatch,
) -> None:
    client = GitLabClient("http://gitlab.test", "fake-token")
    requests = []

    def json_request(method: str, path: str) -> dict[str, str]:
        requests.append((method, path))
        return {
            "web_url": "https://gitlab.test/team/probe/",
            "default_branch": "develop",
        }

    monkeypatch.setattr(client, "json_request", json_request)
    assert (
        _gitlab_blob_prefix(client, "team/probe")
        == "https://gitlab.test/team/probe/-/blob/develop/"
    )
    assert requests == [("GET", "/api/v4/projects/team%2Fprobe")]


def test_gitlab_blob_prefix_rejects_incomplete_metadata(monkeypatch) -> None:
    client = GitLabClient("http://gitlab.test", "fake-token")
    monkeypatch.setattr(
        client,
        "json_request",
        lambda method, path: {"web_url": "https://gitlab.test/team/probe"},
    )
    with pytest.raises(
        ValueError, match="GitLab project metadata is incomplete for team/probe"
    ):
        _gitlab_blob_prefix(client, "team/probe")


def test_renderer_attributes_are_platform_and_value_specific() -> None:
    assert structure('<p dir="rtl">y</p>', platform="github") != structure(
        '<p dir="auto">y</p>', platform="gitlab"
    )
    assert structure('<p dir="auto">y</p>', platform="gitlab") == structure(
        "<p>y</p>", platform="gitlab"
    )
    assert structure('<p dir="rtl">y</p>', platform="gitlab") != structure(
        "<p>y</p>", platform="gitlab"
    )
    assert structure('<table role="table"></table>', platform="github") == structure(
        "<table></table>", platform="github"
    )
    assert structure('<table role="alert"></table>', platform="github") != structure(
        "<table></table>", platform="github"
    )
    assert structure(
        '<a href="x" rel="nofollow">x</a>', platform="github"
    ) == structure('<a href="x">x</a>', platform="github")
    pairs = [
        ("gitlab", '<code class="notranslate">x</code>', "<code>x</code>"),
        ("gitlab", '<a href="x" class="other">x</a>', '<a href="x">x</a>'),
        ("github", '<a href="x" class="gfm">x</a>', '<a href="x">x</a>'),
        ("gitlab", '<a href="x" rel="nofollow">x</a>', '<a href="x">x</a>'),
        ("github", '<a href="x" rel="noopener">x</a>', '<a href="x">x</a>'),
        ("gitlab", '<a href="x" target="_self">x</a>', '<a href="x">x</a>'),
        ("github", '<a href="x" target="_blank">x</a>', '<a href="x">x</a>'),
        ("github", '<pre v-pre="true">x</pre>', "<pre>x</pre>"),
        ("gitlab", '<table role="table"></table>', "<table></table>"),
        ("gitlab", '<pre class="notranslate">x</pre>', "<pre>x</pre>"),
        (
            "github",
            '<pre class="code highlight js-syntax-highlight language-plaintext">x</pre>',
            "<pre>x</pre>",
        ),
        ("gitlab", '<p id="user-content-x">x</p>', "<p>x</p>"),
        ("github", '<h1 id="user-content-x">x</h1>', "<h1>x</h1>"),
        ("gitlab", '<pre v-pre="false">x</pre>', "<pre>x</pre>"),
        ("gitlab", '<a href="x" rel="noopener">x</a>', '<a href="x">x</a>'),
        ("gitlab", '<a href="x" target="_self">x</a>', '<a href="x">x</a>'),
    ]
    for platform, left, right in pairs:
        assert structure(left, platform=platform) != structure(right, platform=platform)


def test_heading_ids_and_empty_generated_anchors_cover_all_heading_levels() -> None:
    for level in range(1, 7):
        rendered = (
            f'<h{level} id="user-content-title">Title'
            '<a class="other anchor" href="#title" aria-label="Link to heading"></a>'
            f"</h{level}>"
        )
        assert structure(rendered, platform="gitlab") == f"<h{level}>Title</h{level}>"
    assert structure(
        '<p><a class="anchor" href="#title"></a></p>', platform="gitlab"
    ) != structure("<p></p>", platform="gitlab")


def test_pair_hash_binds_gitlab_links_after_blob_prefix_normalization() -> None:
    prefix = "http://gitlab.test/cdr-group/probe/-/blob/main/"
    github_html = '<a href="docs/guide.md">guide</a>'
    gitlab_html = f'<a href="{prefix}docs/guide.md">guide</a>'
    normalized_pair = (
        structure(github_html, platform="github")
        + "\0"
        + structure(gitlab_html, platform="gitlab", blob_prefix=prefix)
    )
    assert (
        pair_sha256(github_html, gitlab_html, blob_prefix=prefix)
        == hashlib.sha256(normalized_pair.encode("utf-8")).hexdigest()
    )
    assert pair_sha256(github_html, gitlab_html, blob_prefix=prefix) != pair_sha256(
        github_html, gitlab_html
    )


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
    with pytest.raises(ValueError, match="unknown element <div>"):
        check_render("gitlab", "<div>x</div>")
    with pytest.raises(ValueError, match="unknown element <div>"):
        check_render("gitlab", "<div><pre>x</pre></div>")
    with pytest.raises(ValueError, match="unknown element <span>"):
        check_render("gitlab", "<span>x</span>")


def test_hard_check_rejects_github_mentions_comments_and_unknown_elements() -> None:
    for kind in ("user-mention", "team-mention"):
        with pytest.raises(ValueError, match="GitHub mention anchor"):
            check_render("github", f'<a class="{kind}">@team</a>')
    with pytest.raises(ValueError, match="comment node"):
        check_render("github", "<!-- retained -->")
    with pytest.raises(ValueError, match="unknown element <abbr>"):
        check_render("github", "<abbr>HTML</abbr>")
    with pytest.raises(ValueError, match="unknown element <div>"):
        check_render("github", "<div>x</div>")
    with pytest.raises(ValueError, match="unknown element <div>"):
        check_render("github", "<div><pre>x</pre></div>")


def test_chrome_signatures_are_scoped_to_the_renderer() -> None:
    with pytest.raises(ValueError, match="unknown element <div>"):
        check_render(
            "github",
            '<div class="gl-relative markdown-code-block js-markdown-code"><pre>x</pre></div>',
        )
    with pytest.raises(ValueError, match="unknown element <div>"):
        check_render(
            "gitlab", '<div class="highlight highlight-text-adblock"><pre>x</pre></div>'
        )
    with pytest.raises(ValueError, match="unknown element <span>"):
        check_render(
            "github", '<span data-escaped-char="" data-sourcepos="1:1">x</span>'
        )
    pairs = [
        ("github", '<h1>T<a class="anchor" href="#t"></a></h1>', "<h1>T</h1>"),
        ("gitlab", '<span class="line" id="bad" data-lang="plaintext">x</span>', "x"),
        ("gitlab", '<copy-code title="x">x</copy-code>', ""),
        (
            "github",
            '<markdown-accessiblity-table title="x"><p>x</p></markdown-accessiblity-table>',
            "<p>x</p>",
        ),
    ]
    for platform, left, right in pairs:
        assert structure(left, platform=platform) != structure(right, platform=platform)


def test_hard_check_accepts_inlined_gitlab_heading_code_and_angle_attribute_renders() -> (
    None
):
    check_render("gitlab", REAL_HEADING_HTML)
    check_render("gitlab", REAL_CODE_HTML)
    check_render("gitlab", REAL_ANGLE_ATTRIBUTE_HTML)
    assert "<b>" in REAL_ANGLE_ATTRIBUTE_HTML
    assert (
        structure(REAL_CODE_HTML, platform="gitlab")
        == "<pre>@user &lt;table&gt; @</pre> <p>footer line</p>"
    )
    assert (
        structure(REAL_ANGLE_ATTRIBUTE_HTML, platform="gitlab")
        == "<h1>heading &lt;b&gt; \uff20leehopper`</h1>"
    )
    assert (
        structure(REAL_HEADING_HTML, platform="gitlab")
        == "<h2>`x Heading</h2><p>&lt;b&gt; \uff20leehopper`</p><p>footer line</p>"
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
            "input": f"@{segment} @{'b' * 257} @{path} @{'/'.join(['c'] * 21)}",
            "expected": "",
        }
    ]
    assert derive_handles(cases) == {segment, path}


def test_fullwidth_handle_derivation_uses_the_same_filters() -> None:
    cases = [
        {
            "input": "@ascii",
            "expected": "\uff20visible @ascii \uff20all \uff20x \uff20ignore.git \uff20"
            + "b" * 257,
        }
    ]
    assert derive_handles(cases, fullwidth_only=True) == {"visible"}


def test_canary_names_unresolved_seedable_handles_and_skips_plain_handles() -> None:
    def fake_render(text: str) -> str:
        if text == "@alice":
            return '<a data-reference-type="user" data-original="@alice">@alice</a>'
        return text

    run_canary(["alice", "all", "x"], fake_render)
    with pytest.raises(RuntimeError, match="unresolved handles: missing"):
        run_canary(["alice", "missing", "all", "x"], fake_render)


def test_canary_rejects_wrong_handle_reference() -> None:
    with pytest.raises(RuntimeError, match="alice"):
        run_canary(
            ["alice"],
            lambda text: (
                '<a data-reference-type="user" data-original="@other">other</a>'
            ),
        )


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
            self.raced_user = False
            self.raced_membership = False

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
                created = self._new(self.users, body["username"], body)
                if body["username"] == "alice" and not self.raced_user:
                    self.raced_user = True
                    raise GitLabHTTPError(409, path)
                return created
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
                if not self.raced_membership:
                    self.raced_membership = True
                    raise GitLabHTTPError(409, path)
                return {"id": body["user_id"]}
            raise AssertionError((method, path, body))

    api = FakeGitLab()
    cases = [{"input": "@alice @types/node @team @all @x", "expected": "\uff20bob"}]
    first = seed(cases, api)
    create_count = len(api.creates)
    second = seed(cases, api)
    assert set(first["groups"]) == {"cdr-group", "team", "types", "types/node"}
    assert set(first["users"]) == {"alice", "bob"}
    assert api.raced_user
    assert first["users"]["alice"] == api.users["alice"]
    assert api.raced_membership
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


def _write_divergent_probe_fixture(
    path: Path, *, github_html: str = "<p>a</p>"
) -> None:
    _write_initial_probe_fixture(path)
    document = json.loads(_read_fixture(path))
    row = document["cases"][0]
    digest = input_sha256("\uff20alice\n\nfooter line")
    row["github_probe"].update(html=github_html, input_sha256=digest)
    row["gitlab_probe"] = {
        "renderer": "gitlab api markdown gfm=true project",
        "version": "19.4.1 ce",
        "rendered": "2026-01-01",
        "input": "expected + blank line + footer line",
        "input_sha256": digest,
        "blob_prefix": "http://gitlab.test/-/blob/main/",
        "twin_references": ["@alice"],
        "html": "<p>b</p>",
        "divergence": {
            "note": "old note",
            "issue": 12,
            "pair_sha256": pair_sha256(github_html, "<p>b</p>"),
        },
    }
    write_utf8(str(path), serialize_fixture(document["cases"]))


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


def test_record_fixture_runs_gitlab_canary_before_writing(tmp_path) -> None:
    path = tmp_path / "fixture.json"
    _write_initial_probe_fixture(path)
    original = path.read_bytes()
    writes: list[str] = []
    with pytest.raises(RuntimeError, match="alice"):
        record_fixture(
            str(path),
            platform="gitlab",
            render=lambda text: f"<p>{text}</p>",
            version="19.4.1 ce",
            blob_prefix="http://gitlab.test/-/blob/main/",
            write_text=lambda target, contents: writes.append(contents),
        )
    assert writes == []
    assert path.read_bytes() == original


def test_github_canary_uses_authenticated_login_and_rejects_plain_render(
    tmp_path,
) -> None:
    seen: list[list[str]] = []

    def runner(argv, **kwargs):
        seen.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="lee\n", stderr="")

    assert github_login(runner=runner) == "lee"
    assert seen == [["gh", "api", "user", "--jq", ".login"]]
    with pytest.raises(RuntimeError, match="lee"):
        run_github_canary("lee", lambda text: f"<p>{text}</p>")
    path = tmp_path / "fixture.json"
    _write_initial_probe_fixture(path)
    original = path.read_bytes()
    writes: list[str] = []
    with pytest.raises(RuntimeError, match="lee"):
        record_fixture(
            str(path),
            platform="github",
            render=lambda text: f"<p>{text}</p>",
            get_github_login=lambda: "lee",
            write_text=lambda target, contents: writes.append(contents),
        )
    assert writes == []
    assert path.read_bytes() == original


@pytest.mark.parametrize(
    "canary_html",
    [
        '<a href="https://github.com/lee">@lee</a>',
        '<a class="user-mention" href="https://github.com/other">@other</a>',
    ],
)
def test_github_canary_rejects_unqualified_links_before_write(
    tmp_path, canary_html: str
) -> None:
    path = tmp_path / "fixture.json"
    _write_initial_probe_fixture(path)
    original = path.read_bytes()
    writes: list[str] = []
    with pytest.raises(RuntimeError, match="lee"):
        record_fixture(
            str(path),
            platform="github",
            render=lambda text: canary_html if text == "@lee" else "<p>x</p>",
            get_github_login=lambda: "lee",
            write_text=lambda target, contents: writes.append(contents),
        )
    assert writes == []
    assert path.read_bytes() == original


def test_github_canary_accepts_matching_href_or_text() -> None:
    for rendered in (
        '<a class="notranslate user-mention" href="https://github.com/LeE">user</a>',
        '<a class="user-mention" href="/somewhere">@LeE</a>',
    ):

        def render(text: str, *, value: str = rendered) -> str:
            return value

        run_github_canary("lee", render)


def _github_render_with_canary(text: str) -> str:
    if text == "@lee":
        return '<a class="user-mention" href="/lee">@lee</a>'
    return "<p>changed</p>"


def test_github_record_clears_pair_bound_divergence(tmp_path) -> None:
    path = tmp_path / "fixture.json"
    _write_initial_probe_fixture(path)
    document = json.loads(_read_fixture(path))
    row = document["cases"][0]
    row["github_probe"]["html"] = "<p>old</p>"
    row["github_probe"]["input_sha256"] = input_sha256("\uff20alice\n\nfooter line")
    row["gitlab_probe"] = {
        "renderer": "gitlab api markdown gfm=true project",
        "version": "19.4.1 ce",
        "rendered": "2026-01-01",
        "input": "expected + blank line + footer line",
        "input_sha256": input_sha256("\uff20alice\n\nfooter line"),
        "blob_prefix": "http://gitlab.test/-/blob/main/",
        "twin_references": ["@alice"],
        "html": "<p>new</p>",
        "divergence": {
            "note": "old note",
            "issue": 12,
            "pair_sha256": pair_sha256("<p>old</p>", "<p>new</p>"),
        },
    }
    write_utf8(str(path), serialize_fixture(document["cases"]))
    result = record_fixture(
        str(path),
        platform="github",
        render=_github_render_with_canary,
        get_github_login=lambda: "lee",
    )
    assert result.divergence_cleared == ["probe_row"]
    assert result.cleared_details == ["probe_row: issue 12: old note"]
    assert result.cases[0]["gitlab_probe"]["divergence"] is None


def test_check_detects_twin_only_change_and_stale_divergence(tmp_path) -> None:
    path = tmp_path / "fixture.json"
    _write_initial_probe_fixture(path)
    record_fixture(
        str(path),
        platform="gitlab",
        render=_fake_gitlab_render,
        version="19.4.1 ce",
        blob_prefix="http://gitlab.test/-/blob/main/",
    )

    def twin_changed(text: str) -> str:
        if text.startswith("@alice\n"):
            return '<p><a data-original="@bob" data-reference-type="user">@bob</a></p>'
        return _fake_gitlab_render(text)

    result = record_fixture(
        str(path),
        platform="gitlab",
        render=twin_changed,
        version="19.4.1 ce",
        blob_prefix="http://gitlab.test/-/blob/main/",
        check=True,
    )
    assert result.changed_ids == ["probe_row"]
    assert result.exit_code == 1
    document = json.loads(_read_fixture(path))
    document["cases"][0]["gitlab_probe"]["divergence"] = {
        "note": "stale",
        "issue": 12,
        "pair_sha256": "0" * 64,
    }
    write_utf8(str(path), serialize_fixture(document["cases"]))
    stale = record_fixture(
        str(path),
        platform="gitlab",
        render=_fake_gitlab_render,
        version="19.4.1 ce",
        blob_prefix="http://gitlab.test/-/blob/main/",
        check=True,
    )
    assert stale.divergence_cleared == ["probe_row"]
    assert stale.exit_code == 1


def test_github_check_reports_html_and_stale_divergence_without_writing(
    tmp_path,
) -> None:
    path = tmp_path / "fixture.json"
    _write_divergent_probe_fixture(path)
    original = path.read_bytes()
    writes: list[str] = []
    changed = record_fixture(
        str(path),
        platform="github",
        render=_github_render_with_canary,
        get_github_login=lambda: "lee",
        check=True,
        write_text=lambda target, contents: writes.append(contents),
    )
    assert changed.changed_ids == ["probe_row"]
    assert changed.exit_code == 1
    assert writes == [] and path.read_bytes() == original

    document = json.loads(_read_fixture(path))
    document["cases"][0]["gitlab_probe"]["divergence"]["pair_sha256"] = "0" * 64
    write_utf8(str(path), serialize_fixture(document["cases"]))
    original = path.read_bytes()
    stale = record_fixture(
        str(path),
        platform="github",
        render=lambda text: (
            '<a class="user-mention" href="/lee">@lee</a>'
            if text == "@lee"
            else "<p>a</p>"
        ),
        get_github_login=lambda: "lee",
        check=True,
        write_text=lambda target, contents: writes.append(contents),
    )
    assert stale.changed_ids == []
    assert stale.divergence_cleared == ["probe_row"]
    assert stale.exit_code == 1
    assert writes == [] and path.read_bytes() == original


@pytest.mark.parametrize("platform", ["gitlab", "github"])
def test_main_record_check_and_cleared_divergence_output(
    tmp_path, monkeypatch, capsys, platform: str
) -> None:
    path = tmp_path / "fixture.json"
    _write_divergent_probe_fixture(path)
    monkeypatch.setattr("tests.tools.render_probes._fixture_path", lambda: str(path))
    if platform == "github":
        monkeypatch.setattr("tests.tools.render_probes.github_login", lambda: "lee")
        monkeypatch.setattr(
            "tests.tools.render_probes.render_github", _github_render_with_canary
        )
    else:

        class Client:
            def render(self, text: str, project: str) -> str:
                return _fake_gitlab_render(text)

        monkeypatch.setattr(
            "tests.tools.render_probes._client_from_environment", lambda *args: Client()
        )
        monkeypatch.setattr(
            "tests.tools.render_probes._gitlab_version", lambda client: "19.4.1 ce"
        )
        monkeypatch.setattr(
            "tests.tools.render_probes._gitlab_blob_prefix",
            lambda client, project: "http://gitlab.test/-/blob/main/",
        )
    argv = ["record", "--platform", platform]
    source = path.read_bytes()
    assert main([*argv, "--check"]) == 1
    output = capsys.readouterr().out
    assert "probe_row" in output
    assert "stale divergence: probe_row" in output
    assert path.read_bytes() == source
    assert main(argv) == 0
    assert (
        "cleared divergence: probe_row: issue 12: old note" in capsys.readouterr().out
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

    monkeypatch.setattr("tests.tools.render_probes._fixture_path", lambda: "unused")
    monkeypatch.setattr("tests.tools.render_probes._read_cases", lambda path: cases)
    monkeypatch.setattr(
        "tests.tools.render_probes._client_from_environment", fake_client
    )
    monkeypatch.setattr("tests.tools.render_probes.seed", fake_seed)
    assert main(["seed", "--token-env", "FAKE_ENV"]) == 0
    assert capsys.readouterr().out.strip() == "seeded 1 users and 2 groups"


@pytest.mark.parametrize("platform", ["gitlab", "github"])
def test_equal_structure_divergence_clears_with_matching_hash(
    tmp_path, platform: str
) -> None:
    path = tmp_path / "fixture.json"
    _write_divergent_probe_fixture(path, github_html="<p>b</p>")
    document = json.loads(_read_fixture(path))
    document["cases"][0]["gitlab_probe"]["divergence"]["pair_sha256"] = pair_sha256(
        "<p>b</p>", "<p>b</p>"
    )
    write_utf8(str(path), serialize_fixture(document["cases"]))
    original = path.read_bytes()
    render = (
        (
            lambda text: (
                '<p><a data-original="@alice" data-reference-type="user">@alice</a></p>'
                if text.startswith("@alice")
                else "<p>b</p>"
            )
        )
        if platform == "gitlab"
        else (
            lambda text: (
                '<a class="user-mention" href="/lee">@lee</a>'
                if text == "@lee"
                else "<p>b</p>"
            )
        )
    )
    options = (
        {"version": "19.4.1 ce", "blob_prefix": "http://gitlab.test/-/blob/main/"}
        if platform == "gitlab"
        else {"get_github_login": lambda: "lee"}
    )
    checked = record_fixture(
        str(path), platform=platform, render=render, check=True, **options
    )
    assert checked.changed_ids == []
    assert checked.divergence_cleared == ["probe_row"]
    assert checked.exit_code == 1
    assert path.read_bytes() == original
    recorded = record_fixture(str(path), platform=platform, render=render, **options)
    assert recorded.divergence_cleared == ["probe_row"]
    assert recorded.cleared_details == ["probe_row: issue 12: old note"]
    assert (
        json.loads(_read_fixture(path))["cases"][0]["gitlab_probe"]["divergence"]
        is None
    )


def test_divergence_subcommand_binds_current_text_difference(tmp_path) -> None:
    path = tmp_path / "fixture.json"
    _write_initial_probe_fixture(path)
    document = json.loads(_read_fixture(path))
    row = document["cases"][0]
    digest = input_sha256("\uff20alice\n\nfooter line")
    row["github_probe"]["html"] = "<p>github</p>"
    row["github_probe"]["input_sha256"] = digest
    row["gitlab_probe"] = {
        "input_sha256": digest,
        "html": "<p>gitlab</p>",
        "blob_prefix": "https://gl.test/-/blob/main/",
        "divergence": None,
    }
    write_utf8(str(path), serialize_fixture(document["cases"]))
    record_divergence(str(path), case_id="probe_row", issue=378, note="text differs")
    divergence = json.loads(_read_fixture(path))["cases"][0]["gitlab_probe"][
        "divergence"
    ]
    assert list(divergence) == ["note", "issue", "pair_sha256"]
    assert divergence == {
        "note": "text differs",
        "issue": 378,
        "pair_sha256": pair_sha256(
            "<p>github</p>", "<p>gitlab</p>", blob_prefix="https://gl.test/-/blob/main/"
        ),
    }


def test_divergence_cli_uses_fixture_path_and_stored_pair(
    tmp_path, monkeypatch
) -> None:
    path = tmp_path / "fixture.json"
    _write_initial_probe_fixture(path)
    document = json.loads(_read_fixture(path))
    row = document["cases"][0]
    digest = input_sha256("\uff20alice\n\nfooter line")
    row["github_probe"].update(html="<p>a</p>", input_sha256=digest)
    row["gitlab_probe"] = {
        "input_sha256": digest,
        "html": "<p>b</p>",
        "blob_prefix": "https://gl.test/-/blob/main/",
        "divergence": None,
    }
    write_utf8(str(path), serialize_fixture(document["cases"]))
    monkeypatch.setattr("tests.tools.render_probes._fixture_path", lambda: str(path))
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
    assert (
        json.loads(_read_fixture(path))["cases"][0]["gitlab_probe"]["divergence"][
            "issue"
        ]
        == 378
    )


@pytest.mark.parametrize(
    "failure",
    ["stale", "equal", "skeleton", "nesting", "extra", "duplicate", "issue", "note"],
)
def test_divergence_refuses_invalid_fixture_or_pair(tmp_path, failure: str) -> None:
    path = tmp_path / "fixture.json"
    _write_initial_probe_fixture(path)
    document = json.loads(_read_fixture(path))
    row = document["cases"][0]
    digest = input_sha256("\uff20alice\n\nfooter line")
    row["github_probe"]["input_sha256"] = digest
    row["github_probe"]["html"] = "<p>a</p>"
    row["gitlab_probe"] = {
        "input_sha256": digest,
        "html": "<p>b</p>",
        "blob_prefix": "https://gl.test/-/blob/main/",
        "divergence": None,
    }
    if failure == "stale":
        row["gitlab_probe"]["input_sha256"] = "0" * 64
    elif failure == "equal":
        row["gitlab_probe"]["html"] = "<p>a</p>"
    elif failure == "skeleton":
        row["gitlab_probe"]["html"] = "<h1>b</h1>"
    elif failure == "nesting":
        row["github_probe"]["html"] = "<blockquote><p>a</p></blockquote><p>b</p>"
        row["gitlab_probe"]["html"] = "<blockquote><p>a</p><p>b</p></blockquote>"
    elif failure == "extra":
        document["surprise"] = True
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
            "nesting": "skeleton",
            "extra": "only a cases",
            "duplicate": "expected one fixture row",
            "issue": "positive issue",
            "note": "nonempty note",
        }[failure],
    ):
        record_divergence(
            str(path),
            case_id="probe_row",
            issue=0 if failure == "issue" else 378,
            note="  " if failure == "note" else "text differs",
            write_text=lambda target, contents: writes.append(contents),
        )
    assert writes == []
    assert _read_fixture(path) == source


def test_record_refuses_extra_top_level_key(tmp_path) -> None:
    path = tmp_path / "fixture.json"
    _write_initial_probe_fixture(path)
    source = _read_fixture(path).replace('"cases": [', '"extra": true, "cases": [')
    write_utf8(str(path), source)
    with pytest.raises(ValueError, match="only a cases"):
        record_fixture(str(path), platform="gitlab", render=_fake_gitlab_render)
    assert _read_fixture(path) == source


def test_record_rejects_reference_in_expected_render_without_writing(tmp_path) -> None:
    path = tmp_path / "fixture.json"
    _write_initial_probe_fixture(path)
    source = _read_fixture(path)
    writes: list[str] = []

    def bad_render(text: str) -> str:
        return _fake_gitlab_render("@alice")

    with pytest.raises(ValueError, match="GitLab reference link"):
        record_fixture(
            str(path),
            platform="gitlab",
            render=bad_render,
            version="19.4.1 ce",
            blob_prefix="https://gl.test/-/blob/main/",
            write_text=lambda target, contents: writes.append(contents),
        )
    assert writes == []
    assert _read_fixture(path) == source


def test_gitlab_version_detects_enterprise_and_ee_suffix() -> None:
    class Fake:
        def __init__(self, details):
            self.details = details

        def json_request(self, method: str, path: str, body: object = None):
            return self.details

    assert (
        _gitlab_version(Fake({"version": "19.4.1", "enterprise": True})) == "19.4.1 ee"
    )
    assert (
        _gitlab_version(Fake({"version": "19.4.1-ee", "enterprise": False}))
        == "19.4.1 ee"
    )


def test_twin_references_are_sorted_from_unsorted_html() -> None:
    html_text = '<a data-reference-type="user" data-original="@z"></a><a data-reference-type="user" data-original="@a"></a>'
    assert _reference_originals(html_text) == ["@a", "@z"]


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

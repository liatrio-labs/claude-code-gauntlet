from __future__ import annotations

import base64
import io
import json
import os
import re
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from tests.tools.probe_cli import main
from tests.tools.probe_fixtures import (
    read_utf8,
    record_divergence,
    record_fixture,
    serialize_fixture,
    write_utf8,
)
from tests.tools.probe_github import render_github
from tests.tools.probe_gitlab import (
    GitLabClient,
    GitLabHTTPError,
    _gitlab_blob_prefix,
    _gitlab_version,
    run_canary,
    seed,
)
from tests.tools.probe_html import check_render, structure

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


HTML_NORMALIZATION_CASES = {
    "github-chrome": (
        "github",
        '<div class="highlight highlight-text-adblock"><pre>x</pre></div>'
        "<markdown-accessiblity-table><table></table></markdown-accessiblity-table>",
        "<pre>x</pre><table></table>",
    ),
    "gitlab-chrome": (
        "gitlab",
        REAL_HEADING_HTML + REAL_CODE_HTML,
        "<h2>`x Heading</h2><p>&lt;b&gt; \uff20leehopper`</p><p>footer line</p>"
        "<pre>@user &lt;table&gt; @</pre> <p>footer line</p>",
    ),
}

RENDER_CONTAINMENT_CASES = {
    "gitlab-safe": ("gitlab", "<p><strong>safe</strong> <code>text</code></p>", None),
    "github-safe": ("github", "<p><strong>safe</strong> <code>text</code></p>", None),
    "gitlab-reference-nested": (
        "gitlab",
        '<p><a data-reference-type="user">@user</a></p>',
        "GitLab reference link",
    ),
    "github-team": (
        "github",
        '<a class="team-mention">@team</a>',
        "GitHub mention anchor",
    ),
    "gitlab-comment": ("gitlab", "<!-- retained -->", "comment node"),
    "gitlab-unknown": ("gitlab", "<abbr>HTML</abbr>", "unknown element <abbr>"),
}


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="the fake docker is a shebang script, which CreateProcess cannot run",
)
def test_script_entrypoint_imports_composed_builder_from_outside_repo(
    tmp_path: Path,
) -> None:
    repo_root = Path(__file__).resolve().parents[1]
    script = repo_root / "tests" / "tools" / "render_probes.py"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    capture = tmp_path / "runner.rb"
    fake_docker = fake_bin / "docker"
    fake_docker.write_text(
        f"#!{sys.executable}\n"
        "from pathlib import Path\n"
        "import os\n"
        "import sys\n"
        "Path(os.environ['QUICK_ACTION_RUNNER_CAPTURE']).write_text("
        "sys.stdin.read(), encoding='utf-8')\n"
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
        cwd=tmp_path,
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
    cases: list[dict[str, Any]] = json.loads(
        base64.b64decode(encoded_cases.group(1)).decode("utf-8")
    )
    assert any(case["id"] == "composed:mbq_backtick" for case in cases)


@pytest.mark.parametrize(
    ("platform", "rendered", "expected"),
    HTML_NORMALIZATION_CASES.values(),
    ids=HTML_NORMALIZATION_CASES,
)
def test_html_normalization(platform, rendered, expected) -> None:
    assert structure(rendered, platform=platform) == expected
    check_render(platform, rendered)
    assert structure('<p onclick="run()">x</p>', platform=platform) != "<p>x</p>"


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


@pytest.mark.parametrize("valid", [True, False])
def test_gitlab_blob_prefix(valid, monkeypatch) -> None:
    client = GitLabClient("http://gitlab.test", "fake-token")
    replies = {
        "/api/v4/version": {"version": "19.4.1", "enterprise": True},
        "/api/v4/projects/team%2Fprobe": {
            "web_url": "https://gitlab.test/team/probe/",
            "default_branch": "develop",
        }
        if valid
        else {},
    }
    monkeypatch.setattr(client, "json_request", lambda method, path: replies[path])
    if valid:
        assert _gitlab_version(client) == "19.4.1 ee"
        assert _gitlab_blob_prefix(client, "team/probe") == (
            "https://gitlab.test/team/probe/-/blob/develop/"
        )
    else:
        with pytest.raises(ValueError, match="metadata is incomplete"):
            _gitlab_blob_prefix(client, "team/probe")


@pytest.mark.parametrize("resolved", [True, False])
def test_gitlab_canary(resolved) -> None:
    def render(text):
        return (
            '<a data-reference-type="user" data-original="@alice">@alice</a>'
            if resolved
            else text
        )

    if resolved:
        run_canary(["alice", "all", "x"], render)
    else:
        with pytest.raises(RuntimeError, match="unresolved handles: alice"):
            run_canary(["alice", "all", "x"], render)


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


def test_github_adapter_passes_expected_argv_to_injected_runner() -> None:
    def runner(argv, **kwargs):
        assert argv == [
            "gh",
            "api",
            "markdown",
            "-f",
            "mode=gfm",
            "-f",
            "text=expected text",
        ]
        assert kwargs == {
            "check": True,
            "capture_output": True,
            "text": True,
            "encoding": "utf-8",
        }
        return subprocess.CompletedProcess(argv, 0, stdout="<p>rendered</p>")

    assert render_github("expected text", runner=runner) == "<p>rendered</p>"


DIGEST = "279c65eeaeb5f4b7eb20b33893b9879c12b5a6f6c366e95ce5f6e104c4e179cc"


PREFIX = "http://gitlab.test/-/blob/main/"


PAIR = "aa6eb1a0172a201782cdb664713ced8fe19b43943230740efa6ceb90f81107d6"


def _probe_rows():
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
                "html": "<p>a</p>",
            },
            "gitlab_probe": {
                "renderer": "gitlab api markdown gfm=true project",
                "version": "19.4.1 ce",
                "rendered": "2026-01-01",
                "input": "expected + blank line + footer line",
                "input_sha256": DIGEST,
                "blob_prefix": PREFIX,
                "twin_references": ["@alice"],
                "html": "<p>b</p>",
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


@pytest.mark.parametrize("stage", ["fixture", "canary", "containment"])
def test_record_rejects_before_write(probe_file, stage) -> None:
    if stage == "fixture":
        write_utf8(str(probe_file), '{"cases": [], "extra": true}')
    before = probe_file.read_bytes()
    html = (
        '<p><a data-reference-type="user" data-original="@alice">@alice</a></p>'
        if stage == "containment"
        else "<p>@lee</p>"
    )
    writes = []
    error_type, message = {
        "fixture": (ValueError, "only a cases array"),
        "canary": (RuntimeError, "unresolved login: lee"),
        "containment": (ValueError, "GitLab reference link"),
    }[stage]
    with pytest.raises(error_type, match=message):
        record_fixture(
            str(probe_file),
            platform="gitlab" if stage == "containment" else "github",
            render=lambda text: html,
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
    document: dict[str, Any] = json.loads(read_utf8(str(path)))
    assert document["cases"][0]["gitlab_probe"]["divergence"] is None


def test_main_github_check_preserves_and_record_stores(
    probe_file: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = probe_file
    monkeypatch.setattr("tests.tools.probe_cli._fixture_path", lambda: str(path))
    monkeypatch.setattr("tests.tools.probe_cli.github_login", lambda: "lee")
    monkeypatch.setattr(
        "tests.tools.probe_cli.render_github", _renderer("github", "<p>a</p>")
    )
    source = path.read_bytes()
    assert main(["record", "--platform", "github", "--check"]) == 0
    assert capsys.readouterr().out == ""
    assert path.read_bytes() == source
    monkeypatch.setattr(
        "tests.tools.probe_cli.render_github", _renderer("github", "<p>b</p>")
    )
    assert main(["record", "--platform", "github"]) == 0
    document: dict[str, Any] = json.loads(read_utf8(str(path)))
    assert document["cases"][0]["github_probe"]["html"] == "<p>b</p>"


@pytest.mark.parametrize(
    "failure",
    ["stale", "skeleton"],
)
def test_divergence_refuses_invalid_fixture_or_pair(probe_file, failure: str) -> None:
    path = probe_file
    document = {"cases": _probe_rows()}
    row = document["cases"][0]
    if failure == "stale":
        row["gitlab_probe"]["input_sha256"] = "0" * 64
    elif failure == "skeleton":
        row["gitlab_probe"]["html"] = "<h1>b</h1>"
    source = json.dumps(document, ensure_ascii=False)
    write_utf8(str(path), source)
    writes: list[str] = []
    with pytest.raises(
        ValueError,
        match={
            "stale": "stale",
            "skeleton": "skeleton",
        }[failure],
    ):
        record_divergence(
            str(path),
            case_id="probe_row",
            issue=378,
            note="text differs",
            write_text=lambda target, contents: writes.append(contents),
        )
    assert writes == []
    assert read_utf8(str(path)) == source


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
    document: dict[str, Any] = json.loads(read_utf8(str(probe_file)))
    assert document["cases"][0]["gitlab_probe"]["divergence"] == {
        "note": "text differs",
        "issue": 378,
        "pair_sha256": PAIR,
    }


def test_seed_smoke() -> None:
    groups = {
        name: {"id": index}
        for index, name in enumerate(("cdr-group", "team", "types", "types/node"), 1)
    }
    transcript: list[tuple[str, str, object]] = []
    for name in groups:
        transcript.append(("GET", "/api/v4/groups/" + name.replace("/", "%2F"), None))
        if name == "types/node":
            transcript.append(("GET", "/api/v4/groups/types", groups["types"]))
        transcript.append(("POST", "/api/v4/groups", groups[name]))
    transcript.extend(
        [
            ("GET", "/api/v4/users?username=alice", []),
            ("POST", "/api/v4/users", {"id": 5, "username": "alice"}),
            ("GET", "/api/v4/projects/cdr-group%2Fprobe", None),
            ("POST", "/api/v4/projects", {"id": 6}),
        ]
    )
    for resource in ("groups/1", "groups/2", "projects/6"):
        transcript.extend(
            [
                ("GET", f"/api/v4/{resource}/members/5", None),
                ("POST", f"/api/v4/{resource}/members", {"id": 5}),
            ]
        )
    requests = iter(transcript)

    class Client:
        def json_request(self, method, path, body=None):
            expected_method, expected_path, response = next(requests)
            assert (method, path) == (expected_method, expected_path)
            if method == "POST" and path.endswith("/members"):
                assert body == {"user_id": 5, "access_level": 30}
            if method == "POST" and path == "/api/v4/projects":
                assert body == {
                    "name": "probe",
                    "path": "probe",
                    "namespace_id": 1,
                    "initialize_with_readme": True,
                }
            if method == "POST" and body.get("path") == "node":
                assert body["parent_id"] == 3
            return response

    result = seed(
        [{"input": "@alice @types/node @team @all @x", "expected": ""}], Client()
    )
    assert result == {
        "groups": groups,
        "users": {"alice": {"id": 5, "username": "alice"}},
        "project": {"id": 6},
    }
    assert next(requests, None) is None

from __future__ import annotations

import html
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from tests.tools import outbound_canary as canary
from tests.tools.probe_html import _Node, is_github_mention

ROOT = Path(__file__).resolve().parents[1]
POSITIVE = '<a class="notranslate user-mention" href="https://github.com/lee">@lee</a>'


@pytest.fixture
def offline_live(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)


@pytest.mark.parametrize(
    ("index", "expected"),
    [
        pytest.param(
            0,
            "@lee\n&lt;b&gt;OUTBOUND_PROBE_HTML_0&lt;/b&gt;\n```\n<b>OUTBOUND_PROBE_HTML_0</b>\n```\n<b>OUTBOUND_PROBE_HTML_0</b>",
            id="literal-mention-first",
        ),
        pytest.param(
            1,
            "&#64;lee &commat;lee\n&lt;b&gt;OUTBOUND_PROBE_HTML_1&lt;/b&gt;\n```\n<b>OUTBOUND_PROBE_HTML_1</b>\n```\n<b>OUTBOUND_PROBE_HTML_1</b>",
            id="numeric-and-named-entities",
        ),
        pytest.param(
            2,
            "&amp;#64;lee \uff20lee\n&lt;b&gt;OUTBOUND_PROBE_HTML_2&lt;/b&gt;\n```\n<b>OUTBOUND_PROBE_HTML_2</b>\n```\n<b>OUTBOUND_PROBE_HTML_2</b>",
            id="double-entity-and-fullwidth",
        ),
    ],
)
def test_generator_prefix(index: int, expected: str) -> None:
    assert canary.GENERATOR_VERSION == 1
    assert canary.generate_cases(375, "lee", 3)[index] == expected


def test_generator_reproducibility() -> None:
    first = canary.generate_cases(375, "lee", 40)
    assert first == canary.generate_cases(375, "lee", 40)
    assert first[:15] == canary.generate_cases(375, "lee", 15)
    assert first[13:] != canary.generate_cases(376, "lee", 40)[13:]


@pytest.mark.parametrize(
    ("tag", "classes", "expected"),
    [
        pytest.param(
            "a", "other user-mention notranslate", True, id="user-extra-classes"
        ),
        pytest.param("a", "other team-mention", True, id="team-extra-classes"),
        pytest.param("a", "user-mentions", False, id="class-boundary"),
        pytest.param("span", "user-mention", False, id="anchor-only"),
        pytest.param("a", "", False, id="ordinary-link"),
    ],
)
def test_mention_predicate(tag: str, classes: str, expected: bool) -> None:
    assert is_github_mention(_Node(tag, (("class", classes),))) is expected


@pytest.mark.parametrize(
    ("original", "rendered", "preparation", "expected"),
    [
        pytest.param(
            "safe",
            "<p>&lt;b&gt;OUTBOUND_PROBE_HTML_0&lt;/b&gt;</p>",
            "identity",
            None,
            id="escaped-sentinel",
        ),
        pytest.param(
            "safe",
            "<strong>OUTBOUND_PROBE_HTML_0</strong><h3>ok</h3><hr>",
            "identity",
            None,
            id="ordinary-markdown-not-fixture-vocabulary",
        ),
        pytest.param(
            "safe",
            '<a class="extra user-mention">@lee</a>',
            "identity",
            "mention",
            id="user-mention",
        ),
        pytest.param("safe", "<b>other</b>", "identity", None, id="other-bold"),
        pytest.param(
            "safe",
            "<b>OUTBOUND_PROBE_HTML_0</b>",
            "identity",
            "raw-html",
            id="raw-sentinel-without-attributes",
        ),
        pytest.param(
            "safe",
            "<b><span>OUTBOUND_PROBE_HTML_0</span></b>",
            "identity",
            "raw-html",
            id="sentinel-descendant",
        ),
        pytest.param(
            "<!-- code-gauntlet-findings: {} -->",
            "",
            "identity",
            "marker",
            id="current-summary-marker",
        ),
        pytest.param(
            '<!-- code-gauntlet-finding-key: {"sha":"abcdef0","key":"aaaaaaaaaaaaaaaa"} -->',
            "",
            "identity",
            "marker",
            id="finding-marker",
        ),
        pytest.param(
            "&lt;!-- code-gauntlet-findings: {} --&gt;",
            "",
            "identity",
            None,
            id="displayed-marker-is-safe",
        ),
        pytest.param("safe", "", "append", "fixpoint", id="exact-fixpoint"),
        pytest.param("safe", "RAISE", "identity", "renderer", id="renderer-failure"),
        pytest.param("safe", "", "raise", "preparer", id="preparation-failure"),
    ],
)
def test_checker(
    original: str, rendered: str, preparation: str, expected: str | None
) -> None:
    submissions: list[str] = []

    def prepare(text: str) -> str:
        if preparation == "raise":
            raise ValueError("preparation unavailable")
        return text + "!" if preparation == "append" else text

    def render(text: str) -> str:
        submissions.append(text)
        if rendered == "RAISE":
            raise RuntimeError("renderer unavailable")
        return rendered

    failure = canary.check_case(original, 0, prepare, render)
    assert (failure.property if failure is not None else None) == expected
    assert (failure.second if failure is not None else None) == (
        "safe!!" if expected == "fixpoint" else None
    )
    assert (failure.error if failure is not None else None) == (
        "renderer unavailable"
        if expected == "renderer"
        else "preparation unavailable"
        if expected == "preparer"
        else None
    )
    assert submissions == (
        [] if expected in {"marker", "fixpoint", "preparer"} else [original]
    )


@pytest.mark.parametrize(
    ("arguments", "environment", "message"),
    [
        pytest.param([], {}, "--live is required", id="live-opt-in-required"),
        pytest.param(
            ["--live"], {"GITHUB_ACTIONS": "true"}, "refuses CI", id="actions-refusal"
        ),
        pytest.param(["--live"], {"CI": "0"}, "refuses CI", id="any-nonempty-ci-value"),
        pytest.param(
            ["--live", "--cases", "0"], {}, "--cases must", id="nonpositive-count"
        ),
        pytest.param(
            ["--live", "--cases", "257"], {}, "--cases must", id="bounded-count"
        ),
        pytest.param(
            ["--live", "--case-index", "-1"],
            {},
            "--case-index must",
            id="negative-index",
        ),
        pytest.param(
            ["--live", "--cases", "1", "--case-index", "1"],
            {},
            "--case-index must",
            id="index-outside-count",
        ),
        pytest.param(
            ["--live", "--mention", "@lee"], {}, "--mention must", id="invalid-mention"
        ),
        pytest.param(
            ["--live", "--mention", "org/team"],
            {},
            "--mention must",
            id="team-mention",
        ),
        pytest.param(
            ["--live", "--mention", ""], {}, "--mention must", id="empty-mention"
        ),
    ],
)
def test_cli_refuses_before_transport(
    arguments: list[str],
    environment: dict[str, str],
    message: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    for name in ("CI", "GITHUB_ACTIONS"):
        monkeypatch.delenv(name, raising=False)
    for name, value in environment.items():
        monkeypatch.setenv(name, value)
    calls: list[str] = []

    def forbidden(text: str = "login") -> str:
        calls.append(text)
        raise AssertionError("transport must not run")

    try:
        code = canary.main(arguments, render=forbidden, login=forbidden)
    except SystemExit as exc:
        assert isinstance(exc.code, int)
        code = exc.code
    assert code == 2
    assert calls == []
    captured = capsys.readouterr()
    assert message in captured.err
    if not arguments:
        assert "usage:" in captured.out


@pytest.mark.parametrize(
    ("response", "mention", "expected_calls", "expected_replay"),
    [
        pytest.param(
            "<p>@lee</p>",
            None,
            ["@lee"],
            "Replay: python3 tests/tools/outbound_canary.py --live --seed 375 --cases 1 --mention lee",
            id="plain-preflight",
        ),
        pytest.param(
            POSITIVE,
            "ghost",
            ["@ghost"],
            "Replay: python3 tests/tools/outbound_canary.py --live --seed 375 --cases 1 --mention ghost",
            id="different-mentioned-account",
        ),
    ],
)
def test_preflight_failure(
    response: str,
    mention: str | None,
    expected_calls: list[str],
    expected_replay: str,
    offline_live: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls: list[str] = []

    def login() -> str:
        return "lee"

    def render(text: str) -> str:
        calls.append(text)
        return response

    arguments = ["--live", "--cases", "1"]
    if mention is not None:
        arguments.extend(["--mention", mention])
    assert canary.main(arguments, login=login, render=render) == 1
    lines = capsys.readouterr().out.splitlines()
    diagnostic: dict[str, object] = json.loads(lines[0])
    assert diagnostic["property"] == "preflight"
    assert calls == expected_calls
    assert lines[1] == expected_replay


def test_preflight_missing_gh(
    offline_live: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from tests.tools import probe_github

    def unavailable_login() -> str:
        raise FileNotFoundError("gh unavailable")

    def forbidden_render(text: str) -> str:
        raise AssertionError("transport must not run")

    monkeypatch.setattr(probe_github, "github_login", unavailable_login)
    monkeypatch.setattr(probe_github, "render_github", forbidden_render)
    assert canary.main(["--live"]) == 1
    lines = capsys.readouterr().out.splitlines()
    diagnostic: dict[str, object] = json.loads(lines[0])
    assert diagnostic["property"] == "preflight"
    assert diagnostic["error"] == "gh unavailable"
    assert (
        lines[1]
        == "Replay: python3 tests/tools/outbound_canary.py --live --seed 375 --cases 64"
    )


def test_pipeline_submits_both_actual_preparations(
    offline_live: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls: list[str] = []

    def inputs(seed: int, mention: str, count: int) -> list[str]:
        assert (seed, mention, count) == (375, "lee", 2)
        return ["@lee\n<b>OUTBOUND_PROBE_HTML_0</b>", "second"]

    def render(text: str) -> str:
        calls.append(text)
        return POSITIVE if text == "@lee" else "<p>" + html.escape(text) + "</p>"

    monkeypatch.setattr(canary, "generate_cases", inputs)
    assert (
        canary.main(["--live", "--cases", "2"], login=lambda: "lee", render=render) == 0
    )
    assert calls == [
        "@lee",
        "\uff20lee\n&lt;b>OUTBOUND_PROBE_HTML_0&lt;/b>",
        "\uff20lee &lt;b>OUTBOUND_PROBE_HTML_0&lt;/b>",
        "second",
        "second",
    ]
    assert (
        capsys.readouterr().out
        == "Passed 2 cases (both operations); seed=375 version=1 mention=lee\n"
    )


def test_failure_diagnostic_and_replay(
    offline_live: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls: list[str] = []

    def render(text: str) -> str:
        calls.append(text)
        return POSITIVE

    arguments = [
        "--live",
        "--seed",
        "375",
        "--cases",
        "3",
        "--mention",
        "lee",
        "--case-index",
        "2",
    ]
    assert canary.main(arguments, login=lambda: "lee", render=render) == 1
    output = capsys.readouterr().out
    assert output.isascii()
    lines = output.splitlines()
    diagnostic: dict[str, object] = json.loads(lines[0])
    assert diagnostic == {
        "generator_version": 1,
        "seed": 375,
        "handle": "lee",
        "index": 2,
        "operation": "prose",
        "property": "mention",
        "original": "&amp;#64;lee \uff20lee\n&lt;b&gt;OUTBOUND_PROBE_HTML_2&lt;/b&gt;\n```\n<b>OUTBOUND_PROBE_HTML_2</b>\n```\n<b>OUTBOUND_PROBE_HTML_2</b>",
        "prepared": "&amp;#64;lee \uff20lee\n&lt;b&gt;OUTBOUND_PROBE_HTML_2&lt;/b&gt;\n```\n<b>OUTBOUND_PROBE_HTML_2</b>\n```\n&lt;b>OUTBOUND_PROBE_HTML_2&lt;/b>",
        "second": None,
        "html": POSITIVE,
        "structure": '<a class="notranslate user-mention" href="https://github.com/lee">@lee</a>',
        "error": None,
    }
    assert len(calls) == 2
    assert (
        lines[1]
        == "Replay: python3 tests/tools/outbound_canary.py --live --seed 375 --cases 3 --mention lee --case-index 2"
    )
    replay = shlex.split(lines[1].removeprefix("Replay: "))[2:]
    assert canary.main(replay, login=lambda: "lee", render=render) == 1
    assert capsys.readouterr().out == output


def test_main_diagnostic_serializes_second(
    offline_live: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import gauntlet.text

    def prepare_non_idempotently(text: str) -> str:
        return text + "!"

    def login() -> str:
        return "lee"

    def render(text: str) -> str:
        if text == "@lee":
            return POSITIVE
        return "<p>" + html.escape(text) + "</p>"

    monkeypatch.setattr(gauntlet.text, "prepare_prose", prepare_non_idempotently)
    assert canary.main(["--live", "--cases", "1"], login=login, render=render) == 1
    diagnostic: dict[str, object] = json.loads(capsys.readouterr().out.splitlines()[0])
    assert diagnostic["property"] == "fixpoint"
    assert diagnostic["second"] is not None


def test_entry_from_foreign_cwd_without_live(tmp_path: Path) -> None:
    environment = {
        key: value for key, value in os.environ.items() if key != "PYTHONPATH"
    }
    result = subprocess.run(
        [sys.executable, str(ROOT / "tests/tools/outbound_canary.py")],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert result.returncode == 2
    assert "--live is required" in result.stderr
    assert "ModuleNotFoundError" not in result.stderr
    assert "usage:" in result.stdout


def test_documented_canary_command() -> None:
    subsection = (
        (ROOT / "CONTRIBUTING.md")
        .read_text(encoding="utf-8")
        .split("### Optional outbound canary\n", 1)[1]
        .split("\n## ", 1)[0]
    )
    command = subsection.split("```bash\n", 1)[1].split("\n```", 1)[0]
    words = shlex.split(command)
    assert words[:2] == ["python3", "tests/tools/outbound_canary.py"]
    args = canary.build_argument_parser().parse_args(words[2:])
    assert vars(args) == {
        "live": True,
        "seed": 375,
        "cases": 64,
        "mention": None,
        "case_index": None,
    }

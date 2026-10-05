"""Boundary tests for the maintainer marketplace command."""

import importlib.util
import json
import sys
from functools import partial
from pathlib import Path
from types import SimpleNamespace

import pytest
from gauntlet.cli import Command

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "marketplace_bump", ROOT / ".github" / "marketplace_bump.py"
)
assert SPEC is not None and SPEC.loader is not None
marketplace_bump = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = marketplace_bump
SPEC.loader.exec_module(marketplace_bump)
MODULE = marketplace_bump

TAG = "v1.2.3"
SHA = "a" * 40
SOURCE_REPO = "liatrio-labs/claude-code-gauntlet"
MARKET_REPO = "liatrio-labs/claude-plugins"
SOURCE_URL = f"https://github.com/{SOURCE_REPO}.git"
RELEASE_ARGV = (
    "gh",
    "release",
    "view",
    "--repo",
    "owner/repo",
    "--json",
    "tagName",
)
ISSUE_LIST_ARGV = (
    "gh",
    "issue",
    "list",
    "--repo",
    "owner/repo",
    "--state",
    "all",
    "--author",
    "app/github-actions",
    "--search",
    'in:title "Marketplace bump due" sort:created-desc',
    "--limit",
    "100",
    "--json",
    "number,title,state",
)
MARKETPLACE_API_ARGV = (
    "gh",
    "api",
    "-H",
    "Accept: application/vnd.github.raw",
    f"repos/{MARKET_REPO}/contents/.claude-plugin/marketplace.json",
)


def manifest(version="1.2.3", **overrides):
    value = {"version": version, "description": "A plugin", "keywords": ["review"]}
    value.update(overrides)
    return value


def entry(url=SOURCE_URL, **overrides):
    value = {
        "name": "code-gauntlet",
        "version": "1.2.2",
        "description": "Old description",
        "author": {"name": "Liatrio"},
        "keywords": ["old"],
        "source": {"source": "url", "url": url, "sha": "b" * 40},
        "strict": True,
    }
    value.update(overrides)
    return value


def marketplace(*plugins):
    return {"plugins": list(plugins), "unrelated": {"keep": True}}


def release_responses(manifest_document=None, sha=SHA):
    manifest_document = manifest_document or manifest()
    return {
        ("git", "rev-parse", f"{TAG}^{{commit}}"): sha + "\n",
        ("git", "show", f"{TAG}:.claude-plugin/plugin.json"): json.dumps(
            manifest_document, ensure_ascii=False
        ),
    }


class FakeRunner:
    def __init__(
        self, responses=None, clone_bytes=None, persistent_root=None, create_clone=True
    ):
        self.responses = dict(responses or {})
        self.clone_bytes = clone_bytes
        self.persistent_root = persistent_root
        self.create_clone = create_clone
        self.command_output = ""
        self.calls = []
        self.clone_path = None

    def __call__(self, argv, cwd=None):
        self.calls.append((list(argv), cwd))
        key = tuple(argv)
        absent = object()
        response = self.responses.get(key, absent)
        if response is absent and argv[:3] == ["gh", "issue", "list"]:
            response = next(
                (
                    value
                    for candidate, value in self.responses.items()
                    if candidate[:3] == ("gh", "issue", "list")
                ),
                absent,
            )
        if response is not absent:
            if isinstance(response, BaseException):
                raise response
            if argv[:3] == ["gh", "issue", "list"] and "--author" in argv:
                # Model gh's author filter so the CLI proves bot provenance at its boundary.
                try:
                    issues = json.loads(response)
                except json.JSONDecodeError:
                    return response
                if isinstance(issues, list):
                    response = json.dumps(
                        [
                            value
                            for value in issues
                            if not isinstance(value, dict)
                            or "author" not in value
                            or value["author"]["login"] == "github-actions[bot]"
                        ]
                    )
            return response
        if argv[:3] == ["gh", "repo", "clone"]:
            if not self.create_clone:
                return ""
            clone_name = argv[3].split("/", 1)[1]
            if self.persistent_root is None:
                self.clone_path = Path(cwd) / clone_name
            else:
                self.clone_path = self.persistent_root / clone_name
                self.clone_path.mkdir(parents=True)
                (Path(cwd) / clone_name).symlink_to(
                    self.clone_path, target_is_directory=True
                )
            manifest_path = self.clone_path / ".claude-plugin" / "marketplace.json"
            manifest_path.parent.mkdir(parents=True)
            manifest_path.write_bytes(self.clone_bytes or b"{}\n")
            return ""
        if argv[:3] == ["gh", "pr", "create"]:
            return self.command_output
        if argv[:3] in (["gh", "issue", "create"], ["gh", "issue", "edit"]):
            return self.command_output
        return ""


def open_runner(document, release_manifest=None, sha=SHA, persistent_root=None):
    data = json.dumps(document, ensure_ascii=False, indent=2).encode() + b"\n"
    return FakeRunner(release_responses(release_manifest, sha), data, persistent_root)


def check_runner(marketplace_response):
    responses = release_responses()
    responses[MARKETPLACE_API_ARGV] = (
        marketplace_response
        if isinstance(marketplace_response, str)
        else json.dumps(marketplace_response)
    )
    return FakeRunner(responses)


def issue(number, tag, state="OPEN", author=None):
    value = {
        "number": number,
        "title": f"Marketplace bump due: {tag}",
        "state": state,
    }
    if author is not None:
        value["author"] = {"login": author}
    return value


def remind_runner(issues, tag=TAG):
    return FakeRunner(
        {
            RELEASE_ARGV: json.dumps({"tagName": tag}),
            ISSUE_LIST_ARGV: json.dumps(issues),
        }
    )


def remind_runner_with_payload(payload, tag=TAG):
    runner = remind_runner([])
    runner.responses[ISSUE_LIST_ARGV] = payload
    runner.responses[RELEASE_ARGV] = json.dumps({"tagName": tag})
    return runner


@pytest.mark.parametrize(
    ("argv", "code"),
    [
        (["check", "--tag", TAG], 0),
        (["check", "--tag", TAG], 1),
        (["open-pr"], 2),
        (["--help"], 0),
    ],
    ids=["success", "drift", "usage_error", "help_system_exit"],
)
def test_legacy_command_calls_main_without_arguments_and_preserves_exit_code(
    argv, code, monkeypatch
):
    target = entry(
        version="1.2.3",
        description="A plugin" if code == 0 else "Wrong",
        keywords=["review"],
        source={"url": SOURCE_URL, "sha": SHA},
    )
    runner = check_runner(marketplace(target))
    command = Command.legacy(
        partial(MODULE.main, run=runner), prog="marketplace_bump.py"
    )
    saved = ["caller.py", "unrelated"]
    monkeypatch.setattr(sys, "argv", saved)

    assert command.invoke(argv) == code
    assert sys.argv is saved
    assert len(runner.calls) == (3 if argv[0] == "check" else 0)


def test_open_pr_rewrites_only_matching_entry_and_runs_exact_sequence(capsys, tmp_path):
    second = entry("https://github.com/example/other.git", name="other")
    original = marketplace(entry(), second)
    runner = open_runner(
        original, manifest(description="Révision"), persistent_root=tmp_path
    )
    runner.command_output = "https://github.com/liatrio-labs/claude-plugins/pull/42"

    assert MODULE.main(["open-pr", "--tag", TAG], run=runner) == 0

    expected = marketplace(
        {
            **entry(),
            "version": "1.2.3",
            "description": "Révision",
            "keywords": ["review"],
            "source": {"source": "url", "url": SOURCE_URL, "sha": SHA},
        },
        second,
    )
    assert (runner.clone_path / ".claude-plugin/marketplace.json").read_bytes() == (
        json.dumps(expected, ensure_ascii=False, indent=2) + "\n"
    ).encode()
    title = f"fix(plugins): update {SOURCE_REPO} to {TAG}"
    body = (
        f"Update the marketplace entry to upstream release [{TAG}]"
        f"(https://github.com/{SOURCE_REPO}/releases/tag/{TAG})."
    )
    assert [argv for argv, _ in runner.calls] == [
        ["git", "rev-parse", f"{TAG}^{{commit}}"],
        ["git", "show", f"{TAG}:.claude-plugin/plugin.json"],
        ["gh", "repo", "clone", MARKET_REPO],
        ["git", "switch", "--create", f"bump-marketplace/claude-code-gauntlet-{TAG}"],
        ["git", "add", "--", ".claude-plugin/marketplace.json"],
        ["git", "commit", "-m", title],
        [
            "git",
            "push",
            "--set-upstream",
            "origin",
            f"bump-marketplace/claude-code-gauntlet-{TAG}",
        ],
        [
            "gh",
            "pr",
            "create",
            "--repo",
            MARKET_REPO,
            "--base",
            "main",
            "--title",
            title,
            "--body",
            body,
        ],
    ]
    assert [cwd for _, cwd in runner.calls[:2]] == [str(ROOT), str(ROOT)]
    assert Path(runner.calls[2][1]) / "claude-plugins" == Path(runner.calls[3][1])
    assert all(cwd == runner.calls[3][1] for _, cwd in runner.calls[3:])
    assert capsys.readouterr().out.strip() == (
        "https://github.com/liatrio-labs/claude-plugins/pull/42"
    )


def test_open_pr_already_current_preserves_file_bytes_and_creates_no_branch(
    capsys, tmp_path
):
    raw = (
        f'{{\n  "plugins": [ {{ "name": "café", "version": "1.2.3", '
        f'"description": "A plugin", "author": {{"name":"Liatrio"}}, '
        f'"keywords": ["review"], "source": {{"source":"url", '
        f'"url":"{SOURCE_URL}", "sha":"{SHA}"}}, "strict": true }} ],\n'
        '  "other": "✓"\n}\n'
    ).encode()
    runner = FakeRunner(release_responses(), raw, tmp_path)

    assert MODULE.main(["open-pr", "--tag", TAG], run=runner) == 0

    assert runner.clone_path is not None
    assert (runner.clone_path / ".claude-plugin/marketplace.json").read_bytes() == raw
    assert not any(argv[:2] == ["git", "switch"] for argv, _ in runner.calls)
    assert (
        capsys.readouterr().out.strip() == "The marketplace entry is already current."
    )


def test_open_pr_refuses_manifest_version_that_differs_from_tag(capsys):
    runner = FakeRunner(
        release_responses(manifest(version="1.2.2")),
        json.dumps(marketplace(entry())).encode(),
    )

    assert MODULE.main(["open-pr", "--tag", TAG], run=runner) == 2

    assert [argv for argv, _ in runner.calls] == [
        ["git", "rev-parse", f"{TAG}^{{commit}}"],
        ["git", "show", f"{TAG}:.claude-plugin/plugin.json"],
    ]
    error = capsys.readouterr().err
    assert "does not match tag" in error
    assert len(error.splitlines()) == 1


def test_check_accepts_matching_entry():
    document = marketplace(
        {
            **entry(),
            "version": "1.2.3",
            "description": "A plugin",
            "keywords": ["review"],
            "source": {"source": "url", "url": SOURCE_URL, "sha": SHA},
        }
    )
    responses = release_responses()
    responses[
        (
            "gh",
            "api",
            "-H",
            "Accept: application/vnd.github.raw",
            f"repos/{MARKET_REPO}/contents/.claude-plugin/marketplace.json",
        )
    ] = json.dumps(document)
    runner = FakeRunner(responses)

    assert MODULE.main(["check", "--tag", TAG], run=runner) == 0
    assert [argv for argv, _ in runner.calls] == [
        ["git", "rev-parse", f"{TAG}^{{commit}}"],
        ["git", "show", f"{TAG}:.claude-plugin/plugin.json"],
        list(MARKETPLACE_API_ARGV),
    ]


@pytest.mark.parametrize(
    "field",
    ["version", "source.sha", "description", "keywords"],
)
def test_check_reports_drift_fields(field, capsys):
    target = {
        **entry(),
        "version": "1.2.3",
        "description": "A plugin",
        "keywords": ["review"],
        "source": {"source": "url", "url": SOURCE_URL, "sha": SHA},
    }
    if field == "source.sha":
        target["source"]["sha"] = "c" * 40
    else:
        target[field] = {"version": "9.9.9", "description": "Wrong", "keywords": []}[
            field
        ]
    responses = release_responses()
    responses[
        (
            "gh",
            "api",
            "-H",
            "Accept: application/vnd.github.raw",
            f"repos/{MARKET_REPO}/contents/.claude-plugin/marketplace.json",
        )
    ] = json.dumps(marketplace(target))
    runner = FakeRunner(responses)

    assert MODULE.main(["check", "--tag", TAG], run=runner) == 1

    assert capsys.readouterr().out.splitlines() == [f"drift: {field}"]


@pytest.mark.parametrize(
    ("issues", "action", "issue_number"),
    [
        ([], "create", None),
        ([issue(8, "v1.2.2")], "edit", 8),
        ([issue(9, "v1.2.2", "CLOSED")], "create", None),
        ([issue(10, TAG)], "none", None),
        ([issue(13, TAG, "CLOSED"), issue(12, "v1.2.2")], "none", None),
    ],
    ids=["no_issue", "open_older", "closed_older", "open_latest", "closed_latest"],
)
def test_remind_decision_table(issues, action, issue_number):
    runner = remind_runner(issues)
    if action == "edit":
        runner.command_output = "https://github.com/owner/repo/issues/42"

    assert MODULE.main(["remind", "--repo", "owner/repo"], run=runner) == 0

    issue_calls = [argv for argv, _ in runner.calls if argv[:2] == ["gh", "issue"]]
    assert runner.calls[:2] == [
        (list(RELEASE_ARGV), None),
        (list(ISSUE_LIST_ARGV), None),
    ]
    if action == "none":
        assert issue_calls == [list(ISSUE_LIST_ARGV)]
    else:
        body = (
            f"A release of claude-code-gauntlet is ready for the marketplace: `{TAG}`.\n\n"
            "Open the marketplace PR:\n\n"
            f"`python3 .github/marketplace_bump.py open-pr --tag {TAG}`\n\n"
            "Check the marketplace entry after it merges:\n\n"
            f"`python3 .github/marketplace_bump.py check --tag {TAG}`\n\n"
            "Close this issue once the marketplace PR merges."
        )
        prefix = ["gh", "issue", action]
        if action == "edit":
            prefix.append(str(issue_number))
        assert issue_calls == [
            list(ISSUE_LIST_ARGV),
            [
                *prefix,
                "--repo",
                "owner/repo",
                "--title",
                f"Marketplace bump due: {TAG}",
                "--body",
                body,
            ],
        ]


@pytest.mark.parametrize(
    "ignored",
    [issue(30, TAG, author="not-the-bot"), issue(31, f"{TAG} extra")],
    ids=["non_bot", "near_miss"],
)
def test_remind_ignores_non_bot_and_near_miss_titles_and_passes_author_filter(ignored):
    runner = remind_runner([ignored])

    assert MODULE.main(["remind", "--repo", "owner/repo"], run=runner) == 0

    list_call = next(
        argv for argv, _ in runner.calls if argv[:3] == ["gh", "issue", "list"]
    )
    assert list_call[list_call.index("--author") + 1] == "app/github-actions"
    assert any(argv[:3] == ["gh", "issue", "create"] for argv, _ in runner.calls)


def test_remind_no_release_is_silent(capsys):
    runner = FakeRunner(
        {
            (
                "gh",
                "release",
                "view",
                "--repo",
                "owner/repo",
                "--json",
                "tagName",
            ): MODULE.CommandFailure(("gh", "release", "view"), 1, "release not found")
        }
    )

    assert MODULE.main(["remind", "--repo", "owner/repo"], run=runner) == 0

    assert capsys.readouterr().out == ""


@pytest.mark.parametrize(
    ("argv", "responses", "clone_bytes"),
    [
        (["open-pr", "--tag", "1.2.3"], {}, None),
        (["check", "--tag", "v1.2"], {}, None),
        (
            ["open-pr", "--tag", TAG],
            {
                ("git", "rev-parse", f"{TAG}^{{commit}}"): SHA,
                ("git", "show", f"{TAG}:.claude-plugin/plugin.json"): "{",
            },
            None,
        ),
        (
            ["open-pr", "--tag", TAG],
            {("git", "rev-parse", f"{TAG}^{{commit}}"): "short-sha"},
            None,
        ),
    ],
)
def test_release_bad_inputs_exit_two_with_one_line(
    argv, responses, clone_bytes, capsys
):
    runner = FakeRunner(responses, clone_bytes)

    assert MODULE.main(argv, run=runner) == 2

    error = capsys.readouterr().err
    assert len(error.splitlines()) == 1
    assert "Traceback" not in error


@pytest.mark.parametrize(
    "document",
    ["{", json.dumps({"plugins": []}), json.dumps(marketplace(entry(), entry()))],
)
def test_open_pr_bad_marketplace_json_or_entry_exits_two(document, capsys):
    runner = FakeRunner(release_responses(), document.encode())

    assert MODULE.main(["open-pr", "--tag", TAG], run=runner) == 2

    error = capsys.readouterr().err
    assert len(error.splitlines()) == 1
    assert "Traceback" not in error
    assert not any(argv[:2] == ["git", "switch"] for argv, _ in runner.calls)


def test_reminder_workflow_uses_weekly_issue_reminder_and_old_publish_file_is_absent():
    workflow = (ROOT / ".github/workflows/marketplace-reminder.yml").read_text(
        encoding="utf-8"
    )

    assert "schedule:" in workflow
    assert 'cron: "0 14 * * 1"' in workflow
    assert "issues: write" in workflow
    assert (
        'python3 .github/marketplace_bump.py remind --repo "$GITHUB_REPOSITORY"'
        in workflow
    )
    assert not (ROOT / ".github/workflows/publish-marketplace.yml").exists()


def test_runner_is_the_only_subprocess_boundary(monkeypatch):
    calls = []
    results = [
        SimpleNamespace(returncode=0, stdout="sha\n", stderr=""),
        SimpleNamespace(returncode=7, stdout="", stderr="denied\n"),
    ]

    def fake_subprocess_run(argv, **kwargs):
        calls.append((argv, kwargs))
        return results.pop(0)

    monkeypatch.setattr(MODULE.subprocess, "run", fake_subprocess_run)

    assert MODULE.run_command(["git", "rev-parse", "tag"], "/repo") == "sha\n"
    with pytest.raises(MODULE.CommandFailure) as failure:
        MODULE.run_command(["gh", "api", "endpoint"])

    assert failure.value.returncode == 7
    assert failure.value.stderr == "denied"
    assert calls == [
        (
            ["git", "rev-parse", "tag"],
            {
                "cwd": "/repo",
                "capture_output": True,
                "text": True,
                "encoding": "utf-8",
                "check": False,
            },
        ),
        (
            ["gh", "api", "endpoint"],
            {
                "cwd": None,
                "capture_output": True,
                "text": True,
                "encoding": "utf-8",
                "check": False,
            },
        ),
    ]

    def missing_command(argv, **kwargs):
        raise FileNotFoundError("missing executable")

    monkeypatch.setattr(MODULE.subprocess, "run", missing_command)
    with pytest.raises(MODULE.CommandFailure) as missing:
        MODULE.run_command(["gh", "api"])
    assert missing.value.returncode == 127

    def invalid_output(argv, **kwargs):
        raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid byte")

    monkeypatch.setattr(MODULE.subprocess, "run", invalid_output)
    with pytest.raises(MODULE.InputError, match="not valid UTF-8"):
        MODULE.run_command(["gh", "api"])


def test_argument_errors_and_command_failures_are_one_line(capsys):
    assert MODULE.main(["open-pr"], run=FakeRunner()) == 2
    assert len(capsys.readouterr().err.splitlines()) == 1

    runner = FakeRunner(
        {
            ("git", "rev-parse", f"{TAG}^{{commit}}"): MODULE.CommandFailure(
                ("git", "rev-parse"), 128, "bad tag"
            )
        }
    )
    assert MODULE.main(["open-pr", "--tag", TAG], run=runner) == 2
    error = capsys.readouterr().err
    assert len(error.splitlines()) == 1
    assert "Traceback" not in error


@pytest.mark.parametrize(
    ("argv", "responses"),
    [
        (["open-pr", "--tag", TAG, "--source-repo", "bad"], {}),
        (["check", "--tag", TAG, "--marketplace-repo", "bad"], {}),
        (["remind", "--repo", "bad"], {}),
        (
            ["open-pr", "--tag", TAG],
            {("git", "show", f"{TAG}:.claude-plugin/plugin.json"): "[]"},
        ),
    ],
)
def test_invalid_repositories_and_non_object_manifest_exit_two(argv, responses, capsys):
    defaults = release_responses()
    defaults.update(responses)
    runner = FakeRunner(defaults)

    assert MODULE.main(argv, run=runner) == 2

    error = capsys.readouterr().err
    assert len(error.splitlines()) == 1
    assert "Traceback" not in error


@pytest.mark.parametrize("field", ["version", "description", "keywords"])
def test_missing_manifest_field_exits_two(field, capsys):
    document = manifest()
    del document[field]
    runner = FakeRunner(release_responses(document))

    assert MODULE.main(["open-pr", "--tag", TAG], run=runner) == 2

    assert (
        capsys.readouterr().err.strip()
        == f"release manifest is missing fields: {field}"
    )
    assert len(runner.calls) == 2


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("version", 123),
        ("description", None),
        ("keywords", "not-an-array"),
        ("keywords", ["review", 1]),
    ],
)
def test_manifest_field_types_exit_two(field, value, capsys):
    runner = FakeRunner(release_responses(manifest(**{field: value})))

    assert MODULE.main(["open-pr", "--tag", TAG], run=runner) == 2

    assert len(capsys.readouterr().err.splitlines()) == 1


def test_open_pr_clone_read_error_is_one_line(capsys):
    runner = FakeRunner(release_responses(), create_clone=False)

    assert MODULE.main(["open-pr", "--tag", TAG], run=runner) == 2

    assert len(capsys.readouterr().err.splitlines()) == 1


def test_open_pr_invalid_utf8_clone_is_one_line(capsys):
    runner = FakeRunner(release_responses(), b"\xff")

    assert MODULE.main(["open-pr", "--tag", TAG], run=runner) == 2

    assert len(capsys.readouterr().err.splitlines()) == 1


def test_open_pr_file_write_error_is_one_line(monkeypatch, capsys, tmp_path):
    runner = open_runner(marketplace(entry()), persistent_root=tmp_path)

    def fail_write(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(Path, "write_text", fail_write)

    assert MODULE.main(["open-pr", "--tag", TAG], run=runner) == 2

    assert capsys.readouterr().err.strip() == "file operation failed: disk full"


def test_open_pr_does_not_print_an_empty_pr_url(capsys, tmp_path):
    runner = open_runner(marketplace(entry()), persistent_root=tmp_path)

    assert MODULE.main(["open-pr", "--tag", TAG], run=runner) == 0

    assert capsys.readouterr().out == ""


@pytest.mark.parametrize(
    "response",
    [
        "{",
        "[]",
        json.dumps({"plugins": "not-an-array"}),
        json.dumps(marketplace()),
        json.dumps(marketplace(entry(), entry())),
    ],
)
def test_check_bad_marketplace_responses_exit_two(response, capsys):
    runner = check_runner(response)

    assert MODULE.main(["check", "--tag", TAG], run=runner) == 2

    error = capsys.readouterr().err
    assert len(error.splitlines()) == 1
    assert "drift:" not in error


@pytest.mark.parametrize(
    "release_response",
    ["{", "[]", "{}", json.dumps({"tagName": None}), json.dumps({"tagName": "v1.2"})],
)
def test_remind_bad_release_response_exits_two(release_response, capsys):
    runner = FakeRunner({RELEASE_ARGV: release_response})

    assert MODULE.main(["remind", "--repo", "owner/repo"], run=runner) == 2

    error = capsys.readouterr().err
    assert len(error.splitlines()) == 1
    assert "Traceback" not in error


def test_remind_release_service_failure_is_reported(capsys):
    runner = FakeRunner(
        {
            RELEASE_ARGV: MODULE.CommandFailure(
                RELEASE_ARGV, 1, "HTTP 401: authentication failed"
            )
        }
    )

    assert MODULE.main(["remind", "--repo", "owner/repo"], run=runner) == 2

    error = capsys.readouterr().err
    assert "HTTP 401" in error
    assert len(error.splitlines()) == 1


@pytest.mark.parametrize(
    "payload",
    [
        "{",
        "{}",
        "[null]",
        json.dumps([{**issue(1, TAG), "number": True}]),
        json.dumps([{**issue(1, TAG), "title": None}]),
        json.dumps([{**issue(1, TAG), "state": "MERGED"}]),
    ],
)
def test_remind_bad_issue_response_exits_two(payload, capsys):
    runner = remind_runner_with_payload(payload)

    assert MODULE.main(["remind", "--repo", "owner/repo"], run=runner) == 2

    error = capsys.readouterr().err
    assert len(error.splitlines()) == 1
    assert "Traceback" not in error

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
BRANCH = f"bump-marketplace/claude-code-gauntlet-{TAG}"
TITLE = f"fix(plugins): update {SOURCE_REPO} to {TAG}"
PR_BODY = (
    f"Update the marketplace entry to upstream release [{TAG}]"
    f"(https://github.com/{SOURCE_REPO}/releases/tag/{TAG})."
)
PROVENANCE_ARGV = ("gh", "api", f"repos/{SOURCE_REPO}/commits/{TAG}", "--jq", ".sha")
PR_LIST_ARGV = (
    "gh",
    "pr",
    "list",
    "--repo",
    MARKET_REPO,
    "--head",
    BRANCH,
    "--state",
    "open",
    "--json",
    "url",
)
CLONE_ARGV = ("gh", "repo", "clone", MARKET_REPO)
WRITE_ARGVS = (
    ("git", "switch", "--create", BRANCH),
    ("git", "add", "--", ".claude-plugin/marketplace.json"),
    ("git", "commit", "-m", TITLE),
    ("git", "push", "--force-with-lease", "--set-upstream", "origin", BRANCH),
    (
        "gh",
        "pr",
        "create",
        "--repo",
        MARKET_REPO,
        "--base",
        "main",
        "--title",
        TITLE,
        "--body",
        PR_BODY,
    ),
)
RELEASE_ARGV = ("gh", "release", "view", "--repo", "owner/repo", "--json", "tagName")
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


def current_entry(**overrides):
    value = entry(
        version="1.2.3",
        description="A plugin",
        keywords=["review"],
        source={"source": "url", "url": SOURCE_URL, "sha": SHA},
    )
    value.update(overrides)
    return value


def marketplace(*plugins):
    return {"plugins": list(plugins), "unrelated": {"keep": True}}


def release_responses(manifest_document=None, sha=SHA):
    manifest_document = manifest_document or manifest()
    return {
        ("git", "rev-parse", f"{TAG}^{{commit}}"): sha + "\n",
        PROVENANCE_ARGV: SHA + "\n",
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
        self.calls = []
        self.clone_path = None

    def __call__(self, argv, cwd=None):
        self.calls.append((list(argv), cwd))
        key = tuple(argv)
        assert key in self.responses, f"unregistered command: {argv}"
        response = self.responses[key]
        if isinstance(response, BaseException):
            raise response
        if key == ISSUE_LIST_ARGV:
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
        if key == CLONE_ARGV:
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
        return response


def open_runner(document, release_manifest=None, sha=SHA, persistent_root=None):
    data = json.dumps(document, ensure_ascii=False, indent=2).encode() + b"\n"
    responses = release_responses(release_manifest, sha)
    responses.update({PR_LIST_ARGV: "[]", CLONE_ARGV: ""})
    responses.update(dict.fromkeys(WRITE_ARGVS, ""))
    return FakeRunner(responses, data, persistent_root)


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


def reminder_write_argv(action="create", number=None):
    body = (
        f"A release of claude-code-gauntlet is ready for the marketplace: `{TAG}`.\n\n"
        "Open the marketplace PR:\n\n"
        f"`python3 .github/marketplace_bump.py open-pr --tag {TAG}`\n\n"
        "Check the marketplace entry after it merges:\n\n"
        f"`python3 .github/marketplace_bump.py check --tag {TAG}`\n\n"
        "Close this issue once the marketplace PR merges."
    )
    return (
        "gh",
        "issue",
        action,
        *([str(number)] if action == "edit" else []),
        "--repo",
        "owner/repo",
        "--title",
        f"Marketplace bump due: {TAG}",
        "--body",
        body,
    )


def remind_runner(issues, tag=TAG, action="create", number=None):
    responses = {
        RELEASE_ARGV: json.dumps({"tagName": tag}),
        ISSUE_LIST_ARGV: json.dumps(issues),
    }
    if action != "none":
        responses[reminder_write_argv(action, number)] = ""
    return FakeRunner(responses)


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
    target = current_entry(description="A plugin" if code == 0 else "Wrong")
    runner = check_runner(marketplace(target))
    command = Command.legacy(
        partial(MODULE.main, run=runner), prog="marketplace_bump.py"
    )
    saved = ["caller.py", "unrelated"]
    monkeypatch.setattr(sys, "argv", saved)

    assert command.invoke(argv) == code
    assert sys.argv is saved
    assert len(runner.calls) == (4 if argv[0] == "check" else 0)


@pytest.mark.parametrize(
    "url", ["", "https://github.com/liatrio-labs/claude-plugins/pull/42"]
)
def test_open_pr_rewrites_only_matching_entry_and_runs_exact_sequence(
    url, capsys, tmp_path
):
    second = entry("https://github.com/example/other.git", name="other")
    original = marketplace(entry(), second)
    runner = open_runner(
        original, manifest(description="Révision"), persistent_root=tmp_path
    )
    runner.responses[WRITE_ARGVS[-1]] = url

    if url:
        failed = open_runner(original)
        create = WRITE_ARGVS[-1]
        failed.responses[create] = MODULE.CommandFailure(
            create, 1, "PR creation failed"
        )
        assert MODULE.main(["open-pr", "--tag", TAG], run=failed) == 2
        assert [tuple(argv) for argv, _ in failed.calls[-2:]] == [PR_LIST_ARGV, create]
        assert "PR creation failed" in capsys.readouterr().err

    assert MODULE.main(["open-pr", "--tag", TAG], run=runner) == 0

    expected = marketplace(current_entry(description="Révision"), second)
    assert (runner.clone_path / ".claude-plugin/marketplace.json").read_bytes() == (
        json.dumps(expected, ensure_ascii=False, indent=2) + "\n"
    ).encode()
    assert [argv for argv, _ in runner.calls] == [
        ["git", "rev-parse", f"{TAG}^{{commit}}"],
        list(PROVENANCE_ARGV),
        ["git", "show", f"{TAG}:.claude-plugin/plugin.json"],
        list(CLONE_ARGV),
        *map(list, WRITE_ARGVS[:-1]),
        list(PR_LIST_ARGV),
        list(WRITE_ARGVS[-1]),
    ]
    assert [cwd for _, cwd in runner.calls[:3]] == [str(ROOT), None, str(ROOT)]
    assert Path(runner.calls[3][1]) / "claude-plugins" == Path(runner.calls[4][1])
    assert all(cwd == runner.calls[4][1] for _, cwd in runner.calls[4:8])
    assert runner.calls[8][1] is None
    assert runner.calls[9][1] == runner.calls[4][1]
    assert capsys.readouterr().out == (url + "\n" if url else "")


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
    runner = open_runner(marketplace(), persistent_root=tmp_path)
    runner.clone_bytes = raw

    assert MODULE.main(["open-pr", "--tag", TAG], run=runner) == 0

    assert runner.clone_path is not None
    assert (runner.clone_path / ".claude-plugin/marketplace.json").read_bytes() == raw
    assert [tuple(argv) for argv, _ in runner.calls] == [
        *release_responses(),
        CLONE_ARGV,
    ]
    assert (
        capsys.readouterr().out.strip() == "The marketplace entry is already current."
    )


def test_open_pr_existing_pr_rewrites_commits_and_pushes_before_printing_url(
    capsys, tmp_path
):
    url = "https://github.com/liatrio-labs/claude-plugins/pull/42"
    runner = open_runner(marketplace(entry()), persistent_root=tmp_path)
    runner.responses[PR_LIST_ARGV] = json.dumps([{"url": url}])
    del runner.responses[WRITE_ARGVS[-1]]

    def run(argv, cwd=None):
        if tuple(argv) in (*WRITE_ARGVS[:-1], PR_LIST_ARGV):
            assert capsys.readouterr().out == ""
            assert runner.clone_path is not None
            assert json.loads(
                (runner.clone_path / ".claude-plugin/marketplace.json").read_text(
                    encoding="utf-8"
                )
            ) == marketplace(current_entry())
        return runner(argv, cwd)

    assert MODULE.main(["open-pr", "--tag", TAG], run=run) == 0

    assert [tuple(argv) for argv, _ in runner.calls] == [
        *release_responses(),
        CLONE_ARGV,
        *WRITE_ARGVS[:-1],
        PR_LIST_ARGV,
    ]
    assert capsys.readouterr().out == url + "\n"


@pytest.mark.parametrize("response", ["{", "{}", "[null]", '[{"url": null}]'])
def test_open_pr_invalid_pr_list_reports_error_after_push(response, capsys):
    runner = open_runner(marketplace(entry()))
    runner.responses[PR_LIST_ARGV] = response

    assert MODULE.main(["open-pr", "--tag", TAG], run=runner) == 2

    assert capsys.readouterr().err.startswith("pull request list")
    assert [tuple(argv) for argv, _ in runner.calls] == [
        *release_responses(),
        CLONE_ARGV,
        *WRITE_ARGVS[:-1],
        PR_LIST_ARGV,
    ]


@pytest.mark.parametrize("command", ["open-pr", "check"])
def test_release_provenance_mismatch_refuses_before_clone_or_write(command, capsys):
    published = "c" * 40
    runner = FakeRunner(
        {("git", "rev-parse", f"{TAG}^{{commit}}"): SHA, PROVENANCE_ARGV: published}
    )

    assert MODULE.main([command, "--tag", TAG], run=runner) == 2

    assert capsys.readouterr().err == (
        f"release {TAG} sha mismatch: local {SHA}, published {published}\n"
    )
    assert runner.calls == [
        (["git", "rev-parse", f"{TAG}^{{commit}}"], str(ROOT)),
        (list(PROVENANCE_ARGV), None),
    ]
    assert runner.clone_path is None


def test_check_accepts_matching_entry_and_normalizes_uppercase_sha():
    runner = check_runner(marketplace(current_entry()))
    runner.responses[("git", "rev-parse", f"{TAG}^{{commit}}")] = SHA.upper()

    assert MODULE.main(["check", "--tag", TAG], run=runner) == 0
    assert [argv for argv, _ in runner.calls] == [
        ["git", "rev-parse", f"{TAG}^{{commit}}"],
        list(PROVENANCE_ARGV),
        ["git", "show", f"{TAG}:.claude-plugin/plugin.json"],
        list(MARKETPLACE_API_ARGV),
    ]


@pytest.mark.parametrize(
    ("field", "overrides"),
    [
        ("version", {"version": "9.9.9"}),
        ("source.sha", {"source": {"url": SOURCE_URL, "sha": "c" * 40}}),
        ("description", {"description": "Wrong"}),
        ("keywords", {"keywords": []}),
    ],
)
def test_check_reports_drift_fields(field, overrides, capsys):
    runner = check_runner(marketplace(current_entry(**overrides)))

    assert MODULE.main(["check", "--tag", TAG], run=runner) == 1

    assert capsys.readouterr().out.splitlines() == [f"drift: {field}"]


@pytest.mark.parametrize(
    ("issues", "action", "issue_number"),
    [
        ([], "create", None),
        ([issue(8, "v1.2.2")], "edit", 8),
        ([issue(9, "v1.2.2", "CLOSED")], "create", None),
        ([issue(10, TAG)], "none", None),
        ([issue(12, "v1.2.2"), issue(13, TAG, "CLOSED")], "none", None),
        ([issue(13, TAG, "CLOSED"), issue(12, "v1.2.2")], "none", None),
    ],
    ids=[
        "no_issue",
        "open_older",
        "closed_older",
        "open_latest",
        "closed_latest_listed_last",
        "closed_latest_listed_first",
    ],
)
def test_remind_decision_table(issues, action, issue_number):
    runner = remind_runner(issues, action=action, number=issue_number)
    if action == "edit":
        runner.responses[reminder_write_argv(action, issue_number)] = (
            "https://github.com/owner/repo/issues/42"
        )

    assert MODULE.main(["remind", "--repo", "owner/repo"], run=runner) == 0

    issue_calls = [argv for argv, _ in runner.calls if argv[:2] == ["gh", "issue"]]
    assert runner.calls[:2] == [
        (list(RELEASE_ARGV), None),
        (list(ISSUE_LIST_ARGV), None),
    ]
    if action == "none":
        assert issue_calls == [list(ISSUE_LIST_ARGV)]
    else:
        assert issue_calls == [
            list(ISSUE_LIST_ARGV),
            list(reminder_write_argv(action, issue_number)),
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
        {RELEASE_ARGV: MODULE.CommandFailure(RELEASE_ARGV, 1, "release not found")}
    )

    assert MODULE.main(["remind", "--repo", "owner/repo"], run=runner) == 0

    assert capsys.readouterr().out == ""


@pytest.mark.parametrize(
    ("argv", "responses", "message"),
    [
        (["open-pr", "--tag", "1.2.3"], {}, "malformed release tag"),
        (["check", "--tag", "v1.2"], {}, "malformed release tag"),
        (["remind", "--repo", "bad"], {}, "malformed repository"),
        (
            ["open-pr", "--tag", TAG],
            {
                **release_responses(),
                ("git", "show", f"{TAG}:.claude-plugin/plugin.json"): "[]",
            },
            "release manifest must be a JSON object",
        ),
        (
            ["open-pr", "--tag", TAG],
            {
                ("git", "rev-parse", f"{TAG}^{{commit}}"): SHA,
                PROVENANCE_ARGV: SHA,
                ("git", "show", f"{TAG}:.claude-plugin/plugin.json"): "{",
            },
            "release manifest is not valid JSON",
        ),
        (
            ["open-pr", "--tag", TAG],
            {("git", "rev-parse", f"{TAG}^{{commit}}"): "short-sha"},
            f"git returned a malformed commit sha for {TAG}",
        ),
    ],
)
def test_release_bad_inputs_exit_two_with_one_line(argv, responses, message, capsys):
    runner = FakeRunner(responses)

    assert MODULE.main(argv, run=runner) == 2

    error = capsys.readouterr().err
    assert len(error.splitlines()) == 1
    assert error.startswith(message)
    assert len(runner.calls) == len(responses)


@pytest.mark.parametrize(
    "document",
    [
        "{",
        json.dumps({"plugins": []}),
        json.dumps(marketplace(entry(), entry())),
        json.dumps(marketplace(entry(SOURCE_URL + ".lookalike"))),
    ],
    ids=["invalid_json", "no_entry", "several_entries", "lookalike_url"],
)
def test_open_pr_bad_marketplace_json_or_entry_exits_two(document, capsys, tmp_path):
    runner = open_runner(marketplace(), persistent_root=tmp_path)
    runner.clone_bytes = document.encode()

    assert MODULE.main(["open-pr", "--tag", TAG], run=runner) == 2

    error = capsys.readouterr().err
    assert len(error.splitlines()) == 1
    assert "Traceback" not in error
    assert not any(argv[:2] == ["git", "switch"] for argv, _ in runner.calls)
    assert (
        runner.clone_path / ".claude-plugin/marketplace.json"
    ).read_bytes() == document.encode()
    if SOURCE_URL + ".lookalike" in document:
        assert error.strip() == f"marketplace has no entries for {SOURCE_URL}"


def test_reminder_workflow_uses_weekly_issue_reminder_and_old_publish_file_is_absent():
    workflow = (ROOT / ".github/workflows/marketplace-reminder.yml").read_text(
        encoding="utf-8"
    )

    # The test environment has no YAML parser, so each block is matched whole with its
    # indentation: a commented-out trigger or a key moved to another level breaks the match.
    for block in (
        'on:\n  schedule:\n    - cron: "0 14 * * 1"\n  workflow_dispatch:\n\n',
        "\npermissions:\n  contents: read\n\n",
        "\nconcurrency:\n  group: marketplace-reminder\n  cancel-in-progress: false\n\n",
        "    permissions:\n      contents: read\n      issues: write\n    steps:\n",
        "        env:\n          GH_TOKEN: ${{ github.token }}\n"
        '        run: python3 .github/marketplace_bump.py remind --repo "$GITHUB_REPOSITORY"\n',
    ):
        assert block in workflow
    assert not (ROOT / ".github/workflows/publish-marketplace.yml").exists()


@pytest.mark.parametrize(
    ("result", "code", "message"),
    [
        (
            SimpleNamespace(
                returncode=0, stdout=json.dumps({"tagName": TAG}), stderr=""
            ),
            0,
            "",
        ),
        (
            SimpleNamespace(returncode=7, stdout="", stderr="denied\n"),
            2,
            "command failed (7): " + " ".join(RELEASE_ARGV) + ": denied\n",
        ),
        (
            FileNotFoundError("missing executable"),
            2,
            "command failed (127): "
            + " ".join(RELEASE_ARGV)
            + ": missing executable\n",
        ),
        (
            UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid byte"),
            2,
            "command output is not valid UTF-8\n",
        ),
    ],
)
def test_runner_is_the_only_subprocess_boundary(
    result, code, message, monkeypatch, capsys
):
    calls = []

    def fake_subprocess_run(argv, **kwargs):
        calls.append((argv, kwargs))
        if isinstance(result, BaseException):
            raise result
        if tuple(argv) == ISSUE_LIST_ARGV:
            return SimpleNamespace(
                returncode=0, stdout=json.dumps([issue(1, TAG)]), stderr=""
            )
        return result

    monkeypatch.setattr(MODULE.subprocess, "run", fake_subprocess_run)

    assert (
        MODULE.main(["remind", "--repo", "owner/repo"], run=MODULE.run_command) == code
    )
    assert capsys.readouterr().err == message
    assert calls == [
        (
            list(argv),
            {
                "cwd": None,
                "capture_output": True,
                "text": True,
                "encoding": "utf-8",
                "check": False,
            },
        )
        for argv in ([RELEASE_ARGV, ISSUE_LIST_ARGV] if code == 0 else [RELEASE_ARGV])
    ]


@pytest.mark.parametrize(
    "stderr", ["", "remote: https://github.com/example/repo\ndenied\n \n"]
)
def test_push_failure_reports_last_nonempty_stderr_line(stderr, capsys):
    runner = open_runner(marketplace(entry()))
    push = WRITE_ARGVS[-2]
    runner.responses[push] = MODULE.CommandFailure(push, 1, stderr)

    assert MODULE.main(["open-pr", "--tag", TAG], run=runner) == 2

    assert capsys.readouterr().err == (
        "command failed (1): " + " ".join(push) + (": denied" if stderr else "") + "\n"
    )
    assert tuple(runner.calls[-1][0]) == push


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        *[
            (field, "missing", f"release manifest is missing fields: {field}")
            for field in ("version", "description", "keywords")
        ],
        ("version", 123, "release manifest version must be a string"),
        ("description", None, "release manifest description must be a string"),
        (
            "keywords",
            "not-an-array",
            "release manifest keywords must be an array of strings",
        ),
        (
            "keywords",
            ["review", 1],
            "release manifest keywords must be an array of strings",
        ),
        (
            "version",
            "1.2.2",
            f"release manifest version '1.2.2' does not match tag '{TAG}'",
        ),
    ],
)
def test_invalid_manifest_fields_exit_two(field, value, message, capsys):
    document = manifest(**{field: value})
    if value == "missing":
        del document[field]
    runner = FakeRunner(release_responses(document))

    assert MODULE.main(["open-pr", "--tag", TAG], run=runner) == 2

    assert capsys.readouterr().err == message + "\n"
    assert len(runner.calls) == 3


@pytest.mark.parametrize("create_clone", [False, True], ids=["missing", "invalid_utf8"])
def test_open_pr_clone_read_error_is_one_line(create_clone, capsys):
    runner = open_runner(marketplace())
    runner.create_clone = create_clone
    runner.clone_bytes = b"\xff"

    assert MODULE.main(["open-pr", "--tag", TAG], run=runner) == 2

    assert len(capsys.readouterr().err.splitlines()) == 1


def test_open_pr_file_write_error_is_one_line(monkeypatch, capsys, tmp_path):
    runner = open_runner(marketplace(entry()), persistent_root=tmp_path)

    def fail_write(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(Path, "write_text", fail_write)

    assert MODULE.main(["open-pr", "--tag", TAG], run=runner) == 2

    assert capsys.readouterr().err.strip() == "file operation failed: disk full"


def test_check_bad_marketplace_shape_exits_two(capsys):
    runner = check_runner({"plugins": "not-an-array"})

    assert MODULE.main(["check", "--tag", TAG], run=runner) == 2

    error = capsys.readouterr().err
    assert len(error.splitlines()) == 1
    assert "drift:" not in error


@pytest.mark.parametrize(
    "release_response",
    ["{}", json.dumps({"tagName": "v1.2"})],
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
    runner = remind_runner([])
    runner.responses[ISSUE_LIST_ARGV] = payload

    assert MODULE.main(["remind", "--repo", "owner/repo"], run=runner) == 2

    error = capsys.readouterr().err
    assert len(error.splitlines()) == 1
    assert "Traceback" not in error

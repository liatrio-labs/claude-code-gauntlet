"""Boundary tests for the maintainer marketplace command."""

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

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
PR_LIST_ARGV = tuple(
    f"gh pr list --repo {MARKET_REPO} --head {BRANCH} --state open --json url".split()
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
    def __init__(self, responses=None, clone_bytes=None, persistent_root=None):
        self.responses = dict(responses or {})
        self.clone_bytes = clone_bytes
        self.persistent_root = persistent_root
        self.calls = []
        self.clone_path = None

    def __call__(self, argv, cwd=None):
        self.calls.append((list(argv), cwd))
        key = tuple(argv)
        assert key in self.responses, f"unregistered command: {argv}"
        response = self.responses[key]
        if isinstance(response, BaseException):
            raise response
        if key == CLONE_ARGV:
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


def issue(number, tag, state="OPEN"):
    value = {
        "number": number,
        "title": f"Marketplace bump due: {tag}",
        "state": state,
    }
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


@pytest.mark.parametrize("existing", [False, True])
def test_open_pr_smoke(existing, capsys, tmp_path):
    second = entry("https://github.com/example/other.git", name="other")
    runner = open_runner(
        marketplace(entry(), second),
        manifest(description="Révision"),
        persistent_root=tmp_path,
    )
    url = "https://github.com/liatrio-labs/claude-plugins/pull/42"
    runner.responses[WRITE_ARGVS[-1]] = url
    if existing:
        runner.responses[PR_LIST_ARGV] = json.dumps([{"url": url}])
        del runner.responses[WRITE_ARGVS[-1]]
    assert MODULE.main(["open-pr", "--tag", TAG], run=runner) == 0
    assert (runner.clone_path / ".claude-plugin/marketplace.json").read_bytes() == (
        json.dumps(
            marketplace(current_entry(description="Révision"), second),
            ensure_ascii=False,
            indent=2,
        )
        + "\n"
    ).encode()
    assert [tuple(argv) for argv, _ in runner.calls] == [
        *release_responses(),
        CLONE_ARGV,
        *WRITE_ARGVS[:-1],
        PR_LIST_ARGV,
        *([] if existing else [WRITE_ARGVS[-1]]),
    ]
    assert [cwd for _, cwd in runner.calls[:3]] == [str(ROOT), None, str(ROOT)]
    assert all(cwd == runner.calls[4][1] for _, cwd in runner.calls[4:8])
    assert Path(runner.calls[3][1]) / "claude-plugins" == Path(runner.calls[4][1])
    assert capsys.readouterr().out == url + "\n"


def test_open_pr_already_current_preserves_file_bytes_and_creates_no_branch(
    capsys, tmp_path
):
    runner = open_runner(marketplace(current_entry()), persistent_root=tmp_path)
    runner.clone_bytes = json.dumps(marketplace(current_entry()), indent=4).encode()
    assert MODULE.main(["open-pr", "--tag", TAG], run=runner) == 0
    assert (runner.clone_path / ".claude-plugin/marketplace.json").read_bytes() == (
        runner.clone_bytes
    )
    assert [tuple(argv) for argv, _ in runner.calls] == [
        *release_responses(),
        CLONE_ARGV,
    ]
    assert (
        capsys.readouterr().out.strip() == "The marketplace entry is already current."
    )


@pytest.mark.parametrize("drift", [False, True])
def test_check_smoke_and_drift(drift, capsys):
    runner = check_runner(marketplace(entry() if drift else current_entry()))
    runner.responses[("git", "rev-parse", f"{TAG}^{{commit}}")] = SHA.upper()
    assert MODULE.main(["check", "--tag", TAG], run=runner) == int(drift)
    assert [tuple(argv) for argv, _ in runner.calls] == [
        *release_responses(),
        MARKETPLACE_API_ARGV,
    ]
    assert capsys.readouterr().out.splitlines() == (
        ["drift: version", "drift: source.sha", "drift: description", "drift: keywords"]
        if drift
        else [f"in sync: marketplace entry matches {TAG}"]
    )


@pytest.mark.parametrize(
    ("issues", "action", "issue_number"),
    [
        ([], "create", None),
        ([issue(8, "v1.2.2")], "edit", 8),
        ([issue(9, "v1.2.2", "CLOSED")], "create", None),
        ([issue(12, "v1.2.2"), issue(13, TAG, "CLOSED")], "none", None),
        ([issue(31, f"{TAG} extra")], "create", None),
    ],
    ids=["create", "edit", "closed-older", "latest-closed", "near-miss"],
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
    ("stage", "message"),
    [
        ("tag", "malformed release tag"),
        ("repo", "malformed repository"),
        ("sha", "malformed commit sha"),
        ("provenance", "sha mismatch"),
        ("json", "release manifest is not valid JSON"),
        ("manifest", "release manifest is missing fields"),
        ("version", "does not match tag"),
        ("document", "must contain a plugins array"),
        ("entry", "several entries"),
        ("release", "missing tagName"),
        ("issues", "issue list must be a JSON array"),
    ],
)
def test_refuses_bad_input_before_write(stage, message, capsys, tmp_path):
    runner = open_runner(marketplace(entry()), persistent_root=tmp_path)
    argv = ["open-pr", "--tag", TAG]
    stop = 3
    if stage == "tag":
        argv[-1] = "1.2.3"
        stop = 0
    elif stage == "repo":
        argv = ["remind", "--repo", "bad"]
        stop = 0
    elif stage == "sha":
        runner.responses[("git", "rev-parse", f"{TAG}^{{commit}}")] = "short"
        stop = 1
    elif stage == "provenance":
        runner.responses[PROVENANCE_ARGV] = "c" * 40
        stop = 2
    elif stage in {"json", "manifest", "version"}:
        payload = {
            "json": "{",
            "manifest": json.dumps({"version": "1.2.3"}),
            "version": json.dumps(manifest(version="1.2.2")),
        }[stage]
        runner.responses[("git", "show", f"{TAG}:.claude-plugin/plugin.json")] = payload
    elif stage in {"document", "entry"}:
        runner.clone_bytes = json.dumps(
            {"plugins": {}} if stage == "document" else marketplace(entry(), entry())
        ).encode()
        stop = 4
    else:
        runner = remind_runner([])
        argv = ["remind", "--repo", "owner/repo"]
        if stage == "release":
            runner.responses[RELEASE_ARGV] = "{}"
            stop = 1
        else:
            runner.responses[ISSUE_LIST_ARGV] = "{}"
            stop = 2
    before = runner.clone_bytes
    assert MODULE.main(argv, run=runner) == 2
    error = capsys.readouterr().err
    assert message in error and len(error.splitlines()) == 1
    assert len(runner.calls) == stop
    assert not any(tuple(call) in WRITE_ARGVS for call, _ in runner.calls)
    assert not any(
        call[:3] in [["gh", "issue", "create"], ["gh", "issue", "edit"]]
        for call, _ in runner.calls
    )
    if runner.clone_path:
        assert (
            runner.clone_path / ".claude-plugin/marketplace.json"
        ).read_bytes() == before


@pytest.mark.parametrize("failed", [False, True])
def test_runner_smoke_and_failure(failed, monkeypatch, capsys):
    calls = []

    def run(argv, **kwargs):
        calls.append((tuple(argv), kwargs))
        if failed:
            return SimpleNamespace(returncode=7, stdout="", stderr="denied\n")
        output = {RELEASE_ARGV: {"tagName": TAG}, ISSUE_LIST_ARGV: [issue(1, TAG)]}
        return SimpleNamespace(
            returncode=0, stdout=json.dumps(output[tuple(argv)]), stderr=""
        )

    monkeypatch.setattr(MODULE.subprocess, "run", run)
    assert MODULE.main(["remind", "--repo", "owner/repo"], run=MODULE.run_command) == (
        2 if failed else 0
    )
    assert capsys.readouterr().err == (
        "command failed (7): " + " ".join(RELEASE_ARGV) + ": denied\n" if failed else ""
    )
    assert calls == [
        (
            argv,
            {
                "cwd": None,
                "capture_output": True,
                "text": True,
                "encoding": "utf-8",
                "check": False,
            },
        )
        for argv in ([RELEASE_ARGV] if failed else [RELEASE_ARGV, ISSUE_LIST_ARGV])
    ]

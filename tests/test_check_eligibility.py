"""Eligibility decisions at the shipped CLI boundary with exact forge argv."""

import json
from pathlib import Path

import pytest
from gauntlet import eligibility, proc

from tests.support.pr_files import FIXTURES, fake_files_forge, github_view, gitlab_view

LOCKFILES = (
    "package-lock.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "Cargo.lock",
    "Gemfile.lock",
    "poetry.lock",
    "composer.lock",
    "go.sum",
    "Pipfile.lock",
    "uv.lock",
    "bun.lockb",
    "bun.lock",
    "mix.lock",
    "pubspec.lock",
    "Podfile.lock",
    "packages.lock.json",
    "gradle.lockfile",
    "flake.lock",
    "npm-shrinkwrap.json",
)
CASES = [
    ("interactive", "CLOSED", False, "review", "src/code", "stop", "closed"),
    ("interactive", "MERGED", False, "review", "src/code", "stop", "closed"),
    ("headless", "CLOSED", False, "review", "src/code", "proceed", None),
    ("headless", "MERGED", False, "review", "src/code", "proceed", None),
    ("interactive", "OPEN", True, "review", "src/code", "ask_draft", None),
    ("headless", "OPEN", True, "review", "src/code", "proceed", None),
    ("headless", "OPEN", True, "skip", "src/code", "stop", "draft_policy=skip"),
    ("interactive", "CLOSED", True, "review", "", "stop", "closed"),
    (
        "interactive",
        "OPEN",
        False,
        "review",
        "nested/yarn.lock",
        "stop",
        "lockfile-only",
    ),
    ("headless", "OPEN", False, "review", "yarn.lock\nsrc/code", "proceed", None),
    ("interactive", "OPEN", False, "review", "README.md\nsrc/comment", "proceed", None),
    ("interactive", "OPEN", False, "review", "", "stop", "no changes"),
    ("headless", "OPEN", False, "review", "", "stop", "no changes"),
    ("interactive", "OPEN", True, "review", "yarn.lock", "stop", "lockfile-only"),
    ("interactive", "OPEN", True, "review", "", "stop", "no changes"),
    ("headless", "OPEN", True, "skip", "yarn.lock", "stop", "lockfile-only"),
    ("headless", "CLOSED", False, "review", "yarn.lock", "stop", "lockfile-only"),
    ("headless", "OPEN", False, "skip", "src/code", "proceed", None),
    ("headless", "OPEN", True, "skip", None, "stop", "draft_policy=skip"),
    ("interactive", "OPEN", True, "review", None, "ask_draft", None),
    (
        "headless",
        "OPEN",
        False,
        "review",
        None,
        "proceed",
        "file listing failed: unavailable",
    ),
]


def _invoke(invoke, tmp_path, mode="interactive", policy="review", platform="github"):
    return invoke(
        "check_eligibility",
        ["--platform", platform, "--pr", "7"],
        tmp_path,
        json.dumps({"mode": mode, "resolved": {"draft_policy": policy}}).encode(),
    )


@pytest.mark.parametrize("mode,state,draft,policy,paths,verdict,reason", CASES)
def test_verdict_matrix(
    mode, state, draft, policy, paths, verdict, reason, monkeypatch, tmp_path, invoke
):
    listing = (paths, "", 0) if paths is not None else ("", "unavailable", 1)
    calls = fake_files_forge(monkeypatch, github_view(state, draft), listing)
    result = _invoke(invoke, tmp_path, mode, policy)
    assert result.returncode == 0, result.stderr
    assert result.stderr == b""
    assert json.loads(result.stdout) == {
        "verdict": verdict,
        "reason": reason,
        "state": state.lower(),
        "draft": draft,
    }
    assert len(calls) == 2


@pytest.mark.parametrize("path", LOCKFILES)
def test_closed_lockfile_vocabulary(path, monkeypatch, tmp_path, invoke):
    assert frozenset(LOCKFILES) == eligibility.LOCKFILES
    fake_files_forge(monkeypatch, github_view(), (f"sub/{path}\n", "", 0))
    result = _invoke(invoke, tmp_path)
    assert json.loads(result.stdout)["reason"] == "lockfile-only"


@pytest.mark.parametrize(
    "path",
    [
        "generated/code",
        "src/formatted",
        "README.md",
        "YARN.LOCK",
        "custom.lock",
        "yarn.lock/code",
    ],
)
def test_other_names_proceed(path, monkeypatch, tmp_path, invoke):
    fake_files_forge(monkeypatch, github_view(), (path, "", 0))
    result = _invoke(invoke, tmp_path)
    assert json.loads(result.stdout)["verdict"] == "proceed"


@pytest.mark.parametrize(
    "listing,reason",
    [
        (("", "API unavailable", 1), "file listing failed: API unavailable"),
        (("yarn.lock\n" * 3000, "", 0), "file listing reached the 3000-file cap"),
        (("yarn.lock\n" * 3001, "", 0), "file listing reached the 3000-file cap"),
        (OSError("unavailable"), "file listing failed: unavailable"),
        (ValueError("bad encoding"), "file listing failed: bad encoding"),
        (
            proc.TimeoutExpired("gh", 30),
            "file listing failed: Command 'gh' timed out after 30 seconds",
        ),
    ],
)
def test_uncertain_listing_proceeds_with_reason(
    listing, reason, monkeypatch, tmp_path, invoke
):
    fake_files_forge(monkeypatch, github_view(), listing)
    result = _invoke(invoke, tmp_path, "headless", "review")
    assert result.returncode == 0
    assert json.loads(result.stdout) == {
        "verdict": "proceed",
        "reason": reason,
        "state": "open",
        "draft": False,
    }


def test_below_github_cap_can_stop(monkeypatch, tmp_path, invoke):
    fake_files_forge(monkeypatch, github_view(), ("yarn.lock\n" * 2999, "", 0))
    assert json.loads(_invoke(invoke, tmp_path).stdout)["verdict"] == "stop"


@pytest.mark.parametrize(
    "facts",
    [
        ("", "forbidden", 1),
        ("{", "", 0),
        ("[]", "", 0),
        ('{"state":"unknown","isDraft":false}', "", 0),
        ('{"state":"OPEN","isDraft":"false"}', "", 0),
        OSError("missing gh"),
        proc.TimeoutExpired("gh", 30),
    ],
)
def test_state_failure_is_fatal(facts, monkeypatch, tmp_path, invoke):
    calls = fake_files_forge(monkeypatch, facts)
    result = _invoke(invoke, tmp_path)
    assert result.returncode == 1
    assert result.stdout == b""
    assert b"eligibility: FAILED" in result.stderr
    assert len(calls) == 1


@pytest.mark.parametrize("url", [None, "wrong"])
def test_unusable_pr_url_discloses_listing_failure(url, monkeypatch, tmp_path, invoke):
    fake_files_forge(
        monkeypatch,
        (json.dumps({"state": "OPEN", "isDraft": False, "url": url}), "", 0),
    )
    result = _invoke(invoke, tmp_path)
    assert json.loads(result.stdout)["verdict"] == "proceed"
    assert "file listing failed" in json.loads(result.stdout)["reason"]


@pytest.mark.parametrize(
    "state,draft_fields,expected_state,verdict",
    [
        ("opened", {"draft": True}, "open", "ask_draft"),
        ("opened", {"work_in_progress": True}, "open", "ask_draft"),
        ("opened", {"draft": False, "work_in_progress": True}, "open", "proceed"),
        ("merged", {"draft": False}, "merged", "stop"),
        ("closed", {"draft": False}, "closed", "stop"),
        ("locked", {"draft": False}, "open", "proceed"),
    ],
)
def test_gitlab_normalization(
    state, draft_fields, expected_state, verdict, monkeypatch, tmp_path, invoke
):
    view = gitlab_view(state=state, **draft_fields)
    if "draft" not in draft_fields:
        view.pop("draft")
    calls = fake_files_forge(
        monkeypatch,
        (json.dumps(view), "", 0),
        ((FIXTURES / "glab_mr_diffs.ndjson").read_text(encoding="utf-8"), "", 0),
        "gitlab",
    )
    result = _invoke(invoke, tmp_path, platform="gitlab")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["state"] == expected_state
    assert json.loads(result.stdout)["verdict"] == verdict
    assert len(calls) == 2


@pytest.mark.parametrize(
    "payload,argv",
    [
        (b"{", []),
        (b"[]", []),
        (b"{}", []),
        (b'{"mode":"interactive","resolved":null}', []),
        (b'{"mode":"headless","resolved":{}}', []),
        (b'{"mode":"interactive","resolved":{"draft_policy":"bad"}}', []),
        (b'{"mode":"interactive","resolved":{}}', ["--pr", "0"]),
    ],
)
def test_invalid_input_is_usage_error(payload, argv, tmp_path, invoke):
    result = invoke(
        "check_eligibility",
        ["--platform", "github", "--pr", "7", *argv],
        tmp_path,
        payload,
    )
    assert result.returncode == 2
    assert result.stdout == b""


MUTANTS = [
    ('facts.state != "open" and mode == "interactive"', "False"),
    ('facts.state != "open" and mode == "interactive"', 'facts.state != "open"'),
    ("if not files.paths:", "if False:"),
    ("all(PurePosixPath(path).name in LOCKFILES for path in files.paths)", "False"),
    (
        "all(PurePosixPath(path).name in LOCKFILES for path in files.paths)",
        "any(PurePosixPath(path).name in LOCKFILES for path in files.paths)",
    ),
    ("if facts.draft:", "if False:"),
    ('if policy == "skip":', "if False:"),
    ('return "ask_draft", None', 'return "proceed", None'),
    ("if files.complete:", "if True:"),
    (
        "    if files.complete:",
        '    if not files.complete:\n        return "proceed", files.reason\n    if files.complete:',
    ),
]


@pytest.mark.parametrize("before,after", MUTANTS)
def test_eligibility_arm_mutants_are_killed(
    before, after, monkeypatch, tmp_path, invoke
):
    source = Path(eligibility.__file__).read_text(encoding="utf-8")
    assert source.count(before) == 1
    namespace = vars(eligibility).copy()
    exec(
        compile(source.replace(before, after), str(eligibility.__file__), "exec"),
        namespace,
    )
    monkeypatch.setattr(eligibility, "CLI", namespace["CLI"])
    rows = CASES
    killed = False
    for mode, state, draft, policy, paths, verdict, reason in rows:
        listing = (paths, "", 0) if paths is not None else ("", "unavailable", 1)
        fake_files_forge(monkeypatch, github_view(state, draft), listing)
        result = _invoke(invoke, tmp_path, mode, policy)
        expected = {
            "verdict": verdict,
            "reason": reason,
            "state": state.lower(),
            "draft": draft,
        }
        if result.returncode != 0 or json.loads(result.stdout) != expected:
            killed = True
            break
    assert killed, f"survived: {before} -> {after}"

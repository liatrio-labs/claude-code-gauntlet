"""Eligibility decisions at the shipped CLI boundary with exact forge argv."""

import json

import pytest
from gauntlet import eligibility, proc

from tests.support.pr_files import FIXTURES, fake_files_forge, github_view, gitlab_view

CASES = [
    ("interactive", "CLOSED", True, "review", "", "stop", "closed"),
    ("headless", "MERGED", False, "review", "src/code", "proceed", None),
    ("interactive", "OPEN", True, "review", "src/code", "ask_draft", None),
    (
        "headless",
        "OPEN",
        False,
        "review",
        "timeout",
        "proceed",
        "file listing failed: Command 'gh' timed out after 180 seconds",
    ),
    ("headless", "OPEN", True, "review", "src/code", "proceed", None),
    ("headless", "OPEN", True, "skip", None, "stop", "draft_policy=skip"),
    (
        "interactive",
        "OPEN",
        True,
        "review",
        None,
        "ask_draft",
        "file listing failed: unavailable",
    ),
    (
        "headless",
        "OPEN",
        False,
        "review",
        None,
        "proceed",
        "file listing failed: unavailable",
    ),
    (
        "interactive",
        "OPEN",
        True,
        "review",
        "nested/yarn.lock",
        "stop",
        "lockfile-only",
    ),
    ("headless", "OPEN", True, "skip", "", "stop", "no changes"),
    ("headless", "CLOSED", False, "review", "yarn.lock", "stop", "lockfile-only"),
    ("headless", "OPEN", False, "skip", "yarn.lock\nsrc/code", "proceed", None),
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
    listing = (
        proc.TimeoutExpired("gh", 180)
        if paths == "timeout"
        else ("\n".join(json.dumps(path) for path in paths.splitlines()), "", 0)
        if paths is not None
        else ("", "unavailable", 1)
    )
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


@pytest.mark.parametrize("path", sorted(eligibility.LOCKFILES))
def test_closed_lockfile_vocabulary(path, monkeypatch, tmp_path, invoke):
    fake_files_forge(monkeypatch, github_view(), (json.dumps(f"sub/{path}"), "", 0))
    result = _invoke(invoke, tmp_path)
    assert json.loads(result.stdout)["reason"] == "lockfile-only"


@pytest.mark.parametrize(
    "path",
    [
        "YARN.LOCK",
        "custom.lock",
        "yarn.lock/code",
    ],
)
def test_other_names_proceed(path, monkeypatch, tmp_path, invoke):
    fake_files_forge(monkeypatch, github_view(), (json.dumps(path), "", 0))
    result = _invoke(invoke, tmp_path)
    assert json.loads(result.stdout)["verdict"] == "proceed"


@pytest.mark.parametrize(
    "facts",
    [
        ("", "forbidden", 1),
        ("[]", "", 0),
        ('{"state":"unknown","isDraft":false}', "", 0),
        ('{"state":"OPEN","isDraft":"false"}', "", 0),
        OSError("missing gh"),
    ],
)
def test_state_failure_is_fatal(facts, monkeypatch, tmp_path, invoke):
    calls = fake_files_forge(monkeypatch, facts)
    result = _invoke(invoke, tmp_path)
    assert result.returncode == 1
    assert result.stdout == b""
    assert b"eligibility: FAILED" in result.stderr
    assert len(calls) == 1


def test_unusable_pr_url_discloses_listing_failure(monkeypatch, tmp_path, invoke):
    fake_files_forge(
        monkeypatch,
        (json.dumps({"state": "OPEN", "isDraft": False, "url": "wrong"}), "", 0),
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

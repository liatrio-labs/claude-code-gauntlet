"""Exact forge responses for changed-file CLI tests."""

import json
from pathlib import Path

from gauntlet import proc

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def gitlab_view(**fields):
    payload = json.loads((FIXTURES / "glab_mr_view.json").read_text(encoding="utf-8"))
    payload.update(fields)
    return payload


def fake_files_forge(
    monkeypatch, facts, listing=("src/code\n", "", 0), platform="github", endpoint=None
):
    calls = []
    view = (
        ["gh", "pr", "view", "7", "--json", "state,isDraft,url"]
        if platform == "github"
        else ["glab", "mr", "view", "7", "--output", "json"]
    )
    files = (
        [
            "gh",
            "api",
            "repos/upstream/project/pulls/7/files",
            "--paginate",
            "--jq",
            ".[].filename",
        ]
        if platform == "github"
        else [
            "glab",
            "api",
            endpoint or "projects/cdr-group%2Fprobe/merge_requests/1/diffs",
            "--paginate",
            "--output",
            "ndjson",
        ]
    )

    def output(argv, **kwargs):
        expected = view if not calls else files
        assert argv == expected
        calls.append(argv)
        response = facts if len(calls) == 1 else listing
        if isinstance(response, Exception):
            raise response
        return response

    monkeypatch.setattr(proc, "output", output)
    return calls


def github_view(state="OPEN", draft=False):
    return (
        json.dumps(
            {
                "state": state,
                "isDraft": draft,
                "url": "https://github.com/upstream/project/pull/7",
            }
        ),
        "",
        0,
    )

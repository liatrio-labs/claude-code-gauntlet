"""Real posting entry with queued forge replies and disk capture."""

import json
import os
from dataclasses import dataclass
from pathlib import Path

from gauntlet import proc
from gauntlet.delivery import post
from gauntlet.forge import JsonFetch, PostRequest

from tests.support.forge import FakeForge, FakeGitLab


@dataclass(frozen=True, slots=True)
class PostingRun:
    code: int
    out: str
    err: str
    payload: dict | None
    raw: bytes | None
    requests: tuple[PostRequest, ...]
    fake: FakeForge
    head_calls: tuple[tuple[str, ...], ...]


def invoke_posting(
    directory,
    monkeypatch,
    capsys,
    factory,
    data,
    *,
    diff="",
    diff_status=0,
    diff_error="",
    refs=None,
    entries=None,
    submissions=None,
    availability=None,
    dry_run=True,
    arguments=(),
    head="deadbeefcafe\n",
    head_status=0,
    input_path=None,
):
    path = Path(input_path) if input_path is not None else directory / "findings.json"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    artifact = Path(os.path.abspath(path)).parent / "post-review-payload.json"
    artifact.unlink(missing_ok=True)
    platform = data.get("platform", "github") if isinstance(data, dict) else "github"
    options = {
        "diffs": [(diff, diff_error, diff_status)],
        "entries": [entries] if entries is not None else None,
        "submissions": submissions,
        "availability": availability,
    }
    fake = (
        FakeGitLab(
            refs=[
                refs
                if refs is not None
                else JsonFetch(
                    [
                        {
                            "base_commit_sha": "base1",
                            "head_commit_sha": "head1",
                            "start_commit_sha": "start1",
                        }
                    ],
                    None,
                )
            ],
            **options,
        )
        if platform == "gitlab"
        else FakeForge(**options)
    )
    factory.configure(fake)
    head_calls = []

    def run(command, **kwargs):
        assert command == ["git", "rev-parse", "HEAD"]
        assert kwargs == {"cwd": None, "timeout": None, "errors": "strict"}
        head_calls.append(tuple(command))
        return proc.CompletedProcess(command, head_status, head, "")

    monkeypatch.setattr(proc, "run", run)
    monkeypatch.delenv("CODE_GAUNTLET_POST_MODE", raising=False)
    capsys.readouterr()
    code = post.CLI.invoke([str(path), *arguments, *(["--dry-run"] if dry_run else [])])
    captured = capsys.readouterr()
    raw = artifact.read_bytes() if artifact.exists() else None
    return PostingRun(
        code,
        captured.out,
        captured.err,
        json.loads(raw) if raw is not None else None,
        raw,
        tuple(call.request for call in fake.calls if call.request is not None),
        fake,
        tuple(head_calls),
    )

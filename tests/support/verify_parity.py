"""Replay raw Merge findings through the live projection and receipt command."""

import json
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


def project_findings(findings):
    from tests.support.pipeline import run_node

    return json.loads(
        run_node(
            "import { projectVerifySliceFinding } from './workflows/src/verifyWire.js';"
            "let source = ''; for await (const chunk of process.stdin) source += chunk;"
            "process.stdout.write(JSON.stringify(JSON.parse(source).map(projectVerifySliceFinding)));",
            findings,
        ).stdout
    )


def replay_decisions(inp, directory):
    repository = directory / "repo"
    repository.mkdir()
    # User config, hooks and inherited worktree variables must not affect the oracle.
    env = {
        key: value for key, value in os.environ.items() if not key.startswith("GIT_")
    }
    env.update(
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_CONFIG_SYSTEM=os.devnull,
        GIT_AUTHOR_NAME="Parity Author",
        GIT_AUTHOR_EMAIL="parity@example.invalid",
        GIT_COMMITTER_NAME="Parity Author",
        GIT_COMMITTER_EMAIL="parity@example.invalid",
        GIT_AUTHOR_DATE="2020-01-01T00:00:00+0000",
        GIT_COMMITTER_DATE="2020-01-01T00:00:00+0000",
    )

    def git(*args):
        return subprocess.run(
            ["git", *args],
            cwd=repository,
            env=env,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
        ).stdout.strip()

    git("init", "-q", "--initial-branch=main", "--template=")
    git("config", "commit.gpgsign", "false")
    git("config", "core.hooksPath", os.devnull)
    git("config", "core.autocrlf", "false")
    for phase in ("base", "head"):
        for name, content in inp["repository"][phase].items():
            path = repository / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8", newline="\n")
        git("add", "--", *inp["repository"][phase])
        git("commit", "-qm", phase)
        if phase == "base":
            git("tag", "parity-base")

    source = (
        "import { projectVerifySliceFinding, encodeSliceInline } "
        "from './workflows/src/verifyWire.js';"
        "let source = ''; for await (const chunk of process.stdin) source += chunk;"
        "const findings = JSON.parse(source).map(projectVerifySliceFinding);"
        "process.stdout.write(encodeSliceInline({findings, base_branch:'parity-base'}));"
    )
    token = subprocess.run(
        ["node", "--input-type=module", "-e", source],
        input=json.dumps(inp["findings"], ensure_ascii=True),
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
        timeout=30,
    ).stdout
    output = directory / "receipt.json"
    result = subprocess.run(
        [
            sys.executable,
            str(REPO / "scripts/verify_findings.py"),
            "--input",
            str(directory / "slice.json"),
            "--input-inline",
            token,
            "--head-sha",
            git("rev-parse", "HEAD"),
            "--nonce",
            "parity",
            "--output",
            str(output),
        ],
        cwd=repository,
        env=env,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
    )
    envelope = json.loads(output.read_text(encoding="utf-8"))
    assert envelope["status"] == "ok", (envelope, result.stderr)
    result = envelope["result"]
    post_by_id = {
        finding["id"]: finding for finding in result["verified"] + result["eliminated"]
    }
    return [
        {
            **delta,
            "factual_verification": post_by_id[delta["id"]]["factual_verification"],
            "diff_validation": post_by_id[delta["id"]].get("diff_validation"),
        }
        for delta in result["deltas"]
    ]

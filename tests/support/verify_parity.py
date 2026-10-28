"""Replay raw Merge findings through the live projection and receipt command."""

import json
import subprocess
import sys
from pathlib import Path

from tests.support.git_diff import git, init_git_repo
from tests.support.pipeline import run_node

REPO = Path(__file__).resolve().parents[2]


def project_findings(findings):
    return json.loads(
        run_node(
            "import { projectVerifySliceFinding } from './workflows/src/verifyWire.js';"
            "let source = ''; for await (const chunk of process.stdin) source += chunk;"
            "process.stdout.write(JSON.stringify(JSON.parse(source).map(projectVerifySliceFinding)));",
            findings,
        ).stdout
    )


def run_verify_slice(document, directory, *, head_sha, nonce, diff_file=None):
    """Project and encode in JS, then run verify_findings.py from the cwd."""
    token = run_node(
        "import { projectVerifySliceFinding, encodeSliceInline } "
        "from './workflows/src/verifyWire.js';"
        "let source = ''; for await (const chunk of process.stdin) source += chunk;"
        "const doc = JSON.parse(source); doc.findings = doc.findings.map(projectVerifySliceFinding);"
        "process.stdout.write(encodeSliceInline(doc));",
        document,
        timeout=60,
    ).stdout
    output = directory / "receipt.json"
    args = [sys.executable, str(REPO / "scripts/verify_findings.py")]
    args += ["--target-kind", "branch"]
    args += ["--input", str(directory / "slice.json"), "--input-inline", token]
    args += ["--head-sha", head_sha, "--nonce", nonce, "--output", str(output)]
    if diff_file is not None:
        args += ["--diff-file", str(diff_file)]
    result = subprocess.run(
        args, capture_output=True, text=True, encoding="utf-8", timeout=60
    )
    assert result.returncode == 0, result.stderr
    return json.loads(output.read_text(encoding="utf-8")), result


def replay_decisions(inp, directory, monkeypatch):
    repository = directory / "repo"
    repository.mkdir()
    init_git_repo(repository, monkeypatch)
    for phase in ("base", "head"):
        for name, content in inp["repository"][phase].items():
            path = repository / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8", newline="\n")
        git(repository, "add", "--", *inp["repository"][phase])
        git(repository, "commit", "-qm", phase)
        if phase == "base":
            git(repository, "tag", "parity-base")
    monkeypatch.chdir(repository)
    envelope, result = run_verify_slice(
        {"findings": inp["findings"], "base_branch": "parity-base"},
        directory,
        head_sha=git(repository, "rev-parse", "HEAD"),
        nonce="parity",
    )
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

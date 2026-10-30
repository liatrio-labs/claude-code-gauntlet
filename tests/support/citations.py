"""Real source capture and command transport for citation boundary tests."""

import json
import subprocess
import sys
from pathlib import Path

from gauntlet.location import SourceAdapter

from tests.support.git_diff import git, init_git_repo

ROOT = Path(__file__).resolve().parents[2]
QUOTE = 'return "café 😀 value";'


def citation_cli(tmp_path, monkeypatch, invoke):
    root, output = tmp_path / "repo", tmp_path / "output"
    root.mkdir()
    output.mkdir()
    init_git_repo(root, monkeypatch)
    (root / "source").write_bytes((QUOTE + "\nsecond_source_line();\n").encode())
    (root / "gone").write_bytes(b"assert removed_behavior;\n")
    (root / "pr-description.json").write_bytes((QUOTE + "\n").encode())
    git(root, "add", ".")
    git(root, "commit", "-qm", "base")
    base = git(root, "rev-parse", "HEAD")
    (root / "gone").unlink()
    git(root, "add", ".")
    git(root, "commit", "-qm", "target")
    head = git(root, "rev-parse", "HEAD")
    diff = (git(root, "diff", base, head) + "\n").encode()
    diff_path = output / "diff.patch"
    diff_path.write_bytes(diff)
    capture = (
        SourceAdapter.capture(root, "branch", head)
        .capture_sources(output, "run", diff, base)
        .receipt
    )
    request = {
        "finding_id": "F",
        "stage": "validator",
        "request_id": "claim",
        "role": "claim",
        "snapshot_id": capture["manifest_sha256"],
        "file": "source",
        "side": "head",
        "line_start": 1,
        "line_end": 1,
        "quote": QUOTE,
    }

    def call(
        requests, *, batch="12345678", attempt=1, nonce=None, real=False, extra=()
    ):
        records = []
        for index, q in enumerate(requests):
            record = {"finding_id": "F", "stage": "validator", "request": q}
            records.append({"id": str(index), **record})
        data = {"requests": records, "base_branch": "main"}
        encoded = subprocess.run(
            [
                "node",
                "--input-type=module",
                "-e",
                "import {encodeSliceInline} from './workflows/src/verifyWire.js'; "
                "let s=''; for await (const c of process.stdin) s+=c; "
                "process.stdout.write(encodeSliceInline(JSON.parse(s)));",
            ],
            cwd=ROOT,
            input=json.dumps(data),
            text=True,
            encoding="utf-8",
            capture_output=True,
            check=True,
        ).stdout
        argv = [
            "--mode",
            "cite",
            "--target-kind",
            "branch",
            "--head-sha",
            head[:8],
            "--input",
            str(output / "input.json"),
            "--input-inline",
            encoded,
            "--nonce",
            nonce or f"run.{batch}.{attempt}",
            "--diff-file",
            str(diff_path),
            "--source-capture",
            json.dumps(capture),
            "--capture-output-dir",
            str(output),
            "--cite-batch",
            batch,
            "--cite-attempt",
            str(attempt),
            *extra,
        ]
        if real:
            completed = subprocess.run(
                [sys.executable, str(ROOT / "scripts/verify_findings.py"), *argv],
                cwd=root,
                text=True,
                encoding="utf-8",
                capture_output=True,
                check=True,
            )
        else:
            completed = invoke("verify_findings", argv, root)
            assert completed.returncode == 0, completed.stderr
        envelope = json.loads(completed.stdout)
        assert envelope["status"] == "ok", envelope
        return envelope, data, encoded

    return call, request, capture, root, output

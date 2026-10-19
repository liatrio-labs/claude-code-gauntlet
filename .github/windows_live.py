"""Drive and mechanically check the manually approved Windows runtime probe."""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, TypedDict

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from gauntlet import tasks  # noqa: E402
from gauntlet.fs import confined  # noqa: E402
from gauntlet.materialize import MaterializeReceipt  # noqa: E402


class LiveReceipt(TypedDict):
    ok: bool
    task_id: str
    task_output: str
    artifactPaths: dict[str, str]
    inputProof: dict[str, int]
    materialize: MaterializeReceipt
    planted_file_mentioned: bool
    host_path_gaps: int


def json_documents(text: str) -> list[dict[str, Any]]:
    decoder = json.JSONDecoder()
    documents = []
    pending = [text]
    seen = set()
    while pending:
        raw = pending.pop()
        if raw in seen:
            continue
        seen.add(raw)
        try:
            whole = json.loads(raw)
        except ValueError:
            whole = None
        if isinstance(whole, str):
            pending.append(whole)
        for start, char in enumerate(raw):
            if char != "{":
                continue
            try:
                document, _ = decoder.raw_decode(raw, start)
            except ValueError:
                continue
            if isinstance(document, dict):
                documents.append(document)
                for value in document.values():
                    if isinstance(value, str):
                        pending.append(value)
                    elif isinstance(value, (dict, list)):
                        pending.append(json.dumps(value, ensure_ascii=False))
    return documents


def checked_result(block: dict[str, Any]) -> list[dict[str, Any]]:
    assert not block.get("is_error"), block
    return json_documents(json.dumps(block.get("content", ""), ensure_ascii=False))


def assert_run(transcript: Path, output: Path, config: Path) -> LiveReceipt:
    messages = [
        json.loads(line)
        for line in transcript.read_text(encoding="utf-8").splitlines()
        if line.startswith("{")
    ]
    uses = {}
    results = {}
    for event in messages:
        for block in event.get("message", {}).get("content", []):
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use":
                uses[block["id"]] = block
            if block.get("type") == "tool_result":
                results[block["tool_use_id"]] = block
    launches = [use for use in uses.values() if "Workflow" in use.get("name", "")]
    assert launches, "Workflow was never launched"
    repo_roots = set()
    for launch in launches:
        args = launch.get("input", {}).get("args", {})
        if isinstance(args, str):
            args = json.loads(args)
        assert args.get("python") == "py -3", args
        for key in ("repoRoot", "outputDir", "pluginRoot"):
            root = args.get(key)
            assert isinstance(root, str) and re.match(r"^[A-Za-z]:/", root), (key, root)
            assert "\\" not in root, (key, root)
        repo_roots.add(args["repoRoot"])
    awaits = []
    materializations = []
    for ident, use in uses.items():
        command = use.get("input", {}).get("command", "")
        if "await_workflow.py" in command:
            words = shlex.split(command)
            target = words[words.index("--") + 1]
            assert not tasks.looks_like_path(target), (
                "awaiter must discover output by task id"
            )
            assert "--python" in words and words[words.index("--python") + 1] == "py -3"
            # The command may start with variable assignments, so check the words
            # that launch the script rather than the first words of the line.
            script = next(
                i for i, word in enumerate(words) if word.endswith("await_workflow.py")
            )
            assert words[script - 2 : script] == ["py", "-3"], (
                "live run did not exercise the launcher"
            )
            docs = checked_result(results[ident])
            for doc in docs:
                assert not doc.get("failingPhase"), doc
                if doc.get("ok") is True and doc.get("phaseReached"):
                    awaits.append((target, doc))
        if "materialize_artifacts.py" in command:
            materializations.extend(
                doc
                for doc in checked_result(results[ident])
                if doc.get("channel") == "return"
            )
    assert awaits, "no successful awaiter terminal result by task id"
    target, terminal = awaits[-1]
    assert terminal.get("phaseReached") == "report", terminal
    assert terminal.get("ok") is True, terminal
    assert not terminal.get("failingPhase"), terminal
    gaps = terminal.get("gaps", [])
    assert not any("invalid args" in gap or "args-rejected" in gap for gap in gaps), (
        gaps
    )
    assert terminal.get("stats", {}).get("inputProof", {}).get("proven", 0) >= 1, (
        "no verified inline checksum slice"
    )
    assert materializations, "no successful materialization receipt"
    materialized = materializations[-1]
    assert materialized.get("ok") is True and not materialized.get("errors"), (
        materialized
    )
    assert materialized.get("scanned") == 1, "materializer used nonce discovery"
    paths = list(terminal.get("artifactPaths", {}).values())
    paths += [entry["path"] for entry in materialized.get("materialized", [])]
    paths += [
        entry["path"] for entry in materialized.get("assemble", {}).get("written", [])
    ]
    assert len(paths) >= 4 and all(
        path and confined(path, str(output)) and Path(path).is_file() for path in paths
    ), paths
    report = Path(terminal["artifactPaths"]["report"])
    assert report.stat().st_size > 0
    # Disclosure requires every finding carrying a repository-root spelling to
    # be named in a host-path-text gap; if none carries one, the report must not
    # name the root.
    report_text = report.read_text(encoding="utf-8").lower()
    spellings = {
        spelling.lower()
        for root in repo_roots
        for spelling in (root, root.replace("/", "\\"), f"/{root[0]}/{root[3:]}")
    }
    findings = json.loads(
        Path(terminal["artifactPaths"]["findings"]).read_text(encoding="utf-8")
    )
    if isinstance(findings, dict):
        findings = findings.get("findings", [])
    carrying = {
        str(finding.get("id"))
        for finding in findings
        if any(
            isinstance(value, str) and any(s in value.lower() for s in spellings)
            for value in finding.values()
        )
    }
    disclosed = set(
        re.findall(r"host-path-text: finding (\S+) mentions", "\n".join(gaps))
    )
    assert carrying <= disclosed, (
        "undisclosed host path",
        sorted(carrying - disclosed),
    )
    assert carrying or not any(s in report_text for s in spellings), (
        "report names a host path outside any finding"
    )
    source = materialized.get("source")
    assert source and Path(source).stem == target, (target, source)
    observed = tasks.observe(target, tasks.roots_from_environment(os.environ))
    assert (
        observed.resolved_path
        and Path(observed.resolved_path).resolve() == Path(source).resolve()
    ), observed
    assert observed.terminal and observed.terminal.get("ok") is True, observed
    task_copy = transcript.parent / "workflow.output"
    shutil.copyfile(source, task_copy)
    for record in config.rglob("wf_*.json"):
        shutil.copyfile(record, transcript.parent / record.name)
    receipt: LiveReceipt = {
        "ok": True,
        "task_id": target,
        "task_output": source,
        "artifactPaths": terminal["artifactPaths"],
        "inputProof": terminal["stats"]["inputProof"],
        "materialize": materialized,
        "planted_file_mentioned": "defects.py" in report_text,
        "host_path_gaps": sum("host-path-text" in gap for gap in gaps),
    }
    (transcript.parent / "assertions.json").write_text(
        json.dumps(receipt, indent=2) + "\n", encoding="utf-8", newline="\n"
    )
    return receipt


def main(run: Path) -> None:
    if not os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
        raise SystemExit(
            "CLAUDE_CODE_OAUTH_TOKEN is required from environment windows-live"
        )
    run.mkdir(parents=True, exist_ok=True)
    fixture = run / "fixture"
    fixture.mkdir()
    source = fixture / "defects.py"
    source.write_text(
        "def lookup(items, key):\n    return items.get(key)\n",
        encoding="utf-8",
        newline="\n",
    )
    for argv in (
        ["git", "init", "-q"],
        ["git", "add", "defects.py"],
        [
            "git",
            "-c",
            "user.name=Windows Live",
            "-c",
            "user.email=live@example.invalid",
            "-c",
            "commit.gpgsign=false",
            "commit",
            "-qm",
            "fixture",
        ],
    ):
        subprocess.run(argv, cwd=fixture, check=True)
    source.write_text(
        "import os\nimport sqlite3\n\ndef lookup(items, key):\n    return items[key].lower()\n"
        '\ndef query(db, user):\n    return db.execute("SELECT * FROM users WHERE name=\'" + user + "\'")\n'
        "\ndef cleanup(user_path):\n    os.system('del /s /q ' + user_path)\n"
        "\ndef average(values):\n    return sum(values) / 0\n",
        encoding="utf-8",
        newline="\n",
    )
    stub_bin = run / "stubs"
    stub_bin.mkdir()
    for command in ("python3", "python"):
        stub = stub_bin / command
        stub.write_text("#!/bin/bash\nexit 49\n", encoding="utf-8", newline="\n")
        stub.chmod(0o755)
    output = run / "artifacts"
    config = run / "config"
    env = {
        **os.environ,
        "PATH": str(stub_bin) + os.pathsep + os.environ["PATH"],
        "CLAUDE_CONFIG_DIR": str(config),
        "CODE_GAUNTLET_HEADLESS": "1",
        "CODE_GAUNTLET_DELIVERY": "markdown",
        "CODE_GAUNTLET_OUTPUT_DIR": output.as_posix(),
        "CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS": "0",
    }
    transcript = run / "transcript.jsonl"
    prompt = "/code-gauntlet:code-gauntlet Review the uncommitted local changes. Use local changes mode without forge lookup. Before preflight record Bash pwd and pwd -W. Follow the skill through materialization."
    claude = shutil.which("claude")
    assert claude, "pinned Claude Code installation missing"
    with transcript.open("w", encoding="utf-8", newline="\n") as stdout:
        subprocess.run(
            [
                claude,
                "-p",
                prompt,
                "--verbose",
                "--output-format",
                "stream-json",
                "--dangerously-skip-permissions",
                "--plugin-dir",
                ROOT.as_posix(),
            ],
            cwd=fixture,
            env=env,
            stdout=stdout,
            stderr=subprocess.STDOUT,
            timeout=2700,
            check=True,
        )
    print(json.dumps(assert_run(transcript, output, config), indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dir", type=Path, help="scratch directory for the live run")
    main(parser.parse_args().run_dir.resolve())

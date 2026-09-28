"""Parent-recorded stdout bytes and exit codes for every script entry."""

import base64
import difflib
import importlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
RECORDING = Path(__file__).with_name("fixtures") / "cli_contract.json"
NAMES = (
    "assemble_artifacts",
    "await_workflow",
    "build_style_artifacts",
    "collect_project_rules",
    "detect_prior_review",
    "diff_numstat",
    "emit_style_context",
    "ensure_output_dir",
    "generate_contract_requirements",
    "materialize_artifacts",
    "post_review",
    "render_fix_tasks",
    "report_patches",
    "resolve_config",
    "resolve_pr_identity",
    "stale_truncate",
    "sync_agent_rules",
    "verify_findings",
    "write_shared_context",
)
MISSING = "{missing}"
INVALID = "{invalid}"
MAIN_FAILURE = {
    "assemble_artifacts": ("--plan", MISSING),
    "await_workflow": (
        MISSING,
        "--timeout-seconds",
        "0",
        "--since-epoch",
        "0",
        "--max-attempts",
        "1",
    ),
    "build_style_artifacts": ("--repo-root", MISSING, "--check"),
    "collect_project_rules": ("--repo-root", MISSING, "--out", MISSING),
    "detect_prior_review": ("--platform", "github", "--bodies-file", MISSING),
    "diff_numstat": (MISSING,),
    "emit_style_context": (),
    "ensure_output_dir": ("--cwd", MISSING),
    "generate_contract_requirements": ("--repo-root", MISSING, "--check"),
    "materialize_artifacts": ("--output-dir", MISSING, "--task", MISSING),
    "post_review": (MISSING,),
    "render_fix_tasks": (MISSING, "--repo-root", MISSING),
    "report_patches": ("--output-dir", MISSING, "--head-sha", "abcd"),
    "resolve_config": ("--target", "local", "--cwd", MISSING),
    "resolve_pr_identity": (
        "--platform",
        "github",
        "--url",
        MISSING,
        "--sha",
        "a" * 40,
    ),
    "stale_truncate": ("--output-dir", MISSING, "--head-sha", "abcd"),
    "sync_agent_rules": ("--repo-root", MISSING, "--check"),
    "verify_findings": ("--input", MISSING, "--output", MISSING),
    "write_shared_context": ("--output-dir", MISSING, "--head-sha", "abcd"),
}
BAD_INPUT = {
    "assemble_artifacts": ("--plan", INVALID),
    "await_workflow": (MISSING, "--attempt", "invalid"),
    "build_style_artifacts": ("--repo-root", INVALID, "--check"),
    "collect_project_rules": ("--repo-root", INVALID, "--out", MISSING),
    "detect_prior_review": ("--platform", "bitbucket"),
    "emit_style_context": ("--invalid",),
    "generate_contract_requirements": ("--repo-root", INVALID, "--check"),
    "materialize_artifacts": ("--output-dir", MISSING),
    "post_review": ("--platform", "bitbucket"),
    "render_fix_tasks": (INVALID, "--repo-root", "{root}"),
    "report_patches": ("--output-dir", MISSING, "--head-sha", "invalid"),
    "resolve_config": ("--target", "bitbucket"),
    "resolve_pr_identity": ("--platform", "bitbucket"),
    "stale_truncate": ("--output-dir", MISSING, "--head-sha", "invalid"),
    "sync_agent_rules": ("--repo-root", INVALID, "--check"),
    "verify_findings": ("--input", INVALID, "--output", MISSING),
    "write_shared_context": ("--output-dir", MISSING, "--head-sha", "invalid"),
}
# These commands have no second malformed-input branch beyond the failure row.
PATH_BEARING = set(NAMES) - {"diff_numstat", "resolve_pr_identity"}


def _success(name, directory, root):
    sha = "abc1234"
    if name == "assemble_artifacts":
        from tests.test_assemble_artifacts import _Workspace

        workspace = _Workspace()
        with patch("tempfile.mkdtemp", return_value=str(directory)):
            workspace.__enter__()
        return ("--plan", workspace.write_plan(workspace.plan()))
    if name == "await_workflow":
        from tests.test_await_workflow import SUCCESS_RETURN, envelope

        task = directory / "task.output"
        task.write_text(json.dumps(envelope(SUCCESS_RETURN)), encoding="utf-8")
        return (str(task), "--timeout-seconds", "0")
    if name == "build_style_artifacts":
        return ("--repo-root", str(root), "--check")
    if name == "collect_project_rules":
        return ("--repo-root", str(directory), "--out", str(directory / "rules.md"))
    if name == "detect_prior_review":
        bodies = directory / "bodies.json"
        bodies.write_text("[]", encoding="utf-8")
        return (
            "--platform",
            "github",
            "--bodies-file",
            str(bodies),
            "--head-sha",
            "a" * 40,
        )
    if name == "diff_numstat":
        patch_file = directory / "change.patch"
        patch_file.write_text(
            "--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-old\n+new\n", encoding="utf-8"
        )
        return (str(patch_file),)
    if name == "emit_style_context":
        return ()
    if name == "ensure_output_dir":
        return ("--cwd", str(ROOT))
    if name == "generate_contract_requirements":
        return ("--repo-root", str(root), "--check")
    if name == "materialize_artifacts":
        from tests.test_materialize_artifacts import record_task_output

        task, output = record_task_output(str(directory))
        return ("--output-dir", output, "--task", task)
    if name == "post_review":
        findings = directory / "post.json"
        findings.write_text(
            json.dumps(
                {
                    "platform": "github",
                    "owner": "o",
                    "repo": "r",
                    "pr_number": 5,
                    "sha": "a" * 40,
                    "findings": [],
                }
            ),
            encoding="utf-8",
        )
        return (str(findings), "--dry-run")
    if name == "render_fix_tasks":
        findings = directory / "post-review.json"
        findings.write_text("[]", encoding="utf-8")
        return (str(findings), "--repo-root", str(directory))
    if name == "report_patches":
        (directory / f"code-gauntlet-findings-{sha}.json").write_text(
            "[]", encoding="utf-8"
        )
        (directory / f"code-gauntlet-diff-{sha}.patch").write_text(
            "--- a/x\n+++ b/x\n@@ -1 +1 @@\n-a\n+b\n", encoding="utf-8"
        )
        return ("--output-dir", str(directory), "--head-sha", sha)
    if name == "resolve_config":
        return ("--target", "local", "--cwd", str(ROOT), "--plugin-root", str(root))
    if name == "resolve_pr_identity":
        return (
            "--platform",
            "github",
            "--url",
            "https://github.com/o/r/pull/5",
            "--sha",
            "a" * 40,
        )
    if name == "stale_truncate":
        (directory / f"code-gauntlet-report-{sha}.md").write_text(
            "stale", encoding="utf-8"
        )
        return ("--output-dir", str(directory), "--head-sha", sha, "--unconditional")
    if name == "sync_agent_rules":
        return ("--repo-root", str(root), "--check")
    if name == "verify_findings":
        findings = directory / "findings.json"
        diff = directory / "diff.patch"
        findings.write_text('{"findings": []}', encoding="utf-8")
        diff.write_text("--- a/x\n+++ b/x\n@@ -1 +1 @@\n-a\n+b\n", encoding="utf-8")
        return (str(findings), "--diff-file", str(diff))
    if name == "write_shared_context":
        (directory / f"code-gauntlet-project-rules-{sha}.md").write_text(
            "rules\n", encoding="utf-8"
        )
        (directory / f"code-gauntlet-diff-{sha}.patch").write_text(
            "diff\n", encoding="utf-8"
        )
        return ("--output-dir", str(directory), "--head-sha", sha)
    raise AssertionError(name)


def scenario(name, case, tmp_path, root=ROOT):
    directory = tmp_path / name / case
    directory.mkdir(parents=True, exist_ok=True)
    invalid = directory / "invalid.json"
    invalid.write_text("not json", encoding="utf-8")
    if name == "emit_style_context" and case == "failure":
        carrier = directory / "unreadable-carrier"
        carrier.write_text("x", encoding="utf-8")
        carrier.chmod(0)
    fake_bin = directory / "bin"
    fake_bin.mkdir(exist_ok=True)
    gh = fake_bin / "gh"
    gh.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    gh.chmod(0o755)
    if case == "success":
        argv = _success(name, directory, root)
    elif case == "usage":
        argv = ("--unknown",)
    elif case == "failure":
        argv = MAIN_FAILURE[name]
    elif case == "bad_input":
        argv = BAD_INPUT[name]
    elif case in ("pending", "artifacts_only"):
        argv = (
            str(directory / "pending.output"),
            "--timeout-seconds",
            "0",
            "--since-epoch",
            "0",
            "--max-attempts",
            "2",
        )
        if case == "artifacts_only":
            argv = ("wnosuchtask000", *argv[1:])
            from gauntlet.awaiting import ARTIFACT_BASENAMES

            for template in ARTIFACT_BASENAMES:
                (directory / template.format(sha="abc12345")).write_text(
                    "content", encoding="utf-8"
                )
            argv += (
                "--artifacts-dir",
                str(directory),
                "--head-sha",
                "abc12345",
                "--artifacts-grace-seconds",
                "0",
            )
    else:
        raise AssertionError(case)
    argv = tuple(
        str(directory / "missing")
        if arg == MISSING
        else str(invalid)
        if arg == INVALID
        else str(root)
        if arg == "{root}"
        else arg
        for arg in argv
    )
    return (
        directory,
        argv,
        b"triage\n"
        if name == "write_shared_context" and case == "success"
        else b"{}\n",
        fake_bin,
    )


def normalize(data, tmp_path, root):
    text = data.decode("utf-8", errors="surrogateescape")
    paths = (
        (os.path.realpath(tmp_path), "<TMP>"),
        (str(tmp_path), "<TMP>"),
        (os.path.realpath(root), "<ROOT>"),
        (str(root), "<ROOT>"),
        (os.path.realpath(ROOT), "<ROOT>"),
        (str(ROOT), "<ROOT>"),
    )
    for source, replacement in sorted(
        paths, key=lambda item: len(item[0]), reverse=True
    ):
        text = text.replace(source.replace("\\", "\\\\"), replacement).replace(
            source, replacement
        )
    text = re.sub(r"3\.\d+\.\d+", "<BUNDLE_VERSION>", text)
    text = re.sub(r"fnv1a32:0x[0-9a-f]{8}", "<CHECKSUM>", text)
    text = re.sub(r'("chars"\s*:\s*)\d+', r"\1<CHARS>", text)
    text = re.sub(r'("expected_chars"\s*:\s*)\d+', r"\1<CHARS>", text)
    text = re.sub(r'("waited_seconds"\s*:\s*)[0-9.]+', r"\1<ELAPSED>", text)
    text = re.sub(r'("searched"\s*:\s*)\[[^\n]*(?=,"gap")', r"\1<SEARCH_ROOTS>", text)
    text = re.sub(r"'<TMP>([^']*)'", r"<TMP>\1", text)
    return base64.b64encode(text.encode("utf-8", errors="surrogateescape")).decode(
        "ascii"
    )


def rows():
    for name in NAMES:
        for case in ("success", "usage", "failure", "bad_input"):
            if case == "bad_input" and name not in BAD_INPUT:
                continue
            yield name, case
    yield "await_workflow", "pending"
    yield "await_workflow", "artifacts_only"


@pytest.mark.parametrize(("name", "case"), list(rows()))
def test_recorded_cli(name, case, tmp_path, invoke, monkeypatch):
    if os.name == "nt" and name in PATH_BEARING and case != "usage":
        pytest.skip(
            "the parent POSIX path receipts cannot be compared with Windows paths"
        )
    directory, argv, stdin, fake_bin = scenario(name, case, tmp_path)
    monkeypatch.setenv("PATH", str(fake_bin) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("CODE_GAUNTLET_OUTPUT_DIR", str(directory / "output"))
    if name == "emit_style_context" and case == "failure":
        module = importlib.import_module("gauntlet.style_hook")
        monkeypatch.setattr(module, "CARRIER", str(directory / "unreadable-carrier"))
    result = invoke(name, argv, directory, stdin)
    expected = json.loads(RECORDING.read_text(encoding="utf-8"))[name][case]
    assert result.returncode == expected["code"]
    actual = normalize(result.stdout, tmp_path, ROOT)
    assert actual == expected["stdout"], "".join(
        difflib.unified_diff(
            base64.b64decode(expected["stdout"]).decode().splitlines(True),
            base64.b64decode(actual).decode().splitlines(True),
        )
    )
    if result.returncode and not result.stdout:
        assert result.stderr


def test_recording_covers_all_entry_files():
    assert set(NAMES) == {path.stem for path in (ROOT / "scripts").glob("*.py")}
    assert set(MAIN_FAILURE) == set(NAMES)
    assert set(BAD_INPUT) <= set(NAMES)
    assert len(list(rows())) == len(set(rows()))


@pytest.mark.parametrize("name", NAMES)
def test_entry_runs_from_foreign_cwd(name, tmp_path):
    if os.name == "nt" and name in PATH_BEARING:
        pytest.skip(
            "the parent POSIX path receipts cannot be compared with Windows paths"
        )
    directory, argv, stdin, fake_bin = scenario(name, "success", tmp_path)
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    env.update(
        PYTHONSAFEPATH="1",
        CODE_GAUNTLET_OUTPUT_DIR=str(directory / "output"),
        PATH=str(fake_bin) + os.pathsep + env["PATH"],
    )
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / f"{name}.py"), *argv],
        cwd=directory,
        input=stdin,
        env=env,
        capture_output=True,
        check=False,
        timeout=30,
    )
    expected = json.loads(RECORDING.read_text(encoding="utf-8"))[name]["success"]
    assert result.returncode == expected["code"], result.stderr
    actual = normalize(result.stdout, tmp_path, ROOT)
    assert actual == expected["stdout"], (
        base64.b64decode(actual),
        base64.b64decode(expected["stdout"]),
    )

"""Recorded stdout bytes and statuses for every retained script entry."""

import base64
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
RECORDING = Path(__file__).with_name("fixtures") / "cli_contract.json"
MAIN_RECORDING = Path(__file__).with_name("fixtures") / "cli_main_failure.json"
MISSING = "/nonexistent-gauntlet-contract-input"

# The first case is also the foreign-cwd smoke for each entry.
CASES = {
    "assemble_artifacts": ("--help", "--unknown", MISSING),
    "await_workflow": ("--help", "--unknown", (MISSING, "--artifacts-dir", MISSING)),
    "build_style_artifacts": ("--help", "--unknown", "--check"),
    "collect_project_rules": ("--help", "--unknown", MISSING),
    "detect_prior_review": ("--help", "--unknown", MISSING),
    "diff_numstat": ("--help", "--unknown", MISSING),
    "emit_style_context": ("--help", "--unknown", MISSING),
    "ensure_output_dir": (("--cwd", str(ROOT)), "--unknown", MISSING),
    "generate_contract_requirements": ("--help", "--unknown", "--check"),
    "materialize_artifacts": ("--help", "--unknown", MISSING),
    "post_review": ("--help", "--unknown", MISSING),
    "render_fix_tasks": ("--help", "--unknown", MISSING),
    "report_patches": ("--help", "--unknown", MISSING),
    "resolve_config": (("--target", "local", "--cwd", str(ROOT)), "--unknown", MISSING),
    "resolve_pr_identity": ("--help", "--unknown", MISSING),
    "stale_truncate": ("--help", "--unknown", MISSING),
    "sync_agent_rules": ("--help", "--unknown", "--check"),
    "verify_findings": ("--help", "--unknown", MISSING),
    "write_shared_context": ("--help", "--unknown", MISSING),
}

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

# A few legacy commands intentionally accept or ignore malformed arguments.
# Their recorded status pins that behavior until those commands convert.
BAD_INPUT = {
    "assemble_artifacts": ("--plan", "/dev/null"),
    "await_workflow": (MISSING, "--attempt", "invalid"),
    "build_style_artifacts": ("--repo-root", "/dev/null", "--check"),
    "collect_project_rules": ("--repo-root", "/dev/null", "--out", MISSING),
    "detect_prior_review": ("--platform", "bitbucket"),
    "diff_numstat": (MISSING,),
    "emit_style_context": ("--invalid",),
    "ensure_output_dir": ("--cwd", MISSING),
    "generate_contract_requirements": ("--repo-root", "/dev/null", "--check"),
    "materialize_artifacts": ("--output-dir", MISSING),
    "post_review": ("--platform", "bitbucket"),
    "render_fix_tasks": ("/dev/null", "--repo-root", str(ROOT)),
    "report_patches": ("--output-dir", MISSING, "--head-sha", "invalid"),
    "resolve_config": ("--target", "bitbucket"),
    "resolve_pr_identity": ("--platform", "bitbucket"),
    "stale_truncate": ("--output-dir", MISSING, "--head-sha", "invalid"),
    "sync_agent_rules": ("--repo-root", "/dev/null", "--check"),
    "verify_findings": ("--input", "/dev/null", "--output", MISSING),
    "write_shared_context": ("--output-dir", MISSING, "--head-sha", "invalid"),
}
BAD_RECORDING = Path(__file__).with_name("fixtures") / "cli_bad_input.json"


def _run(name, argument, cwd):
    arguments = (argument,) if isinstance(argument, str) else argument
    environment = dict(os.environ)
    environment.pop("PYTHONPATH", None)
    environment["PYTHONSAFEPATH"] = "1"
    if name == "ensure_output_dir":
        environment["CODE_GAUNTLET_OUTPUT_DIR"] = (
            "/private/tmp/s407-cli-contract-output"
        )
    return subprocess.run(
        [sys.executable, str(ROOT / "scripts" / f"{name}.py"), *arguments],
        cwd=cwd,
        input=b"{}\n",
        env=environment,
        capture_output=True,
        check=False,
        timeout=8,
    )


@pytest.mark.parametrize("name", CASES)
@pytest.mark.parametrize("case", ("success", "usage", "failure"))
def test_recorded_cli(name, case, tmp_path, invoke):
    arguments = dict(zip(("success", "usage", "failure"), CASES[name], strict=True))
    argument = arguments[case]
    if case == "success":
        result = _run(name, argument, tmp_path)
    else:
        argv = (argument,) if isinstance(argument, str) else argument
        result = invoke(name, argv, tmp_path)
    expected = json.loads(RECORDING.read_text(encoding="utf-8"))[name][case]
    assert result.returncode == expected["code"]
    assert result.stdout == base64.b64decode(expected["stdout"])
    if result.returncode:
        assert result.stderr


def test_recording_covers_all_entry_files():
    import ast

    discovered = {
        path.stem
        for path in (ROOT / "scripts").glob("*.py")
        if any(
            (isinstance(node, ast.If) and "__main__" in ast.unparse(node.test))
            or (isinstance(node, ast.Expr) and ast.unparse(node).strip() == "CLI.run()")
            for node in ast.parse(path.read_text(encoding="utf-8")).body
        )
    }
    assert set(CASES) == set(MAIN_FAILURE) == set(BAD_INPUT) == discovered


@pytest.mark.parametrize("name", MAIN_FAILURE)
def test_recorded_main_failure(name, tmp_path, invoke, monkeypatch):
    expected = json.loads(MAIN_RECORDING.read_text(encoding="utf-8"))[name]
    if name == "emit_style_context":
        import gauntlet.emit_style_context as hook

        monkeypatch.setattr(hook, "CARRIER", MISSING)
        result = invoke(name, (), tmp_path)
    else:
        result = _run(name, MAIN_FAILURE[name], tmp_path)
    assert result.returncode == expected["code"]
    assert result.stdout == base64.b64decode(expected["stdout"])
    if result.returncode and not result.stdout:
        assert result.stderr


@pytest.mark.parametrize("name", BAD_INPUT)
def test_recorded_bad_input(name, tmp_path):
    expected = json.loads(BAD_RECORDING.read_text(encoding="utf-8"))[name]
    result = _run(name, BAD_INPUT[name], tmp_path)
    assert result.returncode == expected["code"]
    assert result.stdout == base64.b64decode(expected["stdout"])
    if result.returncode and not result.stdout:
        assert result.stderr


@pytest.mark.parametrize(
    ("settings", "prefix", "code"),
    [
        ({}, b"Resolved config:\n", 0),
        ({"CODE_GAUNTLET_HEADLESS": "1"}, b"Headless config:\n", 0),
        (
            {"CODE_GAUNTLET_HEADLESS": "1", "CODE_GAUNTLET_MODEL_TIER": "bad"},
            b"HEADLESS CONFIG ERROR:",
            1,
        ),
    ],
)
def test_machine_parsed_resolver_stderr(settings, prefix, code, tmp_path):
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("CODE_GAUNTLET_") and key != "PYTHONPATH"
    }
    environment.update(settings)
    environment["PYTHONSAFEPATH"] = "1"
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/resolve_config.py"),
            "--target",
            "local",
            "--cwd",
            str(ROOT),
        ],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        check=False,
    )
    assert result.returncode == code
    assert result.stderr.startswith(prefix)

"""Parent-recorded stdout bytes and exit codes for every script entry."""

import importlib
import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from gauntlet.paths import ENTRY_ROOT

ROOT = Path(__file__).resolve().parents[1]
RECORDED = json.loads(
    (Path(__file__).with_name("fixtures") / "cli_contract.json").read_text(
        encoding="utf-8"
    )
)
SHA = "abc1234"
FULL = "a" * 40
PATCH = "--- a/x\n+++ b/x\n@@ -1 +1 @@\n-a\n+b\n"
# argv strings split on spaces before placeholders expand, so paths may hold spaces.
MAIN_FAILURE = {
    "assemble_artifacts": "--plan {missing}",
    "await_workflow": "{missing} --timeout-seconds 0 --since-epoch 0 --max-attempts 1",
    "build_style_artifacts": "--repo-root {missing} --check",
    "collect_project_rules": "--repo-root {missing} --out {missing}",
    "detect_prior_review": "--platform github --bodies-file {missing}",
    "diff_numstat": "{missing}",
    "emit_style_context": "",
    "ensure_output_dir": "--cwd {missing}",
    "generate_contract_requirements": "--repo-root {missing} --check",
    "materialize_artifacts": "--output-dir {missing} --task {missing}",
    "post_review": "{missing}",
    "render_fix_tasks": "{missing} --repo-root {missing}",
    "report_patches": "--output-dir {missing} --head-sha abcd",
    "resolve_config": "--target local --cwd {missing}",
    "resolve_pr_identity": f"--platform github --url {{missing}} --sha {FULL}",
    "stale_truncate": "--output-dir {missing} --head-sha abcd",
    "sync_agent_rules": "--repo-root {missing} --check",
    "verify_findings": "--input {missing} --output {missing}",
    "write_shared_context": "--output-dir {missing} --head-sha abcd",
}
# diff_numstat, emit_style_context and ensure_output_dir have no malformed-input
# branch beyond the failure row.
BAD_INPUT = {
    "assemble_artifacts": "--plan {invalid}",
    "await_workflow": "{missing} --attempt invalid",
    "build_style_artifacts": "--repo-root {invalid} --check",
    "collect_project_rules": "--repo-root {invalid} --out {missing}",
    "detect_prior_review": "--platform bitbucket",
    "generate_contract_requirements": "--repo-root {invalid} --check",
    "materialize_artifacts": "--output-dir {missing}",
    "post_review": "--platform bitbucket",
    "render_fix_tasks": "{invalid} --repo-root {root}",
    "report_patches": "--output-dir {missing} --head-sha invalid",
    "resolve_config": "--target bitbucket",
    "resolve_pr_identity": "--platform bitbucket",
    "stale_truncate": "--output-dir {missing} --head-sha invalid",
    "sync_agent_rules": "--repo-root {invalid} --check",
    "verify_findings": "--input {invalid} --output {missing}",
    "write_shared_context": "--output-dir {missing} --head-sha invalid",
}
POST = {"platform": "github", "owner": "o", "repo": "r", "pr_number": 5, "sha": FULL}
# Each success row is (files written under the case directory, argv).
SUCCESS = {
    "build_style_artifacts": ({}, "--repo-root {root} --check"),
    "collect_project_rules": ({}, "--repo-root {dir} --out {dir}/rules.md"),
    "detect_prior_review": (
        {"bodies.json": "[]"},
        f"--platform github --bodies-file {{dir}}/bodies.json --head-sha {FULL}",
    ),
    "diff_numstat": ({"change.patch": PATCH}, "{dir}/change.patch"),
    "emit_style_context": ({}, ""),
    "ensure_output_dir": ({}, "--cwd {root}"),
    "generate_contract_requirements": ({}, "--repo-root {root} --check"),
    "post_review": (
        {"post.json": json.dumps({**POST, "findings": []})},
        "{dir}/post.json --dry-run",
    ),
    "render_fix_tasks": ({"p.json": "[]"}, "{dir}/p.json --repo-root {dir}"),
    "report_patches": (
        {
            f"code-gauntlet-findings-{SHA}.json": "[]",
            f"code-gauntlet-diff-{SHA}.patch": PATCH,
        },
        f"--output-dir {{dir}} --head-sha {SHA}",
    ),
    "resolve_config": ({}, "--target local --cwd {root} --plugin-root {root}"),
    "resolve_pr_identity": (
        {},
        f"--platform github --url https://github.com/o/r/pull/5 --sha {FULL}",
    ),
    "stale_truncate": (
        {f"code-gauntlet-report-{SHA}.md": "stale"},
        f"--output-dir {{dir}} --head-sha {SHA} --unconditional",
    ),
    "sync_agent_rules": ({}, "--repo-root {root} --check"),
    "verify_findings": (
        {"findings.json": '{"findings": []}', "diff.patch": PATCH},
        "{dir}/findings.json --diff-file {dir}/diff.patch",
    ),
    "write_shared_context": (
        {
            f"code-gauntlet-project-rules-{SHA}.md": "rules\n",
            f"code-gauntlet-diff-{SHA}.patch": "diff\n",
        },
        f"--output-dir {{dir}} --head-sha {SHA}",
    ),
}
AWAIT = "--timeout-seconds 0 --since-epoch 0 --max-attempts 2"
REQUIRED_ONLY = {
    "diff_numstat": "",
    "materialize_artifacts": "--output-dir {dir}",
    "stale_truncate": "--output-dir {dir}",
}
WINDOWS_PATHS = "the parent POSIX path receipts cannot be compared with Windows paths"
WINDOWS_EXCEPTIONS = {
    ("emit_style_context", "failure"): "chmod 0 does not deny reads on Windows",
    (
        "write_shared_context",
        "success",
    ): "the parent write_text input uses CRLF on Windows",
}


def _windows_skip(name, case, *, platform=None):
    if (os.name if platform is None else platform) != "nt":
        return None
    if (name, case) in WINDOWS_EXCEPTIONS:
        return WINDOWS_EXCEPTIONS[name, case]
    recorded_stdout = RECORDED[f"{name}/{case}"][1]
    if "<TMP>" in recorded_stdout or "<ROOT>" in recorded_stdout:
        return WINDOWS_PATHS
    return None


@pytest.mark.parametrize(
    ("name", "case"),
    (
        ("build_style_artifacts", "success"),
        ("emit_style_context", "success"),
        ("generate_contract_requirements", "success"),
        ("detect_prior_review", "bad_input"),
    ),
)
def test_windows_keeps_path_free_contract_rows(name, case):
    assert _windows_skip(name, case, platform="nt") is None


@pytest.mark.parametrize(
    ("name", "case"),
    (
        ("emit_style_context", "failure"),
        ("write_shared_context", "success"),
        ("await_workflow", "pending"),
    ),
)
def test_windows_skips_only_named_exceptions_or_path_receipts(name, case):
    assert _windows_skip(name, case, platform="nt")


def _built_success(name, directory):
    if name == "assemble_artifacts":
        from tests.test_assemble_artifacts import _Workspace

        workspace = _Workspace()
        with patch("tempfile.mkdtemp", return_value=str(directory)):
            workspace.__enter__()
        return ["--plan", workspace.write_plan(workspace.plan())]
    if name == "await_workflow":
        from tests.test_await_workflow import SUCCESS_RETURN, envelope

        task = directory / "task.output"
        task.write_text(json.dumps(envelope(SUCCESS_RETURN)), encoding="utf-8")
        return [str(task), "--timeout-seconds", "0"]
    from tests.test_materialize_artifacts import record_task_output

    task, output = record_task_output(str(directory))
    return ["--output-dir", output, "--task", task]


def _command_line(name, case, directory):
    if case == "success":
        files, command_line = SUCCESS[name]
        for relative, text in files.items():
            (directory / relative).write_text(text, encoding="utf-8")
        return command_line
    if case == "artifacts_only":
        from gauntlet.awaiting import ARTIFACT_BASENAMES

        for template in ARTIFACT_BASENAMES:
            (directory / template.format(sha="abc12345")).write_text(
                "x", encoding="utf-8"
            )
        return f"wnosuchtask000 {AWAIT} --artifacts-dir {{dir}} --head-sha abc12345 --artifacts-grace-seconds 0"
    return {
        "help": "--help",
        "usage": "--unknown",
        "failure": MAIN_FAILURE.get(name),
        "bad_input": BAD_INPUT.get(name),
        "required_only": REQUIRED_ONLY.get(name),
        "pending": f"{{dir}}/pending.output {AWAIT}",
    }[case]


def scenario(name, case, tmp_path):
    directory = tmp_path / name / case
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "invalid.json").write_text("not json", encoding="utf-8")
    fake_bin = directory / "bin"
    fake_bin.mkdir(exist_ok=True)
    (fake_bin / "gh").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    (fake_bin / "gh").chmod(0o755)
    if case == "success" and name not in SUCCESS:
        argv = _built_success(name, directory)
    else:
        places = {"dir": directory, "root": ROOT, "missing": directory / "missing"}
        places["invalid"] = directory / "invalid.json"
        argv = [
            arg.format(**places) for arg in _command_line(name, case, directory).split()
        ]
    triage = (name, case) == ("write_shared_context", "success")
    return directory, argv, b"triage\n" if triage else b"{}\n", fake_bin


def normalize(data, tmp_path, root=ROOT):
    text = data.decode("utf-8", errors="surrogateescape")
    carrier = (ROOT / "docs/style/session-context.md").read_text(encoding="utf-8")
    text = text.replace(json.dumps(carrier.partition("\n\n")[2])[1:-1], "<CARRIER>")
    paths = {
        os.path.realpath(tmp_path): "<TMP>",
        str(tmp_path): "<TMP>",
        os.path.realpath(root): "<ROOT>",
        str(root): "<ROOT>",
        ENTRY_ROOT: "<ROOT>",
        str(Path(__file__).absolute().parents[1]): "<ROOT>",
    }
    for source, replacement in sorted(
        paths.items(),
        key=lambda item: len(item[0]),
        reverse=True,
    ):
        for spelling in (source.replace("\\", "\\\\"), source):
            text = re.sub(
                re.escape(spelling) + r"(?=$|[/\\'\"\s])",
                lambda match, value=replacement: value,
                text,
            )
    for pattern, replacement in (
        (
            r'(pipeline_version=|"pipeline_version"\s*:\s*")3\.\d+\.\d+',
            r"\1<BUNDLE_VERSION>",
        ),
        (r"fnv1a32:0x[0-9a-f]{8}", "<CHECKSUM>"),
        (r'("(?:expected_)?chars"\s*:\s*)\d+', r"\1<CHARS>"),
        (r'("waited_seconds"\s*:\s*)[0-9.]+', r"\1<ELAPSED>"),
        (r'("searched"\s*:\s*)\[[^\n]*(?=,"gap")', r"\1<SEARCH_ROOTS>"),
        (r"'(<(?:TMP|ROOT)>[^']*)'", r"\1"),
    ):
        text = re.sub(pattern, replacement, text)
    return text


@pytest.mark.parametrize("suffix", ("-link", " space[1]"))
def test_normalize_path_boundaries_and_shell_quotes(tmp_path, suffix):
    root = tmp_path / f"checkout{suffix}"
    data = f"python3 '{root}/scripts/await_workflow.py' {root}x".encode()
    assert normalize(data, tmp_path, root) == (
        f"python3 <ROOT>/scripts/await_workflow.py <TMP>{os.sep}checkout{suffix}x"
    )


@pytest.mark.parametrize("kind", ("symlink", "space"))
def test_pending_receipt_normalizes_noncanonical_root(
    kind, tmp_path, invoke, monkeypatch, request
):
    from gauntlet import paths

    alias = tmp_path / ("plugin-link" if kind == "symlink" else "plugin space[1]")
    if kind == "symlink":
        request.getfixturevalue("symlink_or_skip")
        alias.symlink_to(ROOT, target_is_directory=True)
    else:
        alias.mkdir()
    monkeypatch.setattr(paths, "ENTRY_ROOT", str(alias))
    directory, argv, stdin, _ = scenario("await_workflow", "pending", tmp_path)
    result = invoke("await_workflow", argv, directory, stdin)
    assert result.returncode == 3
    if os.name == "nt":
        marker = json.loads(result.stdout)
        assert shlex.split(marker["next_command"])[1] == str(
            alias / "scripts" / "await_workflow.py"
        )
    else:
        assert (
            normalize(result.stdout, tmp_path, alias)
            == RECORDED["await_workflow/pending"][1]
        )


def rows():
    for name in MAIN_FAILURE:
        yield from ((name, case) for case in ("success", "usage", "failure"))
        if name in BAD_INPUT:
            yield name, "bad_input"
    yield from (
        (name, "help")
        for name in ("diff_numstat", "stale_truncate", "write_shared_context")
    )
    yield "await_workflow", "pending"
    yield "await_workflow", "artifacts_only"
    yield from ((name, "required_only") for name in REQUIRED_ONLY)


def _assert_recorded(name, case, returncode, stdout, tmp_path):
    code, expected = RECORDED[f"{name}/{case}"]
    assert (returncode, normalize(stdout, tmp_path)) == (code, expected)


@pytest.mark.parametrize(("name", "case"), list(rows()))
def test_recorded_cli(name, case, tmp_path, invoke, monkeypatch):
    if reason := _windows_skip(name, case):
        pytest.skip(reason)
    monkeypatch.setenv("COLUMNS", "100")
    directory, argv, stdin, fake_bin = scenario(name, case, tmp_path)
    monkeypatch.setenv("PATH", str(fake_bin) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("CODE_GAUNTLET_OUTPUT_DIR", str(directory / "output"))
    if name == "emit_style_context" and case == "failure":
        carrier = directory / "unreadable-carrier"
        carrier.write_text("x", encoding="utf-8")
        carrier.chmod(0)
        module = importlib.import_module("gauntlet.style_hook")
        monkeypatch.setattr(module, "CARRIER", str(carrier))
    result = invoke(name, argv, directory, stdin)
    _assert_recorded(name, case, result.returncode, result.stdout, tmp_path)
    if result.returncode and not result.stdout and result.converted:
        assert re.fullmatch(rf"{name}: [^\n]+\n", result.stderr.decode())
    elif result.returncode and not result.stdout:
        assert result.stderr


def test_recording_covers_all_entry_files():
    assert set(MAIN_FAILURE) == {path.stem for path in (ROOT / "scripts").glob("*.py")}
    assert set(BAD_INPUT) | set(SUCCESS) <= set(MAIN_FAILURE)
    assert sorted(f"{name}/{case}" for name, case in rows()) == sorted(RECORDED)


@pytest.mark.parametrize("name", MAIN_FAILURE)
def test_entry_runs_from_foreign_cwd(name, tmp_path):
    if reason := _windows_skip(name, "success"):
        pytest.skip(reason)
    directory, argv, stdin, fake_bin = scenario(name, "success", tmp_path)
    env = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
    env.update(
        PYTHONSAFEPATH="1",
        PYTHONIOENCODING="latin-1",
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
    _assert_recorded(name, "success", result.returncode, result.stdout, tmp_path)

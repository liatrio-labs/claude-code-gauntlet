"""Parent-recorded stdout bytes and exit codes for every script entry."""

import importlib
import io
import json
import os
import re
import shlex
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from unittest.mock import patch

import pytest
from gauntlet import cli, marker, patches, prior_review, proc
from gauntlet.forge import GitHub, JsonFetch
from gauntlet.paths import ENTRY_ROOT

from tests.conftest import Invocation
from tests.support.forge import FakeForge, FakeGitLab, ForgeCall
from tests.test_forge import HOST_CASES

ROOT = Path(__file__).resolve().parents[1]
RECORDED = json.loads(
    (Path(__file__).with_name("fixtures") / "cli_contract.json").read_text(
        encoding="utf-8"
    )
)
SHA = "abc1234"
FULL = "a" * 40


@pytest.mark.usefixtures("poster_state")
@pytest.mark.parametrize(
    "mode, flag, dry_run",
    [
        pytest.param(None, False, False, id="default-live"),
        pytest.param("live", False, False, id="env-live"),
        pytest.param("dry-run", False, True, id="env-dry-run"),
        pytest.param(None, True, True, id="flag-dry-run"),
        pytest.param("live", True, True, id="flag-beats-env-live"),
    ],
)
def test_poster_mode_stdout(
    mode, flag, dry_run, tmp_path, invoke, forge_factory, monkeypatch
):
    data = {
        "platform": "github",
        "owner": "o",
        "repo": "r",
        "pr_number": 5,
        "sha": FULL,
        "review_body": "Summary",
        "findings": [
            {
                "file": "foo.py",
                "line": 2,
                "severity": "high",
                "title": "Bug A",
                "body": "Body A",
            }
        ],
    }
    path = tmp_path / "findings.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    fake = forge_factory.configure(
        FakeForge(
            diffs=[
                (
                    "diff --git a/foo.py b/foo.py\n--- a/foo.py\n+++ b/foo.py\n@@ -1,1 +1,2 @@\n context\n+added\n",
                    "",
                    0,
                )
            ]
        )
    )
    if mode is None:
        monkeypatch.delenv("CODE_GAUNTLET_POST_MODE", raising=False)
    else:
        monkeypatch.setenv("CODE_GAUNTLET_POST_MODE", mode)
    monkeypatch.setattr(proc, "run", lambda *a, **k: pytest.fail("Unexpected Git call"))
    result = invoke(
        "post_review", [str(path)] + (["--dry-run"] if flag else []), tmp_path
    )
    assert result.returncode == 0
    assert result.stderr == b""
    assert forge_factory.calls == ["github"]
    writes = [call for call in fake.calls if call.method == "submit"]
    artifact = tmp_path / "post-review-payload.json"
    if dry_run:
        assert writes == []
        assert artifact.exists()
        payload = json.loads(artifact.read_text(encoding="utf-8"))
        assert payload["platform"] == "github"
        assert payload["payload"]["comments"][0]["line"] == 2
        assert b"captured" in result.stdout
        assert b"Review posted:" not in result.stdout
        assert b"inline comment(s) posted." not in result.stdout
    else:
        assert len(writes) == 1
        assert writes[0].request.endpoint == "repos/o/r/pulls/5/reviews"
        assert writes[0].request.payload["event"] == "COMMENT"
        assert writes[0].request.payload["comments"][0]["line"] == 2
        assert not artifact.exists()
        assert b"Review posted:" in result.stdout
        assert b"inline comment(s) posted." in result.stdout
        assert b"captured" not in result.stdout
    assert fake.calls[0] == ForgeCall("diff", fake.calls[0].target)


def _git_result(command, **kwargs):
    assert command[0] == "git"
    if command[1] == "rev-parse":
        return proc.CompletedProcess(
            command, 0, ("b" * 40 if command[-1] == "HEAD" else command[-1]) + "\n", ""
        )
    if command[1:3] in (["cat-file", "-e"], ["merge-base", "--is-ancestor"]):
        return proc.CompletedProcess(command, 0, "", "")
    if command[1:3] == ["rev-list", "--count"]:
        return proc.CompletedProcess(command, 0, "3\n", "")
    raise AssertionError(f"Unexpected Git call: {command}")


@pytest.mark.parametrize("case", ["offline", "unicode-marker", "unicode-read-error"])
def test_detector_receipt(case, tmp_path, invoke, monkeypatch):
    path = tmp_path / "caf\u00e9-missing.json"
    version = "3.0 caf\u00e9 \U0001f41b"
    unknown_key = "future_\u00e9"
    payload = {
        "version": version,
        "findings_count": 1,
        "sha": FULL,
        unknown_key: "ignored",
    }
    body = (
        f"<!-- {marker.MARKER_TOKEN}: {json.dumps(payload)} -->"
        if case == "unicode-marker"
        else marker.build_marker(FULL, 1)
    )
    if case != "unicode-read-error":
        path.write_text(
            json.dumps(
                [
                    {
                        "body": body,
                        "timestamp": "2026-01-01T00:00:00Z",
                        "source": "review",
                        "id": 101,
                    }
                ]
            ),
            encoding="utf-8",
        )
    monkeypatch.setattr(proc, "run", _git_result)
    result = invoke(
        "detect_prior_review",
        ["--platform", "github", "--bodies-file", str(path)],
        tmp_path,
    )
    assert result.returncode == 0
    assert result.stderr == b""
    assert result.stdout.isascii()
    assert result.stdout.endswith(b"\n")
    text = result.stdout.decode().strip()
    receipt, end = json.JSONDecoder().raw_decode(text)
    assert isinstance(receipt, dict)
    assert end == len(text)
    if case == "unicode-read-error":
        assert receipt["previously_reviewed"] is False
        assert receipt["errors"]
    else:
        assert receipt["previously_reviewed"] is True
        if case == "unicode-marker":
            assert receipt["marker"]["version"] == version
            assert receipt["marker"]["unknown_keys"] == [unknown_key]


@pytest.mark.parametrize(
    "argv, code, error",
    [
        pytest.param(
            ["--owner", "o", "--repo", "r", "--number", "5"],
            2,
            "the following arguments are required: --platform",
            id="missing-platform",
        ),
        pytest.param(["--platform", "github"], 0, None, id="missing-number"),
        pytest.param(
            ["--platform", "github", "--nope", "wat"],
            2,
            "unrecognized arguments: --nope wat",
            id="unknown-flag",
        ),
    ],
)
def test_detector_usage(argv, code, error, tmp_path, invoke, monkeypatch):
    monkeypatch.setattr(proc, "run", _git_result)
    result = invoke("detect_prior_review", argv, tmp_path)
    assert result.returncode == code
    if error is not None:
        assert result.stdout == b""
        assert result.stderr == f"detect_prior_review: {error}\n".encode()
    else:
        receipt = json.loads(result.stdout)
        assert receipt["previously_reviewed"] is False
        assert receipt["errors"] == [
            "usage: --number is required unless --bodies-file is given"
        ]


def test_identity_unicode_receipt(tmp_path, invoke):
    result = invoke(
        "resolve_pr_identity",
        [
            "--platform",
            "github",
            "--url",
            "https://github.com/OpenAI/codex/pull/278",
            "--sha",
            FULL,
            "--title",
            "A caf\u00e9 fix",
        ],
        tmp_path,
    )
    assert result.returncode == 0
    assert result.stderr == b""
    assert result.stdout == (
        b'{"owner": "OpenAI", "repo": "codex", "pr_number": 278, "sha_full": "'
        + FULL.encode()
        + b'", "platform": "github", "web_origin": "https://github.com", "title": "A caf\\u00e9 fix"}\n'
    )


@pytest.mark.parametrize(
    "url, sha, error",
    [
        pytest.param(
            "https://gitlab.com/a/r/-/merge_requests/3",
            FULL,
            "URL path does not match a github PR URL",
            id="wrong-platform",
        ),
        pytest.param(
            "https://github.com/a/r/pull/" + "9" * 5000,
            FULL,
            "PR/MR number must be a positive safe integer",
            id="overlong-number",
        ),
    ],
)
def test_identity_data_error(url, sha, error, tmp_path, invoke):
    result = invoke(
        "resolve_pr_identity",
        ["--platform", "github", "--url", url, "--sha", sha],
        tmp_path,
    )
    assert result.returncode == 2
    assert result.stdout == b""
    assert result.stderr == f"resolve_pr_identity: {error}\n".encode()


@pytest.mark.parametrize(
    "name, argv",
    [
        pytest.param("detect_prior_review", ["--platform", "github"], id="detector"),
        pytest.param(
            "resolve_pr_identity",
            [
                "--platform",
                "github",
                "--url",
                "https://github.com/o/r/pull/5",
                "--sha",
                FULL,
            ],
            id="identity",
        ),
    ],
)
def test_converted_serialization_fallback(
    name: str,
    argv: list[str],
    tmp_path: Path,
    invoke: Callable[[str, list[str], Path], Invocation],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(*args: object, **kwargs: object) -> str:
        raise TypeError("injected serialization failure")

    monkeypatch.setattr(proc, "run", _git_result)
    monkeypatch.setattr(cli, "dumps", fail)
    result = invoke(name, argv, tmp_path)
    assert result.returncode == 1
    assert (
        result.stdout == b'{"ok": false, "errors": ["receipt serialization failed"]}\n'
    )
    assert result.stderr == b""


@pytest.mark.parametrize(
    "token",
    ["NaN", "Infinity", "-Infinity", "1e999", "-1e999", "1E999"],
    ids=[
        "nan",
        "positive-infinity",
        "negative-infinity",
        "positive-overflow",
        "negative-overflow",
        "uppercase-exponent-overflow",
    ],
)
def test_detector_nonfinite_marker_exit_zero(token, tmp_path, invoke, monkeypatch):
    body = (
        '<!-- code-gauntlet-findings: {"sha":"'
        + FULL
        + '","findings_count":'
        + token
        + "} -->"
    )
    path = tmp_path / "bodies.json"
    path.write_text(json.dumps([{"body": body}]), encoding="utf-8")
    monkeypatch.setattr(proc, "run", _git_result)
    result = invoke(
        "detect_prior_review",
        ["--platform", "github", "--bodies-file", str(path)],
        tmp_path,
    )
    assert result.returncode == 0
    assert result.stderr == b""

    def reject_constant(value):
        raise ValueError(value)

    receipt = json.loads(result.stdout, parse_constant=reject_constant)
    assert receipt["marker"] is None
    assert receipt["previously_reviewed"] is False
    assert receipt["errors"] == []
    assert (
        result.stdout != b'{"ok": false, "errors": ["receipt serialization failed"]}\n'
    )


def test_detector_real_child_degradation(tmp_path, invoke, monkeypatch):
    real_run = proc.run
    calls = []
    child = r"import sys; sys.stdout.buffer.write(b'\xff\xfe' + b'not json')"

    def child_run(command, **kwargs):
        if command[0] == "git":
            return _git_result(command, **kwargs)
        calls.append((command, kwargs))
        return real_run([sys.executable, "-c", child], **kwargs)

    monkeypatch.setattr(proc, "run", child_run)
    monkeypatch.setattr(prior_review, "make_forge", lambda platform: GitHub())
    result = invoke(
        "detect_prior_review",
        ["--platform", "github", "--owner", "o", "--repo", "r", "--number", "5"],
        tmp_path,
    )
    obj, end = json.JSONDecoder().raw_decode(result.stdout.decode().strip())
    assert result.returncode == 0
    assert end == len(result.stdout.decode().strip())
    assert obj["previously_reviewed"] is False
    assert obj["errors"] == [
        "github reviews: response was not JSON: \ufffd\ufffdnot json"
    ]
    assert calls == [
        (
            ["gh", "api", "--paginate", "repos/o/r/pulls/5/reviews"],
            {"cwd": None, "timeout": 30, "errors": "replace"},
        )
    ]


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


def scenario(name, case, tmp_path, *, shell_fixture=False):
    directory = tmp_path / name / case
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "invalid.json").write_text("not json", encoding="utf-8")
    fake_bin = directory / "bin"
    fake_bin.mkdir(exist_ok=True)
    if shell_fixture and name == "post_review":
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
def test_recorded_cli(name, case, tmp_path, invoke, monkeypatch, forge_factory):
    forge_factory.configure(FakeForge(diffs=[("", "", 0)]))
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
    directory, argv, stdin, fake_bin = scenario(
        name, "success", tmp_path, shell_fixture=True
    )
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


def _gitlab_fake() -> FakeGitLab:
    return FakeGitLab(
        diffs=[("", "", 0)],
        refs=[
            JsonFetch(
                [
                    {
                        "base_commit_sha": "base",
                        "head_commit_sha": "head",
                        "start_commit_sha": "start",
                    }
                ],
                None,
            )
        ],
    )


@pytest.mark.parametrize("remote, status, expected", HOST_CASES)
def test_poster_auto_detection(
    remote, status, expected, tmp_path, invoke, forge_factory, monkeypatch
):
    data: dict[str, object] = {
        "owner": "o",
        "repo": "r",
        "pr_number": 5,
        "sha": FULL,
        "findings": [],
    }
    path = tmp_path / "findings.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    platform, host = expected
    fake = FakeForge(diffs=[("", "", 0)]) if platform == "github" else _gitlab_fake()
    forge_factory.configure(fake)
    origins: list[list[str]] = []

    def run(command, **kwargs):
        assert command == ["git", "remote", "get-url", "origin"]
        assert kwargs == {"cwd": None, "timeout": None, "errors": "strict"}
        origins.append(command)
        return proc.CompletedProcess(command, status, remote, "")

    monkeypatch.setattr(proc, "run", run)
    monkeypatch.delenv("CODE_GAUNTLET_POST_MODE", raising=False)
    result = invoke("post_review", [str(path), "--dry-run"], tmp_path)
    assert origins == [["git", "remote", "get-url", "origin"]]
    artifact = tmp_path / "post-review-payload.json"
    if platform:
        assert result.returncode == 0
        assert result.stderr == b""
        assert result.stdout.startswith(
            f"Detected platform: {platform} (from git remote: {host})\n".encode()
        )
        assert forge_factory.calls == [platform]
        assert json.loads(artifact.read_text(encoding="utf-8"))["platform"] == platform
        assert all(call.method != "submit" for call in fake.calls)
        assert len([call for call in fake.calls if call.method == "diff"]) == 1
    else:
        assert result.returncode == 1
        assert result.stdout == b""
        assert result.stderr == (
            b"ERROR: Could not detect platform from git remote. "
            b"Set 'platform' field in findings JSON to 'github' or 'gitlab'.\n"
        )
        assert forge_factory.calls == []
        assert fake.calls == []
        assert not artifact.exists()


@pytest.mark.parametrize(
    "payload_platform, override, selected, origin_reads, error",
    [
        pytest.param("gitlab", "github", "github", 0, None, id="cli-beats-payload"),
        pytest.param("github", None, "github", 0, None, id="payload-github"),
        pytest.param("gitlab", None, "gitlab", 0, None, id="payload-gitlab"),
        pytest.param("GiTHuB", None, "github", 0, None, id="mixedcase-github"),
        pytest.param("GitLab", None, "gitlab", 0, None, id="mixedcase-gitlab"),
        pytest.param("BitBucket", None, None, 0, "bitbucket", id="invalid-payload"),
        pytest.param("github", "BiTBucket", None, 0, "bitbucket", id="invalid-cli"),
        pytest.param("", None, None, 1, "unknown", id="empty-payload"),
        pytest.param(None, None, None, 1, "unknown", id="null-payload"),
        pytest.param("github", "", None, 1, "unknown", id="empty-cli"),
        pytest.param(7, None, None, 0, "nonstring", id="truthy-nonstring"),
    ],
)
@pytest.mark.parametrize(
    "remote",
    ["git@gitlab.internal.company.com:o/r", "https://[::1]:8443/o/r"],
    ids=["private-gitlab", "ipv6"],
)
def test_explicit_platform(
    payload_platform,
    override,
    selected,
    origin_reads,
    error,
    remote,
    tmp_path,
    invoke,
    forge_factory,
    monkeypatch,
):
    data: dict[str, object] = {
        "owner": "o",
        "repo": "r",
        "pr_number": 5,
        "sha": FULL,
        "findings": [],
        "platform": payload_platform,
    }
    path = tmp_path / "findings.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    github = forge_factory.configure(FakeForge(diffs=[("", "", 0)]))
    gitlab = forge_factory.configure(_gitlab_fake())
    origins: list[list[str]] = []

    def run(command, **kwargs):
        assert command == ["git", "remote", "get-url", "origin"]
        origins.append(command)
        return proc.CompletedProcess(command, 0, remote, "")

    monkeypatch.setattr(proc, "run", run)
    monkeypatch.setenv("GH_HOST", "github.com")
    monkeypatch.setenv("GITLAB_HOST", "gitlab.com")
    result = invoke(
        "post_review",
        [str(path), "--dry-run"]
        + (["--platform", override] if override is not None else []),
        tmp_path,
    )
    assert len(origins) == origin_reads
    assert forge_factory.calls == ([selected] if selected else [])
    if selected:
        assert result.returncode == 0
        assert result.stderr == b""
        assert b"Detected platform:" not in result.stdout
        assert (
            json.loads(
                (tmp_path / "post-review-payload.json").read_text(encoding="utf-8")
            )["platform"]
            == selected
        )
        fake = github if selected == "github" else gitlab
        assert len([call for call in fake.calls if call.method == "diff"]) == 1
    else:
        assert result.returncode == 1
        assert result.stdout == b""
        if error == "unknown":
            assert result.stderr == (
                b"ERROR: Could not detect platform from git remote. "
                b"Set 'platform' field in findings JSON to 'github' or 'gitlab'.\n"
            )
        elif error == "nonstring":
            assert (
                result.stderr
                == b"AttributeError: 'int' object has no attribute 'lower'\n"
            )
        else:
            assert (
                result.stderr
                == f"ERROR: Unsupported platform: '{error}'. Use 'github' or 'gitlab'.\n".encode()
            )
        assert github.calls == [] and gitlab.calls == []


@pytest.mark.parametrize("outcome", ["missing-git", "nonutf8", "timeout"])
def test_poster_implicit_origin_exceptions(
    outcome, tmp_path, forge_factory, monkeypatch
):
    from gauntlet.delivery import post

    data: dict[str, object] = {"owner": "o", "repo": "r", "pr_number": 5, "sha": FULL}
    path = tmp_path / "findings.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    failures: dict[str, Exception] = {
        "missing-git": FileNotFoundError("missing git"),
        "nonutf8": UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid byte"),
        "timeout": proc.TimeoutExpired(["git"], None),
    }

    def run(command, **kwargs):
        assert command == ["git", "remote", "get-url", "origin"]
        assert kwargs == {"cwd": None, "timeout": None, "errors": "strict"}
        raise failures[outcome]

    monkeypatch.setattr(proc, "run", run)
    monkeypatch.setattr(sys, "argv", ["post_review.py", str(path)])
    with pytest.raises(type(failures[outcome])):
        post.main()
    assert forge_factory.calls == []


@pytest.mark.usefixtures("poster_state")
@pytest.mark.parametrize(
    "failure, findings, code",
    [
        pytest.param("encoder", "[]", 0, id="encoder-success"),
        pytest.param("first-write", "[]", 0, id="first-write-success"),
        pytest.param("encoder", "{}", 1, id="encoder-operational-failure"),
    ],
)
def test_patch_serialization_fallback(
    failure, findings, code, tmp_path, invoke, monkeypatch
):
    (tmp_path / "code-gauntlet-findings-abc1234.json").write_text(
        findings, encoding="utf-8"
    )
    calls = 0
    original_dumps = json.dumps

    def dumps(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise TypeError("injected encoder failure")
        return original_dumps(*args, **kwargs)

    class FirstWrite(io.TextIOBase):
        def write(self, text):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise OSError("injected first-write failure")
            return output.write(text)

    output = sys.stdout
    if failure == "first-write":
        monkeypatch.setattr(sys, "stdout", FirstWrite())
    else:
        monkeypatch.setattr(patches.json, "dumps", dumps)
    result = invoke(
        "report_patches", ["--output-dir", str(tmp_path), "--head-sha", SHA], tmp_path
    )
    assert result.returncode == code
    assert result.stdout == (
        b'{"ok": false, "path": null, "oracle": null, "candidates": 0, '
        b'"kept": 0, "downgraded": 0, "reasons": {}, "filtered_earlier": 0, '
        b'"findings": 0, "warnings": [], "errors": ["receipt could not be serialized"]}\n'
    )
    assert result.stderr == b""


@pytest.mark.usefixtures("poster_state")
@pytest.mark.parametrize(
    "case, findings, sha, code, stdout, stderr",
    [
        pytest.param(
            "downgrade",
            '[{"file":"x.py","line":1,"end_line":1,"suggested_fix_code":"changed"}]',
            "abc1234",
            0,
            '{"ok": true, "path": "<TMP>/code-gauntlet-patches-abc1234.md", "oracle": "missing", "candidates": 1, "kept": 0, "downgraded": 1, "reasons": {"no_diff_oracle": 1}, "filtered_earlier": 0, "findings": 1, "warnings": ["report-patch downgraded: x.py:1 (no_diff_oracle)"], "errors": []}\n',
            "WARNING: report-patch downgraded: x.py:1 (no_diff_oracle)\n",
            id="downgrade",
        ),
        pytest.param(
            "invalid-sha",
            "[]",
            "bad/sha",
            2,
            "",
            "usage: report_patches.py [-h] --output-dir DIR --head-sha SHORT\nreport_patches.py: error: --head-sha must match '^[A-Za-z0-9._-]+$': 'bad/sha'\n",
            id="invalid-sha",
        ),
        pytest.param(
            "usage",
            "[]",
            "abc1234",
            2,
            "",
            "usage: report_patches.py [-h] --output-dir DIR --head-sha SHORT\nreport_patches.py: error: the following arguments are required: --output-dir, --head-sha\n",
            id="usage",
        ),
        pytest.param(
            "failure",
            "{}",
            "abc1234",
            1,
            '{"ok": false, "path": "<TMP>/code-gauntlet-patches-abc1234.md", "oracle": "unattempted", "candidates": 0, "kept": 0, "downgraded": 0, "reasons": {}, "filtered_earlier": 0, "findings": 0, "warnings": [], "errors": ["findings file <TMP>/code-gauntlet-findings-abc1234.json must be a JSON array of findings, got dict"]}\n',
            "",
            id="pre-oracle-failure",
        ),
        pytest.param(
            "outside-receipt",
            "[]",
            "abc1234",
            1,
            "",
            "ValueError: oversized hunk\n",
            id="outside-receipt-boundary",
        ),
    ],
)
def test_patch_command_boundaries(
    case, findings, sha, code, stdout, stderr, tmp_path, invoke, monkeypatch
):
    (tmp_path / "code-gauntlet-findings-abc1234.json").write_text(
        findings, encoding="utf-8"
    )
    if case == "outside-receipt":
        (tmp_path / "code-gauntlet-diff-abc1234.patch").write_text(
            PATCH, encoding="utf-8"
        )

        def fail(*args, **kwargs):
            raise ValueError("oversized hunk")

        monkeypatch.setattr(patches, "parse_diff", fail)
    argv = [] if case == "usage" else ["--output-dir", str(tmp_path), "--head-sha", sha]
    result = invoke("report_patches", argv, tmp_path)
    assert result.returncode == code
    assert normalize(result.stdout, tmp_path) == stdout
    assert normalize(result.stderr, tmp_path) == stderr


@pytest.mark.usefixtures("poster_state")
@pytest.mark.parametrize(
    "report, stderr",
    [
        pytest.param(
            "## Findings\n\nOnly findings.\n",
            b"ERROR: Report does not contain a rendered Summary section.\n",
            id="missing-summary",
        ),
        pytest.param(
            "## Summary\n\nOnly summary.\n",
            b"ERROR: Report does not contain a following code-owned heading after Summary.\n",
            id="missing-following-heading",
        ),
    ],
)
def test_poster_report_shape_boundary(report, stderr, tmp_path, invoke, forge_factory):
    findings_path = tmp_path / "findings.json"
    findings_path.write_text(json.dumps({**POST, "findings": []}), encoding="utf-8")
    report_path = tmp_path / "report.md"
    report_path.write_text(report, encoding="utf-8")
    fake = forge_factory.configure(FakeForge())
    result = invoke(
        "post_review", [str(findings_path), "--report", str(report_path)], tmp_path
    )
    assert result.returncode == 1
    assert result.stdout == b""
    assert result.stderr == stderr
    assert fake.calls == []


@pytest.mark.parametrize(
    "path, stdout, stderr",
    [
        pytest.param(
            "src/uni\u00e9.py",
            '{"ok": true, "path": "<TMP>/code-gauntlet-patches-abc1234.md", "oracle": "missing", "candidates": 1, "kept": 0, "downgraded": 1, "reasons": {"no_diff_oracle": 1}, "filtered_earlier": 0, "findings": 1, "warnings": ["report-patch downgraded: src/uni\\u00e9.py:2 (no_diff_oracle)"], "errors": []}\n',
            "WARNING: report-patch downgraded: src/uni\u00e9.py:2 (no_diff_oracle)\n",
            id="foreign-encoding",
        ),
        pytest.param(
            "mo\ud800d.py",
            '{"ok": true, "path": "<TMP>/code-gauntlet-patches-abc1234.md", "oracle": "missing", "candidates": 1, "kept": 0, "downgraded": 1, "reasons": {"no_diff_oracle": 1}, "filtered_earlier": 0, "findings": 1, "warnings": ["report-patch downgraded: mo\\ud800d.py:2 (no_diff_oracle)"], "errors": []}\n',
            "WARNING: report-patch downgraded: mo\\ud800d.py:2 (no_diff_oracle)\n",
            id="surrogate-path",
        ),
        pytest.param(
            "src/bad\ud800.py",
            '{"ok": true, "path": "<TMP>/code-gauntlet-patches-abc1234.md", "oracle": "missing", "candidates": 1, "kept": 0, "downgraded": 1, "reasons": {"no_diff_oracle": 1}, "filtered_earlier": 0, "findings": 1, "warnings": ["report-patch downgraded: src/bad\\ud800.py:2 (no_diff_oracle)"], "errors": []}\n',
            "WARNING: report-patch downgraded: src/bad\\ud800.py:2 (no_diff_oracle)\n",
            id="surrogate-warning",
        ),
    ],
)
def test_patch_stdio(path, stdout, stderr, tmp_path):
    (tmp_path / "code-gauntlet-findings-abc1234.json").write_text(
        json.dumps(
            [{"file": path, "line": 2, "end_line": 2, "suggested_fix_code": "changed"}]
        ),
        encoding="utf-8",
    )
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/report_patches.py"),
            "--output-dir",
            str(tmp_path),
            "--head-sha",
            "abc1234",
        ],
        cwd=tmp_path,
        env={**os.environ, "PYTHONIOENCODING": "ascii"},
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0
    assert normalize(result.stdout, tmp_path) == stdout
    assert result.stderr.decode("utf-8") == stderr

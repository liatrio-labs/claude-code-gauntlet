"""Smoke checks for contributor tooling and generated contract fences."""

import json
import re
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest
from gauntlet import contract_gen as contract_gen

from tests.support.repository import copy_generator_tree, tracked_paths
from tests.support.shell import bash_executable

REPO = Path(__file__).resolve().parents[1]

FORMS = REPO / ".github" / "ISSUE_TEMPLATE"

LABELS_DIFF = REPO / ".github" / "labels_diff.py"

LABEL_CATEGORIES = {"type", "state", "resolution", "outreach", "topic", "area"}


def _read(path: str) -> str:
    return (REPO / path).read_text(encoding="utf-8")


def _pre_commit_hook_span(text: str, hook_id: str) -> tuple[int, int]:
    marker = re.compile(rf"(?m)^      - id: {re.escape(hook_id)}\s*$")
    matches = list(marker.finditer(text))
    if len(matches) != 1:
        raise AssertionError(
            f"expected exactly one {hook_id} hook block, found {len(matches)}"
        )
    start = matches[0].start()
    following = re.search(r"(?m)^      - id: [^\n]+$", text[matches[0].end() :])
    end = matches[0].end() + following.start() if following else len(text)
    return start, end


def _pre_commit_hook_block(text: str, hook_id: str) -> str:
    start, end = _pre_commit_hook_span(text, hook_id)
    return text[start:end]


def _pre_commit_hook_value(block: str, key: str) -> str:
    matches = re.findall(rf"(?m)^        {re.escape(key)}:\s*(.*?)\s*$", block)
    if len(matches) != 1:
        raise AssertionError(
            f"expected exactly one {key}: key in hook block, found {len(matches)}"
        )
    return _unquote(matches[0])


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
        return value[1:-1]
    return value


def _form_files() -> list[Path]:
    return sorted(
        p
        for p in list(FORMS.glob("*.yml")) + list(FORMS.glob("*.yaml"))
        if p.name not in {"config.yml", "config.yaml"}
    )


def _form_id(path: Path) -> str:
    return path.stem


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _sequence_after(lines: list[str], start: int, indent: int) -> list[str]:
    # Mapping continuation keys must not truncate option sequences silently.
    items = []
    for line in lines[start:]:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if _indent(line) > indent:
            continue
        if _indent(line) < indent or not line.lstrip(" ").startswith("- "):
            break
        items.append(_unquote(line.lstrip(" ")[2:]))
    return items


def _form_labels(text: str) -> list[str]:
    # An unreadable label sequence must fail closed; an empty result would make
    # membership checks vacuous.
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if not line.startswith("labels:"):
            continue
        inline = line[len("labels:") :].strip()
        if inline.startswith("[") and inline.endswith("]"):
            return [_unquote(part) for part in inline[1:-1].split(",") if part.strip()]
        if inline:
            raise ValueError(f"unsupported labels: scalar {inline!r}")
        items = _sequence_after(lines, index + 1, indent=2)
        if not items:
            raise ValueError("labels: block sequence is empty or unreadable")
        return items
    return []


def _labels() -> list[dict[str, str]]:
    labels: list[dict[str, str]] = json.loads(_read(".github/labels.json"))["labels"]
    return labels


def test_taxonomy_entries_are_well_formed_and_sorted() -> None:
    # The labels API rejects descriptions over 100 characters instead of truncating
    # them.
    # A leading dash would be parsed as a flag by the sync recipe.
    labels = _labels()
    names = [label["name"] for label in labels]
    assert names == sorted(names), "labels.json entries must be name-sorted"
    assert len(names) == len(set(names)), "duplicate label names in labels.json"
    for label in labels:
        assert set(label) == {"name", "color", "description", "category"}
        assert re.search(r"^[0-9a-f]{6}$", label["color"]), (
            "color must be 6-digit lowercase hex, no leading '#'"
        )
        assert label["description"].strip(), "description must not be empty"
        assert len(label["description"]) <= 100
        assert label["category"] in LABEL_CATEGORIES
        assert not label["name"].startswith("-"), "label name must not start with '-'"
        assert not label["description"].startswith("-")


def _as_live(labels: list[dict[str, str]]) -> list[dict[str, str]]:
    return [
        {
            "name": label["name"],
            "color": label["color"].upper(),
            "description": label["description"],
        }
        for label in labels
    ]


def _run_labels(
    *args: str, live: list[dict[str, str]] | None = None
) -> subprocess.CompletedProcess[str]:
    with tempfile.TemporaryDirectory() as tmp:
        argv = [sys.executable, str(LABELS_DIFF), *args]
        if live is not None:
            path = Path(tmp) / "live.json"
            path.write_text(json.dumps(live), encoding="utf-8")
            argv += ["--live", str(path)]
        return subprocess.run(
            argv, cwd=REPO, capture_output=True, text=True, encoding="utf-8"
        )


def test_labels_commands() -> None:
    result = _run_labels("--commands", "--repo", "acme/widgets")
    assert result.returncode == 0, result.stderr
    lines = result.stdout.strip().splitlines()
    labels = _labels()
    assert len(lines) == len(labels)
    emitted = {}
    for line in lines:
        argv = shlex.split(line)
        assert argv[:3] == ["gh", "label", "create"]
        assert "--force" in argv
        assert "--repo acme/widgets" in line
        assert argv[argv.index("--repo") + 1] == "acme/widgets"
        emitted[argv[3]] = argv[argv.index("--description") + 1]
    assert emitted == {label["name"]: label["description"] for label in labels}


@pytest.mark.parametrize(
    "live,status,expected,commands",
    [
        pytest.param(
            [
                *_as_live(_labels()),
                {
                    "name": "hand-made",
                    "color": "ffffff",
                    "description": "not managed here",
                },
            ],
            0,
            ("unmanaged", "hand-made", "in sync"),
            None,
            id="uppercase-colors-are-in-sync",
        ),
        pytest.param(
            [
                _as_live(_labels())[1] | {"description": "stale description"},
                *_as_live(_labels())[2:],
                {
                    "name": "hand-made",
                    "color": "ffffff",
                    "description": "not managed here",
                },
            ],
            1,
            ("missing: area:agents", "diverging: area:bench", "unmanaged", "hand-made"),
            {"area:agents", "area:bench"},
            id="missing-and-diverging-with-unmanaged",
        ),
    ],
)
def test_labels_drift(
    live: list[dict[str, str]],
    status: int,
    expected: tuple[str, ...],
    commands: set[str] | None,
) -> None:
    result = _run_labels(live=live)
    assert result.returncode == status, result.stdout + result.stderr
    for fragment in expected:
        assert fragment in result.stdout
    if commands is not None:
        narrowed = _run_labels("--commands", live=live).stdout.strip().splitlines()
        assert {shlex.split(line)[3] for line in narrowed} == commands


def test_labels_refused_input(tmp_path: Path) -> None:
    path = tmp_path / "api-error.json"
    path.write_text('{"message": "Not Found", "status": "404"}', encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(LABELS_DIFF), "--live", str(path)],
        cwd=REPO,
        capture_output=True,
    )
    assert result.returncode == 2
    assert result.stdout == b""
    assert b"expected a JSON array of labels" in result.stderr
    assert b"Traceback" not in result.stderr

    manifest = tmp_path / "unemittable-labels.json"
    manifest.write_text(
        json.dumps(
            {
                "labels": [
                    {
                        "name": "ok",
                        "color": "fff; echo pwned",
                        "description": "not hex",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    refused = _run_labels("--manifest", str(manifest), "--commands")
    assert refused.returncode == 2
    assert refused.stdout == ""
    assert "not 6-digit lowercase hex" in refused.stderr
    assert "Traceback" not in refused.stderr


@pytest.mark.parametrize("form", _form_files(), ids=_form_id)
def test_every_auto_applied_label_exists_in_the_taxonomy(form: Path) -> None:
    # GitHub silently drops form labels that do not already exist.
    known = {label["name"] for label in _labels()}
    labels = _form_labels(form.read_text(encoding="utf-8"))
    assert labels, f"{form.name} auto-applies no labels"
    assert "needs-triage" in labels
    for label in labels:
        assert label in known


REQUIRED_PR_CHECK_CONTEXTS = (
    "Run Linting",
    "Run Tests (3.10)",
    "Run Tests (3.11)",
    "Run Tests (3.12)",
    "Run Workflow JS Lint",
    "Run Workflow JS Tests",
    "Run Bench Self-Tests (3.11)",
    "Run Bench Self-Tests (3.12)",
    "Run Tests on Windows (3.12)",
    "Run Tests on Windows (3.14)",
    "Validate plugin.json",
    "lint-pr-title",
)

LOCAL_COMMAND_FOR_REQUIRED_CHECK = {
    "Run Linting": "pre-commit run --all-files",
    "Run Tests (3.10)": "python -m pytest tests/ -q",
    "Run Tests (3.11)": "python -m pytest tests/ -q",
    "Run Tests (3.12)": "python -m pytest tests/ -q",
    "Run Workflow JS Lint": "python3 workflows/test/tools/biome_check.py",
    "Run Workflow JS Tests": "node --test workflows/test/*.test.js",
    "Run Bench Self-Tests (3.11)": "python -m pytest bench/tests/ -q",
    "Run Bench Self-Tests (3.12)": "python -m pytest bench/tests/ -q",
    "Run Tests on Windows (3.12)": "python -m pytest tests/ -q",
    "Run Tests on Windows (3.14)": "python -m pytest tests/ -q",
    "Validate plugin.json": "claude plugin validate .",
    "lint-pr-title": None,  # Needs the PR title; nothing local to run.
}

REQUIRED_PR_CHECK_WORKFLOWS = (
    ".github/workflows/ci.yml",
    ".github/workflows/validate.yml",
    ".github/workflows/pr-title-lint.yml",
)


def _derive_required_pr_check_contexts() -> list[str]:
    # Update the frozen tuple and live ruleset together. Zero required approvals is
    # deliberate; checks, conversations and linear history are the merge gates.
    # Job-level names override job ids.
    contexts: list[str] = []
    for rel in REQUIRED_PR_CHECK_WORKFLOWS:
        for job_id, job in _ci_job_blocks(_read(rel)):
            if re.search(
                r"(?m)^    if: github.event_name == 'workflow_dispatch' && inputs\.windows_live$",
                job,
            ):
                continue
            names = re.findall(r"(?m)^    name:\s*(.+?)\s*$", job)
            label = _unquote(names[0]) if names else job_id
            matrices = re.findall(r"(?m)^        python-version:\s*\[(.*)\]\s*$", job)
            versions = [
                _unquote(part.strip())
                for matrix in matrices
                for part in matrix.split(",")
                if part.strip()
            ]
            if versions:
                contexts.extend(f"{label} ({version})" for version in versions)
            else:
                contexts.append(label)
    return contexts


def test_frozen_tuple_matches_workflow_derived_names() -> None:
    derived = tuple(sorted(_derive_required_pr_check_contexts()))
    frozen = tuple(sorted(REQUIRED_PR_CHECK_CONTEXTS))
    assert derived == frozen, (
        "workflow job names drifted from REQUIRED_PR_CHECK_CONTEXTS"
    )
    assert set(LOCAL_COMMAND_FOR_REQUIRED_CHECK) == set(REQUIRED_PR_CHECK_CONTEXTS)


GATE_MARKERS = ("--cov-fail-under=", "--test-coverage-lines=")


def _documented_coverage_gate_commands(text: str) -> set[str]:
    # Ignore trailing cosmetic blank lines while preserving interior command bytes.
    heading = re.search(r"(?m)^Coverage gates\b.*$", text)
    if not heading:
        raise AssertionError("CONTRIBUTING.md missing 'Coverage gates' paragraph")
    rest = text[heading.end() :]
    fence = re.search(r"```bash\n(.*?)```", rest, re.DOTALL)
    if not fence:
        raise AssertionError("CONTRIBUTING.md Coverage gates missing ```bash fence")
    body = fence.group(1)
    chunks = re.split(r"\n\s*\n", body.strip("\n"))
    commands = {c.rstrip("\n") for c in chunks if c.strip()}
    if len(commands) < 3:
        raise AssertionError(
            f"expected ≥3 gate commands in CONTRIBUTING.md, found {len(commands)}"
        )
    return commands


def _ci_run_blocks(text: str) -> list[str]:
    lines = text.splitlines()
    blocks = []
    i = 0
    while i < len(lines):
        m = re.match(r"^(\s*)run:\s*\|-?\s*$", lines[i])
        if not m:
            i += 1
            continue
        base = len(m.group(1))
        i += 1
        content_indent = None
        collected = []
        while i < len(lines):
            line = lines[i]
            if line.strip() == "":
                collected.append("")
                i += 1
                continue
            indent = len(line) - len(line.lstrip(" "))
            if content_indent is None:
                if indent <= base:
                    break
                content_indent = indent
            if indent < content_indent and line.strip():
                break
            collected.append(
                line[content_indent:] if content_indent is not None else line
            )
            i += 1
        blocks.append("\n".join(collected).rstrip("\n"))
    return blocks


def _ci_job_blocks(text: str) -> list[tuple[str, str]]:
    lines = text.splitlines()
    jobs_index = next(
        (index for index, line in enumerate(lines) if line == "jobs:"), None
    )
    if jobs_index is None:
        raise AssertionError("workflow is missing jobs:")
    starts = []
    jobs_end = len(lines)
    for index in range(jobs_index + 1, len(lines)):
        line = lines[index]
        if line and not line.startswith(" ") and not line.startswith("#"):
            jobs_end = index
            break
        match = re.match(r"^  ([A-Za-z0-9_-]+):\s*$", line)
        if match:
            starts.append((index, match.group(1)))
    jobs = []
    for position, (start, job_id) in enumerate(starts):
        end = starts[position + 1][0] if position + 1 < len(starts) else jobs_end
        jobs.append((job_id, "\n".join(lines[start:end])))
    return jobs


def _ci_gate_commands(text: str) -> set[str]:
    return {
        b for b in _ci_run_blocks(text) if any(marker in b for marker in GATE_MARKERS)
    }


def test_contract_fence_hook_is_scoped_and_covers_derived_inputs() -> None:
    block = _pre_commit_hook_block(
        _read(".pre-commit-config.yaml"), "contract-fences-current"
    )
    assert _pre_commit_hook_value(block, "entry") == (
        "python3 scripts/generate_contract_requirements.py --check"
    )
    assert _pre_commit_hook_value(block, "pass_filenames") == "false"
    scope = re.compile(_pre_commit_hook_value(block, "files"))
    registry = contract_gen.load_registry(str(REPO))
    paths = set(contract_gen.rendered_targets(str(REPO), registry, ""))
    paths.update(tracked_paths(REPO, ("scripts/", "workflows/src/")))
    assert paths
    assert all(scope.match(path) for path in paths)


def test_contract_fence_hook_rejects_stale_fence_without_writing() -> None:
    config = _read(".pre-commit-config.yaml")
    block = _pre_commit_hook_block(config, "contract-fences-current")
    entry = shlex.split(_pre_commit_hook_value(block, "entry"))
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        copy_generator_tree(
            REPO,
            root,
            contract_gen.rendered_targets(
                str(REPO), contract_gen.load_registry(str(REPO)), ""
            ),
        )
        target = root / "skills/code-gauntlet/references/phase1-preflight.md"
        clean_result = subprocess.run(
            entry, cwd=root, capture_output=True, text=True, encoding="utf-8"
        )
        assert clean_result.returncode == 0, clean_result.stdout + clean_result.stderr
        lines = target.read_text(encoding="utf-8").splitlines(keepends=True)
        open_index, close_index = contract_gen.find_identity_pairs(
            [line.rstrip("\n") for line in lines],
            "skills/code-gauntlet/references/phase1-preflight.md",
        )["derived_waist_fields"]
        body_index = next(
            index
            for index in range(open_index + 1, close_index)
            if lines[index].strip()
        )
        lines[body_index] = lines[body_index].rstrip("\n") + " stale\n"
        corrupted = "".join(lines).encode()
        target.write_bytes(corrupted)
        stale_result = subprocess.run(
            entry, cwd=root, capture_output=True, text=True, encoding="utf-8"
        )
        assert stale_result.returncode == 1
        assert (
            "skills/code-gauntlet/references/phase1-preflight.md" in stale_result.stderr
        )
        assert target.read_bytes() == corrupted


def test_documented_and_ci_gate_commands_are_byte_identical_sets() -> None:
    documented = _documented_coverage_gate_commands(_read("CONTRIBUTING.md"))
    ci = _ci_gate_commands(_read(".github/workflows/ci.yml"))
    assert documented == ci
    assert (
        "bare directory form is not a valid `node --test` target on node 24"
        in _read("CONTRIBUTING.md")
    )
    assert (
        "Transform goldens updated with "
        "`UPDATE_GOLDENS=1 node --test workflows/test/goldens.test.js` "
        "in a reviewed diff; verify-wire fixtures updated with "
        "`workflows/test/tools/record_parity.py`"
    ) in _read(".github/pull_request_template.md")


@pytest.mark.parametrize(
    "fault",
    [
        None,
        "no-launch",
        "early-args",
        "args",
        "host-path",
        "no-proof",
        "phase",
        "path-target",
        "interpreter",
        "workflow-python",
        "workflow-repoRoot",
        "workflow-outputDir",
        "workflow-pluginRoot",
        "workflow-native-root",
        "materialize",
        "nonce",
        "outside",
        "missing-report",
    ],
)
def test_windows_live_receipt_gate(fault, tmp_path, monkeypatch):
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "windows_live", REPO / ".github/windows_live.py"
    )
    assert spec is not None and spec.loader is not None
    live = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(live)
    output = tmp_path / "artifacts"
    output.mkdir()
    paths = {
        key: str(output / name)
        for key, name in {
            "report": "report.md",
            "findings": "findings.json",
            "postReview": "post.json",
            "checkpoints": "checkpoint.json",
        }.items()
    }
    for path in paths.values():
        Path(path).write_text("defects.py\n", encoding="utf-8")
    terminal = {
        "ok": True,
        "phaseReached": "report",
        "stats": {"inputProof": {"proven": 1}},
        "gaps": [],
        "artifactPaths": paths,
    }
    task_dir = tmp_path / "tasks"
    task_dir.mkdir()
    source = task_dir / "task1.output"
    monkeypatch.setenv("CODE_GAUNTLET_TASKS_DIR", str(task_dir))
    materialized = {
        "ok": True,
        "channel": "return",
        "source": str(source),
        "scanned": 1,
        "errors": [],
        "materialized": [{"path": paths["report"]}],
        "assemble": {"written": []},
    }
    command = 'py -3 /plugin/scripts/await_workflow.py --python "py -3" -- task1'
    workflow_args = {
        "python": "py -3",
        "repoRoot": "D:/repo",
        "outputDir": "D:/artifacts",
        "pluginRoot": "C:/plugin",
    }
    if fault == "workflow-python":
        workflow_args["python"] = "python3"
    elif fault in ("workflow-repoRoot", "workflow-outputDir", "workflow-pluginRoot"):
        workflow_args[fault.removeprefix("workflow-")] = "/posix/root"
    elif fault == "workflow-native-root":
        workflow_args["repoRoot"] = "D:/repo\\nested"
    if fault == "args":
        terminal["gaps"] = ["invalid args"]
    elif fault == "host-path":
        Path(paths["report"]).write_text("see D:\\repo\\defects.py\n", encoding="utf-8")
    elif fault is None:
        terminal["gaps"] = [
            "host-path-text: finding F1 mentions a host path in challenge_justification"
        ]
    elif fault == "no-proof":
        terminal["stats"]["inputProof"]["proven"] = 0
    elif fault == "phase":
        terminal["phaseReached"] = "verify"
    elif fault == "path-target":
        command = command.replace("-- task1", "-- /tasks/task1.output")
    elif fault == "interpreter":
        command = command.replace("py -3 /plugin", "python /plugin")
    elif fault == "materialize":
        materialized["ok"] = False
    elif fault == "nonce":
        materialized["scanned"] = 2
    elif fault == "outside":
        terminal["artifactPaths"]["report"] = str(tmp_path / "outside.md")
    elif fault == "missing-report":
        Path(paths["report"]).unlink()
    source.write_text(json.dumps({"result": terminal}), encoding="utf-8")
    blocks = [
        {
            "type": "tool_use",
            "id": "w",
            "name": "Workflow",
            "input": {"args": workflow_args},
        },
        {"type": "tool_use", "id": "a", "name": "Bash", "input": {"command": command}},
        {
            "type": "tool_use",
            "id": "m",
            "name": "Bash",
            "input": {
                "command": "py -3 /plugin/scripts/materialize_artifacts.py --task-id task1"
            },
        },
        {"type": "tool_result", "tool_use_id": "a", "content": json.dumps(terminal)},
        {
            "type": "tool_result",
            "tool_use_id": "m",
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(
                        {"stdout": json.dumps(materialized), "stderr": ""}
                    ),
                }
            ],
        },
    ]
    if fault == "no-launch":
        blocks = blocks[1:]
    if fault == "early-args":
        blocks.extend(
            [
                {
                    "type": "tool_use",
                    "id": "earlier",
                    "name": "Bash",
                    "input": {"command": command},
                },
                {
                    "type": "tool_result",
                    "tool_use_id": "earlier",
                    "content": json.dumps(
                        {"ok": False, "phaseReached": "args", "failingPhase": "args"}
                    ),
                },
            ]
        )
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text(
        json.dumps({"message": {"content": blocks}}) + "\n", encoding="utf-8"
    )
    if fault:
        with pytest.raises(AssertionError):
            live.assert_run(transcript, output, tmp_path / "config")
    else:
        receipt = live.assert_run(transcript, output, tmp_path / "config")
        assert receipt["ok"] and receipt["planted_file_mentioned"]
        assert (tmp_path / "workflow.output").read_bytes() == source.read_bytes()


@pytest.mark.parametrize("has_token", [False, True])
def test_windows_live_driver(has_token, tmp_path, monkeypatch, capsys):
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "windows_live_driver", REPO / ".github/windows_live.py"
    )
    assert spec is not None and spec.loader is not None
    live = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(live)
    job = dict(_ci_job_blocks(_read(".github/workflows/ci.yml")))["windows-live"]
    assert "if: github.event_name == 'workflow_dispatch' && inputs.windows_live" in job
    assert "environment: windows-live" in job
    assert job.index("Require subscription token") < job.index(
        "Checkout dispatched ref"
    )
    assert "CLAUDE_CODE_OAUTH_TOKEN is absent from environment windows-live" in job
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    if not has_token:
        with pytest.raises(SystemExit, match="CLAUDE_CODE_OAUTH_TOKEN is required"):
            live.main(tmp_path / "run")
        assert not (tmp_path / "run").exists()
        first_step = re.search(
            r"Require subscription token.*?run: \|\n(.*?)(?=      - name:)", job, re.S
        )
        assert first_step is not None
        command = "\n".join(line[10:] for line in first_step[1].splitlines())
        result = subprocess.run(
            [bash_executable(), "-c", command],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        assert result.returncode == 1
        assert (
            "CLAUDE_CODE_OAUTH_TOKEN is absent from environment windows-live"
            in result.stdout
        )
        return
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "test-subscription")
    calls = []
    monkeypatch.setattr(
        live.subprocess, "run", lambda argv, **kwargs: calls.append((argv, kwargs))
    )
    monkeypatch.setattr(live.shutil, "which", lambda name: name)
    monkeypatch.setattr(live, "assert_run", lambda *args: {"ok": True})
    run = tmp_path / "run"
    live.main(run)
    argv, options = calls[-1]
    assert argv[:2] == ["claude", "-p"]
    assert "--plugin-dir" in argv and "--output-format" in argv
    assert options["env"]["CODE_GAUNTLET_HEADLESS"] == "1"
    assert options["env"]["CODE_GAUNTLET_OUTPUT_DIR"] == (run / "artifacts").as_posix()
    assert options["env"]["PATH"].startswith(str(run / "stubs"))
    assert (run / "stubs/python3").read_text(
        encoding="utf-8"
    ) == "#!/bin/bash\nexit 49\n"
    assert (run / "stubs/python").read_bytes() == (run / "stubs/python3").read_bytes()
    assert len(calls) == 4
    assert json.loads(capsys.readouterr().out) == {"ok": True}

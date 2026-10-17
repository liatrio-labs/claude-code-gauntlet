"""FIX task payloads, command hints, scope, and caller receipts."""

import json
import re
import subprocess
from pathlib import Path

import pytest
from gauntlet import fix_tasks as renderer


@pytest.fixture
def fix_root(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    return root


@pytest.fixture
def fix_artifact(fix_root):
    return fix_root / "post-review.json"


def make_finding(**changes):
    value = {
        "id": "bug-1",
        "file": "src/bug.py",
        "line_start": 10,
        "title": "Avoid stale value",
        "description": "The value is stale.",
        "severity": "medium",
        "confidence": 81,
        "dimension": "bug",
    }
    value.update(changes)
    return value


@pytest.fixture
def write_artifact(fix_artifact):
    def write(findings, wrapper=False):
        data = {"findings": findings} if wrapper else findings
        fix_artifact.write_text(json.dumps(data, ensure_ascii=True), encoding="utf-8")

    return write


@pytest.fixture
def run_renderer(invoke, fix_root, fix_artifact, tmp_path):
    def run(root=None, artifact=None, *extra):
        result = invoke(
            "render_fix_tasks",
            [
                str(artifact or fix_artifact),
                "--repo-root",
                str(root or fix_root),
                *extra,
            ],
            tmp_path,
        )
        return subprocess.CompletedProcess(
            [],
            result.returncode,
            result.stdout.decode("utf-8"),
            result.stderr.decode("utf-8"),
        )

    return run


def output(result):
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


@pytest.fixture
def tracked_files(fix_root):
    def track(*paths):
        subprocess.run(["git", "init"], cwd=fix_root, check=True, capture_output=True)
        for path in paths:
            target = fix_root / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("x", encoding="utf-8")
        subprocess.run(
            ["git", "add", *paths], cwd=fix_root, check=True, capture_output=True
        )

    return track


def test_hand_typed_expected_bytes_for_every_optional_field(
    fix_artifact, run_renderer, write_artifact
):
    finding = make_finding(
        id="security-1",
        line_end=12,
        title="Unsafe value",
        severity="critical",
        confidence=99,
        dimension="security",
        evidence="Observed `bad` input.",
        suggestion="Reject the input.",
        cross_file_refs=["src/other.py"],
        origin="surfaced",
        attack_vector="SQL injection",
    )
    write_artifact([finding])
    result = run_renderer()
    expected_description = (
        "## Issue\n"
        "The value is stale.\n"
        "\n"
        "## Location\n"
        "`src/bug.py:10-12`\n"
        "\n"
        "## Evidence\n"
        "```\n"
        "Observed `bad` input.\n"
        "```\n"
        "\n"
        "## Suggested Fix\n"
        "Reject the input.\n"
        "\n"
        "## Category\n"
        "critical | security\n"
        "\n"
        "## Details\n"
        "**Attack vector:** SQL injection\n"
        "\n"
        "## Toolchain\n"
        "Not detected. Set the test, lint, and build commands before running this task."
    )
    expected = {
        "subject": "FIX: Unsafe value",
        "description": expected_description,
        "metadata": {
            "task_type": "review-fix",
            "task_id": "FIX-security-1",
            "category": "security",
            "severity": "critical",
            "role": "implementer",
            "complexity": "standard",
            "model": "sonnet",
            "scope": {
                "files_to_create": [],
                "files_to_modify": ["src/bug.py"],
                "patterns_to_follow": [],
            },
            "requirements": [
                {
                    "id": "R-security-1.1",
                    "text": "The value is stale.",
                    "testable": True,
                }
            ],
            "proof_artifacts": [{"type": "file", "path": "src/bug.py"}],
            "verification": {"pre": [], "post": []},
            "commit": {"template": "fix(src): Unsafe value"},
            "review_context": {
                "finding_id": "security-1",
                "dimension": "security",
                "confidence": 99,
                "evidence": "Observed `bad` input.",
                "cross_file_refs": ["src/other.py"],
                "blame_classification": "surfaced",
            },
        },
    }
    assert result.returncode == 0
    assert result.stdout == json.dumps([expected], indent=2, ensure_ascii=False) + "\n"
    assert result.stderr.endswith(
        f"Rendered 1 FIX task from {fix_artifact} (0 paths rejected)\n"
    )


def test_only_required_fields_apply_all_omission_rules(run_renderer, write_artifact):
    write_artifact([make_finding()])
    task = output(run_renderer())[0]
    assert "## Evidence" not in task["description"]
    assert "## Suggested Fix" not in task["description"]
    assert "## Details" not in task["description"]
    assert "## Location\n`src/bug.py:10`" in task["description"]
    assert "## Toolchain\nNot detected." in task["description"]
    context = task["metadata"]["review_context"]
    assert context["evidence"] == ""
    assert context["cross_file_refs"] == []
    assert context["blame_classification"] == "unknown"
    assert task["metadata"]["complexity"] == "trivial"
    assert task["metadata"]["model"] == "haiku"


def test_wrapped_aliases_and_canonical_precedence(run_renderer, write_artifact):
    finding = make_finding(line=11, end_line=12, body="canonical description")
    for key in ("line_start", "description"):
        finding.pop(key)
    write_artifact([finding], wrapper=True)
    alias_task = output(run_renderer())[0]
    assert "## Location\n`src/bug.py:11-12`" in alias_task["description"]
    assert alias_task["metadata"]["requirements"] == [
        {"id": "R-bug-1.1", "text": "canonical description", "testable": True}
    ]
    finding.update(line_start=11, line_end=12, description="canonical description")
    finding.update(line=99, end_line=88, body="alias body")
    write_artifact([finding], wrapper=True)
    assert output(run_renderer())[0] == alias_task


def test_invalid_artifact_is_a_content_failure(run_renderer, write_artifact):
    finding = make_finding()
    finding.pop("dimension")
    write_artifact([finding])
    result = run_renderer()
    assert (result.returncode, result.stdout, result.stderr) == (
        1,
        "",
        "render_fix_tasks: finding at index 0 lacks required key dimension\n",
    )


def test_unexpected_build_failure_keeps_stdout_empty(
    run_renderer, write_artifact, monkeypatch
):
    def fail(*_args, **_kwargs):
        raise ValueError("bad content")

    write_artifact([make_finding()])
    monkeypatch.setattr(renderer, "build_tasks", fail)
    result = run_renderer()
    assert (result.returncode, result.stdout, result.stderr) == (
        1,
        "",
        "render_fix_tasks: bad content\n",
    )


# A symlink planted by the reviewed PR must not send the fix agent outside the repo.
@pytest.mark.parametrize("file", ["../outside.py", "link.py"])
def test_outside_path_is_omitted_and_counted(
    file, request, fix_root, tmp_path, run_renderer, write_artifact
):
    if file == "link.py":
        request.getfixturevalue("symlink_or_skip")
        (tmp_path / "outside.py").write_text("x", encoding="utf-8")
        (fix_root / "link.py").symlink_to(tmp_path / "outside.py")
    write_artifact([make_finding(file=file)])
    result = run_renderer()
    task = output(result)[0]
    assert "(1 path rejected)" in result.stderr
    assert file not in result.stdout
    assert "## Location\n(path rejected: outside repo root)" in task["description"]
    assert task["metadata"]["scope"] == {
        "files_to_create": [],
        "files_to_modify": [],
        "patterns_to_follow": [],
    }
    assert task["metadata"]["proof_artifacts"] == []
    assert task["metadata"]["commit"]["template"] == "fix(fix): Avoid stale value"


@pytest.mark.parametrize(
    ("names", "commands"),
    [
        (
            (
                "package.json",
                "Cargo.toml",
                "go.mod",
                "pyproject.toml",
                "Makefile",
                "a.csproj",
            ),
            ("npm test", "npm run lint", "npm run build"),
        ),
        (("Cargo.toml",), ("cargo test", "cargo clippy", "cargo build")),
        (("go.mod",), ("go test ./...", "golangci-lint run", "go build ./...")),
        (("pyproject.toml",), ("pytest", "ruff check .")),
        (("Makefile",), ("make test", "make lint", "make build")),
        (
            ("a.csproj",),
            ("dotnet test", "dotnet format --verify-no-changes", "dotnet build"),
        ),
    ],
)
def test_toolchain_commands(names, commands, fix_root, run_renderer, write_artifact):
    for name in names:
        contents = (
            '{"scripts":{"test":"x","lint":"x","build":"x"}}'
            if name == "package.json"
            else "x"
        )
        (fix_root / name).write_text(contents, encoding="utf-8")
    write_artifact([make_finding()])
    task = output(run_renderer())[0]
    assert "## Toolchain" not in task["description"]
    metadata = task["metadata"]
    assert metadata["proof_artifacts"] == [
        {"type": "test", "command": commands[0], "expected": "All pass"},
        {"type": "file", "path": "src/bug.py"},
    ]
    assert metadata["verification"] == {
        "pre": list(commands[1:]),
        "post": [commands[0]],
    }


def test_custom_package_commands(fix_root, run_renderer, write_artifact):
    (fix_root / "package.json").write_text(
        json.dumps(
            {
                "scripts": dict.fromkeys(
                    ("test:unit", "test:z", "lint:fix", "build:prod"), "x"
                )
            }
        ),
        encoding="utf-8",
    )
    write_artifact([make_finding()])
    metadata = output(run_renderer())[0]["metadata"]
    assert metadata["proof_artifacts"][0] == {
        "type": "test",
        "command": "npm run test:unit",
        "expected": "All pass",
    }
    assert metadata["verification"] == {
        "pre": ["npm run lint:fix", "npm run build:prod"],
        "post": ["npm run test:unit"],
    }


# A contributor's PR can plant this link; the renderer must not read outside the repo.
@pytest.mark.usefixtures("symlink_or_skip")
def test_outside_symlink_package_candidate_is_absent(
    fix_root, run_renderer, tmp_path, write_artifact
):
    outside = tmp_path / "package.json"
    outside.write_text('{"scripts":{"test":"outside"}}', encoding="utf-8")
    (fix_root / "package.json").symlink_to(outside)
    (fix_root / "Cargo.toml").write_text("x", encoding="utf-8")
    write_artifact([make_finding()])
    result = run_renderer()
    proof = output(result)[0]["metadata"]["proof_artifacts"][0]
    assert proof["command"] == "cargo test"
    assert "toolchain candidate package.json rejected" in result.stderr


def test_root_level_siblings_are_ranked_and_capped(
    fix_root, tracked_files, run_renderer, write_artifact
):
    tracked = [
        "main.py",
        "mainx.py",
        "mainy.py",
        "mainz.py",
        "main_utils.py",
        "main.js",
    ]
    tracked_files(*tracked)
    (fix_root / "untracked.py").write_text("x", encoding="utf-8")
    write_artifact(
        [
            make_finding(file="main.py"),
            make_finding(id="delivered", file="mainx.py"),
        ]
    )
    tasks = output(run_renderer())
    assert tasks[0]["metadata"]["scope"]["patterns_to_follow"] == [
        "mainy.py",
        "mainz.py",
    ]
    assert tasks[0]["metadata"]["commit"]["template"] == "fix(main): Avoid stale value"


@pytest.mark.parametrize(
    ("raw", "label", "complexity", "model"),
    [(" HiGh ", "high", "standard", "sonnet"), ("unknown", "low", "trivial", "haiku")],
)
def test_normalized_severity_controls_category_complexity_and_model(
    raw, label, complexity, model, run_renderer, write_artifact
):
    write_artifact([make_finding(severity=raw)])
    task = output(run_renderer())[0]
    assert task["metadata"]["severity"] == label
    assert task["metadata"]["complexity"] == complexity
    assert task["metadata"]["model"] == model
    assert f"## Category\n{label} | bug" in task["description"]


def test_test_coverage_scope_uses_confined_refs_and_production_file_proof(
    fix_root, tracked_files, run_renderer, write_artifact
):
    tracked_files(
        "src/prod.py", "tests/test_prod.py", "tests/test_other.py", "tests/helpers.py"
    )
    findings = [
        make_finding(
            id="with-refs",
            dimension="test_coverage",
            file="src/prod.py",
            cross_file_refs=[
                "tests/test_prod.py",
                "tests/test_other.py",
                "../escape.py",
            ],
            criticality=7,
            failure_scenario="No test catches it",
        ),
        make_finding(
            id="without-refs",
            dimension="test_coverage",
            file="src/prod.py",
            criticality=3,
            failure_scenario="No test exists",
        ),
    ]
    write_artifact(findings)
    result = run_renderer()
    tasks = output(result)
    first, second = tasks
    assert first["metadata"]["scope"]["files_to_modify"] == [
        "tests/test_prod.py",
        "tests/test_other.py",
    ]
    assert first["metadata"]["scope"]["patterns_to_follow"] == ["tests/helpers.py"]
    assert second["metadata"]["scope"]["files_to_modify"] == []
    assert second["metadata"]["scope"]["patterns_to_follow"] == []
    assert first["metadata"]["proof_artifacts"] == [
        {"type": "file", "path": "src/prod.py"}
    ]
    assert "1 path rejected" in result.stderr
    assert "**Criticality:** 7" in first["description"]


@pytest.mark.parametrize(
    ("dimension", "values", "expected"),
    [
        (
            "cross_file_impact",
            {"affected_consumers": ["src/a.py", "src/b.py"]},
            "**Affected consumers:** src/a.py, src/b.py",
        ),
        (
            "convention",
            {
                "claude_md_rule": "Use the project rule.",
                "rule_source": "repo_precedent",
            },
            "**Repo precedent:** Use the project rule.",
        ),
        (
            "convention",
            {"claude_md_rule": "Use the project rule."},
            "**Cited rule:** Use the project rule.",
        ),
    ],
)
def test_detail_fields(dimension, values, expected, run_renderer, write_artifact):
    write_artifact([make_finding(dimension=dimension, **values)])
    description = output(run_renderer())[0]["description"]
    details = description.split("## Details\n", 1)[1].split("\n\n## Toolchain", 1)[0]
    assert details == expected


def test_hostile_markdown_is_sanitized_and_evidence_fence_is_long_enough(
    run_renderer, write_artifact
):
    evidence = "payload````\r\n```\r\n<!-- evidence -->"
    finding = make_finding(
        title="Bad\n## forged\r<!-- title -->",
        description="first\r\n## Evidence\n<!-- hidden -->\tend\x01",
        suggestion="fix\n## Category\n<!-- suggestion -->\x02done\nHeading\n====",
        evidence=evidence,
    )
    write_artifact([finding])
    task = output(run_renderer())[0]
    description = task["description"]
    assert (
        len([line for line in description.splitlines() if line == "## Evidence"]) == 1
    )
    assert (
        len([line for line in description.splitlines() if line == "## Suggested Fix"])
        == 1
    )
    assert "<!--" not in description.replace(evidence, "")
    assert "\x01" not in description
    assert "\x02" not in description
    assert "\t" not in description
    assert "\\## Evidence" in description
    evidence_block = description.rsplit("## Evidence\n", 1)[1].split(
        "\n\n## Suggested Fix", 1
    )[0]
    fence_lines = evidence_block.splitlines()
    longest_content_run = max(len(run) for run in re.findall("`+", evidence))
    assert len(fence_lines[0]) > longest_content_run
    assert len(fence_lines[-1]) > longest_content_run
    assert fence_lines[1:-1] == evidence.splitlines()
    assert "FIX: Bad ## forged &lt;!-- title -->" in task["subject"]
    assert "Heading\n\\====" in description


def test_caller_fix_protocol():
    root = Path(__file__).resolve().parents[1]
    skill = (root / "skills/code-gauntlet/SKILL.md").read_text(encoding="utf-8")
    question = skill.index(
        'question: "Create fix tasks on the task board from these findings?"'
    )
    invocation = skill.index(
        '{python} "{plugin_root}/scripts/render_fix_tasks.py" '
        '"<artifactPaths.postReview>" --repo-root "<repoRoot>"'
    )
    methodology = skill.index("### Print methodology")
    assert question < invocation < methodology
    block = skill[invocation:methodology]
    for phrase in (
        "Parse stdout as a JSON array",
        "assert that its length equals the delivered count",
        "TaskCreate(subject, description)",
        "and then `TaskUpdate(taskId, metadata)`",
        "Stop on any parse, count, or call failure",
        "Never fall back to hand composition",
    ):
        assert phrase in block

"""FIX task output, confined scope, toolchain selection, and sibling discovery."""

import json
import os
import re
import subprocess
from pathlib import Path
from unittest.mock import patch

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
    def write(findings, wrapper=False, raw=None):
        if raw is None:
            data = (
                {
                    "owner": "acme",
                    "repo": "demo",
                    "pr_number": 7,
                    "sha": "abc",
                    "review_body": "",
                    "findings": findings,
                }
                if wrapper
                else findings
            )
            raw = json.dumps(data, ensure_ascii=True)
        fix_artifact.write_text(raw, encoding="utf-8")

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


def category_section(task):
    categories = [
        section
        for section in task["description"].split("\n\n")
        if section.startswith("## Category\n")
    ]
    assert len(categories) == 1
    return categories[0]


@pytest.fixture
def git_repo(fix_root):
    def init():
        subprocess.run(["git", "init"], cwd=fix_root, check=True, capture_output=True)

    return init


@pytest.fixture
def git_add(fix_root):
    def add(*paths):
        subprocess.run(
            ["git", "add", *paths], cwd=fix_root, check=True, capture_output=True
        )

    return add


def test_sibling_index_missing_git_keeps_file_not_found_error(monkeypatch, tmp_path):
    monkeypatch.setattr(renderer.proc, "which", lambda _name: None)
    assert (
        renderer.SiblingIndex(str(tmp_path)).error
        == "could not list tracked files: FileNotFoundError"
    )


def test_hand_typed_expected_bytes_for_every_optional_field(
    run_renderer, write_artifact
):
    finding = make_finding(
        id="security-1",
        file="src/bug.py",
        line_start=10,
        line_end=12,
        title="Unsafe value",
        description="The value is stale.",
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


@pytest.mark.parametrize("wrapper", [False, True], ids=["array", "wrapper"])
def test_wrapper_and_bare_array_inputs_are_identical(
    wrapper, run_renderer, write_artifact
):
    write_artifact([make_finding(line_end=10)], wrapper=wrapper)
    task = output(run_renderer())[0]
    assert task["subject"] == "FIX: Avoid stale value"
    assert task["description"] == (
        "## Issue\nThe value is stale.\n\n"
        "## Location\n`src/bug.py:10`\n\n"
        "## Category\nmedium | bug\n\n"
        "## Toolchain\nNot detected. Set the test, lint, and build commands before running this task."
    )
    assert task["metadata"]["task_id"] == "FIX-bug-1"
    assert task["metadata"]["requirements"] == [
        {"id": "R-bug-1.1", "text": "The value is stale.", "testable": True}
    ]


@pytest.mark.parametrize("value", [float("nan"), float("inf")], ids=["nan", "infinity"])
def test_non_finite_confidence_is_a_content_failure(
    value, run_renderer, write_artifact
):
    write_artifact([make_finding(confidence=value)])
    result = run_renderer()
    assert result.returncode == 1
    assert result.stdout == ""


@pytest.mark.parametrize("shape", ["canonical", "alias", "both"])
def test_alias_only_input_matches_canonical_and_canonical_wins(
    shape, run_renderer, write_artifact
):
    values = make_finding(title="Canonical")
    if shape in ("canonical", "both"):
        values.update(line_start=11, line_end=12, description="canonical description")
    if shape in ("alias", "both"):
        if shape == "alias":
            for key in ("line_start", "description"):
                values.pop(key)
        values.update(
            line=11 if shape == "alias" else 99,
            end_line=12 if shape == "alias" else 88,
            body="canonical description" if shape == "alias" else "alias body",
        )
    write_artifact([values])
    task = output(run_renderer())[0]
    assert task["description"] == (
        "## Issue\ncanonical description\n\n"
        "## Location\n`src/bug.py:11-12`\n\n"
        "## Category\nmedium | bug\n\n"
        "## Toolchain\nNot detected. Set the test, lint, and build commands before running this task."
    )
    assert task["metadata"]["requirements"] == [
        {"id": "R-bug-1.1", "text": "canonical description", "testable": True}
    ]


@pytest.mark.usefixtures("symlink_or_skip")
def test_rejected_paths_degrade_each_finding_and_report_the_count(
    fix_root, run_renderer, tmp_path, write_artifact
):
    outside = tmp_path / "outside.py"
    outside.write_text("x", encoding="utf-8")
    link = fix_root / "link.py"
    os.symlink(outside, link)
    findings = [
        make_finding(id="traversal", file="../x.py"),
        make_finding(id="absolute", file=str(fix_root / "inside.py")),
        make_finding(id="symlink", file="link.py"),
    ]
    write_artifact(findings)
    result = run_renderer()
    tasks = output(result)
    assert len(tasks) == 3
    assert "3 paths rejected" in result.stderr
    for finding, task in zip(findings, tasks, strict=True):
        assert finding["file"] not in result.stdout
        assert "(path rejected: outside repo root)" in task["description"]
        assert task["metadata"]["scope"]["files_to_modify"] == []
        assert task["metadata"]["scope"]["patterns_to_follow"] == []
        assert task["metadata"]["commit"]["template"].split(":", 1)[0] == "fix(fix)"
        assert not any(
            a.get("type") == "file" for a in task["metadata"]["proof_artifacts"]
        )


@pytest.mark.parametrize(
    ("names", "commands"),
    [
        pytest.param(
            ("package.json", "Cargo.toml"),
            ("npm test", "npm run lint", "npm run build"),
            id="npm-before-cargo",
        ),
        pytest.param(
            ("Cargo.toml", "go.mod"),
            ("cargo test", "cargo clippy", "cargo build"),
            id="cargo-before-go",
        ),
        pytest.param(
            ("go.mod", "pyproject.toml"),
            ("go test ./...", "golangci-lint run", "go build ./..."),
            id="go-before-python",
        ),
        pytest.param(
            ("pyproject.toml", "Makefile"),
            ("pytest", "ruff check ."),
            id="python-before-make",
        ),
        pytest.param(
            ("Makefile", "a.csproj"),
            ("make test", "make lint", "make build"),
            id="make-before-dotnet",
        ),
        pytest.param(
            ("a.csproj",),
            ("dotnet test", "dotnet format --verify-no-changes", "dotnet build"),
            id="dotnet-tail",
        ),
    ],
)
def test_toolchain_adjacent_precedence_pairs(
    names, commands, fix_root, run_renderer, write_artifact
):
    for name in names:
        contents = (
            '{"scripts":{"test":"x","lint":"x","build":"x"}}'
            if name == "package.json"
            else "x"
        )
        (fix_root / name).write_text(contents, encoding="utf-8")
    write_artifact([make_finding()])
    task = output(run_renderer())[0]
    metadata = task["metadata"]
    assert metadata["proof_artifacts"] == [
        {"type": "test", "command": commands[0], "expected": "All pass"},
        {"type": "file", "path": "src/bug.py"},
    ]
    assert metadata["verification"] == {
        "pre": list(commands[1:]),
        "post": [commands[0]],
    }


def test_non_object_package_scripts_use_npm_defaults(
    fix_root, run_renderer, write_artifact
):
    (fix_root / "package.json").write_text(
        '{"scripts":["test:unit","lint:fix","build:prod"]}', encoding="utf-8"
    )
    write_artifact([make_finding()])

    metadata = output(run_renderer())[0]["metadata"]
    assert metadata["proof_artifacts"] == [
        {"type": "test", "command": "npm test", "expected": "All pass"},
        {"type": "file", "path": "src/bug.py"},
    ]
    assert metadata["verification"] == {
        "pre": ["npm run lint", "npm run build"],
        "post": ["npm test"],
    }


def test_package_scripts_custom_names_unsafe_names_and_invalid_json(
    fix_root, run_renderer, write_artifact
):
    package = fix_root / "package.json"
    package.write_text(
        json.dumps(
            {
                "scripts": {
                    "test:unit": "x",
                    "test:z": "x",
                    "lint:fix": "x",
                    "build:prod": "x",
                    "lint<script": "x",
                }
            }
        ),
        encoding="utf-8",
    )
    write_artifact([make_finding()])
    metadata = output(run_renderer())[0]["metadata"]
    proof = next(a for a in metadata["proof_artifacts"] if a["type"] == "test")
    assert proof["command"] == "npm run test:unit"
    assert metadata["verification"]["pre"] == ["npm run lint:fix", "npm run build:prod"]
    package.write_text("not json", encoding="utf-8")
    metadata = output(run_renderer())[0]["metadata"]
    proof = next(a for a in metadata["proof_artifacts"] if a["type"] == "test")
    assert proof["command"] == "npm test"


@pytest.mark.usefixtures("symlink_or_skip")
def test_outside_symlink_package_candidate_is_absent(
    fix_root, run_renderer, tmp_path, write_artifact
):
    outside = tmp_path / "package.json"
    outside.write_text('{"scripts":{"test":"outside"}}', encoding="utf-8")
    os.symlink(outside, fix_root / "package.json")
    (fix_root / "Cargo.toml").write_text("x", encoding="utf-8")
    write_artifact([make_finding()])
    result = run_renderer()
    metadata = output(result)[0]["metadata"]
    proof = next(a for a in metadata["proof_artifacts"] if a["type"] == "test")
    assert proof["command"] == "cargo test"
    assert "toolchain candidate package.json rejected" in result.stderr


@pytest.mark.parametrize("kind", ["directory", "large"])
def test_invalid_package_candidate_is_absent_for_directory_and_large_file(
    kind, fix_root, run_renderer, write_artifact
):
    package = fix_root / "package.json"
    if kind == "directory":
        package.mkdir()
    else:
        package.write_bytes(b"x" * (1024 * 1024 + 1))
    (fix_root / "Cargo.toml").write_text("x", encoding="utf-8")
    write_artifact([make_finding()])
    result = run_renderer()
    metadata = output(result)[0]["metadata"]
    assert metadata["verification"]["post"] == ["cargo test"]
    assert (
        len(
            [
                line
                for line in result.stderr.splitlines()
                if "toolchain candidate package.json rejected" in line
            ]
        )
        == 1
    )


def test_siblings_rank_and_exclude_delivered_own_untracked_with_a_two_file_cap(
    fix_root, git_add, git_repo, run_renderer, write_artifact
):
    git_repo()
    tracked = [
        "src/main.py",
        "src/delivered.py",
        "src/mainx.py",
        "src/mainy.py",
        "src/main_utils.py",
        "src/main.js",
    ]
    for path in tracked:
        target = fix_root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("x", encoding="utf-8")
    (fix_root / "src/untracked.py").write_text("x", encoding="utf-8")
    git_add(*tracked)
    write_artifact(
        [
            make_finding(file="src/main.py"),
            make_finding(id="delivered", file="src/delivered.py"),
        ]
    )
    tasks = output(run_renderer())
    assert tasks[0]["metadata"]["scope"]["patterns_to_follow"] == [
        "src/mainx.py",
        "src/mainy.py",
    ]
    assert "src/main.py" not in tasks[0]["metadata"]["scope"]["patterns_to_follow"]
    assert "src/delivered.py" not in tasks[0]["metadata"]["scope"]["patterns_to_follow"]
    assert "src/untracked.py" not in tasks[0]["metadata"]["scope"]["patterns_to_follow"]


def test_root_level_siblings_and_non_git_root(
    fix_root, git_add, git_repo, run_renderer, tmp_path, write_artifact
):
    git_repo()
    for name in ("main.py", "main_test.py", "root.js"):
        (fix_root / name).write_text("x", encoding="utf-8")
    git_add("main.py", "main_test.py", "root.js")
    write_artifact([make_finding(file="main.py")])
    task = output(run_renderer())[0]
    assert task["metadata"]["scope"]["patterns_to_follow"] == [
        "main_test.py",
        "root.js",
    ]
    nongit = tmp_path / "nongit"
    nongit.mkdir()
    write_artifact([make_finding(file="main.py")])
    result = run_renderer(root=nongit)
    task = output(result)[0]
    assert task["metadata"]["scope"]["patterns_to_follow"] == []
    assert "could not list tracked files" in result.stderr


@pytest.mark.usefixtures("symlink_or_skip")
def test_sibling_listing_uses_one_exact_root_git_call_per_run(
    fix_root, tmp_path, write_artifact, run_renderer
):
    root_alias = tmp_path / "repo-alias"
    os.symlink(fix_root, root_alias)
    findings = [
        make_finding(file="src/main.py"),
        make_finding(id="second", file="src/other.py"),
    ]
    fake_result = subprocess.CompletedProcess(
        ["git", "ls-files", "-z"],
        0,
        stdout=b"src/main.py\x00src/other.py\x00src/main_test.py\x00",
        stderr=b"",
    )
    calls = []

    def record_run(*args, **kwargs):
        calls.append((args, kwargs))
        return fake_result

    with patch.object(renderer.proc, "run_bytes", side_effect=record_run):
        write_artifact(findings)
        result = run_renderer(root=root_alias)
        assert result.returncode == 0
    assert len(calls) == 1
    assert calls[0][0] == (["git", "ls-files", "-z"],)
    assert calls[0][1] == {"cwd": os.path.realpath(root_alias), "timeout": 10}


@pytest.mark.parametrize(
    ("raw", "label", "complexity", "model", "category"),
    [
        pytest.param(
            "critical",
            "critical",
            "standard",
            "sonnet",
            "## Category\ncritical | bug",
            id="critical",
        ),
        pytest.param(
            " \u3000HiGh\u3000 ",
            "high",
            "standard",
            "sonnet",
            "## Category\nhigh | bug",
            id="high",
        ),
        pytest.param(
            "medium",
            "medium",
            "trivial",
            "haiku",
            "## Category\nmedium | bug",
            id="medium",
        ),
        pytest.param(
            "low", "low", "trivial", "haiku", "## Category\nlow | bug", id="low"
        ),
        pytest.param(
            "unknown", "low", "trivial", "haiku", "## Category\nlow | bug", id="unknown"
        ),
    ],
)
def test_normalized_severity_controls_category_complexity_and_model(
    raw, label, complexity, model, category, run_renderer, write_artifact
):
    write_artifact([make_finding(severity=raw)])
    task = output(run_renderer())[0]
    assert task["metadata"]["severity"] == label
    assert task["metadata"]["complexity"] == complexity
    assert task["metadata"]["model"] == model
    assert category_section(task) == category


def test_test_coverage_scope_uses_confined_refs_and_production_file_proof(
    fix_root, git_add, git_repo, run_renderer, write_artifact
):
    git_repo()
    for path in ("src/prod.py", "tests/test_prod.py", "tests/test_other.py"):
        target = fix_root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("x", encoding="utf-8")
    git_add("src/prod.py", "tests/test_prod.py", "tests/test_other.py")
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
    assert first["metadata"]["scope"]["patterns_to_follow"] == []
    assert second["metadata"]["scope"]["files_to_modify"] == []
    assert second["metadata"]["scope"]["patterns_to_follow"] == []
    assert first["metadata"]["proof_artifacts"] == [
        {"type": "file", "path": "src/prod.py"}
    ]
    assert "1 path rejected" in result.stderr


@pytest.mark.parametrize(
    ("malformed", "refs", "patterns", "summary"),
    [
        pytest.param(
            "tests/test_prod.py",
            [],
            ["src/prod_test.py"],
            "(0 paths rejected)",
            id="non-list",
        ),
        pytest.param(
            ["tests/test_prod.py", 7],
            ["tests/test_prod.py"],
            [],
            "(1 path rejected)",
            id="mixed-list",
        ),
    ],
)
def test_malformed_cross_file_refs_are_ignored_everywhere(
    malformed,
    refs,
    patterns,
    summary,
    fix_root,
    git_add,
    git_repo,
    run_renderer,
    write_artifact,
):
    git_repo()
    for path in ("src/prod.py", "src/prod_test.py"):
        target = fix_root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("x", encoding="utf-8")
    git_add("src/prod.py", "src/prod_test.py")
    write_artifact(
        [
            make_finding(
                dimension="test_coverage",
                file="src/prod.py",
                cross_file_refs=malformed,
                criticality=7,
                failure_scenario="No test catches it",
            )
        ]
    )
    result = run_renderer()
    task = output(result)[0]
    assert task["metadata"]["scope"]["files_to_modify"] == refs
    assert task["metadata"]["scope"]["patterns_to_follow"] == patterns
    assert task["metadata"]["review_context"]["cross_file_refs"] == refs
    assert summary in result.stderr


def test_setext_headings_are_escaped_in_description_and_suggestion(
    run_renderer, write_artifact
):
    write_artifact(
        [
            make_finding(
                description="Description H1\n====\n\nDescription H2\n----",
                suggestion="Suggestion H1\n====\n\nSuggestion H2\n----",
            )
        ]
    )
    description = output(run_renderer())[0]["description"]
    assert "Description H1\n\\====" in description
    assert "Description H2\n\\----" in description
    assert "Suggestion H1\n\\====" in description
    assert "Suggestion H2\n\\----" in description


@pytest.mark.parametrize(
    ("dimension", "values", "expected"),
    [
        pytest.param(
            "security",
            {"attack_vector": "SQL injection"},
            "**Attack vector:** SQL injection",
            id="text",
        ),
        pytest.param(
            "cross_file_impact",
            {"affected_consumers": ["src/a.py", "src/b.py"]},
            "**Affected consumers:** src/a.py, src/b.py",
            id="list",
        ),
        pytest.param(
            "test_coverage", {"criticality": 7}, "**Criticality:** 7", id="number"
        ),
        pytest.param(
            "convention",
            {"rule_source": "repo_precedent"},
            None,
            id="source-without-rule",
        ),
    ],
)
def test_every_generated_detail_field_renders(
    dimension, values, expected, run_renderer, write_artifact
):
    write_artifact([make_finding(dimension=dimension, **values)])
    description = output(run_renderer())[0]["description"]
    if expected is None:
        assert "## Details" not in description
        assert "Rule source" not in description
        assert "repo_precedent" not in description
    else:
        details = description.split("## Details\n", 1)[1].split("\n\n## Toolchain", 1)[
            0
        ]
        assert details == expected


def test_absolute_inside_nul_non_directory_missing_root_and_nonexistent_confined(
    fix_root, run_renderer, write_artifact
):
    inside = fix_root / "inside.py"
    findings = [
        make_finding(id="absolute", file=str(inside)),
        make_finding(id="nul", file="src/\x00bad.py"),
        make_finding(id="missing", file="src/deleted.py"),
    ]
    write_artifact(findings)
    result = run_renderer()
    tasks = output(result)
    assert tasks[0]["metadata"]["scope"]["files_to_modify"] == []
    assert tasks[1]["metadata"]["scope"]["files_to_modify"] == []
    assert tasks[2]["metadata"]["scope"]["files_to_modify"] == ["src/deleted.py"]
    assert "2 paths rejected" in result.stderr


def test_windows_absolute_path_is_rejected(
    fix_artifact, fix_root, run_renderer, write_artifact, invoke, tmp_path
):
    write_artifact([make_finding(file=r"C:\Windows\System32\hosts")])
    result = run_renderer()
    task = output(result)[0]
    assert task["metadata"]["scope"]["files_to_modify"] == []
    assert "(1 path rejected)" in result.stderr
    missing_root = run_renderer(root=fix_root / "does-not-exist")
    assert missing_root.returncode == 1
    assert missing_root.stdout == ""
    file_root = fix_root / "not-dir"
    file_root.write_text("x", encoding="utf-8")
    not_dir = run_renderer(root=file_root)
    assert not_dir.returncode == 1
    assert not_dir.stdout == ""
    malformed = invoke("render_fix_tasks", [str(fix_artifact)], tmp_path)
    assert malformed.returncode == 2
    assert malformed.stdout == b""
    assert malformed.stderr == (
        b"render_fix_tasks: the following arguments are required: --repo-root\n"
    )


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("repo_precedent", "**Repo precedent:** Use the project rule."),
        ("new_kind", "**Cited rule:** Use the project rule."),
    ],
    ids=["repo-label", "fallback-label"],
)
def test_rule_source_is_a_label_for_cited_rule_details(
    source, expected, run_renderer, write_artifact
):
    write_artifact(
        [
            make_finding(
                dimension="convention",
                claude_md_rule="Use the project rule.",
                rule_source=source,
            )
        ]
    )
    description = output(run_renderer())[0]["description"]
    details = description.split("## Details\n", 1)[1].split("\n\n## Toolchain", 1)[0]
    assert details == expected
    assert "Rule source" not in description
    assert source not in description


def test_hostile_markdown_is_sanitized_and_evidence_fence_is_long_enough(
    run_renderer, write_artifact
):
    evidence = "payload````\r\n```\r\n<!-- evidence -->"
    finding = make_finding(
        title="Bad\n## forged\r<!-- title -->",
        description="first\r\n## Evidence\n<!-- hidden -->\tend\x01",
        suggestion="fix\n## Category\n<!-- suggestion -->\x02done",
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


def test_stdout_is_full_json_and_status_is_stderr(
    fix_artifact, run_renderer, write_artifact
):
    write_artifact([make_finding()])
    result = run_renderer()
    assert result.returncode == 0
    assert isinstance(json.loads(result.stdout), list)
    assert "Rendered" not in result.stdout
    assert (
        f"Rendered 1 FIX task from {fix_artifact} (0 paths rejected)" in result.stderr
    )


def test_caller_fix_protocol():
    root = Path(__file__).resolve().parents[1]
    skill = (root / "skills/code-gauntlet/SKILL.md").read_text(encoding="utf-8")
    question = skill.index(
        'question: "Create fix tasks on the task board from these findings?"'
    )
    invocation = skill.index(
        'python3 "{plugin_root}/scripts/render_fix_tasks.py" "<artifactPaths.postReview>" --repo-root "<repoRoot>"'
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
    phase8 = (root / "skills/code-gauntlet/references/phase8-delivery.md").read_text(
        encoding="utf-8"
    )
    assert "fix-task-metadata.md" not in phase8


def test_caller_fix_help(invoke, tmp_path):
    result = invoke("render_fix_tasks", ["--help"], tmp_path)
    assert result.returncode == 0
    assert result.stderr == b""
    assert result.stdout.startswith(
        b"usage: render_fix_tasks [-h] --repo-root REPO_ROOT POST_REVIEW\n"
    )
    assert (
        b"Render persisted review findings as deterministic FIX-task payloads."
        in result.stdout
    )

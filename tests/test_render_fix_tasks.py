"""Unittest coverage for the deterministic FIX-task renderer."""

import ast
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import generate_contract_requirements as contract_generator
from scripts import render_fix_tasks as renderer

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "render_fix_tasks.py"


class RenderFixTasksTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "repo"
        self.root.mkdir()
        self.artifact = self.root / "post-review.json"

    def finding(self, **changes):
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

    def write_artifact(self, findings, wrapper=False, raw=None):
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
        self.artifact.write_text(raw, encoding="utf-8")

    def run_renderer(self, root=None, artifact=None, *extra):
        result = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                str(artifact or self.artifact),
                "--repo-root",
                str(root or self.root),
                *extra,
            ],
            capture_output=True,
            text=True,
        )
        return result

    def output(self, result):
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_hand_typed_expected_bytes_for_every_optional_field(self):
        finding = self.finding(
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
        self.write_artifact([finding])
        result = self.run_renderer()
        expected_description = (
            "## Issue\n"
            "The value is stale.\n\n"
            "## Location\n"
            "`src/bug.py:10-12`\n\n"
            "## Evidence\n"
            "```\n"
            "Observed `bad` input.\n"
            "```\n\n"
            "## Suggested Fix\n"
            "Reject the input.\n\n"
            "## Category\n"
            "critical | security\n\n"
            "## Details\n"
            "**Attack vector:** SQL injection\n\n"
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
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            result.stdout, json.dumps([expected], indent=2, ensure_ascii=False) + "\n"
        )

    def test_only_required_fields_apply_all_omission_rules(self):
        self.write_artifact([self.finding()])
        task = self.output(self.run_renderer())[0]
        self.assertNotIn("## Evidence", task["description"])
        self.assertNotIn("## Suggested Fix", task["description"])
        self.assertNotIn("## Details", task["description"])
        self.assertIn("## Location\n`src/bug.py:10`", task["description"])
        self.assertIn("## Toolchain\nNot detected.", task["description"])
        context = task["metadata"]["review_context"]
        self.assertEqual(context["evidence"], "")
        self.assertEqual(context["cross_file_refs"], [])
        self.assertEqual(context["blame_classification"], "unknown")

    def test_wrapper_and_bare_array_inputs_are_identical(self):
        finding = self.finding(line_end=10)
        self.write_artifact([finding], wrapper=False)
        bare = self.run_renderer()
        self.write_artifact([finding], wrapper=True)
        wrapped = self.run_renderer()
        self.assertEqual(bare.returncode, wrapped.returncode)
        self.assertEqual(bare.stdout, wrapped.stdout)

    def test_unrecognized_shape_is_a_content_failure_with_empty_stdout(self):
        self.write_artifact([], raw=json.dumps({"review": []}))
        result = self.run_renderer()
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")

    def test_invalid_json_is_a_content_failure(self):
        self.write_artifact([], raw="{")
        result = self.run_renderer()
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")

    def test_non_finite_confidence_is_a_content_failure(self):
        for value in (float("nan"), float("inf")):
            with self.subTest(value=value):
                self.write_artifact([self.finding(confidence=value)])
                result = self.run_renderer()
                self.assertEqual(result.returncode, 1)
                self.assertEqual(result.stdout, "")

    def test_missing_required_key_is_a_content_failure(self):
        finding = self.finding()
        del finding["dimension"]
        self.write_artifact([finding])
        result = self.run_renderer()
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")

    def test_unreadable_artifact_and_non_object_finding_are_content_failures(self):
        unreadable = self.run_renderer(artifact=self.root / "missing.json")
        self.assertEqual(unreadable.returncode, 1)
        self.assertEqual(unreadable.stdout, "")
        self.write_artifact(["not a finding"])
        non_object = self.run_renderer()
        self.assertEqual(non_object.returncode, 1)
        self.assertEqual(non_object.stdout, "")

    def test_alias_only_input_matches_canonical_and_canonical_wins(self):
        canonical = self.finding(
            line_start=11,
            line_end=12,
            description="canonical description",
            title="Canonical",
        )
        alias = dict(canonical)
        for key in ("line_start", "line_end", "description"):
            alias.pop(key)
        alias.update({"line": 11, "end_line": 12, "body": "canonical description"})
        self.write_artifact([canonical])
        canonical_result = self.run_renderer()
        self.write_artifact([alias])
        alias_result = self.run_renderer()
        self.assertEqual(canonical_result.stdout, alias_result.stdout)

        both = dict(
            alias,
            line_start=11,
            line_end=12,
            description="canonical description",
            line=99,
            end_line=88,
            body="alias body",
        )
        self.write_artifact([both])
        task = self.output(self.run_renderer())[0]
        issue, location = task["description"].split("\n\n## Location\n", 1)
        self.assertEqual(issue, "## Issue\ncanonical description")
        self.assertTrue(location.startswith("`src/bug.py:11-12`\n\n"))
        self.assertNotIn("alias body", task["description"])

    def test_rejected_paths_degrade_each_finding_and_report_the_count(self):
        outside = Path(self.tmp.name) / "outside.py"
        outside.write_text("x", encoding="utf-8")
        link = self.root / "link.py"
        os.symlink(outside, link)
        findings = [
            self.finding(id="traversal", file="../x.py"),
            self.finding(id="absolute", file=str(self.root / "inside.py")),
            self.finding(id="symlink", file="link.py"),
        ]
        self.write_artifact(findings)
        result = self.run_renderer()
        tasks = self.output(result)
        self.assertEqual(len(tasks), 3)
        self.assertIn("3 paths rejected", result.stderr)
        for finding, task in zip(findings, tasks, strict=True):
            self.assertNotIn(finding["file"], result.stdout)
            self.assertIn("(path rejected: outside repo root)", task["description"])
            self.assertEqual(task["metadata"]["scope"]["files_to_modify"], [])
            self.assertEqual(task["metadata"]["scope"]["patterns_to_follow"], [])
            self.assertEqual(
                task["metadata"]["commit"]["template"].split(":", 1)[0], "fix(fix)"
            )
            self.assertFalse(
                any(
                    a.get("type") == "file" for a in task["metadata"]["proof_artifacts"]
                )
            )

    def test_toolchain_each_single_config(self):
        cases = [
            (
                "package.json",
                '{"scripts":{"test":"x","lint":"x","build":"x"}}',
                ["npm test", "npm run lint", "npm run build"],
            ),
            (
                "Cargo.toml",
                "[package]\nname='x'\n",
                ["cargo test", "cargo clippy", "cargo build"],
            ),
            (
                "go.mod",
                "module example.com/x\n",
                ["go test ./...", "golangci-lint run", "go build ./..."],
            ),
            ("pyproject.toml", "[tool.pytest]\n", ["pytest", "ruff check ."]),
            ("Makefile", "test:\n\n", ["make test", "make lint", "make build"]),
            (
                "demo.csproj",
                "<Project />\n",
                ["dotnet test", "dotnet format --verify-no-changes", "dotnet build"],
            ),
        ]
        for name, contents, commands in cases:
            with self.subTest(config=name):
                path = self.root / name
                path.write_text(contents, encoding="utf-8")
                self.write_artifact([self.finding()])
                metadata = self.output(self.run_renderer())[0]["metadata"]
                proof = next(
                    a for a in metadata["proof_artifacts"] if a["type"] == "test"
                )
                self.assertEqual(proof["command"], commands[0])
                self.assertEqual(metadata["verification"]["pre"], commands[1:])
                self.assertEqual(metadata["verification"]["post"], [commands[0]])
                path.unlink()

    def test_toolchain_adjacent_precedence_pairs(self):
        cases = [
            (("package.json", "Cargo.toml"), "npm test"),
            (("Cargo.toml", "go.mod"), "cargo test"),
            (("go.mod", "pyproject.toml"), "go test ./..."),
            (("pyproject.toml", "Makefile"), "pytest"),
            (("Makefile", "a.csproj"), "make test"),
        ]
        for names, command in cases:
            with self.subTest(names=names):
                for name in names:
                    (self.root / name).write_text(
                        "{}" if name == "package.json" else "x", encoding="utf-8"
                    )
                self.write_artifact([self.finding()])
                metadata = self.output(self.run_renderer())[0]["metadata"]
                proof = next(
                    a for a in metadata["proof_artifacts"] if a["type"] == "test"
                )
                self.assertEqual(proof["command"], command)
                for name in names:
                    (self.root / name).unlink()

    def test_package_scripts_custom_names_unsafe_names_and_invalid_json(self):
        package = self.root / "package.json"
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
        self.write_artifact([self.finding()])
        metadata = self.output(self.run_renderer())[0]["metadata"]
        proof = next(a for a in metadata["proof_artifacts"] if a["type"] == "test")
        self.assertEqual(proof["command"], "npm run test:unit")
        self.assertEqual(
            metadata["verification"]["pre"], ["npm run lint:fix", "npm run build:prod"]
        )
        package.write_text("not json", encoding="utf-8")
        metadata = self.output(self.run_renderer())[0]["metadata"]
        proof = next(a for a in metadata["proof_artifacts"] if a["type"] == "test")
        self.assertEqual(proof["command"], "npm test")

    def test_nothing_detected_has_no_command_fields_or_sentinel(self):
        self.write_artifact([self.finding()])
        task = self.output(self.run_renderer())[0]
        metadata = task["metadata"]
        self.assertEqual(
            metadata["proof_artifacts"], [{"type": "file", "path": "src/bug.py"}]
        )
        self.assertEqual(metadata["verification"], {"pre": [], "post": []})
        self.assertIn(
            "Not detected. Set the test, lint, and build commands before running this task.",
            task["description"],
        )
        self.assertNotIn('"command": "NO"', json.dumps(task))

    def test_multiple_csproj_and_sln_files_choose_sorted_first(self):
        (self.root / "z.sln").write_text("x", encoding="utf-8")
        (self.root / "a.csproj").write_text("x", encoding="utf-8")
        (self.root / "b.csproj").write_text("x", encoding="utf-8")
        self.write_artifact([self.finding()])
        metadata = self.output(self.run_renderer())[0]["metadata"]
        self.assertEqual(metadata["verification"]["post"], ["dotnet test"])

    def test_outside_symlink_package_candidate_is_absent(self):
        outside = Path(self.tmp.name) / "package.json"
        outside.write_text('{"scripts":{"test":"outside"}}', encoding="utf-8")
        os.symlink(outside, self.root / "package.json")
        (self.root / "Cargo.toml").write_text("x", encoding="utf-8")
        self.write_artifact([self.finding()])
        result = self.run_renderer()
        metadata = self.output(result)[0]["metadata"]
        proof = next(a for a in metadata["proof_artifacts"] if a["type"] == "test")
        self.assertEqual(proof["command"], "cargo test")
        self.assertIn("toolchain candidate package.json rejected", result.stderr)

    def test_invalid_package_candidate_is_absent_for_directory_and_large_file(self):
        for kind in ("directory", "large"):
            with self.subTest(kind=kind):
                package = self.root / "package.json"
                if kind == "directory":
                    package.mkdir()
                else:
                    package.write_bytes(b"x" * (1024 * 1024 + 1))
                (self.root / "Cargo.toml").write_text("x", encoding="utf-8")
                self.write_artifact([self.finding()])
                result = self.run_renderer()
                metadata = self.output(result)[0]["metadata"]
                proof = next(
                    a for a in metadata["proof_artifacts"] if a["type"] == "test"
                )
                self.assertEqual(proof["command"], "cargo test")
                notes = [
                    line
                    for line in result.stderr.splitlines()
                    if "toolchain candidate package.json rejected" in line
                ]
                self.assertEqual(len(notes), 1)
                if kind == "directory":
                    package.rmdir()
                else:
                    package.unlink()
                (self.root / "Cargo.toml").unlink()

    def git_repo(self):
        subprocess.run(["git", "init"], cwd=self.root, check=True, capture_output=True)

    def git_add(self, *paths):
        subprocess.run(
            ["git", "add", *paths], cwd=self.root, check=True, capture_output=True
        )

    def test_siblings_rank_and_exclude_delivered_own_untracked_with_a_two_file_cap(
        self,
    ):
        self.git_repo()
        tracked = [
            "src/main.py",
            "src/delivered.py",
            "src/mainx.py",
            "src/mainy.py",
            "src/main_utils.py",
            "src/main.js",
        ]
        for path in tracked:
            target = self.root / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("x", encoding="utf-8")
        (self.root / "src/untracked.py").write_text("x", encoding="utf-8")
        self.git_add(*tracked)
        self.write_artifact(
            [
                self.finding(file="src/main.py"),
                self.finding(id="delivered", file="src/delivered.py"),
            ]
        )
        tasks = self.output(self.run_renderer())
        self.assertEqual(
            tasks[0]["metadata"]["scope"]["patterns_to_follow"],
            ["src/mainx.py", "src/mainy.py"],
        )
        self.assertNotIn(
            "src/main.py", tasks[0]["metadata"]["scope"]["patterns_to_follow"]
        )
        self.assertNotIn(
            "src/delivered.py", tasks[0]["metadata"]["scope"]["patterns_to_follow"]
        )
        self.assertNotIn(
            "src/untracked.py", tasks[0]["metadata"]["scope"]["patterns_to_follow"]
        )

    def test_root_level_siblings_and_non_git_root(self):
        self.git_repo()
        for name in ("main.py", "main_test.py", "root.js"):
            (self.root / name).write_text("x", encoding="utf-8")
        self.git_add("main.py", "main_test.py", "root.js")
        self.write_artifact([self.finding(file="main.py")])
        task = self.output(self.run_renderer())[0]
        self.assertEqual(
            task["metadata"]["scope"]["patterns_to_follow"], ["main_test.py", "root.js"]
        )

        nongit = Path(self.tmp.name) / "nongit"
        nongit.mkdir()
        self.write_artifact([self.finding(file="main.py")])
        result = self.run_renderer(root=nongit)
        task = self.output(result)[0]
        self.assertEqual(task["metadata"]["scope"]["patterns_to_follow"], [])
        self.assertIn("could not list tracked files", result.stderr)

    def test_sibling_listing_uses_one_exact_root_git_call_per_run(self):
        root_alias = Path(self.tmp.name) / "repo-alias"
        os.symlink(self.root, root_alias)
        findings = [
            self.finding(file="src/main.py"),
            self.finding(id="second", file="src/other.py"),
        ]
        fake_result = subprocess.CompletedProcess(
            ["git", "ls-files", "-z"],
            0,
            stdout=b"src/main.py\0src/other.py\0src/main_test.py\0",
            stderr=b"",
        )
        calls = []

        def record_run(*args, **kwargs):
            calls.append((args, kwargs))
            return fake_result

        with patch.object(renderer.subprocess, "run", side_effect=record_run):
            renderer.build_tasks(findings, os.path.realpath(root_alias), [])

        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], (["git", "ls-files", "-z"],))
        self.assertEqual(
            calls[0][1],
            {
                "cwd": os.path.realpath(root_alias),
                "capture_output": True,
                "timeout": 10,
                "check": False,
            },
        )

    def test_complexity_model_and_unknown_severity_mapping(self):
        severities = ["critical", "high", "medium", "low", "unknown"]
        self.write_artifact(
            [
                self.finding(id=severity, severity=severity, file=f"{severity}.py")
                for severity in severities
            ]
        )
        tasks = self.output(self.run_renderer())
        for severity, task in zip(severities, tasks, strict=True):
            expected = "trivial" if severity in ("medium", "low") else "standard"
            self.assertEqual(task["metadata"]["complexity"], expected)
            self.assertEqual(
                task["metadata"]["model"],
                "haiku" if expected == "trivial" else "sonnet",
            )

    def test_test_coverage_scope_uses_confined_refs_and_production_file_proof(self):
        self.git_repo()
        for path in ("src/prod.py", "tests/test_prod.py", "tests/test_other.py"):
            target = self.root / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("x", encoding="utf-8")
        self.git_add("src/prod.py", "tests/test_prod.py", "tests/test_other.py")
        findings = [
            self.finding(
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
            self.finding(
                id="without-refs",
                dimension="test_coverage",
                file="src/prod.py",
                criticality=3,
                failure_scenario="No test exists",
            ),
        ]
        self.write_artifact(findings)
        result = self.run_renderer()
        tasks = self.output(result)
        first, second = tasks
        self.assertEqual(
            first["metadata"]["scope"]["files_to_modify"],
            ["tests/test_prod.py", "tests/test_other.py"],
        )
        self.assertEqual(first["metadata"]["scope"]["patterns_to_follow"], [])
        self.assertEqual(second["metadata"]["scope"]["files_to_modify"], [])
        self.assertEqual(second["metadata"]["scope"]["patterns_to_follow"], [])
        self.assertEqual(
            first["metadata"]["proof_artifacts"],
            [{"type": "file", "path": "src/prod.py"}],
        )
        self.assertIn("1 path rejected", result.stderr)

    def test_malformed_cross_file_refs_are_ignored_everywhere(self):
        self.git_repo()
        for path in ("src/prod.py", "src/prod_test.py"):
            target = self.root / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("x", encoding="utf-8")
        self.git_add("src/prod.py", "src/prod_test.py")
        cases = (
            ("tests/test_prod.py", [], 0),
            (["tests/test_prod.py", 7], ["tests/test_prod.py"], 1),
            (7, [], 0),
        )
        for malformed, expected_refs, expected_rejected in cases:
            with self.subTest(malformed=malformed):
                self.write_artifact(
                    [
                        self.finding(
                            dimension="test_coverage",
                            file="src/prod.py",
                            cross_file_refs=malformed,
                            criticality=7,
                            failure_scenario="No test catches it",
                        )
                    ]
                )
                result = self.run_renderer()
                task = self.output(result)[0]
                scope = task["metadata"]["scope"]
                self.assertEqual(scope["files_to_modify"], expected_refs)
                self.assertEqual(
                    scope["patterns_to_follow"],
                    [] if expected_refs else ["src/prod_test.py"],
                )
                self.assertEqual(
                    task["metadata"]["review_context"]["cross_file_refs"],
                    expected_refs,
                )
                summary = f"({expected_rejected} {'path' if expected_rejected == 1 else 'paths'} rejected)"
                self.assertIn(summary, result.stderr)

    def test_setext_headings_are_escaped_in_description_and_suggestion(self):
        self.write_artifact(
            [
                self.finding(
                    description="Description H1\n====\n\nDescription H2\n----",
                    suggestion="Suggestion H1\n====\n\nSuggestion H2\n----",
                )
            ]
        )
        description = self.output(self.run_renderer())[0]["description"]
        self.assertIn("Description H1\n\\====", description)
        self.assertIn("Description H2\n\\----", description)
        self.assertIn("Suggestion H1\n\\====", description)
        self.assertIn("Suggestion H2\n\\----", description)

    def test_dimension_details_for_cross_file_impact_and_convention(self):
        findings = [
            self.finding(
                id="impact",
                dimension="cross_file_impact",
                affected_consumers=["src/a.py", "src/b.py"],
            ),
            self.finding(
                id="convention",
                dimension="convention",
                claude_md_rule="Use the project rule.",
            ),
        ]
        self.write_artifact(findings)
        tasks = self.output(self.run_renderer())
        self.assertIn(
            "**Affected consumers:** src/a.py, src/b.py", tasks[0]["description"]
        )
        self.assertIn("**Cited rule:** Use the project rule.", tasks[1]["description"])

    def test_every_generated_detail_field_renders_and_matches_registry(self):
        identity = contract_generator.load_registry(str(REPO))
        source = SCRIPT.read_text(encoding="utf-8")
        marker = "# generated-from-registry-identity:detail_fields"
        start = source.index("_DETAIL_FIELDS_BY_DIMENSION = {", source.index(marker))
        end = source.index("# /generated-from-registry-identity:detail_fields", start)
        actual_fence = source[start:end].rstrip()
        expected_fence = "\n".join(
            contract_generator.identity_body(
                "scripts/render_fix_tasks.py", "detail_fields", identity, str(REPO)
            )
        )
        self.assertEqual(actual_fence, expected_fence)

        for dimension, fields in renderer._DETAIL_FIELDS_BY_DIMENSION.items():
            for field in fields:
                with self.subTest(dimension=dimension, field=field):
                    value = 7 if field == "criticality" else f"{field} value"
                    self.write_artifact(
                        [
                            self.finding(
                                id=f"{dimension}-{field}",
                                dimension=dimension,
                                **{field: value},
                            )
                        ]
                    )
                    task = self.output(self.run_renderer())[0]
                    if field == "rule_source":
                        self.assertNotIn("Rule source", task["description"])
                        self.assertNotIn(value, task["description"])
                        continue
                    details = task["description"].split("## Details\n", 1)[1]
                    details = details.split("\n\n## Toolchain", 1)[0]
                    label = (
                        "Cited rule"
                        if field == "claude_md_rule"
                        else field.replace("_", " ").capitalize()
                    )
                    self.assertEqual(details, f"**{label}:** {value}")

    def test_absolute_inside_nul_non_directory_missing_root_and_nonexistent_confined(
        self,
    ):
        inside = self.root / "inside.py"
        findings = [
            self.finding(id="absolute", file=str(inside)),
            self.finding(id="nul", file="src/\x00bad.py"),
            self.finding(id="missing", file="src/deleted.py"),
        ]
        self.write_artifact(findings)
        result = self.run_renderer()
        tasks = self.output(result)
        self.assertEqual(tasks[0]["metadata"]["scope"]["files_to_modify"], [])
        self.assertEqual(tasks[1]["metadata"]["scope"]["files_to_modify"], [])
        self.assertEqual(
            tasks[2]["metadata"]["scope"]["files_to_modify"], ["src/deleted.py"]
        )
        self.assertIn("2 paths rejected", result.stderr)

    def test_windows_absolute_path_is_rejected(self):
        self.write_artifact([self.finding(file=r"C:\Windows\System32\hosts")])
        result = self.run_renderer()
        task = self.output(result)[0]
        self.assertEqual(task["metadata"]["scope"]["files_to_modify"], [])
        self.assertIn("(1 path rejected)", result.stderr)

        missing_root = self.run_renderer(root=self.root / "does-not-exist")
        self.assertEqual(missing_root.returncode, 1)
        self.assertEqual(missing_root.stdout, "")
        file_root = self.root / "not-dir"
        file_root.write_text("x", encoding="utf-8")
        not_dir = self.run_renderer(root=file_root)
        self.assertEqual(not_dir.returncode, 1)
        self.assertEqual(not_dir.stdout, "")
        malformed = subprocess.run(
            [sys.executable, str(SCRIPT), str(self.artifact)],
            capture_output=True,
            text=True,
        )
        self.assertEqual(malformed.returncode, 2)
        self.assertEqual(malformed.stdout, "")
        self.assertIn("usage:", malformed.stderr)
        self.assertIn(
            "error: the following arguments are required: --repo-root",
            malformed.stderr,
        )

    def test_lone_surrogate_subprocess_round_trips(self):
        self.write_artifact([self.finding(description="bad\ud800", title="surrogate")])
        result = self.run_renderer()
        tasks = self.output(result)
        self.assertEqual(tasks[0]["metadata"]["requirements"][0]["text"], "bad\ud800")
        self.assertEqual(tasks[0]["metadata"]["review_context"]["evidence"], "")

    def test_non_string_origin_is_unknown(self):
        self.write_artifact([self.finding(origin=7)])
        task = self.output(self.run_renderer())[0]
        self.assertEqual(
            task["metadata"]["review_context"]["blame_classification"], "unknown"
        )

    def test_rule_source_is_a_label_for_cited_rule_details(self):
        for source, label in (
            ("repo_precedent", "Repo precedent"),
            ("new_kind", "Cited rule"),
        ):
            with self.subTest(source=source):
                self.write_artifact(
                    [
                        self.finding(
                            dimension="convention",
                            claude_md_rule="Use the project rule.",
                            rule_source=source,
                        )
                    ]
                )
                task = self.output(self.run_renderer())[0]
                details = task["description"].split("## Details\n", 1)[1]
                self.assertIn(f"**{label}:** Use the project rule.", details)
                self.assertNotIn("Rule source", details)
                self.assertNotIn(source, task["description"])

    def test_hostile_markdown_is_sanitized_and_evidence_fence_is_long_enough(self):
        evidence = "payload````\r\n```\r\n<!-- evidence -->"
        finding = self.finding(
            title="Bad\n## forged\r<!-- title -->",
            description="first\r\n## Evidence\n<!-- hidden -->\tend\x01",
            suggestion="fix\n## Category\n<!-- suggestion -->\x02done",
            evidence=evidence,
        )
        self.write_artifact([finding])
        task = self.output(self.run_renderer())[0]
        description = task["description"]
        self.assertEqual(
            len([line for line in description.splitlines() if line == "## Evidence"]), 1
        )
        self.assertEqual(
            len(
                [
                    line
                    for line in description.splitlines()
                    if line == "## Suggested Fix"
                ]
            ),
            1,
        )
        self.assertNotIn("<!--", description.replace(evidence, ""))
        self.assertNotIn("\x01", description)
        self.assertNotIn("\x02", description)
        self.assertNotIn("\t", description)
        self.assertIn("\\## Evidence", description)
        evidence_block = description.rsplit("## Evidence\n", 1)[1].split(
            "\n\n## Suggested Fix", 1
        )[0]
        fence_lines = evidence_block.splitlines()
        longest_content_run = max(len(run) for run in re.findall(r"`+", evidence))
        self.assertGreater(len(fence_lines[0]), longest_content_run)
        self.assertGreater(len(fence_lines[-1]), longest_content_run)
        self.assertEqual(fence_lines[1:-1], evidence.splitlines())
        self.assertIn("FIX: Bad ## forged &lt;!-- title -->", task["subject"])

    def test_stdout_is_full_json_and_status_is_stderr(self):
        self.write_artifact([self.finding()])
        result = self.run_renderer()
        self.assertEqual(result.returncode, 0)
        self.assertIsInstance(json.loads(result.stdout), list)
        self.assertNotIn("Rendered", result.stdout)
        self.assertIn(
            f"Rendered 1 FIX task from {self.artifact} (0 paths rejected)",
            result.stderr,
        )

    def test_rejection_summary_uses_singular_path(self):
        self.write_artifact([self.finding(file="../outside.py")])
        result = self.run_renderer()
        self.assertEqual(result.returncode, 0)
        self.assertIn("Rendered 1 FIX task", result.stderr)
        self.assertIn("(1 path rejected)", result.stderr)

    def test_renderer_imports_only_stdlib_and_local_script_io(self):
        tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
        allowed = set(sys.stdlib_module_names) | {"script_io"}
        imports = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.extend(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imports.append(node.module.split(".")[0])
        self.assertTrue(set(imports) <= allowed, sorted(set(imports) - allowed))

    def test_skill_wiring_uses_the_renderer_and_task_call_order(self):
        skill = (REPO / "skills" / "code-gauntlet" / "SKILL.md").read_text(
            encoding="utf-8"
        )
        question = skill.index(
            'question: "Create fix tasks on the task board from these findings?"'
        )
        invocation = skill.index(
            'python3 "{plugin_root}/scripts/render_fix_tasks.py" "<artifactPaths.postReview>" --repo-root "<repoRoot>"'
        )
        next_section = skill.index("### Print methodology")
        self.assertGreater(invocation, question)
        self.assertLess(invocation, next_section)
        block = skill[invocation:next_section]
        for phrase in (
            "Parse stdout as a JSON array",
            "assert that its length equals the delivered count",
            "TaskCreate(subject, description)",
            "and then `TaskUpdate(taskId, metadata)`",
            "Stop on any parse, count, or call failure",
            "Never fall back to hand composition",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, block)
        phase8 = (
            REPO / "skills" / "code-gauntlet" / "references" / "phase8-delivery.md"
        ).read_text(encoding="utf-8")
        self.assertNotIn("fix-task-metadata.md", phase8)


if __name__ == "__main__":
    unittest.main()

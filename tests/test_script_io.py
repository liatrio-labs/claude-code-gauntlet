"""Tests for shared script I/O and CLI bootstrap behavior."""

import ast
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch

from scripts.script_io import utf8_stdio, write_result

REPO = Path(__file__).resolve().parents[1]
BOOTSTRAP_ROOTS = ("scripts/", ".github/", "workflows/test/tools/")


def _main_guard(node):
    return (
        isinstance(node, ast.If)
        and isinstance(node.test, ast.Compare)
        and isinstance(node.test.left, ast.Name)
        and node.test.left.id == "__name__"
        and len(node.test.ops) == 1
        and isinstance(node.test.ops[0], ast.Eq)
        and len(node.test.comparators) == 1
        and isinstance(node.test.comparators[0], ast.Constant)
        and node.test.comparators[0].value == "__main__"
    )


@dataclass(frozen=True)
class _EntryPointFacts:
    has_main_definition: bool
    main_guards: tuple[ast.If, ...]
    module_calls: tuple[ast.Expr, ...]
    block_statements: tuple[tuple[ast.stmt, ...], ...]


def _entry_point_facts(tree):
    functions = set()
    guards = []
    bare_calls = []
    block_statements = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            functions.add(node.name)
        if isinstance(node, ast.If) and _main_guard(node):
            guards.append(node)
            block_statements.append(tuple(node.body))
        if (
            isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Name)
        ):
            bare_calls.append((node, node.value.func.id))

    module_calls = tuple(node for node, name in bare_calls if name in functions)
    return _EntryPointFacts(
        has_main_definition="main" in functions,
        main_guards=tuple(guards),
        module_calls=module_calls,
        block_statements=tuple(block_statements),
    )


def _is_entry_point(facts):
    return bool(facts.has_main_definition or facts.main_guards or facts.module_calls)


def _bootstrap_problem(source, relpath, tree=None, facts=None):
    if tree is None:
        tree = ast.parse(source, filename=relpath)
    if facts is None:
        facts = _entry_point_facts(tree)
    if not _is_entry_point(facts):
        return None
    if facts.module_calls:
        return (
            "module-level call to a locally defined function is outside the entry block"
        )
    if len(facts.main_guards) != 1:
        return "expected one module-level __main__ block"

    body = list(facts.block_statements[0])
    imports = [
        node
        for node in body[:-1]
        if isinstance(node, ast.ImportFrom)
        and node.module == "script_io"
        and node.level == 0
        and len(node.names) == 1
        and node.names[0].name == "run_entrypoint"
        and node.names[0].asname is None
    ]
    if len(imports) != 1:
        return "expected from script_io import run_entrypoint"

    statement = body[-1]
    if not (
        isinstance(statement, ast.Expr)
        and isinstance(statement.value, ast.Call)
        and isinstance(statement.value.func, ast.Name)
        and statement.value.func.id == "run_entrypoint"
        and not statement.value.keywords
        and len(statement.value.args) in (1, 2)
        and isinstance(statement.value.args[0], ast.Name)
        and statement.value.args[0].id == "main"
    ):
        return "expected run_entrypoint(main[, sys.argv])"
    if len(statement.value.args) == 2:
        argument = statement.value.args[1]
        if not (
            isinstance(argument, ast.Attribute)
            and argument.attr == "argv"
            and isinstance(argument.value, ast.Name)
            and argument.value.id == "sys"
        ):
            return "the optional entry-point argument must be sys.argv"
    return None


def _tracked_bootstrap_modules():
    tracked = subprocess.check_output(["git", "ls-files", "-z", "--", "*.py"], cwd=REPO)
    for raw_path in tracked.split(b"\0"):
        if not raw_path:
            continue
        relpath = raw_path.decode("utf-8")
        if relpath.startswith(BOOTSTRAP_ROOTS):
            yield relpath


class TestWriteResult(unittest.TestCase):
    def test_stdout_escapes_lone_surrogates_and_stays_parseable(self):
        output = io.StringIO()
        with redirect_stdout(output):
            write_result(None, {"value": "bad\ud800"})
        self.assertIn(r"\ud800", output.getvalue())
        self.assertEqual(json.loads(output.getvalue())["value"], "bad\ud800")

    def test_file_escapes_lone_surrogates(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "result.json"
            write_result(str(path), {"value": "bad\udfff"})
            self.assertIn(r"\udfff", path.read_text(encoding="utf-8"))
            self.assertEqual(
                json.loads(path.read_text(encoding="utf-8"))["value"], "bad\udfff"
            )


class TestUtf8Stdio(unittest.TestCase):
    def test_text_streams_use_utf8_mode_errors_and_lf(self):
        stdout_buffer = io.BytesIO()
        stderr_buffer = io.BytesIO()
        stdin_buffer = io.BytesIO(b"\xe2\x80\x94\xff")
        stdout = io.TextIOWrapper(stdout_buffer, encoding="latin-1", newline="\r\n")
        stderr = io.TextIOWrapper(stderr_buffer, encoding="latin-1", newline="\r\n")
        stdin = io.TextIOWrapper(stdin_buffer, encoding="latin-1", newline="\r\n")
        with (
            patch.object(sys, "stdout", stdout),
            patch.object(sys, "stderr", stderr),
            patch.object(sys, "stdin", stdin),
        ):
            utf8_stdio()
            stdout.write("\udcff—\n")
            stderr.write("\ud800\n")
            stdout.flush()
            stderr.flush()
            self.assertEqual(stdin.read(), "—\udcff")
        self.assertEqual(stdout_buffer.getvalue(), b"\xff\xe2\x80\x94\n")
        self.assertEqual(stderr_buffer.getvalue(), b"\\ud800\n")

    def test_non_text_streams_are_left_alone(self):
        streams = (io.StringIO(), io.StringIO(), io.StringIO())
        with (
            patch.object(sys, "stdout", streams[0]),
            patch.object(sys, "stderr", streams[1]),
            patch.object(sys, "stdin", streams[2]),
        ):
            utf8_stdio()


class TestEntryPointBootstrap(unittest.TestCase):
    def test_biome_check_help_runs_as_a_script(self):
        result = subprocess.run(
            [
                sys.executable,
                str(REPO / "workflows/test/tools/biome_check.py"),
                "--help",
            ],
            cwd=REPO,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--cache-dir", result.stdout)

    def test_tracked_entry_points_use_the_canonical_bootstrap(self):
        problems = []
        discovered = {root: [] for root in BOOTSTRAP_ROOTS}
        for relpath in _tracked_bootstrap_modules():
            source = (REPO / relpath).read_text(encoding="utf-8")
            try:
                tree = ast.parse(source, filename=relpath)
                facts = _entry_point_facts(tree)
                if _is_entry_point(facts):
                    root = next(
                        root for root in BOOTSTRAP_ROOTS if relpath.startswith(root)
                    )
                    discovered[root].append(relpath)
                problem = _bootstrap_problem(source, relpath, tree, facts)
            except SyntaxError as error:
                problems.append(f"{relpath}: syntax error: {error}")
                continue
            if problem:
                problems.append(f"{relpath}: {problem}")
        self.assertEqual(problems, [])
        self.assertIn("scripts/", BOOTSTRAP_ROOTS)
        for root, paths in discovered.items():
            self.assertTrue(paths, f"no tracked entry points discovered under {root}")

    def test_each_entry_point_trigger_is_discovered_on_its_own(self):
        cases = (
            "def main():\n    pass\n",
            "if __name__ == '__main__':\n    pass\n",
            "def helper():\n    pass\nhelper()\n",
        )
        for source in cases:
            with self.subTest(source=source):
                self.assertIsNotNone(_bootstrap_problem(source, "scripts/synthetic.py"))
        self.assertIsNone(
            _bootstrap_problem("def helper():\n    pass\n", "scripts/library.py")
        )

    def test_module_level_call_is_discovered_as_an_entry_point(self):
        with tempfile.TemporaryDirectory() as temporary:
            scratch = Path(temporary) / "scripts" / "scratch.py"
            scratch.parent.mkdir()
            source = (REPO / "scripts" / "emit_style_context.py").read_text(
                encoding="utf-8"
            )
            scratch.write_text(source + "\nmain()\n", encoding="utf-8")
            source = scratch.read_text(encoding="utf-8")
            self.assertEqual(
                _bootstrap_problem(source, "scripts/scratch.py"),
                "module-level call to a locally defined function is outside the entry block",
            )

    def test_sys_exit_main_is_rejected(self):
        source = (
            "def main():\n    pass\n\n"
            "if __name__ == '__main__':\n    sys.exit(main())\n"
        )
        self.assertIsNotNone(_bootstrap_problem(source, "scripts/example.py"))

    def test_assemble_artifacts_cli_emits_utf8_with_lf(self):
        from tests.test_assemble_artifacts import _Workspace

        with _Workspace() as workspace:
            plan_path = workspace.tamper_plan(
                lambda plan: plan["postReview"]["ids"].pop()
            )
            environment = dict(os.environ, PYTHONIOENCODING="latin-1")
            result = subprocess.run(
                [
                    sys.executable,
                    str(REPO / "scripts" / "assemble_artifacts.py"),
                    "--plan",
                    plan_path,
                ],
                cwd=REPO,
                env=environment,
                capture_output=True,
            )

        self.assertNotEqual(result.returncode, 0)
        stdout = result.stdout.decode("utf-8")
        self.assertIn("—", stdout)
        self.assertTrue(result.stdout.endswith(b"\n"))
        self.assertFalse(result.stdout.endswith(b"\r\n"))


if __name__ == "__main__":
    unittest.main()

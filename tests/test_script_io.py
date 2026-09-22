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


def _is_script_path_insert(node):
    call = node.value if isinstance(node, ast.Expr) else node
    if not isinstance(call, ast.Call):
        return False
    func = call.func
    if not (
        isinstance(func, ast.Attribute)
        and func.attr == "insert"
        and isinstance(func.value, ast.Attribute)
        and func.value.attr == "path"
        and isinstance(func.value.value, ast.Name)
        and func.value.value.id == "sys"
        and len(call.args) == 2
        and isinstance(call.args[0], ast.Constant)
        and call.args[0].value == 0
        and not call.keywords
    ):
        return False
    destination = call.args[1]
    if not (
        isinstance(destination, ast.Call)
        and isinstance(destination.func, ast.Name)
        and destination.func.id == "str"
        and len(destination.args) == 1
        and isinstance(destination.args[0], ast.BinOp)
        and isinstance(destination.args[0].op, ast.Div)
        and isinstance(destination.args[0].right, ast.Constant)
        and destination.args[0].right.value == "scripts"
    ):
        return False
    anchors = tuple(ast.walk(destination.args[0].left))
    return any(
        isinstance(node, ast.Name) and node.id in {"__file__", "REPO"}
        for node in anchors
    )


def _bootstrap_problem(source, relpath):
    tree = ast.parse(source, filename=relpath)
    functions = {
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    guards = [node for node in tree.body if _main_guard(node)]
    module_calls = [
        node
        for node in tree.body
        if isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Name)
        and node.value.func.id in functions
    ]
    if "main" not in functions and not guards and not module_calls:
        return None
    if module_calls:
        return (
            "module-level call to a locally defined function is outside the entry block"
        )
    if len(guards) != 1:
        return "expected one module-level __main__ block"

    outside_scripts = not relpath.startswith("scripts/")
    body = list(guards[0].body)
    insertions = [node for node in body if _is_script_path_insert(node)]
    if outside_scripts:
        path_inserts = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and _is_script_path_insert(node)
        ]
        if len(path_inserts) != 1:
            return "expected one scripts/ path insertion before the bootstrap import"
        if insertions:
            if len(insertions) != 1 or not body or body[0] is not insertions[0]:
                return (
                    "expected one scripts/ path insertion before the bootstrap import"
                )
            body.pop(0)
        else:
            top_level_insertions = [
                node
                for node in tree.body
                if isinstance(node, ast.Expr) and node.value is path_inserts[0]
            ]
            if (
                len(top_level_insertions) != 1
                or top_level_insertions[0].lineno >= guards[0].lineno
            ):
                return (
                    "expected the scripts/ path insertion before the bootstrap import"
                )
    elif insertions:
        return "scripts/ entry points must not alter sys.path"

    if len(body) != 2:
        return "expected only the bootstrap import and run_entrypoint call"
    imported = body[0]
    if not (
        isinstance(imported, ast.ImportFrom)
        and imported.module == "script_io"
        and imported.level == 0
        and len(imported.names) == 1
        and imported.names[0].name == "run_entrypoint"
        and imported.names[0].asname is None
    ):
        return "expected from script_io import run_entrypoint"

    statement = body[1]
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
    def test_stdout_is_utf8_with_lf_and_only_text_wrappers_are_reconfigured(self):
        buffer = io.BytesIO()
        stream = io.TextIOWrapper(buffer, encoding="latin-1", newline="\r\n")
        with patch.object(sys, "stdout", stream):
            utf8_stdio()
            print("—")
            stream.flush()
        self.assertEqual(buffer.getvalue(), b"\xe2\x80\x94\n")

    def test_non_text_streams_are_left_alone(self):
        streams = (io.StringIO(), io.StringIO(), io.StringIO())
        with (
            patch.object(sys, "stdout", streams[0]),
            patch.object(sys, "stderr", streams[1]),
            patch.object(sys, "stdin", streams[2]),
        ):
            utf8_stdio()


class TestEntryPointBootstrap(unittest.TestCase):
    def test_tracked_entry_points_use_the_canonical_bootstrap(self):
        problems = []
        for relpath in _tracked_bootstrap_modules():
            source = (REPO / relpath).read_text(encoding="utf-8")
            try:
                problem = _bootstrap_problem(source, relpath)
            except SyntaxError as error:
                problems.append(f"{relpath}: syntax error: {error}")
                continue
            if problem:
                problems.append(f"{relpath}: {problem}")
        self.assertEqual(problems, [])

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

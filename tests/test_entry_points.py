"""Entry files and non-package tool bootstraps."""

import ast
import importlib
import os
import subprocess
import sys
from pathlib import Path

import pytest
from gauntlet import COMMAND_MODULES
from gauntlet.cli import Command

ROOT = Path(__file__).resolve().parents[1]
TOOL_ROOTS = (".github/", "workflows/test/tools/")


def _entry_template(name, module):
    return (
        "#!/usr/bin/env python3\n"
        f'"""{name.replace("_", " ").capitalize()} command entry."""\n\n'
        "import os\n"
        "import sys\n\n"
        "sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))\n\n"
        f"from gauntlet.{module} import CLI\n\n"
        "CLI.run()\n"
    )


def _entry_problem(source, name, expected=None):
    tree = ast.parse(source)
    imports = [
        node
        for node in tree.body
        if isinstance(node, ast.ImportFrom)
        and node.module
        and node.module.startswith("gauntlet.")
        and [(alias.name, alias.asname) for alias in node.names] == [("CLI", None)]
    ]
    if len(imports) != 1:
        return "entry must import one command"
    module = imports[0].module.removeprefix("gauntlet.")
    if expected is not None and module != expected:
        return f"entry imports {module}, expected {expected}"
    if source != _entry_template(name, module):
        return "entry differs from template"
    return None


def _imported_module(source):
    tree = ast.parse(source)
    return next(
        node.module.removeprefix("gauntlet.")
        for node in tree.body
        if isinstance(node, ast.ImportFrom)
        and node.module
        and node.module.startswith("gauntlet.")
    )


def test_all_script_entries_use_one_template():
    paths = sorted((ROOT / "scripts").glob("*.py"))
    assert len(paths) == 19
    imported = []
    for path in paths:
        source = path.read_text(encoding="utf-8")
        assert _entry_problem(source, path.stem, COMMAND_MODULES[path.stem]) is None
        module = _imported_module(source)
        cli = importlib.import_module(f"gauntlet.{module}").CLI
        assert isinstance(cli, Command)
        imported.append(module)
    assert len(set(imported)) == len(imported)


@pytest.mark.parametrize(
    "source",
    [
        "def main():\n    pass\n",
        "if __name__ == '__main__':\n    pass\n",
        "def helper():\n    pass\nhelper()\n",
        "def main():\n    pass\nif __name__ == '__main__':\n    sys.exit(main())\n",
    ],
)
def test_non_template_entry_is_rejected(source):
    assert _entry_problem(source, "scratch") is not None


def test_sibling_command_import_is_rejected():
    source = _entry_template("diff_numstat", "stale")
    assert _entry_problem(source, "diff_numstat", "numstat") is not None


def test_extra_module_call_is_rejected():
    source = _entry_template("emit_style_context", "style_hook")
    assert _entry_problem(source + "\nmain()\n", "emit_style_context") == (
        "entry differs from template"
    )


def _is_main_guard(node):
    return (
        isinstance(node, ast.If)
        and isinstance(node.test, ast.Compare)
        and ast.unparse(node.test) == "__name__ == '__main__'"
    )


def _tool_problem(source, relative):
    tree = ast.parse(source)
    guards = [node for node in tree.body if _is_main_guard(node)]
    if len(guards) != 1 or len(guards[0].body) != 3:
        return "tool must have one three-statement main guard"
    insert, imported, runner = guards[0].body
    if not isinstance(insert, ast.Expr) or not isinstance(insert.value, ast.Call):
        return "tool must insert scripts before import"
    if (
        ast.unparse(insert.value.func) != "sys.path.insert"
        or len(insert.value.args) != 2
    ):
        return "tool must insert scripts before import"
    if (
        not isinstance(insert.value.args[0], ast.Constant)
        or insert.value.args[0].value != 0
    ):
        return "tool must insert scripts at the front"
    if '"scripts"' not in ast.get_source_segment(source, insert.value.args[1]):
        return "tool must insert scripts"
    if not isinstance(imported, ast.ImportFrom) or imported.module != "gauntlet.cli":
        return "tool must import Command after insert"
    if [(alias.name, alias.asname) for alias in imported.names] != [("Command", None)]:
        return "tool must import Command"
    if not isinstance(runner, ast.Expr) or not isinstance(runner.value, ast.Call):
        return "tool must run Command"
    call = runner.value
    direct = f"Command.legacy(main, prog='{Path(relative).stem}.py').run"
    parity = "Command.legacy(lambda: main(sys.argv), prog='record_parity.py').run"
    if ast.unparse(call.func) != direct and (
        Path(relative).stem != "record_parity" or ast.unparse(call.func) != parity
    ):
        return "tool must run its own main through Command"
    if call.args or call.keywords:
        return "tool runner takes no arguments"
    return None


def test_all_non_package_tools_have_ordered_bootstraps():
    tracked = (
        subprocess.run(
            ["git", "ls-files", "-z", "--", ".github", "workflows/test/tools"],
            cwd=ROOT,
            capture_output=True,
            check=True,
        )
        .stdout.decode()
        .split("\0")
    )
    counts = {root: 0 for root in TOOL_ROOTS}
    for relative in tracked:
        if not relative.endswith(".py"):
            continue
        source = (ROOT / relative).read_text(encoding="utf-8")
        tree = ast.parse(source)
        if not any(_is_main_guard(node) for node in tree.body):
            continue
        root = next(root for root in TOOL_ROOTS if relative.startswith(root))
        counts[root] += 1
        assert _tool_problem(source, relative) is None, relative
    assert all(counts.values()), counts


def test_biome_check_help_runs_from_foreign_cwd(tmp_path):
    environment = dict(os.environ)
    environment.pop("PYTHONPATH", None)
    environment["PYTHONSAFEPATH"] = "1"
    result = subprocess.run(
        [sys.executable, str(ROOT / "workflows/test/tools/biome_check.py"), "--help"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=environment,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "--cache-dir" in result.stdout


def test_assemble_artifacts_cli_emits_utf8_with_lf():
    from tests.test_assemble_artifacts import _Workspace

    with _Workspace() as workspace:
        plan_path = workspace.tamper_plan(lambda plan: plan["postReview"]["ids"].pop())
        environment = dict(os.environ, PYTHONIOENCODING="latin-1")
        result = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts/assemble_artifacts.py"),
                "--plan",
                plan_path,
            ],
            cwd=ROOT,
            env=environment,
            capture_output=True,
            check=False,
        )
    assert result.returncode != 0
    assert "—" in result.stdout.decode("utf-8")
    assert result.stdout.endswith(b"\n")
    assert not result.stdout.endswith(b"\r\n")

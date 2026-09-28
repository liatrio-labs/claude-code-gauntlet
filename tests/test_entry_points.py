"""Entry files and non-package tool bootstraps."""

import ast
import importlib
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
from gauntlet.cli import Command

ROOT = Path(__file__).resolve().parents[1]
ENTRIES = sorted((ROOT / "scripts").glob("*.py"))
IMPORT = re.compile(r"^from gauntlet\.([\w.]+) import CLI$", re.MULTILINE)


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


@pytest.mark.parametrize("path", ENTRIES, ids=lambda path: path.stem)
def test_entry_is_the_template_over_a_command(path):
    source = path.read_text(encoding="utf-8")
    module = IMPORT.search(source)[1]
    assert source == _entry_template(path.stem, module)
    assert isinstance(importlib.import_module(f"gauntlet.{module}").CLI, Command)


def test_no_two_entries_share_a_module():
    modules = [IMPORT.search(path.read_text(encoding="utf-8"))[1] for path in ENTRIES]
    assert len(modules) == len(set(modules))


def _bootstrap(relative):
    tree = ast.parse((ROOT / relative).read_text(encoding="utf-8"))
    return [
        node
        for node in tree.body
        if isinstance(node, ast.If)
        and ast.dump(node.test)
        == ast.dump(ast.parse("__name__ == '__main__'").body[0].value)
    ]


def _tool_candidate(relative):
    tree = ast.parse((ROOT / relative).read_text(encoding="utf-8"))
    locals_ = {node.name for node in tree.body if isinstance(node, ast.FunctionDef)}
    return any(
        isinstance(node, ast.If)
        or (isinstance(node, ast.FunctionDef) and node.name == "main")
        or (
            isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Name)
            and node.value.func.id in locals_
        )
        for node in tree.body
    )


TOOLS = [
    relative
    for relative in subprocess.run(
        ["git", "ls-files", "-z", "--", ".github", "workflows/test/tools"],
        cwd=ROOT,
        capture_output=True,
        check=True,
    )
    .stdout.decode()
    .split("\0")
    if relative.endswith(".py") and _tool_candidate(relative)
]


def test_both_tool_roots_have_bootstrapped_tools():
    assert {relative.split("/")[0] for relative in TOOLS} == {".github", "workflows"}


@pytest.mark.parametrize("relative", TOOLS)
def test_tool_bootstrap_inserts_scripts_then_runs_its_main(relative):
    guards = _bootstrap(relative)
    assert len(guards) == 1
    prog = Path(relative).stem
    parent = 1 if relative.startswith(".github/") else 3
    expected = ast.parse(
        f'if __name__ == "__main__":\n'
        f'    sys.path.insert(0, str(Path(__file__).resolve().parents[{parent}] / "scripts"))\n'
        "    from gauntlet.cli import Command\n"
        f'    Command.legacy(main, prog="{prog}.py").run()\n'
    ).body[0]
    assert ast.dump(guards[0]) == ast.dump(expected)


@pytest.mark.parametrize("relative", TOOLS)
def test_tool_runs_from_foreign_cwd(relative, tmp_path):
    environment = dict(os.environ)
    environment.pop("PYTHONPATH", None)
    environment["PYTHONSAFEPATH"] = "1"
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / relative),
            "--check" if "record_parity" in relative else "--help",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=environment,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout

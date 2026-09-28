"""Entry files and non-package tool bootstraps."""

import importlib
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
from gauntlet.cli import Command

ROOT = Path(__file__).resolve().parents[1]
TOOLS = (
    ".github/labels_diff.py",
    "workflows/test/tools/biome_check.py",
    "workflows/test/tools/record_parity.py",
)


def _entry_template(name, module):
    return (
        "#!/usr/bin/env python3\n"
        f'"""{name.replace("_", " ").capitalize()} command entry."""\n'
        "import os\n"
        "import sys\n\n"
        "sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))\n\n"
        f"from gauntlet.{module} import CLI  # noqa: E402\n\n"
        "CLI.run()\n"
    )


def _entry_problem(source, name):
    imports = re.findall(r"^from gauntlet\.([a-z_]+) import CLI", source, re.MULTILINE)
    if len(imports) != 1:
        return "entry must import one command"
    if source != _entry_template(name, imports[0]):
        return "entry differs from template"
    return None


def test_all_script_entries_use_one_template():
    names = {path.stem for path in (ROOT / "scripts").glob("*.py")}
    assert len(names) == 19
    modules = {}
    for name in sorted(names):
        path = ROOT / "scripts" / f"{name}.py"
        module = name
        assert _entry_problem(path.read_text(encoding="utf-8"), name) is None
        cli = importlib.import_module(f"gauntlet.{module}").CLI
        assert isinstance(cli, Command)
        modules[name] = module
    assert len(set(modules.values())) == len(names)


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


def test_extra_module_call_is_rejected():
    source = _entry_template("emit_style_context", "emit_style_context")
    assert _entry_problem(source + "\nmain()\n", "emit_style_context") == (
        "entry differs from template"
    )


@pytest.mark.parametrize("relative", TOOLS)
def test_tool_bootstraps_insert_scripts_before_import(relative):
    source = (ROOT / relative).read_text(encoding="utf-8")
    assert ' / "scripts"' in source
    assert "sys.path.insert(0," in source
    assert "from gauntlet.cli import run_entrypoint" in source


def test_biome_check_help_runs_from_foreign_cwd(tmp_path):
    environment = dict(os.environ)
    environment.pop("PYTHONPATH", None)
    environment["PYTHONSAFEPATH"] = "1"
    result = subprocess.run(
        [sys.executable, str(ROOT / TOOLS[1]), "--help"],
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

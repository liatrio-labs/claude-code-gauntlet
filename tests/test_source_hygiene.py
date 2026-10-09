"""Track legacy comments while each module is migrated."""

import ast
import io
import re
import sys
import tokenize
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ISSUE = re.compile(r"(?:\bissue\s*#?\d+|#\d+)", re.IGNORECASE)

# Exact violation matching prevents completed cleanup from staying exempt.
MIGRATING_PYTHON = {
    "numstat.py",
    "stale.py",
    "shared_context.py",
}
MIGRATING_JS = {
    "args.js",
    "pipeline_entry.js",
    "registry.js",
    "stages.js",
}


def test_shipped_scripts_import_only_stdlib_or_gauntlet():
    for path in (ROOT / "scripts").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules = [alias.name.split(".", 1)[0] for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and not node.level and node.module:
                modules = [node.module.split(".", 1)[0]]
            else:
                continue
            for module in modules:
                assert module in sys.stdlib_module_names or module == "gauntlet", (
                    f"{path}:{node.lineno}: {module}"
                )


def _python_violations(source):
    tree = ast.parse(source)
    issues = []
    first = ast.get_docstring(tree)
    if first and len(first.splitlines()) > 3:
        issues.append("module docstring")
    docstrings = {
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
    }
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        commentary = token.type == tokenize.COMMENT or (
            token.type == tokenize.STRING and token.start[0] in docstrings
        )
        if commentary and ISSUE.search(token.string):
            issues.append("issue reference")
    return issues


def _js_violations(source):
    return [
        "issue reference"
        for line in source.splitlines()
        if line.lstrip().startswith("//") and ISSUE.search(line)
    ]


def test_migration_lists_cover_only_current_violations():
    python = {
        path.relative_to(ROOT / "scripts/gauntlet").as_posix()
        for path in (ROOT / "scripts/gauntlet").rglob("*.py")
        if _python_violations(path.read_text(encoding="utf-8"))
    }
    violating_js = {
        path.name
        for path in (ROOT / "workflows/src").glob("*.js")
        if _js_violations(path.read_text(encoding="utf-8"))
    }
    assert python == MIGRATING_PYTHON
    assert violating_js == MIGRATING_JS

"""Enforce import and comment hygiene over shipped sources."""

import ast
import io
import re
import sys
import tokenize
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ISSUE = re.compile(r"(?:\bissue\s*#?\d+|#\d+)", re.IGNORECASE)


def test_shipped_scripts_import_only_stdlib_or_gauntlet() -> None:
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


def _python_violations(source: str) -> list[tuple[int, str]]:
    tree = ast.parse(source)
    issues: list[tuple[int, str]] = []
    first = ast.get_docstring(tree)
    if first and len(first.splitlines()) > 3:
        issues.append((tree.body[0].lineno, "module docstring"))
    docstrings = {
        node.body[0].lineno
        for node in ast.walk(tree)
        if isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        )
        and ast.get_docstring(node) is not None
    }
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        commentary = token.type == tokenize.COMMENT or (
            token.type == tokenize.STRING and token.start[0] in docstrings
        )
        if commentary and ISSUE.search(token.string):
            issues.append((token.start[0], "issue reference"))
    return issues


def _js_violations(source: str) -> list[tuple[int, str]]:
    return [
        (number, "issue reference")
        for number, line in enumerate(source.splitlines(), 1)
        if line.lstrip().startswith("//") and ISSUE.search(line)
    ]


def test_shipped_sources_have_no_hygiene_violations() -> None:
    violations = [
        f"{path.relative_to(ROOT)}:{line}: {reason}"
        for path in (ROOT / "scripts").rglob("*.py")
        for line, reason in _python_violations(path.read_text(encoding="utf-8"))
    ]
    for path in [*(ROOT / "workflows/src").glob("*.js"), ROOT / "workflows/build.js"]:
        violations.extend(
            f"{path.relative_to(ROOT)}:{line}: {reason}"
            for line, reason in _js_violations(path.read_text(encoding="utf-8"))
        )
    assert not violations, "\n".join(violations)


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ('"""Short module."""\nvalue = "issue #1"\n"#2"\n', []),
        (
            '"""Module\nissue #1\nthird\nfourth"""\n# issue #2\n'
            'def f():\n    """issue #3"""\n',
            [
                (1, "module docstring"),
                (1, "issue reference"),
                (5, "issue reference"),
                (7, "issue reference"),
            ],
        ),
    ],
    ids=["accept", "reject"],
)
def test_python_hygiene(source: str, expected: list[tuple[int, str]]) -> None:
    assert _python_violations(source) == expected


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ('const value = "// issue #1";\n// Keep the refusal visible.\n', []),
        ("const value = 1;\n  // issue #1\n", [(2, "issue reference")]),
    ],
    ids=["accept", "reject"],
)
def test_js_hygiene(source: str, expected: list[tuple[int, str]]) -> None:
    assert _js_violations(source) == expected

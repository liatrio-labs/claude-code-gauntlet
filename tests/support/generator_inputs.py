"""Discover generator inputs for contribution and temporary-tree proofs."""

import os
import posixpath
import re

from gauntlet.fs import read_text

_NODE_PROGRAM_ROOTS = (
    "workflows/src/registry.js",
    "workflows/src/args.js",
    "workflows/src/renderReport.js",
    "workflows/src/applyValidations.js",
    "workflows/src/filterFindings.js",
    "workflows/src/stages.js",
    "workflows/src/verifyWire.js",
)
_WORKFLOW_RELATIVE_IMPORT_RE = re.compile(
    r"^\s*(?:import|export)\b.*?\bfrom\s+['\"](\.[^'\"]+)['\"]",
    re.MULTILINE,
)
_PYTHON_FROM_IMPORT_RE = re.compile(
    r"^\s*from\s+(?P<module>[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*)\s+import\s+"
    r"(?P<names>[^\n#]+)$",
    re.MULTILINE,
)
_PYTHON_IMPORT_RE = re.compile(
    r"^\s*import\s+(?P<modules>[^\n#]+)$",
    re.MULTILINE,
)
_DYNAMIC_SCRIPT_PATH_RE = re.compile(
    r"os\.path\.join\(\s*repo_root\s*,\s*['\"]scripts['\"]\s*,\s*"
    r"(?P<package>['\"]gauntlet['\"]\s*,\s*)?"
    r"['\"](?P<path>[^'\"]+\.py)['\"]\s*\)",
)


def _script_module_path(module: str, repo_root: str) -> str | None:
    if module == "gauntlet":
        return None
    if module.startswith("gauntlet."):
        module = module[len("gauntlet.") :]
    elif "." in module:
        return None
    rel_path = posixpath.join("scripts", "gauntlet", *module.split(".")) + ".py"
    if os.path.isfile(os.path.join(repo_root, rel_path)):
        return rel_path
    return None


def _python_imported_script_paths(source: str, repo_root: str) -> set[str]:
    imported: set[str] = set()
    for match in _PYTHON_FROM_IMPORT_RE.finditer(source):
        module = match.group("module")
        names = [part.strip().split()[0] for part in match.group("names").split(",")]
        path = _script_module_path(module, repo_root)
        if path:
            imported.add(path)
        for name in names:
            path = _script_module_path(f"{module}.{name}", repo_root)
            if path:
                imported.add(path)
    for match in _PYTHON_IMPORT_RE.finditer(source):
        for part in match.group("modules").split(","):
            module = part.strip().split()[0]
            path = _script_module_path(module, repo_root)
            if path:
                imported.add(path)
    for match in _DYNAMIC_SCRIPT_PATH_RE.finditer(source):
        prefix = "scripts/gauntlet" if match.group("package") else "scripts"
        rel_path = posixpath.normpath(posixpath.join(prefix, match.group("path")))
        if rel_path.startswith("scripts/") and os.path.isfile(
            os.path.join(repo_root, rel_path)
        ):
            imported.add(rel_path)
    return imported


def _python_import_closure(repo_root: str) -> set[str]:
    pending = ["scripts/gauntlet/contract_gen.py"]
    seen = set()
    while pending:
        rel_path = pending.pop()
        if rel_path in seen:
            continue
        seen.add(rel_path)
        path = os.path.join(repo_root, rel_path)
        source = read_text(path)
        for imported in _python_imported_script_paths(source, repo_root):
            if imported not in seen:
                pending.append(imported)
    return seen


def _workflow_import_closure(repo_root: str) -> set[str]:
    pending = list(_NODE_PROGRAM_ROOTS)
    seen = set()
    while pending:
        rel_path = pending.pop()
        if rel_path in seen:
            continue
        seen.add(rel_path)
        path = os.path.join(repo_root, rel_path)
        source = read_text(path)
        base = posixpath.dirname(rel_path)
        for specifier in _WORKFLOW_RELATIVE_IMPORT_RE.findall(source):
            imported = posixpath.normpath(posixpath.join(base, specifier))
            if imported not in seen:
                pending.append(imported)
    return seen


def declared_inputs(repo_root: str) -> set[str]:
    return _python_import_closure(repo_root) | _workflow_import_closure(repo_root)

"""Track legacy comments while each module is migrated."""

import ast
import io
import re
import tokenize
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ISSUE = re.compile(r"(?:\bissue\s*#?\d+|#\d+)", re.IGNORECASE)

# These bodies moved unchanged; each mechanism slice removes its own entry.
MIGRATING_PYTHON = {
    "assemble_artifacts.py",
    "await_workflow.py",
    "build_style_artifacts.py",
    "collect_project_rules.py",
    "detect_prior_review.py",
    "diff.py",
    "diff_numstat.py",
    "emit_style_context.py",
    "ensure_output_dir.py",
    "generate_contract_requirements.py",
    "marker.py",
    "materialize_artifacts.py",
    "post_review.py",
    "render_fix_tasks.py",
    "report_patches.py",
    "resolve_config.py",
    "resolve_pr_identity.py",
    "stale_truncate.py",
    "sync_agent_rules.py",
    "text.py",
    "verify_findings.py",
    "write_shared_context.py",
}
MIGRATING_JS = {
    "args.js",
    "pipeline_entry.js",
    "registry.js",
    "stages.js",
}


def _python_violations(source):
    tree = ast.parse(source)
    issues = []
    first = ast.get_docstring(tree)
    if first and len(first.splitlines()) > 3:
        issues.append("module docstring")
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type == tokenize.COMMENT and ISSUE.search(token.string):
            issues.append("issue reference")
        if (
            token.type == tokenize.STRING
            and ISSUE.search(token.string)
            and any(
                isinstance(node, ast.Expr)
                and isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, str)
                and node.lineno == token.start[0]
                for node in ast.walk(tree)
            )
        ):
            issues.append("issue reference")
    return issues


def _js_violations(source):
    return [
        "issue reference"
        for line in source.splitlines()
        if line.lstrip().startswith("//") and ISSUE.search(line)
    ]


def test_migration_lists_cover_only_existing_sources():
    assert {
        path.name for path in (ROOT / "scripts/gauntlet").glob("*.py")
    } >= MIGRATING_PYTHON
    assert {path.name for path in (ROOT / "workflows/src").glob("*.js")} >= MIGRATING_JS


def test_no_new_source_hygiene_violations():
    problems = {}
    for path in (ROOT / "scripts/gauntlet").glob("*.py"):
        found = _python_violations(path.read_text(encoding="utf-8"))
        if found and path.name not in MIGRATING_PYTHON:
            problems[str(path.relative_to(ROOT))] = found
    for path in (ROOT / "workflows/src").glob("*.js"):
        found = _js_violations(path.read_text(encoding="utf-8"))
        if found and path.name not in MIGRATING_JS:
            problems[str(path.relative_to(ROOT))] = found
    assert not problems

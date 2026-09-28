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
    "artifacts.py",
    "awaiting.py",
    "style.py",
    "project_rules.py",
    "prior_review.py",
    "diff.py",
    "numstat.py",
    "style_hook.py",
    "output_dir.py",
    "contract_gen.py",
    "marker.py",
    "materialize.py",
    "delivery/post.py",
    "fix_tasks.py",
    "patches.py",
    "config.py",
    "pr_identity.py",
    "stale.py",
    "agent_rules.py",
    "text.py",
    "verify/decide.py",
    "shared_context.py",
}
MIGRATING_JS = {
    "args.js",
    "pipeline_entry.js",
    "registry.js",
    "stages.js",
}
ALL_JS = {
    "applyChallenges.js",
    "applyValidations.js",
    "args.js",
    "filterFindings.js",
    "findingDedup.js",
    "mergeFindings.js",
    "pipeline_entry.js",
    "registry.js",
    "renderReport.js",
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


def test_migration_lists_cover_only_current_violations():
    python = {
        str(path.relative_to(ROOT / "scripts/gauntlet"))
        for path in (ROOT / "scripts/gauntlet").rglob("*.py")
        if _python_violations(path.read_text(encoding="utf-8"))
    }
    js = {path.name for path in (ROOT / "workflows/src").glob("*.js")}
    violating_js = {
        path.name
        for path in (ROOT / "workflows/src").glob("*.js")
        if _js_violations(path.read_text(encoding="utf-8"))
    }
    assert js == ALL_JS
    assert python == MIGRATING_PYTHON
    assert violating_js == MIGRATING_JS


def test_no_new_source_hygiene_violations():
    problems = {}
    for path in (ROOT / "scripts/gauntlet").rglob("*.py"):
        found = _python_violations(path.read_text(encoding="utf-8"))
        if (
            found
            and str(path.relative_to(ROOT / "scripts/gauntlet")) not in MIGRATING_PYTHON
        ):
            problems[str(path.relative_to(ROOT))] = found
    for path in (ROOT / "workflows/src").glob("*.js"):
        found = _js_violations(path.read_text(encoding="utf-8"))
        if found and path.name not in MIGRATING_JS:
            problems[str(path.relative_to(ROOT))] = found
    assert not problems

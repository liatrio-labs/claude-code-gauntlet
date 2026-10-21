"""The stack can be extracted in order without reverse Python dependencies."""

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LAYERS = [
    "__init__ contracts decode epochs costs citations mirrors scorer_source cli",
    "scorer score observations provenance stages ledger anchors scoring_costs profiles",
    "validity invoke check checkpoint run",
    "leaks",
    "populations looks audit compare simulation report",
]


def test_imports_follow_stack_layers_and_trace_is_independent():
    rank = {name: i for i, names in enumerate(LAYERS) for name in names.split()}
    paths = [
        *sorted((ROOT / "bench/runner").glob("*.py")),
        ROOT / "bench/run.py",
        ROOT / "bench/report.py",
    ]
    for path in paths:
        level = rank[path.stem]
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            modules = []
            if isinstance(node, ast.Import):
                modules = [n.name for n in node.names]
            if isinstance(node, ast.ImportFrom):
                modules = [node.module or ""]
                if node.module in ("bench", "bench.runner"):
                    modules = [node.module + "." + n.name for n in node.names]
            for module in modules:
                if module.startswith("bench.runner.") or module in (
                    "bench.run",
                    "bench.report",
                ):
                    target = module.removeprefix("bench.runner.").removeprefix("bench.")
                    assert rank[target] <= level, (path, node.lineno, module)
    # The CLI composition seam loads precisely one registered decisions hook.
    source = (ROOT / "bench/run.py").read_text(encoding="utf-8")
    assert (
        source.count("import_module(") == 1
        and ".register_run_hook(globals())" in source
    )
    for directory in ("workflows/src", "workflows/test", "scripts/gauntlet", "skills"):
        for path in (ROOT / directory).rglob("*"):
            if path.suffix in (".py", ".js", ".mjs", ".md"):
                assert "from bench" not in path.read_text(
                    encoding="utf-8"
                ) and "import bench" not in path.read_text(encoding="utf-8"), path
    assert "from bench" not in (ROOT / "workflows/pipeline.js").read_text(
        encoding="utf-8"
    )

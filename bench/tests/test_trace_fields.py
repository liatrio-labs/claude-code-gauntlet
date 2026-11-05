"""Keep the shared producer projection aligned with its benchmark consumers."""

import ast
from pathlib import Path

from gauntlet.registry import TRACE_FINDING_FIELDS

ROOT = Path(__file__).resolve().parents[2]


def _finding_reads(tree):
    for node in ast.walk(tree):
        receiver = key = None
        if isinstance(node, ast.Subscript) and isinstance(node.ctx, ast.Load):
            receiver, key = node.value, node.slice
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "get"
            and node.args
        ):
            receiver, key = node.func.value, node.args[0]
        if not isinstance(key, ast.Constant) or not isinstance(key.value, str):
            continue
        if (
            isinstance(receiver, ast.Name)
            and receiver.id in {"f", "finding", "primary", "member"}
        ) or (isinstance(receiver, ast.Attribute) and receiver.attr == "finding"):
            yield key.value


def test_every_bench_trace_reader_uses_kept_finding_fields():
    readers = {}
    for path in (ROOT / "bench/runner").glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        if path.stem == "stages" or any(
            isinstance(node, ast.ImportFrom)
            and (
                node.module == "bench.runner.stages"
                or (
                    node.module == "bench.runner"
                    and any(alias.name == "stages" for alias in node.names)
                )
            )
            for node in ast.walk(tree)
        ):
            readers[path.name] = set(_finding_reads(tree))
    # Rendering and provenance delegate finding reads to the production delivery code.
    for path in (ROOT / "scripts/gauntlet/delivery").glob("*.py"):
        readers[f"delivery/{path.name}"] = set(
            _finding_reads(ast.parse(path.read_text(encoding="utf-8")))
        )
    # This marker is read from the full replay checkpoint, before joining trace identity.
    readers["delivery_replay.py"].discard("challenge")
    assert readers["description_eval.py"] >= {
        "evidence",
        "file",
        "line_start",
        "line_end",
    }
    assert {
        reader: sorted(fields - set(TRACE_FINDING_FIELDS))
        for reader, fields in readers.items()
        if fields - set(TRACE_FINDING_FIELDS)
    } == {}

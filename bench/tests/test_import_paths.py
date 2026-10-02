"""Check bench imports work without pytest's inherited import paths."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize(
    "arguments, foreign_cwd",
    [
        pytest.param(
            ("-c", "import bench.adjudicator.adjudicate"), False, id="adjudicate"
        ),
        pytest.param(("-c", "import bench.runner.score"), False, id="score"),
        pytest.param(("-c", "import bench.runner.check"), False, id="check"),
        pytest.param(("-c", "import bench.runner.invoke"), False, id="invoke"),
        pytest.param(("-c", "import bench.runner.anchors"), False, id="anchors"),
        pytest.param(
            ("-m", "bench.runner.citations", "--help"),
            False,
            id="citations-module-help",
        ),
        pytest.param(
            (str(REPO_ROOT / "bench" / "run.py"), "--help"), True, id="run-file-help"
        ),
    ],
)
def test_bench_imports_without_inherited_python_paths(arguments, foreign_cwd, tmp_path):
    environment = dict(os.environ)
    environment.pop("PYTHONPATH", None)
    environment.pop("PYTHONSAFEPATH", None)
    if foreign_cwd:
        environment["PYTHONSAFEPATH"] = "1"

    result = subprocess.run(
        [sys.executable, *arguments],
        cwd=tmp_path if foreign_cwd else REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=environment,
        check=False,
    )
    assert result.returncode == 0, result.stderr

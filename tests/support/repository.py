import json
import subprocess
from collections.abc import Sequence
from functools import cache
from pathlib import Path


def tracked_paths(root: Path, pathspecs: Sequence[str] = ()) -> list[str]:
    result = subprocess.run(
        ["git", "ls-files", "-z", "--", *pathspecs],
        cwd=root,
        capture_output=True,
        check=True,
    )
    return [path for path in result.stdout.decode("utf-8").split("\0") if path]


@cache
def workflow_header(root: Path) -> dict:
    # Only the hoisted declaration is a module; the workflow body has a top-level return.
    script = (
        f"import {{ build }} from {json.dumps((root / 'workflows/build.js').as_uri())};"
        "const header = build().split('\\n')[0];"
        "if (!/^export const meta = \\{.*\\};$/.test(header)) "
        "throw new Error('workflow must begin with the hoisted meta declaration');"
        "const { meta } = await import('data:text/javascript,' + encodeURIComponent(header));"
        "console.log(JSON.stringify(meta));"
    )
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        cwd=root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    meta: dict = json.loads(result.stdout)
    return meta

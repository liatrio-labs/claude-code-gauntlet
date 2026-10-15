"""Tracked source roots needed by temporary generator trees."""

import subprocess


def declared_inputs(repo_root: str) -> set[str]:
    result = subprocess.run(
        ["git", "ls-files", "-z", "--", "scripts/", "workflows/src/"],
        cwd=repo_root,
        capture_output=True,
        check=True,
    )
    return {path for path in result.stdout.decode("utf-8").split("\0") if path}

"""Materialize the scorer named by its git tree, independently of the checkout."""

from __future__ import annotations

import io
import re
import subprocess
import tarfile
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory

from bench.runner.epochs import confined

REPO_ROOT = Path(__file__).resolve().parents[2]


@contextmanager
def materialize_scorer(tree, *, repo=REPO_ROOT):
    if not isinstance(tree, str) or not re.fullmatch(r"[0-9a-f]{40}", tree):
        raise ValueError("scorer requires a full git tree id")
    kind = subprocess.run(
        ["git", "cat-file", "-t", tree], cwd=repo, capture_output=True
    )
    if kind.returncode:
        raise ValueError(
            f"scorer tree {tree} is missing; git fetch origin (git fetch --unshallow origin for a shallow clone)"
        )
    if kind.stdout.strip() != b"tree":
        raise ValueError(f"scorer tree {tree} must name a git tree object")
    result = subprocess.run(
        ["git", "archive", "--format=tar", tree],
        cwd=repo,
        capture_output=True,
        check=True,
    )
    with TemporaryDirectory(prefix="gauntlet-scorer-") as temporary:
        root = Path(temporary)
        with tarfile.open(fileobj=io.BytesIO(result.stdout), mode="r:") as archive:
            for member in archive:
                path = confined(root, member.name)
                if member.isdir():
                    continue
                if not member.isfile():
                    raise ValueError("scorer archive must contain ordinary files")
                path.parent.mkdir(parents=True, exist_ok=True)
                with archive.extractfile(member) as incoming:
                    path.write_bytes(incoming.read())
        yield root


@contextmanager
def scorer_source(epoch):
    with materialize_scorer(epoch.scorer_tree) as root:
        yield root

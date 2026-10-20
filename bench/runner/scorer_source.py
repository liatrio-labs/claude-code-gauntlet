"""Resolve scorer bytes without keeping executable copies in golden epochs."""

from __future__ import annotations

import hashlib
import io
import subprocess
import tarfile
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory

from bench.runner.epochs import (
    confined,
    file_hash,
    read_json,
    scorer_hash,
    validate_source,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
VENDOR_PATH = "bench/vendor/code-review-benchmark"


def verify_source(root, hashes):
    for name, expected in hashes.items():
        path = confined(root, name)
        if not path.is_file() or file_hash(path) != expected:
            raise ValueError(f"scorer file hash mismatch: {name}")


@contextmanager
def materialize_scorer(commit, hashes, *, repo=REPO_ROOT):
    """Read only git objects, reject unexpected entries, and clean up on failure."""
    validate_source(commit, hashes)
    result = subprocess.run(
        ["git", "archive", "--format=tar", f"{commit}:{VENDOR_PATH}"],
        cwd=repo,
        capture_output=True,
        check=True,
    )
    with TemporaryDirectory(prefix="gauntlet-scorer-") as temporary:
        root = Path(temporary)
        seen = set()
        with tarfile.open(fileobj=io.BytesIO(result.stdout), mode="r:") as archive:
            for member in archive:
                if member.isdir():
                    continue
                name = member.name
                if not member.isfile() or name not in hashes or name in seen:
                    raise ValueError("scorer archive file inventory mismatch")
                stream = archive.extractfile(member)
                if stream is None:
                    raise ValueError("scorer archive file missing")
                with stream:
                    content = stream.read()
                if hashlib.sha256(content).hexdigest() != hashes[name]:
                    raise ValueError(f"scorer file hash mismatch: {name}")
                path = confined(root, name)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content)
                seen.add(name)
        if seen != set(hashes):
            raise ValueError("scorer archive file inventory mismatch")
        verify_source(root, hashes)
        yield root


@contextmanager
def scorer_source(epoch):
    hashes = dict(epoch.scorer_file_hashes)
    validate_source(epoch.scorer_commit, hashes)
    index = read_json(REPO_ROOT / "bench/golden/epochs/index.json")
    if epoch.epoch_id == index["active_epoch_id"]:
        root = REPO_ROOT / VENDOR_PATH
        verify_source(root, hashes)
        if scorer_hash(root) != epoch.scorer_sha256:
            raise ValueError("scorer content hash mismatch")
        yield root
    else:
        with materialize_scorer(epoch.scorer_commit, hashes) as root:
            if scorer_hash(root) != epoch.scorer_sha256:
                raise ValueError("scorer content hash mismatch")
            yield root

"""Read-only worktree inspection and temporary materialization of pinned plugin inputs."""

import subprocess
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory

from bench.runner import validity


def git(root, *args):
    return subprocess.run(
        ["git", *args], cwd=root, capture_output=True, check=True
    ).stdout


@contextmanager
def resolve_source(source, *, root=None):
    root = Path.cwd() if root is None else Path(root)
    kind, sep, value = source.partition(":")
    if not sep or not value or kind not in ("sha", "worktree"):
        raise ValueError("arm source requires sha:REV or worktree:PATH")
    if kind == "worktree":
        path = Path(value).resolve(strict=True)
        if (
            Path(git(path, "rev-parse", "--show-toplevel").decode().strip()).resolve()
            != path
        ):
            raise ValueError("source must be a Git worktree root")
        validity.require_clean_treatment(path)
        yield path
        return
    revision = (
        git(root, "rev-parse", "--verify", "--end-of-options", value + "^{commit}")
        .decode()
        .strip()
    )
    objects = Path(git(root, "rev-parse", "--git-path", "objects").decode().strip())
    objects = (root / objects).resolve()
    fmt = git(root, "rev-parse", "--show-object-format").decode().strip()
    with TemporaryDirectory(prefix="bench-treatment-") as tmp:
        path = Path(tmp)
        metadata = path / ".git"
        (metadata / "objects/info").mkdir(parents=True)
        (metadata / "refs").mkdir()
        (metadata / "HEAD").write_text(revision + "\n", encoding="ascii")
        (metadata / "objects/info/alternates").write_text(
            str(objects) + "\n", encoding="utf-8"
        )
        (metadata / "config").write_text(
            "[core]\nrepositoryformatversion = "
            + (
                "1\n[extensions]\nobjectformat = sha256\n" if fmt == "sha256" else "0\n"
            ),
            encoding="ascii",
        )
        git(path, "read-tree", revision)
        entries = git(root, "ls-tree", "-rz", revision, "--", *validity.PLUGIN_PATHS)
        for entry in entries.split(b"\0"):
            if not entry:
                continue
            header, name = entry.split(b"\t", 1)
            mode, objtype, oid = header.split()
            if objtype != b"blob" or mode not in (b"100644", b"100755"):
                raise ValueError("plugin source requires regular files")
            target = path / name.decode("utf-8")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(git(root, "cat-file", "blob", oid.decode("ascii")))
            target.chmod(int(mode, 8) & 0o777)
        yield path

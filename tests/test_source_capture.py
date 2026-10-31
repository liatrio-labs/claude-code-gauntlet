"""Capture resource limits and retry semantics at the source boundary."""

import os
from contextlib import contextmanager

import pytest
from gauntlet import proc, source_capture
from gauntlet.source_capture import SourceAdapter

from tests.support.git_diff import git, init_git_repo


def repository(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    init_git_repo(root, monkeypatch)
    (root / "source").write_bytes(b"return original_value;\n")
    git(root, "add", ".")
    git(root, "commit", "-qm", "base")
    return root, SourceAdapter.capture(root, "local", None)


@pytest.mark.parametrize("operation", ["merge-base", "diff", "ls-files"])
def test_capture_git_failure_does_not_reserve(tmp_path, monkeypatch, operation):
    root, source = repository(tmp_path, monkeypatch)
    output = tmp_path / "output"
    output.mkdir()
    original = proc.run_bytes

    def run(argv, **kwargs):
        if argv[1] in {"diff", "ls-files"}:
            assert kwargs["timeout"] >= 180
        if argv[1] == operation:
            raise proc.TimeoutExpired(argv, kwargs["timeout"])
        return original(argv, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(proc, "run_bytes", run)
        with pytest.raises(ValueError, match="git input unavailable"):
            source.capture_sources(output, "retry", b"")
    assert list(output.iterdir()) == []
    assert source.capture_sources(output, "retry", b"").receipt["head"] == git(
        root, "rev-parse", "HEAD"
    )


def test_capture_orphan_requires_fresh_nonce(tmp_path, monkeypatch):
    _, source = repository(tmp_path, monkeypatch)
    output = tmp_path / "output"
    output.mkdir()
    capture = source.capture_sources(output, "orphan", b"")
    (capture.directory / "manifest.json").unlink()
    with pytest.raises(ValueError, match="interrupted; start over with a fresh nonce"):
        source.capture_sources(output, "orphan", b"")


@pytest.mark.parametrize("limit", ["file", "total", "binary"])
def test_capture_bounds_reads_before_copying(tmp_path, monkeypatch, limit):
    root, source = repository(tmp_path, monkeypatch)
    output = tmp_path / "output"
    output.mkdir()
    monkeypatch.setattr(source_capture, "MAX_COPY_FILE_BYTES", 16384)
    monkeypatch.setattr(source_capture, "MAX_COPY_TOTAL_BYTES", 20000)
    if limit == "total":
        (root / "a-first").write_bytes(b"x" * 12000)
    rejected = root / "z-rejected"
    rejected.write_bytes(
        b"\0" + b"x" * 10000
        if limit == "binary"
        else b"x" * (16385 if limit == "file" else 9000)
    )
    inode = rejected.stat().st_ino
    fdopen = os.fdopen
    reads = []

    @contextmanager
    def guarded(fd, mode):
        with fdopen(fd, mode) as handle:

            class Guard:
                def fileno(self):
                    return handle.fileno()

                def read(self, count=-1):
                    if os.fstat(fd).st_ino == inode:
                        reads.append(count)
                        assert limit == "binary", "oversized file was read"
                        assert 0 <= count <= 8192, "binary was fully read"
                    return handle.read(count)

            yield Guard()

    monkeypatch.setattr(os, "fdopen", guarded)
    capture = source.capture_sources(output, "bounded", b"")
    assert capture.manifest["copies"]["z-rejected"] is None
    assert capture.manifest["failures"] == ["z-rejected"]
    assert capture.receipt["copied_bytes"] == (12000 if limit == "total" else 0)
    assert reads == ([8192] if limit == "binary" else [])


def test_capture_receipt_bounds_failures(tmp_path, monkeypatch):
    root, source = repository(tmp_path, monkeypatch)
    output = tmp_path / "output"
    output.mkdir()
    names = [f"binary-{n:03d}" for n in range(40)]
    for name in names:
        (root / name).write_bytes(b"\0")
    capture = source.capture_sources(output, "failures", b"")
    assert capture.manifest["failures"] == names
    assert "failures" not in capture.receipt
    assert capture.receipt["failure_count"] == len(names)
    assert capture.receipt["failure_sample"] == names[:10]


def test_capture_reads_non_source_paths_without_copying_them(tmp_path, monkeypatch):
    root, _ = repository(tmp_path, monkeypatch)
    names = ["vendor/generated", "dist/pipeline", "tools/build/source", "app/out/view"]
    for name in names:
        file = root / name
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_bytes(b"return captured_value;\n")
    git(root, "add", ".")
    git(root, "commit", "-qm", "non-source")
    for name in names:
        (root / name).write_bytes(b"return changed_value;\n")
    output = tmp_path / "output"
    output.mkdir()
    source = SourceAdapter.capture(root, "local", None)
    capture = source.capture_sources(output, "paths", b"")
    assert capture.manifest["copies"] == {}
    for name in names:
        assert capture.adapter(root).read(name) == ("return captured_value;",)


def test_capture_reuses_git_reads_and_diff_parse(tmp_path, monkeypatch):
    root, _ = repository(tmp_path, monkeypatch)
    base = git(root, "rev-parse", "HEAD")
    (root / "source").write_bytes(b"return changed_value;\n")
    git(root, "add", ".")
    git(root, "commit", "-qm", "target")
    diff = (git(root, "diff", base, "HEAD") + "\n").encode()
    output = tmp_path / "output"
    output.mkdir()
    calls = []
    for name in ("parse_diff", "annotate_diff"):
        original = getattr(source_capture, name)

        def count(*args, _name=name, _original=original, **kwargs):
            calls.append(_name)
            return _original(*args, **kwargs)

        monkeypatch.setattr(source_capture, name, count)
    capture = SourceAdapter.capture(root, "branch", None).capture_sources(
        output, "cache", diff, base
    )
    git_calls = []
    original = proc.run_bytes

    def run(argv, **kwargs):
        git_calls.append(argv)
        return original(argv, **kwargs)

    monkeypatch.setattr(proc, "run_bytes", run)
    for _ in range(3):
        adapter = capture.adapter(root)
        assert adapter.read("source") == ("return changed_value;",)
        assert adapter.read_captured_source(
            "source",
            side="old",
            start=1,
            end=1,
            base=base,
            diff_sha256=capture.receipt["diff_sha256"],
        ) == ("return original_value;",)
    assert [a[1] for a in git_calls] == ["ls-tree", "cat-file"]
    assert calls == ["parse_diff", "annotate_diff"]

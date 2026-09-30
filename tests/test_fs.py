"""Shared file and path behavior used by the pipeline scripts."""

import glob
import os
from pathlib import Path

import pytest
from gauntlet import fs
from gauntlet.fs import JsonReadError, confined, glob_under, read_json, write_atomic

from tests.conftest import probe_symlinks


@pytest.mark.parametrize(
    "operation", ["replace", "encode_failure", "rename_failure", "parents"]
)
def test_write_atomic_replaces_or_preserves(tmp_path, monkeypatch, operation):
    destination = (
        tmp_path / "nested" / "artifact.json"
        if operation == "parents"
        else tmp_path / "artifact.json"
    )
    if operation != "parents":
        destination.write_text("previous", encoding="utf-8")
    if operation == "rename_failure":

        def fail_replace(*_args):
            raise OSError("rename failed")

        monkeypatch.setattr(os, "replace", fail_replace)
    if operation in {"encode_failure", "rename_failure"}:
        with pytest.raises(
            UnicodeEncodeError if operation == "encode_failure" else OSError
        ):
            write_atomic(
                destination, "\ud800" if operation == "encode_failure" else "new"
            )
        assert destination.read_text(encoding="utf-8") == "previous"
    else:
        write_atomic(destination, "new", create_parents=operation == "parents")
        assert destination.read_text(encoding="utf-8") == "new"
        mask = os.umask(0)
        os.umask(mask)
        assert destination.stat().st_mode & 0o777 == 0o666 & ~mask
    assert sorted(path.name for path in destination.parent.iterdir()) == [
        destination.name
    ]


def test_write_atomic_creates_sibling_temp(tmp_path, monkeypatch):
    directories = []
    original = fs.tempfile.mkstemp

    def capture(*args, **kwargs):
        directories.append(kwargs["dir"])
        return original(*args, **kwargs)

    monkeypatch.setattr(fs.tempfile, "mkstemp", capture)
    destination = tmp_path / "out.json"
    write_atomic(destination, "ok")
    assert directories == [str(tmp_path)]


@pytest.mark.parametrize(
    ("relative", "expected"),
    [
        (".", True),
        ("child/file", True),
        ("../root-other/file", False),
        ("../outside", False),
    ],
)
def test_confined_separator_and_traversal(tmp_path, relative, expected):
    root = tmp_path / "root"
    root.mkdir()
    assert confined(root / relative, root) is expected


def test_confined_resolves_symlink_escape(tmp_path, symlink_or_skip):
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    link = root / "alias"
    link.symlink_to(outside, target_is_directory=True)
    assert not confined(link / "file", root)


def test_confined_accepts_child_of_symlinked_root(tmp_path, symlink_or_skip):
    root = tmp_path / "real"
    root.mkdir()
    (root / "child").write_text("", encoding="utf-8")
    link = tmp_path / "alias"
    link.symlink_to(root, target_is_directory=True)
    assert confined(link / "child", link)


@pytest.mark.parametrize(
    ("content", "options", "kind", "cause"),
    [
        (None, {}, "read", FileNotFoundError),
        (b"\xff", {}, "read", UnicodeDecodeError),
        ("[", {}, "parse", ValueError),
        ("deep", {}, "parse", RecursionError),
        (
            "NaN",
            {"parse_constant": lambda _: (_ for _ in ()).throw(ValueError("constant"))},
            "parse",
            ValueError,
        ),
    ],
)
def test_read_json_error_classification(
    tmp_path, monkeypatch, content, options, kind, cause
):
    path = tmp_path / "input.json"
    if content == "deep":
        # Python 3.14 parses documents far past the recursion limit, so inject it.
        def too_deep(*_args, **_kwargs):
            raise RecursionError

        monkeypatch.setattr(fs.json, "loads", too_deep)
        content = "[]"
    if isinstance(content, bytes):
        path.write_bytes(content)
    elif content is not None:
        path.write_text(content, encoding="utf-8")
    with pytest.raises(JsonReadError) as caught:
        read_json(path, **options)
    assert caught.value.kind == kind
    assert isinstance(caught.value.cause, cause)


def test_read_json_replacement_decode_and_constant_policy(tmp_path):
    path = tmp_path / "input.json"
    path.write_bytes(b'{"x":"\xff"}')
    assert read_json(path, errors="replace") == {"x": "\ufffd"}
    path.write_text("NaN", encoding="utf-8")
    assert str(read_json(path)) == "nan"


def test_glob_under_literal_root_and_escaped_id(tmp_path):
    root = tmp_path / "tasks-[one]"
    root.mkdir()
    (root / "run[1].output").write_text("", encoding="utf-8")
    (root / "run1.output").write_text("", encoding="utf-8")
    assert glob_under(root, glob.escape("run[1]") + ".output") == [
        str(root / "run[1].output")
    ]


def test_glob_under_oserror_is_empty(tmp_path, monkeypatch):
    def fail(*_args, **_kwargs):
        raise OSError("glob failed")

    monkeypatch.setattr(glob, "glob", fail)
    assert glob_under(tmp_path, "*.json") == []


@pytest.mark.parametrize("capable", [False, True])
def test_symlink_capability_probe_only_skips_on_failure(
    tmp_path_factory, monkeypatch, capable
):
    original = Path.symlink_to
    calls = []

    def create(self, target, target_is_directory=False):
        # tmp_path_factory links its own "current" directory through this method too.
        if self.name not in {"file-link", "directory-link"}:
            return original(self, target, target_is_directory)
        calls.append((self.name, target_is_directory))
        if not capable:
            raise OSError("symlinks unavailable")
        return None

    monkeypatch.setattr(Path, "symlink_to", create)
    if capable:
        try:
            probe_symlinks(tmp_path_factory)
        except pytest.skip.Exception:
            pytest.fail("a working symlink probe must not skip")
        assert calls == [("file-link", False), ("directory-link", True)]
    else:
        with pytest.raises(pytest.skip.Exception):
            probe_symlinks(tmp_path_factory)
        assert calls == [("file-link", False)]

"""Incremental diff input refusals at the command boundary."""

import json

import pytest


@pytest.mark.parametrize(
    "since,files,error",
    [
        ("HEAD", [], "--since must be a 40-character lowercase hex commit id"),
        ("a" * 40, {}, "--files must contain a JSON array of path strings"),
        ("a" * 40, [""], "--files paths must be non-empty and contain no NUL"),
    ],
)
def test_incremental_diff_refuses_invalid_input(since, files, error, tmp_path, invoke):
    path = tmp_path / "files.json"
    path.write_text(json.dumps(files), encoding="utf-8")
    result = invoke(
        "incremental_diff", ["--files", str(path), "--since", since], tmp_path
    )
    assert result.returncode == 2
    assert result.stdout == b""
    assert result.stderr == f"incremental_diff: {error}\n".encode()


def test_incremental_diff_reports_git_failure(tmp_path, invoke):
    path = tmp_path / "files.json"
    path.write_text('["file"]', encoding="utf-8")
    result = invoke(
        "incremental_diff", ["--files", str(path), "--since", "a" * 40], tmp_path
    )
    assert result.returncode == 1
    assert result.stdout == b""
    assert result.stderr.startswith(b"incremental_diff: git diff failed (exit ")
    assert len(result.stderr.splitlines()) == 1

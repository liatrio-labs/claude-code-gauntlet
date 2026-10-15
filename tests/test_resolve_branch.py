"""Branch resolver refusals at the stdin boundary."""

import subprocess

import pytest


@pytest.mark.parametrize(
    "stdin,code,error",
    [
        (b"one\ntwo\n", 2, "stdin must contain one non-empty line without NUL"),
        (b"unknown-ref\n", 1, "branch does not resolve to a commit (git exit 1)"),
        (b"--help\n", 1, "branch does not resolve to a commit (git exit 1)"),
    ],
)
def test_resolve_branch_refuses_invalid_input(stdin, code, error, tmp_path, invoke):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, capture_output=True, check=True)
    result = invoke("resolve_branch", [], tmp_path, stdin)
    assert result.returncode == code
    assert result.stdout == b""
    assert result.stderr == f"resolve_branch: {error}\n".encode()

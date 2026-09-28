"""Boundary fixtures for Python command tests."""

import importlib
import io
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True, slots=True)
class Invocation:
    returncode: int
    stdout: bytes
    stderr: bytes


@pytest.fixture
def invoke(monkeypatch, capsys):
    def call(name, arguments, cwd):
        module = importlib.import_module(f"gauntlet.{name}")
        monkeypatch.chdir(cwd)
        monkeypatch.setattr(sys, "argv", [str(ROOT / "scripts" / f"{name}.py")])
        monkeypatch.setattr(
            sys, "stdin", io.TextIOWrapper(io.BytesIO(b"{}\n"), encoding="utf-8")
        )
        if name == "ensure_output_dir":
            monkeypatch.setenv(
                "CODE_GAUNTLET_OUTPUT_DIR", "/private/tmp/s407-cli-contract-output"
            )
        capsys.readouterr()
        code = module.CLI.invoke(arguments)
        captured = capsys.readouterr()
        return Invocation(
            code,
            captured.out.encode("utf-8", errors="surrogateescape"),
            captured.err.encode("utf-8", errors="backslashreplace"),
        )

    return call

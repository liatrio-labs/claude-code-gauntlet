"""Boundary fixtures for Python command tests."""

import importlib
import io
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest
from gauntlet import COMMAND_MODULES

ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True, slots=True)
class Invocation:
    returncode: int
    stdout: bytes
    stderr: bytes


@pytest.fixture
def invoke(monkeypatch, capsys):
    def call(name, arguments, cwd, stdin=b"{}\n"):
        module = importlib.import_module(f"gauntlet.{COMMAND_MODULES[name]}")
        monkeypatch.chdir(cwd)
        monkeypatch.setattr(
            sys, "stdin", io.TextIOWrapper(io.BytesIO(stdin), encoding="utf-8")
        )
        capsys.readouterr()
        try:
            code = module.CLI.invoke(arguments)
        except Exception as exc:  # noqa: BLE001 - the wrapper exits one on an uncaught exception
            print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
            code = 1
        captured = capsys.readouterr()
        return Invocation(
            code,
            captured.out.encode("utf-8", errors="surrogateescape"),
            captured.err.encode("utf-8", errors="backslashreplace"),
        )

    return call

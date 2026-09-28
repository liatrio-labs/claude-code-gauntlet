"""Boundary fixtures for Python command tests."""

import importlib
import io
import re
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ENTRY_IMPORT = re.compile(r"^from gauntlet\.([\w.]+) import CLI$", re.MULTILINE)


@dataclass(frozen=True, slots=True)
class Invocation:
    converted: bool
    returncode: int
    stdout: bytes
    stderr: bytes


@pytest.fixture
def invoke(monkeypatch, capsys):
    def call(name, arguments, cwd, stdin=b"{}\n"):
        entry = (ROOT / "scripts" / f"{name}.py").read_text(encoding="utf-8")
        cli = importlib.import_module(f"gauntlet.{ENTRY_IMPORT.search(entry)[1]}").CLI
        monkeypatch.chdir(cwd)
        monkeypatch.setattr(
            sys, "stdin", io.TextIOWrapper(io.BytesIO(stdin), encoding="utf-8")
        )
        capsys.readouterr()
        try:
            code = cli.invoke(arguments)
        except Exception as exc:  # noqa: BLE001 - the wrapper exits one on an uncaught exception
            print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
            code = 1
        captured = capsys.readouterr()
        return Invocation(
            cli.parser is not None,
            code,
            captured.out.encode("utf-8", errors="surrogateescape"),
            captured.err.encode("utf-8", errors="backslashreplace"),
        )

    return call

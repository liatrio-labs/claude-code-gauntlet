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
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else 1
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


@pytest.fixture
def verify_git(monkeypatch):
    from gauntlet import proc

    calls = []
    replies = {
        "log": ("abcdef0123456789\n", "", 0),
        "blame": ("abcdef0 (First Author 2024-01-02 00:00:00 +0000 1) code", "", 0),
        "grep": ("source\n", "", 0),
        "diff": ("", "", 0),
        "rev-parse": ("abcd\n", "", 0),
    }

    def output(argv, **kwargs):
        calls.append((list(argv), kwargs))
        reply = replies[argv[1]]
        if callable(reply):
            return reply(argv)
        if isinstance(reply, Exception):
            raise reply
        return reply

    monkeypatch.setattr(proc, "output", output)
    return calls, replies


def probe_symlinks(tmp_path_factory):
    probe = tmp_path_factory.mktemp("symlink-probe")
    source = probe / "file"
    source.write_text("x", encoding="utf-8")
    directory = probe / "directory"
    directory.mkdir()
    try:
        (probe / "file-link").symlink_to(source)
        (probe / "directory-link").symlink_to(directory, target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlinks unavailable: {exc}")


@pytest.fixture(scope="module")
def symlink_or_skip(tmp_path_factory):
    probe_symlinks(tmp_path_factory)


@pytest.fixture
def forge_factory(monkeypatch, request):
    from gauntlet.delivery import post

    from tests.support.forge import install_forge_factory

    factory = install_forge_factory(monkeypatch, getattr(request, "param", post))
    if request.instance is not None:
        request.instance.forge_factory = factory
    return factory


@pytest.fixture
def poster_state(monkeypatch):
    monkeypatch.delenv("CODE_GAUNTLET_POST_MODE", raising=False)


@pytest.fixture
def poster_workspace(tmp_path, request):
    if request.instance is not None:
        request.instance.tmp = str(tmp_path)
        request.instance.findings_path = str(tmp_path / "findings.json")

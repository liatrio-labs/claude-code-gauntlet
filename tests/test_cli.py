"""CLI serialization and stdio contracts."""

import io
import json
import sys
from contextlib import redirect_stdout
from unittest.mock import patch

import pytest
from gauntlet.cli import Command, Parser, utf8_stdio
from gauntlet.jsjson import write_result


def test_stdout_escapes_lone_surrogates_and_stays_parseable():
    output = io.StringIO()
    with redirect_stdout(output):
        write_result({"value": "bad\ud800"})
    assert r"\ud800" in output.getvalue()
    assert json.loads(output.getvalue())["value"] == "bad\ud800"


def test_text_streams_use_utf8_mode_errors_and_lf():
    stdout_buffer = io.BytesIO()
    stderr_buffer = io.BytesIO()
    stdin_buffer = io.BytesIO(b"\xe2\x80\x94\xff")
    stdout = io.TextIOWrapper(stdout_buffer, encoding="latin-1", newline="\r\n")
    stderr = io.TextIOWrapper(stderr_buffer, encoding="latin-1", newline="\r\n")
    stdin = io.TextIOWrapper(stdin_buffer, encoding="latin-1", newline="\r\n")
    with (
        patch.object(sys, "stdout", stdout),
        patch.object(sys, "stderr", stderr),
        patch.object(sys, "stdin", stdin),
    ):
        utf8_stdio()
        stdout.write("\udcff—\n")
        stderr.write("\ud800\n")
        stdout.flush()
        stderr.flush()
        assert stdin.read() == "—\udcff"
    assert stdout_buffer.getvalue() == b"\xff\xe2\x80\x94\n"
    assert stderr_buffer.getvalue() == b"\\ud800\n"


def test_non_text_streams_are_left_alone():
    streams = (io.StringIO(), io.StringIO(), io.StringIO())
    with (
        patch.object(sys, "stdout", streams[0]),
        patch.object(sys, "stderr", streams[1]),
        patch.object(sys, "stdin", streams[2]),
    ):
        utf8_stdio()
        assert (sys.stdout, sys.stderr, sys.stdin) == streams


@pytest.mark.parametrize(
    ("argv", "status", "expected"),
    [
        ([], 2, b""),
        (["--value", "ok"], 0, b'{"value": "ok"}\n'),
    ],
)
def test_command_invoke(argv, status, expected, capsys):
    parser = Parser(prog="surrogate")
    parser.add_argument("--value", required=True)
    command = Command(parser=parser, main=lambda args: ({"value": args.value}, 0))
    assert command.invoke(argv) == status
    captured = capsys.readouterr()
    assert captured.out.encode() == expected
    if status:
        assert (
            captured.err == "surrogate: the following arguments are required: --value\n"
        )


def test_legacy_command_sets_its_own_prog(monkeypatch):
    seen = []

    def legacy_main():
        seen.append(tuple(sys.argv))
        return 0

    monkeypatch.setattr(sys, "argv", ["foreign.py"])
    command = Command.legacy(legacy_main, prog="owned.py")
    assert command.invoke(["--probe"]) == 0
    assert seen == [("owned.py", "--probe")]
    assert sys.argv == ["foreign.py"]


@pytest.mark.parametrize(
    ("fallback", "expected"),
    [
        (
            lambda exc: {"ok": False, "errors": [type(exc).__name__]},
            b'{"ok": false, "errors": ["TypeError"]}\n',
        ),
        (
            lambda exc: 1 / 0,
            b'{"ok": false, "errors": ["receipt serialization failed"]}\n',
        ),
    ],
)
def test_command_serialization_fallback(fallback, expected, capsys):
    command = Command(
        parser=Parser(prog="surrogate"),
        main=lambda args: ({"unserializable": object()}, 0),
        fallback_receipt=fallback,
    )
    assert command.invoke([]) == 1
    assert capsys.readouterr().out.encode() == expected

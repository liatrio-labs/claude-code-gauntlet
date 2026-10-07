"""CLI serialization and stdio contracts."""

import io
import sys
from unittest.mock import patch

import pytest
from gauntlet.cli import Command, Parser, utf8_stdio


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
    ("argv", "options", "status", "expected"),
    [
        pytest.param([], {}, 2, b"", id="usage"),
        pytest.param(["--value", "ok"], {}, 0, b'{"value": "ok"}\n', id="default"),
        pytest.param(
            ["--value", "ok"], {"compact": True}, 0, b'{"value":"ok"}\n', id="compact"
        ),
        pytest.param(
            ["--value", "ok"],
            {"indent": 2},
            0,
            b'{\n  "value": "ok"\n}\n',
            id="indented",
        ),
    ],
)
def test_command_invoke(argv, options, status, expected, capsys):
    parser = Parser(prog="surrogate")
    parser.add_argument("--value", required=True)
    command = Command(
        parser=parser, main=lambda args: ({"value": args.value}, 0), **options
    )
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


def test_shared_context_unexpected_failure_emits_receipt(monkeypatch, capsys, tmp_path):
    from gauntlet import shared_context

    def fail_read(*args):
        raise RuntimeError("probe")

    monkeypatch.setattr(shared_context, "_read_input", fail_read)
    assert (
        shared_context.CLI.invoke(["--output-dir", str(tmp_path), "--head-sha", "abcd"])
        == 1
    )
    assert capsys.readouterr().out == '{"error": "unexpected RuntimeError: probe"}\n'


def test_command_receipt_succeeds_when_stdout_is_none(monkeypatch):
    monkeypatch.setattr(sys, "stdout", None)
    command = Command(parser=Parser(prog="silent"), main=lambda _: ({"ok": True}, 37))
    assert command.invoke([]) == 37


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


@pytest.mark.parametrize(
    "case, code, output, writes",
    [
        pytest.param(
            "success", 0, '{"ok": true}\n', ['{"ok": true}\n'], id="receipt-one-write"
        ),
        pytest.param(
            "first-write",
            1,
            '{"ok": false}\n',
            ['{"ok": true}\n', '{"ok": false}\n'],
            id="write-fallback-one-write",
        ),
        pytest.param(
            "encoder",
            1,
            '{"ok": false}\n',
            ['{"ok": false}\n'],
            id="encoding-fallback-one-write",
        ),
    ],
)
def test_command_writes_complete_lines(case, code, output, writes, monkeypatch):
    calls = []

    class LineStream(io.StringIO):
        def write(self, text):
            calls.append(text)
            if text == "\n" or (case == "first-write" and len(calls) == 1):
                raise OSError("standalone LF or transient first write")
            return super().write(text)

    stream = LineStream()
    monkeypatch.setattr(sys, "stdout", stream)
    command = Command(
        parser=Parser(prog="probe"),
        main=lambda _: ({"ok": object() if case == "encoder" else True}, 0),
        fallback_line='{"ok": false}',
    )
    assert command.invoke([]) == code
    assert stream.getvalue() == output
    assert calls == writes

"""Command conversion preserves fallback encoding, outcome codes, and root isolation."""

import json
import os
from unittest.mock import patch

import pytest
from gauntlet import artifacts, awaiting, cli, materialize, tasks


@pytest.mark.parametrize(
    "command,argv,expected",
    [
        (
            artifacts.CLI,
            ["--plan", "missing"],
            '{"ok": false, "planVersion": null, "planChecksum": null, "verified": [], "written": [], "errors": ["receipt could not be serialized: ValueError: caf\\u00e9"]}',
        ),
        (
            materialize.CLI,
            ["--output-dir", ".", "--task", "missing.output"],
            '{"ok": false, "channel": "return", "source": null, "scanned": 0, "materialized": [], "assemble": null, "gaps": [], "errors": ["receipt could not be serialized: ValueError: caf\\u00e9"]}',
        ),
    ],
)
def test_command_fallback_receipt_matches_old_ascii_bytes(
    command, argv, expected, monkeypatch, capsys
):
    original = cli.dumps

    def encode(receipt, **kwargs):
        if not kwargs.get("ascii"):
            raise ValueError("caf\u00e9")
        return original(receipt, **kwargs)

    monkeypatch.setattr(cli, "dumps", encode)
    assert command.invoke(argv) == 1
    assert capsys.readouterr().out.strip() == expected


@pytest.mark.parametrize(
    "command,argv,hostile,expected",
    [
        (
            artifacts.CLI,
            ["--plan", "missing"],
            False,
            '{"ok": false, "planVersion": null, "planChecksum": null, "verified": [], "written": [], "errors": ["receipt could not be serialized"]}',
        ),
        (
            materialize.CLI,
            ["--output-dir", ".", "--task", "missing.output"],
            False,
            '{"ok": false, "channel": "return", "source": null, "scanned": 0, "materialized": [], "assemble": null, "gaps": [], "errors": ["receipt could not be serialized"]}',
        ),
        (
            artifacts.CLI,
            ["--plan", "missing"],
            True,
            '{"ok": false, "planVersion": null, "planChecksum": null, "verified": [], "written": [], "errors": ["receipt could not be serialized"]}',
        ),
        (
            materialize.CLI,
            ["--output-dir", ".", "--task", "missing.output"],
            True,
            '{"ok": false, "channel": "return", "source": null, "scanned": 0, "materialized": [], "assemble": null, "gaps": [], "errors": ["receipt could not be serialized"]}',
        ),
    ],
)
def test_command_final_constant_matches_old_bytes(
    command, argv, hostile, expected, monkeypatch, capsys
):
    class Hostile(Exception):
        def __str__(self):
            raise RuntimeError("cannot render")

    def fail(*args, **kwargs):
        raise Hostile() if hostile else ValueError("encoding failed")

    monkeypatch.setattr(cli, "dumps", fail)
    assert command.invoke(argv) == 1
    assert capsys.readouterr().out.strip() == expected


@pytest.mark.parametrize("outcome", [0, 3, 4, 5])
def test_await_encoding_fallback_keeps_outcome(outcome, monkeypatch, capsys):
    monkeypatch.setattr(awaiting, "await_terminal", lambda *_: ({"bad": {1}}, outcome))
    assert awaiting.CLI.invoke(["bare-id", "--timeout-seconds", "0"]) == outcome
    assert capsys.readouterr().out.strip() == (
        '{"await":"error","gap":"workflow-timeout",'
        '"message":"result would not serialize: Object of type set is not JSON serializable"}'
    )


@pytest.mark.parametrize(
    "failures",
    [(RuntimeError,), (RecursionError,), (TypeError, ValueError)],
    ids=["runtime-error", "recursion-error", "fallback-error"],
)
def test_await_encoder_exceptions_keep_original_scope(failures, monkeypatch, capsys):
    pending = iter(failures)

    def fail(*args, **kwargs):
        raise next(pending)("encoding failed")

    monkeypatch.setattr(awaiting, "await_terminal", lambda *_: ({"ok": True}, 5))
    monkeypatch.setattr(json, "dumps", fail)
    with pytest.raises(failures[-1], match=r"^encoding failed$"):
        awaiting.CLI.invoke(["bare-id", "--timeout-seconds", "0"])
    assert capsys.readouterr().out.strip() == ""


def test_await_preserves_compact_ascii_nan_spelling(monkeypatch, capsys):
    monkeypatch.setattr(
        awaiting,
        "await_terminal",
        lambda *_: ({"ok": True, "stats": float("nan"), "label": "caf\u00e9"}, 0),
    )
    assert awaiting.CLI.invoke(["bare-id", "--timeout-seconds", "0"]) == 0
    assert (
        capsys.readouterr().out.strip()
        == '{"ok":true,"stats":NaN,"label":"caf\\u00e9"}'
    )


def test_timeout_default_is_resolved_at_invocation_as_an_integer(monkeypatch, capsys):
    monkeypatch.setenv("BASH_MAX_TIMEOUT_MS", "600000")
    with patch("gauntlet.awaiting.time.time", side_effect=[0, 600, 600]):
        assert awaiting.CLI.invoke(["bare-id", "--since-epoch", "0"]) == 3
    marker = json.loads(capsys.readouterr().out)
    assert "--timeout-seconds 540 " in marker["next_command"]
    monkeypatch.setenv("BASH_MAX_TIMEOUT_MS", "300000")
    with patch("gauntlet.awaiting.time.time", side_effect=[0, 300, 300]):
        assert awaiting.CLI.invoke(["bare-id", "--since-epoch", "0"]) == 3
    marker = json.loads(capsys.readouterr().out)
    assert "--timeout-seconds 240 " in marker["next_command"]


def test_directory_target_retains_file_bytes(tmp_path, monkeypatch, capsys):
    target = tmp_path / "directory.output"
    target.mkdir()
    getsize = os.path.getsize
    monkeypatch.setattr(
        os.path, "getsize", lambda path: 37 if path == str(target) else getsize(path)
    )
    assert awaiting.CLI.invoke([str(target), "--timeout-seconds", "0"]) == 3
    marker = json.loads(capsys.readouterr().out)
    assert marker["file_bytes"] == 37
    assert marker["resolved_path"] == str(target)


def test_explicit_await_path_never_probes_discovery_roots(
    tmp_path, monkeypatch, capsys
):
    target = tmp_path / "task.output"
    target.write_text('{"ok":true,"stats":{}}', encoding="utf-8")
    monkeypatch.setattr(
        os.path,
        "realpath",
        lambda *_: pytest.fail("explicit path consulted roots"),
    )
    assert awaiting.CLI.invoke([str(target), "--timeout-seconds", "0"]) == 0
    assert capsys.readouterr().out.strip() == '{"ok":true,"stats":{}}'


@pytest.mark.parametrize(
    "command,argv,expected_code,expected",
    [
        (
            materialize.CLI,
            ["--output-dir", ".", "--task", "missing.output"],
            1,
            {
                "ok": False,
                "channel": "return",
                "source": None,
                "scanned": 1,
                "materialized": [],
                "assemble": None,
                "gaps": [],
                "errors": [
                    "no task output file carrying this run's returned artifacts was found "
                    "(looked at 1 candidate file(s) for target 'missing.output' / nonce None)"
                ],
            },
        ),
        (
            materialize.CLI,
            ["--output-dir", ".", "--task", "missing"],
            1,
            {
                "ok": False,
                "channel": "return",
                "source": None,
                "scanned": 0,
                "materialized": [],
                "assemble": None,
                "gaps": [],
                "errors": [
                    "materializer failed unexpectedly: OSError: discovery failed"
                ],
            },
        ),
        (
            materialize.CLI,
            ["--output-dir", ".", "--nonce", "run"],
            1,
            {
                "ok": False,
                "channel": "return",
                "source": None,
                "scanned": 0,
                "materialized": [],
                "assemble": None,
                "gaps": [],
                "errors": [
                    "materializer failed unexpectedly: OSError: discovery failed"
                ],
            },
        ),
        (
            materialize.CLI,
            ["--output-dir", ".", "--task", "missing.output", "--nonce", "run"],
            1,
            {
                "ok": False,
                "channel": "return",
                "source": None,
                "scanned": 0,
                "materialized": [],
                "assemble": None,
                "gaps": [],
                "errors": [
                    "materializer failed unexpectedly: OSError: discovery failed"
                ],
            },
        ),
        (
            awaiting.CLI,
            ["missing", "--timeout-seconds", "0"],
            4,
            {
                "await": "error",
                "gap": "workflow-timeout",
                "message": "OSError: discovery failed",
                "target": "missing",
                "attempt": 1,
                "max_attempts": 4,
            },
        ),
        (
            awaiting.CLI,
            ["missing.output", "--timeout-seconds", "0"],
            0,
            {"ok": True, "stats": {}},
        ),
    ],
    ids=[
        "materialize-path",
        "materialize-id",
        "materialize-nonce",
        "materialize-path-with-nonce",
        "await-id",
        "await-path",
    ],
)
def test_discovery_failure_stays_inside_command_receipts(
    command, argv, expected_code, expected, tmp_path, monkeypatch, capsys
):
    monkeypatch.chdir(tmp_path)
    if command is awaiting.CLI:
        (tmp_path / "missing.output").write_text(
            '{"ok":true,"stats":{}}', encoding="utf-8"
        )

    def fail(environ):
        raise OSError("discovery failed")

    monkeypatch.setattr(tasks, "roots_from_environment", fail)
    assert command.invoke(argv) == expected_code
    assert json.loads(capsys.readouterr().out.strip()) == expected

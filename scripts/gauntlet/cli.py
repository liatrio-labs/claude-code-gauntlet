"""Shared command boundary for retained scripts."""

from __future__ import annotations

import argparse
import io
import json
import re
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import NoReturn

from gauntlet.jsjson import dumps


def warn(message: str) -> None:
    print(f"WARNING: {message}", file=sys.stderr)


def require_head_sha(value: str) -> None:
    """Reject values outside the lowercase abbreviated/full Git SHA shape."""
    if re.fullmatch(r"[0-9a-f]{4,40}", value) is None:
        raise UsageError(
            "--head-sha must be 4 to 40 lowercase hexadecimal characters", 2
        )


def require_full_sha(value: str, name: str) -> None:
    if re.fullmatch(r"[0-9a-f]{40}", value) is None:
        raise CliError(f"{name} must be a 40-character lowercase hex commit id", 2)


def utf8_stdio() -> None:
    """Use Python UTF-8 mode stream encodings and LF output at CLI boundaries."""
    if isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout.reconfigure(encoding="utf-8", errors="surrogateescape", newline="\n")
    if isinstance(sys.stderr, io.TextIOWrapper):
        sys.stderr.reconfigure(
            encoding="utf-8", errors="backslashreplace", newline="\n"
        )
    if isinstance(sys.stdin, io.TextIOWrapper):
        sys.stdin.reconfigure(encoding="utf-8", errors="surrogateescape")


def _write_line(line: str) -> None:
    if sys.stdout is not None:
        # A separate LF write can fail after the receipt has already escaped.
        sys.stdout.write(line + "\n")


class CliError(Exception):
    def __init__(self, message: str, code: int = 1) -> None:
        super().__init__(" ".join(message.splitlines()))
        self.code = code


class UsageError(CliError):
    pass


def read_stdin_bytes() -> bytes:
    if sys.stdin is None:
        raise UsageError("cannot read stdin", 2)
    try:
        return sys.stdin.buffer.read()
    except OSError:
        raise UsageError("cannot read stdin", 2) from None


def read_stdin_json_object() -> dict[str, object]:
    try:
        payload = json.loads(read_stdin_bytes().decode("utf-8"))
    except (ValueError, UnicodeError, RecursionError):
        raise CliError("stdin must contain a JSON object", 2) from None
    if not isinstance(payload, dict):
        raise CliError("stdin must contain a JSON object", 2)
    return payload


def read_stdin_line() -> str:
    try:
        value = read_stdin_bytes().decode("utf-8")
    except UnicodeError:
        raise CliError("stdin must be UTF-8", 2) from None
    if value.endswith("\r\n"):
        value = value[:-2]
    elif value.endswith("\n"):
        value = value[:-1]
    if not value or any(character in value for character in "\r\n\0"):
        raise CliError("stdin must contain one non-empty line without NUL", 2)
    return value


class Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise UsageError(message, 2)


@dataclass(frozen=True, slots=True)
class Command:
    parser: Parser
    main: Callable[[argparse.Namespace], int | tuple[Mapping[str, object], int]]
    failure_receipt: Callable[[str], Mapping[str, object]] | None = None
    ascii: bool = True
    compact: bool = False
    indent: int | None = None
    fallback_receipt: Callable[[Exception], Mapping[str, object]] | None = None
    fallback_line: str = '{"ok": false, "errors": ["receipt serialization failed"]}'
    fallback_code: int = 1

    def run(self) -> NoReturn:
        utf8_stdio()
        raise SystemExit(self.invoke(sys.argv[1:]))

    def invoke(self, argv: Sequence[str]) -> int:
        try:
            outcome = self.main(self.parser.parse_args(argv))
        except CliError as exc:
            if self.failure_receipt is None or isinstance(exc, UsageError):
                print(f"{self.parser.prog}: {exc}", file=sys.stderr)
                return exc.code
            outcome = (self.failure_receipt(str(exc)), exc.code)
        except Exception as exc:
            if self.failure_receipt is None:
                raise
            outcome = (
                self.failure_receipt(f"unexpected {type(exc).__name__}: {exc}"),
                1,
            )
        if isinstance(outcome, int):
            return outcome
        receipt, code = outcome
        try:
            _write_line(
                dumps(
                    receipt, ascii=self.ascii, compact=self.compact, indent=self.indent
                )
            )
        # Keep a receipt on serialization failure.
        except Exception as exc:  # noqa: BLE001
            line = self.fallback_line
            try:
                if self.fallback_receipt is not None:
                    line = dumps(self.fallback_receipt(exc), ascii=True)
            except Exception:  # noqa: BLE001 - constant is the final receipt
                pass
            _write_line(line)
            return self.fallback_code
        return code

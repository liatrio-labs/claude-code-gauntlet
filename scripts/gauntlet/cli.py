"""Shared CLI result-write helpers for retained scripts (stdlib-only)."""

from __future__ import annotations

import argparse
import io
import re
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import NoReturn


def require_head_sha(prog: str, value: str) -> None:
    """Reject values outside the lowercase abbreviated/full Git SHA shape."""
    if re.fullmatch(r"[0-9a-f]{4,40}", value) is None:
        raise CliError("--head-sha must be 4 to 40 lowercase hexadecimal characters", 2)


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


def run_entrypoint(main, *args) -> None:
    """Apply the shared stdio contract, then propagate the CLI's exit status."""
    utf8_stdio()
    raise SystemExit(main(*args))


class CliError(Exception):
    def __init__(self, message: str, code: int = 1) -> None:
        super().__init__(" ".join(message.splitlines()))
        self.code = code


class UsageError(CliError):
    pass


class Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise UsageError(message, 2)


@dataclass(frozen=True, slots=True)
class Command:
    parser: Parser | None = None
    main: (
        Callable[[argparse.Namespace], int | tuple[Mapping[str, object], int]] | None
    ) = None
    failure_receipt: Callable[[str], Mapping[str, object]] | None = None
    ascii: bool = True
    compact: bool = False
    fallback_receipt: Callable[[Exception], Mapping[str, object]] | None = None
    fallback_line: str = '{"ok": false, "errors": ["receipt serialization failed"]}'
    fallback_code: int = 1
    _legacy: Callable[[], int] | None = None

    @classmethod
    def legacy(cls, main: Callable[[], int]) -> Command:
        return cls(_legacy=main)

    def run(self) -> NoReturn:
        utf8_stdio()
        raise SystemExit(self.invoke(sys.argv[1:]))

    def invoke(self, argv: Sequence[str]) -> int:
        if self._legacy is not None:
            saved = sys.argv
            sys.argv = [saved[0], *argv]
            try:
                return self._legacy()
            except SystemExit as exc:
                if isinstance(exc.code, int):
                    return exc.code
                if exc.code is None:
                    return 0
                print(exc.code, file=sys.stderr)
                return 1
            finally:
                sys.argv = saved
        if self.parser is None or self.main is None:
            raise RuntimeError("command has no parser or main")
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
            from gauntlet.jsjson import dumps

            print(dumps(receipt, ascii=self.ascii, compact=self.compact))
        except Exception as exc:  # noqa: BLE001 - receipt serialization must have a fallback
            if self.fallback_receipt is not None:
                try:
                    print(dumps(self.fallback_receipt(exc), ascii=True))
                except Exception:  # noqa: BLE001 - constant is the final receipt
                    print(self.fallback_line)
            else:
                print(self.fallback_line)
            return self.fallback_code
        return code

"""The subprocess boundary, error types and exit handling the release scripts share."""

import argparse
import os
import re
import subprocess
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn

TAG = r"v[0-9]+\.[0-9]+\.[0-9]+"
TAG_PATTERN = re.compile(f"^{TAG}$")


@dataclass(frozen=True, slots=True)
class CommandFailure(Exception):
    argv: tuple[str, ...]
    returncode: int
    stderr: str


class InputError(Exception):
    """An input or external response a release command cannot safely use."""

    def report(self) -> list[str]:
        # External text can run long; its first line names the problem.
        return str(self).splitlines()[:1]


class ArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        raise InputError(message)


def validate_tag(tag: str) -> str:
    if not TAG_PATTERN.fullmatch(tag):
        raise InputError(f"malformed release tag: {tag!r}; expected vX.Y.Z")
    return tag


def run_command(
    argv: list[str],
    cwd: str | Path | None = None,
    env: Mapping[str, str] | None = None,
) -> str:
    """Run the only subprocess boundary and return stdout as text."""
    extra = {"env": {**os.environ, **env}} if env else {}
    try:
        result = subprocess.run(
            argv,
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
            **extra,
        )
    except UnicodeDecodeError:
        raise InputError("command output is not valid UTF-8") from None
    except OSError as error:
        raise CommandFailure(tuple(argv), 127, str(error)) from None
    if result.returncode:
        raise CommandFailure(tuple(argv), result.returncode, result.stderr.strip())
    return result.stdout


def describe(failure: CommandFailure) -> str:
    detail = [line.strip() for line in failure.stderr.splitlines() if line.strip()]
    suffix = f": {detail[-1]}" if detail else ""
    return f"command failed ({failure.returncode}): {' '.join(failure.argv)}{suffix}"


def exit_code(operation: Callable[[], int]) -> int:
    """Run a command body, turning each expected failure into stderr lines and exit 2."""
    try:
        return operation()
    except InputError as error:
        # A failure without a message still has to say something.
        lines = error.report() or [type(error).__name__]
    except CommandFailure as error:
        lines = [describe(error)]
    except OSError as error:
        lines = [f"file operation failed: {''.join(str(error).splitlines()[:1])}"]
    for line in lines:
        print(line, file=sys.stderr)
    return 2


def entry(main: Callable[[], int]) -> NoReturn:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    from gauntlet.cli import utf8_stdio

    utf8_stdio()
    raise SystemExit(main())

"""Ordered generated-target comparison, atomic writes, and command outcomes."""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping, Sequence

from gauntlet.cli import CliError
from gauntlet.fs import read_text, write_atomic


def sync_targets(
    repo_root: str | os.PathLike[str],
    targets: Mapping[str, str | Callable[[str], str]],
    check: bool,
) -> list[str]:
    stale = []
    for name, target in targets.items():
        path = os.path.join(repo_root, name)
        try:
            current = read_text(path)
        except FileNotFoundError:
            if callable(target):
                raise
            current = ""
        expected = target(current) if callable(target) else target
        if expected == current:
            continue
        stale.append(name)
        if not check:
            write_atomic(path, expected)
    return stale


def finish(
    stale: Sequence[str],
    check: bool,
    *,
    current_message: str,
    stale_description: str,
    command: str,
) -> int:
    if not stale:
        print(current_message)
    elif check:
        raise CliError(f"{stale_description}: {', '.join(stale)}; run: {command}")
    else:
        print(f"regenerated: {', '.join(stale)}")
    return 0

from __future__ import annotations

import subprocess
from collections.abc import Callable
from urllib.parse import urlsplit

from tests.tools.probe_html import (
    _attrs,
    _TreeParser,
    _walk,
)


def render_github(
    text: str,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> str:
    result = runner(
        ["gh", "api", "markdown", "-f", "mode=gfm", "-f", f"text={text}"],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return result.stdout


def github_login(
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> str:
    result = runner(
        ["gh", "api", "user", "--jq", ".login"],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    login = result.stdout.strip()
    if not login:
        raise RuntimeError("GitHub login is empty")
    return login


def run_github_canary(login: str, render: Callable[[str], str]) -> None:
    parser = _TreeParser()
    parser.feed(render("@" + login))
    parser.close()
    if not any(
        node.tag == "a"
        and "user-mention" in (_attrs(node).get("class") or "").split()
        and (
            urlsplit(_attrs(node).get("href") or "")
            .path.lower()
            .endswith("/" + login.lower())
            or "".join(
                child for child in node.children if isinstance(child, str)
            ).lower()
            == "@" + login.lower()
        )
        for node, _ in _walk(parser.root)
    ):
        raise RuntimeError(f"GitHub mention canary unresolved login: {login}")

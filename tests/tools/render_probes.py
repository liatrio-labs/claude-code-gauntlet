"""Record and check renderer probes.

Start GitLab with the same external URL the recorded probes use, since rendered
links are absolute::

    docker run --detach --name cdr-gitlab --hostname localhost \
      --publish 8929:8929 --shm-size 256m \
      --env GITLAB_OMNIBUS_CONFIG="external_url 'http://localhost:8929'" \
      gitlab/gitlab-ce:19.4.1-ce.0

When ``curl -s -o /dev/null -w '%{http_code}' http://localhost:8929/api/v4/version``
prints 401, export a root API token::

    export GITLAB_TOKEN="$(docker exec cdr-gitlab gitlab-rails runner \
      'u=User.find_by_username("root"); puts u.personal_access_tokens.create!(name: "render-probes", scopes: ["api"], expires_at: Date.today + 30).token')"

Then run ``python3 tests/tools/render_probes.py seed`` and
``python3 tests/tools/render_probes.py record --platform gitlab``. The GitHub probe
needs only an authenticated ``gh``: ``python3 tests/tools/render_probes.py record
--platform github``. Add ``--check`` to either ``record`` to re-render and compare
without writing.
"""

from __future__ import annotations

import argparse
import base64
import copy
import hashlib
import html
import json
import os
import re
import secrets
import subprocess
import sys
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Protocol, cast
from urllib.error import HTTPError
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen

RENDERER_ATTRIBUTES_BY_PLATFORM: dict[
    str, dict[tuple[str, str], str | Callable[[str | None], bool] | None]
] = {
    "gitlab": {
        ("*", "data-sourcepos"): None,
        ("*", "dir"): "auto",
        ("a", "data-canonical-src"): None,
        ("pre", "data-canonical-lang"): None,
        ("pre", "data-lang-params"): None,
        ("pre", "v-pre"): "true",
        ("a", "rel"): "nofollow noreferrer noopener",
        ("a", "target"): "_blank",
        **{
            (f"h{level}", "id"): lambda value: (
                isinstance(value, str) and value.startswith("user-content-")
            )
            for level in range(1, 7)
        },
    },
    "github": {
        ("table", "role"): "table",
        ("pre", "data-meta"): None,
        ("pre", "lang"): None,
        ("a", "rel"): "nofollow",
    },
}
_RENDERER_CLASS_VALUES = {
    "gitlab": {
        ("a", "gfm"),
        ("pre", "code highlight js-syntax-highlight language-plaintext"),
    },
    "github": {("pre", "notranslate"), ("code", "notranslate")},
}
INHERENTLY_PLAIN = {
    "all": "GitLab 19.4.1 renders @all as plain text",
    "x": "GitLab refuses one-character usernames",
}
SEED_GROUPS = ("team",)
_SEGMENT = r"(?:[a-zA-Z0-9_.][a-zA-Z0-9_\-.]{0,254}[a-zA-Z0-9_\-]|[a-zA-Z0-9_])"
_HANDLE_PATTERN = re.compile(
    rf"(?<![A-Za-z0-9_])[@\uff20]({_SEGMENT}(?:/{_SEGMENT}){{0,19}})"
)
ELEMENT_VOCABULARIES = {
    "gitlab": frozenset(
        {
            "a",
            "blockquote",
            "code",
            "h1",
            "h2",
            "li",
            "ol",
            "p",
            "pre",
            "strong",
            "table",
            "tbody",
            "td",
            "th",
            "thead",
            "tr",
            "ul",
        }
    ),
    "github": frozenset(
        {
            "a",
            "blockquote",
            "br",
            "code",
            "h1",
            "h2",
            "li",
            "ol",
            "p",
            "pre",
            "strong",
            "table",
            "tbody",
            "td",
            "th",
            "thead",
            "tr",
            "ul",
        }
    ),
}


@dataclass
class _Node:
    tag: str
    attrs: tuple[tuple[str, str | None], ...] = ()
    children: list[_Node | str] = field(default_factory=list)


class _TreeParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.root = _Node("#root")
        self.stack = [self.root]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        node = _Node(tag, tuple(attrs))
        self.stack[-1].children.append(node)
        if tag not in {
            "area",
            "base",
            "br",
            "col",
            "embed",
            "hr",
            "img",
            "input",
            "link",
            "meta",
            "param",
            "source",
            "track",
            "wbr",
        }:
            self.stack.append(node)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.stack[-1].children.append(_Node(tag, tuple(attrs)))

    def handle_endtag(self, tag: str) -> None:
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                break

    def handle_data(self, data: str) -> None:
        self.stack[-1].children.append(data)

    def handle_entityref(self, name: str) -> None:
        self.handle_data(html.unescape(f"&{name};"))

    def handle_charref(self, name: str) -> None:
        self.handle_data(html.unescape(f"&#{name};"))

    def handle_comment(self, data: str) -> None:
        self.stack[-1].children.append(_Node("#comment", (), [data]))


class _GitLabAPI(Protocol):
    def json_request(
        self, method: str, path: str, body: Mapping[str, object] | None = None
    ) -> Any:
        """Send one GitLab API request and return its decoded JSON reply."""


def _attrs(node: _Node) -> dict[str, str | None]:
    return dict(node.attrs)


def _is_renderer_attribute(
    platform: str, tag: str, name: str, value: str | None
) -> bool:
    if name == "class":
        return (tag, value) in _RENDERER_CLASS_VALUES[platform]
    attributes = RENDERER_ATTRIBUTES_BY_PLATFORM[platform]
    for key in ((tag, name), ("*", name)):
        if key in attributes:
            expected = attributes[key]
            return (
                expected(value)
                if callable(expected)
                else expected is None or expected == value
            )
    return False


def _heading_anchor(node: _Node, parent: _Node | None) -> bool:
    attrs = _attrs(node)
    return (
        parent is not None
        and parent.tag in {f"h{number}" for number in range(1, 7)}
        and parent.children[-1] is node
        and not node.children
        and "anchor" in (attrs.get("class") or "").split()
        and (attrs.get("href") or "").startswith("#")
        and set(attrs) <= {"href", "class", "aria-label", "data-heading-content"}
    )


_CHROME_RULES: tuple[
    tuple[str, str, str, Callable[[_Node, _Node | None], bool]], ...
] = (
    (
        "gitlab",
        "div",
        "unwrap",
        lambda n, p: (
            _attrs(n) == {"class": "gl-relative markdown-code-block js-markdown-code"}
        ),
    ),
    (
        "github",
        "div",
        "unwrap",
        lambda n, p: _attrs(n) == {"class": "highlight highlight-text-adblock"},
    ),
    ("github", "markdown-accessiblity-table", "unwrap", lambda n, p: not n.attrs),
    (
        "gitlab",
        "span",
        "unwrap",
        lambda n, p: (
            _attrs(n).get("class") == "line"
            and re.fullmatch(r"LC[0-9]+", _attrs(n).get("id") or "") is not None
            and set(_attrs(n)) == {"id", "class", "data-lang"}
        ),
    ),
    (
        "gitlab",
        "span",
        "unwrap",
        lambda n, p: (
            _attrs(n).get("data-escaped-char") == ""
            and set(_attrs(n)) == {"data-escaped-char", "data-sourcepos"}
        ),
    ),
    ("gitlab", "copy-code", "drop", lambda n, p: not n.attrs),
    ("gitlab", "insert-code-snippet", "drop", lambda n, p: not n.attrs),
    ("gitlab", "a", "drop", _heading_anchor),
)


def _chrome_action(node: _Node, parent: _Node | None, platform: str) -> str | None:
    return next(
        (
            action
            for scope, tag, action, matches in _CHROME_RULES
            if scope in {"both", platform} and node.tag == tag and matches(node, parent)
        ),
        None,
    )


def _escape_text(text: str) -> str:
    return html.escape(text, quote=False)


def _serialize_parts(
    node: _Node, platform: str, blob_prefix: str | None, parent: _Node | None = None
) -> list[tuple[bool, str]]:
    if node.tag == "#root":
        return _children_parts(node, platform, blob_prefix)
    if node.tag == "#comment":
        data = (
            node.children[0]
            if node.children and isinstance(node.children[0], str)
            else ""
        )
        return [(False, "<!--" + data + "-->")]
    action = _chrome_action(node, parent, platform)
    if action == "drop":
        return []
    if action == "unwrap":
        return _children_parts(node, platform, blob_prefix)
    attrs = _attrs(node)
    if node.tag == "a" and blob_prefix:
        href = attrs.get("href")
        if href == blob_prefix or (href and href.startswith(blob_prefix)):
            attrs["href"] = href[len(blob_prefix) :]
    attrs = {
        key: value
        for key, value in attrs.items()
        if not _is_renderer_attribute(platform, node.tag, key, value)
    }
    rendered_attrs = "".join(
        f" {key}" if value is None else f' {key}="{html.escape(value, quote=True)}"'
        for key, value in sorted(attrs.items())
    )
    if node.tag == "br":
        if rendered_attrs:
            return [(True, " "), (False, f"<br{rendered_attrs}>")]
        return [(True, " ")]
    children = node.children
    if (
        node.tag == "pre"
        and len(children) == 1
        and isinstance(children[0], _Node)
        and children[0].tag == "code"
        and all(
            _is_renderer_attribute(platform, children[0].tag, name, value)
            for name, value in children[0].attrs
        )
    ):
        children = children[0].children
    body = _children_parts(node, platform, blob_prefix, children)
    if node.tag == "pre" and body and body[-1][0]:
        body[-1] = (True, body[-1][1].rstrip(" \t\n\r\f"))
    return [(False, f"<{node.tag}{rendered_attrs}>"), *body, (False, f"</{node.tag}>")]


def _children_parts(
    parent: _Node,
    platform: str,
    blob_prefix: str | None,
    children: Iterable[_Node | str] | None = None,
) -> list[tuple[bool, str]]:
    parts: list[tuple[bool, str]] = []
    for child in parent.children if children is None else children:
        if isinstance(child, _Node):
            parts.extend(_serialize_parts(child, platform, blob_prefix, parent))
        else:
            parts.append((True, _escape_text(child)))
    return parts


def _join_parts(parts: list[tuple[bool, str]], *, trim_root: bool = False) -> str:
    normalized: list[tuple[bool, str]] = []
    for is_text, value in parts:
        if is_text:
            if normalized and normalized[-1][0]:
                normalized[-1] = (
                    True,
                    re.sub(r"[ \t\n\r\f]+", " ", normalized[-1][1] + value),
                )
            elif value:
                normalized.append((True, re.sub(r"[ \t\n\r\f]+", " ", value)))
        else:
            normalized.append((False, value))
    if trim_root and normalized and normalized[0][0]:
        normalized[0] = (True, normalized[0][1].lstrip(" \t\n\r\f"))
    if trim_root and normalized and normalized[-1][0]:
        normalized[-1] = (True, normalized[-1][1].rstrip(" \t\n\r\f"))
    return "".join(value for _, value in normalized if value)


def structure(html_text: str, *, platform: str, blob_prefix: str | None = None) -> str:
    """Return normalized renderer structure while preserving meaningful markup."""
    parser = _TreeParser()
    parser.feed(html_text)
    parser.close()
    if platform not in ELEMENT_VOCABULARIES:
        raise ValueError(f"unknown render platform: {platform}")
    return _join_parts(
        _serialize_parts(parser.root, platform, blob_prefix), trim_root=True
    )


def _walk(node: _Node, parent: _Node | None = None) -> list[tuple[_Node, _Node | None]]:
    result = [(node, parent)]
    for child in node.children:
        if isinstance(child, _Node):
            result.extend(_walk(child, node))
    return result


def check_render(platform: str, html_text: str) -> None:
    """Raise ValueError when a render violates its platform containment rules."""
    if platform not in ELEMENT_VOCABULARIES:
        raise ValueError(f"unknown render platform: {platform}")
    parser = _TreeParser()
    parser.feed(html_text)
    parser.close()
    problems: list[str] = []
    for node, parent in _walk(parser.root):
        if node.tag == "#comment":
            problems.append("comment node")
            continue
        if node.tag == "#root":
            continue
        attrs = _attrs(node)
        if (
            node.tag not in ELEMENT_VOCABULARIES[platform]
            and _chrome_action(node, parent, platform) is None
        ):
            problems.append(f"unknown element <{node.tag}>")
        if platform == "gitlab" and "data-reference-type" in attrs:
            problems.append("GitLab reference link")
        if platform == "github" and node.tag == "a":
            mentions = set((attrs.get("class") or "").split()) & {
                "user-mention",
                "team-mention",
            }
            if mentions:
                problems.append("GitHub mention anchor")
    if problems:
        raise ValueError("; ".join(problems))


class GitLabHTTPError(RuntimeError):
    def __init__(self, status: int, path: str, message: str | None = None):
        detail = f": {message}" if message else ""
        super().__init__(f"GitLab request failed with HTTP {status}: {path}{detail}")
        self.status = status


class GitLabClient:
    def __init__(
        self,
        base_url: str,
        token: str,
        opener: Callable[[Request], Any] = urlopen,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._token = token
        self.opener = opener
        self.sleeper = sleeper

    def request(
        self, method: str, path: str, body: Mapping[str, object] | None = None
    ) -> bytes:
        data = (
            None
            if body is None
            else json.dumps(body, ensure_ascii=False).encode("utf-8")
        )
        request = Request(
            self.base_url + path,
            data=data,
            headers={"PRIVATE-TOKEN": self._token, "Content-Type": "application/json"},
            method=method,
        )
        for attempt in range(4):
            try:
                response = self.opener(request)
                try:
                    status = int(getattr(response, "status", 200))
                    payload = cast(bytes, response.read())
                finally:
                    close = getattr(response, "close", None)
                    if callable(close):
                        close()
            except HTTPError as error:
                status = error.code
                payload = error.read()
                error.close()
            if status == 429 and attempt < 3:
                self.sleeper(min(0.25 * (2**attempt), 2.0))
                continue
            if status >= 400:
                message = _gitlab_error_message(payload, self._token)
                raise GitLabHTTPError(status, path, message)
            return payload
        raise GitLabHTTPError(429, path)

    def json_request(
        self, method: str, path: str, body: Mapping[str, object] | None = None
    ) -> Any:
        payload = self.request(method, path, body)
        return json.loads(payload.decode("utf-8")) if payload else None

    def render(self, text: str, project: str) -> str:
        result = self.json_request(
            "POST",
            "/api/v4/markdown",
            {"text": text, "gfm": True, "project": project},
        )
        html_text = result.get("html") if isinstance(result, dict) else None
        if not isinstance(html_text, str):
            raise ValueError("GitLab markdown response has no html string")
        return html_text


def _gitlab_error_message(payload: bytes, token: str) -> str | None:
    try:
        result = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    message = result.get("message") if isinstance(result, dict) else None
    if not isinstance(message, str):
        return None
    if token:
        message = message.replace(token, "[redacted]")
    return message[:200]


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


def _lookup(client: _GitLabAPI, path: str) -> Any:
    try:
        return client.json_request("GET", path)
    except GitLabHTTPError as error:
        if error.status == 404:
            return None
        raise


def _ensure_group(client: _GitLabAPI, group_path: str) -> dict[str, Any]:
    existing = _lookup(client, "/api/v4/groups/" + quote(group_path, safe=""))
    if isinstance(existing, dict):
        return existing
    if "/" in group_path:
        parent_path, leaf = group_path.rsplit("/", 1)
        parent = _ensure_group(client, parent_path)
    else:
        leaf = group_path
        parent = None
    body: dict[str, object] = {"name": leaf, "path": leaf}
    if parent is not None:
        body["parent_id"] = parent["id"]
    try:
        created = client.json_request("POST", "/api/v4/groups", body)
    except GitLabHTTPError as error:
        if error.status != 409:
            raise
        created = _lookup(client, "/api/v4/groups/" + quote(group_path, safe=""))
    if not isinstance(created, dict):
        raise RuntimeError(f"GitLab did not return group {group_path}")
    return created


def _ensure_user(client: _GitLabAPI, username: str) -> dict[str, Any]:
    lookup_path = "/api/v4/users?username=" + quote(username, safe="")
    existing = _lookup(client, lookup_path)
    if isinstance(existing, list):
        for user in existing:
            if isinstance(user, dict) and user.get("username") == username:
                return user
    body = {
        "name": username,
        "username": username,
        "email": f"{username}@render-probes.invalid",
        "password": secrets.token_urlsafe(32),
        "skip_confirmation": True,
    }
    try:
        created = client.json_request("POST", "/api/v4/users", body)
    except GitLabHTTPError as error:
        if error.status != 409:
            raise
        existing = _lookup(client, lookup_path)
        if isinstance(existing, list):
            for user in existing:
                if isinstance(user, dict) and user.get("username") == username:
                    return user
        raise
    if not isinstance(created, dict):
        raise RuntimeError(f"GitLab did not return user {username}")
    return created


def _ensure_membership(
    client: _GitLabAPI, resource: str, resource_id: int, user_id: int
) -> None:
    path = f"/api/v4/{resource}/{resource_id}/members/{user_id}"
    if _lookup(client, path) is not None:
        return
    try:
        client.json_request(
            "POST",
            f"/api/v4/{resource}/{resource_id}/members",
            {"user_id": user_id, "access_level": 30},
        )
    except GitLabHTTPError as error:
        if error.status != 409:
            raise


def seed(cases: Iterable[Mapping[str, object]], client: _GitLabAPI) -> dict[str, Any]:
    """Idempotently seed the derived users, group chains, project, and memberships."""
    handles = derive_handles(cases) - INHERENTLY_PLAIN.keys()
    group_handles = {handle for handle in handles if "/" in handle} | (
        set(SEED_GROUPS) & handles
    )
    user_names = sorted(handles - group_handles)
    group_paths = {"cdr-group", "team"}
    for group_handle in group_handles:
        segments = group_handle.split("/")
        group_paths.update(
            "/".join(segments[:index]) for index in range(1, len(segments) + 1)
        )
    group_paths.update(SEED_GROUPS)
    groups: dict[str, dict[str, Any]] = {}
    for group_path in sorted(group_paths, key=lambda value: (value.count("/"), value)):
        groups[group_path] = _ensure_group(client, group_path)
    users = {name: _ensure_user(client, name) for name in user_names}
    project_path = "/api/v4/projects/" + quote("cdr-group/probe", safe="")
    project = _lookup(client, project_path)
    if not isinstance(project, dict):
        try:
            project = client.json_request(
                "POST",
                "/api/v4/projects",
                {
                    "name": "probe",
                    "path": "probe",
                    "namespace_id": groups["cdr-group"]["id"],
                    "initialize_with_readme": True,
                },
            )
        except GitLabHTTPError as error:
            if error.status != 409:
                raise
            project = _lookup(client, project_path)
    if not isinstance(project, dict):
        raise RuntimeError("GitLab did not return project cdr-group/probe")
    for user in users.values():
        user_id = int(user["id"])
        for group_path in ("cdr-group", "team"):
            _ensure_membership(client, "groups", int(groups[group_path]["id"]), user_id)
        _ensure_membership(client, "projects", int(project["id"]), user_id)
    return {"groups": groups, "users": users, "project": project}


@dataclass
class RecordResult:
    cases: list[dict[str, Any]]
    changed_ids: list[str] = field(default_factory=list)
    divergence_cleared: list[str] = field(default_factory=list)
    cleared_details: list[str] = field(default_factory=list)
    version_mismatch: tuple[str, str] | None = None
    check_mode: bool = False

    @property
    def exit_code(self) -> int:
        return int(
            self.check_mode
            and bool(
                self.changed_ids or self.divergence_cleared or self.version_mismatch
            )
        )


def input_text(case: Mapping[str, Any]) -> str:
    return f"{case['expected']}\n\nfooter line"


def input_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def build_composed_quick_action_cases() -> list[dict[str, Any]]:
    """Compose deterministic complete GitLab bodies through delivery composers."""
    import scripts.post_review as post_review

    sha = "a" * 40
    keys = ("0123456789abcdef", "fedcba9876543210")
    discussion_marker = post_review._delivery_marker_suffix(sha, [keys[0]])

    def finding(body: str, **values: Any) -> dict[str, Any]:
        return {
            "file": "src/probe.py",
            "line": 1,
            "end_line": 1,
            "severity": "medium",
            "title": "Probe finding",
            "body": body,
            **values,
        }

    def inline_body(
        body_finding: dict[str, Any],
        *,
        surface: str = "discussion",
        marker: str = discussion_marker,
        corroborators: list[dict[str, Any]] | None = None,
        fence_offsets: tuple[int, int] | None = None,
    ) -> str:
        sections = post_review._render_group_sections(
            body_finding, corroborators or [], fence_offsets=fence_offsets
        )
        composed = post_review.compose_inline_body(
            sections, platform="gitlab", surface=surface, marker_suffix=marker
        )
        return cast(str, composed.body + marker)

    cases: list[dict[str, Any]] = []

    def add_inline(case_id: str, text: str, **kwargs: Any) -> None:
        cases.append(
            {
                "id": case_id,
                "text": inline_body(finding(text), **kwargs),
                "route": kwargs.get("surface", "discussion"),
            }
        )

    add_inline("mbq_backtick", ">>>\n```\n>>>\n/close\n```")
    add_inline("mbq_tilde", ">>>\n~~~\n>>>\n/close\n~~~")

    patch_finding = finding(
        ">>>\nSee the patch below.",
        suggested_fix_code=">>>\n/close\nreturn x",
        end_line=3,
    )
    valid_lines = {("src/probe.py", line): None for line in range(1, 4)}
    line_texts = {("src/probe.py", line): f"old line {line}" for line in range(1, 4)}
    patch_range, patch_offsets, cap_exceeded = post_review._gitlab_apply_range(
        patch_finding, 1
    )
    patch_ok, _reason = post_review._fence_verdict(
        patch_finding, patch_range, valid_lines, line_texts
    )
    if cap_exceeded or not patch_ok:
        raise ValueError("deterministic quick-action patch case failed the GitLab gate")
    cases.append(
        {
            "id": "suggested_patch",
            "text": inline_body(patch_finding, fence_offsets=patch_offsets),
            "route": "discussion",
        }
    )

    add_inline("slash_bad_info", "```a`b\n/close")
    cases.append(
        {
            "id": "slash_display_math",
            "text": post_review.compose_review_body(
                "$$\n/close\n$$",
                [],
                platform="gitlab",
                findings_count=1,
                sha=sha,
            ).body,
            "route": "summary",
        }
    )
    add_inline("slash_details", "<details>\n/close\n</details>", surface="note")
    add_inline("trusted_backtick", "```text\n/close\n```")
    add_inline("trusted_tilde", "~~~text\n/close\n~~~")
    add_inline("trusted_four_backticks", "````text\n/close\n````")
    fence = "`" * 3

    def breakout(opener: str) -> str:
        return f"{opener}\n{fence}\n>>>\n/close\n{fence}"

    add_inline("mbq_trailing_space", breakout(">>> "))
    add_inline("mbq_trailing_tab", breakout(">>>\t"))
    add_inline("mbq_four", breakout(">>>>"))
    add_inline("mbq_one_space", breakout(" >>>"))
    add_inline("mbq_container_quote", breakout("> >>>"))
    add_inline("mbq_content_text", ">>> x")
    alert_breakout = breakout(">>> [!note]")
    add_inline("mbq_alert_breakout_discussion", alert_breakout)
    cases.append(
        {
            "id": "mbq_alert_breakout_summary",
            "text": post_review.compose_review_body(
                alert_breakout,
                [],
                platform="gitlab",
                findings_count=1,
                sha=sha,
            ).body,
            "route": "summary",
        }
    )
    alert_patch_finding = finding(
        ">>> [!note]\nSee the patch below.",
        suggested_fix_code=">>>\n/label ~zz377nolabel\nreturn x\n",
        end_line=3,
    )
    alert_patch_range, alert_patch_offsets, alert_cap_exceeded = (
        post_review._gitlab_apply_range(alert_patch_finding, 1)
    )
    alert_patch_ok, _alert_patch_reason = post_review._fence_verdict(
        alert_patch_finding, alert_patch_range, valid_lines, line_texts
    )
    if alert_cap_exceeded or not alert_patch_ok:
        raise ValueError("deterministic alert patch case failed the GitLab gate")
    cases.append(
        {
            "id": "suggestion_alert_payload",
            "text": inline_body(alert_patch_finding, fence_offsets=alert_patch_offsets),
            "route": "discussion",
        }
    )
    add_inline("slash_body_line", "/close")
    cases.append(
        {
            "id": "slash_suggestion_line",
            "text": inline_body(
                finding("Safe body", suggestion="Fix context.\n/close")
            ),
            "route": "discussion",
        }
    )

    primary = finding(
        "Grouped primary", consolidation_key="probe", consolidation_primary=True
    )
    corroborator = finding(
        "<details>\n/close\n</details>",
        agent="probe-agent",
        dimension="correctness",
        confidence="high",
        consolidation_key="probe",
    )
    group = post_review.consolidate_delivery([primary, corroborator])[0]
    group_marker = post_review._delivery_marker_suffix(sha, list(keys))
    cases.append(
        {
            "id": "grouped_corroborator",
            "text": inline_body(
                group["primary"],
                corroborators=group["corroborators"],
                marker=group_marker,
            ),
            "route": "discussion",
        }
    )
    return cases


def build_quick_action_case_list(
    outbound_cases: Iterable[Mapping[str, Any]],
) -> list[dict[str, str]]:
    """Build expected, raw, and freshly composed oracle inputs in stable order."""
    cases: list[dict[str, str]] = []
    for row in outbound_cases:
        row_id = str(row["id"])
        expected = input_text(row)
        raw_input = row.get("input")
        if not isinstance(raw_input, str):
            raise ValueError(f"outbound case {row_id!r} has no string input")
        cases.extend(
            (
                {"id": f"expected:{row_id}", "group": "expected", "text": expected},
                {
                    "id": f"raw:{row_id}",
                    "group": "raw",
                    "text": f"{raw_input}\n\nfooter line",
                },
            )
        )
    cases.extend(
        {"id": f"composed:{case['id']}", "group": "composed", "text": case["text"]}
        for case in build_composed_quick_action_cases()
    )
    suggestion_fence = "`" * 3
    cases.append(
        {
            "id": "raw:suggestion_alert_payload_breakout",
            "group": "raw",
            "text": "\n".join(
                (
                    ">>> [!note]",
                    "See the patch below.",
                    "",
                    f"{suggestion_fence}suggestion",
                    ">>>",
                    "/label ~zz377nolabel",
                    "return x",
                    suggestion_fence,
                )
            ),
        }
    )
    return cases


QUICK_ACTION_SENTINEL = "GITLAB_QUICK_ACTION_VERDICTS_JSON:"
QUICK_ACTION_CONTAINER = "cdr-gitlab"
QUICK_ACTION_GITLAB_VERSION = "19.4.1"


@dataclass
class QuickActionRecordResult:
    document: dict[str, Any]
    changed_ids: list[str] = field(default_factory=list)
    check_mode: bool = False

    @property
    def exit_code(self) -> int:
        return int(self.check_mode and bool(self.changed_ids))


def _quick_action_runner_script(cases: Iterable[Mapping[str, str]]) -> str:
    payload = [
        {"id": case["id"], "group": case["group"], "text": case["text"]}
        for case in cases
    ]
    encoded = base64.b64encode(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).decode("ascii")
    source_path = Path(__file__).with_name("gitlab_quick_action_verdicts.rb")
    source = read_utf8(str(source_path))
    if source.count("__CASES_B64__") != 1:
        raise ValueError(
            "GitLab quick-action runner template has an invalid case marker"
        )
    return source.replace("__CASES_B64__", encoded, 1)


def parse_quick_action_runner_output(stdout: str) -> dict[str, Any]:
    lines = [
        line for line in stdout.splitlines() if line.startswith(QUICK_ACTION_SENTINEL)
    ]
    if len(lines) != 1:
        raise ValueError(
            "GitLab quick-action runner output must contain exactly one sentinel result"
        )
    try:
        result = json.loads(lines[0][len(QUICK_ACTION_SENTINEL) :])
    except json.JSONDecodeError as error:
        raise ValueError("GitLab quick-action runner returned invalid JSON") from error
    if not isinstance(result, dict):
        raise ValueError("GitLab quick-action runner result is not an object")
    return result


def run_gitlab_quick_action_verdicts(
    cases: Iterable[Mapping[str, str]],
    *,
    container: str = QUICK_ACTION_CONTAINER,
    runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
) -> dict[str, Any]:
    if not container or any(character.isspace() for character in container):
        raise ValueError("GitLab container must be a non-empty single argument")
    case_list = list(cases)
    case_ids = [case["id"] for case in case_list]
    if len(case_ids) != len(set(case_ids)):
        raise ValueError("GitLab quick-action case ids must be unique")
    command = ["docker", "exec", "-i", container, "gitlab-rails", "runner", "-"]
    invoke = subprocess.run if runner is None else runner
    completed = invoke(
        command,
        input=_quick_action_runner_script(case_list),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=180,
        check=False,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip()
        raise RuntimeError(
            f"GitLab quick-action runner exited {completed.returncode}"
            + (f": {detail}" if detail else "")
        )
    return parse_quick_action_runner_output(completed.stdout)


def _validate_quick_action_paragraphs(
    text: str, paragraphs: object, case_id: str
) -> list[dict[str, int]]:
    if not isinstance(paragraphs, list):
        raise ValueError(f"invalid paragraphs for GitLab case {case_id!r}")
    line_count = len(text.split("\n"))
    validated: list[dict[str, int]] = []
    for paragraph in paragraphs:
        if (
            not isinstance(paragraph, dict)
            or set(paragraph) != {"start_line", "end_line"}
            or type(paragraph.get("start_line")) is not int
            or type(paragraph.get("end_line")) is not int
        ):
            raise ValueError(f"invalid paragraph interval for GitLab case {case_id!r}")
        start_line = paragraph["start_line"]
        end_line = paragraph["end_line"]
        if not 0 <= start_line <= end_line < line_count:
            raise ValueError(
                f"out-of-bounds paragraph interval for GitLab case {case_id!r}"
            )
        validated.append({"start_line": start_line, "end_line": end_line})
    return validated


def _validated_quick_action_rows(
    source_cases: list[dict[str, str]], result: Mapping[str, Any]
) -> list[dict[str, Any]]:
    version = result.get("gitlab_version")
    if version != QUICK_ACTION_GITLAB_VERSION:
        raise ValueError(
            f"expected GitLab {QUICK_ACTION_GITLAB_VERSION}, got {version!r}"
        )
    raw_rows = result.get("cases")
    if not isinstance(raw_rows, list):
        raise ValueError("GitLab quick-action runner has no cases list")
    if len(raw_rows) != len(source_cases):
        raise ValueError("GitLab quick-action runner returned a different case count")
    rows: list[dict[str, Any]] = []
    for source, raw in zip(source_cases, raw_rows, strict=True):
        if not isinstance(raw, dict):
            raise ValueError("GitLab quick-action verdict is not an object")
        if raw.get("id") != source["id"] or raw.get("group") != source["group"]:
            raise ValueError(
                "GitLab quick-action runner changed case identity or group"
            )
        digest = raw.get("sha256")
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError(f"invalid SHA-256 for GitLab case {source['id']!r}")
        if not isinstance(raw.get("paragraphs"), list):
            raise ValueError(f"invalid paragraphs for GitLab case {source['id']!r}")
        if not isinstance(raw.get("commands"), list):
            raise ValueError(f"invalid commands for GitLab case {source['id']!r}")
        paragraphs = _validate_quick_action_paragraphs(
            source["text"], raw["paragraphs"], source["id"]
        )
        stored_equals_posted = raw.get("stored_equals_posted")
        if type(stored_equals_posted) is not bool:
            raise ValueError(
                f"invalid stored-body comparison for GitLab case {source['id']!r}"
            )
        normalized_posted = source["text"].replace("\r", "").rstrip()
        has_content = "content" in raw
        content = raw.get("content", normalized_posted)
        if not isinstance(content, str):
            raise ValueError(
                f"invalid Extractor content for GitLab case {source['id']!r}"
            )
        if stored_equals_posted != (content == normalized_posted):
            raise ValueError(
                f"inconsistent Extractor content for GitLab case {source['id']!r}"
            )
        if has_content == (content == normalized_posted):
            raise ValueError(
                f"Extractor content must be recorded only when changed for GitLab case {source['id']!r}"
            )
        row = {
            "id": source["id"],
            "group": source["group"],
            "sha256": digest,
            "paragraphs": paragraphs,
            "commands": raw["commands"],
            "stored_equals_posted": stored_equals_posted,
        }
        if has_content:
            row["content"] = content
        rows.append(row)
    return rows


def serialize_quick_action_fixture(document: Mapping[str, Any]) -> str:
    rows = [
        "    " + json.dumps(case, ensure_ascii=False, separators=(",", ":"))
        for case in document["cases"]
    ]
    return (
        "{\n"
        f'  "gitlab_version": {json.dumps(document["gitlab_version"])},\n'
        f'  "recorded": {json.dumps(document["recorded"])},\n'
        '  "cases": [\n' + ",\n".join(rows) + "\n  ]\n}\n"
    )


def _quick_action_changed_ids(old: object, new: Mapping[str, Any]) -> list[str]:
    if not isinstance(old, dict) or not isinstance(old.get("cases"), list):
        return ["fixture"]
    changed: list[str] = []
    if old.get("gitlab_version") != new["gitlab_version"]:
        changed.append("gitlab_version")
    old_rows = [row for row in old["cases"] if isinstance(row, dict)]
    if len(old_rows) != len(old["cases"]):
        return [*changed, "fixture"]
    old_by_id = {str(row.get("id")): row for row in old_rows}
    new_rows = new["cases"]
    new_ids = [str(row["id"]) for row in new_rows]
    old_ids = [str(row.get("id")) for row in old_rows]
    for row in new_rows:
        case_id = str(row["id"])
        if old_by_id.get(case_id) != row:
            changed.append(case_id)
    changed.extend(case_id for case_id in old_ids if case_id not in set(new_ids))
    if old_ids != new_ids and not changed:
        changed.append("case-order")
    return changed


def record_quick_action_fixture(
    path: str,
    outbound_cases: Iterable[Mapping[str, Any]],
    *,
    container: str = QUICK_ACTION_CONTAINER,
    runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
    clock: Callable[[], str] = lambda: date.today().isoformat(),
    read_text: Callable[[str], str] | None = None,
    write_text: Callable[[str, str], None] | None = None,
    check: bool = False,
) -> QuickActionRecordResult:
    read_fixture = read_utf8 if read_text is None else read_text
    write_fixture = write_utf8 if write_text is None else write_text
    cases = build_quick_action_case_list(outbound_cases)
    result = run_gitlab_quick_action_verdicts(cases, container=container, runner=runner)
    document = {
        "gitlab_version": QUICK_ACTION_GITLAB_VERSION,
        "recorded": clock(),
        "cases": _validated_quick_action_rows(cases, result),
    }
    if check:
        try:
            old = json.loads(read_fixture(path))
        except FileNotFoundError:
            changed_ids = ["fixture"]
        except json.JSONDecodeError:
            changed_ids = ["fixture"]
        else:
            changed_ids = _quick_action_changed_ids(old, document)
        return QuickActionRecordResult(document, changed_ids, True)
    write_fixture(path, serialize_quick_action_fixture(document))
    return QuickActionRecordResult(document)


def _reference_originals(html_text: str) -> list[str]:
    parser = _TreeParser()
    parser.feed(html_text)
    parser.close()
    originals = []
    for node, _ in _walk(parser.root):
        if node.tag == "a" and "data-reference-type" in _attrs(node):
            value = _attrs(node).get("data-original")
            if isinstance(value, str):
                originals.append(value)
    return sorted(originals)


class _TagCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.tags: list[tuple[str, tuple[tuple[str, str | None], ...]]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tags.append((tag, tuple(sorted(attrs))))

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tags.append((tag, tuple(sorted(attrs))))

    def handle_endtag(self, tag: str) -> None:
        self.tags.append(("/" + tag, ()))


def _skeleton(
    html_text: str, *, platform: str, blob_prefix: str | None = None
) -> tuple[tuple[str, tuple[tuple[str, str | None], ...]], ...]:
    collector = _TagCollector()
    collector.feed(structure(html_text, platform=platform, blob_prefix=blob_prefix))
    collector.close()
    return tuple(collector.tags)


def pair_sha256(
    github_html: str, gitlab_html: str, *, blob_prefix: str | None = None
) -> str:
    pair = (
        structure(github_html, platform="github")
        + "\0"
        + structure(gitlab_html, platform="gitlab", blob_prefix=blob_prefix)
    )
    return hashlib.sha256(pair.encode("utf-8")).hexdigest()


def update_cases(
    cases: list[dict[str, Any]],
    *,
    platform: str,
    render: Callable[[str], str],
    rendered: str,
    version: str | None = None,
    blob_prefix: str | None = None,
    check: bool = False,
) -> RecordResult:
    if platform not in {"github", "gitlab"}:
        raise ValueError(f"unknown render platform: {platform}")
    updated = copy.deepcopy(cases)
    changed: list[str] = []
    cleared: list[str] = []
    cleared_details: list[str] = []
    version_mismatch = None
    for case in updated:
        text = input_text(case)
        digest = input_sha256(text)
        expected_html = render(text)
        check_render(platform, expected_html)
        if platform == "github":
            probe: dict[str, Any] = {
                "renderer": "gh api markdown mode=gfm",
                "rendered": rendered,
                "input": "expected + blank line + footer line",
                "input_sha256": digest,
                "html": expected_html,
            }
            old = case.get("github_probe")
            if check and _probe_changed(
                old, probe, ("renderer", "input", "input_sha256", "html")
            ):
                changed.append(str(case["id"]))
            if not check:
                case["github_probe"] = probe
        else:
            if version is None or blob_prefix is None:
                raise ValueError("GitLab recording requires version and blob_prefix")
            old = case.get("gitlab_probe")
            old_divergence = old.get("divergence") if isinstance(old, dict) else None
            twin_html = render(text.replace("\uff20", "@"))
            probe = {
                "renderer": "gitlab api markdown gfm=true project",
                "version": version,
                "rendered": rendered,
                "input": "expected + blank line + footer line",
                "input_sha256": digest,
                "blob_prefix": blob_prefix,
                "twin_references": _reference_originals(twin_html),
                "html": expected_html,
                "divergence": old_divergence,
            }
            if check:
                compared = (
                    "renderer",
                    "input",
                    "input_sha256",
                    "blob_prefix",
                    "twin_references",
                    "html",
                )
                if _probe_changed(old, probe, compared):
                    changed.append(str(case["id"]))
                if isinstance(old, dict) and old.get("version") != version:
                    version_mismatch = (str(old.get("version", "missing")), version)
            else:
                _insert_after(case, "github_probe", "gitlab_probe", probe)
        github_probe = case.get("github_probe")
        gitlab_probe = case.get("gitlab_probe")
        divergence = (
            gitlab_probe.get("divergence") if isinstance(gitlab_probe, dict) else None
        )
        if divergence is not None:
            if isinstance(github_probe, dict) and isinstance(gitlab_probe, dict):
                gh_html = (
                    probe["html"]
                    if platform == "github"
                    else github_probe.get("html", "")
                )
                gl_html = (
                    probe["html"]
                    if platform == "gitlab"
                    else gitlab_probe.get("html", "")
                )
                prefix = (
                    blob_prefix
                    if platform == "gitlab"
                    else gitlab_probe.get("blob_prefix")
                )
                gh_structure = structure(gh_html, platform="github")
                gl_structure = structure(gl_html, platform="gitlab", blob_prefix=prefix)
                valid = (
                    isinstance(divergence, dict)
                    and isinstance(divergence.get("note"), str)
                    and type(divergence.get("issue")) is int
                    and divergence.get("pair_sha256")
                    == pair_sha256(gh_html, gl_html, blob_prefix=prefix)
                    and gh_structure != gl_structure
                    and _skeleton(gh_html, platform="github")
                    == _skeleton(gl_html, platform="gitlab", blob_prefix=prefix)
                )
            else:
                valid = False
            if not valid:
                if check:
                    cleared.append(str(case["id"]))
                elif isinstance(gitlab_probe, dict):
                    gitlab_probe["divergence"] = None
                    cleared.append(str(case["id"]))
                    if isinstance(divergence, dict):
                        cleared_details.append(
                            f"{case['id']}: issue {divergence.get('issue')}: {divergence.get('note')}"
                        )
    return RecordResult(
        updated, changed, cleared, cleared_details, version_mismatch, check
    )


def _probe_changed(
    old: object, new: Mapping[str, Any], fields: tuple[str, ...]
) -> bool:
    return not isinstance(old, dict) or any(
        old.get(field) != new.get(field) for field in fields
    )


def _insert_after(case: dict[str, Any], anchor: str, key: str, value: Any) -> None:
    reordered: dict[str, Any] = {}
    for current_key, current_value in case.items():
        if current_key != key:
            reordered[current_key] = current_value
        if current_key == anchor:
            reordered[key] = value
    if key not in reordered:
        reordered[key] = value
    case.clear()
    case.update(reordered)


def serialize_fixture(cases: Iterable[Mapping[str, Any]]) -> str:
    rows = [
        "    " + json.dumps(case, ensure_ascii=False, separators=(",", ":"))
        for case in cases
    ]
    return '{\n  "cases": [\n' + ",\n".join(rows) + "\n  ]\n}\n"


def read_utf8(path: str) -> str:
    with open(path, encoding="utf-8", newline="") as stream:
        return stream.read()


def write_utf8(path: str, contents: str) -> None:
    with open(path, "w", encoding="utf-8", newline="") as stream:
        stream.write(contents)


def record_fixture(
    path: str,
    *,
    platform: str,
    render: Callable[[str], str],
    clock: Callable[[], str] = lambda: date.today().isoformat(),
    read_text: Callable[[str], str] = read_utf8,
    write_text: Callable[[str, str], None] = write_utf8,
    version: str | None = None,
    blob_prefix: str | None = None,
    check: bool = False,
    get_github_login: Callable[[], str] = github_login,
) -> RecordResult:
    document = _load_document(read_text(path))
    cases = document["cases"]
    if platform == "gitlab":
        run_canary(derive_handles(cases), render)
    elif platform == "github":
        run_github_canary(get_github_login(), render)
    result = update_cases(
        cases,
        platform=platform,
        render=render,
        rendered=clock(),
        version=version,
        blob_prefix=blob_prefix,
        check=check,
    )
    if not check:
        write_text(path, serialize_fixture(result.cases))
    return result


def _load_document(contents: str) -> dict[str, Any]:
    document = json.loads(contents)
    if (
        not isinstance(document, dict)
        or list(document) != ["cases"]
        or not isinstance(document["cases"], list)
        or not all(isinstance(case, dict) for case in document["cases"])
    ):
        raise ValueError("fixture must contain only a cases array")
    return document


def record_divergence(
    path: str,
    *,
    case_id: str,
    issue: int,
    note: str,
    read_text: Callable[[str], str] = read_utf8,
    write_text: Callable[[str, str], None] = write_utf8,
) -> None:
    document = _load_document(read_text(path))
    if issue < 1 or not note.strip():
        raise ValueError("divergence needs a positive issue and nonempty note")
    matches = [case for case in document["cases"] if case.get("id") == case_id]
    if len(matches) != 1:
        raise ValueError(f"expected one fixture row for {case_id}")
    case = matches[0]
    github = case["github_probe"]
    gitlab = case["gitlab_probe"]
    digest = input_sha256(input_text(case))
    if any(probe["input_sha256"] != digest for probe in (github, gitlab)):
        raise ValueError(f"stale probe input for {case_id}")
    gh_html, gl_html = github["html"], gitlab["html"]
    prefix = gitlab["blob_prefix"]
    if structure(gh_html, platform="github") == structure(
        gl_html, platform="gitlab", blob_prefix=prefix
    ):
        raise ValueError(f"equal structures for {case_id}")
    if _skeleton(gh_html, platform="github") != _skeleton(
        gl_html, platform="gitlab", blob_prefix=prefix
    ):
        raise ValueError(f"different element skeletons for {case_id}")
    gitlab["divergence"] = {
        "note": note,
        "issue": issue,
        "pair_sha256": pair_sha256(gh_html, gl_html, blob_prefix=prefix),
    }
    write_text(path, serialize_fixture(document["cases"]))


def derive_handles(
    cases: Iterable[Mapping[str, object]], *, fullwidth_only: bool = False
) -> set[str]:
    """Collect GitLab reference handles from each case's input and expected text."""
    handles: set[str] = set()
    for case in cases:
        for field_name in ("input", "expected"):
            value = case.get(field_name)
            if isinstance(value, str):
                for match in _HANDLE_PATTERN.finditer(value):
                    if fullwidth_only and (
                        field_name != "expected" or value[match.start()] != "\uff20"
                    ):
                        continue
                    handle = match.group(1)
                    suffix = value[match.end(1) :]
                    continues_handle = bool(
                        suffix
                        and (
                            (
                                suffix[0].isascii()
                                and (suffix[0].isalnum() or suffix[0] in "_-")
                            )
                            or (
                                suffix.startswith("/")
                                and len(suffix) > 1
                                and suffix[1]
                                in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_."
                            )
                        )
                    )
                    if (
                        not continues_handle
                        and not handle.endswith((".git", ".atom"))
                        and (not fullwidth_only or handle not in INHERENTLY_PLAIN)
                    ):
                        handles.add(handle)
    return handles


def _has_reference_anchor(html_text: str, handle: str) -> bool:
    parser = _TreeParser()
    parser.feed(html_text)
    parser.close()
    return any(
        node.tag == "a"
        and "data-reference-type" in _attrs(node)
        and _attrs(node).get("data-original") == "@" + handle
        for node, _ in _walk(parser.root)
    )


def run_canary(handles: Iterable[str], render: Callable[[str], str]) -> None:
    """Require the renderer to link every seedable derived handle."""
    unresolved = [
        handle
        for handle in sorted(set(handles) - INHERENTLY_PLAIN.keys())
        if not _has_reference_anchor(render(f"@{handle}"), handle)
    ]
    if unresolved:
        raise RuntimeError(
            "GitLab reference canary unresolved handles: " + ", ".join(unresolved)
        )


def _client_from_environment(base_url: str, token_env: str) -> GitLabClient:
    token = os.environ.get(token_env)
    if not token:
        raise ValueError(f"environment variable {token_env} is not set")
    return GitLabClient(base_url, token)


def _fixture_path() -> str:
    return str(
        Path(__file__).resolve().parents[1] / "fixtures" / "outbound_comment_cases.json"
    )


def _quick_action_fixture_path() -> str:
    return str(
        Path(__file__).resolve().parents[1]
        / "fixtures"
        / "gitlab_quick_action_verdicts_19_4_1.json"
    )


def _read_cases(path: str) -> list[dict[str, Any]]:
    document = _load_document(read_utf8(path))
    return cast(list[dict[str, Any]], document["cases"])


def _gitlab_version(client: _GitLabAPI) -> str:
    details = client.json_request("GET", "/api/v4/version")
    if not isinstance(details, dict):
        raise ValueError("GitLab version response is not an object")
    match = re.match(r"(\d+\.\d+\.\d+)(?:-([a-z]+))?", str(details.get("version", "")))
    if not match:
        raise ValueError("GitLab version response has no semantic version")
    edition = "ee" if details.get("enterprise") or match.group(2) == "ee" else "ce"
    return f"{match.group(1)} {edition}"


def _gitlab_blob_prefix(client: GitLabClient, project: str) -> str:
    details = client.json_request("GET", "/api/v4/projects/" + quote(project, safe=""))
    if (
        not isinstance(details, dict)
        or not details.get("web_url")
        or not details.get("default_branch")
    ):
        raise ValueError(f"GitLab project metadata is incomplete for {project}")
    return f"{str(details['web_url']).rstrip('/')}/-/blob/{details['default_branch']}/"


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Record Markdown renderer probes.")
    commands = parser.add_subparsers(dest="command", required=True)
    seed_parser = commands.add_parser(
        "seed", help="seed the local GitLab render project"
    )
    seed_parser.add_argument("--base-url", default="http://localhost:8929")
    seed_parser.add_argument("--token-env", default="GITLAB_TOKEN")
    record_parser = commands.add_parser(
        "record", help="record or check a renderer probe"
    )
    record_parser.add_argument(
        "--platform", choices=("github", "gitlab"), required=True
    )
    record_parser.add_argument("--check", action="store_true")
    record_parser.add_argument("--base-url", default="http://localhost:8929")
    record_parser.add_argument("--token-env", default="GITLAB_TOKEN")
    record_parser.add_argument("--project", default="cdr-group/probe")
    quick_actions_parser = commands.add_parser(
        "quick-actions", help="record or check GitLab quick-action verdicts"
    )
    quick_actions_parser.add_argument(
        "action", choices=("record",), nargs="?", default="record"
    )
    quick_actions_parser.add_argument("--check", action="store_true")
    quick_actions_parser.add_argument("--container", default=QUICK_ACTION_CONTAINER)
    divergence_parser = commands.add_parser(
        "divergence", help="bind a text-only render difference"
    )
    divergence_parser.add_argument("--id", required=True)
    divergence_parser.add_argument("--issue", required=True, type=int)
    divergence_parser.add_argument("--note", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_argument_parser().parse_args(argv)
    path = _fixture_path()
    if args.command == "divergence":
        record_divergence(path, case_id=args.id, issue=args.issue, note=args.note)
        return 0
    if args.command == "seed":
        client = _client_from_environment(args.base_url, args.token_env)
        seeded = seed(_read_cases(path), client)
        print(f"seeded {len(seeded['users'])} users and {len(seeded['groups'])} groups")
        return 0
    if args.command == "quick-actions":
        quick_result = record_quick_action_fixture(
            _quick_action_fixture_path(),
            _read_cases(path),
            container=args.container,
            check=args.check,
        )
        if args.check:
            if quick_result.changed_ids:
                for case_id in quick_result.changed_ids:
                    print(case_id)
            else:
                print("GitLab quick-action verdict fixture matches")
        else:
            print(
                f"recorded {len(quick_result.document['cases'])} GitLab quick-action verdicts"
            )
        return quick_result.exit_code
    if args.platform == "github":
        result = record_fixture(
            path,
            platform="github",
            render=render_github,
            check=args.check,
            get_github_login=github_login,
        )
    else:
        client = _client_from_environment(args.base_url, args.token_env)

        def render(text: str) -> str:
            return client.render(text, args.project)

        version = _gitlab_version(client)
        blob_prefix = _gitlab_blob_prefix(client, args.project)
        result = record_fixture(
            path,
            platform="gitlab",
            render=render,
            version=version,
            blob_prefix=blob_prefix,
            check=args.check,
        )
    for case_id in result.changed_ids:
        print(case_id)
    for detail in result.cleared_details:
        print(f"cleared divergence: {detail}")
    if args.check:
        for case_id in result.divergence_cleared:
            print(f"stale divergence: {case_id}")
    if result.version_mismatch:
        before, after = result.version_mismatch
        print(f"version mismatch: {before} -> {after}")
    return result.exit_code


if __name__ == "__main__":
    # The composed-case builder imports scripts.post_review from the repository root.
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    raise SystemExit(main())

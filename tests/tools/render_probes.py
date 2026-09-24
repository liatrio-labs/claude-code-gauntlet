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
import copy
import hashlib
import html
import json
import os
import re
import secrets
import subprocess
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Protocol, cast
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen

_GL_CODE_BLOCK_CLASS = "gl-relative markdown-code-block js-markdown-code"
_GH_HIGHLIGHT_CLASS = "highlight highlight-text-adblock"
RENDERER_ATTRIBUTES_BY_PLATFORM = {
    "gitlab": frozenset(
        {
            "data-sourcepos",
            "dir",
            "data-canonical-src",
            "data-canonical-lang",
            "data-lang-params",
            "v-pre",
            "data-lang",
            "data-escaped-char",
            "data-heading-content",
            "rel",
            "target",
            "class",
        }
    ),
    "github": frozenset({"role", "data-meta", "lang", "rel", "class"}),
}
_RENDERER_ATTRIBUTE_NAMES = frozenset().union(
    *RENDERER_ATTRIBUTES_BY_PLATFORM.values()
) - {"class"}
_RENDERER_ATTRIBUTE_VALUES = {
    ("a", "class", "gfm"),
    ("pre", "class", "code highlight js-syntax-highlight language-plaintext"),
    ("pre", "class", "notranslate"),
    ("code", "class", "notranslate"),
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
            "copy-code",
            "div",
            "h1",
            "h2",
            "insert-code-snippet",
            "li",
            "ol",
            "p",
            "pre",
            "span",
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
            "div",
            "h1",
            "h2",
            "li",
            "markdown-accessiblity-table",
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
    ) -> Any: ...


def _attrs(node: _Node) -> dict[str, str | None]:
    return dict(node.attrs)


def _is_renderer_attribute(tag: str, name: str, value: str | None) -> bool:
    return (
        name in _RENDERER_ATTRIBUTE_NAMES
        or (tag, name, value) in _RENDERER_ATTRIBUTE_VALUES
    )


def _is_wrapper(node: _Node) -> bool:
    attrs = _attrs(node)
    if node.tag == "div" and attrs == {"class": _GL_CODE_BLOCK_CLASS}:
        return True
    if node.tag == "div" and attrs == {"class": _GH_HIGHLIGHT_CLASS}:
        return True
    if (
        node.tag == "div"
        and not attrs
        and len(node.children) == 1
        and isinstance(node.children[0], _Node)
        and node.children[0].tag == "pre"
    ):
        return True
    if node.tag == "markdown-accessiblity-table" and not attrs:
        return True
    if (
        node.tag == "span"
        and attrs.get("class") == "line"
        and re.fullmatch(r"LC[0-9]+", attrs.get("id") or "")
        and set(attrs) == {"id", "class", "data-lang"}
    ):
        return True
    return (
        node.tag == "span"
        and attrs.get("data-escaped-char") == ""
        and set(attrs) == {"data-escaped-char", "data-sourcepos"}
    )


def _is_heading_anchor(node: _Node) -> bool:
    attrs = _attrs(node)
    return (
        node.tag == "a"
        and attrs.get("class") == "anchor"
        and (attrs.get("href") or "").startswith("#")
        and set(attrs) <= {"href", "class", "aria-label", "data-heading-content"}
    )


def _escape_text(text: str) -> str:
    return html.escape(text, quote=False)


def _serialize_parts(node: _Node, blob_prefix: str | None) -> list[tuple[bool, str]]:
    if node.tag == "#root":
        return _children_parts(node.children, blob_prefix)
    if node.tag == "#comment":
        data = (
            node.children[0]
            if node.children and isinstance(node.children[0], str)
            else ""
        )
        return [(False, "<!--" + data + "-->")]
    if node.tag in {"copy-code", "insert-code-snippet"} or _is_heading_anchor(node):
        return []
    if _is_wrapper(node):
        return _children_parts(node.children, blob_prefix)
    attrs = _attrs(node)
    if node.tag == "a" and blob_prefix:
        href = attrs.get("href")
        if href == blob_prefix or (href and href.startswith(blob_prefix)):
            attrs["href"] = href[len(blob_prefix) :]
    if node.tag in {"h1", "h2"} and (attrs.get("id") or "").startswith("user-content-"):
        attrs.pop("id", None)
    attrs = {
        key: value
        for key, value in attrs.items()
        if not _is_renderer_attribute(node.tag, key, value)
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
            _is_renderer_attribute(children[0].tag, name, value)
            for name, value in children[0].attrs
        )
    ):
        children = children[0].children
    body = _children_parts(children, blob_prefix)
    if node.tag == "pre" and body and body[-1][0]:
        body[-1] = (True, body[-1][1].rstrip())
    return [(False, f"<{node.tag}{rendered_attrs}>"), *body, (False, f"</{node.tag}>")]


def _children_parts(
    children: Iterable[_Node | str], blob_prefix: str | None
) -> list[tuple[bool, str]]:
    parts: list[tuple[bool, str]] = []
    for child in children:
        if isinstance(child, _Node):
            parts.extend(_serialize_parts(child, blob_prefix))
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
                    re.sub(r"\s+", " ", normalized[-1][1] + value),
                )
            elif value:
                normalized.append((True, re.sub(r"\s+", " ", value)))
        else:
            normalized.append((False, value))
    if trim_root and normalized and normalized[0][0]:
        normalized[0] = (True, normalized[0][1].lstrip())
    if trim_root and normalized and normalized[-1][0]:
        normalized[-1] = (True, normalized[-1][1].rstrip())
    return "".join(value for _, value in normalized if value)


def structure(html_text: str, *, blob_prefix: str | None = None) -> str:
    """Return normalized renderer structure while preserving meaningful markup."""
    parser = _TreeParser()
    parser.feed(html_text)
    parser.close()
    return _join_parts(_serialize_parts(parser.root, blob_prefix), trim_root=True)


def _walk(node: _Node) -> list[_Node]:
    result = [node]
    for child in node.children:
        if isinstance(child, _Node):
            result.extend(_walk(child))
    return result


def check_render(platform: str, html_text: str) -> None:
    """Raise ValueError when a render violates its platform containment rules."""
    if platform not in ELEMENT_VOCABULARIES:
        raise ValueError(f"unknown render platform: {platform}")
    parser = _TreeParser()
    parser.feed(html_text)
    parser.close()
    problems: list[str] = []
    for node in _walk(parser.root):
        if node.tag == "#comment":
            problems.append("comment node")
            continue
        if node.tag == "#root":
            continue
        attrs = _attrs(node)
        if node.tag not in ELEMENT_VOCABULARIES[platform]:
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
        create_path = "/api/v4/groups"
    else:
        leaf = group_path
        parent = None
        create_path = "/api/v4/groups"
    body: dict[str, object] = {"name": leaf, "path": leaf}
    if parent is not None:
        body["parent_id"] = parent["id"]
    try:
        created = client.json_request("POST", create_path, body)
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


def _reference_originals(html_text: str) -> list[str]:
    parser = _TreeParser()
    parser.feed(html_text)
    parser.close()
    originals = []
    for node in _walk(parser.root):
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


def _skeleton(
    html_text: str, blob_prefix: str | None = None
) -> tuple[tuple[str, tuple[tuple[str, str | None], ...]], ...]:
    collector = _TagCollector()
    collector.feed(structure(html_text, blob_prefix=blob_prefix))
    collector.close()
    return tuple(collector.tags)


def pair_sha256(
    github_html: str, gitlab_html: str, *, blob_prefix: str | None = None
) -> str:
    pair = (
        structure(github_html) + "\0" + structure(gitlab_html, blob_prefix=blob_prefix)
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
                gh_structure = structure(gh_html)
                gl_structure = structure(gl_html, blob_prefix=prefix)
                valid = (
                    isinstance(divergence, dict)
                    and isinstance(divergence.get("note"), str)
                    and type(divergence.get("issue")) is int
                    and divergence.get("pair_sha256")
                    == pair_sha256(gh_html, gl_html, blob_prefix=prefix)
                    and gh_structure != gl_structure
                    and _skeleton(gh_html) == _skeleton(gl_html, prefix)
                )
            else:
                valid = False
            if not valid:
                if check:
                    cleared.append(str(case["id"]))
                elif isinstance(gitlab_probe, dict):
                    gitlab_probe["divergence"] = None
                    cleared.append(str(case["id"]))
    return RecordResult(updated, changed, cleared, version_mismatch, check)


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
    canary: bool = True,
) -> RecordResult:
    document = json.loads(read_text(path))
    if not isinstance(document, dict) or not isinstance(document.get("cases"), list):
        raise ValueError("fixture must be an object with a cases array")
    cases = document["cases"]
    if platform == "gitlab" and canary:
        run_canary(derive_handles(cases), render)
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


def derive_handles(cases: Iterable[Mapping[str, object]]) -> set[str]:
    """Collect GitLab reference handles from each case's input and expected text."""
    handles: set[str] = set()
    for case in cases:
        for field_name in ("input", "expected"):
            value = case.get(field_name)
            if isinstance(value, str):
                for match in _HANDLE_PATTERN.finditer(value):
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
                    if not continues_handle and not handle.endswith((".git", ".atom")):
                        handles.add(handle)
    return handles


def _has_reference_anchor(html_text: str) -> bool:
    parser = _TreeParser()
    parser.feed(html_text)
    parser.close()
    return any(
        node.tag == "a" and "data-reference-type" in _attrs(node)
        for node in _walk(parser.root)
    )


def run_canary(handles: Iterable[str], render: Callable[[str], str]) -> None:
    """Require the renderer to link every seedable derived handle."""
    unresolved = [
        handle
        for handle in sorted(set(handles) - INHERENTLY_PLAIN.keys())
        if not _has_reference_anchor(render(f"@{handle}"))
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


def _read_cases(path: str) -> list[dict[str, Any]]:
    document = json.loads(read_utf8(path))
    if (
        not isinstance(document, dict)
        or not isinstance(document.get("cases"), list)
        or not all(isinstance(case, dict) for case in document["cases"])
    ):
        raise ValueError("fixture must be an object with a cases array")
    return cast(list[dict[str, Any]], document["cases"])


def _gitlab_version(client: GitLabClient) -> str:
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
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_argument_parser().parse_args(argv)
    path = _fixture_path()
    if args.command == "seed":
        client = _client_from_environment(args.base_url, args.token_env)
        seeded = seed(_read_cases(path), client)
        print(f"seeded {len(seeded['users'])} users and {len(seeded['groups'])} groups")
        return 0
    if args.platform == "github":
        result = record_fixture(
            path, platform="github", render=render_github, check=args.check
        )
    else:
        client = _client_from_environment(args.base_url, args.token_env)

        def render(text: str) -> str:
            return client.render(text, args.project)

        run_canary(derive_handles(_read_cases(path)), render)
        version = _gitlab_version(client)
        blob_prefix = _gitlab_blob_prefix(client, args.project)
        result = record_fixture(
            path,
            platform="gitlab",
            render=render,
            version=version,
            blob_prefix=blob_prefix,
            check=args.check,
            canary=False,
        )
    for case_id in result.changed_ids:
        print(case_id)
    for case_id in result.divergence_cleared:
        print(f"cleared divergence: {case_id}")
    if result.version_mismatch:
        before, after = result.version_mismatch
        print(f"version mismatch: {before} -> {after}")
    return result.exit_code


if __name__ == "__main__":
    raise SystemExit(main())

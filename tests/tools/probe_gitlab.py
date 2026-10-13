from __future__ import annotations

import json
import os
import re
import secrets
import time
from collections.abc import Callable, Iterable, Mapping
from typing import Any, Protocol, cast
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen

from tests.tools.probe_html import (
    _attrs,
    _TreeParser,
    _walk,
)

INHERENTLY_PLAIN = {
    "all": "GitLab 19.4.1 renders @all as plain text",
    "x": "GitLab refuses one-character usernames",
}


SEED_GROUPS = ("team",)


_SEGMENT = r"(?:[a-zA-Z0-9_.][a-zA-Z0-9_\-.]{0,254}[a-zA-Z0-9_\-]|[a-zA-Z0-9_])"


_HANDLE_PATTERN = re.compile(
    rf"(?<![A-Za-z0-9_])[@\uff20]({_SEGMENT}(?:/{_SEGMENT}){{0,19}})"
)


class _GitLabAPI(Protocol):
    def json_request(
        self, method: str, path: str, body: Mapping[str, object] | None = None
    ) -> Any:
        """Send one GitLab API request and return its decoded JSON reply."""


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

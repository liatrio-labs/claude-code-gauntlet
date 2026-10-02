"""Forge targets, URL policies and semantic review requests."""

from __future__ import annotations

import json
import os
import re
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from ipaddress import IPv6Address
from typing import ClassVar, Literal, Protocol
from urllib.parse import urlsplit

from gauntlet import proc

Platform = Literal["github", "gitlab"]


@dataclass(frozen=True, slots=True)
class ReviewTarget:
    owner: str
    repo: str
    number: int | str


@dataclass(frozen=True, slots=True)
class Remote:
    hostname: str | None
    path: str
    scheme: str | None


@dataclass(frozen=True, slots=True)
class RepoSlug:
    owner: str
    repo: str


@dataclass(frozen=True, slots=True)
class ParsedPrUrl:
    owner: str
    repo: str
    number: int
    web_origin: str


@dataclass(frozen=True, slots=True)
class PlatformDetection:
    platform: Platform | None
    host: str | None


@dataclass(frozen=True, slots=True)
class JsonFetch:
    payload: object
    error: str | None


@dataclass(frozen=True, slots=True)
class PostRequest:
    platform: Platform
    endpoint: str
    method: str
    headers: tuple[str, ...]
    payload: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class PostResult:
    response: object
    error: str | None
    warning: str | None


class Forge(Protocol):
    platform: Platform

    def ensure_available(self) -> None: ...
    def diff(self, target: ReviewTarget) -> tuple[str, str, int]: ...
    def review_entries(self, target: ReviewTarget) -> JsonFetch: ...
    def submit(self, request: PostRequest) -> PostResult: ...


_DNS_LABEL_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")
_SSH_AUTHORITY_RE = re.compile(
    r"(?:[A-Za-z0-9._-]+@)?(?:[A-Za-z0-9.-]+|\[[0-9A-Fa-f:.]+\])(?::[0-9]+)?"
)
_FETCH_TIMEOUT_SECONDS = 30
_SCP_PATH_RE = re.compile(r"[^@/]+@[^:/]+:(.+?)(?:\.git)?/?$")
_URL_PATH_RE = re.compile(
    r"[a-zA-Z][a-zA-Z0-9+.-]*://(?:[^@/]+@)?[^/]+/(.+?)(?:\.git)?/?$"
)
_PR_PATHS = {
    "github": re.compile(r"^/([^/]+)/([^/]+)/pull/([1-9][0-9]*)/?$"),
    "gitlab": re.compile(r"^/(.+)/([^/]+)/-/merge_requests/([1-9][0-9]*)/?$"),
}


def _remote_hostname(authority: str, *, ssh: bool) -> str | None:
    # Git percent-decodes ssh:// URLs before splitting the authority, so escapes are unsafe.
    if ssh and not _SSH_AUTHORITY_RE.fullmatch(authority):
        return None
    # urlsplit discards tabs/newlines, so inspect the original web authority first.
    if not ssh and any(ord(char) < 32 or char.isspace() for char in authority):
        return None
    host_port = authority.rsplit("@", 1)[-1]
    try:
        parsed = urlsplit(f"ssh://{authority}")
        hostname = parsed.hostname
        port = parsed.port
        if not hostname:
            return None
        _validated_host(host_port, hostname, port)
        if host_port.startswith("["):
            # Remote hosts require real IPv6; PR identity retains its existing URL contract.
            IPv6Address(hostname)
    except ValueError:
        return None
    if not ssh:
        host_text = (
            host_port[1 : host_port.find("]")]
            if host_port.startswith("[")
            else host_port.partition(":")[0]
        )
        if host_text.casefold() != hostname:
            return None
    return hostname.lower()


def parse_remote(url: str) -> Remote | None:
    # Slug extraction is lexical even when the host syntax cannot be recognized.
    match = _SCP_PATH_RE.match(url) or _URL_PATH_RE.match(url)
    path = match.group(1) if match else ""
    unknown = Remote(None, path, None) if path else None
    if url.startswith(("/", "\\", "./", "../")):
        return unknown
    if "://" in url:
        scheme, tail = url.split("://", 1)
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9+.-]*", scheme):
            return unknown
        delimiter = r"[/?#]" if scheme.lower() in {"http", "https"} else r"/"
        authority = re.split(delimiter, tail, maxsplit=1)[0]
        return Remote(
            _remote_hostname(authority, ssh=scheme.lower() == "ssh"),
            path,
            scheme.lower(),
        )
    if re.match(r"^[A-Za-z]:", url):
        return unknown
    prefix = url.split(":", 1)[0]
    host_start = prefix.rfind("@") + 1
    if url[host_start:].startswith("["):
        close = url.find("]", host_start)
        colon = close + 1 if close >= 0 and url[close + 1 :].startswith(":") else -1
    else:
        colon = url.find(":")
    slash = url.find("/")
    if colon < 0 or (slash >= 0 and colon > slash):
        return unknown
    authority = url[:colon]
    return Remote(_remote_hostname(authority, ssh=True), path, None)


def remote_slug(remote: Remote | None) -> RepoSlug | None:
    # Host validation must not discard a detector's usable lexical slug.
    if remote is None:
        return None
    owner, sep, repo = remote.path.strip("/").partition("/")
    return RepoSlug(owner, repo) if sep and owner and repo else None


def detect_platform(remote: Remote | None) -> PlatformDetection:
    if remote is None or remote.hostname is None:
        return PlatformDetection(None, None)
    host = remote.hostname
    platform: Platform | None = None
    if ":" not in host and remote.scheme in {None, "http", "https", "ssh"}:
        if host == "github.com" or host.endswith(".github.com"):
            platform = "github"
        elif host == "gitlab.com" or host.endswith(".gitlab.com"):
            platform = "gitlab"
    return PlatformDetection(platform, host)


def _validated_host(host_port: str, hostname: str, port: int | None) -> str:
    if host_port.startswith("["):
        close = host_port.find("]")
        suffix = host_port[close + 1 :]
        if close < 0 or (suffix and not re.fullmatch(r":[0-9]+", suffix)):
            raise ValueError("URL has an invalid IPv6 host or port")
        if not re.fullmatch(r"[0-9A-Fa-f:.]{2,45}", hostname):
            raise ValueError("URL has an invalid IPv6 host")
        host = f"[{hostname.lower()}]"
    else:
        if host_port.count(":") > 1:
            raise ValueError("IPv6 hosts must be bracketed")
        if ":" in host_port and not host_port.rpartition(":")[2]:
            raise ValueError("URL has an empty port")
        host = hostname.lower()
        if len(host) > 253 or not all(
            _DNS_LABEL_RE.fullmatch(label) for label in host.split(".")
        ):
            raise ValueError("URL has an invalid DNS host")
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("URL port must be between 1 and 65535")
    return host


def _web_origin(url: str) -> tuple[str, str]:
    try:
        parsed = urlsplit(url)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise ValueError(f"unparseable URL: {exc}") from exc
    if parsed.scheme not in {"http", "https"} or not hostname:
        raise ValueError("URL must use http(s) and include a host")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("URL must not contain user information")
    host = _validated_host(parsed.netloc, hostname, port)
    port_suffix = f":{port}" if port is not None else ""
    return f"{parsed.scheme.lower()}://{host}{port_suffix}", parsed.path


def parse_pr_url(platform: Platform, url: str) -> ParsedPrUrl:
    origin, path = _web_origin(url)
    match = _PR_PATHS[platform].fullmatch(path)
    if not match:
        target = "PR" if platform == "github" else "MR"
        raise ValueError(f"URL path does not match a {platform} {target} URL")
    owner, repo, number_text = match.groups()
    if len(number_text) > 16:
        raise ValueError("PR/MR number must be a positive safe integer")
    number = int(number_text)
    if number > 9007199254740991:
        raise ValueError("PR/MR number must be a positive safe integer")
    return ParsedPrUrl(owner, repo, number, origin)


def gitlab_project_id(owner: str, repo: str) -> str:
    return f"{owner}/{repo}".replace("/", "%2F")


def github_review_request(
    target: ReviewTarget, payload: Mapping[str, object]
) -> PostRequest:
    return PostRequest(
        "github",
        f"repos/{target.owner}/{target.repo}/pulls/{target.number}/reviews",
        "POST",
        ("Accept: application/vnd.github+json",),
        payload,
    )


def _gitlab_request(
    target: ReviewTarget,
    surface: Literal["notes", "discussions"],
    payload: Mapping[str, object],
) -> PostRequest:
    project = gitlab_project_id(target.owner, target.repo)
    return PostRequest(
        "gitlab",
        f"projects/{project}/merge_requests/{target.number}/{surface}",
        "POST",
        ("Content-Type: application/json",),
        payload,
    )


def gitlab_note_request(
    target: ReviewTarget, payload: Mapping[str, object]
) -> PostRequest:
    return _gitlab_request(target, "notes", payload)


def gitlab_discussion_request(
    target: ReviewTarget, payload: Mapping[str, object]
) -> PostRequest:
    return _gitlab_request(target, "discussions", payload)


class ForgeUnavailable(RuntimeError):
    """The selected forge CLI is absent."""


def _ensure_available(tool: str) -> None:
    if proc.which(tool) is None:
        raise ForgeUnavailable(
            f"'{tool}' CLI tool not found. "
            "Install it and ensure it is authenticated before running this script."
        )


def origin_remote(
    *, timeout: float | None = None, errors: Literal["strict", "replace"] = "strict"
) -> Remote | None:
    stdout, _, status = proc.output(
        ["git", "remote", "get-url", "origin"], timeout=timeout, errors=errors
    )
    if status != 0:
        return None
    remote = parse_remote(stdout.strip())
    if remote is None:
        return None
    # Extract the slug from the stripped URL to preserve its bytes. For the host,
    # remove only Git's trailing newline so stray whitespace or controls make it unknown.
    original = parse_remote(stdout.removesuffix("\n").removesuffix("\r"))
    return replace(remote, hostname=original.hostname if original else None)


def _parse_pages(text: str) -> list[object] | None:
    """Flatten concatenated pages while preserving a valid prefix before bad JSON."""
    text = text.strip()
    decoder = json.JSONDecoder()
    items: list[object] = []
    index = 0
    while index < len(text):
        while index < len(text) and text[index] in " \t\r\n":
            index += 1
        if index >= len(text):
            break
        try:
            doc, index = decoder.raw_decode(text, index)
        except (ValueError, RecursionError):
            return items if items else None
        if isinstance(doc, list):
            items.extend(doc)
        else:
            items.append(doc)
    return items


def _review_entries(command: Sequence[str], label: str) -> JsonFetch:
    try:
        stdout, stderr, status = proc.output(
            command, timeout=_FETCH_TIMEOUT_SECONDS, errors="replace"
        )
    except proc.TimeoutExpired:
        stdout, stderr, status = "", f"timed out after {_FETCH_TIMEOUT_SECONDS}s", -1
    except OSError as exc:
        stdout, stderr, status = "", str(exc), -1
    if status != 0:
        detail = (stderr.strip() or stdout.strip())[:300]
        return JsonFetch([], f"{label}: fetch failed (exit {status}): {detail}")
    items = _parse_pages(stdout)
    if items is None:
        return JsonFetch([], f"{label}: response was not JSON: {stdout.strip()[:120]}")
    return JsonFetch(items, None)


def _submit(tool: str, header_flag: str, request: PostRequest) -> PostResult:
    command = [tool, "api", "--method", request.method]
    for header in request.headers:
        command.extend([header_flag, header])
    command.append(request.endpoint)
    fd, path = tempfile.mkstemp(suffix=".json")
    try:
        # newline="" preserves LF bytes on Windows as well as POSIX.
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as stream:
            json.dump(dict(request.payload), stream, ensure_ascii=False)
        command.extend(["--input", path])
        stdout, stderr, status = proc.output(command)
        if status != 0:
            return PostResult(
                None,
                f"API call failed (exit {status}).\n"
                f"Command: {' '.join(command)}\n"
                f"stderr: {stderr.strip()}",
                None,
            )
        if not stdout.strip():
            return PostResult({}, None, None)
        try:
            response: object = json.loads(stdout)
            return PostResult(response, None, None)
        except json.JSONDecodeError:
            return PostResult(
                {"raw": stdout},
                None,
                f"Could not parse API response as JSON: {stdout[:200]}",
            )
    finally:
        if os.path.exists(path):
            os.unlink(path)


class _ForgeAdapter:
    platform: Platform
    _tool: ClassVar[str]
    _header_flag: ClassVar[str]

    def ensure_available(self) -> None:
        _ensure_available(self._tool)

    def submit(self, request: PostRequest) -> PostResult:
        if request.platform != self.platform:
            raise ValueError("Request platform does not match forge platform")
        return _submit(self._tool, self._header_flag, request)


class GitHub(_ForgeAdapter):
    platform: Platform = "github"
    _tool: ClassVar[str] = "gh"
    _header_flag: ClassVar[str] = "-H"

    def diff(self, target: ReviewTarget) -> tuple[str, str, int]:
        return proc.output(
            [
                self._tool,
                "pr",
                "diff",
                str(target.number),
                "--repo",
                f"{target.owner}/{target.repo}",
            ]
        )

    def review_entries(self, target: ReviewTarget) -> JsonFetch:
        return _review_entries(
            [
                self._tool,
                "api",
                "--paginate",
                f"repos/{target.owner}/{target.repo}/pulls/{target.number}/reviews",
            ],
            "github reviews",
        )


class GitLab(_ForgeAdapter):
    platform: Platform = "gitlab"
    _tool: ClassVar[str] = "glab"
    _header_flag: ClassVar[str] = "--header"

    def diff(self, target: ReviewTarget) -> tuple[str, str, int]:
        # Plain glab mr diff, never --raw or --repo: tests/fixtures/glab_diff/
        # records both output shapes, which parse_diff_text distinguishes per file.
        return proc.output([self._tool, "mr", "diff", str(target.number)])

    def review_entries(self, target: ReviewTarget) -> JsonFetch:
        # GitLab pages notes at 20 and the summary is posted first, so an
        # unpaginated read loses the summary past 20 notes.
        project = gitlab_project_id(target.owner, target.repo)
        return _review_entries(
            [
                self._tool,
                "api",
                "--paginate",
                f"projects/{project}/merge_requests/{target.number}/notes",
            ],
            "gitlab notes",
        )

    def diff_refs(self, target: ReviewTarget) -> JsonFetch:
        project = gitlab_project_id(target.owner, target.repo)
        stdout, stderr, status = proc.output(
            [
                self._tool,
                "api",
                f"projects/{project}/merge_requests/{target.number}/versions",
            ]
        )
        if status != 0:
            return JsonFetch(
                None,
                f"Failed to fetch MR versions (exit {status}): {stderr.strip()}\n"
                "Ensure glab is authenticated and the MR IID is correct.",
            )
        try:
            payload: object = json.loads(stdout)
        except json.JSONDecodeError:
            return JsonFetch(
                None, f"Could not parse MR versions response: {stdout[:200]}"
            )
        return JsonFetch(payload, None)


def make_forge(platform: Platform) -> GitHub | GitLab:
    return GitHub() if platform == "github" else GitLab()

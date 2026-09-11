#!/usr/bin/env python3
"""Resolve a PR/MR URL into the registry-owned delivery.prIdentity shape.

Field order, descriptions, and the authoritative consumer checks live in
workflows/src/registry.js as PR_IDENTITY_FIELDS.
"""

import argparse
import json
import re
import sys
import urllib.parse

SHA_FULL_RE = re.compile(r"^[0-9a-f]{40}$")
DNS_LABEL_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")
PATH_PATTERNS = {
    "github": re.compile(r"^/([^/]+)/([^/]+)/pull/([1-9][0-9]*)/?$"),
    "gitlab": re.compile(r"^/(.+)/([^/]+)/-/merge_requests/([1-9][0-9]*)/?$"),
}
MAX_SAFE_INTEGER = 9007199254740991


def fail(message):
    print(f"PR IDENTITY ERROR: {message}", file=sys.stderr)
    raise SystemExit(2)


def web_origin(url):
    try:
        parsed = urllib.parse.urlsplit(url)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        fail(f"unparseable URL: {exc}")
    if parsed.scheme not in {"http", "https"} or not hostname:
        fail("URL must use http(s) and include a host")
    if parsed.username is not None or parsed.password is not None:
        fail("URL must not contain user information")

    raw_authority = parsed.netloc
    if raw_authority.startswith("["):
        close = raw_authority.find("]")
        suffix = raw_authority[close + 1 :]
        if close < 0 or (suffix and not re.fullmatch(r":[0-9]+", suffix)):
            fail("URL has an invalid IPv6 host or port")
        if not re.fullmatch(r"[0-9A-Fa-f:.]{2,45}", hostname):
            fail("URL has an invalid IPv6 host")
        host = f"[{hostname.lower()}]"
    else:
        if raw_authority.count(":") > 1:
            fail("IPv6 hosts must be bracketed")
        if ":" in raw_authority and not raw_authority.rpartition(":")[2]:
            fail("URL has an empty port")
        host = hostname.lower()
        if len(host) > 253 or not all(
            DNS_LABEL_RE.fullmatch(label) for label in host.split(".")
        ):
            fail("URL has an invalid DNS host")

    if port is not None and not 1 <= port <= 65535:
        fail("URL port must be between 1 and 65535")
    port_suffix = f":{port}" if port is not None else ""
    return f"{parsed.scheme.lower()}://{host}{port_suffix}", parsed.path


def resolve(platform, url, sha, title=None):
    if not SHA_FULL_RE.fullmatch(sha):
        fail("sha must be a 40-character lowercase hex commit id")
    origin, path = web_origin(url)
    match = PATH_PATTERNS[platform].fullmatch(path)
    if not match:
        target = "PR" if platform == "github" else "MR"
        fail(f"URL path does not match a {platform} {target} URL")
    owner, repo, number_text = match.groups()
    number = int(number_text)
    if number > MAX_SAFE_INTEGER:
        fail("PR/MR number must be a positive safe integer")
    identity = {
        "owner": owner,
        "repo": repo,
        "pr_number": number,
        "sha_full": sha,
        "platform": platform,
        "web_origin": origin,
    }
    if title is not None and title.strip():
        identity["title"] = title
    return identity


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--platform", choices=("github", "gitlab"), required=True)
    parser.add_argument("--url", required=True)
    parser.add_argument("--sha", required=True)
    parser.add_argument("--title")
    args = parser.parse_args(argv)
    print(
        json.dumps(
            resolve(args.platform, args.url, args.sha, args.title),
            ensure_ascii=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

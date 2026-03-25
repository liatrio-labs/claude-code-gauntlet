# Bulletproof JSON payloads for AI agents calling REST APIs

**The single most reliable pattern for an AI agent constructing complex JSON in a bash sandbox: build the payload as a Python dict, serialize with `json.dumps()`, and pass the result to the CLI tool via a temp file or stdin pipe.** This eliminates the double-escaping trap — JSON escaping layered on shell escaping — that causes the vast majority of failures when LLMs generate shell commands with embedded JSON. The patterns below are tested against the hardest cases: markdown with code fences, emoji, unicode, dollar signs, backticks, and nested quotes.

---

## Why shell-constructed JSON fails and Python doesn't

When an LLM writes `gh api -f body="some markdown"` directly in bash, it must simultaneously reason about **two escaping layers**: JSON requires escaping `"` and `\`, while bash requires escaping `$`, `` ` ``, `"`, `'`, `\`, and `!`. These layers interact multiplicatively. A backtick inside a double-quoted string triggers command substitution. A `$` triggers variable expansion. A backslash meant for JSON gets consumed by the shell. **Python's `json.dumps()` collapses this to zero escaping layers** — the developer writes a native dict, and Python handles all JSON serialization automatically.

The ranked approaches from most to least reliable for AI agents:

- **Python `requests` library directly** — zero escaping layers, full control, no shell involvement at all
- **Python `json.dumps()` to temp file → `gh api --input /tmp/file.json`** — one clean handoff, shell never sees the JSON content
- **Python `subprocess` piping JSON to stdin** — same safety, no temp file needed
- **`jq -n --arg` piping to `gh api --input -`** — shell-native, safe string interpolation via `--arg`
- **Quoted heredoc `<<'EOF'`** — acceptable only for completely static JSON with no variable interpolation
- **`-f`/`-F` flags with inline values** — only for simple flat key-value payloads, never for nested objects or markdown content

---

## GitHub: batched PR review with inline comments

The endpoint `POST /repos/{owner}/{repo}/pulls/{pull_number}/reviews` requires a `comments` array of objects. **The `-f`/`-F` flags cannot construct arrays of objects** — this is confirmed in GitHub CLI issues #1484 and #3955. You must use `--input`.

### The payload structure

The review payload takes a `body` (summary string), `event` (`"COMMENT"`, `"APPROVE"`, or `"REQUEST_CHANGES"`), and a `comments` array. Each comment needs `path` (relative file path), `line` (line number in the file, preferred over the deprecated `position`), `side` (`"RIGHT"` for added/unchanged lines, `"LEFT"` for deleted lines), and `body` (markdown string). For multi-line comments, add `start_line` and `start_side`.

### Pattern 1: Python temp file (recommended default)

```python
#!/usr/bin/env python3
"""Post a batched PR review with inline comments via gh CLI."""
import json, subprocess, sys, os, tempfile

owner = "OWNER"
repo = "REPO"
pr_number = 42

comments = [
    {
        "path": "src/auth.py",
        "line": 87,
        "side": "RIGHT",
        "body": (
            "🔒 **Security issue**: This stores the token in plaintext.\n\n"
            "Use the keyring module instead:\n"
            "```python\n"
            "import keyring\n"
            "keyring.set_password(\"myapp\", user, token)\n"
            "```\n\n"
            "See [OWASP guidance](https://owasp.org/credential-storage) "
            "for details — don't use `os.environ` either since `/proc` "
            "exposes it. Cost: $0 to fix now, $$$$ later."
        ),
    },
    {
        "path": "src/utils.py",
        "line": 23,
        "side": "RIGHT",
        "body": "Nit: `Optional[str]` is clearer than `str | None` for Python 3.9 compat.",
    },
]

payload = {
    "body": "Reviewed — 2 comments, 1 security issue to address before merge.",
    "event": "REQUEST_CHANGES",
    "comments": comments,
}

# Write to temp file — shell never sees the JSON content
fd, tmppath = tempfile.mkstemp(suffix=".json")
try:
    with os.fdopen(fd, "w") as f:
        json.dump(payload, f)
    result = subprocess.run(
        [
            "gh", "api",
            "--method", "POST",
            "-H", "Accept: application/vnd.github+json",
            f"repos/{owner}/{repo}/pulls/{pr_number}/reviews",
            "--input", tmppath,
        ],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(f"FAILED ({result.returncode}): {result.stderr}", file=sys.stderr)
        sys.exit(1)
    response = json.loads(result.stdout)
    print(f"Review posted: {response.get('html_url', response.get('id'))}")
finally:
    os.unlink(tmppath)
```

### Pattern 2: Python subprocess with stdin pipe (no temp file)

```python
#!/usr/bin/env python3
"""Post PR review by piping JSON to gh api stdin."""
import json, subprocess, sys

payload = {
    "body": "LGTM with minor suggestions ✅",
    "event": "COMMENT",
    "comments": [
        {
            "path": "README.md",
            "line": 5,
            "side": "RIGHT",
            "body": "Typo: `teh` → `the`",
        }
    ],
}

result = subprocess.run(
    ["gh", "api", "--method", "POST",
     "-H", "Accept: application/vnd.github+json",
     "repos/OWNER/REPO/pulls/42/reviews",
     "--input", "-"],
    input=json.dumps(payload),
    capture_output=True, text=True,
)

if result.returncode != 0:
    print(f"Error: {result.stderr}", file=sys.stderr)
    sys.exit(1)
print(json.loads(result.stdout).get("html_url", "Success"))
```

### Pattern 3: jq construction piped to gh (shell-native)

```bash
# Safe even with arbitrary markdown content in variables
REVIEW_BODY="Reviewed — see inline comments"
COMMENT_BODY='Consider using `dataclass` here:

```python
@dataclass
class Config:
    name: str
    port: int = 8080
```

This costs $0 and saves maintenance time.'

jq -n \
  --arg body "$REVIEW_BODY" \
  --arg event "COMMENT" \
  --arg c_path "src/config.py" \
  --argjson c_line 15 \
  --arg c_body "$COMMENT_BODY" \
  '{
    body: $body,
    event: $event,
    comments: [{
      path: $c_path,
      line: $c_line,
      side: "RIGHT",
      body: $c_body
    }]
  }' | gh api \
    --method POST \
    -H "Accept: application/vnd.github+name" \
    repos/OWNER/REPO/pulls/42/reviews \
    --input -
```

The critical detail: `jq --arg` treats the value as a pre-escaped string, so `$`, backticks, quotes, and newlines in `$COMMENT_BODY` all survive intact. Use `--argjson` for integers and booleans (line numbers, boolean flags).

### When `-f`/`-F` flags are sufficient

For **standalone single review comments** (not batched reviews), the flat flag approach works:

```bash
SHA=$(gh pr view 42 --json headRefOid --jq .headRefOid)
gh api repos/OWNER/REPO/pulls/42/comments \
  -f body="Simple comment without special chars" \
  -f path="src/main.py" \
  -f commit_id="$SHA" \
  -F line=42 \
  -f side="RIGHT"
```

Use `-f` for strings (always treated as strings, no conversion). Use `-F` for integers and booleans (`-F line=42` sends the integer `42`, while `-f line=42` sends the string `"42"`). **`-F value=@filename`** reads a value from a file, and **`-F value=@-`** reads from stdin — useful for long markdown bodies. But none of these flags can construct the `comments` array needed for batched reviews.

---

## GitLab: merge request discussions with position data

The `glab api` command **does support `--input`** with identical semantics to `gh api`: `--input filename` reads a JSON body from a file, `--input -` reads from stdin, and when `--input` is used, any `-F`/`-f` flags become URL query parameters instead of body parameters. You must include `--header "Content-Type: application/json"` explicitly when using `--input` with `glab`.

### Getting the required SHA values

Inline MR comments require three SHA values (`base_sha`, `head_sha`, `start_sha`) from the MR diff versions endpoint. For a line added in the new version, set `position.new_line` and omit `position.old_line`. For a removed line, set `position.old_line` and omit `position.new_line`. For an unchanged context line, set both.

### Pattern 1: Python temp file for GitLab MR discussion

```python
#!/usr/bin/env python3
"""Post an inline MR discussion on GitLab with position data."""
import json, subprocess, sys, os, tempfile

project_id = "123"  # or URL-encoded path: "mygroup%2Fmyproject"
mr_iid = 7

# Step 1: fetch MR version SHAs
versions_raw = subprocess.run(
    ["glab", "api", f"projects/{project_id}/merge_requests/{mr_iid}/versions"],
    capture_output=True, text=True, check=True,
).stdout
versions = json.loads(versions_raw)
latest = versions[0]
base_sha = latest["base_commit_sha"]
head_sha = latest["head_commit_sha"]
start_sha = latest["start_commit_sha"]

# Step 2: build the discussion payload
payload = {
    "body": (
        "⚠️ **Potential regression**: this changes the return type.\n\n"
        "```diff\n"
        "- def get_user() -> User:\n"
        "+ def get_user() -> Optional[User]:\n"
        "```\n\n"
        "All 14 callers assume non-`None` return. "
        "Add a migration note or keep the old behavior."
    ),
    "position": {
        "position_type": "text",
        "base_sha": base_sha,
        "head_sha": head_sha,
        "start_sha": start_sha,
        "old_path": "src/users/service.py",
        "new_path": "src/users/service.py",
        "new_line": 42,
    },
}

# Step 3: write and send
fd, tmppath = tempfile.mkstemp(suffix=".json")
try:
    with os.fdopen(fd, "w") as f:
        json.dump(payload, f)
    result = subprocess.run(
        ["glab", "api", "--method", "POST",
         "--header", "Content-Type: application/json",
         "--input", tmppath,
         f"projects/{project_id}/merge_requests/{mr_iid}/discussions"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(f"FAILED: {result.stderr}", file=sys.stderr)
        sys.exit(1)
    resp = json.loads(result.stdout)
    print(f"Discussion created: {resp.get('id')}")
finally:
    os.unlink(tmppath)
```

### Pattern 2: jq + glab for shell-native GitLab workflow

```bash
#!/bin/bash
set -euo pipefail

PROJECT_ID=":id"  # auto-resolved by glab when inside git repo
MR_IID=7

# Get version SHAs
VERSIONS=$(glab api "projects/${PROJECT_ID}/merge_requests/${MR_IID}/versions")
BASE_SHA=$(echo "$VERSIONS" | jq -r '.[0].base_commit_sha')
HEAD_SHA=$(echo "$VERSIONS" | jq -r '.[0].head_commit_sha')
START_SHA=$(echo "$VERSIONS" | jq -r '.[0].start_commit_sha')

COMMENT_BODY='Missing null check — `user` can be None here when the session expires.

```python
if user is None:
    raise AuthenticationError("Session expired")
```'

jq -n \
  --arg body "$COMMENT_BODY" \
  --arg base_sha "$BASE_SHA" \
  --arg head_sha "$HEAD_SHA" \
  --arg start_sha "$START_SHA" \
  --arg old_path "src/auth.py" \
  --arg new_path "src/auth.py" \
  --argjson new_line 55 \
  '{
    body: $body,
    position: {
      position_type: "text",
      base_sha: $base_sha,
      head_sha: $head_sha,
      start_sha: $start_sha,
      old_path: $old_path,
      new_path: $new_path,
      new_line: $new_line
    }
  }' | glab api --method POST \
    --header "Content-Type: application/json" \
    --input - \
    "projects/${PROJECT_ID}/merge_requests/${MR_IID}/discussions"
```

### Fallback: curl with glab auth token

If `glab api --input` behaves unexpectedly, extract the token and use curl directly. GitLab's API accepts `--form` encoded data natively, which avoids JSON entirely for flat parameters:

```bash
# Extract token — glab stores it in ~/.config/glab-cli/config.yml
GITLAB_TOKEN=$(glab auth status -t 2>&1 | grep -oP 'Token: \K\S+')

curl -s --request POST \
  --header "PRIVATE-TOKEN: $GITLAB_TOKEN" \
  --header "Content-Type: application/json" \
  --data @/tmp/payload.json \
  "https://gitlab.example.com/api/v4/projects/123/merge_requests/7/discussions"
```

---

## The universal Python helper for Claude Code skill files

This is the single pattern to memorize. It works for any REST API, handles every escaping edge case, includes error handling, and requires only Python standard library plus `gh` or `glab` for auth.

```python
#!/usr/bin/env python3
"""
Universal pattern: construct JSON payload in Python, send via CLI tool.
Works for GitHub (gh), GitLab (glab), or raw curl.
"""
import json, subprocess, sys, os, tempfile
from typing import Any

def api_call(
    tool: str,           # "gh", "glab", or "curl"
    method: str,         # "GET", "POST", "PATCH", etc.
    endpoint: str,       # API path (gh/glab) or full URL (curl)
    payload: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> dict:
    """Make an API call with a JSON payload, handling all escaping safely."""

    headers = headers or {}
    fd, tmppath = tempfile.mkstemp(suffix=".json") if payload else (None, None)

    try:
        # Write payload to temp file if present
        if payload and tmppath:
            with os.fdopen(fd, "w") as f:
                json.dump(payload, f, ensure_ascii=False)

        # Build command based on tool
        if tool == "gh":
            cmd = ["gh", "api", "--method", method]
            for k, v in headers.items():
                cmd += ["-H", f"{k}: {v}"]
            if tmppath:
                cmd += ["--input", tmppath]
            cmd.append(endpoint)

        elif tool == "glab":
            cmd = ["glab", "api", "--method", method]
            cmd += ["--header", "Content-Type: application/json"]
            for k, v in headers.items():
                cmd += ["--header", f"{k}: {v}"]
            if tmppath:
                cmd += ["--input", tmppath]
            cmd.append(endpoint)

        elif tool == "curl":
            token_cmd = ["gh", "auth", "token"]  # adjust for glab
            token = subprocess.run(
                token_cmd, capture_output=True, text=True, check=True
            ).stdout.strip()
            cmd = ["curl", "-s", "-X", method,
                   "-H", f"Authorization: Bearer {token}",
                   "-H", "Content-Type: application/json"]
            for k, v in headers.items():
                cmd += ["-H", f"{k}: {v}"]
            if tmppath:
                cmd += ["-d", f"@{tmppath}"]
            cmd.append(endpoint)
        else:
            raise ValueError(f"Unknown tool: {tool}")

        result = subprocess.run(cmd, capture_output=True, text=True)

        if result.returncode != 0:
            raise RuntimeError(
                f"{tool} failed (exit {result.returncode}): {result.stderr}"
            )

        if not result.stdout.strip():
            return {"status": "ok"}
        return json.loads(result.stdout)

    finally:
        if tmppath and os.path.exists(tmppath):
            os.unlink(tmppath)


# ── Usage examples ──────────────────────────────────────────────

# GitHub: batched PR review
review = api_call(
    tool="gh",
    method="POST",
    endpoint="repos/myorg/myrepo/pulls/42/reviews",
    headers={"Accept": "application/vnd.github+json"},
    payload={
        "body": "Review complete — 2 issues found ✅",
        "event": "REQUEST_CHANGES",
        "comments": [
            {
                "path": "src/main.py",
                "line": 87,
                "side": "RIGHT",
                "body": (
                    "🔒 Don't use `eval()` here:\n"
                    "```python\n"
                    "result = ast.literal_eval(expr)\n"
                    "```"
                ),
            },
            {
                "path": "config.yaml",
                "line": 12,
                "side": "RIGHT",
                "body": "This `$SECRET_KEY` reference won't resolve at runtime.",
            },
        ],
    },
)
print(f"GitHub review: {review.get('html_url', review)}")

# GitLab: inline MR discussion
discussion = api_call(
    tool="glab",
    method="POST",
    endpoint="projects/123/merge_requests/7/discussions",
    payload={
        "body": "Missing error handling for `None` return value.",
        "position": {
            "position_type": "text",
            "base_sha": "abc123",
            "head_sha": "def456",
            "start_sha": "ghi789",
            "old_path": "lib/auth.rb",
            "new_path": "lib/auth.rb",
            "new_line": 55,
        },
    },
)
print(f"GitLab discussion: {discussion.get('id', discussion)}")
```

### Running Python from Claude Code's bash sandbox

Since Claude Code operates in bash, wrap the Python in a quoted heredoc to prevent the shell from interpreting any special characters in the Python code:

```bash
python3 << 'PYTHON_EOF'
import json, subprocess, tempfile, os

payload = {
    "body": "All the dangerous chars: $ ` \" ' \\ \n newlines and 日本語 emoji 🎉",
    "event": "COMMENT",
    "comments": []
}

fd, tmp = tempfile.mkstemp(suffix=".json")
with os.fdopen(fd, "w") as f:
    json.dump(payload, f)

subprocess.run(
    ["gh", "api", "--method", "POST",
     "repos/OWNER/REPO/pulls/42/reviews",
     "--input", tmp],
    check=True
)
os.unlink(tmp)
PYTHON_EOF
```

**The `<<'PYTHON_EOF'` (with quotes around the delimiter) is critical.** Without quotes, bash expands `$`, backticks, and `\` inside the heredoc before Python ever sees the code. With quotes, the entire Python script passes through to the interpreter verbatim.

---

## Escaping failure modes and how each pattern avoids them

Understanding *why* patterns fail matters as much as knowing which ones work. The table below maps dangerous characters to the approach that neutralizes them.

| Character | Bash danger | `json.dumps` | `jq --arg` | `<<'EOF'` | `-f` flag |
|-----------|------------|-------------|-----------|----------|----------|
| `` ` `` backtick | Command substitution in `""` and unquoted heredocs | ✅ Safe (never hits shell) | ✅ Safe (jq escapes) | ✅ Safe (no expansion) | ❌ Breaks in double-quoted context |
| `$` dollar | Variable expansion | ✅ Safe | ✅ Safe | ✅ Safe | ❌ Expands in `"$val"` |
| `"` double quote | Terminates strings | ✅ Auto-escaped to `\"` | ✅ Auto-escaped | ✅ Literal | ⚠️ Requires manual `\"` |
| `\n` newline | Breaks arguments | ✅ Escaped to `\\n` in JSON | ✅ Escaped | ✅ Literal in heredoc | ❌ Often mangled |
| `\` backslash | Shell escape char, JSON escape char — double-escaping trap | ✅ Auto-doubled | ✅ Auto-doubled | ✅ Literal | ❌ Double-escape required |
| `'` single quote | Cannot be escaped inside `'...'` | ✅ Safe | ✅ Safe | ✅ Safe (heredoc not `'`-quoted) | ⚠️ OK in `"..."` context |
| Unicode/emoji | Encoding issues | ✅ `ensure_ascii=False` preserves UTF-8 | ✅ Passes through | ✅ Literal | ⚠️ Depends on locale |

The **fundamental insight**: every approach that keeps JSON construction inside a programming language (Python, jq) and passes the serialized result as a file or stdin stream is safe. Every approach that embeds JSON in shell string literals is fragile.

---

## Error handling and validation checklist

Robust API calls need three layers of error handling: pre-flight validation, HTTP error capture, and response parsing.

```python
import json, subprocess, tempfile, os, sys

def safe_api_post(tool, endpoint, payload, max_retries=2):
    """Post JSON with validation, error capture, and retry."""

    # 1. Pre-flight: validate payload is serializable
    try:
        json_str = json.dumps(payload, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as e:
        print(f"Payload serialization failed: {e}", file=sys.stderr)
        return None

    # 2. Write to temp file
    fd, tmp = tempfile.mkstemp(suffix=".json")
    with os.fdopen(fd, "w") as f:
        f.write(json_str)

    try:
        for attempt in range(max_retries + 1):
            if tool == "gh":
                cmd = ["gh", "api", "--method", "POST",
                       "-H", "Accept: application/vnd.github+json",
                       "--input", tmp, endpoint]
            elif tool == "glab":
                cmd = ["glab", "api", "--method", "POST",
                       "--header", "Content-Type: application/json",
                       "--input", tmp, endpoint]

            result = subprocess.run(cmd, capture_output=True, text=True)

            # 3. Parse response
            if result.returncode == 0:
                try:
                    return json.loads(result.stdout) if result.stdout.strip() else {}
                except json.JSONDecodeError:
                    return {"raw": result.stdout}

            # Check for retryable errors (rate limit, server error)
            stderr = result.stderr.lower()
            if any(x in stderr for x in ["rate limit", "502", "503", "504"]):
                if attempt < max_retries:
                    import time
                    wait = 2 ** attempt
                    print(f"Retrying in {wait}s...", file=sys.stderr)
                    time.sleep(wait)
                    continue

            # Non-retryable error
            print(f"API error (exit {result.returncode}):", file=sys.stderr)
            print(result.stderr, file=sys.stderr)
            return None

    finally:
        os.unlink(tmp)
```

**Three validations you should always perform**: verify `json.dumps` succeeds with `allow_nan=False` (catches `NaN`/`Infinity` which produce invalid JSON), check the CLI exit code is zero, and confirm the response body parses as valid JSON. For GitHub specifically, a **422 Unprocessable Entity** usually means the `line` value doesn't exist in the PR diff — validate line numbers against the actual diff before posting.

---

## Conclusion

The core principle is **separation of concerns**: let Python handle JSON serialization, let the CLI tool handle authentication and HTTP transport, and never let the shell see unescaped JSON content. The `python3 << 'EOF'` + `json.dump()` + `gh api --input /tmp/file.json` pipeline is the most robust pattern for Claude Code because it has exactly zero points where shell metacharacters can cause corruption. The `jq -n --arg` pipeline is the best alternative when staying in pure bash is necessary. Avoid string interpolation of markdown content into shell commands entirely — this is where every escaping failure originates.
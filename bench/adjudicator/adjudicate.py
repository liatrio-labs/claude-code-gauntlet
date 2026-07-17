"""Adjudicator: classify a non-golden-matched review comment as valid_extra | noise.

A comment that the LLM judge did not match to any golden is not automatically
"noise" — it may be a real, grounded issue the golden set simply does not cover.
The adjudicator makes that call with a deliberately narrow view: the comment
text, the diff hunk it targets, and a few nearby lines of the changed file, and
nothing else about the repository (frozen prompt in ``prompt.txt``). It is a
single, temperature-0 call to the same pinned Opus 4.8 snapshot the judge uses,
via the Anthropic OpenAI-compatible chat-completions endpoint (spec H5).

Public surface:

* ``adjudicate(comment_text, diff_hunk, file_context, pin, api_key)`` -> a dict
  ``{"bucket": "valid_extra"|"noise", "failed_check": 1|2|3|4|None,
  "reason": <str>}``. Strict JSON parse of the model reply with a single retry
  on malformed JSON; a second malformed reply raises ``ValueError`` rather than
  guessing a bucket.
* ``slice_hunk(diff_text, path, line)`` and ``file_context(file_lines, line)``
  are pure helpers that build the two context strings; they are the load-bearing
  logic and are tested exhaustively. Network I/O lives behind ``_transport`` so
  tests never touch the wire.

stdlib-only (CLAUDE.md): ``urllib.request`` for HTTP, no third-party deps.
"""

import json
import re
import urllib.request
from pathlib import Path

__all__ = ["adjudicate", "slice_hunk", "file_context", "FROZEN_PROMPT"]

CHAT_COMPLETIONS_URL = "https://api.anthropic.com/v1/chat/completions"

# The frozen classifier prompt lives beside this module and is copied verbatim
# from the plan. It is the system message on every adjudication call.
_PROMPT_PATH = Path(__file__).resolve().parent / "prompt.txt"
FROZEN_PROMPT = _PROMPT_PATH.read_text(encoding="utf-8")

_CONTEXT_RADIUS = 5  # nearby head-file lines shown on each side of the target line
_HUNK_HEADER_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


# ------------------------------------------------------------------ diff slicing


def _diff_path(header_line):
    """Return the new-file path named by a ``+++ b/<path>`` line, or None."""
    if not header_line.startswith("+++ "):
        return None
    target = header_line[4:].strip()
    if target == "/dev/null":
        return None
    # Strip a leading ``b/`` (git) prefix; some diffs omit it.
    if target.startswith("b/"):
        target = target[2:]
    # Drop a trailing tab-timestamp that some diff tools append.
    return target.split("\t", 1)[0]


def _iter_file_hunks(diff_text, path):
    """Yield ``(new_start, new_count, hunk_text)`` for each hunk under ``path``.

    ``new_start``/``new_count`` come from the ``@@ -a,b +c,d @@`` header and
    describe the hunk's span in the *new* file. ``hunk_text`` is the header line
    plus its body, verbatim.
    """
    lines = diff_text.splitlines(keepends=True)
    current_path = None
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        if line.startswith("+++ "):
            current_path = _diff_path(line.rstrip("\n"))
            i += 1
            continue
        if line.startswith("--- ") or line.startswith("diff --git "):
            i += 1
            continue
        m = _HUNK_HEADER_RE.match(line)
        if m and current_path == path:
            new_start = int(m.group(1))
            new_count = int(m.group(2)) if m.group(2) is not None else 1
            body = [line]
            j = i + 1
            while j < n:
                nxt = lines[j]
                if nxt.startswith("@@ ") or nxt.startswith("diff --git ") or nxt.startswith("+++ "):
                    break
                body.append(nxt)
                j += 1
            yield new_start, new_count, "".join(body)
            i = j
            continue
        i += 1


def slice_hunk(diff_text, path, line):
    """Return the diff hunk under ``path`` that covers new-file ``line``.

    ``line`` is a 1-based line number in the new (post-change) file. A hunk
    covers ``[new_start, new_start + new_count - 1]`` inclusive, so a line at a
    hunk boundary is matched. If no hunk contains ``line`` but the path has
    hunks, the nearest hunk (by distance to its new-file span) is returned so
    the adjudicator still sees relevant context. Raises ``ValueError`` when the
    path is absent from the diff (an informative error, never a guess).
    """
    hunks = list(_iter_file_hunks(diff_text, path))
    if not hunks:
        raise ValueError(
            "path {!r} not found in diff (no +++ header / hunks for it)".format(path)
        )

    for new_start, new_count, text in hunks:
        span = new_count if new_count > 0 else 1
        if new_start <= line <= new_start + span - 1:
            return text

    # Line outside every hunk for this path — return the closest hunk.
    def distance(h):
        new_start, new_count, _ = h
        span = new_count if new_count > 0 else 1
        end = new_start + span - 1
        if line < new_start:
            return new_start - line
        return line - end

    return min(hunks, key=distance)[2]


def file_context(file_lines, line, radius=_CONTEXT_RADIUS):
    """Render new-file lines ``line-radius .. line+radius`` with line numbers.

    ``file_lines`` may be a list of lines or a single string (split on newlines).
    ``line`` is 1-based. The window is clamped to the file bounds, so a target
    near the first or last line simply yields a shorter window. Returns ``""``
    when there are no lines to show. Pure — the caller decides where the head
    file comes from (a live worktree, a saved snapshot, or nothing).
    """
    if isinstance(file_lines, str):
        file_lines = file_lines.splitlines()
    else:
        file_lines = list(file_lines)
    total = len(file_lines)
    if total == 0 or line is None:
        return ""

    start = max(1, line - radius)
    end = min(total, line + radius)
    if start > end:
        return ""

    out = []
    for num in range(start, end + 1):
        content = file_lines[num - 1].rstrip("\n")
        marker = ">" if num == line else " "
        out.append("{} {}: {}".format(marker, num, content))
    return "\n".join(out)


# ------------------------------------------------------------------- HTTP + call


def _transport(url, headers, payload):
    """POST ``payload`` as JSON and return the decoded JSON reply (real wire).

    Isolated so tests inject a fake and never hit the network.
    """
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _build_user_message(comment_text, diff_hunk, file_context_str):
    return (
        "Code-review comment:\n"
        "{comment}\n\n"
        "Diff hunk it targets:\n"
        "{hunk}\n\n"
        "Nearby lines from the changed file:\n"
        "{ctx}\n"
    ).format(
        comment=comment_text or "",
        hunk=diff_hunk or "(no hunk located)",
        ctx=file_context_str or "(no file context available)",
    )


def _extract_content(reply):
    """Pull the assistant message text out of an OpenAI-compat chat reply."""
    choices = reply.get("choices") or []
    if not choices:
        raise ValueError("no choices in chat-completions reply")
    message = choices[0].get("message") or {}
    content = message.get("content")
    if not isinstance(content, str):
        raise ValueError("chat-completions reply has no string content")
    return content


def _parse_verdict(content):
    """Strip optional code fences and parse the strict verdict JSON."""
    text = content.strip()
    if text.startswith("```"):
        parts = text.split("```")
        text = parts[1] if len(parts) > 1 else text
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()
    data = json.loads(text)  # may raise json.JSONDecodeError
    bucket = data.get("bucket")
    if bucket not in ("valid_extra", "noise"):
        raise ValueError("verdict bucket must be valid_extra|noise, got {!r}".format(bucket))
    return {
        "bucket": bucket,
        "failed_check": data.get("failed_check"),
        "reason": data.get("reason", ""),
    }


def adjudicate(comment_text, diff_hunk, file_context, pin, api_key, transport=None):
    """Classify one non-golden-matched comment as ``valid_extra`` or ``noise``.

    Sends a single temperature-0 request to the pinned model and parses the
    strict-JSON verdict. On a malformed reply the call is retried exactly once;
    a second malformed reply raises ``ValueError`` (the caller surfaces the bug
    rather than silently bucketing). ``transport`` is injectable for tests.
    """
    post = transport or _transport
    headers = {
        "Authorization": "Bearer {}".format(api_key),
        "Content-Type": "application/json",
    }
    payload = {
        "model": pin,
        "temperature": 0,
        "messages": [
            {"role": "system", "content": FROZEN_PROMPT},
            {"role": "user", "content": _build_user_message(comment_text, diff_hunk, file_context)},
        ],
    }

    last_error = None
    for _attempt in range(2):
        reply = post(CHAT_COMPLETIONS_URL, headers, payload)
        try:
            return _parse_verdict(_extract_content(reply))
        except (ValueError, json.JSONDecodeError) as exc:
            last_error = exc
    raise ValueError("adjudicator returned unparseable JSON twice: {}".format(last_error))

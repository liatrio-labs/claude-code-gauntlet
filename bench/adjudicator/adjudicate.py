"""Judge unmatched review comments using a constrained context snapshot.

The pinned judge sees only the comment, its diff hunk, and nearby changed file
lines. Network I/O lives behind a transport seam so tests can exercise
adjudication without contacting the endpoint.
"""

import json
import re
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from gauntlet.diff import HunkEvent, raw_hunks

__all__ = ["FROZEN_PROMPT", "adjudicate", "file_context", "slice_hunk"]

CHAT_COMPLETIONS_URL = "https://api.anthropic.com/v1/chat/completions"

# The frozen classifier prompt lives beside this module and is copied verbatim
# from the plan. It is the system message on every adjudication call.
_PROMPT_PATH = Path(__file__).resolve().parent / "prompt.txt"
FROZEN_PROMPT = _PROMPT_PATH.read_text(encoding="utf-8")

_CONTEXT_RADIUS = 5  # nearby head-file lines shown on each side of the target line


# ------------------------------------------------------------------ diff slicing


def slice_hunk(diff_text: str, path: str, line: int) -> str:
    """Keep the judge's view local even when a finding falls between hunks."""
    hunks: list[tuple[HunkEvent, str]] = []
    for raw_hunk in raw_hunks(diff_text):
        if raw_hunk.new_path is None:
            continue
        # Bench diffs are git output and candidate paths are repo-relative.
        target = raw_hunk.new_path.strip().removeprefix("b/")
        if target != path or target == "/dev/null":
            continue
        hunks.append((raw_hunk.hunk, raw_hunk.text))
    if not hunks:
        raise ValueError(
            f"path {path!r} not found in diff (no +++ header / hunks for it)"
        )

    for hunk, text in hunks:
        span = max(hunk.new_count, 1)
        if hunk.new_line <= line <= hunk.new_line + span - 1:
            return text

    def distance(item: tuple[HunkEvent, str]) -> int:
        hunk, _ = item
        end = hunk.new_line + max(hunk.new_count, 1) - 1
        if line < hunk.new_line:
            return hunk.new_line - line
        return line - end

    return min(hunks, key=distance)[1]


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
        out.append(f"{marker} {num}: {content}")
    return "\n".join(out)


# ------------------------------------------------------------------- HTTP + call


def _transport(url, headers, payload):
    """POST ``payload`` as JSON and return the decoded JSON reply (real wire).

    Isolated so tests inject a fake and never hit the network.
    """
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=120) as resp:
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


# Recovery for a reply whose overall shape is the fixed verdict object but whose free-text
# "reason" value carries model-emitted UNescaped inner double-quotes -- a `"` inside reason
# that json.loads reads as the string's closing quote, then trips on the next char (the
# "Expecting ',' delimiter" char-112 failure observed on the discourse-graphite#6 comment,
# whose reason quoted the Ruby snippet `"." << website_host`). The verdict schema is small and
# fixed: bucket is a closed enum, failed_check is 1-4|null, reason is the LAST field. Extract
# the two load-bearing fields by anchored pattern and take reason as the trailing string, so a
# malformed reason never costs the scorer the bucket. This repairs ONLY the adjudicator's own
# reply -- the judged comment content is never inspected or altered here.
_RECOVER_BUCKET_RE = re.compile(r'"bucket"\s*:\s*"(valid_extra|noise)"')
_RECOVER_FAILED_CHECK_RE = re.compile(r'"failed_check"\s*:\s*(null|"?[1-4]"?)')
_RECOVER_REASON_RE = re.compile(r'"reason"\s*:\s*"(.*)"\s*}\s*$', re.DOTALL)


def _recover_verdict(text):
    """Best-effort structured parse of a verdict reply that strict JSON rejected.

    Returns the same dict shape as the strict path, or raises ValueError when the
    load-bearing ``bucket`` cannot be recovered -- a genuinely unusable reply still
    fails loud, preserving ``adjudicate``'s retry-then-raise contract.
    """
    bucket_m = _RECOVER_BUCKET_RE.search(text)
    if not bucket_m:
        raise ValueError("verdict recovery failed: no valid bucket found in reply")
    failed_check = None
    fc_m = _RECOVER_FAILED_CHECK_RE.search(text)
    if fc_m:
        raw = fc_m.group(1).strip('"')
        if raw != "null":
            failed_check = int(raw)
    reason_m = _RECOVER_REASON_RE.search(text)
    return {
        "bucket": bucket_m.group(1),
        "failed_check": failed_check,
        "reason": reason_m.group(1) if reason_m else "",
    }


def _parse_verdict(content):
    """Strip optional code fences and parse the verdict JSON.

    Strict ``json.loads`` first -- unchanged for every well-formed reply. Only on a
    ``JSONDecodeError`` (a malformed reply, e.g. unescaped inner quotes in "reason")
    does it fall back to ``_recover_verdict``; a recovered dict already carries a valid
    bucket, so it skips the object/bucket re-checks below.
    """
    text = content.strip()
    if text.startswith("```"):
        parts = text.split("```")
        text = parts[1] if len(parts) > 1 else text
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()
    try:
        data = json.loads(
            text
        )  # strict path: behavior unchanged for well-formed replies
    except json.JSONDecodeError:
        return _recover_verdict(text)
    if not isinstance(data, dict):
        raise ValueError(f"verdict must be a JSON object, got {type(data).__name__}")
    bucket = data.get("bucket")
    if bucket not in ("valid_extra", "noise"):
        raise ValueError(f"verdict bucket must be valid_extra|noise, got {bucket!r}")
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
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": pin,
        "temperature": 0,
        "messages": [
            {"role": "system", "content": FROZEN_PROMPT},
            {
                "role": "user",
                "content": _build_user_message(comment_text, diff_hunk, file_context),
            },
        ],
    }

    last_error = None
    for _attempt in range(2):
        reply = post(CHAT_COMPLETIONS_URL, headers, payload)
        try:
            return _parse_verdict(_extract_content(reply))
        except (ValueError, json.JSONDecodeError) as exc:
            last_error = exc
    raise ValueError(f"adjudicator returned unparseable JSON twice: {last_error}")

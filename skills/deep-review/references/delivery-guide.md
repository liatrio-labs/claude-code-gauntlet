# Delivery Guide

Implementation details for each delivery method in Phase 7.

---

## PR/MR Comments (platform-aware)

**Only comment on files in the diff.** The GitHub/GitLab API rejects inline comments on files not part of the PR/MR diff (HTTP 422). Before posting each inline comment, verify the file path is in the changed files list. Cross-file impact findings about files outside the diff go in the top-level summary comment with a note: "This finding references `path/to/file.cs` which is not in this PR's diff."

**Batch ALL inline comments into a single review event** — one GitHub notification instead of N separate ones. Notification fatigue causes teams to auto-dismiss AI review within ~10 days.

**Inline comment default cap: 8.** When the user selects "Default — top 8 by severity," post the top 8 findings as inline comments (highest severity first); remaining findings go in condensed format in the executive summary comment. When the user selects "Let me pick" and chooses findings via the interactive walkthrough, post **all selected findings** as inline comments — no cap applies. The user made a deliberate selection; respect it.

### Comment body format

```
**{emoji} [{severity}] {finding.title}**

{finding.description}

[If finding.suggestion is a direct code replacement — use a suggestion block:]
```suggestion
{the fixed code lines}
```

[If finding.suggestion is prose advice — use plain text:]
**Suggested fix:** {finding.suggestion}
```

**Suggestion blocks vs prose heuristic:** If `suggestion` contains code that could directly replace lines at `finding.file:line_start-line_end` (syntax characters, matches file language, complete statement/block), use a `suggestion` block. If advisory text ("consider using...", "add validation for..."), use prose. When in doubt, use prose — a broken suggestion block is worse than none.

Severity emojis: 🔴 critical, 🟠 high, 🟡 medium, 💡 low.

### Posting comments — use Python, not shell JSON

Shell-constructed JSON fails because of the double-escaping trap (JSON escaping + bash metacharacters). Python `json.dumps()` to temp file → `gh/glab api --input` is the most reliable pattern. **Always use this approach — never construct JSON payloads in bash.**

Write a Python script to a temp file and execute it. Do NOT use heredocs (`<< 'EOF'`) — they cause escaping issues where `!=` becomes `\!=` (invalid Python). Instead, use the Write tool to create the script file, then run it with Bash:

```
Write the script to $TMPDIR/post_review.py, then:
python3 $TMPDIR/post_review.py
```

Reference implementation below (adapt, don't paste into a heredoc):

#### GitHub — batched PR review

```bash
python3 << 'PYTHON_EOF'
import json, subprocess, tempfile, os, sys

owner = "{owner}"
repo = "{repo}"
pr_number = {number}

payload = {
    "body": "{summary comment with verdict, finding counts, and footer}",
    "event": "COMMENT",  # or "REQUEST_CHANGES" per verdict criteria
    "comments": [
        {
            "path": "{file_path}",
            "line": {line_number},
            "side": "RIGHT",
            "body": "{comment body with emoji, markdown, code blocks}",
        },
        # ... up to 8 inline comments (cap)
    ],
}

fd, tmp = tempfile.mkstemp(suffix=".json")
try:
    with os.fdopen(fd, "w") as f:
        json.dump(payload, f, ensure_ascii=False)
    result = subprocess.run(
        ["gh", "api", "--method", "POST",
         "-H", "Accept: application/vnd.github+json",
         f"repos/{owner}/{repo}/pulls/{pr_number}/reviews",
         "--input", tmp],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(f"FAILED: {result.stderr}", file=sys.stderr)
        sys.exit(1)
    resp = json.loads(result.stdout)
    print(f"Review posted: {resp.get('html_url', resp.get('id'))}")
finally:
    os.unlink(tmp)
PYTHON_EOF
```

#### GitLab — inline MR discussions

GitLab requires separate API calls per inline comment (no batched review endpoint). Each needs position SHAs from the MR versions endpoint:

```bash
python3 << 'PYTHON_EOF'
import json, subprocess, tempfile, os, sys

project_id = "{project_id}"  # or URL-encoded path
mr_iid = {number}

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

# Step 2: post each inline comment as a discussion
comments = [
    {"path": "{file}", "line": {line}, "body": "{comment body}"},
    # ...
]

for c in comments:
    payload = {
        "body": c["body"],
        "position": {
            "position_type": "text",
            "base_sha": base_sha,
            "head_sha": head_sha,
            "start_sha": start_sha,
            "old_path": c["path"],
            "new_path": c["path"],
            "new_line": c["line"],
        },
    }
    fd, tmp = tempfile.mkstemp(suffix=".json")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(payload, f, ensure_ascii=False)
        result = subprocess.run(
            ["glab", "api", "--method", "POST",
             "--header", "Content-Type: application/json",
             "--input", tmp,
             f"projects/{project_id}/merge_requests/{mr_iid}/discussions"],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            print(f"FAILED for {c['path']}:{c['line']}: {result.stderr}", file=sys.stderr)
    finally:
        os.unlink(tmp)

print(f"Posted {len(comments)} inline comments on MR !{mr_iid}")
PYTHON_EOF
```

After inline comments, post the executive summary as a top-level PR/MR comment using the same Python pattern.

### Findings metadata footer

Append a hidden HTML comment to the executive summary for incremental review support:

```html
<!-- deep-review-findings: {"version":1,"sha":"{full_sha}","findings":[{"id":"{id}","file":"{file}","line":{line},"dim":"{dim}","title_hash":"{hash}"}]} -->
```

Place adjacent to the `Generated by deep-review | Reviewed up to: {sha}` footer line.

---

## Task Creation

**Always let the user choose which findings become tasks.** Do NOT create tasks automatically. The interactive walkthrough in SKILL.md Phase 7 handles the selection UX — this section covers what happens after the user has made their selections.

### Create FIX tasks

Read `references/fix-task-metadata.md` for the full template. The process is:
1. **Detect toolchain** (Step 2.5) — scan for package.json, Cargo.toml, go.mod, etc.
2. **Detect patterns_to_follow** (Step 3a) — identify 1-2 nearby files as style references
3. **TaskCreate** (Step 3b) — structured description with Issue, Location, Evidence, Suggested Fix
4. **TaskUpdate with metadata** (Step 3c) — full cw-execute-compatible metadata

After creating: "Created N tasks from review findings."

---

## Markdown File

Write the full report to `./deep-review-<date>.md` or a user-specified path.

---

## Chat

Print the full report in the conversation. For large reports, use collapsible sections for medium/low findings.

---

## Dismissed Findings

After delivery, ask if findings should be suppressed in future reviews:

```
AskUserQuestion(
  question: "Should any of these findings be ignored in future reviews? This adds them to REVIEW.md so they won't be flagged again.",
  options: [
    "Yes — let me pick which ones to dismiss",
    "No — all findings are valid"
  ]
)
```

If yes, show the same numbered list and let the user pick (same natural patterns as task selection). Ask for a brief reason.

### Show proposed entries before writing

```
These entries would be added to REVIEW.md under ## Ignore:

- security:"prompt injection via template tokens" (not exploitable in current architecture, 2026-03-24)
- bug:"DateTime.UtcNow testability" (tracked in ROADMAP.md as deferred item, 2026-03-24)
```

Each entry: dimension, pattern matching finding title, parenthesized reason with date.

### Confirm via AskUserQuestion before writing

```
AskUserQuestion(
  question: "Add these to REVIEW.md?",
  options: [
    "Yes — add to REVIEW.md",
    "No — skip"
  ]
)
```

If confirmed:
- If no REVIEW.md exists → offer to create using scaffolding template from `references/review-md-spec.md`
- If REVIEW.md exists without `## Ignore` → append the section
- If `## Ignore` exists → append new entries

After writing: "Added N dismissed findings to REVIEW.md. These won't be flagged in future reviews."

If declined, skip without modifying files.

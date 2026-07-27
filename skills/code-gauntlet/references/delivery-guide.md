# Delivery Guide

Implementation details for each delivery method in Phase 8.

---

## PR/MR Comments (platform-aware)

**Only comment on files in the diff.** The GitHub/GitLab API rejects inline comments on files not part of the PR/MR diff (HTTP 422). Before posting each inline comment, verify the file path is in the changed files list. Cross-file impact findings about files outside the diff go in the top-level summary comment with a note: "This finding references `path/to/file.cs` which is not in this PR's diff."

**Batch ALL inline comments into a single review event** — one GitHub notification instead of N separate ones. Notification fatigue causes teams to auto-dismiss AI review within ~10 days.

**Inline comment default cap: 6.** When the user selects "Default — top 6 by severity," post the top 6 findings as inline comments (highest severity first); remaining findings go in condensed format in the executive summary comment. When the user selects "Let me pick" and chooses findings via the interactive walkthrough, post **all selected findings** as inline comments — no cap applies. The user made a deliberate selection; respect it.

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

### Using post_review.py

**Do NOT post PR comments via direct `gh api` or `glab api` calls.** Use the bundled `scripts/post_review.py` script instead. It handles platform detection, diff validation, and API calls deterministically.

**Usage:**
```bash
python3 {plugin_root}/scripts/post_review.py <findings_json_path>
```

**Findings JSON schema:**

```json
{
    "review_body": "Executive summary comment with finding counts (post_review.py appends the footer)",
    "findings": [
        {
            "file": "src/foo.py",
            "line": 42,
            "end_line": 45,
            "severity": "critical|high|medium|low",
            "title": "Finding title",
            "body": "Detailed description (note: delivery schema uses 'body', not 'description' — the orchestrator maps description→body when constructing delivery JSON)",
            "suggested_fix_code": "code block (optional; renders as suggestion)"
        }
    ],
    "owner": "repository-owner",
    "repo": "repository-name",
    "pr_number": 123,
    "platform": "github|gitlab",
    "sha": "<full_sha>"
}
```

**Fields:**

- `review_body` — executive summary comment (counts, no spoilers)
- `findings` — array of inline comments
  - `file` — relative path in repository
  - `line` — line number in diff (new version)
  - `end_line` — optional; enables multi-line comments on GitHub
  - `severity` — emoji selected from: critical, high, medium, low
  - `title` — one-line finding summary
  - `body` — explanation and context
  - `suggested_fix_code` — optional code block rendered as GitHub/GitLab suggestion
- `owner` — repository owner (GitHub org/user or GitLab group)
- `repo` — repository name
- `pr_number` — GitHub PR number or GitLab MR IID
- `platform` — optional; "github" or "gitlab". Auto-detected from git remote if omitted.
- `sha` — optional; the full commit the review actually ran against. `post_review.py` stamps this into the prior-review marker, falling back to `git rev-parse HEAD` when absent. Always set it (the workflow's `prIdentity` wrapper already carries it) — if HEAD moved between the review and the post, the fallback records a commit no review examined, and the next run's incremental diff is scoped against it.

**Example workflow:**

```bash
# 1. Build findings JSON using Python (handles all escaping; no heredoc/Write tool issues)
# 2. Post review comments to PR
Bash(
  description="Posting {N} review comments to PR #{pr_number}",
  command="""python3 -c "
import json, sys
findings = {
    'review_body': 'Found 3 issues: 1 critical, 2 medium.',
    'findings': [
        {
            'file': 'app.js',
            'line': 42,
            'severity': 'critical',
            'title': 'SQL injection in query builder',
            'body': 'User input concatenated into SQL without parameterization.',
            'suggested_fix_code': 'const query = db.prepare(\'SELECT * FROM users WHERE id = ?\').get(id);'
        }
    ],
    'owner': 'myorg',
    'repo': 'myapp',
    'pr_number': 42,
    'sha': 'd4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0c1d2e3'
}
with open(sys.argv[1], 'w') as f:
    json.dump(findings, f, ensure_ascii=False, indent=2)
" "{output_dir}/code-gauntlet-post-review-input-{head_sha_short}.json"

python3 {plugin_root}/scripts/post_review.py "{output_dir}/code-gauntlet-post-review-input-{head_sha_short}.json"
""")
```

**Script behavior:**

- **GitHub:** Posts a single batched review with inline comments (event: COMMENT), then summary.
- **GitLab:** Posts a summary note, then per-finding inline discussions with position metadata.
- **Diff validation:** Parses diff to verify each finding line is in the PR/MR. Skips invalid lines with warning.
- **Metadata footer:** Appends both the prose `Generated by code-gauntlet | Reviewed up to: {sha}` line and the `code-gauntlet-findings` HTML comment to `review_body`, mechanically and idempotently (each half is skipped if already present).

### Findings metadata footer

`scripts/post_review.py` appends this to `review_body` — never hand-type it. `<!-- Canonical source: scripts/review_marker.py -->`. What the code writes (for reference):

```html
---
Generated by code-gauntlet | Reviewed up to: {full_sha}

<!-- code-gauntlet-findings: {"version":"3.0","findings_count":{N},"sha":"{full_sha}"} -->
```

The `findings` key is reserved/optional — present only when the caller supplies it (owned by issue #36; absent today).

---

## Task Creation

**Always let the user choose which findings become tasks.** Do NOT create tasks automatically. The interactive walkthrough in SKILL.md Phase 8 handles the selection UX — this section covers what happens after the user has made their selections.

> Headless exception (`CODE_GAUNTLET_HEADLESS=1`): the task board is skipped (Phase 8 Stage 2), so no tasks are created and this section does not run. See `references/headless-mode.md`.

### Create FIX tasks

Read `references/fix-task-metadata.md` for the full template. The process is:

1. **Detect toolchain** (Step 2.5) — scan for package.json, Cargo.toml, go.mod, etc.
2. **Detect patterns_to_follow** (Step 3a) — identify 1-2 nearby files as style references
3. **TaskCreate** (Step 3b) — structured description with Issue, Location, Evidence, Suggested Fix
4. **TaskUpdate with metadata** (Step 3c) — full cw-execute-compatible metadata

After creating: "Created N tasks from review findings."

---

## Markdown File

Write the full report to `./code-gauntlet-<date>.md` or a user-specified path.

---

## Chat

Print the full report in the conversation. For large reports, use collapsible sections for medium/low findings.

---

## Dismissed Findings

> Headless exception (`CODE_GAUNTLET_HEADLESS=1`): this entire flow is unreachable — no walkthrough runs, so dismissed_set is empty (Phase 8 Stage 3). Present neither `AskUserQuestion` below and never write REVIEW.md (read-only in headless mode). See `references/headless-mode.md`.

After delivery, ask if findings should be suppressed in future reviews:

```
AskUserQuestion(
  questions: [{
    question: "Should any of these findings be ignored in future reviews? This adds them to REVIEW.md so they won't be flagged again.",
    header: "Dismissed Findings",
    multiSelect: false,
    options: [
      { label: "Yes — let me pick which ones to dismiss", description: "Choose specific findings to suppress in future reviews" },
      { label: "No — all findings are valid", description: "Keep all findings active for future reviews" }
    ]
  }]
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
  questions: [{
    question: "Add these to REVIEW.md?",
    header: "Save to REVIEW.md",
    multiSelect: false,
    options: [
      { label: "Yes — add to REVIEW.md", description: "Write the dismissed findings to REVIEW.md now" },
      { label: "No — skip", description: "Discard the dismissals; findings remain active" }
    ]
  }]
)
```

If confirmed:

- If no REVIEW.md exists → offer to create using scaffolding template from `references/review-md-spec.md`
- If REVIEW.md exists without `## Ignore` → append the section
- If `## Ignore` exists → append new entries

After writing: "Added N dismissed findings to REVIEW.md. These won't be flagged in future reviews."

If declined, skip without modifying files.

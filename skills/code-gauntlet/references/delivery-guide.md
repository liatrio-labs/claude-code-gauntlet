# Delivery Guide

Implementation details for each delivery method in Phase 8.

---

## PR/MR Comments (platform-aware)

**Only comment on files in the diff.** The GitHub/GitLab API rejects inline comments on files not part of the PR/MR diff (HTTP 422). Before posting each inline comment, verify the file path is in the changed files list. Cross-file impact findings about files outside the diff go in the top-level summary comment with a note: "This finding references `path/to/file.cs` which is not in this PR's diff."

**Batch ALL inline comments into a single review event** — one GitHub notification instead of N separate ones. Notification fatigue causes teams to auto-dismiss AI review within ~10 days.

**The inline comment set is the pipeline's, not yours.** When the user answers "Post to PR/MR" at the Phase 8 delivery question, post the `artifactPaths.postReview` payload verbatim — `selectDelivery` already applied the delivery tier, ranked the survivors, and capped them at `limits.deliveryCap`. Do not re-rank, re-filter, or re-apply the cap. There is no per-finding selection pass: the choice is post or don't.

### Comment body format

**You do not compose the comment body.** `scripts/post_review.py::render_comment_body` builds it
from the fields you supply. Its output, for reference:

```
**{emoji} [{SEVERITY}] {finding.title}**

{body}

**Suggested fix:**
{suggestion}

**Cited rule:**
> {claude_md_rule, falling back to spec_text — blockquoted, one `>` line per source line}

```suggestion
{suggested_fix_code}
```

```

Every section after `{body}` is emitted only when its field is present — `null`, `""` and
whitespace-only all count as absent and produce no heading.

**`suggestion` always renders as prose; it never becomes a suggestion block.** Only
`suggested_fix_code` produces the committable ```suggestion fence, and no review-pipeline agent
emits that field. This used to be documented as a per-finding judgement call ("if `suggestion`
looks like code, fence it"), which is precisely the wrong shape: a ```suggestion fence is a
one-click APPLY button, so turning prose into one on a hunch writes the guess straight into the
author's branch. The rule is now structural — a fence comes from a field that exists to be a
patch, or it does not appear.

Severity emojis: 🔴 critical, 🟠 high, 🟡 medium, 💡 low.

### Using post_review.py

**Do NOT post PR comments via direct `gh api` or `glab api` calls.** Use the bundled `scripts/post_review.py` script instead. It handles platform detection, diff validation, and API calls deterministically.

**Usage:**
```bash
python3 {plugin_root}/scripts/post_review.py <findings_json_path>
```

**Findings JSON schema:**

> `suggested_fix_code` below is caller-supplied only — no review-pipeline agent emits it and no dispatch schema declares it. `post_review.py` still renders it when present, for callers that construct their own post-review JSON.

> `post_review.py` reads the **v2-aliased** field names (`body`, `line`, `end_line`).
> The persist boundary adds these aliases alongside the canonical names
> (`description`, `line_start`, `line_end`) — a union schema; existing keys are
> never overwritten. Report markdown uses the canonical names — see
> `report-format.md`.

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
            "body": "Detailed explanation and context",
            "suggestion": "prose fix advice (optional; renders as a **Suggested fix:** block)",
            "claude_md_rule": "the cited project rule (optional; renders as **Cited rule:**)",
            "spec_text": "the contradicted spec text (optional; renders as **Cited rule:** when there is no claude_md_rule)",
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
  - `body` — explanation and context (delivery alias of canonical `description`; see boundary note above)
  - `suggestion` — optional prose fix advice, rendered under a **Suggested fix:** heading. Carried on every finding the pipeline produces (canonical schema), so the delivery JSON should pass it straight through.
  - `claude_md_rule` / `spec_text` — optional; whichever survives sanitize renders under a **Cited rule:** heading as a blockquote (`claude_md_rule` preferred when both survive). These are how a convention or intent finding shows the reviewer the rule it is measured against.
  - `suggested_fix_code` — optional code block rendered as GitHub/GitLab suggestion; caller-supplied only, never populated by the review pipeline
  - Every optional field above treats `null`, `""` and whitespace-only identically to absent: no heading is emitted at all.
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
- **Exit status:** Non-zero when nothing could be delivered, and — GitLab only — when a `--dry-run` finds any malformed inline position. The dry-run payload is still written; read it for what was wrong.
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

**Never create tasks automatically.** The single Yes/No task-board question in Phase 8 Stage 2
(`references/phase8-delivery.md`) is the consent gate; this section covers what happens after a "Yes".

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

Canonical behavior is the "Markdown only" branch of Phase 8 Stage 1 in `phase8-delivery.md`: surface the path to the already-persisted `artifactPaths.report` — no default write. A write outside `{output_dir}` happens only when the user names a destination.

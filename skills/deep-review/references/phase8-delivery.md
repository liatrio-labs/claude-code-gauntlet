# Phase 8 Delivery Reference

Full UX orchestration flow for Phase 8: report delivery, PR comment selection, task board, and dismissed findings.

---

## Stage 0: Generate Report (Internal)

> **Generate the report internally.** You may output a brief summary to chat, but the full report is delivered per the method(s) selected in Phase 1.

**Read the delivery-ready findings** from `{output_dir}/deep-review-delivery-{head_sha_short}.json` (output of `apply_challenges.py`). This file contains the final ranked, deduped, capped findings with all challenge metadata applied. Do not reconstruct findings from memory — use this file as the source of truth for the report.

Read `references/report-format.md` for the full template and PR comment format.

The report includes: executive summary with finding counts (no verdict), severity-grouped findings, surfaced findings section, improvement suggestions section, per-dimension summary, and a **required** Review Methodology section documenting agents dispatched, model tier, validation stats, challenge results, and failures.

### Permalinks

Use platform-appropriate full-SHA permalink format:

- **GitHub:** `https://github.com/{owner}/{repo}/blob/{full_sha}/{path}#L{start}-L{end}`
- **GitLab:** `https://gitlab.com/{group}/{project}/-/blob/{full_sha}/{path}#L{start}-L{end}`

Always use the full 40-character SHA from `git rev-parse HEAD`.

---

## Stage 1: Deliver the Report

**Re-check eligibility** — verify the PR is still open. If closed/merged: deliver via chat/markdown only.

Deliver using the method(s) selected in Phase 1, in this order:

**Step A. Chat** — if selected, output the full report per `references/report-format.md`.

**Step B. PR comments** — if selected, run the PR comment selection flow before posting.

Complete the selection flow below before posting any PR comments.

> Headless exception (`DEEP_REVIEW_HEADLESS=1`): do not present this `AskUserQuestion`. Use selection=`default` — top `min($DEEP_REVIEW_PR_COMMENT_CAP, count)` main-report findings by severity then confidence, Improvement Suggestions excluded. The "Let me pick" walkthrough is unavailable. Posting obeys `$DEEP_REVIEW_POST_MODE` (`dry-run` ⇒ `post_review.py --dry-run`). See `references/headless-mode.md`.

```
AskUserQuestion(
  questions: [{
    question: "Which findings should I post as PR comments?",
    header: "PR Comments",
    multiSelect: false,
    options: [
      { label: "Default — top {min(6, finding_count)} by severity", description: "Post the highest-severity findings as inline comments" },
      { label: "Let me pick", description: "Walk through each finding and choose" }
    ]
  }]
)
```

- **"Default"** → top min(6, count) main-report findings by severity then confidence. Improvement Suggestions excluded.
- **"Let me pick"** → run the **interactive finding walkthrough** (see below). Includes Improvement Suggestions. All selected findings posted — no cap.

Track which findings were selected (**pr_comment_set**) for Stage 2 shortcut.

**Step B.1. Write findings JSON and run post_review.py**

Write the selected findings to a JSON file in the findings format specified in `references/delivery-guide.md`, then invoke the delivery script.

Use the Python json.dumps pattern — it handles all escaping and avoids Write tool "file not read" failures:

```bash
Bash(
  description="Posting {N} review comments to PR #{pr_number}",
  command="""python3 -c "
import json, sys
findings = {
    'review_body': '''REVIEW_BODY_HERE''',
    'findings': [
        {
            'file': 'src/foo.py',
            'line': 42,
            'end_line': 45,
            'severity': 'high',
            'title': '...',
            'body': '...',
            'suggested_fix_code': '...'
        }
    ],
    'owner': 'OWNER',
    'repo': 'REPO',
    'pr_number': PR_NUMBER
}
with open(sys.argv[1], 'w') as f:
    json.dump(findings, f, ensure_ascii=False, indent=2)
" "{output_dir}/deep-review-post-review-input-{head_sha_short}.json"

python3 {plugin_root}/scripts/post_review.py "{output_dir}/deep-review-post-review-input-{head_sha_short}.json"
""")
```

See `references/delivery-guide.md` for the findings JSON schema and validation details.

**Step C. Markdown file** — if selected, write to `./deep-review-{date}.md`.

---

## Stage 2: Task Board

The user decides whether to create tasks — always ask before finishing.

> Headless exception (`DEEP_REVIEW_HEADLESS=1`): the task board is skipped — present neither `AskUserQuestion` below and create no tasks. See `references/headless-mode.md`.

**If pr_comment_set exists:**

```
AskUserQuestion(
  questions: [{
    question: "Would you like to add any findings to the task board?",
    header: "Task Board",
    multiSelect: false,
    options: [
      { label: "Yes — from my PR comments", description: "Create a task for each finding I posted as a PR comment (F-01, F-02, ...)" },
      { label: "Yes — let me pick from all findings", description: "Walk through the full list using the summary table and choose" },
      { label: "No — done", description: "Finish the review" }
    ]
  }]
)
```

**If no pr_comment_set:**

```
AskUserQuestion(
  questions: [{
    question: "Would you like to add any findings to the task board?",
    header: "Task Board",
    multiSelect: false,
    options: [
      { label: "Yes — walk me through them", description: "Use the summary table above to select findings for the task board" },
      { label: "No — done", description: "Finish the review" }
    ]
  }]
)
```

When walking through findings for task creation, use the same summary table from the Interactive Finding Walkthrough (already shown to the user). Reference findings by their IDs (F-01, F-02, etc.) when describing which tasks will be created.

Create FIX tasks for all included findings using the task creation flow in `references/delivery-guide.md` (metadata per `references/fix-task-metadata.md`). After creating: "Created N tasks from review findings."

---

## Stage 3: Dismissed Findings

**Only run this stage if dismissed_set is non-empty** — i.e., the user explicitly skipped one or more findings during the Interactive Finding Walkthrough.

If dismissed_set is non-empty, ask whether to suppress those findings in future reviews. Pre-populate the proposed entries list from dismissed_set (the findings the user skipped), so the user does not have to re-identify them.

See `references/delivery-guide.md` for the full dismissed findings flow (AskUserQuestion template, proposed entries preview, REVIEW.md write logic).

**Stage 3 self-check:** After delivery and task board, verify Stage 3 (dismissed findings -> REVIEW.md suppression offer) was offered to the user. If dismissed_set is non-empty and you did not present the suppression prompt, go back and present it now before finishing the review.

> Headless exception (`DEEP_REVIEW_HEADLESS=1`): Stage 3 is unreachable — selection=`default` means no walkthrough runs, so dismissed_set is always empty. Skip the self-check and never write REVIEW.md (read-only in headless mode). See `references/headless-mode.md`.

---

## Interactive Finding Walkthrough

Reusable selection pattern for both PR comment selection (Stage 1 Step B) and task board selection (Stage 2).

> Headless exception (`DEEP_REVIEW_HEADLESS=1`): the walkthrough is unreachable — Stage 1 uses selection=`default` and Stage 2 (task board) is skipped, so neither caller invokes it. The per-finding `AskUserQuestion` below is never presented and dismissed_set stays empty. See `references/headless-mode.md`.

### Step 1: Show Summary Table

Before prompting for any selection, output the full findings table grouped by severity:

```
| # | Severity | Title | Confidence | File |
|---|----------|-------|------------|------|
| F-01 | 🔴 Critical | SQL injection in query builder | 94% | src/db.py:42 |
| F-02 | 🟠 High | Missing auth check on admin endpoint | 88% | src/routes.py:117 |
| F-03 | 🟡 Medium | Unhandled null in user lookup | 76% | src/users.py:33 |
| F-04 | 💡 Low | Deprecated API usage | 65% | src/legacy.py:8 |
```

List ALL findings from the main report (including Improvement Suggestions, which are listed after all bug/security findings). Group rows by severity: Critical first, then High, Medium, Low. Use finding IDs that match the report (e.g. F-01, F-02 or S-01, S-02 for surfaced).

### Step 2: Walk Through Each Severity Group

After showing the table, walk through each severity group one finding at a time.

For each finding, show:

```
AskUserQuestion(
  questions: [{
    question: "{emoji} {id}: {title}\n{file}:{lines} | Confidence: {N}%\n\n{one-sentence description}",
    header: "{emoji} {Severity} — finding {M} of {N}",
    multiSelect: false,
    options: [
      { label: "Include as PR comment", description: "Post this finding as an inline comment on the PR" },
      { label: "Skip this finding", description: "Remove from delivery, won't be posted" },
      { label: "Include all remaining {Severity}", description: "Auto-include all remaining {severity} findings without prompting" },
      { label: "Done — keep what I've selected", description: "Stop selection and deliver findings chosen so far" }
    ]
  }]
)
```

Emojis: critical=🔴, high=🟠, medium=🟡, low=💡.

**Option behavior:**

- **"Include as PR comment"** — add to selection set, advance to next finding
- **"Skip this finding"** — exclude from selection set, add to dismissed_set, advance to next finding
- **"Include all remaining {Severity}"** — auto-include all unreviewed findings in the current severity group, then advance to the next severity group
- **"Done — keep what I've selected"** — stop walkthrough immediately; deliver findings chosen so far

When all findings in a severity group are exhausted, advance automatically to the next severity group. When all severity groups are done, end the walkthrough.

Track skipped findings in **dismissed_set** for Stage 3 integration.

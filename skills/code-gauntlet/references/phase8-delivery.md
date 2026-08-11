# Phase 8 Delivery Reference

Full UX orchestration flow for Phase 8: report delivery, PR comment selection, task board, and dismissed findings.

---

## Stage 0: Collect Artifacts (from the workflow return)

> **The workflow already generated the report.** The Report stage rendered `report.md`; the main session puts it on disk (RETURN channel) or collects what the artifact-writer persisted, and in neither case re-generates it. You may output a brief summary to chat, but the full report is delivered per the method(s) selected in Phase 1.

The Phase 3 `Workflow` call returned a compact object that always includes a `checkpoints` field alongside `artifactPaths`:

```
{ ok, phaseReached, stats, artifactPaths: { findings, report, checkpoints }, resolvedPolicy, gaps, checkpoints,
  persistReturn }   // RETURN channel only
```

**When `persistReturn` is present, run `materialize_artifacts.py` before anything below** — the artifacts do not exist yet. SKILL.md Phase 8 → "Materialize the artifacts" owns the command and its exit-code table. The short version: the pipeline returned its primaries instead of dictating them to an artifact-writer, the harness put that return on disk at `tasks/<task-id>.output`, and this one command writes the primaries out of it and derives `post-review.json` + `checkpoint-all.json` from them. Exit 0 means everything landed and every content proof matched.

Never hand-write an artifact from `persistReturn`'s contents. The whole channel exists because a model transcribing those bytes loses 36% of them, most damagingly by rewriting long prose shorter with the schema intact — which is why `await_workflow.py` elides them from its stdout and only the path reaches you.

**On `ok: true` (writer succeeded):** read the artifacts — they are the source of truth for delivery. Do not reconstruct, re-filter, or re-rank findings from the return value or from memory.

- `artifactPaths.postReview` — the pipeline's **pre-selected delivery payload**: the challenge-survivors chosen by the delivery tier (`args.delivery.tier` — `all` (default) keeps every survivor, `main_only` keeps main-tagged only), ranked by `selectDelivery` and truncated to `limits.deliveryCap`, each carrying its `report_tag`. Same **union schema** as the findings file, so `post_review.py` consumes it unchanged. This is the PR-comment set — post every entry as a comment, verbatim; the live agent never re-selects.
- `artifactPaths.findings` — the full persisted findings JSON (every high-confidence survivor). It carries the **union schema**: the v2 aliases `line`/`end_line`/`body` alongside the canonical `line_start`/`line_end`/`description`, so `post_review.py` consumes it unchanged. The interactive "Let me pick" walkthrough selects from `artifactPaths.postReview` (deselection from the pipeline's delivery set), not from this full file — Step B.1 replaces the postReview wrapper's `findings` with a strict subset of that wrapper's entries.
- `artifactPaths.report` — the rendered report markdown (already includes the severity-grouped findings, surfaced section, improvement suggestions, per-dimension summary, and Review Methodology).
- The return's own `checkpoints` is just `{ completed: [...] }` (phase names). A **slim** resume checkpoint (`{ phases, completed, phaseReached, counts }` — full output only for the resume-consumed `challenge` phase, plus a per-phase `counts` map for every phase including `filter`) is persisted at `artifactPaths.checkpoints`. Read that file if a later re-run needs to resume a successful-but-superseded run: it reuses the delivered `challenge` findings verbatim and re-runs the upstream phases (discover/verify/validate/**filter**/report) — `filter` is deliberately not persisted: it is a pure, agent-free JS function, so re-running it on resume costs nothing (issue #38, P1). The fast full-skip resume map still rides back **in-memory** on the failure path below.

**On `ok: false`, or `ok: true` with a partial-artifacts gap** (writer failed, `artifactPaths` empty/null): the run reached `phaseReached` but did not finish, and nothing was persisted — so the resume state rides back **in the return's `checkpoints` field**, not on disk. Offer **resume-from-checkpoint** before delivering anything partial:

1. Inspect `return.checkpoints`.
   - Has a `.phases` map → re-invoke the same `Workflow(scriptPath, args)` call with `args.checkpoints` set to `return.checkpoints`. The workflow skips every already-completed phase (it unwraps `.phases`) and resumes at the first missing one.
   - Is `{ completed, truncated: true }` (the phase-outputs map exceeded the ~1M-char return budget, so the workflow withheld the findings bulk) → there is no phase map and nothing was persisted; **re-run from scratch** (re-invoke without `args.checkpoints`) and note the truncation in the methodology.
2. If resume is declined or fails again, deliver whatever `artifactPaths.report` exists via chat and report the `gaps`.

> Headless exception (`CODE_GAUNTLET_HEADLESS=1`): never prompt. Auto-resume **once** when `return.checkpoints` has a `.phases` map; otherwise (truncated, or the retry also fails) deliver the partial report + `gaps` and stop. See `references/headless-mode.md`.

**Surface `gaps` in the methodology regardless of `ok`** — each entry is a degraded/skipped stage (unverified findings, skipped validation batch, capped challenges, minimal report, partial artifacts).

Read `references/report-format.md` for the report template and PR comment format. If the persisted report is the deterministic **minimal report** (a report-writer failure — indicated by a report gap), note that in delivery: it lists findings from pipeline stats without the full narrative.

### Methodology inputs

The methodology section must disclose: **plugin version** (`.claude-plugin/plugin.json` `version`), **PIPELINE_VERSION** (the `PIPELINE_VERSION` constant in `workflows/pipeline.js`), **per-stage models** (from `resolvedPolicy` — a `subagentModel` override if present, else the S5 defaults; when `resolvedPolicy.provider` is not `firstParty`, note that agents dispatched bare aliases resolved by the provider's deployment mapping rather than pinned first-party model IDs), the **effective config** (delivery, limits), the **review scope** (`Full`, or `Incremental since {sha} (N commits)` — the workflow's Report stage has no knowledge of the previously-reviewed gate, so the orchestrator appends this line at delivery, not the pipeline), and `stats`/`gaps`. If `resolvedPolicy.subagentModel` is set, disclose it prominently — `CLAUDE_CODE_SUBAGENT_MODEL` overrode every per-stage model.

### Permalinks

Use platform-appropriate full-SHA permalink format:

- **GitHub:** `https://github.com/{owner}/{repo}/blob/{full_sha}/{path}#L{start}-L{end}`
- **GitLab:** `https://gitlab.com/{group}/{project}/-/blob/{full_sha}/{path}#L{start}-L{end}`

Always use the full 40-character SHA from `git rev-parse HEAD`.

---

## Stage 1: Deliver the Report

**Re-check eligibility** — verify the PR is still open. If closed/merged: deliver via chat/markdown only.

> Headless exception (`CODE_GAUNTLET_HEADLESS=1`): the closed/merged chat/markdown-only restriction does not apply — headless delivers per `CODE_GAUNTLET_DELIVERY` regardless of PR state (a merged PR is still delivered via `pr_comments`, which in `dry-run` captures the payload without posting). Posting obeys `CODE_GAUNTLET_POST_MODE`. See `references/headless-mode.md`.

Deliver using the method(s) selected in Phase 1, in this order:

**Step A. Chat** — if selected, output the full report per `references/report-format.md`. **If Chat was NOT selected, cap chat output to a short completion summary** (finding counts, artifact paths, methodology pointer) — do not print the full report into the conversation; the user chose where the results go, and a full-report chat dump on a "PR comments"-only selection ignores that choice (observed live, PR-310 run).

**Step B. PR comments** — if selected, run the PR comment selection flow before posting.

The delivery set is the pipeline's pre-selected `artifactPaths.postReview` payload — the survivors already chosen per the Phase 1 delivery tier (`args.delivery.tier`: `all` (default) keeps every survivor, `main_only` keeps main-tagged only), then ranked and capped at `limits.deliveryCap`. **Every finding in that payload posts as a PR comment** — suggestions are not a separate delivery destination; the `report_tag` affects only where a finding renders in the report ("Improvement Suggestions" section) and, under `main_only`, whether the pipeline already withheld it. Never re-filter by tag, re-rank, or re-apply the cap to this payload.

> Headless exception (`CODE_GAUNTLET_HEADLESS=1`): do not present this `AskUserQuestion`. Post the `artifactPaths.postReview` payload **verbatim** — the workflow already applied the delivery tier (`$CODE_GAUNTLET_DELIVERY_TIER`, default `all`) plus rank + cap `$CODE_GAUNTLET_PR_COMMENT_CAP`. The "Let me pick" walkthrough is unavailable. Posting obeys `$CODE_GAUNTLET_POST_MODE` (`dry-run` ⇒ `post_review.py --dry-run`). See `references/headless-mode.md`.

```
AskUserQuestion(
  questions: [{
    question: "Which findings should I post as PR comments?",
    header: "PR Comments",
    multiSelect: false,
    options: [
      { label: "Default — the pipeline's selected set ({postReview_count})", description: "Post the pre-selected postReview payload (the Phase 1 tier's survivors, ranked and capped) verbatim" },
      { label: "Let me pick", description: "Walk through each finding and choose" }
    ]
  }]
)
```

- **"Default"** → post the `artifactPaths.postReview` payload verbatim (the tier's survivors, already ranked and capped at `limits.deliveryCap`). Do not re-select.
- **"Let me pick"** → run the **interactive finding walkthrough** (see below) over the `artifactPaths.postReview` payload. Includes any Improvement Suggestions the pipeline already kept in that set. The user hand-selects a strict subset; all selected findings posted — no additional cap. This is user-driven deselection from the pipeline's delivery set, not agent re-filtering and not a second pass over the uncapped `artifactPaths.findings` file.

Track which findings were selected (**pr_comment_set**) for Stage 2 shortcut.

**Step B.1. Write findings JSON and run post_review.py**

Write the selected findings to a JSON file in the findings format specified in `references/delivery-guide.md`, then invoke the delivery script. **When `delivery.prIdentity` was set in the args waist, the persisted `artifactPaths.postReview` file already IS the post_review-ready wrapper** (`{ owner, repo, pr_number, sha, review_body, findings }`) — consume it directly: optionally set `review_body` to the composed summary (it persists as `""`), keep its `sha` field (it pins the marker to the commit the review ran against), and for the **default** selection pass the file to `post_review.py` unchanged. For **"Let me pick"** the user's deselections apply to the wrapper too: replace the wrapper's `findings` array with the user's chosen subset (a strict subset of the wrapper's entries, order preserved — deselection only, never re-ranking or re-filtering), keep every other wrapper field, then post. Only when the artifact is the legacy bare findings array (no prIdentity — e.g. a local-diff review that later gains a PR target) do you hand-wrap: for the **default** selection the "selected findings" are the `artifactPaths.postReview` entries **verbatim** — do not drop, reorder, or cap them; only wrap them with `review_body`, `owner`, `repo`, `pr_number`, and `sha` (the full head SHA the review ran against, from Phase 2 — omitting it leaves `post_review.py` to fall back to `git rev-parse HEAD`, which may not be the commit reviewed). For "Let me pick", they are the user's chosen subset.

Use the Python json.dumps pattern — it handles all escaping and avoids Write tool "file not read" failures. Pass `suggestion` and `claude_md_rule`/`spec_text` straight through from the finding when present: `post_review.py` renders them as the comment's **Suggested fix:** block and **Cited rule:** heading plus blockquote (one `>` line per source line; capped at 500 chars), and a finding that carries them loses the reviewer-facing half of itself if the hand-built wrapper drops them. `claude_md_rule` and `spec_text` are alternatives for the cited-rule section (`claude_md_rule` is preferred when both *survive sanitize* — a comment-only rule falls through to `spec_text`) — a finding typically carries one, not both. (`suggested_fix_code` below is caller-supplied only — no review-pipeline agent emits it; omit it unless you are hand-constructing this JSON yourself.)

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
            'suggestion': '...',
            'claude_md_rule': '...',
            'spec_text': '...',
            'suggested_fix_code': '...'
        }
    ],
    'owner': 'OWNER',
    'repo': 'REPO',
    'pr_number': PR_NUMBER,
    'sha': 'FULL_HEAD_SHA'
}
with open(sys.argv[1], 'w') as f:
    json.dump(findings, f, ensure_ascii=False, indent=2)
" "{output_dir}/code-gauntlet-post-review-input-{head_sha_short}.json"

python3 {plugin_root}/scripts/post_review.py "{output_dir}/code-gauntlet-post-review-input-{head_sha_short}.json"
""")
```

> Headless carve-out (`CODE_GAUNTLET_POST_MODE=dry-run`): append `--dry-run` to the `post_review.py` invocation so it captures the payload instead of posting. `post_review.py` self-enforces this regardless — it reads `CODE_GAUNTLET_POST_MODE` directly and treats `dry-run` as `--dry-run` even when the flag is omitted (belt-and-braces) — but pass the flag explicitly so the dry-run intent is visible in the command.

See `references/delivery-guide.md` for the findings JSON schema and validation details.

**Step C. Markdown file** — if selected, **do not write a new file**. The full report is already persisted at `artifactPaths.report` (e.g. `{output_dir}/code-gauntlet-report-{head_sha_short}.md`). Tell the user that absolute path in chat — that is the delivery. The only allowed write outside `{output_dir}` is when the user explicitly names a destination path; then copy/write the report there. Never invent a root-level `./code-gauntlet-{date}.md` (or any other default path outside `{output_dir}`).

---

## Stage 2: Task Board

The user decides whether to create tasks — always ask before finishing.

> Headless exception (`CODE_GAUNTLET_HEADLESS=1`): the task board is skipped — present neither `AskUserQuestion` below and create no tasks. See `references/headless-mode.md`.

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

> Headless exception (`CODE_GAUNTLET_HEADLESS=1`): Stage 3 is unreachable — selection=`default` means no walkthrough runs, so dismissed_set is always empty. Skip the self-check and never write REVIEW.md (read-only in headless mode). See `references/headless-mode.md`.

---

## Interactive Finding Walkthrough

Reusable selection pattern for both PR comment selection (Stage 1 Step B) and task board selection (Stage 2).

> Headless exception (`CODE_GAUNTLET_HEADLESS=1`): the walkthrough is unreachable — Stage 1 uses selection=`default` and Stage 2 (task board) is skipped, so neither caller invokes it. The per-finding `AskUserQuestion` below is never presented and dismissed_set stays empty. See `references/headless-mode.md`.

### Step 1: Show Summary Table

Before prompting for any selection, output the findings table grouped by severity — **the rows are the `artifactPaths.postReview` entries**, not the uncapped `artifactPaths.findings` file:

```
| # | Severity | Title | Confidence | File |
|---|----------|-------|------------|------|
| F-01 | 🔴 Critical | SQL injection in query builder | 94% | src/db.py:42 |
| F-02 | 🟠 High | Missing auth check on admin endpoint | 88% | src/routes.py:117 |
| F-03 | 🟡 Medium | Unhandled null in user lookup | 76% | src/users.py:33 |
| F-04 | 💡 Low | Deprecated API usage | 65% | src/legacy.py:8 |
```

List every entry from the postReview payload (including Improvement Suggestions the pipeline kept under the delivery tier). Group rows by severity: Critical first, then High, Medium, Low. Use finding IDs that match the report (e.g. F-01, F-02 or S-01, S-02 for surfaced).

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

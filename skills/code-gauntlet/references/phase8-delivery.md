# Phase 8 Delivery Reference

Full UX orchestration flow for Phase 8: report delivery, PR comment posting, and the task board — the two
questions of the run.

---

## Stage 0: Collect Artifacts (from the workflow return)

> **The workflow already generated the report.** The Report stage rendered `report.md`; the main session puts it on disk (RETURN channel) or collects what the artifact-writer persisted, and in neither case re-generates it. You may output a brief summary to chat, but the full report is delivered per the Phase 8 delivery question (Stage 1 below).

The Phase 3 `Workflow` call returned a compact object that always includes a `checkpoints` field alongside `artifactPaths`:

```
{ ok, phaseReached, stats, artifactPaths: { findings, report, checkpoints }, resolvedPolicy, gaps, checkpoints,
  persistReturn }   // RETURN channel only
```

**All-degraded mode (issue #178):** an `error` prefixed `all-degraded:` means every active discovery dimension failed and **no review was performed** — deliver it as a hard failure via resume-from-checkpoint below, never as a clean/empty report. The per-agent `gaps` and `resolvedPolicy` (model/provider actually resolved) carry the diagnosis — a model/provider mismatch is the usual cause. The resume re-dispatches discovery because the `discover` checkpoint is deliberately absent from `return.checkpoints`.

**When `persistReturn` is present, run `materialize_artifacts.py` before anything below** — the artifacts do not exist yet. SKILL.md Phase 8 → "Materialize the artifacts" owns the command and its exit-code table. The short version: the pipeline returned its primaries instead of dictating them to an artifact-writer, the harness put that return on disk at `tasks/<task-id>.output`, and this one command writes the primaries out of it and derives `post-review.json` + `checkpoint-all.json` from them. Exit 0 means everything landed and every content proof matched.

Never hand-write an artifact from `persistReturn`'s contents. The whole channel exists because a model transcribing those bytes loses 36% of them, most damagingly by rewriting long prose shorter with the schema intact — which is why `await_workflow.py` elides them from its stdout and only the path reaches you.

**Then render the apply-checked patches** whenever `artifactPaths.findings` is non-null — SKILL.md Phase 8 → "Render apply-checked patches" owns the command, the exit-code table, and where the patches path is surfaced in delivery.

**On `ok: true` (writer succeeded):** read the artifacts — they are the source of truth for delivery. Do not reconstruct, re-filter, or re-rank findings from the return value or from memory.

- `artifactPaths.postReview` — the pipeline's **pre-selected delivery payload**: the challenge-survivors chosen by the delivery tier (`args.delivery.tier` — `all` (default) keeps every survivor, `main_only` keeps main-tagged only), ranked by `selectDelivery` and truncated to `limits.deliveryCap`, each carrying its `report_tag`. Same **union schema** as the findings file, so `post_review.py` consumes it unchanged. This is the PR-comment set — post every entry as a comment, verbatim; the live agent never re-selects.
- `artifactPaths.findings` — the full persisted findings JSON (every high-confidence survivor). It carries the **union schema**: the v2 aliases `line`/`end_line`/`body` alongside the canonical `line_start`/`line_end`/`description`, so `post_review.py` consumes it unchanged. Delivery never selects from this full file: the posted set is `artifactPaths.postReview`, posted verbatim.
- `artifactPaths.report` — the rendered report markdown: the report-writer's opening summary, the high-confidence findings, the unverified/pipeline-degraded secondary section, and (segment 0 only) the pre-rendered Review Dimensions Summary table. It carries no Review Methodology section — the report-writer's contract never emits one; that section is composed by the orchestrator at delivery (see "Methodology inputs" below) and is never part of the persisted report bytes.
- The return's own `checkpoints` is just `{ completed: [...] }` (phase names). A **slim** resume checkpoint (`{ phases, completed, phaseReached, counts }` — full output only for the resume-consumed `challenge` phase, plus a per-phase `counts` map for every phase including `filter`) is persisted at `artifactPaths.checkpoints`. Read that file if a later re-run needs to resume a successful-but-superseded run: it reuses the delivered `challenge` findings verbatim and re-runs the upstream phases (discover/verify/validate/**filter**/report) — `filter` is deliberately not persisted: it is a pure, agent-free JS function, so re-running it on resume costs nothing (issue #38, P1). The fast full-skip resume map still rides back **in-memory** on the failure path below.

**On `ok: false`, or `ok: true` with a partial-artifacts gap** (writer failed, `artifactPaths` empty/null): the run reached `phaseReached` but did not finish, and nothing was persisted — so the resume state rides back **in the return's `checkpoints` field**, not on disk. Offer **resume-from-checkpoint** before delivering anything partial:

1. Inspect `return.checkpoints`.
   - Has a `.phases` map → re-invoke the same `Workflow(scriptPath, args)` call with `args.checkpoints` set to `return.checkpoints`. The workflow skips every already-completed phase (it unwraps `.phases`) and resumes at the first missing one.
   - Is `{ completed, truncated: true }` (the phase-outputs map exceeded the ~1M-char return budget, so the workflow withheld the findings bulk) → there is no phase map and nothing was persisted; **re-run from scratch** (re-invoke without `args.checkpoints`) and note the truncation in the methodology.
2. If resume is declined or fails again, deliver whatever `artifactPaths.report` exists markdown-only —
   report the path plus a short chat summary — and report the `gaps`.

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

**Re-check eligibility** — verify the PR is still open. If closed/merged: deliver markdown-only — report the
path of `artifactPaths.report` plus a short chat summary, and skip posting.

> Headless exception (`CODE_GAUNTLET_HEADLESS=1`): the closed/merged markdown-only restriction does not apply — headless delivers per `CODE_GAUNTLET_DELIVERY` regardless of PR state (a merged PR is still delivered via `pr_comments`, which in `dry-run` captures the payload without posting). Posting obeys `CODE_GAUNTLET_POST_MODE`. See `references/headless-mode.md`.

**Question 1 of 2 — Delivery.** The report already exists on disk; the only open question is whether the
findings also post to the PR. Ask once, after materialization, before anything is posted:

> Headless exception (`CODE_GAUNTLET_HEADLESS=1`): do not present this `AskUserQuestion`. Deliver per
> `CODE_GAUNTLET_DELIVERY` and post `artifactPaths.postReview` **verbatim** — the workflow already applied
> the delivery tier (`$CODE_GAUNTLET_DELIVERY_TIER`, default `all`) plus rank + cap
> `$CODE_GAUNTLET_PR_COMMENT_CAP`. Posting obeys `$CODE_GAUNTLET_POST_MODE` (`dry-run` ⇒
> `post_review.py --dry-run`). See `references/headless-mode.md`.

```
AskUserQuestion(
  questions: [{
    question: "The review is done and the report is saved. Post the {postReview_count} selected findings to PR #{pr_number} as inline comments?",
    header: "Delivery",
    multiSelect: false,
    options: [
      { label: "Post to PR (Recommended)", description: "Post the pipeline's selected set as inline comments, batched into one review event" },
      { label: "Markdown only", description: "Skip posting — I'll give you the path to the saved report" }
    ]
  }]
)
```

For a GitLab target, say `MR !{pr_number}` in the question and `Post to MR (Recommended)` in the label.
**When the PR is closed or merged, or the target is a local diff, do not ask** — there is nothing to post
to; deliver markdown-only and say so. That is the same closed/merged rule as before, now expressed by
skipping the question instead of narrowing its options.

- **"Post to PR"** → post `artifactPaths.postReview` **verbatim**. This is the pipeline's pre-selected
  set: the survivors chosen by `args.delivery.tier`, ranked by `selectDelivery`, capped at
  `limits.deliveryCap`. There is no per-finding walkthrough and no deselection UI — the user's choice is
  post or don't. Never re-filter by tag, re-rank, or re-apply the cap.
- **"Markdown only"** → post nothing and **do not write a new file**. The full report is already
  persisted at `artifactPaths.report` (e.g. `{output_dir}/code-gauntlet-report-{head_sha_short}.md`). Tell
  the user that absolute path in chat — that is the delivery. The only allowed write outside
  `{output_dir}` is when the user explicitly names a destination path; then copy/write the report there.
  Never invent a root-level `./code-gauntlet-{date}.md` (or any other default path outside
  `{output_dir}`).

Either way, print a short completion summary to chat: finding counts by severity, the report path, and the
methodology pointer. Never dump the full report into the conversation unprompted.

**Post via post_review.py — only on a "Post to PR/MR" answer.** A "Markdown only" answer ends Stage 1 at
the completion summary above: skip this whole posting step, run nothing, and go straight to Stage 2. (A
headless run reaches this step only when `CODE_GAUNTLET_DELIVERY` includes `pr_comments`, and its posting
obeys `CODE_GAUNTLET_POST_MODE`.)

**When `delivery.prIdentity` was set in the args waist, the persisted `artifactPaths.postReview` file
already IS the post_review-ready wrapper** (`{ owner, repo, pr_number, sha, review_body, findings }`) —
consume it directly: optionally set `review_body` to the composed summary (it persists as `""`), keep its
`sha` field (it pins the marker to the commit the review ran against), and pass the file to
`post_review.py` unchanged. Only when the artifact is the legacy bare findings array (no `prIdentity` —
e.g. a local-diff review that later gains a PR target) do you hand-wrap: the findings are the
`artifactPaths.postReview` entries **verbatim** — do not drop, reorder, or cap them; only add
`review_body`, `owner`, `repo`, `pr_number`, and `sha` (the full head SHA the review ran against, from
Phase 2 — omitting it leaves `post_review.py` to fall back to `git rev-parse HEAD`, which may not be the
commit reviewed).

Use the Python json.dumps pattern — it handles all escaping and avoids Write tool "file not read" failures. Pass `suggestion` and `claude_md_rule`/`spec_text` straight through from the finding when present: `post_review.py` renders them as the comment's **Suggested fix:** block and **Cited rule:** heading plus blockquote (one `>` line per source line; capped at 500 chars), and a finding that carries them loses the reviewer-facing half of itself if the hand-built wrapper drops them. `claude_md_rule` and `spec_text` are alternatives for the cited-rule section (`claude_md_rule` is preferred when both *survive sanitize* — a comment-only rule falls through to `spec_text`) — a finding typically carries one, not both. Pass `suggested_fix_code` straight through too when the finding carries it: `post_review.py` runs it through a deterministic apply-check before ever rendering it as a committable `suggestion` fence, downgrading silently to the prose `suggestion` on any failure, so passing it through is always safe — never hand-construct or edit its value yourself.

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

---

## Stage 2: Task Board

**Question 2 of 2 — and the last question of the run.** One call, Yes/No shaped. Never a per-finding loop.

> Headless exception (`CODE_GAUNTLET_HEADLESS=1`): the task board is skipped — present no
> `AskUserQuestion` and create no tasks. See `references/headless-mode.md`.

```
AskUserQuestion(
  questions: [{
    question: "Create fix tasks on the task board from these findings?",
    header: "Task Board",
    multiSelect: false,
    options: [
      { label: "Yes — create tasks", description: "One FIX task per delivered finding (F-01, F-02, ...)" },
      { label: "No — done", description: "Finish the review without creating tasks" }
    ]
  }]
)
```

"Yes" creates a FIX task for every finding in the delivered set — the `artifactPaths.postReview` entries
when the user chose "Post to PR", otherwise the same payload's entries as listed in the report. There is
no third "let me pick" option: hand-selection was the unbounded question loop this issue removed. A user
who wants a subset says so in chat and you create that subset.

Create the tasks using the task-creation flow in `references/delivery-guide.md` (metadata per
`references/fix-task-metadata.md`). After creating: "Created N tasks from review findings."

The `ignore:` list is user-edited by hand per the #94 contract (`references/review-md-spec.md` →
Ignore); no Phase 8 write path exists.

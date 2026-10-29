# Phase 8 Delivery Reference

## Contents

- [Stage 0: Collect Artifacts (from the workflow return)](#stage-0-collect-artifacts-from-the-workflow-return)
- [Stage 1: Deliver the Report](#stage-1-deliver-the-report)
- [Stage 2: Task Board](#stage-2-task-board)

Full UX orchestration flow for Phase 8: report delivery, PR comment posting, and the task board — the two
questions of the run.

---

## Stage 0: Collect Artifacts (from the workflow return)

> **The workflow already generated the report.** The Report stage rendered `report.md`; the main session puts it on disk (RETURN channel) or collects what the artifact-writer persisted, and in neither case re-generates it. You may output a brief summary to chat, but the full report is delivered per the Phase 8 delivery question (Stage 1 below).

The Phase 3 `Workflow` call returned a compact object that always includes a `checkpoints` field alongside `artifactPaths`:

```
{ ok, phaseReached, stats, artifactPaths: { findings, report, postReview, checkpoints }, resolvedPolicy, gaps, checkpoints,
  persistReturn }   // RETURN channel only
```

**All-degraded mode:** an `error` prefixed `all-degraded:` means every active discovery dimension failed and **no review was performed** — deliver it as a hard failure via resume-from-checkpoint below, never as a clean/empty report. The per-agent `gaps` and `resolvedPolicy` (model/provider actually resolved) carry the diagnosis — a model/provider mismatch is the usual cause. The resume re-dispatches discovery because the `discover` checkpoint is deliberately absent from `return.checkpoints`.

**When `persistReturn` is present, run `materialize_artifacts.py` before anything below** — the artifacts do not exist yet. [SKILL.md: Phase 8: Materialize, collect, patch, deliver](../SKILL.md#phase-8-materialize-collect-patch-deliver) owns the command and its exit-code table. The short version: the pipeline returned its primaries instead of dictating them to an artifact-writer, the harness put that return on disk at `tasks/<task-id>.output`, and this one command writes the primaries out of it and derives `post-review.json` + `checkpoint-all.json` from them. Exit 0 means everything landed and every content proof matched.

Never hand-write an artifact from `persistReturn`; `await_workflow.py` elides its entries so the original serialized text reaches `materialize_artifacts.py` without model transcription.

Findings with skipped validation or unknown verify origin are challenged, then disposed as `held_back` to the report's Unverified / pipeline-degraded section alongside capped or failed challenges. They remain in findings.json with their `disposition`, which grants no PR-comment, patch, or fix-task rights. Old challenge checkpoints pass through the same disposal before delivery.

**Then render the apply-checked patches** whenever `artifactPaths.findings` is non-null — [SKILL.md: Phase 8: Materialize, collect, patch, deliver](../SKILL.md#phase-8-materialize-collect-patch-deliver) owns the command, its exit-code rules, and where the patches path is surfaced in delivery.

**On `ok: true` after materialization or writer persistence:** read the artifacts — they are the source of truth for delivery. Do not reconstruct, re-filter, or re-rank findings from the return value or from memory.

- `artifactPaths.postReview` — the pipeline's **pre-selected delivery payload**: the challenge-survivors chosen by the delivery tier (`args.delivery.tier` — `all` (default) keeps every survivor, `main_only` keeps main-tagged only), ranked by `selectDelivery` and truncated to `limits.deliveryCap`, each carrying its `report_tag`. Same **union schema** as the findings file, so `post_review.py` consumes it unchanged. This is the PR-comment set — post every entry as a comment, verbatim; the live agent never re-selects.
- `artifactPaths.findings` — every disposed record, including body-routed, held-back, report-only, and eliminated findings; rights live in `disposition`. It carries the **union schema**: the v2 aliases `line`/`end_line`/`body` alongside the canonical `line_start`/`line_end`/`description`, so `post_review.py` consumes it unchanged. Delivery never selects from this full file: the posted set is `artifactPaths.postReview`, posted verbatim.
- `artifactPaths.report` — the rendered report markdown, produced **in code** by
  `renderReport()` (`workflows/src/renderReport.js`): title, identity line, Summary, Findings
  by severity, the Unverified/pipeline-degraded section, the Review Dimensions Summary, and
  the last-section Review Methodology with its code-rendered identity receipt and methodology
  table. The report is complete when materialized; the orchestrator never appends to `report.md`.
- `artifactPaths.checkpoints` holds the signed slim resume object. The return's own `checkpoints` contains only completed phase names. See [crash recovery](crash-recovery.md#3-checkpoint-resume-fallback-when-resumefromrunid-is-unavailable).

**On failure, partial artifacts, or a checkpoint refusal:** follow [crash recovery](crash-recovery.md#3-checkpoint-resume-fallback-when-resumefromrunid-is-unavailable). That reference owns whole-object copying (`checksum` included), checksum re-copy, and interactive/headless retry limits. Never edit the checkpoint.

Code renders the integer gap count in report methodology regardless of `ok`. Chat points to that section and adds only gaps arising after the report stage. Each entry names a degraded or skipped stage (unverified findings, skipped validation batch, capped challenges, partial artifacts).

Read `references/report-format.md` for the PR comment format.

### Methodology inputs

The renderer owns the methodology section and receives its inputs through the args waist: plugin
and pipeline version, resolved policy and per-stage models, effective delivery selection,
review scope, stage and merge statistics, the integer gap count, and dispatched/degraded
dimensions. The chat methodology at delivery points to this report section and adds only what
the orchestrator knows after the report stage: materialization proof, the patches path, delivery
outcome, post-report gaps, and wall-clock duration. It never reconstructs or appends report
content.

### Permalinks

<!-- generated-from-registry-identity:permalink_formats — do not edit; run scripts/generate_contract_requirements.py -->
- `github`: blob `{origin}/{owner}/{repo}/blob/{sha}/{path}`; line `#L{start}`; range `#L{start}-L{end}`; ref `#{number}`; ref URL `{origin}/{owner}/{repo}/pull/{number}`.
- `gitlab`: blob `{origin}/{owner}/{repo}/-/blob/{sha}/{path}`; line `#L{start}`; range `#L{start}-{end}`; ref `!{number}`; ref URL `{origin}/{owner}/{repo}/-/merge_requests/{number}`.
- Encode owner, repo, and file paths one segment at a time with `encodeURIComponent` semantics; also percent-encode `!`, `'`, `(`, `)`, and `*`, while preserving `/` separators. A file path containing an empty, `.`, or `..` segment renders as a plain code span.
<!-- /generated-from-registry-identity:permalink_formats -->

---

## Stage 1: Deliver the Report

**Re-check eligibility** — verify the PR is still open. If closed/merged: deliver markdown-only — report the
path of `artifactPaths.report` plus a short chat summary, and skip posting.

> Headless mode exception: the closed/merged markdown-only restriction does not apply. Delivery follows `configResult.resolved.delivery`, and posting follows `configResult.resolved.post_mode`. See `references/headless-mode.md`.

**Question 1 of 2 — Delivery.** The report already exists on disk; the only open question is whether the
findings also post to the PR. Ask once, after materialization, before anything is posted:

> Headless mode exception: do not present this `AskUserQuestion`; deliver per `configResult.resolved.delivery`, with `selectDelivery` applying the receipt-backed tier and cap after Challenge and before Phase 8 posts `artifactPaths.postReview` verbatim.
> See `references/headless-mode.md`.

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
to; deliver markdown-only and say so.

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

Every run except headless chat delivery prints a short completion summary to chat: finding counts by
severity, the report path, and the methodology pointer. Headless chat delivery includes the full report
per [delivery-guide.md: Chat](delivery-guide.md#chat).

**Post via post_review.py — only on a "Post to PR/MR" answer.** A "Markdown only" answer ends Stage 1 at
the completion summary above: skip this whole posting step, run nothing, and go straight to Stage 2. (A
headless run reaches this step only when `configResult.resolved.delivery` includes `"pr_comments"`, and its posting
follows `configResult.resolved.post_mode`.)

When `delivery.prIdentity` is set in the args waist, the persisted `artifactPaths.postReview` file
already is the post_review-ready wrapper (`{ owner, repo, pr_number, sha, platform, web_origin, review_body, findings }`).
Pass that file to `post_review.py` unchanged. Its `review_body` is exactly the report's outcome-first Summary section body: counts, selected findings index, and any remainder. Keep its `sha` because it pins the prior-review marker to the reviewed commit. Code-owned byte budgets and folding are documented in [delivery rendering](../../../docs/delivery-rendering.md).

When the artifact is the legacy bare findings array, invoke `post_review.py` with `--report` and the
identity flags. The script derives the Summary body from the report and forms the wrapper in code:

```bash
{python} "{plugin_root}/scripts/post_review.py" "{artifactPaths.postReview}" \
  --report "{artifactPaths.report}" --owner "{owner}" --repo "{repo}" \
  --pr-number "{pr_number}" --platform "{platform}" --web-origin "{web_origin}" --sha "{full_head_sha}"
```

The `{web_origin}` placeholder comes from `delivery.prIdentity.web_origin`.

> Headless carve-out (`configResult.resolved.post_mode == "dry-run"`): append `--dry-run` to the `post_review.py` invocation so it captures the payload instead of posting. `post_review.py` self-enforces this regardless — it reads `CODE_GAUNTLET_POST_MODE` directly and treats `dry-run` as `--dry-run` even when the flag is omitted (belt-and-braces) — but pass the flag explicitly so the dry-run intent is visible in the command.

See `references/delivery-guide.md` for the findings JSON schema and validation details.

---

## Stage 2: Task Board

**Question 2 of 2 — and the last question of the run.** One call, Yes/No shaped. Never a per-finding loop.

> Headless mode exception: the task board is skipped — present no
> `AskUserQuestion` and create no tasks. See `references/headless-mode.md`.

The question itself is the block under the Phase 8 MANDATORY GATE in SKILL.md; it is asked there, verbatim.

"Yes" creates one FIX task for every finding in the delivered set. When `artifactPaths.postReview` is
null, the delivery set was not persisted, so do not present the task-board offer and create no tasks.

Run this command after a Yes answer:

```bash
{python} "{plugin_root}/scripts/render_fix_tasks.py" "<artifactPaths.postReview>" --repo-root "<repoRoot>"
```

Parse stdout as a JSON array and assert that its length equals the delivered count. For each object,
call `TaskCreate(subject, description)` and then `TaskUpdate(taskId, metadata)`. Stop on any parse,
count, or call failure and report it. Never fall back to hand composition. After creating, report the
number of created tasks.

The `ignore:` list is user-edited by hand (`references/review-md-spec.md` →
Ignore); no Phase 8 write path exists.

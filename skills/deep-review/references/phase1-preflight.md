# Phase 1 Pre-Flight Reference

Workflow-tool availability check, review target resolution, eligibility logic, AskUserQuestion templates, and consolidated pre-flight configuration gate for Phase 1.

> **Note:** SHA resolution (`git rev-parse --short=8 HEAD` → `head_sha_short`) and gitignore check (`git check-ignore`) happen in Phase 2 after checkout — see `phase2-triage.md` section 2b-post. Phase 1 only runs the availability check, resolves the output directory, and runs `mkdir -p`.

---

## Workflow-Tool Availability Check (run first)

v3 orchestration is a single `Workflow` tool call (Phase 3). There is no in-session fallback — the break from v2's inline subagent dispatch is a locked decision. Before any other work, confirm the **`Workflow` tool is present in this session's available tools**.

- **Present** → continue with target resolution.
- **Absent** → print exactly the message below and STOP. Do not emulate the pipeline by dispatching agents inline.

```
deep-review v3 requires Claude Code >= 2.1.154 with dynamic workflows. Install deep-review v2.x for older CLIs.
```

This is the Phase-0 test-13 recipe: the session inspects its own tool registry for `Workflow` before dispatch. The check is identical in interactive and headless modes.

---

## Resolve Review Target

The user's input determines the review target. Resolve it before eligibility checks — the target type affects every subsequent step. The ARGUMENTS value from the skill invocation is the user's explicit input — a bare number (e.g., `1`, `42`) is always a PR/MR number and must be resolved via `gh pr view` before considering any other target type. Do not compare it against the branch name or second-guess it; the branch may track a different upstream PR.

**Input → target resolution (check in this order):**

1. **User passed a PR/MR number** (e.g., `/deep-review 42`, `review PR 42`, `review #42`, or ARGUMENTS: `1`) → **PR/MR mode**. Store the number as `pr_number`. Use this number for all `gh pr` / `glab mr` commands. Do NOT extract numbers from branch names — the branch name may contain the upstream PR number which differs from the PR number in the current repo.
2. **User passed a URL** (e.g., `github.com/.../pull/42`) → **PR/MR mode**. Extract `pr_number` from the URL path.
3. **User said "review" with no number/URL** and a PR/MR exists for the current branch → **PR/MR mode**. Use `gh pr view --json number --jq '.number'` to get the number for the current branch.
4. **No PR/MR found** → **Local changes mode**. Review uncommitted changes or branch diff.

**Validation:** After resolving to PR/MR mode, verify the PR/MR exists by running `gh pr view {pr_number}` (or `glab mr view`). If the command fails, do NOT silently fall back to local mode — ask the user:

> Headless exception (`DEEP_REVIEW_HEADLESS=1`): do not present this `AskUserQuestion`. Apply `DEEP_REVIEW_PR_NOT_FOUND_POLICY` — `error` stops the run, `local` proceeds as a local review with `pr_number` cleared. See `references/headless-mode.md`.

```
AskUserQuestion(
  questions: [{
    question: "Could not find PR #{pr_number} on this repository. The PR may not exist, or the number may be wrong. How should I proceed?",
    header: "PR Not Found",
    multiSelect: false,
    options: [
      { label: "Proceed as local review", description: "Review the branch diff without PR integration (no PR comments)" },
      { label: "Try a different number", description: "I'll provide the correct PR number" },
      { label: "Cancel", description: "Stop the review" }
    ]
  }]
)
```

If the user provides a different number, re-resolve with the corrected value. If they choose local review, set `target_type` to `local` and clear `pr_number`.

Store the resolved `target_type` (`pr`, `mr`, or `local`) and `pr_number` for use in all subsequent phases.

---

## Eligibility Checks

> Headless exception (`DEEP_REVIEW_HEADLESS=1`): none of the `AskUserQuestion` gates in this section are presented. The draft gate applies `DEEP_REVIEW_DRAFT_POLICY` (`review` proceeds, `skip` stops); both previously-reviewed variants apply `DEEP_REVIEW_REVIEWED_POLICY` (`incremental` / `full` / `skip`). Closed/merged does **not** stop the run headless — it proceeds against the pinned head exactly as resolved (benchmarking historical merged PRs is the headless use case; posting safety is governed by `DEEP_REVIEW_POST_MODE`, and `dry-run` posts nothing). Trivial-only-changes still stops deterministically. See `references/headless-mode.md`.

1. **Closed/merged?** — Stop: "This PR is already closed/merged. No review needed."

2. **Draft?** — Warn and ask:

   ```
   AskUserQuestion(
     questions: [{
       question: "This PR is marked as draft. Do you still want a full review?",
       header: "Draft PR",
       multiSelect: false,
       options: [
         { label: "Yes — review it", description: "Run the full review on this draft" },
         { label: "No — skip", description: "Don't review until it's ready" }
       ]
     }]
   )
   ```

3. **Previously reviewed?** — Check PR/MR comments for the `Generated by deep-review` footer. Parse `Reviewed up to: {sha}` to find the last reviewed commit SHA.
   - If new commits exist since last review:

     ```
     AskUserQuestion(
       questions: [{
         question: "This PR was previously reviewed at commit {short_sha}. {N} new commits have been pushed since. How would you like to proceed?",
         header: "Previously Reviewed",
         multiSelect: false,
         options: [
           { label: "Incremental — only changes since last review", description: "Review new commits only" },
           { label: "Full — review entire PR from scratch", description: "Start fresh" },
           { label: "Skip — don't review again", description: "No review needed" }
         ]
       }]
     )
     ```

     If **Incremental**: use `git diff {last_reviewed_sha}...HEAD`, parse `deep-review-findings` hidden HTML comment from previous review for report diffing (see Phase 7, post-challenge finalization).
   - If no new commits:

     ```
     AskUserQuestion(
       questions: [{
         question: "This PR was already reviewed and no new commits have been pushed. Review again with fresh eyes?",
         header: "Previously Reviewed",
         multiSelect: false,
         options: [
           { label: "Yes — review again", description: "Run a fresh review" },
           { label: "No — skip", description: "Keep the existing review" }
         ]
       }]
     )
     ```

4. **Trivially simple?** — If ONLY lockfile/generated/auto-formatted changes with no logic modifications, stop.

---

## Pre-Flight Configuration Gate

> **STOP: Complete this gate before Phase 2.** Do not skip or assume defaults — this includes preferences remembered from prior sessions or memory. Preferences change between reviews; asking takes 5 seconds, a wrong assumption wastes the entire review.
>
> Headless exception (`DEEP_REVIEW_HEADLESS=1`): the entire gate below resolves deterministically — `model_tier` and `default_delivery` come from the environment (env > REVIEW.md explicit > headless default) per `references/headless-mode.md`, no REVIEW.md-setup question is asked, and the `Headless config:` block replaces every `AskUserQuestion` in this section (resolution logic, question templates, confirmation-only template, and combined-call example). Do not present any `AskUserQuestion`.

### Resolution logic

1. **Quick-check root REVIEW.md** for `model_tier` and `default_delivery`. Only explicitly set values count (not comments or examples).
2. **Build a questions array** containing only the items not already resolved by REVIEW.md:

| Config key | Resolved when | Question if unresolved |
|---|---|---|
| `model_tier` | REVIEW.md sets it explicitly | Review mode question (see template below) |
| `default_delivery` | REVIEW.md sets it explicitly | Delivery preference question (see template below) |
| REVIEW.md presence | REVIEW.md exists in repo root | REVIEW.md setup question (see template below) |

3. **Dispatch based on how many questions remain:**

| Unresolved count | Action |
|---|---|
| **0** | Still present AskUserQuestion with a single confirmation question (see "Confirmation-only template" below). Never skip — Phase 2 checks that AskUserQuestion was called. |
| **1-3** | Single AskUserQuestion with all unresolved items in the `questions` array. |

> Headless exception (`DEEP_REVIEW_HEADLESS=1`): neither the "0" nor the "1-3" row applies — dispatch no `AskUserQuestion` regardless of how many items REVIEW.md leaves unresolved. Headless defaults fill any gap and the `Headless config:` block records the resolution; Phase 2's entry check is satisfied by that block, not by an `AskUserQuestion` call.

### Question templates

**Review mode** (when `model_tier` not set in REVIEW.md):

```
{
  question: "Which review mode?",
  header: "Review Mode",
  multiSelect: false,
  options: [
    { label: "Optimized", description: "Sonnet for most agents, Opus for security. Faster and ~40% cheaper." },
    { label: "Frontier", description: "Frontier model behind the frontier flag (challenger upgraded). Maximum depth for high-stakes reviews." }
  ]
}
```

**This answer sets the workflow's model policy (the `frontier` flag).** It is not cosmetic — it is threaded into `args.policy` in Phase 2:

- **Optimized** → `policy.tier = "optimized"`, `policy.frontier = false`, `policy.frontierModelId = null`.
- **Frontier** → `policy.tier = "frontier"`, `policy.frontier = true`, and `policy.frontierModelId` **must** be set to a full model-id string. The workflow's `validateArgs` rejects `frontier: true` with a missing `frontierModelId` (no silent alias fallback — the Fable alias is Phase-0-deferred). The `frontier` flag currently upgrades the **challenger** stage only; discovery security-reviewer stays Opus and the other stages stay Sonnet.

**Delivery preference** (when `default_delivery` not set in REVIEW.md):

```
{
  question: "How should I deliver the review results?",
  header: "Delivery",
  multiSelect: true,
  options: [
    { label: "Chat (Recommended)", description: "Full report in the conversation" },
    { label: "PR comments", description: "Inline comments on the PR" },
    { label: "Markdown file", description: "Save as deep-review-{date}.md" }
  ]
}
```

When the review target is local changes (not a PR/MR), omit the "PR comments" option.

**REVIEW.md setup** (only when no REVIEW.md found in repo root):

```
{
  question: "No REVIEW.md found. Want to create one? It pre-configures review settings so you get zero questions next time.",
  header: "REVIEW.md Setup",
  multiSelect: false,
  options: [
    { label: "Skip for now", description: "Continue without REVIEW.md" },
    { label: "Create one after review", description: "I'll offer to generate it at the end" }
  ]
}
```

### Confirmation-only template (when REVIEW.md pre-configures both `model_tier` and `default_delivery`)

```
AskUserQuestion(
  questions: [{
    question: "Ready to start. REVIEW.md configured: [mode] mode, delivering via [method]. Proceed?",
    header: "Review Configuration",
    multiSelect: false,
    options: [
      { label: "Yes — start review", description: "Proceed with the configured settings" },
      { label: "No — change settings", description: "I'll answer the full configuration questions instead" }
    ]
  }]
)
```

If "No — change settings": clear REVIEW.md-resolved values and re-run the gate with all three questions (mode, delivery, REVIEW.md setup).

### Combined call example (worst case — nothing pre-configured, no REVIEW.md)

```
AskUserQuestion(
  questions: [
    { question: "Which review mode?", header: "Review Mode", multiSelect: false, options: [
        { label: "Optimized", description: "Sonnet for most agents, Opus for security. Faster and ~40% cheaper." },
        { label: "Frontier", description: "All Opus agents. Maximum depth for high-stakes reviews." }
    ]},
    { question: "How should I deliver the review results?", header: "Delivery", multiSelect: true, options: [
        { label: "Chat (Recommended)", description: "Full report in the conversation" },
        { label: "PR comments", description: "Inline comments on the PR" },
        { label: "Markdown file", description: "Save as deep-review-{date}.md" }
    ]},
    { question: "No REVIEW.md found. Want to create one?", header: "REVIEW.md Setup", multiSelect: false, options: [
        { label: "Skip for now", description: "Continue without REVIEW.md" },
        { label: "Create one after review", description: "I'll offer to generate it at the end" }
    ]}
  ]
)
```

Store the delivery selection for Phase 8. Confirm all resolved settings in output before continuing.

---

## Light Review Template (Phase 2d)

> **Note:** This template is triggered during Phase 2d (risk classification). It lives here because it is a pre-flight UX decision — the user's answer affects what review dimensions run, so it is collected alongside the other pre-flight gates.

Used when ALL files are low-risk AND total lines <50:

> Headless exception (`DEEP_REVIEW_HEADLESS=1`): do not present this `AskUserQuestion`. Apply `DEEP_REVIEW_TRIVIAL_SCOPE` — `light` runs bugs+security only, `full` runs all dimensions. See `references/headless-mode.md`.

```
AskUserQuestion(
  questions: [{
    question: "This is a small, low-risk change ({N} files, {M} lines, all low-risk). How thorough should the review be?",
    header: "Review Scope",
    multiSelect: false,
    options: [
      { label: "Light review (Recommended)", description: "Bugs + security only (faster, 2 agents)" },
      { label: "Full review", description: "All dimensions (5-7 agents)" }
    ]
  }]
)
```

Skipped when REVIEW.md sets `focus`. In light mode, triage announcement shows `Review dimensions: bugs, security (light review mode)`.

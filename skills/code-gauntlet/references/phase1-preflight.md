# Phase 1 Pre-Flight Reference

Workflow-tool availability check, review target resolution, eligibility logic, AskUserQuestion templates, and consolidated pre-flight configuration gate for Phase 1.

> **Note:** SHA resolution (`git rev-parse --short=8 HEAD` → `head_sha_short`) and gitignore check (`git check-ignore`) happen in Phase 2 after checkout — see `phase2-triage.md` section 2b-post. Phase 1's own Bash work is the single composite call in SKILL.md (output directory + `mkdir -p`, plugin-dir confirmation, PR state, a root REVIEW.md quick-check, and the trivial-check file list) — no checkout-dependent state.

---

## Workflow-Tool Availability Check (run first)

v3 orchestration is a single `Workflow` tool call (Phase 3). There is no in-session fallback — the break from v2's inline subagent dispatch is a locked decision. Before any other work, confirm the **`Workflow` tool is present in this session's available tools**.

- **Present** → continue with target resolution.
- **Absent** → print exactly the message below and STOP. Do not emulate the pipeline by dispatching agents inline.

```
code-gauntlet v3 requires Claude Code >= 2.1.154 with dynamic workflows. Install code-gauntlet v2.x for older CLIs.
```

This is the Phase-0 test-13 recipe: the session inspects its own tool registry for `Workflow` before dispatch. The check is identical in interactive and headless modes.

---

## Resolve Review Target

The user's input determines the review target. Resolve it before eligibility checks — the target type affects every subsequent step. The ARGUMENTS value from the skill invocation is the user's explicit input — a bare number (e.g., `1`, `42`) is always a PR/MR number and must be resolved via `gh pr view` before considering any other target type. Do not compare it against the branch name or second-guess it; the branch may track a different upstream PR.

**Input → target resolution (check in this order):**

1. **User passed a PR/MR number** (e.g., `/code-gauntlet 42`, `review PR 42`, `review #42`, or ARGUMENTS: `1`) → **PR/MR mode**. Store the number as `pr_number`. Use this number for all `gh pr` / `glab mr` commands. Do NOT extract numbers from branch names — the branch name may contain the upstream PR number which differs from the PR number in the current repo.
2. **User passed a URL** (e.g., `github.com/.../pull/42`) → **PR/MR mode**. Extract `pr_number` from the URL path.
3. **User said "review" with no number/URL** and a PR/MR exists for the current branch → **PR/MR mode**. Use `gh pr view --json number --jq '.number'` to get the number for the current branch.
4. **No PR/MR found** → **Local changes mode**. Review uncommitted changes or branch diff.

**Validation:** After resolving to PR/MR mode, verify the PR/MR exists by running `gh pr view {pr_number}` (or `glab mr view`). If the command fails, do NOT silently fall back to local mode — ask the user:

> Headless exception (`CODE_GAUNTLET_HEADLESS=1`): do not present this `AskUserQuestion`. Apply `CODE_GAUNTLET_PR_NOT_FOUND_POLICY` — `error` stops the run, `local` proceeds as a local review with `pr_number` cleared. See `references/headless-mode.md`.

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

> Headless exception (`CODE_GAUNTLET_HEADLESS=1`): none of the `AskUserQuestion` gates in this section are presented. The draft gate applies `CODE_GAUNTLET_DRAFT_POLICY` (`review` proceeds, `skip` stops); the previously-reviewed gate — which runs in Phase 2 after checkout, see below — applies `CODE_GAUNTLET_REVIEWED_POLICY` (`incremental` / `full` / `skip`). Closed/merged does **not** stop the run headless — it proceeds against the pinned head exactly as resolved (benchmarking historical merged PRs is the headless use case; posting safety is governed by `CODE_GAUNTLET_POST_MODE`, and `dry-run` posts nothing). Trivial-only-changes still stops deterministically. See `references/headless-mode.md`.

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

3. **Previously reviewed?** — **Deferred to Phase 2**, immediately after checkout. See "Previously-Reviewed Gate" below for the full gate; `phase2-triage.md` section 2b-post step 3 is where it runs. The reason is the same one that moves head-SHA resolution out of Phase 1 (see the note at the top of this file): the gate compares the last-reviewed commit against the PR head and counts the commits between them, and before `gh pr checkout` the working tree is on whatever branch the session started on — often with the PR's objects not even fetched. Running it here would compare against the wrong tree and silently mis-gate the incremental path.

4. **Trivially simple?** — If ONLY lockfile/generated/auto-formatted changes with no logic modifications, stop. The file list to check comes from the Phase 1 composite call in SKILL.md (`changed_files` section) — for PR/MR mode that's `gh pr diff {pr_number} --name-only` (there is no `gh pr diff --stat`; that flag does not exist and the command fails). For branch/local targets use `git diff --name-only <base>...HEAD` / `git diff --name-only HEAD`. Never issue a second call to re-fetch this list — it is already on hand from the composite.

---

## Previously-Reviewed Gate

> Runs in **Phase 2, section 2b-post step 3** — after checkout, never in Phase 1. Listed here with the other pre-flight templates because it is a pre-flight UX decision.

Skip entirely when `target_type == local` (no PR/MR ⇒ no signal to detect). Otherwise run the detector. `{platform}` is `github` or `gitlab` per the resolved target type. Resolve `{owner}`/`{repo}` first — they are the PR's repository, not necessarily the `origin` remote, which points at the fork in a fork clone:

```bash
gh pr view {pr_number} --json url --jq '.url | split("/") | .[3] + "/" + .[4]'   # owner/repo
```

Split the result on the first `/` into `owner`/`repo`. This reads the PR's **own URL**, which always lives on the base repository — `gh repo view` would resolve the *current* clone, which in a fork is the fork, exactly the case the explicit flags exist to fix. (GitLab: `glab mr view {pr_number} --output json | jq -r '.web_url'`, then take the path segments before `/-/merge_requests/`.)

```bash
python3 "{plugin_root}/scripts/detect_prior_review.py" --platform {platform} --owner {owner} --repo {repo} --number {pr_number} --head-sha {head_sha_full}
```

It always exits 0 and prints exactly one JSON object on stdout (detection degrades, never blocks the review):

```json
{
  "previously_reviewed": true,
  "signal": "marker",
  "source": "review",
  "legacy": false,
  "last_reviewed_sha": "<full>",
  "last_reviewed_sha_short": "<8>",
  "sha_resolvable": true,
  "sha_is_ancestor": true,
  "head_sha": "<full>",
  "head_advanced": true,
  "new_commit_count": 3,
  "incremental_safe": true,
  "marker": { "...": "allow-listed keys, bounded; unknown keys named under unknown_keys" },
  "scanned": { "review": 4 },
  "errors": []
}
```

`marker` is not a verbatim echo of the parsed payload — `sanitize_marker` allow-lists which keys it returns (`version`, `findings_count`, `sha`, `findings`, `_token`, `_legacy`), bounds every value, and reports any other key by NAME only under `unknown_keys` (values never echoed). The parsed marker is attacker-controllable — anyone with read access can post a comment carrying one — and the orchestrator is told to consume `marker` directly, so an unbounded verbatim echo would pipe arbitrary text into a model's context.

When nothing is found, `previously_reviewed` is `false` with `signal`/`source`/`marker`/`last_reviewed_sha` `null` and `incremental_safe` `false`.

`incremental_safe` is the single boolean that gates whether the incremental path may be offered. Branch in this exact order — each earlier case also satisfies a later case's condition, so checking out of order mis-classifies it: `sha_resolvable` before "no new commits" (an unresolvable SHA also reports `head_advanced: false` and would otherwise be announced as "no new commits" when commits may well have been pushed); `sha_is_ancestor` before "no new commits" (a rewritten history — rebase, squash, or a backward force-push — also reports `head_advanced: false`, since `head_advanced` requires `sha_is_ancestor`, so unchecked it reads identically to "nothing changed"). `{short_sha}` below is `last_reviewed_sha_short`:

- `previously_reviewed == false` → continue, no question asked.
- `previously_reviewed == true` and `sha_resolvable == false` → present **neither** template. Disclose that a prior review was found but its commit is not present locally (force-push, shallow clone, or unfetched object) and continue as a full review.
- `previously_reviewed == true`, `sha_resolvable == true`, `sha_is_ancestor == false` → present **neither** template. History was rewritten since the last review (rebase, squash, or a backward force-push): the reviewed commit still exists locally but is no longer an ancestor of HEAD, so the commits cannot be compared. Disclose that the PR was previously reviewed at `{short_sha}` but its history has since been rewritten, and continue as a full review. `new_commit_count` counts commits on the diverged history here — it is meaningless and must not be quoted to the user.
- `incremental_safe == true` → ask. Fill `{N}` from `new_commit_count`; if it is `0` or `null`, drop the count clause and say "new commits have been pushed since" rather than rendering "0 new commits":

  ```
  AskUserQuestion(
    questions: [{
      question: "This PR was previously reviewed at commit {short_sha}. {N} new commits have been pushed since. How would you like to proceed?",
      header: "Previously Reviewed",
      multiSelect: false,
      options: [
        { label: "Incremental — only changes since last review", description: "Review new commits only" },
        { label: "Full — review entire PR from scratch", description: "Start fresh" },
        { label: "Skip — don't review again", description: "No review needed — the working tree stays checked out on this PR; nothing is reverted" }
      ]
    }]
  )
  ```

  If **Incremental**: store `last_reviewed_sha` — Phase 2 2c's incremental diff branch uses it. The marker payload is already parsed and available under the JSON's `marker` key above; do not re-parse the footer or hand-write a regex.
- `previously_reviewed == true`, `sha_resolvable == true`, `sha_is_ancestor == true`, `head_advanced == false` (the reviewed commit IS the head — genuinely no new commits) → ask:

  ```
  AskUserQuestion(
    questions: [{
      question: "This PR was already reviewed and no new commits have been pushed. Review again with fresh eyes?",
      header: "Previously Reviewed",
      multiSelect: false,
      options: [
        { label: "Yes — review again", description: "Run a fresh review" },
        { label: "No — skip", description: "Keep the existing review — the working tree stays checked out on this PR; nothing is reverted" }
      ]
    }]
  )
  ```

**Degradations** (state explicitly, never block the review):

- `errors` non-empty → disclose "prior-review detection unavailable ({reason})" and continue as a fresh review. `errors` can be non-empty alongside a successful detection — report it either way.
- Never treat a detection failure as a reason to stop the run: the detector exits 0 for every outcome, and a review that cannot check its own history is still a valid full review.

**Boundary:** the signal exists only when a prior review actually posted to the PR/MR. A chat-only or markdown-only prior run writes nothing to the PR/MR, so a rerun is correctly detected as fresh.

---

## Pre-Flight Configuration Gate

> **STOP: Complete this gate before Phase 2.** Do not skip or assume defaults — this includes preferences remembered from prior sessions or memory. Preferences change between reviews; asking takes 5 seconds, a wrong assumption wastes the entire review.
>
> Headless exception (`CODE_GAUNTLET_HEADLESS=1`): the entire gate below resolves deterministically — `model_tier`, `default_delivery`, and `delivery_tier` (from `CODE_GAUNTLET_DELIVERY_TIER`, default `all`) come from the environment (env > REVIEW.md explicit > headless default) per `references/headless-mode.md`, no REVIEW.md-setup question is asked, and the `Headless config:` block replaces every `AskUserQuestion` in this section (resolution logic, question templates, confirmation-only template, and combined-call example). Do not present any `AskUserQuestion`. Thread the resolved `delivery_tier` into `args.delivery.tier`.

### Resolution logic

1. **Quick-check root REVIEW.md** for `model_tier` and `default_delivery`. Only explicitly set values count (not comments or examples).
2. **Build a questions array** containing only the items not already resolved by REVIEW.md:

| Config key | Resolved when | Question if unresolved |
|---|---|---|
| `model_tier` | Always resolved — fixed to `optimized`, the single benchmarked policy (a legacy REVIEW.md value self-heals to `optimized` with a loud warning; an env value other than `optimized` fails loud; alternate modes are roadmap #17) | Never asked |
| `default_delivery` | REVIEW.md sets it explicitly | Delivery preference question (see template below) |
| `delivery_tier` | Defaults to `all` (Recommended) — no REVIEW.md key, like the other policy knobs | Delivery-tier question (see template below), asked alongside the delivery question when the target is a PR/MR |
| REVIEW.md presence | REVIEW.md exists in repo root | REVIEW.md setup question (see template below) |

3. **Dispatch based on how many questions remain:**

| Unresolved count | Action |
|---|---|
| **0** | Still present AskUserQuestion with a single confirmation question (see "Confirmation-only template" below). Never skip — Phase 2 checks that AskUserQuestion was called. |
| **1-3** | Single AskUserQuestion with all unresolved items in the `questions` array. |

> Headless exception (`CODE_GAUNTLET_HEADLESS=1`): neither the "0" nor the "1-3" row applies — dispatch no `AskUserQuestion` regardless of how many items REVIEW.md leaves unresolved. Headless defaults fill any gap and the `Headless config:` block records the resolution; Phase 2's entry check is satisfied by that block, not by an `AskUserQuestion` call.

### Question templates

**Model policy** (never asked): `policy.tier = "optimized"` always — discovery agents on Sonnet with security-reviewer on Opus, the single configuration the benchmark numbers were measured under. A **REVIEW.md** `Model Tier` value other than `optimized` (legacy v2-era files in the wild say `frontier`) **self-heals to `optimized`** — never ask, never abort on this field, in both interactive and headless modes; print a loud methodology warning naming the ignored value and recommending a REVIEW.md update, and record it in the report methodology. An **env** `CODE_GAUNTLET_MODEL_TIER` value other than `optimized` keeps the fail-loud contract (an explicit operator pin is an error, not a preference to heal). Alternate model modes (fable) are roadmap work tracked in issue #17 and land under the ratcheted measurement policy in `bench/MEASUREMENT.md` (owner-triggered paired measurement when recall/noise may move — not a default gate for every behavior change).

**Delivery preference** (when `default_delivery` not set in REVIEW.md):

```
{
  question: "How should I deliver the review results?",
  header: "Delivery",
  multiSelect: true,
  options: [
    { label: "Chat (Recommended)", description: "Full report in the conversation" },
    { label: "PR comments", description: "Inline comments on the PR" },
    { label: "Markdown file", description: "Save as code-gauntlet-{date}.md" }
  ]
}
```

When the review target is local changes (not a PR/MR), omit the "PR comments" option.

**Delivery tier** (which challenge-survivors post as PR comments — asked alongside the delivery preference when the target is a PR/MR; omit for local reviews, which have no PR to post to):

```
{
  question: "Which findings should post as PR comments?",
  header: "PR Comment Tier",
  multiSelect: false,
  options: [
    { label: "All challenge-surviving findings (Recommended)", description: "Post every finding that survived the blind challenge, including improvement suggestions" },
    { label: "Main findings only", description: "Post bugs/security/correctness findings; suggestions stay in the report, not posted" }
  ]
}
```

**This answer threads into `args.delivery.tier` (Phase 2), consumed by the workflow's `selectDelivery`:** "All challenge-surviving findings" → `args.delivery.tier = "all"` (the default — the pipeline posts every survivor regardless of tag); "Main findings only" → `args.delivery.tier = "main_only"` (the pipeline keeps only main-tagged survivors, so suggestions render in the report but are not posted). If this question is not presented (target is local, or the delivery preference came from REVIEW.md so no delivery question runs), default `args.delivery.tier = "all"`. The tier only affects PR-comment inclusion; it never changes the report, which always shows every finding.

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

### Confirmation-only template (when REVIEW.md pre-configures `default_delivery`)

```
AskUserQuestion(
  questions: [{
    question: "Ready to start. REVIEW.md configured: delivering via [method]. Proceed?",
    header: "Review Configuration",
    multiSelect: false,
    options: [
      { label: "Yes — start review", description: "Proceed with the configured settings" },
      { label: "No — change settings", description: "I'll answer the full configuration questions instead" }
    ]
  }]
)
```

If "No — change settings": clear REVIEW.md-resolved values and re-run the gate with the remaining questions (delivery, delivery tier, REVIEW.md setup).

### Combined call example (worst case — nothing pre-configured, no REVIEW.md)

```
AskUserQuestion(
  questions: [
    { question: "How should I deliver the review results?", header: "Delivery", multiSelect: true, options: [
        { label: "Chat (Recommended)", description: "Full report in the conversation" },
        { label: "PR comments", description: "Inline comments on the PR" },
        { label: "Markdown file", description: "Save as code-gauntlet-{date}.md" }
    ]},
    { question: "Which findings should post as PR comments?", header: "PR Comment Tier", multiSelect: false, options: [
        { label: "All challenge-surviving findings (Recommended)", description: "Post every finding that survived the blind challenge, including improvement suggestions" },
        { label: "Main findings only", description: "Post bugs/security/correctness findings; suggestions stay in the report, not posted" }
    ]},
    { question: "No REVIEW.md found. Want to create one?", header: "REVIEW.md Setup", multiSelect: false, options: [
        { label: "Skip for now", description: "Continue without REVIEW.md" },
        { label: "Create one after review", description: "I'll offer to generate it at the end" }
    ]}
  ]
)
```

Store the delivery selection and the delivery tier (`args.delivery.tier`, default `all`) for Phase 8. Confirm all resolved settings in output before continuing.

---

## Light Review Template (Phase 2d)

> **Note:** This template is triggered during Phase 2d (risk classification). It lives here because it is a pre-flight UX decision — the user's answer affects what review dimensions run, so it is collected alongside the other pre-flight gates.

> A `light` answer stamps `args.agentFlags = { deep: false }`, which the Discover stage honours by dispatching only the two core agents (`bug-detector`, `security-reviewer`); `full` stamps `{}` and runs all seven. Announce the actual dimensions that will run.

Used when ALL files are low-risk AND total lines <50:

> Headless exception (`CODE_GAUNTLET_HEADLESS=1`): do not present this `AskUserQuestion`. Apply `CODE_GAUNTLET_TRIVIAL_SCOPE` — `light` runs bugs+security only, `full` runs all dimensions. See `references/headless-mode.md`.

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

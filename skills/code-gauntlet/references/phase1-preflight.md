# Phase 1 Pre-Flight Reference

## Contents

- [Workflow-Tool Availability Check (run first)](#workflow-tool-availability-check-run-first)
- [Resolve Review Target](#resolve-review-target)
- [Draft Review Template](#draft-review-template)
- [Phase 1 composite](#phase-1-composite-output-dir-plugin-confirmation-config-eligibility)
- [Previously-Reviewed Gate](#previously-reviewed-gate)
- [Configuration Resolution (no questions)](#configuration-resolution-no-questions)
- [Light Review Template (Phase 2e)](#light-review-template-phase-2e)

Workflow-tool availability check, review target resolution, the error-path AskUserQuestion templates, and question-free configuration resolution for Phase 1.

> **Note:** SHA resolution (`git rev-parse --short=8 HEAD` → `head_sha_short`) happens in Phase 2 after checkout — see `phase2-triage.md` section 2b-post. Output-directory resolve/ignore/mkdir is owned by `scripts/ensure_output_dir.py` in the composite below — not Phase 2. Phase 1's own Bash work is that composite (ensure_output_dir, plugin confirmation, config resolution, and eligibility) — no checkout-dependent state.

---

## Workflow-Tool Availability Check (run first)

Inspect the session tool definitions for `Workflow` before other work. `ToolSearch` searches deferred tools and can miss a top-level `Workflow`. There is no in-session fallback; never emulate the pipeline inline.

- **No Bash tool (PowerShell only)** -> print `Install Git for Windows to run code-gauntlet in Bash.` and STOP.
- **Present in your tool definitions** → continue with target resolution.
- **Absent from your tool definitions** → print exactly the message below and STOP. Do not emulate the pipeline by dispatching agents inline.

```
code-gauntlet v3 requires Claude Code >= 2.1.154 with dynamic workflows. Install the pre-rename deep-review v2.x for older CLIs.
```

The same availability check applies in interactive and headless modes.

---

## Resolve Review Target

The user's input determines the review target. Resolve it before eligibility checks — the target type affects every subsequent step. The ARGUMENTS value from the skill invocation is the user's explicit input — a bare number (e.g., `1`, `42`) is always a PR/MR number and must be resolved via `gh pr view` before considering any other target type. Do not compare it against the branch name or second-guess it; the branch may track a different upstream PR.

**Input → target resolution (check in this order):**

1. **User passed a PR/MR number** (e.g., `/code-gauntlet 42`, `review PR 42`, `review #42`, or ARGUMENTS: `1`) → **PR/MR mode**. Store the number as `pr_number`. Use this number for all `gh pr` / `glab mr` commands. Do NOT extract numbers from branch names — the branch name may contain the upstream PR number which differs from the PR number in the current repo.
2. **User passed a URL** (e.g., `github.com/.../pull/42`) → **PR/MR mode**. Extract `pr_number` from the URL path.
3. **User said "review" with no number/URL** and a PR/MR exists for the current branch → **PR/MR mode**. Use `gh pr view --json number` and read its `number` field for the current branch.
4. **No PR/MR found** → **Local changes mode**. Review uncommitted changes or branch diff.

**Validation:** After resolving to PR/MR mode, verify the PR/MR exists by running `gh pr view {pr_number}` (or `glab mr view`). If the command fails, do NOT silently fall back to local mode — ask the user:

> Headless exception (`CODE_GAUNTLET_HEADLESS=1`): call `scripts/resolve_config.py` with no `--target` on this path. The question is never presented in headless mode. Branch on `resolved.pr_not_found_policy`: `error` stops the run, and `local` proceeds with `pr_number` cleared. See `references/headless-mode.md`.

```
AskUserQuestion(
  questions: [{
    question: "Could not find PR #{pr_number} on this repository. The PR may not exist, or the number may be wrong. How should I proceed?",
    header: "PR Not Found",
    multiSelect: false,
    options: [
      { label: "Proceed as local review", description: "Review locally without PR comments" },
      { label: "Cancel", description: "Stop the review" }
    ]
  }]
)
```

If the user provides a different number, re-resolve with the corrected value. If they choose local review, set `target_type` to `local` and clear `pr_number`.

Store the resolved `target_type` (`pr`, `mr`, or `local`) and `pr_number` for use in all subsequent phases.

---

## Draft Review Template

Use this template only for the `ask_draft` verdict from Phase 1's eligibility receipt.

```
AskUserQuestion(
  questions: [{
    question: "This PR is marked as draft. Do you still want a full review?",
    header: "Draft PR",
    multiSelect: false,
    options: [
      { label: "Yes - review it", description: "Run the full review on this draft" },
      { label: "No - skip", description: "Don't review until it's ready" }
    ]
  }]
)
```

---

## Phase 1 composite: output dir, plugin confirmation, config, eligibility

One Bash call sets up the output directory, confirms the plugin, resolves config, and checks PR/MR eligibility. Eligibility consumes the resolved config on stdin.

```bash
PYTHON=()
for candidate in python3 python py; do
  argv=("$candidate")
  if [ "$candidate" = py ]; then argv+=(-3); fi
  if "${argv[@]}" -c 'import sys; raise SystemExit(sys.version_info < (3, 10))' >/dev/null 2>&1; then
    PYTHON=("${argv[@]}")
    break
  fi
done
if [ "${#PYTHON[@]}" -eq 0 ]; then
  echo "Install Python 3.10+ reachable as python3, python, or py -3."
  exit 1
fi
printf 'python=%s\n' "${PYTHON[*]}"
echo "=== output_dir ==="
if ! OUTPUT_DIR=$("${PYTHON[@]}" "{plugin_root}/scripts/ensure_output_dir.py"); then
  echo "output_dir: FAILED"
  exit 1
fi
printf '%s\n' "$OUTPUT_DIR"

echo "=== plugin_dirs ==="
command ls "{plugin_root}/scripts" "{plugin_root}/agents" "{plugin_root}/workflows"

echo "=== config ==="
if ! CONFIG_JSON=$("${PYTHON[@]}" "{plugin_root}/scripts/resolve_config.py" --target {target_type} --plugin-root "{plugin_root}"); then
  echo "config: FAILED"
  exit 1
fi
printf '%s\n' "$CONFIG_JSON"

echo "=== eligibility ==="
if ! ELIGIBILITY_JSON=$(printf '%s' "$CONFIG_JSON" | "${PYTHON[@]}" "{plugin_root}/scripts/check_eligibility.py" --platform github --pr {pr_number}); then
  echo "eligibility: FAILED"
  exit 1
fi
printf '%s\n' "$ELIGIBILITY_JSON"
```

For GitLab, replace the eligibility section with:

```bash
if ! ELIGIBILITY_JSON=$(printf '%s' "$CONFIG_JSON" | "${PYTHON[@]}" "{plugin_root}/scripts/check_eligibility.py" --platform gitlab --pr {pr_number}); then
  echo "eligibility: FAILED"
  exit 1
fi
printf '%s\n' "$ELIGIBILITY_JSON"
```

Local and branch targets omit the eligibility section. Their changed files are resolved in Phase 2.

**Output-dir hard stops** (same severity class — stop before any agent dispatch; script stderr already flowed into this Bash result with disclosures/remedy lines):

| Marker | Script exit | Meaning |
| --- | --- | --- |
| `output_dir: FAILED` | **1** | Cannot establish ignore for an in-repo dir (`info/exclude` unwritable/unresolvable and not otherwise ignored), or `mkdir` failed. Empty stdout — do not stamp `args.outputDir`. Remedy (ignore case): set `$CODE_GAUNTLET_OUTPUT_DIR` outside the repo and re-run. |
| `output_dir: FAILED` | **2** | Usage: not a git repo, empty/whitespace `$CODE_GAUNTLET_OUTPUT_DIR`, output dir equals repo root, repo or resolved output path contains shell-active/control characters, or `git check-ignore` error (exit 128). Empty stdout — do not stamp. |

On success, stdout is one absolute path line — store it as `{output_dir}` / `args.outputDir`. Ignore establishment (including `.git/info/exclude` append when needed) is owned by `ensure_output_dir.py` in this call; Phase 2 does not re-run it.

Store `python` from the first line as one of `python3`, `python`, or `py -3`.
Substitute `{python}` with those fixed argv words in every later command; quote each word separately.
Copy it into `args.python` and pass `--python "{python}"` to the awaiter.
Store: `output_dir` from section 1.
Store the plugin-dir confirmation from section 2. If a directory is missing, stop because `plugin_root` was resolved wrong.
Store the resolver JSON and stderr block from section 3. Retain them as `configResult`; its stderr block is the Phase 2 gate.
Store the eligibility receipt from section 4 for PR/MR targets.
The resolver owns its configuration inputs.

**Do not resolve the head SHA yet** — it is computed after PR checkout in Phase 2 so the SHA reflects the actual PR HEAD, not whatever branch was checked out when the session started.

### Eligibility checks

For PR/MR targets, branch on the composite's eligibility receipt:

- `proceed`: continue and disclose any non-null `reason`.
- `stop`: print `reason` and end the run.
- `ask_draft`: disclose any non-null `reason` before asking; use the draft template in `references/phase1-preflight.md`; continue only if the user chooses review. This verdict is interactive only.

Local and branch targets continue directly to Phase 2.
The previously-reviewed gate runs after checkout in Phase 2, section 2b-post step 3.

### Resolve configuration

The Phase 1 composite calls `scripts/resolve_config.py`. It reads `CODE_GAUNTLET_HEADLESS` to select `headless` or `interactive` mode.

The resolver owns precedence and validation. It renders the configuration block to stderr in the Bash result under `=== config ===`.

That stderr block is the Phase 2 entry gate. Retain the JSON as `configResult`, including `mode`, `waist`, `resolved`, and `identity`.

Copy `configResult.waist` verbatim into args. For later decisions, use only `configResult.resolved.<knob>`.

The resolver returns its JSON on stdout. A resolver failure stops the composite with `config: FAILED`.

---

## Previously-Reviewed Gate

> Runs in **Phase 2, section 2b-post step 3** — after checkout, never in Phase 1. Listed here with the other pre-flight templates because it is a pre-flight UX decision.

Skip entirely for local and branch targets (no PR/MR signal to detect). Otherwise use the literal forge sections below in Composite A so fork clones use the base repository and its host.

GitHub:

```bash
echo "=== status ==="
PR_STATUS=$({python} "{plugin_root}/scripts/pr_status.py" --platform github --pr {pr_number}) || exit 1
TARGET_SHA=${PR_STATUS%%$'\n'*}
TARGET_SHA=${TARGET_SHA#head_sha=}
```

```bash
echo "=== pr_url ==="
PR_URL=${PR_STATUS#*pr_url=}
printf '%s\n' "$PR_URL"
```

GitLab:

```bash
echo "=== status ==="
PR_STATUS=$({python} "{plugin_root}/scripts/pr_status.py" --platform gitlab --pr {pr_number}) || exit 1
TARGET_SHA=${PR_STATUS%%$'\n'*}
TARGET_SHA=${TARGET_SHA#head_sha=}
```

```bash
echo "=== pr_url ==="
PR_URL=${PR_STATUS#*pr_url=}
printf '%s\n' "$PR_URL"
```

After checkout and SHA resolution, pass the URL unchanged in the same composite:

```bash
{python} "{plugin_root}/scripts/detect_prior_review.py" --platform {platform} --pr-url "$PR_URL" --head-sha {head_sha_full}
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
  "ignored_foreign": 2,
  "errors": []
}
```

`marker` is not a verbatim echo of the parsed payload. `sanitize_marker`
allow-lists its keys (`version`, `findings_count`, `sha`, `_token`, `_legacy`),
bounds every value, and reports other keys by NAME only under `unknown_keys`.
Values of unknown keys are never echoed. The poster's body can contain untrusted
text, so an unbounded echo could pipe arbitrary text into the orchestrator's
context.

Prior-review detection and GitLab delivery deduplication trust only entries written by
this run's posting account. GitHub logins compare without case; GitLab usernames
compare exactly. Missing authors and GitLab system notes are ignored. Summary
markers must end the body; legacy attributed prose must be the last non-blank
line, and finding markers must form the trailing block. `ignored_foreign` counts
entries rejected by the author filter; `scanned` counts accepted bodies by source.

Set `CODE_GAUNTLET_POSTER` to the account used to post reviews when `/user` cannot
resolve it, including Actions' `GITHUB_TOKEN`, GitHub App tokens and GitLab
fine-grained tokens without User:Read. Without a pin or a successful lookup,
detection trusts no entries and reports the identity failure in `errors`; GitLab
posting warns and continues without deduplication. Rotating a GitLab project
access token creates a new bot user, making its earlier notes foreign: expect
one full review and possible duplicate notes. Users with write access who edit
the poster's own body are trusted. Offline `--bodies-file` entries remain trusted.

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
      header: "Reviewed",
      multiSelect: false,
      options: [
        { label: "Incremental", description: "Review new commits only" },
        { label: "Full — review entire PR from scratch", description: "Start fresh" },
        { label: "Skip — don't review again", description: "Stop; leave this PR checked out" }
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
      header: "Reviewed",
      multiSelect: false,
      options: [
        { label: "Yes — review again", description: "Run a fresh review" },
        { label: "No — skip", description: "Keep the review; leave this PR checked out" }
      ]
    }]
  )
  ```

**Degradations** (state explicitly, never block the review):

- `errors` non-empty → disclose "prior-review detection unavailable ({reason})" and continue as a fresh review. `errors` can be non-empty alongside a successful detection — report it either way.
- Never treat a detection failure as a reason to stop the run: the detector exits 0 for every outcome, and a review that cannot check its own history is still a valid full review.

**Boundary:** the signal exists only when a prior review actually posted to the PR/MR. A chat-only or markdown-only prior run writes nothing to the PR/MR, so a rerun is correctly detected as fresh.

---

## Configuration Resolution (no questions)

Configuration resolution asks no questions. The eligibility receipt selects the interactive draft question.
PR-not-found handling and the previously-reviewed gate keep their own templates.
Headless runs resolve every gate without `AskUserQuestion`.

The resolver owns configuration values. The generated block below names the waist fields the workflow derives from its receipt.

| Config key | Resolution |
|---|---|
| `policy.tier` | Copy `configResult.resolved.model_tier`; `validateArgs` accepts only `optimized`. |
| Delivery destination | The resolver owns headless delivery methods. Interactive delivery remains the Phase 8 question after the report exists (`references/phase8-delivery.md` Stage 1). |

<!-- generated-from-registry-identity:derived_waist_fields — do not edit; run scripts/generate_contract_requirements.py -->
The workflow derives these waist fields from the copied `configEcho` receipt. Do not stamp a derived field; when a listed derivation applies, a stamped value that disagrees with its receipt is refused before dispatch.

- `limits.deliveryCap` (headless and interactive runs): from `configEcho.pr_comment_cap`; digits derive as a JSON number; on interactive runs the receipt spelling `null` derives as JSON `null`. Stamp `limits` and leave `deliveryCap` out of it.
- `delivery.tier` (headless and interactive runs): from `configEcho.delivery_tier`. Leave `tier` out of any stamped `delivery`.
- `reviewScope.requested` (headless runs, when `reviewScope.detector` is an object): from `configEcho.reviewed_policy`; `skip` derives as `full`. Stamp `reviewScope` and leave `requested` out of it.
- `scopeAnswer` (headless runs, when every changed file is low risk and fewer than 50 lines changed): from `configEcho.trivial_scope`. Leave it out.

The workflow fills these receipt entries itself; never stamp them.

- `configEcho.review_md` (interactive runs): `present` when the `reviewMd` array is nonempty, else `absent`; without `reviewMd`, `present` when `reviewConfigPath` is set, else `absent`.
<!-- /generated-from-registry-identity:derived_waist_fields -->

Discovery emits the canonical REVIEW.md notice (see `references/review-md-spec.md` -> Discovery).

**Resolved-config echo.** The generated `config_receipt` fence in SKILL.md contains both resolver fixtures. Phase 1 uses the resolver block in the Bash result, and Phase 2 gates on that result.

---

## Light Review Template (Phase 2e)

> **Note:** This template is triggered during Phase 2e (risk classification). It lives here because it is a pre-flight UX decision — the user's answer affects what review dimensions run, so it is collected alongside the other pre-flight gates.

> The answer is stamped verbatim into `args.scopeAnswer` (`"light"` or `"full"`) — the workflow computes the dimension flags itself from `scopeAnswer` plus the Phase 2e `riskTable`/`changedLines` (`deriveAgentFlags`, `workflows/src/discover.js`): `light` dispatches only the two core agents (`bug-detector`, `security-reviewer`); `full` runs all seven discovery agents covering nine dimensions. Announce the actual dimensions that will run.

Used when ALL files are low-risk AND total lines <50:

> Headless exception: do not present this `AskUserQuestion`; leave `scopeAnswer` out. See `references/headless-mode.md`.

```
AskUserQuestion(
  questions: [{
    question: "This is a small, low-risk change ({N} files, {M} lines, all low-risk). How thorough should the review be?",
    header: "Review Scope",
    multiSelect: false,
    options: [
      { label: "Light review (Recommended)", description: "Bugs + security only (faster, 2 agents)" },
      { label: "Full review", description: "All nine dimensions (7 agents)" }
    ]
  }]
)
```

There is no REVIEW.md key that skips this question — dimension selection is not REVIEW.md-configurable (`references/review-md-spec.md` → "Rules and other prose"). In light mode, triage announcement shows `Review dimensions: bugs, security (light review mode)`.
